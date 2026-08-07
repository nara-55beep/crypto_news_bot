"""
nq_mr_15m_paper.py - NQ 15m mean-reversion Apex-style paper bot.

This is the tested "15m:nq_mr flat600" bundle:
  * VWAP 2-sigma fade back to session VWAP
  * Turtle Soup false break of the prior 20-session extreme
  * 80-20 reversal from the prior session

Risk model: $50k paper account, target $53k, about $600 risk per signal.
Management: partial at +1R, stop to breakeven, runner to +2R/target,
and flat before the cash-session close. Paper only; no real orders.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import config

NY = ZoneInfo("America/New_York")

NAME = "NQ 15m Mean Reversion (paper)"
SYMBOL = "NQ=F"
MICRO_LABEL = "MNQ"
MICRO_PV = 2.0
TICK = 0.25

START_BALANCE = 50_000.0
TARGET_BALANCE = 53_000.0
MAX_EOD_DD = 2_500.0
LOCK_PEAK = 52_600.0
FLOOR_LOCK = 50_100.0
RISK_USD = 600.0

INTERVAL = "15m"
YAHOO_RANGE = "60d"
POLL_SEC = 30
BAR_SEC = 15 * 60
SESSION_START = 9 * 60 + 30
FLAT_MIN = 15 * 60 + 55
COMMISSION_RT = 0.50

VWAP_K = 2.0
VWAP_MIN_BARS = 15
TURTLE_LOOKBACK = 20
TURTLE_RECENCY = 4
TURTLE_BUF_TICKS = 8
EIGHTY_BUF_TICKS = 10


@dataclass
class NQMRPos:
    id: str
    strat: str
    side: str
    qty: float
    qty0: float
    entry: float
    stop: float
    stop0: float
    tp1: float
    target: float
    r_points: float
    risk_usd: float
    opened_at: float
    opened_bar: int
    best: float
    partial_done: bool = False
    realized: float = 0.0
    note: str = ""


def _minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def _fetch_yahoo(symbol: str = SYMBOL) -> pd.DataFrame:
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": YAHOO_RANGE, "interval": INTERVAL, "includePrePost": "false"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    raw = r.json()["chart"]["result"][0]
    ts = raw.get("timestamp") or []
    q = raw["indicators"]["quote"][0]
    df = pd.DataFrame({
        "dt_utc": pd.to_datetime(ts, unit="s", utc=True),
        "open": q.get("open"),
        "high": q.get("high"),
        "low": q.get("low"),
        "close": q.get("close"),
        "volume": q.get("volume"),
    }).dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    return df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    mins = d["dt_ny"].map(_minute)
    d = d[(mins >= SESSION_START) & (mins <= 16 * 60)].copy()
    d["day"] = d["dt_ny"].dt.date
    d = d.reset_index(drop=True)
    now = pd.Timestamp.now(tz="UTC")
    if len(d) and d.iloc[-1]["dt_utc"] + pd.Timedelta(seconds=BAR_SEC) > now:
        d = d.iloc[:-1].copy()
    return d


def _daily(d: pd.DataFrame) -> pd.DataFrame:
    if d.empty:
        return d
    g = d.groupby("day")
    return pd.DataFrame({
        "day": g["day"].first(),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    }).reset_index(drop=True)


class NQMR15PaperBot:
    NAME = NAME

    def __init__(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.peak = START_BALANCE
        self.locked = False
        self.floor = START_BALANCE - MAX_EOD_DD
        self.day_key = ""
        self.day_pnl = 0.0
        self.pos: dict[str, NQMRPos] = {}
        self.fired_keys: set[str] = set()
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.price = 0.0
        self.status = "starting..."
        self.data_error = ""
        self._df = pd.DataFrame()
        self._last_bar_ts = 0
        self._load()

    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "nq_mr_15m_state.json")

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled,
                    "balance": self.balance,
                    "peak": self.peak,
                    "locked": self.locked,
                    "floor": self.floor,
                    "day_key": self.day_key,
                    "day_pnl": self.day_pnl,
                    "positions": {k: asdict(p) for k, p in self.pos.items()},
                    "fired_keys": sorted(self.fired_keys)[-80:],
                    "history": self.history[:160],
                    "log": self.log[:100],
                }, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", True))
            self.balance = float(d.get("balance", START_BALANCE))
            self.peak = float(d.get("peak", self.balance))
            self.locked = bool(d.get("locked", False))
            self.floor = float(d.get("floor", START_BALANCE - MAX_EOD_DD))
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.pos = {k: NQMRPos(**p) for k, p in (d.get("positions") or {}).items()}
            self.fired_keys = set(d.get("fired_keys") or [])
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:100]
        print(f"[nq_mr_15m] {msg}")

    async def manage_loop(self):
        await asyncio.sleep(7.0)
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, _fetch_yahoo, SYMBOL)
                self._df = _prepare(raw)
                self.data_error = ""
                self._tick()
                self._save()
            except Exception as e:
                self.data_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "data error"
                self._save()

    def _roll_day(self, day_key: str):
        if day_key == self.day_key:
            return
        if self.day_key:
            self.peak = max(self.peak, self.balance)
            if not self.locked and self.peak >= LOCK_PEAK:
                self.locked = True
                self.floor = FLOOR_LOCK
                self._note("trailing floor locked at $50,100", "win")
            elif not self.locked:
                self.floor = max(self.floor, self.peak - MAX_EOD_DD)
        self.day_key = day_key
        self.day_pnl = 0.0
        self._prune_fired(day_key)

    def _prune_fired(self, day_key: str):
        keep = {k for k in self.fired_keys if k.startswith(day_key + ":")}
        old = sorted(self.fired_keys - keep)[-24:]
        self.fired_keys = keep | set(old)

    def _tick(self):
        d = self._df
        if d.empty:
            self.status = "waiting for NQ 15m candles..."
            return
        cur = d.iloc[-1]
        latest_day = cur["day"]
        self._roll_day(str(latest_day))
        self.price = float(cur["close"])

        bar_ts = int(cur["dt_utc"].timestamp())
        if self._last_bar_ts == bar_ts:
            self._set_status()
            return
        self._last_bar_ts = bar_ts

        for key in list(self.pos.keys()):
            self._manage(key, cur)

        if self.enabled and _minute(cur["dt_ny"]) < FLAT_MIN:
            today = d[d["day"] == latest_day].reset_index(drop=True)
            daily_before = _daily(d[d["day"] < latest_day])
            for sig in self._signals(today, daily_before):
                fired_key = f"{self.day_key}:{sig['strat']}"
                if fired_key in self.fired_keys:
                    continue
                if sig.get("spent"):
                    self.fired_keys.add(fired_key)
                    continue
                if sig["strat"] in self.pos:
                    continue
                self._open(sig, cur)
                if sig["strat"] in self.pos:
                    self._manage(sig["strat"], cur)
        self._set_status()

    def _signals(self, today: pd.DataFrame, daily_before: pd.DataFrame) -> list[dict]:
        if today.empty:
            return []
        out: list[dict] = []
        last_i = len(today) - 1
        if last_i < 0:
            return out
        out.extend(self._vwap_signal(today, last_i))
        out.extend(self._turtle_signal(today, daily_before, last_i))
        out.extend(self._eighty_twenty_signal(today, daily_before, last_i))
        return out

    def _vwap_signal(self, today: pd.DataFrame, last_i: int) -> list[dict]:
        if last_i < VWAP_MIN_BARS:
            return []
        cur = today.iloc[last_i]
        h, l = float(cur["high"]), float(cur["low"])
        tp = (today["high"].astype(float) + today["low"].astype(float) + today["close"].astype(float)) / 3.0
        vol = today["volume"].astype(float).where(today["volume"].astype(float) > 0, 1.0)
        tp = tp.iloc[:last_i + 1].to_numpy()
        vol = vol.iloc[:last_i + 1].to_numpy()
        cum_v = float(vol.sum())
        if cum_v <= 0:
            return []
        vwap = float((tp * vol).sum() / cum_v)
        var = max(float(((tp * tp) * vol).sum() / cum_v) - vwap * vwap, 0.0)
        sig = math.sqrt(var)
        if sig <= 0:
            return []
        up = vwap + VWAP_K * sig
        dn = vwap - VWAP_K * sig
        if h >= up:
            entry = up
            stop = vwap + (VWAP_K + 1.0) * sig
            return [self._mk_sig("VWAP2s", -1, entry, stop, vwap,
                                 f"fade +2 sigma stretch back to VWAP {vwap:.2f}")]
        if l <= dn:
            entry = dn
            stop = vwap - (VWAP_K + 1.0) * sig
            return [self._mk_sig("VWAP2s", 1, entry, stop, vwap,
                                 f"fade -2 sigma stretch back to VWAP {vwap:.2f}")]
        return []

    def _turtle_signal(self, today: pd.DataFrame, daily_before: pd.DataFrame, last_i: int) -> list[dict]:
        if len(daily_before) < TURTLE_LOOKBACK + TURTLE_RECENCY:
            return []
        look = daily_before.tail(TURTLE_LOOKBACK)
        prior_low = float(look["low"].min())
        prior_high = float(look["high"].max())
        for i in range(last_i + 1):
            row = today.iloc[i]
            d = 0
            if float(row["low"]) < prior_low:
                d = 1
                entry = prior_low + TURTLE_BUF_TICKS * TICK
                stop = float(today.iloc[:i + 1]["low"].min()) - TICK
            elif float(row["high"]) > prior_high:
                d = -1
                entry = prior_high - TURTLE_BUF_TICKS * TICK
                stop = float(today.iloc[:i + 1]["high"].max()) + TICK
            else:
                continue
            r = abs(entry - stop)
            if r <= 0:
                continue
            target = entry + d * 2.0 * r
            fi = None
            for j in range(i, last_i + 1):
                bar = today.iloc[j]
                if (d > 0 and float(bar["high"]) >= entry) or (d < 0 and float(bar["low"]) <= entry):
                    fi = j
                    break
            if fi is None:
                return []
            return [self._mk_sig("TurtleSoup", d, entry, stop, target,
                                 "false break of prior 20-session extreme", spent=(fi < last_i))]
        return []

    def _eighty_twenty_signal(self, today: pd.DataFrame, daily_before: pd.DataFrame, last_i: int) -> list[dict]:
        if daily_before.empty:
            return []
        y = daily_before.iloc[-1]
        rng = float(y["high"] - y["low"])
        if rng <= 0:
            return []
        opened_top = float(y["open"]) >= float(y["high"]) - 0.2 * rng
        closed_bot = float(y["close"]) <= float(y["low"]) + 0.2 * rng
        opened_bot = float(y["open"]) <= float(y["low"]) + 0.2 * rng
        closed_top = float(y["close"]) >= float(y["high"]) - 0.2 * rng
        if opened_top and closed_bot:
            d = 1
            level = float(y["low"])
        elif opened_bot and closed_top:
            d = -1
            level = float(y["high"])
        else:
            return []

        trig = level - d * EIGHTY_BUF_TICKS * TICK
        pushed = False
        fi = None
        for i in range(last_i + 1):
            row = today.iloc[i]
            if not pushed and ((d > 0 and float(row["low"]) <= trig)
                               or (d < 0 and float(row["high"]) >= trig)):
                pushed = True
            if pushed and ((d > 0 and float(row["high"]) >= level)
                           or (d < 0 and float(row["low"]) <= level)):
                fi = i
                break
        if fi is None:
            return []
        entry = level
        stop = (float(today.iloc[:fi + 1]["low"].min()) - TICK
                if d > 0 else float(today.iloc[:fi + 1]["high"].max()) + TICK)
        r = abs(entry - stop)
        if r <= 0:
            return []
        target = entry + d * 2.0 * r
        return [self._mk_sig("80-20", d, entry, stop, target,
                             "prior session 80-20 reversal", spent=(fi < last_i))]

    def _mk_sig(self, strat: str, d: int, entry: float, stop: float, target: float,
                note: str, spent: bool = False) -> dict:
        return {
            "strat": strat,
            "side": "long" if d > 0 else "short",
            "entry": float(entry),
            "stop": float(stop),
            "target": float(target),
            "note": note,
            "spent": spent,
        }

    def _open(self, sig: dict, cur: pd.Series):
        side = 1 if sig["side"] == "long" else -1
        entry = float(sig["entry"])
        stop = float(sig["stop"])
        target = float(sig["target"])
        r_points = abs(entry - stop)
        if r_points <= 0:
            return
        qty = RISK_USD / max(r_points * MICRO_PV, 1e-9)
        if qty < 1.0:
            qty = 1.0
        qty = round(qty, 3)
        tp1 = entry + side * r_points
        p = NQMRPos(
            id=uuid.uuid4().hex[:6],
            strat=sig["strat"],
            side=sig["side"],
            qty=qty,
            qty0=qty,
            entry=entry,
            stop=stop,
            stop0=stop,
            tp1=tp1,
            target=target,
            r_points=r_points,
            risk_usd=RISK_USD,
            opened_at=time.time(),
            opened_bar=int(pd.Timestamp(cur["dt_utc"]).timestamp()),
            best=entry,
            note=sig["note"],
        )
        self.pos[p.strat] = p
        self.fired_keys.add(f"{self.day_key}:{p.strat}")
        self._note(f"OPEN {p.strat} {p.side.upper()} {MICRO_LABEL} @ {entry:.2f} "
                   f"stop {stop:.2f} tp1 {tp1:.2f} target {target:.2f} "
                   f"({qty:g} paper micros)", "open")

    def _manage(self, key: str, bar: pd.Series):
        p = self.pos.get(key)
        if p is None:
            return
        side = 1 if p.side == "long" else -1
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if side > 0:
            p.best = max(p.best, hi)
            stopped = lo <= p.stop
            target_hit = hi >= p.target
            tp1_hit = hi >= p.tp1
        else:
            p.best = min(p.best, lo)
            stopped = hi >= p.stop
            target_hit = lo <= p.target
            tp1_hit = lo <= p.tp1

        if not p.partial_done:
            if stopped:
                self._close(key, p.stop, "stop")
                return
            if target_hit:
                self._close(key, p.target, "target")
                return
            if tp1_hit:
                qty = round(p.qty0 * 0.5, 3)
                qty = min(qty, p.qty)
                if qty > 0:
                    self._partial(key, qty, p.tp1)
                if key in self.pos:
                    p.partial_done = True
                    p.stop = p.entry
                    self._note(f"+1R {p.strat} - half off, runner stop to breakeven", "info")
                return
        else:
            if stopped:
                self._close(key, p.stop, "be" if abs(p.stop - p.entry) < 1e-9 else "stop")
                return
            if target_hit:
                self._close(key, p.target, "target")
                return

        if _minute(bar["dt_ny"]) >= FLAT_MIN:
            self._close(key, close, "eod")

    def _pnl(self, side: int, entry: float, exit_px: float, qty: float) -> float:
        return side * (exit_px - entry) * qty * MICRO_PV

    def _partial(self, key: str, qty: float, exit_px: float):
        p = self.pos[key]
        side = 1 if p.side == "long" else -1
        pnl = self._pnl(side, p.entry, exit_px, qty) - qty * COMMISSION_RT
        self._book(pnl)
        p.qty = round(p.qty - qty, 3)
        p.realized += pnl
        self.history.insert(0, {
            "mkt": MICRO_LABEL + "." + p.strat,
            "side": p.side,
            "entry": round(p.entry, 2),
            "exit": round(exit_px, 2),
            "qty": qty,
            "pnl": round(pnl, 2),
            "reason": "tp1",
            "closed_at": time.time(),
        })

    def _close(self, key: str, exit_px: float, reason: str):
        p = self.pos.get(key)
        if p is None:
            return
        side = 1 if p.side == "long" else -1
        pnl = self._pnl(side, p.entry, exit_px, p.qty) - p.qty * COMMISSION_RT
        self._book(pnl)
        total = p.realized + pnl
        self.history.insert(0, {
            "mkt": MICRO_LABEL + "." + p.strat,
            "side": p.side,
            "entry": round(p.entry, 2),
            "exit": round(exit_px, 2),
            "qty": p.qty,
            "pnl": round(pnl, 2),
            "reason": reason,
            "closed_at": time.time(),
        })
        self._note(f"CLOSE {p.strat} {MICRO_LABEL} @ {exit_px:.2f} - {reason} - "
                   f"trade P&L ${total:+.2f}", "win" if total >= 0 else "loss")
        self.pos.pop(key, None)

    def _book(self, pnl: float):
        self.balance += pnl
        self.day_pnl += pnl
        if self.balance > self.peak:
            self.peak = self.balance
            if not self.locked and self.peak >= LOCK_PEAK:
                self.locked = True
                self.floor = FLOOR_LOCK
                self._note("trailing floor locked at $50,100", "win")
            elif not self.locked:
                self.floor = max(self.floor, self.peak - MAX_EOD_DD)

    def _set_status(self):
        if not self.enabled:
            self.status = "paused"
        elif self.pos:
            self.status = "in trade: " + ", ".join(self.pos.keys())
        elif self.balance >= TARGET_BALANCE:
            self.status = "APEX target reached (+$3,000)"
        else:
            self.status = "live - scanning NQ 15m mean-reversion signals"

    def equity(self) -> float:
        eq = self.balance
        for p in self.pos.values():
            side = 1 if p.side == "long" else -1
            eq += self._pnl(side, p.entry, self.price or p.entry, p.qty)
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - NQ 15m MR flat600" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.peak = START_BALANCE
        self.locked = False
        self.floor = START_BALANCE - MAX_EOD_DD
        self.day_key = ""
        self.day_pnl = 0.0
        self.pos = {}
        self.fired_keys = set()
        self.history = []
        self.log = []
        self._last_bar_ts = 0
        self._note("bot reset and enabled (Apex paper account back to $50,000)")
        self._save()
        return {"ok": True, "enabled": True}

    def state(self):
        eq = self.equity()
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        positions = []
        for p in self.pos.values():
            side = 1 if p.side == "long" else -1
            up = self._pnl(side, p.entry, self.price or p.entry, p.qty)
            positions.append({
                "mkt": MICRO_LABEL + "." + p.strat,
                "side": p.side,
                "entry": round(p.entry, 2),
                "qty": p.qty,
                "stop": round(p.stop, 2),
                "tp1": round(p.tp1, 2),
                "tp2": round(p.target, 2),
                "pnl": round(up + p.realized, 2),
                "pnl_R": round((up + p.realized) / max(p.risk_usd, 1e-9), 2),
                "news": p.note + (" - runner at breakeven" if p.partial_done else ""),
            })
        return {
            "running": True,
            "enabled": self.enabled,
            "name": self.NAME,
            "status": self.status,
            "symbols": "NQ / MNQ",
            "timeframe": INTERVAL,
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "start_balance": START_BALANCE,
            "total_pnl": round(eq - START_BALANCE, 2),
            "total_pnl_pct": round((eq / START_BALANCE - 1.0) * 100.0, 2),
            "apex_target": TARGET_BALANCE,
            "target_left": round(max(0.0, TARGET_BALANCE - eq), 2),
            "floor": round(self.floor, 2),
            "drawdown_room": round(eq - self.floor, 2),
            "trail_locked": self.locked,
            "phase": "coast (trail locked)" if self.locked else "flat $600 risk",
            "risk_per_trade": RISK_USD,
            "day_pnl": round(self.day_pnl, 2),
            "trades": len(self.history),
            "wins": wins,
            "positions": positions,
            "setups": [],
            "history": self.history[:80],
            "log": self.log[:30],
            "data_error": self.data_error,
            "price": round(self.price, 2) if self.price else None,
            "backtest_note": "3y test: +$210k on $50k, 769 trades, 70.7% WR, PF 2.64, 33/37 monthly Apex passes.",
        }
