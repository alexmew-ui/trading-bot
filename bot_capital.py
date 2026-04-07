"""
Capital.com Trading Bot
Strategy: Bollinger Band + RSI Mean Reversion — Both Directions
Timeframe: Hourly candles

Identical strategy to the IG bot, rewritten for the Capital.com API.
Trades 8 markets simultaneously, each with its own optimised settings.
Checks every hour via a scheduler.
"""

import requests
import threading
import pandas as pd
import numpy as np
import os
import json
import time
import schedule
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
API_KEY      = os.getenv("CAPITAL_API_KEY")
EMAIL        = os.getenv("CAPITAL_EMAIL")
PASSWORD     = os.getenv("CAPITAL_PASSWORD")
ACCOUNT_TYPE = os.getenv("CAPITAL_ACCOUNT_TYPE", "demo")

# --- Email alerts ---
EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
EXTRA_RECIPIENTS   = ["shane@blott.io"]

BASE_URL = (
    "https://demo-api-capital.backend-capital.com/api/v1"
    if ACCOUNT_TYPE == "demo"
    else "https://api-capital.backend-capital.com/api/v1"
)

RISK_PER_TRADE_PCT = 2.0
LOCK_FILE          = "bot_capital.lock"
SESSION_FILE       = "capital_session.json"

# --- Markets ---
# Epic codes confirmed against Capital.com API.
# All use Yahoo Finance for price history (hourly, free, no allowance limits).
# Settings are the robust parameters from the robustness backtest.

MARKETS = {
    "nasdaq": {
        "name":       "Nasdaq 100",
        "epic":       "US100",
        "bb_period":  25, "bb_std": 2.5, "rsi_period": 14,
        "oversold":   25, "overbought": 70,
        "stop_pct":   3.0, "target_pct": 5.0,
    },
    "natural_gas": {
        "name":       "Natural Gas",
        "epic":       "NATURALGAS",
        "bb_period":  30, "bb_std": 2.0, "rsi_period": 20,
        "oversold":   25, "overbought": 70,
        "stop_pct":   3.0, "target_pct": 6.0,
    },
    "brent_crude": {
        "name":       "Brent Crude Oil",
        "epic":       "OIL_BRENT",
        "bb_period":  30, "bb_std": 2.0, "rsi_period": 14,
        "oversold":   25, "overbought": 75,
        "stop_pct":   2.5, "target_pct": 5.0,
    },
    "silver": {
        "name":       "Silver",
        "epic":       "SILVER",
        "bb_period":  30, "bb_std": 2.0, "rsi_period": 14,
        "oversold":   35, "overbought": 85,
        "stop_pct":   3.0, "target_pct": 6.0,
    },
    "nikkei": {
        "name":       "Nikkei 225",
        "epic":       "J225",
        "bb_period":  20, "bb_std": 1.5, "rsi_period": 14,
        "oversold":   30, "overbought": 75,
        "stop_pct":   2.0, "target_pct": 4.0,
        "price_divisor": 190,   # J225 is priced in JPY; divide by ~190 to convert to GBP for sizing
    },
    "dax": {
        "name":       "DAX",
        "epic":       "DE40",
        "bb_period":  25, "bb_std": 2.0, "rsi_period": 14,
        "oversold":   25, "overbought": 75,
        "stop_pct":   2.0, "target_pct": 4.0,
    },
    "dow_jones": {
        "name":       "Dow Jones",
        "epic":       "US30",
        "bb_period":  25, "bb_std": 1.5, "rsi_period": 10,
        "oversold":   35, "overbought": 85,
        "stop_pct":   2.0, "target_pct": 5.0,
    },
    "sp500": {
        "name":       "S&P 500",
        "epic":       "US500",
        "bb_period":  20, "bb_std": 1.5, "rsi_period": 14,
        "oversold":   35, "overbought": 75,
        "stop_pct":   2.0, "target_pct": 5.0,
    },
    "gold": {
        "name":       "Gold",
        "epic":       "GOLD",
        "bb_period":  20, "bb_std": 2.5, "rsi_period": 20,
        "oversold":   35, "overbought": 75,
        "stop_pct":   2.5, "target_pct": 4.0,
    },
    "wti_crude": {
        "name":       "WTI Crude Oil",
        "epic":       "OIL_CRUDE",
        "bb_period":  25, "bb_std": 2.5, "rsi_period": 10,
        "oversold":   25, "overbought": 75,
        "stop_pct":   2.5, "target_pct": 6.0,
    },
    "copper": {
        "name":       "Copper",
        "epic":       "COPPER",
        "bb_period":  20, "bb_std": 1.5, "rsi_period": 10,
        "oversold":   30, "overbought": 80,
        "stop_pct":   2.5, "target_pct": 6.0,
        "atr_stops":  True,   # use ATR-based stops: 2×ATR stop, 3×ATR target
        "atr_period": 14,
    },
    "wheat": {
        "name":       "Wheat",
        "epic":       "WHEAT",
        "bb_period":  30, "bb_std": 1.5, "rsi_period": 14,
        "oversold":   25, "overbought": 65,
        "stop_pct":   2.5, "target_pct": 4.0,
    },
    "coffee": {
        "name":       "Coffee",
        "epic":       "COFFEEARABICA",
        "bb_period":  25, "bb_std": 2.5, "rsi_period": 10,
        "oversold":   30, "overbought": 80,
        "stop_pct":   2.5, "target_pct": 4.0,
    },
    "russell2000": {
        "name":       "Russell 2000",
        "epic":       "RTY",
        "bb_period":  20, "bb_std": 1.5, "rsi_period": 20,
        "oversold":   35, "overbought": 75,
        "stop_pct":   2.5, "target_pct": 5.0,
    },
}


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

LOG_FILE = "trading_log_capital.txt"

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────

def send_email(subject, body):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return
    try:
        recipients = [EMAIL_ADDRESS] + EXTRA_RECIPIENTS
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = EMAIL_ADDRESS
        msg["To"]      = ", ".join(recipients)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, recipients, msg.as_string())
        log("Email alert sent.")
    except Exception as e:
        log(f"Email failed: {e}")




# ─────────────────────────────────────────────
# Capital.com API client
# ─────────────────────────────────────────────

class CapitalClient:
    def __init__(self):
        self.session     = requests.Session()
        self.cst         = None
        self.token       = None
        self._login_lock = threading.Lock()

    def login(self, silent=False):
        try:
            r = requests.post(f"{BASE_URL}/session", json={
                "identifier": EMAIL,
                "password":   PASSWORD,
            }, headers={
                "X-CAP-API-KEY": API_KEY,
                "Content-Type":  "application/json",
            }, timeout=15)

            if r.status_code == 200:
                self.cst   = r.headers.get("CST")
                self.token = r.headers.get("X-SECURITY-TOKEN")
                self._update_headers()
                # Save session tokens so dashboard can reuse them without logging in again
                try:
                    with open(SESSION_FILE, "w") as f:
                        json.dump({"cst": self.cst, "token": self.token}, f)
                except Exception:
                    pass
                account_info = r.json().get("accountInfo", {})
                if not silent:
                    log(f"Logged in to Capital.com. Balance: £{account_info.get('balance', '?'):,}")
                return True
            else:
                log(f"Capital.com login failed: {r.status_code} {r.text[:100]}")
                return False
        except Exception as e:
            log(f"Capital.com login error: {e}")
            return False

    def load_session(self):
        """Load saved session tokens instead of logging in fresh (avoids invalidating other sessions)."""
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE) as f:
                    data = json.load(f)
                self.cst   = data.get("cst")
                self.token = data.get("token")
                if self.cst and self.token:
                    self._update_headers()
                    return True
        except Exception:
            pass
        return False

    def _update_headers(self):
        self.session.headers.update({
            "X-CAP-API-KEY":    API_KEY,
            "CST":              self.cst,
            "X-SECURITY-TOKEN": self.token,
            "Content-Type":     "application/json",
        })

    def _get(self, url, **kwargs):
        """GET with automatic re-login on session expiry."""
        r = self.session.get(url, **kwargs)
        if r.status_code == 401:
            with self._login_lock:
                r2 = self.session.get(url, **kwargs)
                if r2.status_code == 401:
                    log("Capital.com session expired — re-logging in...")
                    if self.login(silent=True):
                        r2 = self.session.get(url, **kwargs)
                r = r2
        return r

    def _post(self, url, **kwargs):
        """POST with automatic re-login on session expiry."""
        r = self.session.post(url, **kwargs)
        if r.status_code == 401:
            with self._login_lock:
                r2 = self.session.post(url, **kwargs)
                if r2.status_code == 401:
                    log("Capital.com session expired — re-logging in...")
                    if self.login(silent=True):
                        r2 = self.session.post(url, **kwargs)
                r = r2
        return r

    def _delete(self, url, **kwargs):
        """DELETE with automatic re-login on session expiry."""
        r = self.session.delete(url, **kwargs)
        if r.status_code == 401:
            with self._login_lock:
                if self.login(silent=True):
                    r = self.session.delete(url, **kwargs)
        return r

    def get_account_balance(self):
        try:
            r = self._get(f"{BASE_URL}/accounts")
            if r.status_code == 200:
                accounts = r.json().get("accounts", [])
                for a in accounts:
                    if a.get("preferred"):
                        return a.get("balance", {}).get("balance")
                if accounts:
                    return accounts[0].get("balance", {}).get("balance")
        except Exception as e:
            log(f"Could not fetch balance: {e}")
        return None

    def get_open_positions(self):
        try:
            r = self._get(f"{BASE_URL}/positions")
            if r.status_code == 200:
                return r.json().get("positions", [])
        except Exception as e:
            log(f"Could not fetch positions: {e}")
        return []

    def get_prices(self, epic, num_points=100):
        """Fetch hourly candles directly from Capital.com."""
        try:
            r = self._get(f"{BASE_URL}/prices/{epic}",
                          params={"resolution": "HOUR", "max": num_points})
            if r.status_code != 200:
                log(f"Capital.com price history error for {epic}: {r.status_code} {r.text[:100]}")
                return None
            raw = r.json().get("prices", [])
            if not raw:
                return None
            rows = []
            for c in raw:
                mid = lambda side: (c[side]["bid"] + c[side]["ask"]) / 2
                rows.append({
                    "high":  mid("highPrice"),
                    "low":   mid("lowPrice"),
                    "close": mid("closePrice"),
                })
            return pd.DataFrame(rows).dropna().reset_index(drop=True)
        except Exception as e:
            log(f"Capital.com price fetch error for {epic}: {e}")
            return None

    def get_live_price(self, epic):
        """Fetch current mid-price from Capital.com."""
        try:
            r = self._get(f"{BASE_URL}/markets/{epic}")
            if r.status_code == 200:
                snap = r.json().get("snapshot", {})
                bid  = snap.get("bid")
                ask  = snap.get("offer")
                if bid and ask:
                    return (bid + ask) / 2
        except Exception:
            pass
        return None

    def get_min_deal_size(self, epic):
        """Fetch the minimum trade size for an instrument."""
        try:
            r = self._get(f"{BASE_URL}/markets/{epic}")
            if r.status_code == 200:
                rules = r.json().get("dealingRules", {})
                return rules.get("minDealSize", {}).get("value", 1.0)
        except Exception:
            pass
        return 1.0

    def place_trade(self, epic, market_name, direction, size, stop_level, limit_level):
        """Submit a trade order to Capital.com."""
        try:
            body = {
                "epic":           epic,
                "direction":      direction,
                "size":           size,
                "guaranteedStop": False,
                "stopLevel":      stop_level,
                "profitLevel":    limit_level,
            }
            r = self._post(f"{BASE_URL}/positions", json=body)
            if r.status_code == 200:
                deal_ref = r.json().get("dealReference", "unknown")
                log(f"Trade submitted: {direction} {market_name} | Stop: {stop_level:.2f} | Target: {limit_level:.2f} | Ref: {deal_ref}")
                return deal_ref
            else:
                log(f"Trade failed for {market_name}: {r.status_code} {r.text[:200]}")
                return None
        except Exception as e:
            log(f"Trade error for {market_name}: {e}")
            return None

    def confirm_trade(self, deal_ref, market_name, direction, size, stop_level, limit_level, stop_pct, target_pct, balance):
        """Poll Capital.com to confirm a trade was accepted and get the fill price."""
        if not deal_ref:
            return False
        time.sleep(2)
        try:
            r = self._get(f"{BASE_URL}/confirms/{deal_ref}")
            if r.status_code == 200:
                data   = r.json()
                status = data.get("dealStatus", "")
                reason = data.get("reason", "")
                if status == "ACCEPTED":
                    fill_price = data.get("level") or data.get("price", 0)
                    deal_id    = data.get("dealId", "")
                    log(f"{market_name} | Trade CONFIRMED. Filled at: {fill_price} | Deal ID: {deal_id}")
                    stop_pts  = round(abs(fill_price - stop_level), 2)
                    limit_pts = round(abs(fill_price - limit_level), 2)
                    send_email(
                        f"Trade opened: {direction} {market_name}",
                        f"Capital.com Trade Notification\n"
                        f"{'─'*40}\n"
                        f"Market:        {market_name}\n"
                        f"Direction:     {direction}\n"
                        f"Fill price:    {fill_price}\n"
                        f"Stop loss:     {stop_level} ({stop_pts} pts)\n"
                        f"Take profit:   {limit_level} ({limit_pts} pts)\n"
                        f"Trade size:    {size}\n"
                        f"Max loss:      £{round(size * fill_price * stop_pct / 100, 2)}\n"
                        f"Account type:  {ACCOUNT_TYPE.upper()}\n"
                    )
                    return True
                else:
                    log(f"{market_name} | Trade REJECTED. Reason: {reason}")
                    return False
        except Exception as e:
            log(f"{market_name} | Confirm error: {e}")
        return False

    def close_position(self, deal_id, epic, direction, size):
        """Close an open position."""
        try:
            close_direction = "SELL" if direction == "BUY" else "BUY"
            r = self._delete(f"{BASE_URL}/positions/{deal_id}")
            if r.status_code == 200:
                log(f"Position {deal_id} closed.")
                return True
            else:
                log(f"Failed to close {deal_id}: {r.status_code} {r.text[:100]}")
        except Exception as e:
            log(f"Close error: {e}")
        return False


# ─────────────────────────────────────────────
# Strategy indicators
# ─────────────────────────────────────────────

def add_indicators(df, m):
    closes = df["close"].astype(float)
    df["bb_mid"]   = closes.rolling(m["bb_period"]).mean()
    df["bb_std"]   = closes.rolling(m["bb_period"]).std()
    df["bb_upper"] = df["bb_mid"] + m["bb_std"] * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - m["bb_std"] * df["bb_std"]
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(m["rsi_period"]).mean()
    loss  = (-delta.clip(upper=0)).rolling(m["rsi_period"]).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))
    if m.get("atr_stops"):
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        tr = pd.concat([
            highs - lows,
            (highs - closes.shift()).abs(),
            (lows  - closes.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(m["atr_period"]).mean()
    return df

def get_signal(df, m):
    curr = df.iloc[-1]
    if pd.isna(curr["rsi"]) or pd.isna(curr["bb_lower"]):
        return "HOLD"
    if curr["close"] <= curr["bb_lower"] and curr["rsi"] <= m["oversold"]:
        return "BUY"
    if curr["close"] >= curr["bb_upper"] and curr["rsi"] >= m["overbought"]:
        return "SELL"
    return "HOLD"


# ─────────────────────────────────────────────
# Trade sizing
# ─────────────────────────────────────────────

def calculate_trade_size(balance, price, stop_pct, min_size=1.0, price_divisor=1.0):
    # price_divisor converts a foreign-currency price to GBP for sizing purposes.
    # e.g. J225 is priced in JPY (~52000), so price_divisor=190 gives ~£274 equivalent.
    price_gbp     = price / price_divisor
    max_loss      = balance * (RISK_PER_TRADE_PCT / 100)
    loss_per_unit = price_gbp * (stop_pct / 100)
    raw_size      = max_loss / loss_per_unit
    size          = max(min_size, round(raw_size * 10) / 10)
    # Hard safety cap: never risk more than 3× the intended max loss, regardless of min_size.
    # This prevents a bad get_min_deal_size() value from causing a catastrophically large trade.
    max_safe_size = (max_loss * 3) / loss_per_unit
    return min(size, round(max_safe_size * 10) / 10)


# ─────────────────────────────────────────────
# Check one market
# ─────────────────────────────────────────────

def check_market(client, market_key, market_cfg, balance, open_positions):
    epic  = market_cfg["epic"]
    name  = market_cfg["name"]

    position_open = any(p.get("market", {}).get("epic") == epic for p in (open_positions or []))

    num_points = market_cfg["bb_period"] + market_cfg["rsi_period"] + 10
    df = client.get_prices(epic, num_points=max(num_points, 60))
    if df is None or len(df) < num_points:
        log(f"{name} | Not enough price data. Skipping.")
        return

    # Get live mid-price from Capital.com
    live = client.get_live_price(epic)
    if live is None:
        log(f"{name} | Could not fetch live price. Skipping.")
        return

    # Update last candle with live price so indicators use the freshest close
    df.iloc[-1, df.columns.get_loc("close")] = live

    df     = add_indicators(df, market_cfg)
    signal = get_signal(df, market_cfg)
    curr   = df.iloc[-1]
    price  = live

    log(f"{name} | Price: {price:.2f} | RSI: {curr['rsi']:.1f} | "
        f"BB lower: {curr['bb_lower']:.2f} | BB upper: {curr['bb_upper']:.2f} | "
        f"Signal: {signal} | Position open: {position_open}")

    if position_open:
        log(f"{name} | Position already open. Skipping.")
        return

    if signal == "HOLD":
        log(f"{name} | No signal. Waiting.")
        return

    stop_pct   = market_cfg["stop_pct"]
    target_pct = market_cfg["target_pct"]

    # ATR-based stops override fixed % for markets flagged with atr_stops=True
    if market_cfg.get("atr_stops"):
        atr_val = float(curr.get("atr", float("nan")))
        if not pd.isna(atr_val) and atr_val > 0:
            stop_pct   = round(atr_val / price * 100 * 2, 4)   # 2 × ATR
            target_pct = round(atr_val / price * 100 * 3, 4)   # 3 × ATR
            log(f"{name} | ATR stops active. ATR={atr_val:.4f} | "
                f"stop_pct={stop_pct:.2f}% target_pct={target_pct:.2f}%")
        else:
            log(f"{name} | ATR not available — falling back to fixed stops.")

    # Get minimum deal size
    price_divisor = market_cfg.get("price_divisor", 1.0)
    min_size      = client.get_min_deal_size(epic)
    trade_size    = calculate_trade_size(balance, price, stop_pct, min_size=min_size, price_divisor=price_divisor)
    max_loss      = round(trade_size * (price / price_divisor) * stop_pct / 100, 2)

    if trade_size == min_size and calculate_trade_size(balance, price, stop_pct, price_divisor=price_divisor) < min_size:
        log(f"{name} | Size raised to minimum ({min_size}) — exceeds 2% risk rule (£{max_loss} max loss)")

    if signal == "BUY":
        stop_level  = round(price * (1 - stop_pct   / 100), 2)
        limit_level = round(price * (1 + target_pct / 100), 2)
        log(f"{name} | BUY signal. Entry: {price:.2f} | Stop: {stop_level} | "
            f"Target: {limit_level} | Size: {trade_size} | Max loss: £{max_loss}")
        ref = client.place_trade(epic, name, "BUY", trade_size, stop_level, limit_level)
        client.confirm_trade(ref, name, "BUY", trade_size, stop_level, limit_level,
                             stop_pct, target_pct, balance)

    elif signal == "SELL":
        stop_level  = round(price * (1 + stop_pct   / 100), 2)
        limit_level = round(price * (1 - target_pct / 100), 2)
        log(f"{name} | SELL signal. Entry: {price:.2f} | Stop: {stop_level} | "
            f"Target: {limit_level} | Size: {trade_size} | Max loss: £{max_loss}")
        ref = client.place_trade(epic, name, "SELL", trade_size, stop_level, limit_level)
        client.confirm_trade(ref, name, "SELL", trade_size, stop_level, limit_level,
                             stop_pct, target_pct, balance)


# ─────────────────────────────────────────────
# Main bot run
# ─────────────────────────────────────────────

_bot_client = CapitalClient()
_bot_client.login()


def run_bot():
    if os.path.exists(LOCK_FILE):
        log("Bot already running — skipping this check.")
        return

    open(LOCK_FILE, "w").close()
    try:
        log("--- Bot check starting ---")
        client = _bot_client

        balance = client.get_account_balance()
        if balance is None:
            log("Could not fetch balance. Skipping.")
            return
        log(f"Account balance: £{balance:,.2f}")

        positions = client.get_open_positions()
        log(f"Open positions: {len(positions)}")

        for key, m in MARKETS.items():
            try:
                check_market(client, key, m, balance, positions)
            except Exception as e:
                log(f"{m['name']} | Error: {e}")

        log("Bot check complete.")

    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


STARTUP_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_capital_running.pid")

if __name__ == "__main__":
    # Prevent two bot processes running simultaneously.
    # Write our PID to a startup lock file; refuse to start if another live process holds it.
    if os.path.exists(STARTUP_LOCK):
        try:
            existing_pid = int(open(STARTUP_LOCK).read().strip())
            os.kill(existing_pid, 0)   # signal 0 = just check if process exists
            print(f"[ERROR] Bot is already running as PID {existing_pid}. Exiting.")
            print(f"        If this is wrong, delete {STARTUP_LOCK} and try again.")
            raise SystemExit(1)
        except (ProcessLookupError, ValueError):
            pass   # stale lock from a crashed process — safe to overwrite

    with open(STARTUP_LOCK, "w") as f:
        f.write(str(os.getpid()))

    try:
        log(f"Capital.com bot started. Monitoring {len(MARKETS)} markets.")
        log(f"Markets: {', '.join(m['name'] for m in MARKETS.values())}")
        log("Scheduled to run at 1 minute past every hour.")

        # Schedule at :01 past every hour (clock-aligned, not relative to start time)
        schedule.every().hour.at(":01").do(run_bot)

        # Run immediately on start as well
        run_bot()

        while True:
            schedule.run_pending()
            time.sleep(30)
    finally:
        if os.path.exists(STARTUP_LOCK):
            os.remove(STARTUP_LOCK)
