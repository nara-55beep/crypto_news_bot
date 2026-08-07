"""
ICT vocabulary intraday backtest on official Binance USD-M futures data.

This is the redo for true intraday bars: 1m, 5m, and 15m. It uses Binance
public monthly kline ZIP files, not Yahoo.

Important: BTC/ETH/SOL futures are not Apex CME futures. This script answers
"does the ICT vocabulary strategy work on full-year 1m/5m/15m intraday data?"
and applies Apex-like 50K evaluation math as a stress test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from zipfile import ZipFile
from io import BytesIO
import bisect
import itertools
import math
import time

import numpy as np
import pandas as pd
import requests


HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache" / "binance_intraday"
CACHE.mkdir(parents=True, exist_ok=True)
NY = ZoneInfo("America/New_York")

START = 50_000.0
TARGET = 53_000.0
MAX_DD = 2_000.0
DAILY_LOSS = 1_000.0
RISK_USD = 250.0
MAX_NOTIONAL = 250_000.0
TAKER_FEE = 0.0004
SLIP_BPS = 1.0

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = ["1m", "5m", "15m"]


@dataclass(frozen=True)
class Params:
    score: int = 8
    entry_model: str = "fvg_ob"      # fvg_ob, ote, close
    target_model: str = "opposing"   # opposing, fixed2
    rr_min: float = 1.5
    use_premium: bool = True
    time_filter: str = "broad"       # broad, nyam


@dataclass
class Result:
    day: object
    pnl: float
    trades: int
    signal: str = ""


def months(start="2025-06", end="2026-05") -> list[str]:
    out = []
    cur = pd.Timestamp(start + "-01")
    stop = pd.Timestamp(end + "-01")
    while cur <= stop:
        out.append(cur.strftime("%Y-%m"))
        cur += pd.DateOffset(months=1)
    return out


def monthly_url(symbol: str, interval: str, ym: str) -> str:
    return (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"
    )


def load_symbol_interval(symbol: str, interval: str, refresh=False) -> pd.DataFrame:
    out_path = CACHE / f"{symbol}_{interval}_2025-06_2026-05.csv"
    if out_path.exists() and not refresh:
        df = pd.read_csv(out_path, parse_dates=["dt"])
        df["dt"] = pd.to_datetime(df["dt"], utc=True)
        return finish_df(df)

    one_path = CACHE / f"{symbol}_1m_2025-06_2026-05.csv"
    if interval != "1m" and one_path.exists() and not refresh:
        one = pd.read_csv(one_path, parse_dates=["dt"])
        one["dt"] = pd.to_datetime(one["dt"], utc=True)
        df = resample_ohlcv(one, interval)
        df.to_csv(out_path, index=False)
        return finish_df(df)

    frames = []
    for ym in months():
        url = monthly_url(symbol, interval, ym)
        print(f"download {symbol} {interval} {ym}")
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            print(f"  skip HTTP {r.status_code}")
            continue
        with ZipFile(BytesIO(r.content)) as zf:
            name = zf.namelist()[0]
            raw = pd.read_csv(zf.open(name), header=None)
        if raw.iloc[0, 0] == "open_time":
            raw = raw.iloc[1:].reset_index(drop=True)
        raw = raw.iloc[:, :6]
        raw.columns = ["open_time", "open", "high", "low", "close", "volume"]
        raw["dt"] = pd.to_datetime(pd.to_numeric(raw["open_time"]), unit="ms", utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            raw[c] = pd.to_numeric(raw[c], errors="coerce")
        frames.append(raw[["dt", "open", "high", "low", "close", "volume"]].dropna())
        time.sleep(0.15)
    if not frames:
        raise RuntimeError(f"no data for {symbol} {interval}")
    df = pd.concat(frames, ignore_index=True).drop_duplicates("dt").sort_values("dt")
    df.to_csv(out_path, index=False)
    return finish_df(df)


def resample_ohlcv(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule = {"5m": "5min", "15m": "15min"}.get(interval)
    if not rule:
        return df.copy()
    d = df.copy().sort_values("dt").set_index("dt")
    out = d.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna(subset=["open", "high", "low", "close"]).reset_index()
    return out


def finish_df(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ny"] = d["dt"].dt.tz_convert(NY)
    d["day"] = d["ny"].dt.date
    d["week"] = d["ny"].dt.strftime("%G-%V")
    d["month"] = d["ny"].dt.strftime("%Y-%m")
    d["minute"] = d["ny"].dt.hour * 60 + d["ny"].dt.minute
    pc = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - pc).abs(),
        (d["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    daily = d.groupby("day").agg(
        pdh=("high", "max"), pdl=("low", "min"), pdo=("open", "first"),
    ).shift(1)
    weekly = d.groupby("week").agg(
        pwh=("high", "max"), pwl=("low", "min"), pwo=("open", "first"),
    ).shift(1)
    monthly = d.groupby("month").agg(
        pmh=("high", "max"), pml=("low", "min"), pmo=("open", "first"),
    ).shift(1)
    d = d.merge(daily, left_on="day", right_index=True, how="left")
    d = d.merge(weekly, left_on="week", right_index=True, how="left")
    d = d.merge(monthly, left_on="month", right_index=True, how="left")
    return d.reset_index(drop=True)


def in_time(minute: int, mode: str) -> bool:
    nyam = 8 * 60 + 30 <= minute < 11 * 60 + 30
    london = 2 * 60 <= minute < 5 * 60
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
    return nyam if mode == "nyam" else (london or nyam or nypm or macros)


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


def make_static_context(hist: pd.DataFrame, px: float):
    base = hist.tail(1500).reset_index(drop=True)
    hi, lo = swing_points(base, 2)
    tol = max(px * 0.0008, 0.01)
    h = base["high"].to_numpy(float); l = base["low"].to_numpy(float)
    eqh, eql = [], []
    for a, b in itertools.combinations(hi[-32:], 2):
        p = (h[a] + h[b]) / 2
        if abs(h[a] - h[b]) <= tol and all(abs(p - x) > tol for x in eqh):
            eqh.append(float(p))
    for a, b in itertools.combinations(lo[-32:], 2):
        p = (l[a] + l[b]) / 2
        if abs(l[a] - l[b]) <= tol and all(abs(p - x) > tol for x in eql):
            eql.append(float(p))
    old_h = [float(h[idx]) for idx in hi[-6:]]
    old_l = [float(l[idx]) for idx in lo[-6:]]
    return eqh[-6:], eql[-6:], old_h, old_l


def context_pools(day: pd.DataFrame, i: int, static_ctx) -> list[tuple[str, int, float, int]]:
    row = day.iloc[i]
    pools: list[tuple[str, int, float, int]] = []

    def add(name, side, price, prio):
        if price is not None and np.isfinite(price) and price > 0:
            pools.append((name, side, float(price), prio))

    add("PDH", 1, row.get("pdh"), 5); add("PDL", -1, row.get("pdl"), 5)
    add("PWH", 1, row.get("pwh"), 4); add("PWL", -1, row.get("pwl"), 4)
    add("PMH", 1, row.get("pmh"), 3); add("PML", -1, row.get("pml"), 3)
    add("HOD", 1, day.iloc[: i + 1]["high"].max(), 2)
    add("LOD", -1, day.iloc[: i + 1]["low"].min(), 2)
    m = day.iloc[: i + 1]["minute"]
    for name, mask, prio in [
        ("asia", (m >= 18 * 60) | (m < 2 * 60), 3),
        ("london", (m >= 2 * 60) & (m < 5 * 60), 3),
        ("nyam", (m >= 8 * 60 + 30) & (m < 11 * 60 + 30), 2),
    ]:
        s = day.iloc[: i + 1][mask]
        if len(s) >= 2:
            add(f"{name}_h", 1, s["high"].max(), prio)
            add(f"{name}_l", -1, s["low"].min(), prio)

    eqh, eql, old_h, old_l = static_ctx
    for p in eqh: add("EQH", 1, p, 3)
    for p in eql: add("EQL", -1, p, 3)
    for p in old_h: add("old high", 1, p, 2)
    for p in old_l: add("old low", -1, p, 2)

    dedup = []
    for p in sorted(pools, key=lambda x: (-x[3], x[0])):
        t = max(p[2] * 0.0002, 0.01)
        if not any(abs(p[2] - q[2]) <= t and p[1] == q[1] for q in dedup):
            dedup.append(p)
    return dedup


def fvg(day: pd.DataFrame, start: int, end: int, side: int):
    for i in range(max(2, start), min(len(day), end + 1)):
        a = day.iloc[i]["atr"]
        a = a if np.isfinite(a) and a > 0 else max(day.iloc[i]["high"] - day.iloc[i]["low"], 1.0)
        if side > 0 and day.iloc[i]["low"] > day.iloc[i - 2]["high"] + 0.03 * a:
            top = float(day.iloc[i]["low"]); bot = float(day.iloc[i - 2]["high"])
            return top, bot, (top + bot) / 2
        if side < 0 and day.iloc[i]["high"] < day.iloc[i - 2]["low"] - 0.03 * a:
            top = float(day.iloc[i - 2]["low"]); bot = float(day.iloc[i]["high"])
            return top, bot, (top + bot) / 2
    return None


def ob(day: pd.DataFrame, start: int, end: int, side: int):
    for i in range(end - 1, max(start - 1, 0), -1):
        c = day.iloc[i]
        if side > 0 and c["close"] < c["open"]:
            return float(c["high"]), float(c["low"]), (float(c["high"]) + float(c["low"])) / 2
        if side < 0 and c["close"] > c["open"]:
            return float(c["high"]), float(c["low"]), (float(c["high"]) + float(c["low"])) / 2
    return None


def fee(qty: float, entry: float, exit_px: float) -> float:
    return (abs(qty) * entry + abs(qty) * exit_px) * TAKER_FEE


def trade_day(df: pd.DataFrame, idxs: np.ndarray, p: Params) -> Result:
    day = df.iloc[idxs].reset_index(drop=True)
    if len(day) < 30:
        return Result(day.iloc[0]["day"] if len(day) else None, 0.0, 0, "short")
    hist = df.iloc[max(0, idxs[0] - 1500):idxs[0]].copy()
    if len(hist) < 300:
        return Result(day.iloc[0]["day"], 0.0, 0, "nohist")
    swings_h, swings_l = swing_points(day, 2)
    static_ctx = make_static_context(hist, float(day.iloc[0]["close"]))
    day_obj = day.iloc[0]["day"]
    eq_low = np.nanmin([day.iloc[0].get("pdl", np.nan), day.iloc[0].get("pwl", np.nan), day.iloc[0].get("pml", np.nan)])
    eq_high = np.nanmax([day.iloc[0].get("pdh", np.nan), day.iloc[0].get("pwh", np.nan), day.iloc[0].get("pmh", np.nan)])
    eq_mid = (eq_high + eq_low) / 2 if np.isfinite(eq_high) and np.isfinite(eq_low) and eq_high > eq_low else None

    for i in range(5, len(day) - 5):
        row = day.iloc[i]
        if not in_time(int(row["minute"]), p.time_filter):
            continue
        a = float(row["atr"]) if np.isfinite(row["atr"]) and row["atr"] > 0 else float(row["high"] - row["low"])
        if a <= 0:
            continue
        pools = context_pools(day, i, static_ctx)
        px = float(row["close"])
        pen = max(a * 0.05, px * 0.00006)
        swept = None
        for name, liq_side, level, prio in pools:
            if abs(level - px) / px > 0.025:
                continue
            if liq_side < 0 and row["low"] < level - pen and row["close"] > level:
                swept = (name, 1, level, prio, float(row["low"]))
                break
            if liq_side > 0 and row["high"] > level + pen and row["close"] < level:
                swept = (name, -1, level, prio, float(row["high"]))
                break
        if not swept:
            continue
        pool_name, side, pool_px, pool_prio, raid = swept
        prior = [x for x in (swings_h if side > 0 else swings_l) if max(0, i - 30) <= x < i]
        if prior:
            mss = float(day.iloc[prior[-1]]["high" if side > 0 else "low"])
        else:
            look = day.iloc[max(0, i - 12):i]
            mss = float(look["high"].max() if side > 0 else look["low"].min())

        mss_i = None
        for j in range(i + 1, min(len(day), i + 20)):
            c = day.iloc[j]
            broke = c["close"] > mss if side > 0 else c["close"] < mss
            if not broke:
                continue
            rng = max(float(c["high"] - c["low"]), 1e-9)
            body = abs(float(c["close"] - c["open"]))
            tr = max(float(c["high"] - c["low"]), abs(float(c["high"] - day.iloc[j - 1]["close"])), abs(float(c["low"] - day.iloc[j - 1]["close"])))
            if tr >= 0.65 * a or body / rng >= 0.55:
                mss_i = j
                break
        if mss_i is None:
            continue

        fv = fvg(day, i, mss_i, side)
        block = ob(day, i, mss_i, side)
        if p.entry_model == "close":
            entry = float(day.iloc[mss_i]["close"]); src = "MSS"
        elif p.entry_model == "ote":
            impulse = float(day.iloc[mss_i]["close"]) - raid
            entry = raid + 0.705 * impulse; src = "OTE"
        else:
            zone = fv or block
            if not zone:
                continue
            entry = zone[2]; src = "FVG" if fv else "OB"
        if p.use_premium and eq_mid is not None:
            if side > 0 and entry >= eq_mid:
                continue
            if side < 0 and entry <= eq_mid:
                continue
        stop = raid - side * 0.08 * a
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        score = pool_prio + 2 + 2 + (1 if fv else 0) + (1 if block else 0) + (1 if p.use_premium else 0)
        if score < p.score:
            continue
        above = sorted([x[2] for x in pools if x[1] > 0 and x[2] > entry])
        below = sorted([x[2] for x in pools if x[1] < 0 and x[2] < entry], reverse=True)
        if p.target_model == "opposing":
            target = above[0] if side > 0 and above else below[0] if side < 0 and below else entry + side * 2 * risk
        else:
            target = entry + side * 2 * risk
        if abs(target - entry) / risk < p.rr_min:
            continue
        qty = min(RISK_USD / risk, MAX_NOTIONAL / entry)
        filled = p.entry_model == "close"
        fill_i = mss_i
        for k in range(mss_i + 1, min(len(day), mss_i + 40)):
            bar = day.iloc[k]
            if not filled:
                if bar["low"] <= entry <= bar["high"]:
                    filled = True; fill_i = k
                else:
                    continue
            slip = entry * SLIP_BPS / 10000.0
            if side > 0:
                if bar["low"] <= stop:
                    ex = stop - slip
                    pnl = qty * (ex - entry) - fee(qty, entry, ex)
                    return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/SL")
                if bar["high"] >= target:
                    ex = target - slip
                    pnl = qty * (ex - entry) - fee(qty, entry, ex)
                    return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/TP")
            else:
                if bar["high"] >= stop:
                    ex = stop + slip
                    pnl = qty * (entry - ex) - fee(qty, entry, ex)
                    return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/SL")
                if bar["low"] <= target:
                    ex = target + slip
                    pnl = qty * (entry - ex) - fee(qty, entry, ex)
                    return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/TP")
            if k - fill_i >= 20:
                ex = float(bar["close"]) - side * slip
                pnl = side * qty * (ex - entry) - fee(qty, entry, ex)
                return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/time")
        if filled:
            bar = day.iloc[min(len(day) - 1, fill_i + 40)]
            ex = float(bar["close"]) - side * entry * SLIP_BPS / 10000.0
            pnl = side * qty * (ex - entry) - fee(qty, entry, ex)
            return Result(day_obj, float(pnl), 1, f"{pool_name}/{src}/eod")
    return Result(day_obj, 0.0, 0, "none")


def eval_apex(days, pnls, trades, window_days=30):
    wins = []
    for i, d in enumerate(days):
        end = (pd.Timestamp(d) + pd.Timedelta(days=window_days)).date()
        j = bisect.bisect_right(days, end) - 1
        if j > i and end <= days[-1]:
            wins.append((i, j))
    rows = []
    for i, j in wins:
        eq = START; peak = START; threshold = START - MAX_DD
        passed = failed = False; ntr = 0; min_eq = START
        for k in range(i, j + 1):
            if pnls[k] <= -DAILY_LOSS:
                failed = True; eq += pnls[k]; break
            eq += pnls[k]; ntr += int(trades[k]); min_eq = min(min_eq, eq)
            if eq <= threshold:
                failed = True; break
            if eq >= TARGET:
                passed = True; break
            peak = max(peak, eq); threshold = max(threshold, peak - MAX_DD)
        rows.append((passed, failed, eq - START, ntr, min_eq - START))
    prof = np.array([r[2] for r in rows], float) if rows else np.array([0.0])
    lows = np.array([r[4] for r in rows], float) if rows else np.array([0.0])
    return {
        "windows": len(rows), "passes": int(sum(r[0] for r in rows)),
        "fails": int(sum(r[1] for r in rows)),
        "rate": float(sum(r[0] for r in rows) / len(rows)) if rows else 0.0,
        "median": float(np.median(prof)), "worst": float(prof.min()),
        "best": float(prof.max()), "worst_dd": float(lows.min()),
    }


def one_year_stats(days, pnls, trades):
    p = np.array(pnls, float); t = np.array(trades, int)
    eq = START + np.cumsum(p)
    peak = np.maximum.accumulate(np.r_[START, eq])[:-1]
    dd = eq - peak
    active = p[p != 0]
    return {
        "pnl": float(p.sum()), "trades": int(t.sum()),
        "trades_week": float(t.sum() / max(len(days) / 7, 1)),
        "max_dd": float(dd.min()) if len(dd) else 0.0,
        "win_rate": float((active > 0).mean()) if len(active) else 0.0,
    }


def run(refresh=False, symbols=None, intervals=None):
    symbols = symbols or SYMBOLS
    intervals = intervals or INTERVALS
    params = [
        Params(8, "fvg_ob", "opposing", 1.5, True, "broad"),
        Params(7, "fvg_ob", "fixed2", 1.2, True, "broad"),
        Params(7, "ote", "fixed2", 1.2, True, "nyam"),
        Params(6, "close", "fixed2", 1.2, True, "broad"),
    ]
    rows = []
    for sym in symbols:
        for interval in intervals:
            df = load_symbol_interval(sym, interval, refresh=refresh)
            idx_by_day = [g.index.to_numpy() for _, g in df.groupby("day", sort=True)]
            for p in params:
                vals = [trade_day(df, idxs, p) for idxs in idx_by_day]
                days = [x.day for x in vals]
                pnls = np.array([x.pnl for x in vals], float)
                trades = np.array([x.trades for x in vals], int)
                yr = one_year_stats(days, pnls, trades)
                if yr["trades"] < 20:
                    continue
                ap = eval_apex(days, pnls, trades)
                rows.append({"symbol": sym, "interval": interval, "params": p, "apex": ap, "year": yr, "signals": vals})
                print(sym, interval, p, "passes", ap["passes"], "/", ap["windows"], "pnl", round(yr["pnl"], 2), "trades", yr["trades"])
    rows.sort(key=lambda r: (r["apex"]["rate"], -r["apex"]["fails"], r["apex"]["median"], r["year"]["pnl"]), reverse=True)
    print("\nTop Binance intraday ICT candidates")
    print("symbol tf score entry target time | pass fails median worst 1yPnl 1yDD trades/w win")
    for r in rows[:30]:
        p = r["params"]; a = r["apex"]; y = r["year"]
        print(
            f"{r['symbol']:<7} {r['interval']:<3} {p.score:<2} {p.entry_model:<6} {p.target_model:<8} {p.time_filter:<5} | "
            f"{a['passes']:>3}/{a['windows']:<3} {a['fails']:>3} {a['median']:>7.0f} {a['worst']:>7.0f} "
            f"{y['pnl']:>8.0f} {y['max_dd']:>7.0f} {y['trades_week']:>4.1f} {y['win_rate']*100:>4.1f}%"
        )
    if rows:
        import json
        best = rows[0]
        out = {
            "symbol": best["symbol"], "interval": best["interval"],
            "params": best["params"].__dict__, "apex": best["apex"], "year": best["year"],
            "recent_signals": [x.__dict__ for x in best["signals"][-50:] if x.trades],
        }
        (HERE / "ict_intraday_binance_best.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--intervals", default=",".join(INTERVALS))
    args = ap.parse_args()
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]
    run(refresh=args.refresh, symbols=symbols, intervals=intervals)


if __name__ == "__main__":
    main()
