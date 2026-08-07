"""
================================================================================
 meanrev_paper.py  —  "BOLLINGER + RSI + 200-SMA MEAN REVERSION"  (paper)
================================================================================
Built exactly to the spec the user provided:

  * Timeframe:   5m  (per user request — all paper bots moved to the 5-minute chart)
  * Entry:       price TOUCHES the lower Bollinger Band (20, 2σ)  AND  RSI < 35
  * Trend filter: price must be ABOVE the 200 SMA  (long-only; non-negotiable)
  * Exit target:  the 20 SMA (the Bollinger middle band)
  * Stop loss:    2 × ATR below entry
  * Account:      $100 paper, ALL-IN 20x, BTC/USDT. Long-only.

A 20x liquidation backstop (~ -5%) caps each trade so the paper balance can't go
below $0. All paper, real Binance data. Not a guaranteed winner — it's the ~68%
backtest / ~56% live strategy from the writeup, run honestly on paper.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import numpy as np
import pandas as pd

import config

STATE_PATH = os.path.join(config.DATA_DIR, "meanrev_state.json")

SYMBOL    = "BTC/USDT"
TIMEFRAME = "5m"
START_BAL = 100.0
LEVERAGE  = 20.0
FEE       = 0.0           # ZERO — Lighter (the real venue) has no trading fees
POLL_SEC  = 30          # 5m chart → check twice a minute
FETCH     = 600          # 5m bars (need 200 for the SMA + buffer)

BB_LEN   = 20            # Bollinger length (also the 20-SMA exit target)
BB_STD   = 2.0           # 2 sigma
RSI_LEN  = 14
RSI_BUY  = 35            # RSI < 35
SMA_TREND = 200          # price must be above the 200 SMA
ATR_LEN  = 14
ATR_STOP = 2.0           # stop = entry - 2 * ATR


def _sma(s, n): return s.rolling(n).mean()

def _rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    ag = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    return (100 - 100/(1 + ag/al.replace(0, np.nan))).fillna(50)

def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


class MeanReversionBot:
    def __init__(self, market=None):
        self.market = market
        self._ex = None
        self.enabled = True            # activated to trade
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.snap: dict = {}           # latest indicator readout for the panel
        self.price = 0.0
        self._last_bar = None          # don't enter twice on the same closed bar
        self.status = "starting…"
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance, "pos": self.pos,
                           "history": self.history, "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            d = json.load(open(STATE_PATH))
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            print(f"[meanrev] restored: bal ${self.balance:.2f}, {len(self.history)} trades, "
                  f"{'1 open' if self.pos else 'flat'}")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[meanrev] {msg}")

    # ---- data ----
    def _ensure_ex(self):
        if self._ex is None:
            import ccxt
            self._ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        return self._ex

    def _fetch(self):
        rows = self._ensure_ex().fetch_ohlcv(SYMBOL, TIMEFRAME, limit=FETCH)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = df["ts"].astype("int64")
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        return df

    def _indi(self, df):
        d = df.copy()
        d["bb_mid"] = _sma(d["close"], BB_LEN)
        sd = d["close"].rolling(BB_LEN).std()
        d["bb_up"] = d["bb_mid"] + BB_STD*sd
        d["bb_lo"] = d["bb_mid"] - BB_STD*sd
        d["rsi"] = _rsi(d["close"], RSI_LEN)
        d["sma200"] = _sma(d["close"], SMA_TREND)
        d["atr"] = _atr(d, ATR_LEN)
        return d

    # ---- loop ----
    async def manage_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                d = await loop.run_in_executor(None, self._fetch)
                d = self._indi(d)
                self._tick(d)
            except Exception as e:
                self.status = f"loop error: {type(e).__name__}: {str(e)[:80]}"

    def _tick(self, d):
        if len(d) < SMA_TREND + 5 or pd.isna(d["atr"].iloc[-1]):
            self.status = "waiting for enough 5m history…"
            return
        price = float(d["close"].iloc[-1])     # forming bar (live-ish)
        hi = float(d["high"].iloc[-1]); lo = float(d["low"].iloc[-1])
        self.price = price
        c2 = d.iloc[-2]                         # last CLOSED bar
        self.snap = {"price": round(price, 1), "bb_lo": round(float(c2["bb_lo"]), 1),
                     "bb_mid": round(float(c2["bb_mid"]), 1), "bb_up": round(float(c2["bb_up"]), 1),
                     "rsi": round(float(c2["rsi"]), 1), "sma200": round(float(c2["sma200"]), 1),
                     "above_200": bool(c2["close"] > c2["sma200"])}
        if self.pos is not None:
            self._manage(price, hi, lo, d)
        elif self.enabled:
            self._maybe_enter(price, c2, float(d["atr"].iloc[-1]), d.index[-2])
        self._set_status(c2)
        self._save()

    def _set_status(self, c2):
        eq = self._equity()
        reg = "ABOVE 200SMA (longs armed)" if bool(c2["close"] > c2["sma200"]) else "below 200SMA (no longs)"
        self.status = (f"{reg} · RSI {float(c2['rsi']):.0f} · equity ${eq:,.2f} · "
                       f"{'in trade' if self.pos else 'flat'} · {'armed' if self.enabled else 'paused'}")

    # ---- entry ----
    def _maybe_enter(self, price, c2, atr, bar_id):
        if bar_id == self._last_bar:           # already acted on this closed bar
            return
        touch_lower = c2["low"] <= c2["bb_lo"]            # price TOUCHED lower Bollinger band
        rsi_ok = c2["rsi"] < RSI_BUY                      # RSI < 35
        above_trend = c2["close"] > c2["sma200"]          # ABOVE 200 SMA (non-negotiable)
        if touch_lower and rsi_ok and above_trend and atr > 0:
            self._open(price, atr, bar_id)

    def _open(self, price, atr, bar_id):
        eq = self._equity()
        if eq <= 0:
            return
        notional = eq * LEVERAGE                          # all-in 20x
        qty = notional / price
        self.balance -= notional * FEE
        stop = price - ATR_STOP * atr                     # 2 x ATR below entry
        liq = price * (1 - 1/LEVERAGE)                    # 20x liquidation backstop
        self.pos = {"id": uuid.uuid4().hex[:6], "side": "long", "entry": price, "qty": qty,
                    "notional": round(notional, 2), "margin": round(eq, 2),
                    "stop": stop, "liq": liq, "atr": atr, "opened_at": time.time()}
        self._last_bar = bar_id
        self._note(f"OPEN LONG @ ${price:,.1f} · ${notional:,.0f} (20x on ${eq:,.2f}) · "
                   f"stop ${stop:,.1f} (2xATR) · target = 20SMA", "open")

    # ---- management ----
    def _manage(self, price, hi, lo, d):
        p = self.pos
        target = float(d["bb_mid"].iloc[-1])              # exit at the 20 SMA (middle band)
        reason = None
        if lo <= p["liq"]:
            reason, exit_px = "liquidation", p["liq"]
        elif lo <= p["stop"]:
            reason, exit_px = "stop_2ATR", p["stop"]
        elif hi >= target:
            reason, exit_px = "target_20SMA", max(target, p["entry"])
        if reason:
            self._close(exit_px, reason)

    def _close(self, price, reason):
        p = self.pos
        pnl = p["qty"] * (price - p["entry"]) - p["notional"] * FEE
        pnl = max(pnl, -p["margin"])                      # isolated 20x margin
        self.balance = max(0.0, self.balance + pnl)
        self.history.insert(0, {"side": "long", "entry": round(p["entry"], 1), "exit": round(price, 1),
                                "pnl": round(pnl, 2), "reason": reason,
                                "opened_at": p["opened_at"], "closed_at": time.time()})
        self.history = self.history
        self.pos = None
        self._note(f"CLOSE LONG @ ${price:,.1f} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")

    # ---- helpers + controls ----
    def _equity(self):
        eq = self.balance
        if self.pos and self.price:
            eq += self.pos["qty"] * (self.price - self.pos["entry"])
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self._last_bar = None
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        pos = None
        if self.pos:
            p = self.pos
            up = p["qty"] * (self.price - p["entry"]) if self.price else 0.0
            pos = {"side": "long", "entry": round(p["entry"], 1), "notional": p["notional"],
                   "stop": round(p["stop"], 1), "liq": round(p["liq"], 1),
                   "mark": round(self.price, 1), "upnl": round(up, 2)}
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled, "status": self.status, "snap": self.snap,
            "symbol": SYMBOL, "timeframe": TIMEFRAME,
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BAL, "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq/START_BAL - 1)*100, 2),
            "position": pos, "trades": len(self.history), "wins": wins,
            "history": self.history, "log": self.log[:25],
        }
