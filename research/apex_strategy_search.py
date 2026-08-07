"""
Search for Apex-style manual signal strategies.

This is research-only. It does not trade. It answers one hard question:

    On rolling 30-calendar-day windows, can a strategy reach +$3,000 on a
    $50K Apex EOD-style account without breaching a $2,000 trailing drawdown?

Data:
    Yahoo chart API, 1h futures candles, regular-hours subset. Yahoo is not
    institutional data, but it gives a longer free sample than 5m candles.

Important:
    This is not a guarantee and not an automation tool. Apex trade execution
    would still be manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
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


@dataclass(frozen=True)
class Spec:
    key: str
    yahoo: str
    point_value: float
    tick: float
    max_contracts: int


@dataclass(frozen=True)
class Candidate:
    market: str
    peer: str
    signal: str
    contracts: int
    stop_atr: float
    rr: float
    trend: str = "none"
    peer_filter: str = "none"
    gap_min_atr: float = 0.0
    atr_max_pct: float = 1.0


SPECS = {
    "ES": Spec("ES", "ES=F", 50.0, 0.25, 2),
    "NQ": Spec("NQ", "NQ=F", 20.0, 0.25, 1),
    "MES": Spec("MES", "MES=F", 5.0, 0.25, 6),
    "MNQ": Spec("MNQ", "MNQ=F", 2.0, 0.25, 6),
    "BTC": Spec("BTC", "BTC=F", 5.0, 5.0, 1),
}


def cache_path(symbol: str) -> Path:
    return CACHE / f"apex_search_{symbol.replace('=', '_')}_1h_730d.csv"


def fetch_yahoo(symbol: str, refresh: bool = False) -> pd.DataFrame:
    path = cache_path(symbol)
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["dt", "ny"])
        df["day"] = pd.to_datetime(df["day"]).dt.date
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
    df["hour"] = df["ny"].dt.hour
    # 09:00 is kept because Yahoo's hourly bars are anchored at 09:00 and include
    # the 09:30-10:00 cash-open segment. 16:00 is kept as the closing hour.
    df = df[(df["hour"] >= 9) & (df["hour"] <= 16)].copy().reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
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
    d["atr_pct"] = (d["atr"] / d["close"]).rolling(60, min_periods=20).rank(pct=True)
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    daily = d.groupby("day").agg(
        prev_hi=("high", "max"),
        prev_lo=("low", "min"),
        prev_close=("close", "last"),
        prev_open=("open", "first"),
    )
    daily = daily.shift(1)
    return d.merge(daily, left_on="day", right_index=True, how="left")


def build_days(df: pd.DataFrame) -> list[dict]:
    out: list[dict] = []
    for day, g0 in df.groupby("day", sort=True):
        g = g0.reset_index(drop=True)
        out.append(
            {
                "day": day,
                "dt": g["dt"].to_numpy(),
                "open": g["open"].to_numpy(dtype=float),
                "high": g["high"].to_numpy(dtype=float),
                "low": g["low"].to_numpy(dtype=float),
                "close": g["close"].to_numpy(dtype=float),
                "atr": g["atr"].to_numpy(dtype=float),
                "atr_pct": g["atr_pct"].to_numpy(dtype=float),
                "ema20": g["ema20"].to_numpy(dtype=float),
                "ema50": g["ema50"].to_numpy(dtype=float),
                "prev_hi": g["prev_hi"].to_numpy(dtype=float),
                "prev_lo": g["prev_lo"].to_numpy(dtype=float),
                "prev_close": g["prev_close"].to_numpy(dtype=float),
            }
        )
    return out


def build_peer_map(days: list[dict]) -> dict[np.datetime64, int]:
    out: dict[np.datetime64, int] = {}
    for d in days:
        for dt, close, ema20 in zip(d["dt"], d["close"], d["ema20"]):
            out[dt] = 1 if close > ema20 else (-1 if close < ema20 else 0)
    return out


def trend_ok(day: dict, i: int, side: int, mode: str) -> bool:
    if mode == "none":
        return True
    c = day["close"][i]
    e20 = day["ema20"][i]
    e50 = day["ema50"][i]
    if mode == "ema20":
        return (c > e20) if side > 0 else (c < e20)
    if mode == "ema_stack":
        return (c > e20 > e50) if side > 0 else (c < e20 < e50)
    return True


def peer_ok(day: dict, i: int, side: int, peer_map: dict[np.datetime64, int], mode: str) -> bool:
    if mode == "none":
        return True
    p = peer_map.get(day["dt"][i], 0)
    if mode == "same":
        return p in (0, side)
    if mode == "strict":
        return p == side
    return True


def get_signal(day: dict, cand: Candidate, peer_map: dict[np.datetime64, int]) -> tuple[int, int] | None:
    n = len(day["open"])
    if n < 4:
        return None

    first_side = 1 if day["close"][0] > day["open"][0] else -1
    second_side = 1 if day["close"][1] > day["open"][0] else -1
    gap = 0.0
    if np.isfinite(day["prev_close"][0]) and day["atr"][0] > 0:
        gap = (day["open"][0] - day["prev_close"][0]) / day["atr"][0]

    if cand.gap_min_atr and abs(gap) < cand.gap_min_atr:
        return None

    def finish(i: int, side: int) -> tuple[int, int] | None:
        if not np.isfinite(day["atr"][i]) or day["atr"][i] <= 0:
            return None
        atrp = day["atr_pct"][i]
        if np.isfinite(atrp) and atrp > cand.atr_max_pct:
            return None
        if trend_ok(day, i, side, cand.trend) and peer_ok(day, i, side, peer_map, cand.peer_filter):
            return i, side
        return None

    if cand.signal == "first_mom":
        return finish(1, first_side)
    if cand.signal == "first_fade":
        return finish(1, -first_side)
    if cand.signal == "two_mom":
        return finish(2, second_side)
    if cand.signal == "two_fade":
        return finish(2, -second_side)
    if cand.signal == "gap_mom":
        if gap == 0:
            return None
        return finish(1, 1 if gap > 0 else -1)
    if cand.signal == "gap_fade":
        if gap == 0:
            return None
        return finish(1, -1 if gap > 0 else 1)
    if cand.signal == "orb_break":
        hi = np.nanmax(day["high"][:2])
        lo = np.nanmin(day["low"][:2])
        for i in range(2, n - 1):
            if day["close"][i] > hi:
                got = finish(i, 1)
                if got:
                    return got
            if day["close"][i] < lo:
                got = finish(i, -1)
                if got:
                    return got
        return None
    if cand.signal == "orb_fade":
        hi = np.nanmax(day["high"][:2])
        lo = np.nanmin(day["low"][:2])
        for i in range(2, n - 1):
            if day["high"][i] > hi and day["close"][i] < hi:
                got = finish(i, -1)
                if got:
                    return got
            if day["low"][i] < lo and day["close"][i] > lo:
                got = finish(i, 1)
                if got:
                    return got
        return None
    if cand.signal == "prior_break":
        for i in range(1, n - 1):
            if np.isfinite(day["prev_hi"][i]) and day["close"][i] > day["prev_hi"][i]:
                got = finish(i, 1)
                if got:
                    return got
            if np.isfinite(day["prev_lo"][i]) and day["close"][i] < day["prev_lo"][i]:
                got = finish(i, -1)
                if got:
                    return got
        return None
    if cand.signal == "ema_reclaim":
        for i in range(1, n - 1):
            if day["close"][i - 1] <= day["ema20"][i - 1] and day["close"][i] > day["ema20"][i]:
                got = finish(i, 1)
                if got:
                    return got
            if day["close"][i - 1] >= day["ema20"][i - 1] and day["close"][i] < day["ema20"][i]:
                got = finish(i, -1)
                if got:
                    return got
        return None
    return None


def trade_pnl(day: dict, signal: tuple[int, int] | None, spec: Spec, cand: Candidate) -> tuple[float, int]:
    if signal is None:
        return 0.0, 0
    i, side = signal
    entry_i = i + 1
    if entry_i >= len(day["open"]):
        return 0.0, 0
    a = day["atr"][i]
    if not np.isfinite(a) or a <= 0:
        return 0.0, 0
    entry = day["open"][entry_i] + side * spec.tick
    dist = cand.stop_atr * a
    risk = dist * spec.point_value * cand.contracts
    if risk <= 0 or risk > DAILY_LOSS * 0.95:
        return 0.0, 0

    stop = entry - side * dist
    target = None if cand.rr == 0 else entry + side * cand.rr * dist
    exit_px = day["close"][-1]
    reason = "eod"
    for j in range(entry_i, len(day["open"])):
        hi = day["high"][j]
        lo = day["low"][j]
        cl = day["close"][j]
        if side > 0:
            if lo <= stop:
                exit_px = stop - spec.tick
                reason = "stop"
                break
            if target is not None and hi >= target:
                exit_px = target - spec.tick
                reason = "target"
                break
        else:
            if hi >= stop:
                exit_px = stop + spec.tick
                reason = "stop"
                break
            if target is not None and lo <= target:
                exit_px = target + spec.tick
                reason = "target"
                break
        exit_px = cl

    if reason == "eod":
        exit_px -= side * spec.tick
    pnl = side * (exit_px - entry) * spec.point_value * cand.contracts
    pnl -= 5.0 * cand.contracts
    return float(pnl), 1


def window_indexes(days: list[object]) -> list[tuple[int, int]]:
    wins = []
    for i, d in enumerate(days):
        end = (pd.Timestamp(d) + pd.Timedelta(days=30)).date()
        j = bisect.bisect_right(days, end) - 1
        if j > i and end <= days[-1]:
            wins.append((i, j))
    return wins


def roll_eval(days: list[object], windows: list[tuple[int, int]], pnls: np.ndarray, trades: np.ndarray) -> dict:
    rows = []
    for i, j in windows:
        eq = START
        threshold = START - MAX_DD
        eod_peak = START
        passed = False
        failed = False
        ntr = 0
        pass_day = None
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

    profits = np.array([r[2] for r in rows], dtype=float)
    pass_days = [r[4] for r in rows if r[4] is not None]
    return {
        "windows": len(rows),
        "passes": int(sum(r[0] for r in rows)),
        "fails": int(sum(r[1] for r in rows)),
        "pass_rate": float(sum(r[0] for r in rows) / len(rows)) if rows else 0.0,
        "median_profit": float(np.median(profits)) if len(profits) else 0.0,
        "worst_profit": float(profits.min()) if len(profits) else 0.0,
        "best_profit": float(profits.max()) if len(profits) else 0.0,
        "avg_pass_days": float(np.mean(pass_days)) if pass_days else None,
    }


def candidates() -> list[Candidate]:
    out: list[Candidate] = []
    signals = [
        "first_mom",
        "first_fade",
        "two_mom",
        "two_fade",
        "gap_mom",
        "gap_fade",
        "orb_break",
        "orb_fade",
        "prior_break",
        "ema_reclaim",
    ]
    trend_modes = ["none", "ema20"]
    peer_modes = ["none", "same"]
    for market, peer in [("ES", "NQ"), ("NQ", "ES"), ("BTC", "ES")]:
        spec = SPECS[market]
        for signal, contracts, stop_atr, rr, trend, peer_filter, atr_max_pct in itertools.product(
            signals,
            range(1, spec.max_contracts + 1),
            [0.25, 0.4, 0.6, 0.8],
            [0.0, 1.5, 2.0, 2.5, 3.0],
            trend_modes,
            peer_modes,
            [1.0],
        ):
            # Keep the grid finite and realistic: full NQ/BTC should not pyramid
            # inside a $2K drawdown account.
            if market in ("NQ", "BTC") and contracts > 1:
                continue
            out.append(
                Candidate(
                    market=market,
                    peer=peer,
                    signal=signal,
                    contracts=contracts,
                    stop_atr=stop_atr,
                    rr=rr,
                    trend=trend,
                    peer_filter=peer_filter,
                    gap_min_atr=0.0,
                    atr_max_pct=atr_max_pct,
                )
            )
    return out


def main(refresh: bool = False) -> None:
    raw = {k: prepare(fetch_yahoo(spec.yahoo, refresh=refresh)) for k, spec in SPECS.items()}
    days_by_market = {k: build_days(df) for k, df in raw.items()}
    peer_maps = {k: build_peer_map(days) for k, days in days_by_market.items()}

    print("Data coverage", flush=True)
    for k, df in raw.items():
        print(f"  {k}: {df['ny'].iloc[0]} -> {df['ny'].iloc[-1]} | {len(df)} RTH-ish 1h bars | {df['day'].nunique()} days", flush=True)

    all_cands = candidates()
    results = []
    started = time.time()
    for idx, cand in enumerate(all_cands, 1):
        spec = SPECS[cand.market]
        day_list = days_by_market[cand.market]
        days = [d["day"] for d in day_list]
        wins = window_indexes(days)
        peer_map = peer_maps[cand.peer]

        vals = [trade_pnl(day, get_signal(day, cand, peer_map), spec, cand) for day in day_list]
        pnls = np.array([v[0] for v in vals], dtype=float)
        trades = np.array([v[1] for v in vals], dtype=int)
        m = roll_eval(days, wins, pnls, trades)
        m["candidate"] = cand
        m["total_trades"] = int(trades.sum())
        results.append(m)
        if idx % 2000 == 0:
            print(f"  tested {idx}/{len(all_cands)} in {time.time() - started:.1f}s", flush=True)

    results.sort(
        key=lambda m: (
            m["pass_rate"],
            -m["fails"],
            m["median_profit"],
            m["best_profit"],
        ),
        reverse=True,
    )

    print("\nTop candidates by rolling 30-day Apex pass rate", flush=True)
    for m in results[:30]:
        c: Candidate = m["candidate"]
        avg_days = "-" if m["avg_pass_days"] is None else f"{m['avg_pass_days']:.1f}"
        print(
            f"{c.market:<3} {c.signal:<11} ct={c.contracts} stop={c.stop_atr:<4} rr={c.rr:<3} "
            f"trend={c.trend:<9} peer={c.peer_filter:<4} atrMax={c.atr_max_pct:<3} | "
            f"pass {m['passes']:>3}/{m['windows']} ({m['pass_rate']*100:>5.1f}%) "
            f"ddFail {m['fails']:>3} med ${m['median_profit']:>7.0f} "
            f"worst ${m['worst_profit']:>7.0f} best ${m['best_profit']:>7.0f} "
            f"avgPassDay {avg_days} trades {m['total_trades']}",
            flush=True,
        )


if __name__ == "__main__":
    main(refresh=False)
