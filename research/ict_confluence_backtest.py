"""
Broad ICT-style confluence backtest for Apex-style evaluation.

This is research only. It converts common ICT concepts into strict mechanical
rules so they can be tested:

  - NY killzones
  - previous-day, opening-range, Asia/premarket, and equal-high/low liquidity
  - liquidity sweep / raid
  - market-structure shift
  - displacement
  - fair value gap
  - order block midpoint
  - premium / discount filter
  - simple HTF bias filter
  - target at opposite liquidity or fixed R

It is not "all ICT" in the discretionary sense. A bot cannot see a teacher's
chart narrative unless the narrative is made into exact code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
import bisect
import itertools
import math

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
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
    contracts: int
    entry_model: str = "fvg_ob"      # fvg, ob, fvg_ob, close
    target_model: str = "opp"        # opp, 2r, 3r
    min_score: int = 4
    use_htf_bias: bool = False
    use_premium_discount: bool = True
    include_pm: bool = False
    lookback: int = 4
    min_disp_atr: float = 0.8
    stop_buffer_atr: float = 0.05
    kill_start: str = "09:30"
    kill_end: str = "11:30"
    expiry_bars: int = 18


SPECS = {
    "ES": Spec("ES", "ES=F", 50.0, 0.25, 2),
    "NQ": Spec("NQ", "NQ=F", 20.0, 0.25, 1),
    "MES": Spec("MES", "MES=F", 5.0, 0.25, 6),
    "MNQ": Spec("MNQ", "MNQ=F", 2.0, 0.25, 6),
    "BTC": Spec("BTC", "BTC=F", 5.0, 5.0, 1),
}


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def safe_symbol(symbol: str) -> str:
    return symbol.replace("=", "_").replace("/", "_")


def load_5m(market: str) -> pd.DataFrame:
    spec = SPECS[market]
    # Full premarket cache exists for index futures. BTC currently has the RTH ICT cache.
    full = CACHE / f"apex_{safe_symbol(spec.yahoo)}_5m_60d.csv"
    ict = CACHE / f"apex_ict_{safe_symbol(spec.yahoo)}_5m_60d.csv"
    path = full if full.exists() else ict
    df = pd.read_csv(path)
    if "dt_utc" in df.columns:
        df = df.rename(columns={"dt_utc": "dt", "dt_ny": "ny"})
    df["dt"] = pd.to_datetime(df["dt"], utc=True)
    df["ny"] = pd.to_datetime(df["ny"], utc=True, errors="coerce")
    if df["ny"].isna().any():
        df["ny"] = df["dt"].dt.tz_convert(NY)
    else:
        df["ny"] = df["dt"].dt.tz_convert(NY)
    df["day"] = df["ny"].dt.date
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    pc = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - pc).abs(),
        (d["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    mins = d["ny"].map(minute)
    rth = d[(mins >= 9 * 60 + 30) & (mins <= 16 * 60)].copy()
    daily = rth.groupby("day").agg(
        pdh=("high", "max"),
        pdl=("low", "min"),
        pdc=("close", "last"),
    )
    daily["pdm"] = (daily["pdh"] + daily["pdl"]) / 2.0
    daily["sma5"] = daily["pdc"].rolling(5).mean()
    shifted = daily.shift(1)
    return d.merge(shifted, left_on="day", right_index=True, how="left")


def rth_day(df: pd.DataFrame, day) -> pd.DataFrame:
    g = df[df["day"] == day].copy()
    mins = g["ny"].map(minute)
    return g[(mins >= 9 * 60 + 30) & (mins <= 16 * 60)].reset_index(drop=True)


def full_day(df: pd.DataFrame, day) -> pd.DataFrame:
    return df[df["day"] == day].copy().reset_index(drop=True)


def fvg_in_leg(g: pd.DataFrame, lo: int, hi: int, side: int, min_gap: float):
    lo = max(2, lo)
    hi = min(hi, len(g) - 1)
    out = []
    for j in range(lo, hi + 1):
        a = g.iloc[j - 2]
        c = g.iloc[j]
        if side > 0:
            gap = float(c["low"] - a["high"])
            if gap >= min_gap:
                out.append((float((c["low"] + a["high"]) / 2.0), gap, j))
        else:
            gap = float(a["low"] - c["high"])
            if gap >= min_gap:
                out.append((float((a["low"] + c["high"]) / 2.0), gap, j))
    if not out:
        return None
    return max(out, key=lambda x: x[1])


def order_block_mid(g: pd.DataFrame, lo: int, hi: int, side: int):
    lo = max(0, lo)
    hi = min(hi, len(g) - 1)
    for j in range(hi, lo - 1, -1):
        r = g.iloc[j]
        bearish = r["close"] < r["open"]
        bullish = r["close"] > r["open"]
        if (side > 0 and bearish) or (side < 0 and bullish):
            return (float((r["high"] + r["low"]) / 2.0), j)
    return None


def swing_points(prior: pd.DataFrame, w: int = 2):
    highs, lows = [], []
    if len(prior) < 2 * w + 1:
        return highs, lows
    for i in range(w, len(prior) - w):
        h = prior.iloc[i]["high"]
        l = prior.iloc[i]["low"]
        if all(h > prior.iloc[i - k]["high"] for k in range(1, w + 1)) and all(h > prior.iloc[i + k]["high"] for k in range(1, w + 1)):
            highs.append(float(h))
        if all(l < prior.iloc[i - k]["low"] for k in range(1, w + 1)) and all(l < prior.iloc[i + k]["low"] for k in range(1, w + 1)):
            lows.append(float(l))
    return highs, lows


def equal_pool(vals: list[float], tol: float):
    if len(vals) < 2 or not math.isfinite(tol) or tol <= 0:
        return None
    vals = sorted(vals)
    best = None
    for a, b in zip(vals, vals[1:]):
        if abs(a - b) <= tol:
            best = (a + b) / 2.0
    return best


def build_static_pools(day_full: pd.DataFrame, day_rth: pd.DataFrame):
    pools = []
    first = day_rth.iloc[0]
    if np.isfinite(first.get("pdh", np.nan)):
        pools.append(("pdh", -1, float(first["pdh"]), 2))
    if np.isfinite(first.get("pdl", np.nan)):
        pools.append(("pdl", 1, float(first["pdl"]), 2))
    mins = day_rth["ny"].map(minute)
    or15 = day_rth[(mins >= 9 * 60 + 30) & (mins < 9 * 60 + 45)]
    if len(or15):
        pools.append(("orh", -1, float(or15["high"].max()), 1))
        pools.append(("orl", 1, float(or15["low"].min()), 1))
    mins_full = day_full["ny"].map(minute)
    asia = day_full[(mins_full >= 0) & (mins_full < 8 * 60 + 30)]
    if len(asia) >= 8:
        pools.append(("asia_h", -1, float(asia["high"].max()), 1))
        pools.append(("asia_l", 1, float(asia["low"].min()), 1))
    return pools


def choose_entry(g: pd.DataFrame, i: int, j: int, side: int, p: Params, atr: float):
    fvg = fvg_in_leg(g, i + 1, j, side, 0.15 * atr)
    ob = order_block_mid(g, i, j, side)
    if p.entry_model == "fvg":
        return (fvg[0], "fvg", bool(fvg), bool(ob)) if fvg else None
    if p.entry_model == "ob":
        return (ob[0], "ob", bool(fvg), bool(ob)) if ob else None
    if p.entry_model == "close":
        return (float(g.iloc[j]["close"]), "close", bool(fvg), bool(ob))
    # fvg_ob: prefer FVG midpoint, fallback to OB midpoint.
    if fvg:
        return (fvg[0], "fvg", True, bool(ob))
    if ob:
        return (ob[0], "ob", False, True)
    return None


def trade_day(day_full: pd.DataFrame, day_rth: pd.DataFrame, p: Params, spec: Spec):
    if len(day_rth) < 35:
        return 0.0, 0, "none"
    g = day_rth.reset_index(drop=True)
    mins = g["ny"].map(minute)
    tradable = np.where((mins >= hhmm(p.kill_start)) & (mins <= hhmm(p.kill_end)))[0]
    if p.include_pm:
        pm = np.where((mins >= 13 * 60 + 30) & (mins <= 15 * 60 + 30))[0]
        tradable = np.array(sorted(set(tradable) | set(pm)))
    if len(tradable) == 0:
        return 0.0, 0, "none"
    static_pools = build_static_pools(day_full, g)
    if not static_pools:
        return 0.0, 0, "none"
    first = g.iloc[0]
    day_mid = float(first["pdm"]) if np.isfinite(first.get("pdm", np.nan)) else None
    htf = 0
    if np.isfinite(first.get("pdc", np.nan)) and np.isfinite(first.get("sma5", np.nan)):
        htf = 1 if first["pdc"] > first["sma5"] else -1

    for i in tradable:
        if i < max(4, p.lookback):
            continue
        row = g.iloc[i]
        atr = float(row["atr"])
        if not np.isfinite(atr) or atr <= 0:
            continue
        prior = g.iloc[max(0, i - 60):i]
        highs, lows = swing_points(prior)
        eqh = equal_pool(highs[-8:], 0.10 * atr)
        eql = equal_pool(lows[-8:], 0.10 * atr)
        pools = list(static_pools)
        if eqh is not None:
            pools.append(("eqh", -1, eqh, 1))
        if eql is not None:
            pools.append(("eql", 1, eql, 1))

        for pool_name, side, level, pool_score in pools:
            if side > 0 and row["low"] >= level - 0.05 * atr:
                continue
            if side < 0 and row["high"] <= level + 0.05 * atr:
                continue
            raid = float(row["low"] if side > 0 else row["high"])
            mss_prior = g.iloc[max(0, i - p.lookback):i]
            if len(mss_prior) < 2:
                continue
            mss_level = float(mss_prior["high"].max()) if side > 0 else float(mss_prior["low"].min())
            for j in range(i + 1, min(len(g) - 1, i + p.expiry_bars + 1)):
                r = g.iloc[j]
                rng = max(float(r["high"] - r["low"]), 1e-9)
                body = abs(float(r["close"] - r["open"]))
                leg = abs(float(r["close"]) - raid)
                disp = (rng >= p.min_disp_atr * atr and body / rng >= 0.5) or leg >= 1.5 * atr
                mss = (r["close"] > mss_level) if side > 0 else (r["close"] < mss_level)
                if not (mss and disp):
                    continue
                chosen = choose_entry(g, i, j, side, p, atr)
                if chosen is None:
                    continue
                entry, entry_type, has_fvg, has_ob = chosen
                stop = raid - side * p.stop_buffer_atr * atr
                risk_pts = abs(entry - stop)
                if risk_pts <= 0:
                    continue
                dollar_risk = risk_pts * spec.point_value * p.contracts
                if dollar_risk > DAILY_LOSS * 0.95:
                    continue

                score = 2 + pool_score
                if has_fvg:
                    score += 1
                if has_ob:
                    score += 1
                if htf == side:
                    score += 1
                elif p.use_htf_bias:
                    continue
                pd_ok = True
                if p.use_premium_discount and day_mid is not None:
                    pd_ok = entry < day_mid if side > 0 else entry > day_mid
                    if pd_ok:
                        score += 1
                if score < p.min_score:
                    continue

                opp = None
                opposite = [x for x in static_pools if x[1] == -side]
                if side > 0:
                    above = [x[2] for x in opposite if x[2] > entry]
                    opp = min(above) if above else None
                else:
                    below = [x[2] for x in opposite if x[2] < entry]
                    opp = max(below) if below else None
                if p.target_model == "2r":
                    target = entry + side * 2.0 * risk_pts
                elif p.target_model == "3r":
                    target = entry + side * 3.0 * risk_pts
                else:
                    target = opp if opp is not None else entry + side * 2.0 * risk_pts
                    if side > 0 and target <= entry:
                        target = entry + 2.0 * risk_pts
                    if side < 0 and target >= entry:
                        target = entry - 2.0 * risk_pts
                rr = abs(target - entry) / risk_pts
                if rr < 1.5:
                    continue

                filled = p.entry_model == "close"
                start_k = j + 1
                for k in range(start_k, len(g)):
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
                            pnl = (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                            return float(pnl), 1, f"{pool_name}/{entry_type}/sl"
                        if hi >= target:
                            exit_px = target - spec.tick
                            pnl = (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                            return float(pnl), 1, f"{pool_name}/{entry_type}/tp"
                    else:
                        if hi >= stop:
                            exit_px = stop + spec.tick
                            pnl = (entry - exit_px) * spec.point_value * p.contracts - 5 * p.contracts
                            return float(pnl), 1, f"{pool_name}/{entry_type}/sl"
                        if lo <= target:
                            exit_px = target + spec.tick
                            pnl = (entry - exit_px) * spec.point_value * p.contracts - 5 * p.contracts
                            return float(pnl), 1, f"{pool_name}/{entry_type}/tp"
                    if minute(b["ny"]) >= 15 * 60 + 55:
                        exit_px = close - side * spec.tick
                        pnl = side * (exit_px - entry) * spec.point_value * p.contracts - 5 * p.contracts
                        return float(pnl), 1, f"{pool_name}/{entry_type}/eod"
    return 0.0, 0, "none"


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


def main():
    profiles = [
        # Broad ICT confluence profile:
        # liquidity sweep + MSS/displacement + FVG/OB entry + premium/discount
        # + opposite-liquidity target.
        ("broad_confluence", dict(
            entry_model="fvg_ob",
            target_model="opp",
            min_score=4,
            use_htf_bias=False,
            use_premium_discount=True,
            include_pm=False,
            min_disp_atr=0.8,
        )),
    ]
    grids = []
    for market, spec in SPECS.items():
        df = prep(load_5m(market))
        days = sorted(df["day"].unique())
        grouped = [(d, full_day(df, d), rth_day(df, d)) for d in days]
        grouped = [(d, f, r) for d, f, r in grouped if len(r) >= 35]
        for profile_name, kwargs in profiles:
            for contracts in range(1, spec.max_contracts + 1):
                p = Params(
                    market=market,
                    contracts=contracts,
                    **kwargs,
                )
                vals = [trade_day(f, r, p, spec) for _, f, r in grouped]
                pnls = np.array([x[0] for x in vals], dtype=float)
                trades = np.array([x[1] for x in vals], dtype=int)
                m = eval_windows([d for d, _, _ in grouped], pnls, trades)
                m["params"] = p
                m["profile"] = profile_name
                m["trades"] = int(trades.sum())
                grids.append(m)
    grids.sort(key=lambda x: (x["rate"], -x["fails"], x["median"], x["best"]), reverse=True)
    print("Top broad ICT confluence candidates")
    print("market profile entry target ct score htf pd pm disp | pass fails median worst best trades")
    for m in grids[:40]:
        p = m["params"]
        print(
            f"{p.market:<3} {m['profile']:<17} {p.entry_model:<6} {p.target_model:<3} {p.contracts:<2} "
            f"{p.min_score:<2} {str(p.use_htf_bias):<5} {str(p.use_premium_discount):<5} "
            f"{str(p.include_pm):<5} {p.min_disp_atr:<3} | "
            f"{m['passes']:>2}/{m['windows']:<2} {m['rate']*100:>5.1f}% "
            f"{m['fails']:>2} {m['median']:>7.0f} {m['worst']:>7.0f} "
            f"{m['best']:>7.0f} {m['trades']:>3}"
        )


if __name__ == "__main__":
    main()
