"""
ICT-style Apex backtest.

This is a research script, not a trading bot. It tests a mechanical ICT-style
setup on Yahoo 5m futures candles:

  - NY AM only.
  - Liquidity sweep of either previous-day high/low or the first 15m opening range.
  - Market-structure shift after the sweep.
  - Optional fair value gap (3-candle imbalance), entry at the 50% midpoint.
  - Stop beyond the sweep, target by R multiple or opposite liquidity.
  - Apex 50K-style rolling 30-calendar-day evaluation:
      pass at +$3,000, fail on $2,000 trailing EOD drawdown.

Yahoo 5m history is short (about 60 calendar days). That is enough to test recent
30-day windows, but not enough to prove long-term robustness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
import bisect
import itertools

import numpy as np
import pandas as pd
import requests


HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)
NY = ZoneInfo("America/New_York")

START = 50_000.0
TARGET = 53_000.0
MAX_DD = 2_000.0
DAILY_LOSS = 1_000.0


@dataclass(frozen=True)
class Spec:
    key: str
    yahoo: str
    point_value: float
    tick: float
    max_contracts: int


@dataclass(frozen=True)
class Params:
    market: str
    pool: str
    contracts: int
    lookback: int
    stop_buffer_atr: float
    rr: float
    require_fvg: bool
    min_disp_atr: float
    kill_start: str = "09:30"
    kill_end: str = "11:30"
    expiry_bars: int = 18


SPECS = {
    "ES": Spec("ES", "ES=F", 50.0, 0.25, 2),
    "NQ": Spec("NQ", "NQ=F", 20.0, 0.25, 1),
    "BTC": Spec("BTC", "BTC=F", 5.0, 5.0, 1),
}


def cache_path(symbol: str) -> Path:
    return CACHE / f"apex_ict_{symbol.replace('=', '_')}_5m_60d.csv"


def fetch_yahoo(symbol: str, refresh: bool = False) -> pd.DataFrame:
    path = cache_path(symbol)
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["dt", "ny"])
        df["day"] = pd.to_datetime(df["day"]).dt.date
        return df
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "60d", "interval": "5m", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    raw = r.json()["chart"]["result"][0]
    q = raw["indicators"]["quote"][0]
    df = pd.DataFrame({
        "dt": pd.to_datetime(raw["timestamp"], unit="s", utc=True),
        "open": q.get("open"),
        "high": q.get("high"),
        "low": q.get("low"),
        "close": q.get("close"),
        "volume": q.get("volume"),
    }).dropna(subset=["open", "high", "low", "close"])
    df["ny"] = df["dt"].dt.tz_convert(NY)
    df["day"] = df["ny"].dt.date
    mins = df["ny"].dt.hour * 60 + df["ny"].dt.minute
    df = df[(mins >= 9 * 60 + 30) & (mins <= 16 * 60)].copy().reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    pc = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - pc).abs(),
        (d["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    daily = d.groupby("day").agg(
        pdh=("high", "max"),
        pdl=("low", "min"),
        pdc=("close", "last"),
    ).shift(1)
    return d.merge(daily, left_on="day", right_index=True, how="left")


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def fvg_in_leg(g: pd.DataFrame, lo: int, hi: int, side: int, min_gap: float) -> tuple[float, float] | None:
    # Return (entry_midpoint, gap_edge_for_invalidation).
    lo = max(2, lo)
    hi = min(hi, len(g) - 1)
    for j in range(lo, hi + 1):
        a = g.iloc[j - 2]
        c = g.iloc[j]
        if side > 0:
            gap = c["low"] - a["high"]
            if gap >= min_gap:
                return (float((c["low"] + a["high"]) / 2.0), float(a["high"]))
        else:
            gap = a["low"] - c["high"]
            if gap >= min_gap:
                return (float((a["low"] + c["high"]) / 2.0), float(a["low"]))
    return None


def ict_trade_for_day(g: pd.DataFrame, p: Params, spec: Spec) -> tuple[float, int]:
    if len(g) < 40:
        return 0.0, 0
    g = g.reset_index(drop=True)
    start = hhmm(p.kill_start)
    end = hhmm(p.kill_end)
    mins = g["ny"].map(minute)
    tradable = np.where((mins >= start) & (mins <= end))[0]
    if len(tradable) == 0:
        return 0.0, 0

    or15 = g[(mins >= 9 * 60 + 30) & (mins < 9 * 60 + 45)]
    if len(or15) == 0:
        return 0.0, 0
    if p.pool == "prevday":
        hi_pool = float(g.iloc[0]["pdh"]) if np.isfinite(g.iloc[0]["pdh"]) else np.nan
        lo_pool = float(g.iloc[0]["pdl"]) if np.isfinite(g.iloc[0]["pdl"]) else np.nan
    else:
        hi_pool = float(or15["high"].max())
        lo_pool = float(or15["low"].min())
    if not np.isfinite(hi_pool) or not np.isfinite(lo_pool):
        return 0.0, 0

    for i in tradable:
        row = g.iloc[i]
        a = float(row["atr"])
        if not np.isfinite(a) or a <= 0:
            continue
        side = 0
        raid = None
        pool = None
        opp_pool = None
        if row["low"] < lo_pool:
            side, raid, pool, opp_pool = 1, float(row["low"]), lo_pool, hi_pool
        elif row["high"] > hi_pool:
            side, raid, pool, opp_pool = -1, float(row["high"]), hi_pool, lo_pool
        if side == 0:
            continue

        prior = g.iloc[max(0, i - p.lookback):i]
        if len(prior) < 2:
            continue
        mss_level = float(prior["high"].max()) if side > 0 else float(prior["low"].min())
        for j in range(i + 1, min(len(g) - 1, i + p.expiry_bars + 1)):
            r = g.iloc[j]
            body = abs(float(r["close"] - r["open"]))
            rng = max(float(r["high"] - r["low"]), 1e-9)
            disp_ok = rng >= p.min_disp_atr * a and body / rng >= 0.5
            mss = (r["close"] > mss_level) if side > 0 else (r["close"] < mss_level)
            if not (mss and disp_ok):
                continue
            fvg = fvg_in_leg(g, i + 1, j, side, 0.10 * a)
            if p.require_fvg and fvg is None:
                continue
            entry = fvg[0] if fvg is not None else float(r["close"])
            stop = raid - side * p.stop_buffer_atr * a
            risk_pts = abs(entry - stop)
            if risk_pts <= 0:
                continue
            dollar_risk = risk_pts * spec.point_value * p.contracts
            if dollar_risk > DAILY_LOSS * 0.95:
                continue
            target = entry + side * p.rr * risk_pts if p.rr > 0 else opp_pool
            if side > 0 and target <= entry:
                target = entry + 2.0 * risk_pts
            if side < 0 and target >= entry:
                target = entry - 2.0 * risk_pts

            filled = False
            for k in range(j + 1, len(g)):
                b = g.iloc[k]
                if not filled:
                    if side > 0 and b["low"] <= entry:
                        filled = True
                    elif side < 0 and b["high"] >= entry:
                        filled = True
                    else:
                        continue
                hi, lo, close = float(b["high"]), float(b["low"]), float(b["close"])
                if side > 0:
                    if lo <= stop:
                        exit_px = stop - spec.tick
                        pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                        return float(pnl), 1
                    if hi >= target:
                        exit_px = target - spec.tick
                        pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                        return float(pnl), 1
                else:
                    if hi >= stop:
                        exit_px = stop + spec.tick
                        pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                        return float(pnl), 1
                    if lo <= target:
                        exit_px = target + spec.tick
                        pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                        return float(pnl), 1
                # Exit at 15:55/close if still open.
                if minute(b["ny"]) >= 15 * 60 + 55:
                    exit_px = close - side * spec.tick
                    pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                    return float(pnl), 1
            return 0.0, 0
    return 0.0, 0


def eval_windows(days: list[object], pnls: np.ndarray, trades: np.ndarray) -> dict:
    wins = []
    for i, d in enumerate(days):
        end = (pd.Timestamp(d) + pd.Timedelta(days=30)).date()
        j = bisect.bisect_right(days, end) - 1
        if j > i and end <= days[-1]:
            wins.append((i, j))
    rows = []
    for i, j in wins:
        eq = START
        threshold = START - MAX_DD
        eod_peak = START
        passed = failed = False
        pass_day = None
        ntr = 0
        for k in range(i, j + 1):
            eq += pnls[k]
            ntr += int(trades[k])
            if eq <= threshold:
                failed = True
                break
            if eq >= TARGET:
                passed = True
                pass_day = (pd.Timestamp(days[k]) - pd.Timestamp(days[i])).days + 1
                break
            eod_peak = max(eod_peak, eq)
            threshold = max(threshold, eod_peak - MAX_DD)
        rows.append((passed, failed, eq - START, ntr, pass_day))
    prof = np.array([r[2] for r in rows], dtype=float) if rows else np.array([0.0])
    pass_days = [r[4] for r in rows if r[4] is not None]
    return {
        "windows": len(rows),
        "passes": int(sum(r[0] for r in rows)),
        "fails": int(sum(r[1] for r in rows)),
        "rate": float(sum(r[0] for r in rows) / len(rows)) if rows else 0.0,
        "median": float(np.median(prof)),
        "worst": float(prof.min()),
        "best": float(prof.max()),
        "avg_days": float(np.mean(pass_days)) if pass_days else None,
    }


def main(refresh: bool = False) -> None:
    raw = {k: prep(fetch_yahoo(spec.yahoo, refresh=refresh)) for k, spec in SPECS.items()}
    print("Data coverage", flush=True)
    for k, df in raw.items():
        print(f"  {k}: {df['ny'].iloc[0]} -> {df['ny'].iloc[-1]} | {len(df)} bars | {df['day'].nunique()} days", flush=True)
    presets = [
        # strict ICT 2022-style
        ("prevday", 5, 0.10, 3.0, True, 1.2),
        ("or15", 5, 0.10, 3.0, True, 1.2),
        # more frequent variants
        ("prevday", 3, 0.05, 2.0, False, 0.8),
        ("or15", 3, 0.05, 2.0, False, 0.8),
        ("or15", 3, 0.05, 0.0, False, 0.8),
    ]
    results = []
    for market, spec in SPECS.items():
        groups = [(d, g.reset_index(drop=True)) for d, g in raw[market].groupby("day", sort=True)]
        days = [d for d, _ in groups]
        for pool, lookback, buf, rr, require_fvg, disp in presets:
            for contracts in range(1, spec.max_contracts + 1):
                if market in ("ES",) and contracts > 2:
                    continue
                if market in ("NQ",) and contracts > 1:
                    continue
                p = Params(market, pool, contracts, lookback, buf, rr, require_fvg, disp)
                vals = [ict_trade_for_day(g, p, spec) for _, g in groups]
                pnls = np.array([v[0] for v in vals], dtype=float)
                trades = np.array([v[1] for v in vals], dtype=int)
                m = eval_windows(days, pnls, trades)
                m["params"] = p
                m["trades"] = int(trades.sum())
                results.append(m)
    results.sort(key=lambda m: (m["rate"], -m["fails"], m["median"], m["best"]), reverse=True)
    print("\nTop ICT-style candidates", flush=True)
    for m in results[:30]:
        p = m["params"]
        avg = "-" if m["avg_days"] is None else f"{m['avg_days']:.1f}"
        print(
            f"{p.market:<3} pool={p.pool:<7} ct={p.contracts} lb={p.lookback} buf={p.stop_buffer_atr:<4} "
            f"rr={p.rr:<3} fvg={str(p.require_fvg):<5} disp={p.min_disp_atr:<3} | "
            f"pass {m['passes']:>2}/{m['windows']} ({m['rate']*100:>5.1f}%) "
            f"ddFail {m['fails']:>2} med ${m['median']:>7.0f} worst ${m['worst']:>7.0f} "
            f"best ${m['best']:>7.0f} avgDay {avg} trades {m['trades']}",
            flush=True,
        )


if __name__ == "__main__":
    main(refresh=False)
