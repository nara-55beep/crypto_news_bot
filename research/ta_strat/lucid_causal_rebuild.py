"""
lucid_causal_rebuild.py

Research-only causal rebuild of the invalidated Lucid five-strategy basket.

Design constraints:
  * every signal is evaluated on a COMPLETED one-minute or multi-minute block;
  * every entry is the NEXT one-minute open, with one tick of adverse slippage;
  * stops/targets are managed from that next minute, stop-first if both are hit;
  * exits receive one additional tick of adverse slippage;
  * position size is an integer number of micros, capped at 40;
  * commission is charged per micro;
  * all positions are flat before 16:00 New York time (45 minutes before Lucid's cutoff);
  * pass statistics include zero-trade sessions and current LucidPro 50K rules.

The search is deliberately split:
  train       2016-07-01 .. 2021-12-31
  validation  2022-01-01 .. 2023-12-31
  test        2024-01-01 .. end of cached data

The script tests causal repairs of the same strategy family: VWAP rejection/cross-back,
VWAP momentum, opening-range breakout/fade, confirmed Turtle Soup, gap-safe NR7,
80/20 reversal, prior-range breakout, and trend-pullback rejection. It does not modify
either live paper bot.
"""
from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from datetime import date, time as dtime
from typing import Iterable

import numpy as np
import pandas as pd


HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"

MARKETS = {
    "es": {"pv": 5.0, "tick": 0.25},
    "nq": {"pv": 2.0, "tick": 0.25},
    "cl": {"pv": 100.0, "tick": 0.01},
}

SESSION_START = dtime(9, 30)
SESSION_END = dtime(16, 0)  # exclusive; 15:59 exit is 46 minutes before the cutoff

TRAIN_END = date(2021, 12, 31)
VALID_END = date(2023, 12, 31)

START_BALANCE = 50_000.0
TARGET_PROFIT = 3_000.0
MAX_LOSS = 2_000.0
LOCK_TRIGGER = 2_100.0
LOCKED_FLOOR = 100.0
DAILY_LOSS_LIMIT = 1_200.0
MAX_MICROS = 40
# Lucid publishes $0.50 PER SIDE for MES/MNQ/MCL, hence $1.00 round turn.
COMMISSION_RT = 1.00


@dataclass(frozen=True)
class Day:
    market: str
    day: date
    ts: np.ndarray
    minute: np.ndarray
    op: np.ndarray
    hi: np.ndarray
    lo: np.ndarray
    cl: np.ndarray
    vol: np.ndarray
    vwap: np.ndarray
    sigma: np.ndarray
    atr: np.ndarray
    ema9: np.ndarray
    ema20: np.ndarray


@dataclass(frozen=True)
class Config:
    family: str
    tf: int = 5
    k: float = 2.0
    rr: float = 1.5
    target_mode: str = "rr"
    stop_mode: str = "bar"
    or_min: int = 30
    ext: float = 0.33
    lookback: int = 10
    recency: int = 4
    buf_ticks: int = 8
    tol_atr: float = 0.20

    @property
    def label(self) -> str:
        bits = [self.family, f"tf{self.tf}"]
        if self.family.startswith("vwap"):
            bits += [f"k{self.k:g}", self.target_mode, f"r{self.rr:g}"]
        elif self.family.startswith("or_"):
            bits += [f"or{self.or_min}", self.stop_mode, f"r{self.rr:g}"]
            if self.family == "or_fade":
                bits.append(f"x{self.ext:g}")
        elif self.family == "turtle":
            bits += [f"lb{self.lookback}", f"b{self.buf_ticks}", f"r{self.rr:g}"]
        elif self.family == "nr7":
            bits += [self.stop_mode, f"r{self.rr:g}"]
        elif self.family == "trend_pullback":
            bits += [f"tol{self.tol_atr:g}", f"r{self.rr:g}"]
        elif self.family == "eighty_twenty":
            bits += [f"b{self.buf_ticks}", f"r{self.rr:g}"]
        elif self.family == "prior_breakout":
            bits += [f"k{self.k:g}", self.stop_mode, f"r{self.rr:g}"]
        return "_".join(bits)


@dataclass(frozen=True)
class Trade:
    market: str
    strategy: str
    day: date
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    side: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    risk_per_micro: float
    gross_per_micro: float


@dataclass(frozen=True)
class SizedTrade:
    trade: Trade
    qty: int
    pnl: float


def _ema(x: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(x).ewm(span=span, adjust=False).mean().to_numpy(float)


def _day_from_group(market: str, day: date, g: pd.DataFrame) -> Day:
    op = g["open"].to_numpy(float)
    hi = g["high"].to_numpy(float)
    lo = g["low"].to_numpy(float)
    cl = g["close"].to_numpy(float)
    vol = g["volume"].to_numpy(float)
    tp = (hi + lo + cl) / 3.0
    weights = np.where(vol > 0, vol, 1.0)
    cv = np.cumsum(weights)
    cp = np.cumsum(tp * weights)
    cp2 = np.cumsum(tp * tp * weights)
    vwap = cp / cv
    sigma = np.sqrt(np.maximum(cp2 / cv - vwap * vwap, 0.0))
    prev = np.r_[op[0], cl[:-1]]
    tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev), np.abs(lo - prev)))
    atr = pd.Series(tr).ewm(alpha=1 / 14.0, adjust=False).mean().to_numpy(float)
    local = g["dt_utc"].dt.tz_convert(NY)
    minute = (local.dt.hour * 60 + local.dt.minute - (9 * 60 + 30)).to_numpy(np.int16)
    return Day(
        market=market,
        day=day,
        ts=g["dt_utc"].to_numpy(),
        minute=minute,
        op=op,
        hi=hi,
        lo=lo,
        cl=cl,
        vol=vol,
        vwap=vwap,
        sigma=sigma,
        atr=atr,
        ema9=_ema(cl, 9),
        ema20=_ema(cl, 20),
    )


def _load_rth_minutes(market: str) -> pd.DataFrame:
    # The repaired 3y seed continues beyond the older 10y aggregate. Merge both and
    # let the newer seed win on overlap, so the July repair is actually exercised.
    paths = [
        os.path.join(CACHE, f"{market}_1m_10y.csv"),
        os.path.join(CACHE, f"{market}_1m_3y.csv"),
        os.path.join(CACHE, f"{market}_1m_rth_repair.csv"),
    ]
    frames = [
        pd.read_csv(p, usecols=["dt_utc", "open", "high", "low", "close", "volume"])
        for p in paths if os.path.exists(p)
    ]
    if not frames:
        raise FileNotFoundError(f"no one-minute history for {market}")
    df = pd.concat(frames, ignore_index=True)
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    df = df.dropna(subset=["dt_utc", "open", "high", "low", "close"])
    df = df.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc")
    ny = df["dt_utc"].dt.tz_convert(NY)
    tm = ny.dt.time
    mask = (tm >= SESSION_START) & (tm < SESSION_END)
    df = df.loc[mask].copy()
    df["day"] = ny.loc[mask].dt.date
    return df


def _is_complete_rth_minutes(minute: np.ndarray) -> bool:
    return (
        len(minute) == 390
        and np.array_equal(minute, np.arange(390, dtype=np.int16))
    )


def load_days_and_sessions(market: str) -> tuple[list[Day], list[date]]:
    """Return tradable complete days and the broader observed session calendar.

    Bad-data and shortened sessions count as zero-trade evaluation days, but
    they cannot form model features or trades.  This prevents cleaning a data
    hole from making a 20/30-session pass window artificially shorter.
    """
    df = _load_rth_minutes(market)
    out = []
    sessions = []
    for d, g in df.groupby("day", sort=True):
        g = g.reset_index(drop=True)
        local = g["dt_utc"].dt.tz_convert(NY)
        if len(g) >= 60:
            sessions.append(d)
        # Row-indexed strategy logic is valid only when every row is exactly the
        # corresponding RTH clock minute.  A loose row-count threshold allowed
        # internal holes to stretch five-minute bars and fixed-horizon exits.
        # Require the complete 09:30..15:59 sequence; this also deliberately
        # excludes early-close holidays.
        minute = (
            local.dt.hour * 60 + local.dt.minute - (9 * 60 + 30)
        ).to_numpy(np.int16)
        if not _is_complete_rth_minutes(minute):
            continue
        out.append(_day_from_group(market, d, g))
    return out, sessions


def load_days(market: str) -> list[Day]:
    return load_days_and_sessions(market)[0]


def load_session_dates(market: str) -> list[date]:
    return load_days_and_sessions(market)[1]


def _sample_indices(day: Day, tf: int, start: int = 0) -> np.ndarray:
    """Indices of clock-aligned completed bars, independent of missing leading minutes."""
    minute = day.minute.astype(np.int64)
    mask = (minute >= max(tf - 1, start)) & (minute % tf == tf - 1)
    idx = np.flatnonzero(mask)
    return idx[idx < len(day.cl) - 1].astype(np.int64)


def _block_extreme(x: np.ndarray, i: int, tf: int, fn) -> float:
    a = max(0, i - tf + 1)
    return float(fn(x[a:i + 1]))


def _make_trade(day: Day, signal_i: int, side: int, stop: float, *,
                rr: float, strategy: str, fixed_target: float | None = None) -> Trade | None:
    fi = signal_i + 1
    if fi >= len(day.cl):
        return None
    tick = MARKETS[day.market]["tick"]
    pv = MARKETS[day.market]["pv"]
    entry = float(day.op[fi]) + side * tick
    stop = float(stop)
    if (side > 0 and stop >= entry) or (side < 0 and stop <= entry):
        return None
    r_points = abs(entry - stop)
    if r_points < tick:
        return None
    target = float(fixed_target) if fixed_target is not None else entry + side * rr * r_points
    if (side > 0 and target <= entry) or (side < 0 and target >= entry):
        return None
    raw_exit = float(day.cl[-1])
    reason = "eod"
    exit_i = len(day.cl) - 1
    for j in range(fi, len(day.cl)):
        stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
        targeted = day.hi[j] >= target if side > 0 else day.lo[j] <= target
        # Stop priority is conservative when both occur in the same one-minute candle.
        if stopped:
            # A stop cannot fill at a price skipped by a gap.
            raw_exit = min(stop, float(day.op[j])) if side > 0 else max(stop, float(day.op[j]))
            reason = "stop"
            exit_i = j
            break
        if targeted:
            raw_exit = target
            reason = "target"
            exit_i = j
            break
    exit_px = raw_exit - side * tick
    gross_per_micro = side * (exit_px - entry) * pv
    return Trade(
        market=day.market,
        strategy=strategy,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[fi]),
        exit_ts=pd.Timestamp(day.ts[exit_i]),
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        exit=exit_px,
        reason=reason,
        # Include the modeled adverse exit tick in sizing. Commission is added by
        # size_trades because callers can stress it independently.
        risk_per_micro=(r_points + tick) * pv,
        gross_per_micro=gross_per_micro,
    )


def _vwap_trade(day: Day, cfg: Config) -> Trade | None:
    prev_i = None
    for i in _sample_indices(day, cfg.tf, start=max(20, cfg.tf - 1)):
        if day.sigma[i] <= 0:
            prev_i = i
            continue
        up = day.vwap[i] + cfg.k * day.sigma[i]
        dn = day.vwap[i] - cfg.k * day.sigma[i]
        bh = _block_extreme(day.hi, i, cfg.tf, np.max)
        bl = _block_extreme(day.lo, i, cfg.tf, np.min)
        bo = float(day.op[max(0, i - cfg.tf + 1)])
        side = 0
        stop = 0.0
        if cfg.family == "vwap_reject":
            if bh >= up and day.cl[i] < up and day.cl[i] < bo:
                side = -1
                stop = bh + MARKETS[day.market]["tick"]
            elif bl <= dn and day.cl[i] > dn and day.cl[i] > bo:
                side = 1
                stop = bl - MARKETS[day.market]["tick"]
        elif cfg.family == "vwap_crossback" and prev_i is not None:
            pup = day.vwap[prev_i] + cfg.k * day.sigma[prev_i]
            pdn = day.vwap[prev_i] - cfg.k * day.sigma[prev_i]
            if day.cl[prev_i] > pup and day.cl[i] < up:
                side = -1
                stop = max(float(day.hi[prev_i]), bh) + MARKETS[day.market]["tick"]
            elif day.cl[prev_i] < pdn and day.cl[i] > dn:
                side = 1
                stop = min(float(day.lo[prev_i]), bl) - MARKETS[day.market]["tick"]
        elif cfg.family == "vwap_momentum" and prev_i is not None:
            pup = day.vwap[prev_i] + cfg.k * day.sigma[prev_i]
            pdn = day.vwap[prev_i] - cfg.k * day.sigma[prev_i]
            if day.cl[prev_i] <= pup and day.cl[i] > up:
                side = 1
                stop = min(bl, float(day.vwap[i]))
            elif day.cl[prev_i] >= pdn and day.cl[i] < dn:
                side = -1
                stop = max(bh, float(day.vwap[i]))
        if side:
            fixed = float(day.vwap[i]) if cfg.target_mode == "vwap" else None
            trade = _make_trade(day, int(i), side, stop, rr=cfg.rr,
                                strategy=cfg.label, fixed_target=fixed)
            if trade is not None:
                return trade
        prev_i = i
    return None


def _or_trade(day: Day, cfg: Config) -> Trade | None:
    if len(day.cl) <= cfg.or_min + 1:
        return None
    opening = day.minute < cfg.or_min
    if not np.any(opening):
        return None
    or_hi = float(np.max(day.hi[opening]))
    or_lo = float(np.min(day.lo[opening]))
    or_mid = (or_hi + or_lo) / 2.0
    or_range = or_hi - or_lo
    if or_range <= 0:
        return None
    prev_i = None
    for i in _sample_indices(day, cfg.tf, start=cfg.or_min + cfg.tf - 1):
        bh = _block_extreme(day.hi, i, cfg.tf, np.max)
        bl = _block_extreme(day.lo, i, cfg.tf, np.min)
        bo = float(day.op[max(0, i - cfg.tf + 1)])
        side = 0
        stop = 0.0
        fixed = None
        if cfg.family == "or_breakout":
            opening_last = int(np.flatnonzero(opening)[-1])
            prev_close = day.cl[prev_i] if prev_i is not None else day.cl[opening_last]
            if prev_close <= or_hi and day.cl[i] > or_hi:
                side = 1
            elif prev_close >= or_lo and day.cl[i] < or_lo:
                side = -1
            if side:
                if cfg.stop_mode == "opposite":
                    stop = or_lo if side > 0 else or_hi
                elif cfg.stop_mode == "mid":
                    stop = or_mid
                else:
                    stop = bl - MARKETS[day.market]["tick"] if side > 0 else bh + MARKETS[day.market]["tick"]
        else:  # opening-range extension rejection
            if bh > or_hi + cfg.ext * or_range and day.cl[i] < or_hi and day.cl[i] < bo:
                side = -1
                stop = bh + MARKETS[day.market]["tick"]
                fixed = or_mid if cfg.target_mode == "mid" else None
            elif bl < or_lo - cfg.ext * or_range and day.cl[i] > or_lo and day.cl[i] > bo:
                side = 1
                stop = bl - MARKETS[day.market]["tick"]
                fixed = or_mid if cfg.target_mode == "mid" else None
        if side:
            trade = _make_trade(day, int(i), side, stop, rr=cfg.rr,
                                strategy=cfg.label, fixed_target=fixed)
            if trade is not None:
                return trade
        prev_i = i
    return None


def _turtle_trade(day: Day, cfg: Config, prior: list[Day]) -> Trade | None:
    if len(prior) < cfg.lookback:
        return None
    look = prior[-cfg.lookback:]
    lows = np.array([float(np.min(x.lo)) for x in look])
    highs = np.array([float(np.max(x.hi)) for x in look])
    prior_low = float(lows.min())
    prior_high = float(highs.max())
    low_age = len(look) - 1 - int(np.argmin(lows))
    high_age = len(look) - 1 - int(np.argmax(highs))
    tick = MARKETS[day.market]["tick"]
    long_recovery = prior_low + cfg.buf_ticks * tick
    short_recovery = prior_high - cfg.buf_ticks * tick
    for i in _sample_indices(day, cfg.tf, start=cfg.tf - 1):
        bh = _block_extreme(day.hi, i, cfg.tf, np.max)
        bl = _block_extreme(day.lo, i, cfg.tf, np.min)
        if low_age >= cfg.recency and bl < prior_low and day.cl[i] > long_recovery:
            return _make_trade(day, int(i), 1, bl - tick, rr=cfg.rr, strategy=cfg.label)
        if high_age >= cfg.recency and bh > prior_high and day.cl[i] < short_recovery:
            return _make_trade(day, int(i), -1, bh + tick, rr=cfg.rr, strategy=cfg.label)
    return None


def _nr7_trade(day: Day, cfg: Config, prior: list[Day]) -> Trade | None:
    if len(prior) < 7:
        return None
    daily_ranges = [float(np.max(x.hi) - np.min(x.lo)) for x in prior[-7:]]
    if daily_ranges[-1] >= min(daily_ranges[:-1]):
        return None
    last = prior[-1]
    tick = MARKETS[day.market]["tick"]
    high_level = float(np.max(last.hi)) + tick
    low_level = float(np.min(last.lo)) - tick
    # Do not pretend that a threshold was fillable if the modeled session opened beyond it.
    if day.op[0] > high_level or day.op[0] < low_level:
        return None
    prev_i = None
    for i in _sample_indices(day, cfg.tf, start=cfg.tf - 1):
        prev_close = day.cl[prev_i] if prev_i is not None else day.op[0]
        side = 0
        if prev_close <= high_level and day.cl[i] > high_level:
            side = 1
        elif prev_close >= low_level and day.cl[i] < low_level:
            side = -1
        if side:
            if cfg.stop_mode == "opposite":
                stop = low_level if side > 0 else high_level
            elif cfg.stop_mode == "atr":
                stop = day.cl[i] - side * max(2.0 * day.atr[i], tick)
            else:
                stop = (_block_extreme(day.lo, i, cfg.tf, np.min) - tick
                        if side > 0 else
                        _block_extreme(day.hi, i, cfg.tf, np.max) + tick)
            trade = _make_trade(day, int(i), side, float(stop), rr=cfg.rr, strategy=cfg.label)
            if trade is not None:
                return trade
        prev_i = i
    return None


def _trend_pullback_trade(day: Day, cfg: Config) -> Trade | None:
    tick = MARKETS[day.market]["tick"]
    for i in _sample_indices(day, cfg.tf, start=max(30, cfg.tf - 1)):
        bh = _block_extreme(day.hi, i, cfg.tf, np.max)
        bl = _block_extreme(day.lo, i, cfg.tf, np.min)
        bo = float(day.op[max(0, i - cfg.tf + 1)])
        tol = cfg.tol_atr * day.atr[i]
        if (day.cl[i] > day.vwap[i] and day.ema9[i] > day.ema20[i]
                and bl <= day.ema9[i] + tol and day.cl[i] > day.ema9[i]
                and day.cl[i] > bo):
            trade = _make_trade(day, int(i), 1, bl - tick, rr=cfg.rr, strategy=cfg.label)
            if trade is not None:
                return trade
        if (day.cl[i] < day.vwap[i] and day.ema9[i] < day.ema20[i]
                and bh >= day.ema9[i] - tol and day.cl[i] < day.ema9[i]
                and day.cl[i] < bo):
            trade = _make_trade(day, int(i), -1, bh + tick, rr=cfg.rr, strategy=cfg.label)
            if trade is not None:
                return trade
    return None


def _eighty_twenty_trade(day: Day, cfg: Config, prior: list[Day]) -> Trade | None:
    """Causal 80/20 reversal.

    The original implementation allowed the sweep and recovery to occur in one candle.
    Here the sweep must complete first, recovery must close on a strictly later sampled
    bar, and entry is still delayed to the following one-minute open.
    """
    if not prior:
        return None
    last = prior[-1]
    ph = float(np.max(last.hi))
    pl = float(np.min(last.lo))
    po = float(last.op[0])
    pc = float(last.cl[-1])
    pr = ph - pl
    if pr <= 0:
        return None
    long_setup = po >= ph - 0.20 * pr and pc <= pl + 0.20 * pr
    short_setup = po <= pl + 0.20 * pr and pc >= ph - 0.20 * pr
    if not long_setup and not short_setup:
        return None
    tick = MARKETS[day.market]["tick"]
    side = 1 if long_setup else -1
    level = pl if side > 0 else ph
    trigger = level - side * cfg.buf_ticks * tick
    pushed_i: int | None = None
    for i in _sample_indices(day, cfg.tf, start=cfg.tf - 1):
        bh = _block_extreme(day.hi, i, cfg.tf, np.max)
        bl = _block_extreme(day.lo, i, cfg.tf, np.min)
        if pushed_i is None:
            if (side > 0 and bl <= trigger) or (side < 0 and bh >= trigger):
                pushed_i = int(i)
            continue
        if i <= pushed_i:
            continue
        recovered = day.cl[i] > level if side > 0 else day.cl[i] < level
        if recovered:
            stop = (float(np.min(day.lo[:i + 1])) - tick if side > 0
                    else float(np.max(day.hi[:i + 1])) + tick)
            return _make_trade(day, int(i), side, stop, rr=cfg.rr, strategy=cfg.label)
    return None


def _prior_breakout_trade(day: Day, cfg: Config, prior: list[Day]) -> Trade | None:
    """Confirmed prior-range momentum, entered only after a completed close beyond level."""
    if not prior:
        return None
    last = prior[-1]
    ph = float(np.max(last.hi))
    pl = float(np.min(last.lo))
    prior_range = ph - pl
    if prior_range <= 0:
        return None
    tick = MARKETS[day.market]["tick"]
    long_level = float(day.op[0]) + cfg.k * prior_range
    short_level = float(day.op[0]) - cfg.k * prior_range
    prev_i = None
    for i in _sample_indices(day, cfg.tf, start=cfg.tf - 1):
        prior_close = float(day.op[0]) if prev_i is None else float(day.cl[prev_i])
        side = 0
        if prior_close <= long_level and day.cl[i] > long_level:
            side = 1
        elif prior_close >= short_level and day.cl[i] < short_level:
            side = -1
        if side:
            if cfg.stop_mode == "open":
                stop = float(day.op[0])
            elif cfg.stop_mode == "opposite":
                stop = short_level if side > 0 else long_level
            else:
                stop = (_block_extreme(day.lo, i, cfg.tf, np.min) - tick if side > 0
                        else _block_extreme(day.hi, i, cfg.tf, np.max) + tick)
            trade = _make_trade(day, int(i), side, float(stop), rr=cfg.rr, strategy=cfg.label)
            if trade is not None:
                return trade
        prev_i = int(i)
    return None


def generate(days: list[Day], cfg: Config) -> list[Trade]:
    out: list[Trade] = []
    prior: list[Day] = []
    for day in days:
        if cfg.family.startswith("vwap"):
            trade = _vwap_trade(day, cfg)
        elif cfg.family.startswith("or_"):
            trade = _or_trade(day, cfg)
        elif cfg.family == "turtle":
            trade = _turtle_trade(day, cfg, prior)
        elif cfg.family == "nr7":
            trade = _nr7_trade(day, cfg, prior)
        elif cfg.family == "trend_pullback":
            trade = _trend_pullback_trade(day, cfg)
        elif cfg.family == "eighty_twenty":
            trade = _eighty_twenty_trade(day, cfg, prior)
        elif cfg.family == "prior_breakout":
            trade = _prior_breakout_trade(day, cfg, prior)
        else:
            raise ValueError(cfg.family)
        if trade is not None:
            out.append(trade)
        prior.append(day)
    return out


def size_trades(trades: Iterable[Trade], risk_usd: float, commission_rt: float = COMMISSION_RT,
                max_micros: int = MAX_MICROS) -> list[SizedTrade]:
    out = []
    for t in trades:
        all_in_stop_risk = t.risk_per_micro + commission_rt
        qty = min(max_micros, int(math.floor(risk_usd / max(all_in_stop_risk, 1e-12))))
        if qty < 1:
            continue
        pnl = t.gross_per_micro * qty - commission_rt * qty
        out.append(SizedTrade(t, qty, pnl))
    return out


def basic_stats(trades: list[SizedTrade]) -> dict:
    if not trades:
        return {"n": 0, "net": 0.0, "pf": 0.0, "win": 0.0, "maxdd": 0.0, "avg": 0.0}
    pnl = np.array([t.pnl for t in trades], dtype=float)
    gp = float(pnl[pnl > 0].sum())
    gl = float(-pnl[pnl <= 0].sum())
    curve = np.cumsum(pnl)
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    dd = curve - peaks
    return {
        "n": len(pnl),
        "net": float(pnl.sum()),
        "pf": gp / gl if gl > 0 else 99.0,
        "win": float(np.mean(pnl > 0)),
        "maxdd": float(dd.min()) if len(dd) else 0.0,
        "avg": float(pnl.mean()),
    }


def _slice(trades: list[Trade], lo: date | None, hi: date | None) -> list[Trade]:
    return [t for t in trades if (lo is None or t.day >= lo) and (hi is None or t.day <= hi)]


def configs() -> list[Config]:
    out: list[Config] = []
    for tf in (3, 5, 15):
        for k in (1.5, 2.0, 2.5):
            out.append(Config("vwap_reject", tf=tf, k=k, target_mode="vwap"))
            out.append(Config("vwap_reject", tf=tf, k=k, target_mode="rr", rr=1.5))
    for tf in (3, 5):
        for k in (1.5, 2.0, 2.5):
            out.append(Config("vwap_crossback", tf=tf, k=k, target_mode="vwap"))
            out.append(Config("vwap_crossback", tf=tf, k=k, target_mode="rr", rr=1.5))
    for tf in (5, 15):
        for k in (1.5, 2.0):
            out.append(Config("vwap_momentum", tf=tf, k=k, rr=2.0))
    for or_min in (30, 60):
        for stop_mode in ("mid", "opposite", "bar"):
            for rr in (1.5, 2.0):
                out.append(Config("or_breakout", tf=5, or_min=or_min,
                                  stop_mode=stop_mode, rr=rr))
        for ext in (0.25, 0.50):
            out.append(Config("or_fade", tf=5, or_min=or_min, ext=ext,
                              target_mode="mid", rr=1.5))
    for tf in (1, 5):
        for lookback in (10, 20):
            out.append(Config("turtle", tf=tf, lookback=lookback,
                              buf_ticks=8, rr=2.0))
    for tf in (1, 5):
        for stop_mode in ("bar", "atr", "opposite"):
            out.append(Config("nr7", tf=tf, stop_mode=stop_mode, rr=2.0))
    for tf in (3, 5, 15):
        for tol in (0.10, 0.25):
            for rr in (1.0, 1.5):
                out.append(Config("trend_pullback", tf=tf, tol_atr=tol, rr=rr))
    for tf in (1, 5):
        for buf_ticks in (4, 8, 12):
            for rr in (1.5, 2.0):
                out.append(Config("eighty_twenty", tf=tf, buf_ticks=buf_ticks, rr=rr))
    for tf in (3, 5, 15):
        for k in (0.25, 0.50, 0.75):
            for stop_mode in ("open", "bar"):
                out.append(Config("prior_breakout", tf=tf, k=k, stop_mode=stop_mode, rr=2.0))
    # Stable de-duplication.
    return list(dict.fromkeys(out))


def _period_stats(trades: list[Trade], risk: float, lo: date | None, hi: date | None) -> dict:
    return basic_stats(size_trades(_slice(trades, lo, hi), risk))


def search_market(market: str, days: list[Day], risk: float, top: int) -> tuple[list[dict], dict[str, list[Trade]]]:
    rows = []
    cache: dict[str, list[Trade]] = {}
    train_lo = days[0].day
    valid_lo = date(2022, 1, 1)
    test_lo = date(2024, 1, 1)
    for n, cfg in enumerate(configs(), 1):
        trades = generate(days, cfg)
        cache[cfg.label] = trades
        tr = _period_stats(trades, risk, train_lo, TRAIN_END)
        va = _period_stats(trades, risk, valid_lo, VALID_END)
        # Test is printed only after ranking by train+validation.
        stability = min(tr["pf"], va["pf"])
        score = stability * math.log1p(min(tr["n"], va["n"])) + min(tr["avg"], va["avg"]) / 100.0
        rows.append({"market": market, "cfg": cfg, "label": cfg.label,
                     "train": tr, "valid": va, "score": score})
        if n % 20 == 0:
            print(f"  [{market}] {n}/{len(configs())} configurations", flush=True)
    rows.sort(key=lambda r: r["score"], reverse=True)
    finalists = rows[:top]
    for row in finalists:
        row["test"] = _period_stats(cache[row["label"]], risk, test_lo, None)
    return finalists, cache


def non_overlapping(trades: Iterable[Trade]) -> list[Trade]:
    """Conservative portfolio: at most one open position across all markets."""
    out: list[Trade] = []
    busy_until: pd.Timestamp | None = None
    for t in sorted(trades, key=lambda x: (x.entry_ts, x.market, x.strategy)):
        if busy_until is not None and t.entry_ts <= busy_until:
            continue
        out.append(t)
        busy_until = t.exit_ts
    return out


def eval_lucid(trades: list[SizedTrade], all_days: list[date], horizon: int = 30) -> dict:
    by_day: dict[date, list[SizedTrade]] = {d: [] for d in all_days}
    for trade in trades:
        if trade.trade.day in by_day:
            by_day[trade.trade.day].append(trade)
    for rows in by_day.values():
        rows.sort(key=lambda x: x.trade.exit_ts)
    outcomes = []
    pass_days = []
    # Only start windows with the full requested number of sessions available. This
    # prevents end-of-dataset censoring from flattering or depressing the pass rate.
    n_starts = max(0, len(all_days) - horizon + 1)
    for i0 in range(n_starts):
        balance = 0.0
        eod_peak = 0.0
        floor = -MAX_LOSS
        outcome = "undecided"
        used = 0
        for k in range(i0, min(i0 + horizon, len(all_days))):
            day_pnl = 0.0
            for trade in by_day[all_days[k]]:
                if day_pnl <= -DAILY_LOSS_LIMIT:
                    break
                balance += trade.pnl
                day_pnl += trade.pnl
                # Current MLL is monitored against the level established at the prior EOD.
                if balance <= floor:
                    outcome = "fail"
                    break
                if balance >= TARGET_PROFIT:
                    outcome = "pass"
                    break
            used = k - i0 + 1
            if outcome != "undecided":
                break
            eod_peak = max(eod_peak, balance)
            floor = LOCKED_FLOOR if eod_peak > LOCK_TRIGGER else eod_peak - MAX_LOSS
        outcomes.append(outcome)
        if outcome == "pass":
            pass_days.append(used)
    n = len(outcomes)
    passes = outcomes.count("pass")
    fails = outcomes.count("fail")
    undecided = outcomes.count("undecided")
    return {
        "starts": n,
        "passes": passes,
        "fails": fails,
        "undecided": undecided,
        "pass_all": passes / n if n else 0.0,
        "pass_decided": passes / (passes + fails) if passes + fails else 0.0,
        "median_days": float(np.median(pass_days)) if pass_days else None,
        "mean_days": float(np.mean(pass_days)) if pass_days else None,
        "p90_days": float(np.percentile(pass_days, 90)) if pass_days else None,
    }


def print_speed_diagnostic(row: dict, trades: list[Trade], days: list[Day]) -> None:
    """Show the risk/speed/failure tradeoff on untouched test sessions only."""
    test_days = [d.day for d in days if d.day >= date(2024, 1, 1)]
    test_trades = _slice(trades, date(2024, 1, 1), None)
    print(f"  untouched-test Lucid windows for {row['label']}:")
    print("    risk   12-session pass   30-session pass   30-session fail   median pass")
    for risk in (150.0, 200.0, 300.0, 400.0, 500.0):
        sized = size_trades(test_trades, risk)
        e12 = eval_lucid(sized, test_days, horizon=12)
        e30 = eval_lucid(sized, test_days, horizon=30)
        med = "-" if e30["median_days"] is None else f"{e30['median_days']:.0f}d"
        print(
            f"    ${risk:<4.0f} {e12['pass_all']*100:>9.1f}%"
            f"{e30['pass_all']*100:>18.1f}%"
            f"{e30['fails']/e30['starts']*100 if e30['starts'] else 0:>18.1f}%"
            f"{med:>14}"
        )


def print_row(row: dict) -> None:
    def f(s):
        return f"n={s['n']:4d} PF={s['pf']:.2f} net={s['net']:+9.0f} avg={s['avg']:+6.1f} DD={s['maxdd']:8.0f}"
    print(f"{row['market']} {row['label']:<48} train {f(row['train'])}")
    print(f"{'':52} valid {f(row['valid'])}")
    print(f"{'':52} TEST  {f(row['test'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--markets", nargs="+", default=["es", "nq", "cl"])
    args = ap.parse_args()

    days_by_market = {}
    all_days: set[date] = set()
    finalists = []
    caches = {}
    for market in args.markets:
        days = load_days(market)
        days_by_market[market] = days
        all_days.update(x.day for x in days)
        print(f"[{market}] {len(days)} sessions {days[0].day} -> {days[-1].day}", flush=True)
        rows, cache = search_market(market, days, args.risk, args.top)
        finalists += rows
        caches[market] = cache
        print(f"\n[{market}] finalists selected without test data:")
        for row in rows:
            print_row(row)
        if rows:
            print_speed_diagnostic(rows[0], cache[rows[0]["label"]], days)

    # Portfolio only includes candidates with positive PF in train, validation, and test.
    survivors = [
        r for r in finalists
        if r["train"]["pf"] > 1.10 and r["valid"]["pf"] > 1.10 and r["test"]["pf"] > 1.10
        and min(r["train"]["n"], r["valid"]["n"], r["test"]["n"]) >= 30
    ]
    print("\n=== SURVIVORS (PF > 1.10 in train, validation, and untouched test) ===")
    if not survivors:
        print("NONE")
        return 0
    for row in survivors:
        print_row(row)

    combined = []
    for row in survivors:
        combined.extend(caches[row["market"]][row["label"]])
    combined = non_overlapping(combined)
    sized = size_trades(combined, args.risk)
    stats = basic_stats(sized)
    ev = eval_lucid(sized, sorted(all_days), horizon=30)
    print("\n=== CONSERVATIVE NON-OVERLAPPING PORTFOLIO ===")
    print(f"trades={stats['n']} net=${stats['net']:+,.0f} PF={stats['pf']:.2f} "
          f"win={stats['win']*100:.1f}% maxDD=${stats['maxdd']:,.0f}")
    print(f"Lucid starts={ev['starts']} pass={ev['passes']} fail={ev['fails']} "
          f"undecided={ev['undecided']} pass(all)={ev['pass_all']*100:.1f}% "
          f"median={ev['median_days']} mean={ev['mean_days']} p90={ev['p90_days']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
