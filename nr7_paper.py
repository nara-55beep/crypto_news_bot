"""
nr7_paper.py - NR7 breakout Apex paper bot (ES + NQ + CL), the validated best-odds plan.

Strategy (the one that survived the 3-fold walk-forward on all 3 markets, all 3 years):
  * NR7 day = a regular session whose range (High-Low) is the NARROWEST of the last 7 sessions.
  * The NEXT session: breakout of that NR7 day's High (long) / Low (short). First side hit = entry,
    stop = the opposite end of the NR7 day (= risk R). Manage: 1/2 off at +1R, stop -> breakeven,
    runner to +2R. Flat by session close (Apex-legal, no overnight).
Account: $50k EOD-trailing Apex eval. LOCK-THE-TRAIL SPRINT sizing - risk $big until the trail locks
(peak >= $52,600 -> floor $50,100), then $small to coast to +$3,000. Micro contracts (MES/MNQ/MCL)
for fine sizing. Data: Yahoo 5m regular-hours candles, daily bars derived for NR7 detection.

Paper only. No broker/exchange orders are sent.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, asdict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import config

NY = ZoneInfo("America/New_York")

START_BALANCE = 50_000.0
TARGET_BALANCE = 53_000.0          # +$3,000 Apex pass target
MAX_EOD_DD = 2_500.0               # $2,500 trailing drawdown
LOCK_PEAK = 52_600.0               # once peak hits this, the trail locks...
FLOOR_LOCK = 50_100.0              # ...at $50,100 forever
RISK_BIG = 400.0                   # sprint risk/trade while trail not yet locked
RISK_SMALL = 150.0                 # coast risk/trade after the trail locks

# market -> (micro $/point, tick, micro label)
MARKETS = {
    "ES=F": (5.0, 0.25, "MES"),
    "NQ=F": (2.0, 0.25, "MNQ"),
    "CL=F": (100.0, 0.01, "MCL"),
}

INTERVAL = "5m"
YAHOO_RANGE = "60d"
POLL_SEC = 30
BAR_SEC = 5 * 60
SESSION_START = 9 * 60 + 30        # 09:30 ET
ENTRY_END = 14 * 60               # no new entries after 14:00
FLAT_MIN = 15 * 60 + 55           # flatten 15:55
STOP_BUF_TICKS = 1
SLIP_TICKS = 1.0
COMMISSION_RT = 0.50               # per micro round turn (approx)


@dataclass
class NR7Pos:
    id: str
    mkt: str
    side: str
    qty: int
    entry: float
    stop: float
    stop0: float
    tp1: float
    target: float
    r_points: float
    micro_pv: float
    risk_usd: float
    opened_at: float
    best: float
    half_done: bool = False
    realized: float = 0.0
    note: str = ""


def _minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def _fetch_yahoo(symbol: str) -> pd.DataFrame:
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
        "open": q.get("open"), "high": q.get("high"),
        "low": q.get("low"), "close": q.get("close"), "volume": q.get("volume"),
    }).dropna(subset=["open", "high", "low", "close"])
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    df = df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)
    return df


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    mins = d["dt_ny"].map(_minute)
    d = d[(mins >= SESSION_START) & (mins <= 16 * 60)].copy()
    d["day"] = d["dt_ny"].dt.date
    d = d.reset_index(drop=True)
    now = pd.Timestamp.now(tz="UTC")
    if len(d) and d.iloc[-1]["dt_utc"] + pd.Timedelta(seconds=BAR_SEC) > now:
        d = d.iloc[:-1].copy()      # drop the still-forming bar
    return d


def _daily(d: pd.DataFrame) -> pd.DataFrame:
    if d.empty:
        return d
    g = d.groupby("day")
    out = pd.DataFrame({
        "high": g["high"].max(), "low": g["low"].min(),
        "open": g["open"].first(), "close": g["close"].last(),
    }).reset_index()
    out["range"] = out["high"] - out["low"]
    return out


def _nr7_levels(daily: pd.DataFrame) -> tuple | None:
    """If the most recent COMPLETED day is an NR7 day, return (hi, lo, range). Else None."""
    if len(daily) < 8:
        return None
    last = daily.iloc[-1]
    prior6 = daily.iloc[-7:-1]
    if last["range"] < prior6["range"].min():
        return float(last["high"]), float(last["low"]), float(last["range"])
    return None


class NR7PaperBot:
    NAME = "NR7 Breakout Apex (ES+NQ+CL paper)"

    def __init__(self):
        self.enabled = False
        self.balance = START_BALANCE
        self.peak = START_BALANCE
        self.locked = False
        self.floor = START_BALANCE - MAX_EOD_DD
        self.day_key = ""
        self.day_pnl = 0.0
        self.pos: dict[str, NR7Pos] = {}     # mkt -> open position
        self.setups: dict[str, dict] = {}    # mkt -> {hi, lo, range, day, fired}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.prices: dict[str, float] = {}
        self.status = "starting..."
        self.data_error = ""
        self._df: dict[str, pd.DataFrame] = {}
        self._last_bar_ts: dict[str, int] = {}
        self._load()

    # ---- persistence ----
    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "nr7_state.json")

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled, "balance": self.balance, "peak": self.peak,
                    "locked": self.locked, "floor": self.floor, "day_key": self.day_key,
                    "day_pnl": self.day_pnl, "history": self.history[:120], "log": self.log[:80],
                    "pos": {m: asdict(p) for m, p in self.pos.items()},
                    "setups": self.setups,
                }, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.peak = float(d.get("peak", self.balance))
            self.locked = bool(d.get("locked", False))
            self.floor = float(d.get("floor", START_BALANCE - MAX_EOD_DD))
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.setups = d.get("setups", {}) or {}
            self.pos = {m: NR7Pos(**p) for m, p in (d.get("pos") or {}).items()}
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[nr7] {msg}")

    # ---- main loop ----
    async def manage_loop(self):
        await asyncio.sleep(9.0)
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                loop = asyncio.get_running_loop()
                results = await asyncio.gather(*[
                    loop.run_in_executor(None, _fetch_yahoo, sym) for sym in MARKETS
                ])
                for sym, raw in zip(MARKETS, results):
                    self._df[sym] = _prepare(raw)
                self.data_error = ""
                self._tick()
                self._save()
            except Exception as e:
                self.data_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "data error"
                self._save()

    def _risk_usd(self) -> float:
        return RISK_BIG if not self.locked else RISK_SMALL

    def _roll_day(self, day_key: str):
        if day_key == self.day_key:
            return
        # new session: bank EOD peak, refresh setups
        if self.day_key:
            self.peak = max(self.peak, self.balance)
            if not self.locked and self.peak >= LOCK_PEAK:
                self.locked = True; self.floor = FLOOR_LOCK
                self._note("trailing drawdown LOCKED at $50,100 - now coasting to +$3k", "win")
            elif not self.locked:
                self.floor = max(self.floor, self.peak - MAX_EOD_DD)
        self.day_key = day_key
        self.day_pnl = 0.0
        for m in self.setups:
            self.setups[m]["fired"] = False

    def _tick(self):
        # determine the latest common session day across markets that have data
        latest_day = None
        for sym, d in self._df.items():
            if not d.empty:
                latest_day = max(latest_day, d.iloc[-1]["day"]) if latest_day else d.iloc[-1]["day"]
        if latest_day is None:
            self.status = "waiting for ES/NQ/CL candles..."
            return
        self._roll_day(str(latest_day))

        for sym, d in self._df.items():
            if d.empty:
                continue
            self.prices[sym] = float(d.iloc[-1]["close"])
            # refresh NR7 setup from completed daily bars
            daily = _daily(d[d["day"] < latest_day]) if d.iloc[-1]["day"] == latest_day else _daily(d)
            lv = _nr7_levels(daily)
            if lv:
                hi, lo, rng = lv
                prev = self.setups.get(sym)
                self.setups[sym] = {"hi": hi, "lo": lo, "range": rng, "day": str(latest_day),
                                    "fired": (prev or {}).get("fired", False) if (prev or {}).get("day") == str(latest_day) else False}
            elif sym in self.setups and self.setups[sym].get("day") != str(latest_day):
                self.setups.pop(sym, None)

            today = d[d["day"] == latest_day].reset_index(drop=True)
            if today.empty:
                continue
            cur = today.iloc[-1]
            bar_ts = int(cur["dt_utc"].timestamp())
            if self._last_bar_ts.get(sym) == bar_ts:
                continue
            self._last_bar_ts[sym] = bar_ts

            if sym in self.pos:
                self._manage(sym, cur)
            if sym not in self.pos and self.enabled:
                self._scan(sym, cur)
        self._set_status()

    def _scan(self, sym: str, cur: pd.Series):
        s = self.setups.get(sym)
        if not s or s.get("fired") or s.get("day") != self.day_key:
            return
        minute = _minute(cur["dt_ny"])
        if minute < SESSION_START or minute > ENTRY_END:
            return
        pv, tick, label = MARKETS[sym]
        hi, lo = s["hi"], s["lo"]
        side = 0
        if float(cur["high"]) >= hi:
            side = 1; entry = hi + STOP_BUF_TICKS * tick; stop = lo - STOP_BUF_TICKS * tick
        elif float(cur["low"]) <= lo:
            side = -1; entry = lo - STOP_BUF_TICKS * tick; stop = hi + STOP_BUF_TICKS * tick
        if side == 0:
            return
        r_points = abs(entry - stop)
        if r_points <= 0:
            return
        risk_usd = self._risk_usd()
        qty = int(math.floor(risk_usd / (r_points * pv)))
        if qty < 1:
            qty = 1     # always take at least 1 micro
        fill = entry + side * tick * SLIP_TICKS
        tp1 = fill + side * r_points
        target = fill + side * 2 * r_points
        self.pos[sym] = NR7Pos(
            id=uuid.uuid4().hex[:6], mkt=sym, side="long" if side > 0 else "short", qty=qty,
            entry=fill, stop=stop, stop0=stop, tp1=tp1, target=target, r_points=r_points,
            micro_pv=pv, risk_usd=risk_usd, opened_at=time.time(), best=fill,
            note=f"NR7 {label} breakout, R={r_points:.2f}pt x {qty}",
        )
        s["fired"] = True
        phase = "SPRINT" if not self.locked else "coast"
        self._note(f"OPEN {self.pos[sym].side.upper()} {label} @ {fill:.2f} stop {stop:.2f} "
                   f"({qty} micro, risk ${risk_usd:.0f}, {phase})", "open")

    def _manage(self, sym: str, bar: pd.Series):
        p = self.pos[sym]
        side = 1 if p.side == "long" else -1
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        minute = _minute(bar["dt_ny"])
        if side > 0:
            p.best = max(p.best, hi); stopped = lo <= p.stop; tp1_hit = hi >= p.tp1; tgt_hit = hi >= p.target
        else:
            p.best = min(p.best, lo); stopped = hi >= p.stop; tp1_hit = lo <= p.tp1; tgt_hit = lo <= p.target
        if stopped:
            self._close(sym, p.stop, "be" if p.half_done else "stop"); return
        if not p.half_done and tp1_hit:
            half = p.qty // 2
            if half >= 1:
                self._partial(sym, half, p.tp1)
            p.half_done = True
            p.stop = p.entry      # runner to breakeven
            self._note(f"+1R {MARKETS[sym][2]} - half off, stop to breakeven", "info")
        if tgt_hit:
            self._close(sym, p.target, "target"); return
        if minute >= FLAT_MIN:
            self._close(sym, close, "eod")

    def _pnl(self, side, entry, exit_px, qty, pv):
        return side * (exit_px - entry) * qty * pv

    def _partial(self, sym: str, qty: int, raw_exit: float):
        p = self.pos[sym]
        side = 1 if p.side == "long" else -1
        exit_px = raw_exit - side * MARKETS[sym][1] * SLIP_TICKS
        pnl = self._pnl(side, p.entry, exit_px, qty, p.micro_pv) - qty * COMMISSION_RT
        self._book(pnl)
        p.qty -= qty
        p.realized += pnl
        self.history.insert(0, {"mkt": MARKETS[sym][2], "side": p.side, "entry": round(p.entry, 2),
                                "exit": round(exit_px, 2), "qty": qty, "pnl": round(pnl, 2),
                                "reason": "tp1", "closed_at": time.time()})

    def _close(self, sym: str, raw_exit: float, reason: str):
        p = self.pos[sym]
        side = 1 if p.side == "long" else -1
        exit_px = raw_exit - side * MARKETS[sym][1] * SLIP_TICKS
        pnl = self._pnl(side, p.entry, exit_px, p.qty, p.micro_pv) - p.qty * COMMISSION_RT
        self._book(pnl)
        total = p.realized + pnl
        self.history.insert(0, {"mkt": MARKETS[sym][2], "side": p.side, "entry": round(p.entry, 2),
                                "exit": round(exit_px, 2), "qty": p.qty, "pnl": round(pnl, 2),
                                "reason": reason, "closed_at": time.time()})
        self._note(f"CLOSE {MARKETS[sym][2]} @ {exit_px:.2f} - {reason} - trade P&L ${total:+.2f}",
                   "win" if total >= 0 else "loss")
        self.pos.pop(sym, None)

    def _book(self, pnl: float):
        self.balance += pnl
        self.day_pnl += pnl
        if self.balance > self.peak:
            self.peak = self.balance
            if not self.locked and self.peak >= LOCK_PEAK:
                self.locked = True; self.floor = FLOOR_LOCK
                self._note("trailing drawdown LOCKED at $50,100 - coasting to +$3k now", "win")
            elif not self.locked:
                self.floor = max(self.floor, self.peak - MAX_EOD_DD)

    def _set_status(self):
        if not self.enabled:
            self.status = "paused"
        elif self.pos:
            self.status = "in trade: " + ", ".join(MARKETS[m][2] for m in self.pos)
        elif self.balance >= TARGET_BALANCE:
            self.status = "APEX TARGET PASSED (+$3,000)"
        else:
            active = [MARKETS[m][2] for m in self.setups if self.setups[m].get("day") == self.day_key]
            self.status = ("watching NR7 setups: " + ", ".join(active)) if active else "live - no NR7 setup today"

    def equity(self) -> float:
        eq = self.balance
        for sym, p in self.pos.items():
            px = self.prices.get(sym, p.entry)
            side = 1 if p.side == "long" else -1
            eq += self._pnl(side, p.entry, px, p.qty, p.micro_pv)
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - NR7 breakout ES+NQ+CL" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = START_BALANCE; self.peak = START_BALANCE; self.locked = False
        self.floor = START_BALANCE - MAX_EOD_DD; self.day_key = ""; self.day_pnl = 0.0
        self.pos = {}; self.setups = {}; self.history = []; self.log = []; self._last_bar_ts = {}
        self._note("bot reset (Apex paper account back to $50,000)")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self.equity()
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        positions = []
        for sym, p in self.pos.items():
            px = self.prices.get(sym, p.entry); side = 1 if p.side == "long" else -1
            up = self._pnl(side, p.entry, px, p.qty, p.micro_pv)
            positions.append({"mkt": MARKETS[sym][2], "side": p.side, "entry": round(p.entry, 2),
                              "qty": p.qty, "stop": round(p.stop, 2), "tp2": round(p.target, 2),
                              "pnl": round(up + p.realized, 2),
                              "pnl_R": round((up + p.realized) / max(p.risk_usd, 1e-9), 2),
                              "news": p.note + (" - runner at breakeven" if p.half_done else "")})
        setups = [{"mkt": MARKETS[m][2], "hi": round(s["hi"], 2), "lo": round(s["lo"], 2),
                   "range": round(s["range"], 2), "fired": s.get("fired", False)}
                  for m, s in self.setups.items() if s.get("day") == self.day_key]
        return {
            "running": True, "enabled": self.enabled, "name": self.NAME, "status": self.status,
            "symbols": "ES + NQ + CL (micros)", "timeframe": INTERVAL,
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BALANCE, "total_pnl": round(eq - START_BALANCE, 2),
            "total_pnl_pct": round((eq / START_BALANCE - 1) * 100, 2),
            "apex_target": TARGET_BALANCE, "target_left": round(max(0.0, TARGET_BALANCE - eq), 2),
            "floor": round(self.floor, 2), "drawdown_room": round(eq - self.floor, 2),
            "trail_locked": self.locked, "phase": "coast (trail locked)" if self.locked else "SPRINT to lock",
            "risk_per_trade": self._risk_usd(), "day_pnl": round(self.day_pnl, 2),
            "trades": len(self.history), "wins": wins, "positions": positions, "setups": setups,
            "history": self.history[:60], "log": self.log[:25], "data_error": self.data_error,
        }
