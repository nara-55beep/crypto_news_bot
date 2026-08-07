"""
apex_vwap_paper.py - ES 5m Apex-style VWAP + Opening Range paper bot.

This mirrors the backtested rule set in research/apex_vwap_orb_bot.py:

  - ES=F 5m regular-hours candles, NQ=F as peer confirmation.
  - First 15 minutes define the opening range.
  - Trade only after OR break agrees with VWAP, EMA9/EMA20, and NQ direction.
  - Do not chase: wait for pullback into OR/VWAP/EMA, then rejection candle.
  - Enter on the next completed bar's open, 1 ES contract.
  - Stop behind the rejection candle, min/max ATR guard, 2.5R target, 1R trail after +1R.
  - Apex-style account display: $50k start, $3k pass target, $2k EOD drawdown guard.

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
TARGET_BALANCE = 53_000.0
MAX_EOD_DD = 2_000.0
MAX_DAILY_LOSS = 1_000.0

ES_SYMBOL = "ES=F"
NQ_SYMBOL = "NQ=F"
POINT_VALUE = 50.0
TICK = 0.25

INTERVAL = "5m"
YAHOO_RANGE = "60d"
POLL_SEC = 30
BAR_SEC = 5 * 60

RISK_USD = 1_000.0
MAX_CONTRACTS = 1
MAX_TRADES_DAY = 2
OR_MINUTES = 15
TRADE_END = "10:30"
FLAT_TIME = "15:55"
MIN_STOP_ATR = 0.35
MAX_STOP_ATR = 1.8
PULLBACK_TOL_ATR = 0.15
RR1 = 1.0
RR_FINAL = 2.5
TRAIL_R = 1.0
SLIP_TICKS = 1.0
COMMISSION_RT = 1.24


@dataclass
class ApexPos:
    id: str
    side: str
    qty: int
    entry: float
    stop: float
    stop0: float
    target: float
    r_points: float
    opened_at: float
    entry_bar_ts: int
    best: float
    tp1: float
    tp1_done: bool = False
    realized: float = 0.0
    note: str = ""


def _minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - pc).abs(),
            (df["low"] - pc).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _fetch_yahoo(symbol: str) -> pd.DataFrame:
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": YAHOO_RANGE, "interval": INTERVAL, "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    raw = r.json()["chart"]["result"][0]
    ts = raw.get("timestamp") or []
    q = raw["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "dt_utc": pd.to_datetime(ts, unit="s", utc=True),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "close": q.get("close"),
            "volume": q.get("volume"),
        }
    ).dropna(subset=["open", "high", "low", "close"])
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    df = df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)
    return df


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    mins = d["dt_ny"].map(_minute)
    d = d[(mins >= 9 * 60 + 30) & (mins <= 16 * 60)].copy()
    d["day"] = d["dt_ny"].dt.date
    d = d.reset_index(drop=True)

    # Yahoo usually includes the still-forming latest 5m candle. Drop it so the
    # live paper bot acts only on completed bars, matching the backtest.
    now = pd.Timestamp.now(tz="UTC")
    if len(d) and d.iloc[-1]["dt_utc"] + pd.Timedelta(seconds=BAR_SEC) > now:
        d = d.iloc[:-1].copy()

    d["ema9"] = _ema(d["close"], 9)
    d["ema20"] = _ema(d["close"], 20)
    d["atr14"] = _atr(d, 14)
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    d["pv"] = tp * d["volume"].fillna(0)
    d["cum_pv"] = d.groupby("day")["pv"].cumsum()
    d["cum_v"] = d.groupby("day")["volume"].transform(lambda x: x.fillna(0).cumsum())
    d["vwap"] = d["cum_pv"] / d["cum_v"].replace(0, np.nan)
    return d.drop(columns=["pv", "cum_pv", "cum_v"])


def _row_by_time(df: pd.DataFrame) -> dict[pd.Timestamp, pd.Series]:
    return {row["dt_utc"]: row for _, row in df.iterrows()}


def _peer_ok(peer_row: pd.Series | None, side: int) -> bool:
    if peer_row is None:
        return False
    if side > 0:
        return peer_row["close"] > peer_row["vwap"] and peer_row["ema9"] >= peer_row["ema20"]
    return peer_row["close"] < peer_row["vwap"] and peer_row["ema9"] <= peer_row["ema20"]


def _levels(row: pd.Series, or_hi: float, or_lo: float, side: int) -> list[float]:
    return [or_hi, row["vwap"], row["ema9"], row["ema20"]] if side > 0 else [or_lo, row["vwap"], row["ema9"], row["ema20"]]


def _rejection(row: pd.Series, side: int) -> bool:
    rng = max(float(row["high"] - row["low"]), 1e-9)
    close_pos = float(row["close"] - row["low"]) / rng
    if side > 0:
        return row["close"] > row["open"] and close_pos >= 0.55
    return row["close"] < row["open"] and close_pos <= 0.45


def _touch_pullback(row: pd.Series, levels: list[float], side: int, tol: float) -> bool:
    clean = [float(x) for x in levels if np.isfinite(x)]
    if side > 0:
        return any(row["low"] <= x + tol and row["close"] > x for x in clean)
    return any(row["high"] >= x - tol and row["close"] < x for x in clean)


def _entry_price(px: float, side: int) -> float:
    return px + side * TICK * SLIP_TICKS


def _exit_price(px: float, side: int) -> float:
    return px - side * TICK * SLIP_TICKS


def _pnl(side: int, entry: float, exit_px: float, qty: int) -> float:
    return side * (exit_px - entry) * qty * POINT_VALUE


class ApexVWAPPaperBot:
    NAME = "Apex ES VWAP ORB (paper)"

    def __init__(self):
        self.enabled = False
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.eod_peak = START_BALANCE
        self.eod_threshold = START_BALANCE - MAX_EOD_DD
        self.day_key = ""
        self.day_pnl = 0.0
        self.trades_today = 0
        self.pos: ApexPos | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.snap: dict = {}
        self.price = 0.0
        self.status = "starting..."
        self.data_error = ""
        self._es = pd.DataFrame()
        self._nq = pd.DataFrame()
        self._last_bar_ts = 0
        self._pending: dict | None = None
        self._breakout_side = 0
        self._load()

    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "apex_vwap_state.json")

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "enabled": self.enabled,
                        "balance": self.balance,
                        "start_balance": self.start_balance,
                        "eod_peak": self.eod_peak,
                        "eod_threshold": self.eod_threshold,
                        "day_key": self.day_key,
                        "day_pnl": self.day_pnl,
                        "trades_today": self.trades_today,
                        "pos": asdict(self.pos) if self.pos else None,
                        "history": self.history,
                        "log": self.log[:80],
                        "pending": self._pending,
                        "breakout_side": self._breakout_side,
                    },
                    f,
                )
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.start_balance = float(d.get("start_balance", self.start_balance))
            self.eod_peak = float(d.get("eod_peak", max(self.start_balance, self.balance)))
            self.eod_threshold = float(d.get("eod_threshold", self.start_balance - MAX_EOD_DD))
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.trades_today = int(d.get("trades_today", 0))
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self._pending = d.get("pending") or None
            self._breakout_side = int(d.get("breakout_side", 0))
            pos = d.get("pos") or None
            self.pos = ApexPos(**pos) if pos else None
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[apex-vwap] {msg}")

    async def manage_loop(self):
        await asyncio.sleep(8.0)
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                loop = asyncio.get_running_loop()
                es_raw, nq_raw = await asyncio.gather(
                    loop.run_in_executor(None, _fetch_yahoo, ES_SYMBOL),
                    loop.run_in_executor(None, _fetch_yahoo, NQ_SYMBOL),
                )
                self._es = _prepare(es_raw)
                self._nq = _prepare(nq_raw)
                self.data_error = ""
                self._tick()
                self._save()
            except Exception as e:
                self.data_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "data error"
                self._save()

    def _current_day_df(self) -> pd.DataFrame:
        if self._es.empty:
            return self._es
        day = self._es.iloc[-1]["day"]
        return self._es[self._es["day"] == day].reset_index(drop=True)

    def _roll_day(self, day: object):
        key = str(day)
        if key == self.day_key:
            return
        if self.day_key:
            self.eod_peak = max(self.eod_peak, self.balance)
            self.eod_threshold = max(self.eod_threshold, self.eod_peak - MAX_EOD_DD)
        self.day_key = key
        self.day_pnl = 0.0
        self.trades_today = 0
        self._pending = None
        self._breakout_side = 0

    def _tick(self):
        g = self._current_day_df()
        if len(g) < 4:
            self.status = "waiting for ES 5m regular-hours candles..."
            return
        cur = g.iloc[-1]
        self.price = float(cur["close"])
        self._roll_day(cur["day"])
        self._snap(g, cur)
        bar_ts = int(cur["dt_utc"].timestamp())
        if bar_ts == self._last_bar_ts:
            return
        self._last_bar_ts = bar_ts

        if self.pos is not None:
            self._manage_bar(cur)

        if self.pos is None and self._pending is not None:
            if bar_ts > int(self._pending.get("signal_ts", 0)):
                self._fill_pending(cur)

        if self.pos is None and self.enabled:
            self._scan_signal(g)
        self._set_status()

    def _snap(self, g: pd.DataFrame, cur: pd.Series):
        or_bars = max(1, OR_MINUTES // 5)
        or_part = g.iloc[:or_bars]
        self.snap = {
            "es": round(float(cur["close"]), 2),
            "vwap": round(float(cur["vwap"]), 2) if np.isfinite(cur["vwap"]) else None,
            "ema9": round(float(cur["ema9"]), 2) if np.isfinite(cur["ema9"]) else None,
            "ema20": round(float(cur["ema20"]), 2) if np.isfinite(cur["ema20"]) else None,
            "atr14": round(float(cur["atr14"]), 2) if np.isfinite(cur["atr14"]) else None,
            "or_high": round(float(or_part["high"].max()), 2) if len(or_part) else None,
            "or_low": round(float(or_part["low"].min()), 2) if len(or_part) else None,
            "breakout": "long" if self._breakout_side > 0 else ("short" if self._breakout_side < 0 else "none"),
            "pending": bool(self._pending),
        }

    def _scan_signal(self, g: pd.DataFrame):
        or_bars = max(1, OR_MINUTES // 5)
        if len(g) <= or_bars + 1:
            return
        row = g.iloc[-1]
        minute = _minute(row["dt_ny"])
        if minute > _hhmm(TRADE_END):
            self.status = "outside entry window"
            return
        if self.day_pnl <= -MAX_DAILY_LOSS:
            self.status = "daily loss stop active"
            return
        if self.trades_today >= MAX_TRADES_DAY:
            self.status = "max trades reached today"
            return
        if not np.isfinite(row["vwap"]) or not np.isfinite(row["atr14"]):
            return

        peer_row = _row_by_time(self._nq).get(row["dt_utc"])
        or_part = g.iloc[:or_bars]
        or_hi = float(or_part["high"].max())
        or_lo = float(or_part["low"].min())

        if self._breakout_side == 0:
            long_break = row["close"] > or_hi and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"] and _peer_ok(peer_row, 1)
            short_break = row["close"] < or_lo and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"] and _peer_ok(peer_row, -1)
            if long_break:
                self._breakout_side = 1
                self._note(f"ES broke OR high {or_hi:,.2f}; waiting for pullback", "info")
            elif short_break:
                self._breakout_side = -1
                self._note(f"ES broke OR low {or_lo:,.2f}; waiting for pullback", "info")
            return

        side = self._breakout_side
        tol = PULLBACK_TOL_ATR * float(row["atr14"])
        if _peer_ok(peer_row, side) and _touch_pullback(row, _levels(row, or_hi, or_lo, side), side, tol) and _rejection(row, side):
            stop_raw = float(row["low"] - 2 * TICK) if side > 0 else float(row["high"] + 2 * TICK)
            self._pending = {
                "side": side,
                "signal_ts": int(row["dt_utc"].timestamp()),
                "signal_low": float(row["low"]),
                "signal_high": float(row["high"]),
                "stop_raw": stop_raw,
                "atr": float(row["atr14"]),
                "note": f"OR pullback rejection; NQ confirms; VWAP {float(row['vwap']):,.2f}",
            }
            self._note(("LONG" if side > 0 else "SHORT") + " signal; pending next 5m open", "open")

    def _fill_pending(self, entry_bar: pd.Series):
        p = self._pending or {}
        side = int(p.get("side", 0))
        if side == 0:
            self._pending = None
            return
        entry = _entry_price(float(entry_bar["open"]), side)
        stop = float(p["stop_raw"])
        stop_dist = abs(entry - stop)
        atr_val = float(p["atr"])
        if not np.isfinite(atr_val) or atr_val <= 0:
            self._pending = None
            return
        min_dist = MIN_STOP_ATR * atr_val
        max_dist = MAX_STOP_ATR * atr_val
        if stop_dist < min_dist:
            stop_dist = min_dist
            stop = entry - side * stop_dist
        stop_dist = abs(entry - stop)
        if stop_dist > max_dist:
            self._note(f"skipped: stop {stop_dist:.2f} pts > {MAX_STOP_ATR} ATR", "skip")
            self._pending = None
            return
        qty = int(min(MAX_CONTRACTS, math.floor(RISK_USD / (stop_dist * POINT_VALUE))))
        if qty < 1:
            self._note("skipped: stop too wide for 1 ES contract risk", "skip")
            self._pending = None
            return
        target = entry + side * RR_FINAL * stop_dist
        tp1 = entry + side * RR1 * stop_dist
        self.pos = ApexPos(
            id=uuid.uuid4().hex[:6],
            side="long" if side > 0 else "short",
            qty=qty,
            entry=entry,
            stop=stop,
            stop0=stop,
            target=target,
            r_points=stop_dist,
            opened_at=time.time(),
            entry_bar_ts=int(entry_bar["dt_utc"].timestamp()),
            best=entry,
            tp1=tp1,
            note=str(p.get("note", "")),
        )
        self._pending = None
        self.trades_today += 1
        self._note(
            f"OPEN {self.pos.side.upper()} ES @ {entry:,.2f} - stop {stop:,.2f} - target {target:,.2f} - 1 contract",
            "open",
        )

    def _manage_bar(self, bar: pd.Series):
        p = self.pos
        if p is None:
            return
        side = 1 if p.side == "long" else -1
        hi = float(bar["high"])
        lo = float(bar["low"])
        close = float(bar["close"])
        minute = _minute(bar["dt_ny"])

        if side > 0:
            p.best = max(p.best, hi)
            stopped = lo <= p.stop
            tp1_hit = hi >= p.tp1
            final_hit = hi >= p.target
        else:
            p.best = min(p.best, lo)
            stopped = hi >= p.stop
            tp1_hit = lo <= p.tp1
            final_hit = lo <= p.target

        if stopped:
            self._close(p.stop, "stop" if not p.tp1_done else "trail")
            return
        if not p.tp1_done and tp1_hit:
            p.tp1_done = True
            if side > 0:
                p.stop = max(p.entry, p.best - TRAIL_R * p.r_points)
            else:
                p.stop = min(p.entry, p.best + TRAIL_R * p.r_points)
            self._note(f"+1R reached; trailing stop now {p.stop:,.2f}", "info")
        if p.tp1_done:
            if side > 0:
                p.stop = max(p.stop, p.best - TRAIL_R * p.r_points)
            else:
                p.stop = min(p.stop, p.best + TRAIL_R * p.r_points)
        if final_hit:
            self._close(p.target, "target")
            return
        if minute >= _hhmm(FLAT_TIME):
            self._close(close, "eod")

    def _close(self, raw_exit: float, reason: str):
        p = self.pos
        if p is None:
            return
        side = 1 if p.side == "long" else -1
        exit_px = _exit_price(float(raw_exit), side)
        pnl = _pnl(side, p.entry, exit_px, p.qty) - p.qty * COMMISSION_RT
        self.balance += pnl
        self.day_pnl += pnl
        rec = {
            "side": p.side,
            "entry": round(p.entry, 2),
            "exit": round(exit_px, 2),
            "qty": p.qty,
            "pnl": round(pnl, 2),
            "rr": round(pnl / max(p.r_points * POINT_VALUE * p.qty, 1e-9), 2),
            "reason": reason,
            "opened_at": p.opened_at,
            "closed_at": time.time(),
        }
        self.history.insert(0, rec)
        self._note(f"CLOSE {p.side.upper()} ES @ {exit_px:,.2f} - {reason} - P&L ${pnl:+.2f}", "win" if pnl >= 0 else "loss")
        self.pos = None
        self._breakout_side = 0

    def _set_status(self):
        if not self.enabled:
            self.status = "paused"
        elif self.pos:
            self.status = "in ES trade"
        elif self._pending:
            self.status = "pending next 5m open"
        elif self.day_pnl <= -MAX_DAILY_LOSS:
            self.status = "daily loss stop active"
        elif self.balance >= TARGET_BALANCE:
            self.status = "Apex target reached"
        else:
            self.status = "live - waiting for NY open setup"

    def equity(self) -> float:
        eq = self.balance
        if self.pos and self.price:
            side = 1 if self.pos.side == "long" else -1
            eq += _pnl(side, self.pos.entry, self.price, self.pos.qty)
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - ES 5m VWAP/OR pullback strategy" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.eod_peak = START_BALANCE
        self.eod_threshold = START_BALANCE - MAX_EOD_DD
        self.day_key = ""
        self.day_pnl = 0.0
        self.trades_today = 0
        self.pos = None
        self.history = []
        self.log = []
        self.snap = {}
        self._pending = None
        self._breakout_side = 0
        self._last_bar_ts = 0
        self._note("bot reset (Apex paper account back to $50,000)")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self.equity()
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        positions = []
        if self.pos:
            p = self.pos
            side = 1 if p.side == "long" else -1
            up = _pnl(side, p.entry, self.price or p.entry, p.qty)
            positions.append(
                {
                    "id": p.id,
                    "side": p.side,
                    "entry": round(p.entry, 2),
                    "qty": p.qty,
                    "stop": round(p.stop, 2),
                    "tp1": round(p.tp1, 2),
                    "tp2": round(p.target, 2),
                    "pnl": round(up, 2),
                    "pnl_R": round(up / max(p.r_points * POINT_VALUE * p.qty, 1e-9), 2),
                    "news": p.note + (" - +1R trail active" if p.tp1_done else ""),
                }
            )
        return {
            "running": True,
            "enabled": self.enabled,
            "name": self.NAME,
            "status": self.status,
            "symbol": "ES=F",
            "peer": "NQ=F",
            "timeframe": INTERVAL,
            "contracts": 1,
            "price": round(self.price, 2) if self.price else None,
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "start_balance": START_BALANCE,
            "total_pnl": round(eq - START_BALANCE, 2),
            "total_pnl_pct": round((eq / START_BALANCE - 1) * 100, 2),
            "apex_target": TARGET_BALANCE,
            "target_left": round(max(0.0, TARGET_BALANCE - eq), 2),
            "eod_threshold": round(self.eod_threshold, 2),
            "drawdown_room": round(eq - self.eod_threshold, 2),
            "day_pnl": round(self.day_pnl, 2),
            "trades_today": self.trades_today,
            "max_trades_day": MAX_TRADES_DAY,
            "trades": len(self.history),
            "wins": wins,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
            "snap": self.snap,
            "data_error": self.data_error,
        }
