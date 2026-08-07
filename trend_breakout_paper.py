"""
================================================================================
 trend_breakout_paper.py  —  "CRYPTO TREND BREAKOUT + ATR RISK SYSTEM"  (paper)
================================================================================
A faithful paper-trading bot of the Glen Goodman ("The Crypto Trader") method:
DAILY-timeframe trend-following on the majors (BTC, ETH, SOL). It does not scalp —
it waits for a real trend and rides it.

The pipeline (matches the 10-point spec):
  1. MARKET FILTER FIRST — BTC is the bellwether. Long-mode only when BTC daily close
     is above a flat/rising 200-day MA. Otherwise "danger mode": cash is a position.
  2. Universe — BTC/ETH/SOL only (liquid majors).
  3. Entry A — base breakout: daily close above the prior 60-day high (resistance), on
     above-average volume, while in long-mode and the coin is above its own 200d MA.
     Enter HALF; add the other HALF on a retest that holds old resistance as support.
     Don't chase (skip if already > 2.5 ATR above the breakout).
  4. Entry B — continuation pullback: uptrend (close>200d MA, 50d MA rising), price
     pulls back to the 20/50d MA then closes back strong above it. Enter full.
  5. Stop — entry - 2*ATR(14); never widened; judged on daily closes.
  6. Sizing — risk 0.5-1% of equity per trade; qty = risk$ / (entry - stop) (ATR-based).
  7. Exit — ride with a chandelier trailing stop (highest 30d high - 5*ATR(30)); also
     exit on a daily close back below the 200d MA (trend bent).
  8. Partials — +2R: trim 25% & move stop to breakeven. +4R (if extended): trim 25%.
     Let the runner run.
  9. Shorts — OFF (the book says shorting isn't the edge; in danger mode we hold cash).

All paper. Real Binance daily data. No leverage (position sizes are small by design).
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

STATE_PATH = os.path.join(config.DATA_DIR, "trend_breakout_state.json")

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
START_BAL   = 100.0         # paper account (USDT)
POLL_SEC    = 90            # how often we refresh daily data + manage (daily strategy = slow)
FEE         = 0.0          # ZERO — Lighter (the real venue) has no trading fees
ATR_LEN     = 14
DONCH       = 60            # base-breakout lookback (resistance = prior 60-day high)
ATR_STOP    = 2.0           # initial stop = entry - ATR_STOP * ATR
CHAND_LEN   = 30            # chandelier: highest high of last 30 days
CHAND_ATR   = 5.0           # chandelier trail = highest_high_30 - 5 * ATR(30)
VOL_MULT    = 1.3           # breakout volume must beat this * 20-day average
NOCHASE_ATR = 2.5           # skip a breakout already this many ATRs extended
RISKS = {"0.5": "0.5% risk/trade (safer)", "0.75": "0.75% risk/trade", "1.0": "1.0% risk/trade"}
DEFAULT_RISK = 0.75


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def _atr(df, n):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


class TrendBreakoutBot:
    def __init__(self, market=None):
        self.market = market            # optional MarketData (unused for data; kept for parity)
        self._ex = None                 # lazy ccxt exchange
        self.enabled = True
        self.risk = DEFAULT_RISK        # % of equity risked per trade
        self.balance = START_BAL        # realized cash
        self.positions: dict[str, dict] = {}   # symbol -> position
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.regime = "starting…"
        self.snap: dict[str, dict] = {}        # per-symbol latest indicator snapshot (for the panel)
        self.prices: dict[str, float] = {}
        self.status = "starting…"
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence ------------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"enabled": self.enabled, "risk": self.risk, "balance": self.balance,
                           "positions": self.positions, "history": self.history,
                           "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.risk = float(d.get("risk", self.risk))
            self.balance = float(d.get("balance", self.balance))
            self.positions = d.get("positions", {}) or {}
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            print(f"[trend] restored: bal ${self.balance:.2f}, {len(self.history)} trades, "
                  f"{len(self.positions)} open")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[trend] {msg}")

    # ---- data -------------------------------------------------------------
    def _ensure_ex(self):
        if self._ex is None:
            import ccxt
            self._ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        return self._ex

    def _fetch_daily(self, symbol, limit=400):
        rows = self._ensure_ex().fetch_ohlcv(symbol, "1d", limit=limit)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).astype(float)
        df = df[["open", "high", "low", "close", "volume"]]
        return df

    # ---- indicators -------------------------------------------------------
    def _indi(self, df):
        d = df.copy()
        d["ma200"] = d["close"].rolling(200).mean()
        d["ma50"] = d["close"].rolling(50).mean()
        d["ma20"] = d["close"].rolling(20).mean()
        d["atr"] = _atr(d, ATR_LEN)
        d["atr30"] = _atr(d, 30)
        d["donch_hi"] = d["high"].rolling(DONCH).max().shift(1)     # prior 60-day high = resistance
        d["hh30"] = d["high"].rolling(CHAND_LEN).max()
        d["vol_ma"] = d["volume"].rolling(20).mean()
        return d

    # ---- the loop ---------------------------------------------------------
    async def manage_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                dfs = {}
                for sym in SYMBOLS:
                    raw = await loop.run_in_executor(None, self._fetch_daily, sym)
                    dfs[sym] = self._indi(raw)
                self._tick(dfs)
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:90]}", "error")

    def _tick(self, dfs):
        # ---- 1) MARKET FILTER: BTC daily 200d MA regime ----
        btc = dfs.get("BTC/USDT")
        if btc is None or len(btc) < 220 or pd.isna(btc["ma200"].iloc[-1]):
            self.status = "waiting for enough BTC daily history…"
            return
        btc_close = float(btc["close"].iloc[-1])
        ma200_now = float(btc["ma200"].iloc[-1])
        ma200_20 = float(btc["ma200"].iloc[-21]) if len(btc) > 21 else ma200_now
        rising_or_flat = ma200_now >= ma200_20
        long_mode = (btc_close > ma200_now) and rising_or_flat
        self.regime = ("LONG-MODE · BTC above rising 200d MA" if long_mode
                       else "DANGER-MODE · cash is a position (BTC below/under-200d MA)")

        for sym in SYMBOLS:
            d = dfs.get(sym)
            if d is None or len(d) < 220 or pd.isna(d["atr"].iloc[-1]):
                continue
            price = float(d["close"].iloc[-1])     # forming daily candle (live-ish)
            self.prices[sym] = price
            self.snap[sym] = {
                "price": round(price, 2), "ma200": round(float(d["ma200"].iloc[-1]), 2),
                "above_200": price > float(d["ma200"].iloc[-1]),
                "donch_hi": round(float(d["donch_hi"].iloc[-1]), 2),
                "atr": round(float(d["atr"].iloc[-1]), 2),
            }
            if sym in self.positions:
                self._manage(sym, d)
            elif self.enabled and long_mode:
                self._try_enter(sym, d)

        self._set_status(long_mode)
        self._save()

    def _set_status(self, long_mode):
        eq = self._equity()
        self.status = (f"{self.regime} · {len(self.positions)} open · equity ${eq:,.0f} · "
                       f"{'armed' if self.enabled else 'paused'}")

    # ---- entries ----------------------------------------------------------
    def _try_enter(self, sym, d):
        # use the last CLOSED daily bar (-2) for signals; forming bar (-1) is "today"
        c2, c3 = d.iloc[-2], d.iloc[-3]
        price = float(d["close"].iloc[-1])
        atr = float(d["atr"].iloc[-1])
        if pd.isna(c2["ma200"]) or pd.isna(c2["donch_hi"]) or atr <= 0:
            return
        coin_uptrend = c2["close"] > c2["ma200"]
        if not coin_uptrend:
            return

        # ---- Entry A: FRESH base breakout above the prior 60-day high ----
        fresh_breakout = (c2["close"] > c2["donch_hi"]) and (c3["close"] <= c3["donch_hi"])
        vol_ok = c2["volume"] > VOL_MULT * c2["vol_ma"] if c2["vol_ma"] == c2["vol_ma"] else False
        strong = c2["close"] > c2["open"]
        not_chasing = price <= c2["donch_hi"] + NOCHASE_ATR * atr
        if fresh_breakout and vol_ok and strong and not_chasing:
            self._open(sym, price, atr, kind="breakout", half=True, breakout=float(c2["donch_hi"]))
            return

        # ---- Entry B: continuation pullback that reclaims the 20/50d MA ----
        ma50_rising = c2["ma50"] > d["ma50"].iloc[-22] if len(d) > 22 else False
        recent_low = float(d["low"].iloc[-6:-1].min())
        touched_ma = recent_low <= max(float(c2["ma20"]), float(c2["ma50"])) * 1.01
        reclaim = (c2["close"] > c2["ma20"]) and strong
        higher = c2["close"] > float(d["close"].iloc[-22]) if len(d) > 22 else False
        if ma50_rising and touched_ma and reclaim and higher and not_chasing:
            self._open(sym, price, atr, kind="pullback", half=False, breakout=float(c2["ma20"]))

    def _open(self, sym, price, atr, kind, half, breakout):
        entry = price * (1 + FEE * 0)                  # paper: fill at price; fee charged separately
        stop = entry - ATR_STOP * atr
        if stop <= 0 or entry <= stop:
            return
        eq = self._equity()
        full_qty = (eq * (self.risk / 100.0)) / (entry - stop)
        if full_qty <= 0:
            return
        qty = full_qty * (0.5 if half else 1.0)
        self.balance -= entry * qty * FEE
        self.positions[sym] = {
            "id": uuid.uuid4().hex[:6], "symbol": sym, "kind": kind, "side": "long",
            "entry": entry, "qty": qty, "full_qty": full_qty, "init_stop": stop, "stop": stop,
            "breakout": breakout, "added": not half, "best": entry,
            "took1": False, "took2": False, "opened_at": time.time(),
        }
        self._note(f"OPEN {sym} {kind} @ ${entry:,.2f} · stop ${stop:,.2f} · "
                   f"{qty:.4f} ({'half' if half else 'full'}) · risk {self.risk}%", "open")

    # ---- management (stop / retest add / partials / trailing exit) --------
    def _manage(self, sym, d):
        pos = self.positions.get(sym)
        if pos is None:
            return
        price = float(d["close"].iloc[-1])
        hi = float(d["high"].iloc[-1]); lo = float(d["low"].iloc[-1])
        atr = float(d["atr"].iloc[-1])
        closed_close = float(d["close"].iloc[-2])
        ma200_now = float(d["ma200"].iloc[-1])
        chand = float(d["hh30"].iloc[-1]) - CHAND_ATR * float(d["atr30"].iloc[-1])
        pos["best"] = max(pos["best"], hi)
        R = pos["entry"] - pos["init_stop"]

        # retest add: a breakout that pulled back to old resistance and held -> add 2nd half
        if not pos["added"]:
            if lo <= pos["breakout"] * 1.012 and price >= pos["breakout"]:
                add = pos["full_qty"] - pos["qty"]
                if add > 0:
                    self.balance -= price * add * FEE
                    new_qty = pos["qty"] + add
                    pos["entry"] = (pos["entry"] * pos["qty"] + price * add) / new_qty
                    pos["qty"] = new_qty
                    pos["added"] = True
                    self._note(f"ADD {sym} retest hold @ ${price:,.2f} · now {new_qty:.4f}", "open")
            elif price > pos["breakout"] + 3 * atr:
                pos["added"] = True                    # ran away without a retest — keep the half

        # partial profits
        if R > 0:
            if (not pos["took1"]) and price >= pos["entry"] + 2 * R:
                self._partial(sym, price, 0.25, "+2R")
                pos["stop"] = max(pos["stop"], pos["entry"])     # move to breakeven
                pos["took1"] = True
            extended = price > float(d["ma20"].iloc[-1]) * 1.10
            if (not pos["took2"]) and price >= pos["entry"] + 4 * R and extended:
                self._partial(sym, price, 0.25, "+4R extended")
                pos["took2"] = True

        # trailing exit: chandelier OR daily close below the 200d MA (trend bent)
        eff_stop = max(pos["stop"], chand)
        if lo <= eff_stop:
            self._close(sym, eff_stop, "trail_stop")
        elif closed_close < ma200_now:
            self._close(sym, price, "below_200d_MA")

    def _partial(self, sym, price, frac, why):
        pos = self.positions.get(sym)
        if pos is None:
            return
        q = pos["qty"] * frac
        pnl = q * (price - pos["entry"]) - price * q * FEE
        self.balance += pnl
        pos["qty"] -= q
        self.history.insert(0, {"symbol": sym, "side": "long", "kind": pos["kind"],
                                "entry": round(pos["entry"], 2), "exit": round(price, 2),
                                "qty": round(q, 4), "pnl": round(pnl, 2), "reason": f"partial {why}",
                                "opened_at": pos["opened_at"], "closed_at": time.time()})
        self.history = self.history
        self._note(f"PARTIAL {sym} {why} · sold {q:.4f} @ ${price:,.2f} · banked ${pnl:+.2f}", "win")

    def _close(self, sym, exit_price, reason):
        pos = self.positions.pop(sym, None)
        if pos is None:
            return
        q = pos["qty"]
        pnl = q * (exit_price - pos["entry"]) - exit_price * q * FEE
        self.balance += pnl
        self.history.insert(0, {"symbol": sym, "side": "long", "kind": pos["kind"],
                                "entry": round(pos["entry"], 2), "exit": round(exit_price, 2),
                                "qty": round(q, 4), "pnl": round(pnl, 2), "reason": reason,
                                "opened_at": pos["opened_at"], "closed_at": time.time()})
        self.history = self.history
        self._note(f"CLOSE {sym} · {reason} · @ ${exit_price:,.2f} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")

    # ---- helpers + controls ----------------------------------------------
    def _equity(self):
        eq = self.balance
        for sym, p in self.positions.items():
            mark = self.prices.get(sym, p["entry"])
            eq += p["qty"] * (mark - p["entry"])
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_risk(self, risk):
        s = str(risk)
        if s not in RISKS:
            return {"ok": False, "error": f"risk must be one of {list(RISKS)}"}
        if self.positions:
            return {"ok": False, "error": "close open positions before changing risk"}
        self.risk = float(s)
        self._note(f"risk set to {self.risk}% per trade")
        self._save()
        return {"ok": True, "risk": self.risk}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.positions = {}
        self.history = []
        self.log = []
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        positions = []
        for sym, p in self.positions.items():
            mark = self.prices.get(sym, p["entry"])
            upnl = p["qty"] * (mark - p["entry"])
            R = p["entry"] - p["init_stop"]
            r_mult = ((mark - p["entry"]) / R) if R > 0 else 0.0
            positions.append({
                "symbol": sym, "kind": p["kind"], "entry": round(p["entry"], 2),
                "qty": round(p["qty"], 5), "stop": round(p["stop"], 2), "mark": round(mark, 2),
                "upnl": round(upnl, 2), "r_mult": round(r_mult, 2), "added": p.get("added", True),
                "took1": p.get("took1", False), "took2": p.get("took2", False),
            })
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled, "risk": self.risk, "risks": RISKS, "status": self.status,
            "regime": self.regime, "symbols": SYMBOLS, "snap": self.snap,
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BAL, "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100, 2),
            "positions": positions, "trades": len(self.history), "wins": wins,
            "history": self.history, "log": self.log[:25],
        }
