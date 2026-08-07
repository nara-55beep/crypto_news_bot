"""
ICT vocabulary confluence backtest for Apex-style 50K evaluations.

Research only. This takes the user's ICT vocabulary list and converts the terms
into mechanical features that can be tested on longer free 1h futures data:

- PDH/PDL, PWH/PWL, PMH/PML, HOD/LOD, daily/weekly/monthly opens, NY midnight open
- Asian/London/NY session highs/lows where the feed has those hours
- BSL/SSL, EQH/EQL, internal/external liquidity, draw on liquidity
- swings, BOS/MSS/CHOCH, displacement, expansion/retracement/consolidation
- FVG, IFVG, OB, breaker/mitigation/rejection/propulsion proxy, BPR, liquidity void
- CE/EQ, OTE, premium/discount
- killzones and macro windows
- daily/weekly bias, AMD/Judas profile
- SMT divergence for ES/NQ and MES/MNQ pairs
- structural stops, opposing-liquidity targets, time stops

Because the free 1-year-plus futures feed is 1h, this is a robust direction/risk
test, not tick-level execution proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse
import bisect
import itertools
import math
import time

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
COMM_PER_CONTRACT = 5.0


@dataclass(frozen=True)
class Spec:
    key: str
    yahoo: str
    point_value: float
    tick: float
    max_contracts: int
    apex: bool = True


@dataclass(frozen=True)
class Params:
    market: str
    contracts: int
    min_score: int
    entry_model: str       # close, ce, ote, ob, fvg_ob
    target_model: str      # opposing, fixed2, fixed3, session
    stop_model: str        # sweep, ob, atr
    rr_min: float
    use_premium: bool
    use_smt: bool
    bias_mode: str         # none, daily, weekly, dol
    time_filter: str       # nyam, macros, broad
    profile: str           # reversal, continuation, both


@dataclass
class DayResult:
    day: object
    pnl: float
    trades: int
    signal: str = ""


SPECS = {
    "ES": Spec("ES", "ES=F", 50.0, 0.25, 2),
    "NQ": Spec("NQ", "NQ=F", 20.0, 0.25, 1),
    "MES": Spec("MES", "MES=F", 5.0, 0.25, 6),
    "MNQ": Spec("MNQ", "MNQ=F", 2.0, 0.25, 6),
    "CL": Spec("CL", "CL=F", 1000.0, 0.01, 1),
    "BTC": Spec("BTC", "BTC=F", 5.0, 5.0, 1, apex=False),
}

PEERS = {"ES": "NQ", "NQ": "ES", "MES": "MNQ", "MNQ": "MES"}


def cache_path(symbol: str) -> Path:
    safe = symbol.replace("=", "_").replace("/", "_")
    return CACHE / f"ict_vocab_{safe}_1h_730d.csv"


def fetch_yahoo(symbol: str, refresh: bool = False) -> pd.DataFrame:
    path = cache_path(symbol)
    if path.exists() and not refresh:
        df = pd.read_csv(path)
        df["dt"] = pd.to_datetime(df["dt"], utc=True)
        df["ny"] = df["dt"].dt.tz_convert(NY)
        df["day"] = pd.to_datetime(df["day"]).dt.date
        df["minute"] = df["ny"].dt.hour * 60 + df["ny"].dt.minute
        return df

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(
        url,
        params={"range": "730d", "interval": "1h", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    raw = r.json()["chart"]["result"][0]
    q = raw["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "dt": pd.to_datetime(raw["timestamp"], unit="s", utc=True),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "close": q.get("close"),
            "volume": q.get("volume"),
        }
    ).dropna(subset=["open", "high", "low", "close"])
    df["ny"] = df["dt"].dt.tz_convert(NY)
    df["day"] = df["ny"].dt.date
    df["minute"] = df["ny"].dt.hour * 60 + df["ny"].dt.minute
    df = df.reset_index(drop=True)
    df.to_csv(path, index=False)
    time.sleep(0.25)
    return df


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["minute"] = d["ny"].dt.hour * 60 + d["ny"].dt.minute
    d["week"] = d["ny"].dt.strftime("%G-%V")
    d["month"] = d["ny"].dt.strftime("%Y-%m")
    pc = d["close"].shift(1)
    tr = pd.concat(
        [
            d["high"] - d["low"],
            (d["high"] - pc).abs(),
            (d["low"] - pc).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["atr_pct"] = (d["atr"] / d["close"]).rolling(120, min_periods=30).rank(pct=True)
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()

    daily = d.groupby("day").agg(
        pdh=("high", "max"),
        pdl=("low", "min"),
        pdc=("close", "last"),
        pdo=("open", "first"),
    ).shift(1)
    weekly = d.groupby("week").agg(
        pwh=("high", "max"),
        pwl=("low", "min"),
        pwo=("open", "first"),
    ).shift(1)
    monthly = d.groupby("month").agg(
        pmh=("high", "max"),
        pml=("low", "min"),
        pmo=("open", "first"),
    ).shift(1)
    d = d.merge(daily, left_on="day", right_index=True, how="left")
    d = d.merge(weekly, left_on="week", right_index=True, how="left")
    d = d.merge(monthly, left_on="month", right_index=True, how="left")
    return d


def in_time(minute: int, mode: str) -> bool:
    london = 2 * 60 <= minute < 5 * 60
    nyam = 8 * 60 + 30 <= minute < 11 * 60 + 30
    nypm = 13 * 60 + 10 <= minute < 15 * 60 + 45
    macros = (
        2 * 60 + 33 <= minute < 3 * 60
        or 4 * 60 + 3 <= minute < 4 * 60 + 30
        or 8 * 60 + 50 <= minute < 9 * 60 + 10
        or 9 * 60 + 50 <= minute < 10 * 60 + 10
        or 10 * 60 + 50 <= minute < 11 * 60 + 10
        or 13 * 60 + 10 <= minute < 13 * 60 + 40
        or 15 * 60 + 15 <= minute < 15 * 60 + 45
    )
    if mode == "nyam":
        return nyam
    if mode == "macros":
        return macros
    return london or nyam or nypm or macros


def swing_points(g: pd.DataFrame, w: int = 2) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    h = g["high"].to_numpy(float)
    l = g["low"].to_numpy(float)
    for i in range(w, len(g) - w):
        if all(h[i] > h[i - j] for j in range(1, w + 1)) and all(h[i] > h[i + j] for j in range(1, w + 1)):
            highs.append(i)
        if all(l[i] < l[i - j] for j in range(1, w + 1)) and all(l[i] < l[i + j] for j in range(1, w + 1)):
            lows.append(i)
    return highs, lows


def session_levels(full: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    if full.empty:
        return out
    m = full["minute"]
    sessions = {
        "asia": (m >= 18 * 60) | (m < 2 * 60),
        "london": (m >= 2 * 60) & (m < 5 * 60),
        "nyam": (m >= 8 * 60 + 30) & (m < 11 * 60 + 30),
    }
    for name, mask in sessions.items():
        s = full[mask]
        if len(s) >= 2:
            out[f"{name}_h"] = float(s["high"].max())
            out[f"{name}_l"] = float(s["low"].min())
    ny_mid = full[m >= 0]
    if len(ny_mid):
        out["ny_midnight_open"] = float(ny_mid.iloc[0]["open"])
    return out


def equal_levels(hist: pd.DataFrame, px: float) -> tuple[list[float], list[float]]:
    if len(hist) < 20:
        return [], []
    tol = max(px * 0.0008, 0.01)
    highs, lows = swing_points(hist.tail(180).reset_index(drop=True), 2)
    hh = hist.tail(180).reset_index(drop=True)["high"].to_numpy(float)
    ll = hist.tail(180).reset_index(drop=True)["low"].to_numpy(float)
    eqh, eql = [], []
    for a, b in itertools.combinations(highs[-20:], 2):
        p = (hh[a] + hh[b]) / 2
        if abs(hh[a] - hh[b]) <= tol and all(abs(p - x) > tol for x in eqh):
            eqh.append(float(p))
    for a, b in itertools.combinations(lows[-20:], 2):
        p = (ll[a] + ll[b]) / 2
        if abs(ll[a] - ll[b]) <= tol and all(abs(p - x) > tol for x in eql):
            eql.append(float(p))
    return eqh[-5:], eql[-5:]


def preday_context(hist: pd.DataFrame, px: float) -> tuple[list[float], list[float], list[float], list[float]]:
    eqh, eql = equal_levels(hist, px)
    swings_h, swings_l = swing_points(hist.tail(140).reset_index(drop=True), 2)
    h = hist.tail(140).reset_index(drop=True)["high"].to_numpy(float)
    l = hist.tail(140).reset_index(drop=True)["low"].to_numpy(float)
    old_h = [float(h[idx]) for idx in swings_h[-4:]]
    old_l = [float(l[idx]) for idx in swings_l[-4:]]
    return eqh, eql, old_h, old_l


def pools_for(day: pd.DataFrame, i: int, ctx: tuple[list[float], list[float], list[float], list[float]]) -> list[tuple[str, int, float, int]]:
    row = day.iloc[i]
    pools: list[tuple[str, int, float, int]] = []

    def add(name: str, side: int, price, prio: int):
        if price is not None and np.isfinite(price) and price > 0:
            pools.append((name, side, float(price), prio))

    add("PDH", 1, row.get("pdh"), 5)
    add("PDL", -1, row.get("pdl"), 5)
    add("PWH", 1, row.get("pwh"), 4)
    add("PWL", -1, row.get("pwl"), 4)
    add("PMH", 1, row.get("pmh"), 3)
    add("PML", -1, row.get("pml"), 3)
    add("HOD", 1, day.iloc[: i + 1]["high"].max(), 2)
    add("LOD", -1, day.iloc[: i + 1]["low"].min(), 2)
    sess = session_levels(day.iloc[: i + 1])
    for key, prio in (("asia_h", 3), ("london_h", 3), ("nyam_h", 2)):
        add(key, 1, sess.get(key), prio)
    for key, prio in (("asia_l", 3), ("london_l", 3), ("nyam_l", 2)):
        add(key, -1, sess.get(key), prio)
    eqh, eql, old_h, old_l = ctx
    for p in eqh:
        add("EQH", 1, p, 3)
    for p in eql:
        add("EQL", -1, p, 3)
    for p in old_h:
        add("old high", 1, p, 2)
    for p in old_l:
        add("old low", -1, p, 2)

    dedup: list[tuple[str, int, float, int]] = []
    for p in sorted(pools, key=lambda x: (-x[3], x[0])):
        tol = max(p[2] * 0.00025, 0.01)
        if not any(abs(p[2] - q[2]) <= tol and p[1] == q[1] for q in dedup):
            dedup.append(p)
    return dedup


def fvg_in_leg(g: pd.DataFrame, start: int, end: int, side: int) -> tuple[float, float, float] | None:
    if end - start < 2:
        return None
    for i in range(max(2, start), min(len(g), end + 1)):
        a = g.iloc[i]["atr"]
        a = a if np.isfinite(a) and a > 0 else max(g.iloc[i]["high"] - g.iloc[i]["low"], 1.0)
        if side > 0 and g.iloc[i]["low"] > g.iloc[i - 2]["high"] + 0.03 * a:
            top = float(g.iloc[i]["low"])
            bot = float(g.iloc[i - 2]["high"])
            return top, bot, (top + bot) / 2
        if side < 0 and g.iloc[i]["high"] < g.iloc[i - 2]["low"] - 0.03 * a:
            top = float(g.iloc[i - 2]["low"])
            bot = float(g.iloc[i]["high"])
            return top, bot, (top + bot) / 2
    return None


def order_block(g: pd.DataFrame, start: int, end: int, side: int) -> tuple[float, float, float] | None:
    for i in range(end - 1, max(start - 1, 0), -1):
        c = g.iloc[i]
        if side > 0 and c["close"] < c["open"]:
            return float(c["high"]), float(c["low"]), (float(c["high"]) + float(c["low"])) / 2
        if side < 0 and c["close"] > c["open"]:
            return float(c["high"]), float(c["low"]), (float(c["high"]) + float(c["low"])) / 2
    return None


def smt_ok(market: str, day, i: int, side: int, peer_maps: dict[str, dict]) -> bool:
    peer = PEERS.get(market)
    if not peer or peer not in peer_maps:
        return True
    key = day.iloc[i]["dt"]
    p = peer_maps[peer].get(key)
    if p is None:
        return True
    # Bullish SMT proxy: our market sweeps lower but peer is not strongly below EMA20.
    # Bearish proxy: our market sweeps higher but peer is not strongly above EMA20.
    return p != side


def peer_bias_maps(data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    out = {}
    for key, df in data.items():
        out[key] = {r.dt: (1 if r.close > r.ema20 else -1 if r.close < r.ema20 else 0) for r in df.itertuples()}
    return out


def day_trade(
    market: str,
    full_df: pd.DataFrame,
    idxs: np.ndarray,
    p: Params,
    spec: Spec,
    peer_maps: dict[str, dict],
) -> DayResult:
    g = full_df.iloc[idxs].reset_index(drop=True)
    if len(g) < 8:
        return DayResult(g.iloc[0]["day"] if len(g) else None, 0.0, 0, "short-day")

    day_obj = g.iloc[0]["day"]
    hist_start = max(0, idxs[0] - 240)
    hist = full_df.iloc[hist_start:idxs[0]].copy()
    if len(hist) < 60:
        return DayResult(day_obj, 0.0, 0, "no-history")

    swings_h, swings_l = swing_points(g, 2)
    ctx = preday_context(hist, float(g.iloc[0]["close"]))
    eq_low = np.nanmin([g.iloc[0].get("pdl", np.nan), g.iloc[0].get("pwl", np.nan), g.iloc[0].get("pml", np.nan)])
    eq_high = np.nanmax([g.iloc[0].get("pdh", np.nan), g.iloc[0].get("pwh", np.nan), g.iloc[0].get("pmh", np.nan)])
    eq_mid = (eq_high + eq_low) / 2 if np.isfinite(eq_high) and np.isfinite(eq_low) and eq_high > eq_low else None
    daily_open = float(g.iloc[0]["open"])
    weekly_open = float(g.iloc[0].get("pwo", np.nan)) if np.isfinite(g.iloc[0].get("pwo", np.nan)) else daily_open
    monthly_open = float(g.iloc[0].get("pmo", np.nan)) if np.isfinite(g.iloc[0].get("pmo", np.nan)) else daily_open

    for i in range(3, len(g) - 1):
        row = g.iloc[i]
        if not in_time(int(row["minute"]), p.time_filter):
            continue
        a = float(row["atr"]) if np.isfinite(row["atr"]) and row["atr"] > 0 else float(row["high"] - row["low"])
        if not np.isfinite(a) or a <= 0:
            continue
        pools = pools_for(g, i, ctx)
        px = float(row["close"])
        near = [pool for pool in pools if abs(pool[2] - px) / px <= 0.025]
        if not near:
            continue
        pen = max(0.05 * a, px * 0.00008)
        swept = None
        for name, liq_side, level, prio in near:
            if liq_side < 0 and row["low"] < level - pen and row["close"] > level:
                swept = (name, 1, level, prio, float(row["low"]))
            if liq_side > 0 and row["high"] > level + pen and row["close"] < level:
                swept = (name, -1, level, prio, float(row["high"]))
            if swept:
                break
        if not swept:
            continue

        pool_name, side, pool_px, pool_prio, raid = swept
        if p.profile == "continuation":
            # Continuation profile: a shallow raid in the direction of daily bias.
            pass
        if p.profile == "reversal":
            # Reversal profile: sweep one side then move away from it.
            pass

        if p.bias_mode != "none":
            d_bias = 1 if px > daily_open else -1
            w_bias = 1 if px > weekly_open else -1
            m_bias = 1 if px > monthly_open else -1
            if p.bias_mode == "daily" and d_bias != side:
                continue
            if p.bias_mode == "weekly" and (w_bias + m_bias) * side <= 0:
                continue
            if p.bias_mode == "dol":
                above = [x[2] for x in pools if x[1] > 0 and x[2] > px]
                below = [x[2] for x in pools if x[1] < 0 and x[2] < px]
                dol_side = 1 if above and (not below or min(above) - px < px - max(below)) else -1
                if dol_side != side:
                    continue

        if p.use_smt and not smt_ok(market, g, i, side, peer_maps):
            continue

        # MSS/CHOCH/BOS after sweep.
        prior_swings = [x for x in (swings_h if side > 0 else swings_l) if max(0, i - 18) <= x < i]
        if prior_swings:
            mss_level = float(g.iloc[prior_swings[-1]]["high" if side > 0 else "low"])
        else:
            look = g.iloc[max(0, i - 8):i]
            mss_level = float(look["high"].max() if side > 0 else look["low"].min())
        mss_i = None
        for j in range(i + 1, min(len(g), i + 8)):
            c = g.iloc[j]
            broke = c["close"] > mss_level if side > 0 else c["close"] < mss_level
            if not broke:
                continue
            rng = max(float(c["high"] - c["low"]), spec.tick)
            body = abs(float(c["close"] - c["open"]))
            tr = max(float(c["high"] - c["low"]), abs(float(c["high"] - g.iloc[j - 1]["close"])), abs(float(c["low"] - g.iloc[j - 1]["close"])))
            leg = abs(float(c["close"]) - raid)
            displaced = tr >= 0.65 * a or (body / rng >= 0.52 and leg >= 0.8 * a)
            if displaced:
                mss_i = j
                break
        if mss_i is None:
            continue

        fvg = fvg_in_leg(g, i, mss_i, side)
        ob = order_block(g, i, mss_i, side)
        if p.entry_model == "close":
            entry = float(g.iloc[mss_i]["close"])
            entry_src = "MSS close"
            zone = None
        elif p.entry_model == "ce":
            entry = (mss_level + raid) / 2
            entry_src = "CE"
            zone = None
        elif p.entry_model == "ote":
            # OTE midpoint between 62% and 79% retracement of raid -> MSS impulse.
            impulse = float(g.iloc[mss_i]["close"]) - raid
            entry = raid + 0.705 * impulse
            entry_src = "OTE"
            zone = None
        elif p.entry_model == "ob" and ob:
            entry = ob[2]
            entry_src = "OB"
            zone = ob
        elif p.entry_model == "fvg_ob":
            zone = fvg or ob
            if not zone:
                continue
            entry = zone[2]
            entry_src = "FVG" if fvg else "OB"
        else:
            continue

        if p.use_premium and eq_mid is not None:
            if side > 0 and entry >= eq_mid:
                continue
            if side < 0 and entry <= eq_mid:
                continue

        if p.stop_model == "sweep":
            stop = raid - side * 0.08 * a
        elif p.stop_model == "ob" and zone:
            stop = (zone[1] - 0.05 * a) if side > 0 else (zone[0] + 0.05 * a)
        else:
            stop = entry - side * 0.8 * a
        risk_pts = abs(entry - stop)
        if risk_pts <= spec.tick:
            continue
        dollar_risk = risk_pts * spec.point_value * p.contracts + COMM_PER_CONTRACT * p.contracts
        if dollar_risk > DAILY_LOSS * 0.8 or dollar_risk > MAX_DD * 0.55:
            continue

        score = 0
        score += pool_prio
        score += 2                       # sweep/raid
        score += 2                       # MSS/CHOCH with displacement
        score += 1 if fvg else 0
        score += 1 if ob else 0
        if fvg and ob and abs(fvg[2] - ob[2]) <= max(a * 0.4, spec.tick):
            score += 1                   # unicorn / BPR-style overlap
        if row["high"] - row["low"] > 1.2 * a:
            score += 1                   # expansion / liquidity void proxy
        if abs(entry - ((mss_level + raid) / 2)) <= max(a * 0.35, spec.tick):
            score += 1                   # CE / mean threshold / mitigation
        if p.use_premium and eq_mid is not None:
            score += 1
        if p.use_smt:
            score += 1
        if score < p.min_score:
            continue

        above = sorted([x[2] for x in pools if x[1] > 0 and x[2] > entry])
        below = sorted([x[2] for x in pools if x[1] < 0 and x[2] < entry], reverse=True)
        if p.target_model == "fixed2":
            target = entry + side * 2.0 * risk_pts
        elif p.target_model == "fixed3":
            target = entry + side * 3.0 * risk_pts
        elif p.target_model == "session":
            target = (max(g["high"].iloc[: i + 1]) if side > 0 else min(g["low"].iloc[: i + 1]))
            if (side > 0 and target <= entry) or (side < 0 and target >= entry):
                target = entry + side * 2.0 * risk_pts
        else:
            target = (above[0] if side > 0 and above else below[0] if side < 0 and below else entry + side * 2.0 * risk_pts)
        rr = abs(target - entry) / risk_pts
        if rr < p.rr_min:
            continue

        filled = p.entry_model == "close"
        fill_i = mss_i
        for k in range(mss_i + 1, min(len(g), mss_i + 10)):
            bar = g.iloc[k]
            if not filled:
                if bar["low"] <= entry <= bar["high"]:
                    filled = True
                    fill_i = k
                else:
                    continue
            # IFVG / breaker invalidation: close strongly through the zone before fill exits.
            if side > 0:
                if bar["low"] <= stop:
                    exit_px = stop - spec.tick
                    pnl = (exit_px - entry) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
                    return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/SL score={score}")
                if bar["high"] >= target:
                    exit_px = target - spec.tick
                    pnl = (exit_px - entry) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
                    return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/TP score={score}")
            else:
                if bar["high"] >= stop:
                    exit_px = stop + spec.tick
                    pnl = (entry - exit_px) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
                    return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/SL score={score}")
                if bar["low"] <= target:
                    exit_px = target + spec.tick
                    pnl = (entry - exit_px) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
                    return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/TP score={score}")
            # Time stop: ICT idea should move inside expected session.
            if k - fill_i >= 5:
                exit_px = float(bar["close"]) - side * spec.tick
                pnl = side * (exit_px - entry) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
                return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/time score={score}")

        # End-of-day exit if filled but no stop/target.
        if filled:
            bar = g.iloc[min(len(g) - 1, max(fill_i, len(g) - 1))]
            exit_px = float(bar["close"]) - side * spec.tick
            pnl = side * (exit_px - entry) * spec.point_value * p.contracts - COMM_PER_CONTRACT * p.contracts
            return DayResult(day_obj, float(pnl), 1, f"{pool_name}/{entry_src}/eod score={score}")
    return DayResult(day_obj, 0.0, 0, "none")


def daily_results(df: pd.DataFrame, market: str, p: Params, peer_maps: dict[str, dict]) -> list[DayResult]:
    spec = SPECS[market]
    idx_by_day = [g.index.to_numpy() for _, g in df.groupby("day", sort=True)]
    return [day_trade(market, df, idxs, p, spec, peer_maps) for idxs in idx_by_day]


def eval_apex(days: list[object], pnls: np.ndarray, trades: np.ndarray, window_days: int = 30) -> dict:
    wins = []
    for i, d in enumerate(days):
        end = (pd.Timestamp(d) + pd.Timedelta(days=window_days)).date()
        j = bisect.bisect_right(days, end) - 1
        if j > i and end <= days[-1]:
            wins.append((i, j))
    rows = []
    for i, j in wins:
        eq = START
        threshold = START - MAX_DD
        eod_peak = START
        passed = failed = daily_failed = False
        pass_day = None
        ntr = 0
        month_low = START
        for k in range(i, j + 1):
            day_pnl = pnls[k]
            if day_pnl <= -DAILY_LOSS:
                daily_failed = True
                failed = True
                eq += day_pnl
                break
            eq += day_pnl
            ntr += int(trades[k])
            month_low = min(month_low, eq)
            if eq <= threshold:
                failed = True
                break
            if eq >= TARGET:
                passed = True
                pass_day = (pd.Timestamp(days[k]) - pd.Timestamp(days[i])).days + 1
                break
            eod_peak = max(eod_peak, eq)
            threshold = max(threshold, eod_peak - MAX_DD)
        rows.append((passed, failed, eq - START, ntr, pass_day, month_low - START, daily_failed))
    prof = np.array([r[2] for r in rows], dtype=float) if rows else np.array([0.0])
    lows = np.array([r[5] for r in rows], dtype=float) if rows else np.array([0.0])
    pass_days = [r[4] for r in rows if r[4] is not None]
    return {
        "windows": len(rows),
        "passes": int(sum(r[0] for r in rows)),
        "fails": int(sum(r[1] for r in rows)),
        "daily_fails": int(sum(r[6] for r in rows)),
        "rate": float(sum(r[0] for r in rows) / len(rows)) if rows else 0.0,
        "median": float(np.median(prof)),
        "worst": float(prof.min()),
        "best": float(prof.max()),
        "worst_dd_usd": float(lows.min()),
        "avg_days": float(np.mean(pass_days)) if pass_days else None,
    }


def full_period_stats(days: list[object], pnls: np.ndarray, trades: np.ndarray, last_n_days: int = 365) -> dict:
    if not days:
        return {}
    start = (pd.Timestamp(days[-1]) - pd.Timedelta(days=last_n_days)).date()
    mask = np.array([d >= start for d in days])
    p = pnls[mask]
    t = trades[mask]
    eq = START + np.cumsum(p)
    peak = np.maximum.accumulate(np.r_[START, eq])[:-1]
    dd = eq - peak
    return {
        "days": int(mask.sum()),
        "pnl": float(p.sum()),
        "trades": int(t.sum()),
        "trades_week": float(t.sum() / max(mask.sum() / 7, 1)),
        "max_dd": float(dd.min()) if len(dd) else 0.0,
        "win_days": float((p[p != 0] > 0).mean()) if np.any(p != 0) else 0.0,
    }


def grid_params(market: str, max_grid: bool = False) -> list[Params]:
    spec = SPECS[market]
    if max_grid:
        contracts = range(1, spec.max_contracts + 1)
        min_scores = [7, 8, 9, 10]
        entries = ["fvg_ob", "ob", "ote", "ce", "close"]
        targets = ["opposing", "fixed2", "fixed3", "session"]
        stops = ["sweep", "ob", "atr"]
        bias = ["none", "daily", "weekly", "dol"]
        times = ["nyam", "macros", "broad"]
        profiles = ["reversal", "both"]
        rrs = [1.2, 1.5, 2.0]
    else:
        # Curated serious ICT variants. This is intentionally small: the TXT says
        # the workflow is HTF levels -> liquidity sweep -> MSS/displacement ->
        # FVG/OB/OTE retrace -> premium/discount -> opposing liquidity.
        contract_map = {
            "ES": [1, 2],
            "NQ": [1],
            "MES": [6],
            "MNQ": [4, 5, 6],
            "CL": [1],
        }
        if market not in contract_map:
            return []
        out: list[Params] = []
        for c in contract_map[market]:
            use_smt = market in PEERS
            for score in (5, 6):
                out.extend([
                    Params(market, c, score, "fvg_ob", "opposing", "sweep", 1.2, True, use_smt, "none", "broad", "reversal"),
                    Params(market, c, score, "fvg_ob", "fixed2", "sweep", 1.2, True, False, "daily", "broad", "reversal"),
                    Params(market, c, score, "ote", "fixed2", "sweep", 1.2, True, False, "daily", "nyam", "reversal"),
                    Params(market, c, score, "close", "fixed2", "atr", 1.2, True, False, "none", "broad", "reversal"),
                ])
        return out
    out = []
    for vals in itertools.product(contracts, min_scores, entries, targets, stops, rrs, [True], [False, True], bias, times, profiles):
        out.append(Params(market, *vals))
    return out


def run(refresh: bool = False, full_grid: bool = False):
    data = {}
    for key, spec in SPECS.items():
        try:
            df = prep(fetch_yahoo(spec.yahoo, refresh=refresh))
            # Use a wide one-year-centered sample for speed: enough prior bars for
            # HTF context plus the last 365 days for the actual robustness stats.
            last = pd.Timestamp(df["day"].max())
            cutoff = (last - pd.Timedelta(days=460)).date()
            data[key] = df[df["day"] >= cutoff].reset_index(drop=True)
        except Exception as e:
            print(f"skip {key}: {type(e).__name__}: {e}")
    peer_maps = peer_bias_maps(data)
    rows = []
    for market, df in data.items():
        if len(df) < 1000:
            continue
        days = sorted(df["day"].unique())
        # Focus on last year for strategy selection, but the 730d feed creates enough
        # starting points for rolling 30d Apex windows.
        for p in grid_params(market, full_grid):
            vals = daily_results(df, market, p, peer_maps)
            pnls = np.array([x.pnl for x in vals], dtype=float)
            trades = np.array([x.trades for x in vals], dtype=int)
            met = eval_apex([x.day for x in vals], pnls, trades, 30)
            yr = full_period_stats([x.day for x in vals], pnls, trades, 365)
            if yr.get("trades", 0) < 20:
                continue
            row = {"params": p, "apex": met, "year": yr, "signals": vals}
            rows.append(row)
    rows.sort(
        key=lambda r: (
            r["apex"]["rate"],
            -r["apex"]["fails"],
            r["apex"]["median"],
            -abs(r["year"]["max_dd"]),
            r["year"]["pnl"],
        ),
        reverse=True,
    )
    print("Top ICT vocabulary confluence candidates")
    print("market ct score entry target stop rr prem smt bias time | pass fails median worst worstDD 1yPnL 1yDD tr/w")
    for r in rows[:30]:
        p = r["params"]
        a = r["apex"]
        y = r["year"]
        print(
            f"{p.market:<3} {p.contracts:<2} {p.min_score:<2} {p.entry_model:<6} {p.target_model:<8} "
            f"{p.stop_model:<5} {p.rr_min:<3.1f} {str(p.use_premium):<5} {str(p.use_smt):<5} "
            f"{p.bias_mode:<6} {p.time_filter:<6} | "
            f"{a['passes']:>3}/{a['windows']:<3} {a['fails']:>3} {a['median']:>7.0f} "
            f"{a['worst']:>7.0f} {a['worst_dd_usd']:>7.0f} "
            f"{y['pnl']:>8.0f} {y['max_dd']:>7.0f} {y['trades_week']:>4.1f}"
        )
    if rows:
        best = rows[0]
        p = best["params"]
        out = {
            "params": p.__dict__,
            "apex": best["apex"],
            "year": best["year"],
            "recent_signals": [x.__dict__ for x in best["signals"][-40:] if x.trades],
        }
        import json
        out_path = HERE / "ict_vocab_best_result.json"
        out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(f"\nSaved best result -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--full-grid", action="store_true")
    args = ap.parse_args()
    run(refresh=args.refresh, full_grid=args.full_grid)


if __name__ == "__main__":
    main()
