from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import numpy as np
import pandas as pd

import config


SYMBOL = "BTC/USDT"
TIMEFRAME = "15m"
START_BAL = 100.0
LEVERAGE = 10.0
COST_BPS = 1.5
POLL_SEC = 30
MAX_HOLD_BARS = 72
BAR_MS = 15 * 60 * 1000

RSI_ENTRY = 8.0
RSI_EXIT_LONG = 70.0
RSI_EXIT_SHORT = 30.0
EMA_LEN = 50
STOP_PCT = 0.01
TAKE_PCT = 0.015
DAILY_STOP = 0.05
COOLDOWN_SEC = 12 * 60 * 60
ATR_LOOKBACK_BARS = 96 * 30


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


class RSI2ScalperPaperBot:
    def __init__(self, key: str, label: str, use_atr_filter: bool):
        self.key = key
        self.label = label
        self.use_atr_filter = bool(use_atr_filter)
        self.state_path = os.path.join(config.DATA_DIR, f"{key}_state.json")
        self.market = None
        self._ex = None
        self.enabled = True
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.snap: dict = {}
        self.price = 0.0
        self.status = "starting..."
        self._last_signal_bar = None
        self.cooldown_until = 0.0
        self.day_key = ""
        self.day_start_equity = START_BAL
        self.day_paused = False
        self._load()

    def attach(self, market):
        self.market = market

    def _save(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "enabled": self.enabled,
                        "balance": self.balance,
                        "pos": self.pos,
                        "history": self.history,
                        "log": self.log[:80],
                        "last_signal_bar": self._last_signal_bar,
                        "cooldown_until": self.cooldown_until,
                        "day_key": self.day_key,
                        "day_start_equity": self.day_start_equity,
                        "day_paused": self.day_paused,
                    },
                    f,
                )
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self.state_path):
                return
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self._last_signal_bar = d.get("last_signal_bar")
            self.cooldown_until = float(d.get("cooldown_until", 0.0))
            self.day_key = str(d.get("day_key", ""))
            self.day_start_equity = float(d.get("day_start_equity", self.balance))
            self.day_paused = bool(d.get("day_paused", False))
            print(f"[{self.key}] restored: bal ${self.balance:.2f}, {len(self.history)} trades")
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[{self.key}] {msg}")

    def _ensure_ex(self):
        if self._ex is None:
            import ccxt

            self._ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        return self._ex

    def _fetch(self) -> pd.DataFrame:
        ex = self._ensure_ex()
        now_ms = int(time.time() * 1000)
        days = 35 if self.use_atr_filter else 5
        since = now_ms - days * 24 * 60 * 60 * 1000
        rows: list[list] = []
        while True:
            batch = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + 1
            if nxt <= since or nxt >= now_ms - BAR_MS:
                break
            since = nxt
            if len(rows) > 4200:
                break
        if not rows:
            raise RuntimeError("no candles")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("ts").sort_values("ts")
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        df["ts"] = df["ts"].astype("int64")
        return df

    def _indi(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["ema50"] = _ema(d["close"], EMA_LEN)
        d["rsi2"] = _rsi(d["close"], 2)
        d["atr_pct"] = _atr(d, 14) / d["close"]
        d["atr_q15"] = d["atr_pct"].rolling(ATR_LOOKBACK_BARS, min_periods=96 * 10).quantile(0.15)
        d["atr_q85"] = d["atr_pct"].rolling(ATR_LOOKBACK_BARS, min_periods=96 * 10).quantile(0.85)
        return d

    async def manage_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                raw = await loop.run_in_executor(None, self._fetch)
                self._tick(self._indi(raw))
            except Exception as e:
                self.status = f"data error: {type(e).__name__}: {str(e)[:80]}"
                self._save()

    def _tick(self, d: pd.DataFrame):
        need = 1000 if self.use_atr_filter else 80
        if len(d) < need:
            self.status = "waiting for enough 15m candles..."
            return
        cur = d.iloc[-1]
        c2 = d.iloc[-2]
        self.price = float(cur["close"])
        self._roll_day()
        self.snap = {
            "price": round(self.price, 2),
            "ema50": round(float(c2["ema50"]), 2),
            "rsi2": round(float(c2["rsi2"]), 2),
            "atr_pct": round(float(c2["atr_pct"]) * 100, 4) if c2["atr_pct"] == c2["atr_pct"] else None,
            "atr_q15": round(float(c2["atr_q15"]) * 100, 4) if c2["atr_q15"] == c2["atr_q15"] else None,
            "atr_q85": round(float(c2["atr_q85"]) * 100, 4) if c2["atr_q85"] == c2["atr_q85"] else None,
            "atr_filter": self.use_atr_filter,
        }
        if self.pos:
            self._manage(cur, c2)
        elif self.enabled:
            self._maybe_enter(c2)
        self._set_status()
        self._save()

    def _roll_day(self):
        now_key = time.strftime("%Y-%m-%d", time.gmtime())
        if now_key != self.day_key:
            self.day_key = now_key
            self.day_start_equity = self._equity()
            self.day_paused = False
        if self.day_start_equity > 0 and self._equity() / self.day_start_equity - 1 <= -DAILY_STOP:
            self.day_paused = True

    def _set_status(self):
        parts = ["live" if self.enabled else "paused"]
        if self.day_paused:
            parts.append("daily stop active")
        remaining = int(max(0, self.cooldown_until - time.time()))
        if remaining:
            parts.append(f"cooldown {remaining // 60}m")
        if self.pos:
            parts.append("in trade")
        else:
            parts.append("flat")
        if self.snap.get("rsi2") is not None:
            parts.append(f"RSI2 {self.snap['rsi2']}")
        self.status = " - ".join(parts)

    def _atr_ok(self, c2) -> bool:
        if not self.use_atr_filter:
            return True
        vals = (c2["atr_pct"], c2["atr_q15"], c2["atr_q85"])
        if any(v != v for v in vals):
            return False
        return float(c2["atr_q15"]) <= float(c2["atr_pct"]) <= float(c2["atr_q85"])

    def _maybe_enter(self, c2):
        bar_id = int(c2["ts"])
        if self._last_signal_bar == bar_id:
            return
        if self.day_paused or time.time() < self.cooldown_until:
            return
        if not self._atr_ok(c2):
            return
        side = None
        if float(c2["close"]) > float(c2["ema50"]) and float(c2["rsi2"]) < RSI_ENTRY:
            side = "long"
        elif float(c2["close"]) < float(c2["ema50"]) and float(c2["rsi2"]) > 100 - RSI_ENTRY:
            side = "short"
        if side:
            self._open(self.price, side, bar_id)

    def _open(self, price: float, side: str, bar_id: int):
        eq = self._equity()
        if eq <= 0 or price <= 0:
            return
        notional = eq * LEVERAGE
        qty = notional / price
        entry_fee = notional * COST_BPS / 1e4
        self.balance -= entry_fee
        stop = price * (1 - STOP_PCT) if side == "long" else price * (1 + STOP_PCT)
        target = price * (1 + TAKE_PCT) if side == "long" else price * (1 - TAKE_PCT)
        self.pos = {
            "id": uuid.uuid4().hex[:6],
            "side": side,
            "entry": price,
            "qty": qty,
            "notional": notional,
            "margin": eq,
            "stop": stop,
            "target": target,
            "opened_at": time.time(),
            "bar_ts": bar_id,
            "entry_fee": entry_fee,
        }
        self._last_signal_bar = bar_id
        self._note(
            f"OPEN {side.upper()} @ ${price:,.2f} - 10x on ${eq:.2f} - SL 1% - TP 1.5%",
            "open",
        )

    def _manage(self, cur, c2):
        p = self.pos
        if not p:
            return
        hi = float(cur["high"])
        lo = float(cur["low"])
        price = float(cur["close"])
        side = p["side"]
        exit_px = None
        reason = None
        if side == "long":
            if lo <= p["stop"]:
                exit_px, reason = p["stop"], "SL"
            elif hi >= p["target"]:
                exit_px, reason = p["target"], "TP"
            elif float(c2["rsi2"]) > RSI_EXIT_LONG:
                exit_px, reason = price, "RSI"
        else:
            if hi >= p["stop"]:
                exit_px, reason = p["stop"], "SL"
            elif lo <= p["target"]:
                exit_px, reason = p["target"], "TP"
            elif float(c2["rsi2"]) < RSI_EXIT_SHORT:
                exit_px, reason = price, "RSI"
        if reason is None and int(cur["ts"]) - int(p.get("bar_ts", cur["ts"])) >= MAX_HOLD_BARS * BAR_MS:
            exit_px, reason = price, "TIME"
        if reason:
            self._close(exit_px, reason)

    def _close(self, price: float, reason: str):
        p = self.pos
        if not p:
            return
        side = p["side"]
        gross = p["qty"] * ((price - p["entry"]) if side == "long" else (p["entry"] - price))
        exit_fee = p["notional"] * COST_BPS / 1e4
        pnl = gross - exit_fee
        net_pnl = pnl - p.get("entry_fee", 0.0)
        self.balance = max(0.0, self.balance + pnl)
        ret_pct = net_pnl / max(p["margin"], 1e-9) * 100.0
        rec = {
            "side": side,
            "entry": round(p["entry"], 2),
            "exit": round(price, 2),
            "pnl": round(net_pnl, 2),
            "pnl_pct": round(ret_pct, 2),
            "reason": reason,
            "opened_at": p["opened_at"],
            "closed_at": time.time(),
        }
        self.history.insert(0, rec)
        self.pos = None
        if net_pnl < 0:
            self.cooldown_until = time.time() + COOLDOWN_SEC
        self._roll_day()
        self._note(
            f"CLOSE {side.upper()} @ ${price:,.2f} - {reason} - P&L ${rec['pnl']:+.2f} ({rec['pnl_pct']:+.2f}%)",
            "win" if rec["pnl"] >= 0 else "loss",
        )

    def _equity(self) -> float:
        eq = self.balance
        if self.pos and self.price:
            p = self.pos
            if p["side"] == "long":
                eq += p["qty"] * (self.price - p["entry"])
            else:
                eq += p["qty"] * (p["entry"] - self.price)
        return max(0.0, eq)

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("ENABLED" if self.enabled else "PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self.snap = {}
        self._last_signal_bar = None
        self.cooldown_until = 0.0
        self.day_key = ""
        self.day_start_equity = START_BAL
        self.day_paused = False
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        pos = None
        if self.pos:
            p = self.pos
            upnl = p["qty"] * ((self.price - p["entry"]) if p["side"] == "long" else (p["entry"] - self.price))
            pos = {
                "side": p["side"],
                "entry": round(p["entry"], 2),
                "qty": round(p["qty"], 6),
                "notional": round(p["notional"], 2),
                "stop": round(p["stop"], 2),
                "tp1": round(p["target"], 2),
                "mark": round(self.price, 2),
                "pnl": round(upnl, 2),
                "pnl_pct": round(upnl / max(p["margin"], 1e-9) * 100.0, 2),
                "leverage": int(LEVERAGE),
            }
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        cooldown_left = max(0, int(self.cooldown_until - time.time()))
        return {
            "running": True,
            "enabled": self.enabled,
            "label": self.label,
            "status": self.status,
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "leverage": int(LEVERAGE),
            "atr_filter": self.use_atr_filter,
            "daily_stop_pct": DAILY_STOP * 100,
            "cooldown_hours": COOLDOWN_SEC / 3600,
            "cost_bps": COST_BPS,
            "snap": self.snap,
            "price": round(self.price, 2) if self.price else None,
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "start_balance": START_BAL,
            "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100, 2),
            "positions": [pos] if pos else [],
            "trades": len(self.history),
            "wins": wins,
            "history": self.history,
            "log": self.log[:25],
            "day_paused": self.day_paused,
            "day_pnl_pct": round((eq / self.day_start_equity - 1) * 100, 2) if self.day_start_equity else 0.0,
            "cooldown_left": cooldown_left,
        }
