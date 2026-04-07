"""
Capital.com Trading Bot Dashboard — Flask API server
Run with: python3 dashboard_capital.py
Then open http://localhost:5002 in your browser.
"""

import os
import re
import json
import time
import threading
import secrets
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

import sys
sys.path.insert(0, os.path.dirname(__file__))
from bot_capital import (
    CapitalClient, MARKETS, add_indicators, get_signal,
    run_bot, ACCOUNT_TYPE, LOCK_FILE, BASE_URL,
    _bot_client, SESSION_FILE,
)

app = Flask(__name__, static_folder=".")
CORS(app)

LOG_FILE = os.path.join(os.path.dirname(__file__), "trading_log_capital.txt")

# Use a separate client that loads the bot's saved session tokens rather than
# logging in independently — two simultaneous logins invalidate each other.
_dashboard_client = CapitalClient()
if not _dashboard_client.load_session():
    _dashboard_client.login()

# ── Response cache ──────────────────────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 30   # seconds

def get_cached(key, ttl=None):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (datetime.now() - entry["time"]).seconds < (ttl or CACHE_TTL):
            return entry["data"]
    return None

def set_cached(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "time": datetime.now()}

def last_log_line(keyword):
    if not os.path.exists(LOG_FILE):
        return None
    result = None
    with open(LOG_FILE) as f:
        for line in f:
            if keyword in line:
                result = line[1:20]
    return result


# ── API Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    from flask import send_from_directory, Response
    resp = send_from_directory(os.path.dirname(__file__), "dashboard_capital.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/status")
def api_status():
    cached = get_cached("status")
    if cached:
        return jsonify(cached)
    try:
        _dashboard_client.load_session()
        client    = _dashboard_client
        balance   = client.get_account_balance()
        positions = client.get_open_positions() or []

        data = {
            "account_type":   ACCOUNT_TYPE.upper(),
            "balance":        balance,
            "open_positions": len(positions),
            "last_run":       last_log_line("Bot check complete"),
            "bot_running":    os.path.exists(LOCK_FILE),
        }
        set_cached("status", data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/markets")
def api_markets():
    cached = get_cached("markets")
    if cached:
        return jsonify(cached)
    try:
        _dashboard_client.load_session()
        client    = _dashboard_client
        positions = client.get_open_positions() or []
        result    = []

        # Build a lookup: epic -> position data (for open position details on cards)
        pos_by_epic = {}
        for p in positions:
            pos  = p.get("position", p)
            mkt  = p.get("market", {})
            epic = mkt.get("epic") or pos.get("epic")
            if epic:
                pos_by_epic[epic] = {"pos": pos, "mkt": mkt}

        for key, m in MARKETS.items():
            epic       = m["epic"]
            num_points = m["bb_period"] + m["rsi_period"] + 10

            time.sleep(0.3)   # avoid hitting Capital.com rate limit across 16 markets

            df = client.get_prices(epic, num_points=max(num_points, 60))
            if df is None or len(df) < num_points:
                result.append({"key": key, "name": m["name"], "epic": epic, "error": "No data"})
                continue

            live = client.get_live_price(epic)
            if live is not None:
                df.iloc[-1, df.columns.get_loc("close")] = live

            df     = add_indicators(df, m)
            signal = get_signal(df, m)
            curr   = df.iloc[-1]

            position_open = epic in pos_by_epic
            cap_pos       = pos_by_epic.get(epic)

            price    = round(live if live else float(curr["close"]), 2)
            bb_lower = round(float(curr["bb_lower"]), 2)
            bb_upper = round(float(curr["bb_upper"]), 2)
            bb_mid   = round(float(curr["bb_mid"]), 2)
            bb_range = bb_upper - bb_lower
            bb_pct   = round((price - bb_lower) / bb_range * 100, 1) if bb_range > 0 else 50

            entry = {
                "key":           key,
                "name":          m["name"],
                "epic":          epic,
                "price":         price,
                "rsi":           round(float(curr["rsi"]), 1),
                "bb_lower":      bb_lower,
                "bb_mid":        bb_mid,
                "bb_upper":      bb_upper,
                "bb_pct":        bb_pct,
                "signal":        signal,
                "position_open": position_open,
                "oversold":      m["oversold"],
                "overbought":    m["overbought"],
                "stop_pct":      m["stop_pct"],
                "target_pct":    m["target_pct"],
                "bb_period":     m["bb_period"],
                "bb_std":        m["bb_std"],
                "rsi_period":    m["rsi_period"],
            }

            result.append(entry)

        set_cached("markets", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions")
def api_positions():
    cached = get_cached("positions")
    if cached:
        return jsonify(cached)
    try:
        _dashboard_client.load_session()
        client    = _dashboard_client
        positions = client.get_open_positions() or []
        result    = []

        for p in positions:
            pos = p.get("position", p)
            mkt = p.get("market", {})
            direction  = pos.get("direction")
            size       = pos.get("size")
            net_change = mkt.get("netChange")
            pct_change = mkt.get("percentageChange")

            if net_change is not None and size is not None:
                multiplier = 1 if direction == "BUY" else -1
                day_pnl_gbp = round(net_change * size * multiplier, 2)
                day_pnl_pct = round(pct_change * multiplier, 2) if pct_change is not None else None
            else:
                day_pnl_gbp = None
                day_pnl_pct = None

            result.append({
                "deal_id":     pos.get("dealId"),
                "market":      mkt.get("instrumentName") or pos.get("epic"),
                "epic":        mkt.get("epic") or pos.get("epic"),
                "direction":   direction,
                "size":        size,
                "level":       pos.get("level"),
                "stop":        pos.get("stopLevel"),
                "limit":       pos.get("profitLevel"),
                "created_at":  pos.get("createdDateUTC"),
                "upl":         pos.get("upl"),
                "day_pnl_gbp": day_pnl_gbp,
                "day_pnl_pct": day_pnl_pct,
            })

        set_cached("positions", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify([])
        with open(LOG_FILE) as f:
            lines = f.readlines()
        filtered = [l.strip() for l in lines if l.strip() and not l.startswith("127.0.0.1")]
        return jsonify(filtered[-500:])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history")
def api_history():
    cached = get_cached("history", ttl=300)
    if cached:
        return jsonify(cached)
    try:
        _dashboard_client.load_session()
        client = _dashboard_client

        r = client._get(f"{BASE_URL}/history/transactions",
                        params={"type": "ALL", "pageSize": 500})
        transactions = r.json().get("transactions", []) if r.status_code == 200 else []

        trades = []
        starting_balance = 11000.0
        for t in transactions:
            if t.get("transactionType") not in ("DEAL", "TRADE"):
                continue
            raw_pnl = t.get("profitAndLoss", "0").replace("£", "").replace(",", "").strip()
            try:
                pnl_gbp = float(raw_pnl)
            except ValueError:
                continue

            open_level  = str(t.get("openLevel",  "-"))
            close_level = str(t.get("closeLevel", "-"))
            size        = str(t.get("size",        "-"))

            try:
                open_f  = float(open_level.replace(",", ""))
                close_f = float(close_level.replace(",", ""))
                size_f  = float(size.replace(",", ""))
                pnl_pct = ((close_f - open_f) / open_f * 100) * (1 if size_f > 0 else -1)
            except (ValueError, AttributeError, ZeroDivisionError):
                pnl_pct = None

            trades.append({
                "date":        t.get("date", t.get("dateUtc", "")),
                "open_date":   t.get("openDate", t.get("openDateUtc", "")),
                "market":      t.get("instrumentName", t.get("market", "")),
                "pnl_gbp":     round(pnl_gbp, 2),
                "pnl_pct":     round(pnl_pct, 2) if pnl_pct is not None else None,
                "open_level":  open_level,
                "close_level": close_level,
                "size":        size,
            })

        trades.sort(key=lambda x: x["date"])

        balance = starting_balance
        balance_series = [{"date": "2026-03-25T00:00:00", "balance": starting_balance}]
        for t in trades:
            balance += t["pnl_gbp"]
            balance_series.append({"date": t["date"], "balance": round(balance, 2)})

        log_balances = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                for line in f:
                    m = re.search(
                        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Account balance: £([\d,]+\.?\d*)",
                        line
                    )
                    if m:
                        log_balances.append({
                            "date":    m.group(1).replace(" ", "T"),
                            "balance": float(m.group(2).replace(",", "")),
                        })

        market_stats = {}
        for t in trades:
            name = t["market"]
            if name not in market_stats:
                market_stats[name] = {"market": name, "trades": 0, "pnl_gbp": 0.0, "wins": 0, "losses": 0}
            market_stats[name]["trades"]  += 1
            market_stats[name]["pnl_gbp"] += t["pnl_gbp"]
            if t["pnl_gbp"] >= 0:
                market_stats[name]["wins"]   += 1
            else:
                market_stats[name]["losses"] += 1

        total_pnl = sum(s["pnl_gbp"] for s in market_stats.values())
        for s in market_stats.values():
            s["pnl_gbp"]      = round(s["pnl_gbp"], 2)
            s["pnl_pct_port"] = round(s["pnl_gbp"] / starting_balance * 100, 2)
            s["contribution"] = round(s["pnl_gbp"] / total_pnl * 100, 1) if total_pnl != 0 else 0
            s["win_rate"]     = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0

        total_pnl_gbp = round(sum(t["pnl_gbp"] for t in trades), 2)
        total_pnl_pct = round(total_pnl_gbp / starting_balance * 100, 2)

        now = datetime.utcnow()
        def trades_since(days):
            cutoff = (now - timedelta(days=days)).isoformat()
            subset = [t for t in trades if t["date"] >= cutoff]
            pnl    = round(sum(t["pnl_gbp"] for t in subset), 2)
            return {"pnl_gbp": pnl, "pnl_pct": round(pnl / starting_balance * 100, 2), "trades": len(subset)}

        data = {
            "starting_balance": starting_balance,
            "current_balance":  round(balance, 2),
            "total_pnl_gbp":    total_pnl_gbp,
            "total_pnl_pct":    total_pnl_pct,
            "periods": {
                "7d":  trades_since(7),
                "30d": trades_since(30),
                "90d": trades_since(90),
                "all": {"pnl_gbp": total_pnl_gbp, "pnl_pct": total_pnl_pct, "trades": len(trades)},
            },
            "trades":           trades,
            "balance_series":   balance_series,
            "log_balances":     log_balances,
            "market_breakdown": sorted(market_stats.values(), key=lambda x: x["pnl_gbp"], reverse=True),
        }
        set_cached("history", data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    if os.path.exists(LOCK_FILE):
        return jsonify({"status": "already_running", "message": "Bot is already running, please wait."})
    threading.Thread(target=run_bot, daemon=True).start()
    return jsonify({"status": "started", "message": "Bot check started."})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5002))
    print(f"Capital.com Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=port, debug=False)
