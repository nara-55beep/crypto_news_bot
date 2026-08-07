"""
apex_vwap_orb_bot.py

Research-only backtest for the Apex-style manual strategy:

NY Open VWAP + Opening Range Pullback Continuation

Rules:
  - Instruments: MES=F and MNQ=F, using Yahoo intraday futures data.
  - Session: regular US cash session only, 09:30-16:00 New York time.
  - Opening range: first 15 minutes.
  - Direction: trade only after price breaks the opening range and agrees with VWAP,
    EMA9/EMA20, and the paired index future.
  - Entry: wait for a pullback into OR high/low, VWAP, EMA9, or EMA20, then a
    rejection candle. Enter next bar open.
  - Stop: beyond the rejection candle, constrained by ATR so risk is not nonsense.
  - Exit: partial at +1R if enough contracts, trail runner, force flat by 15:55 NY.
  - Risk: 50K Apex-style account, 150-200 USD/trade, max 2 trades/day,
    max 400 USD daily loss, max 6 micro contracts.

This is not intended for automated Apex execution. Apex rules currently prohibit
automation; this is a tester and learning tool.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo
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


@dataclass(frozen=True)
class MarketSpec:
    symbol: str
    label: str
    point_value: float
    tick: float = 0.25


@dataclass(frozen=True)
class Params:
    interval: str = "5m"
    yahoo_range: str = "60d"
    start_equity: float = 50_000.0
    risk_usd: float = 200.0
    max_daily_loss: float = 400.0
    max_contracts: int = 6
    max_trades_day: int = 2
    or_minutes: int = 15
    trade_end: str = "11:30"
    flat_time: str = "15:55"
    min_stop_atr: float = 0.35
    max_stop_atr: float = 1.40
    pullback_tol_atr: float = 0.15
    rr1: float = 1.0
    rr_final: float = 2.5
    trail_r: float = 1.0
    slip_ticks: float = 1.0
    commission_rt: float = 1.24
    require_peer: bool = True


MARKETS = {
    "MES": MarketSpec("MES=F", "Micro E-mini S&P 500", 5.0),
    "MNQ": MarketSpec("MNQ=F", "Micro E-mini Nasdaq-100", 2.0),
}


def _cache_name(symbol: str, interval: str, rng: str) -> Path:
    safe = symbol.replace("=", "_").replace("^", "").replace("/", "_")
    return CACHE / f"apex_{safe}_{interval}_{rng}.csv"


def fetch_yahoo(symbol: str, interval: str, rng: str, refresh: bool = False) -> pd.DataFrame:
    path = _cache_name(symbol, interval, rng)
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["dt_utc", "dt_ny"])

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "range": rng,
        "interval": interval,
        "includePrePost": "true",
    }
    r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
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
    df.to_csv(path, index=False)
    return df


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
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


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _minute_of_day(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def _rth(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    mins = d["dt_ny"].map(_minute_of_day)
    d = d[(mins >= 9 * 60 + 30) & (mins <= 16 * 60)].copy()
    d["day"] = d["dt_ny"].dt.date
    d = d.reset_index(drop=True)
    return d


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = _rth(df)
    d["ema9"] = ema(d["close"], 9)
    d["ema20"] = ema(d["close"], 20)
    d["atr14"] = atr(d, 14)
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    d["pv"] = tp * d["volume"].fillna(0)
    d["cum_pv"] = d.groupby("day")["pv"].cumsum()
    d["cum_v"] = d.groupby("day")["volume"].transform(lambda x: x.fillna(0).cumsum())
    d["vwap"] = d["cum_pv"] / d["cum_v"].replace(0, np.nan)
    return d.drop(columns=["pv", "cum_pv", "cum_v"])


def _peer_ok(peer_row: pd.Series | None, side: int, require_peer: bool) -> bool:
    if not require_peer or peer_row is None:
        return True
    if side > 0:
        return (
            peer_row["close"] > peer_row["vwap"]
            and peer_row["ema9"] >= peer_row["ema20"]
        )
    return (
        peer_row["close"] < peer_row["vwap"]
        and peer_row["ema9"] <= peer_row["ema20"]
    )


def _row_by_time(df: pd.DataFrame) -> dict[pd.Timestamp, pd.Series]:
    return {row["dt_utc"]: row for _, row in df.iterrows()}


def _get_peer(peer_map: dict[pd.Timestamp, pd.Series], ts: pd.Timestamp) -> pd.Series | None:
    return peer_map.get(ts)


def _levels(row: pd.Series, or_hi: float, or_lo: float, side: int) -> list[float]:
    if side > 0:
        return [or_hi, row["vwap"], row["ema9"], row["ema20"]]
    return [or_lo, row["vwap"], row["ema9"], row["ema20"]]


def _rejection(row: pd.Series, side: int) -> bool:
    rng = max(float(row["high"] - row["low"]), 1e-9)
    close_pos = (float(row["close"] - row["low"]) / rng)
    if side > 0:
        return row["close"] > row["open"] and close_pos >= 0.55
    return row["close"] < row["open"] and close_pos <= 0.45


def _touch_pullback(row: pd.Series, levels: list[float], side: int, tol: float) -> bool:
    clean = [float(x) for x in levels if np.isfinite(x)]
    if side > 0:
        return any(row["low"] <= x + tol and row["close"] > x for x in clean)
    return any(row["high"] >= x - tol and row["close"] < x for x in clean)


def _exit_price(px: float, side: int, tick: float, slip_ticks: float) -> float:
    return px - side * tick * slip_ticks


def _entry_price(px: float, side: int, tick: float, slip_ticks: float) -> float:
    return px + side * tick * slip_ticks


def _pnl(side: int, entry: float, exit_px: float, qty: int, point_value: float) -> float:
    return side * (exit_px - entry) * qty * point_value


def simulate_trade(
    day_df: pd.DataFrame,
    entry_pos: int,
    signal_pos: int,
    side: int,
    spec: MarketSpec,
    params: Params,
) -> tuple[dict, int]:
    sig = day_df.iloc[signal_pos]
    entry_bar = day_df.iloc[entry_pos]
    entry = _entry_price(float(entry_bar["open"]), side, spec.tick, params.slip_ticks)
    stop_raw = float(sig["low"] - 2 * spec.tick) if side > 0 else float(sig["high"] + 2 * spec.tick)

    stop_dist = abs(entry - stop_raw)
    a = float(sig["atr14"])
    if not np.isfinite(a) or a <= 0:
        return {}, entry_pos

    min_dist = params.min_stop_atr * a
    max_dist = params.max_stop_atr * a
    if stop_dist < min_dist:
        stop_dist = min_dist
        stop = entry - side * stop_dist
    else:
        stop = stop_raw
    stop_dist = abs(entry - stop)
    if stop_dist > max_dist:
        return {}, entry_pos

    qty = int(min(params.max_contracts, math.floor(params.risk_usd / (stop_dist * spec.point_value))))
    if qty < 1:
        return {}, entry_pos

    r_pts = stop_dist
    tp1 = entry + side * params.rr1 * r_pts
    final_target = entry + side * params.rr_final * r_pts
    partial_qty = qty // 2 if qty >= 2 else 0
    rem_qty = qty
    realized = 0.0
    hit_tp1 = False
    stop_cur = stop
    best = entry
    exit_reason = "eod"
    exit_px_last = float(entry)
    exit_pos = entry_pos

    flat_h, flat_m = _parse_hhmm(params.flat_time)
    flat_min = flat_h * 60 + flat_m

    for j in range(entry_pos, len(day_df)):
        bar = day_df.iloc[j]
        if _minute_of_day(bar["dt_ny"]) > flat_min:
            break

        hi = float(bar["high"])
        lo = float(bar["low"])
        close = float(bar["close"])

        if side > 0:
            best = max(best, hi)
            stopped = lo <= stop_cur
            final_hit = hi >= final_target
            tp1_hit = hi >= tp1
        else:
            best = min(best, lo)
            stopped = hi >= stop_cur
            final_hit = lo <= final_target
            tp1_hit = lo <= tp1

        if stopped:
            exit_px = _exit_price(stop_cur, side, spec.tick, params.slip_ticks)
            realized += _pnl(side, entry, exit_px, rem_qty, spec.point_value)
            exit_reason = "stop" if not hit_tp1 else "trail"
            exit_px_last = exit_px
            exit_pos = j
            rem_qty = 0
            break

        if (not hit_tp1) and tp1_hit:
            hit_tp1 = True
            if partial_qty > 0:
                exit_px = _exit_price(tp1, side, spec.tick, params.slip_ticks)
                realized += _pnl(side, entry, exit_px, partial_qty, spec.point_value)
                rem_qty -= partial_qty
            stop_cur = entry

        if hit_tp1 and rem_qty > 0:
            if side > 0:
                stop_cur = max(stop_cur, best - params.trail_r * r_pts)
            else:
                stop_cur = min(stop_cur, best + params.trail_r * r_pts)

        if final_hit and rem_qty > 0:
            exit_px = _exit_price(final_target, side, spec.tick, params.slip_ticks)
            realized += _pnl(side, entry, exit_px, rem_qty, spec.point_value)
            exit_reason = "target"
            exit_px_last = exit_px
            exit_pos = j
            rem_qty = 0
            break

        exit_px_last = close
        exit_pos = j

    if rem_qty > 0:
        last = day_df.iloc[exit_pos]
        exit_px = _exit_price(float(last["close"]), side, spec.tick, params.slip_ticks)
        realized += _pnl(side, entry, exit_px, rem_qty, spec.point_value)
        exit_px_last = exit_px

    realized -= qty * params.commission_rt

    trade = {
        "day": str(entry_bar["day"]),
        "entry_time": entry_bar["dt_ny"],
        "exit_time": day_df.iloc[exit_pos]["dt_ny"],
        "symbol": spec.symbol,
        "side": "long" if side > 0 else "short",
        "qty": qty,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "exit": round(exit_px_last, 2),
        "r_points": round(r_pts, 2),
        "pnl": round(realized, 2),
        "reason": exit_reason,
    }
    return trade, max(exit_pos, entry_pos)


def backtest_one(
    data: pd.DataFrame,
    peer: pd.DataFrame,
    spec: MarketSpec,
    params: Params,
    date_start: object | None = None,
    date_end: object | None = None,
    peer_map: dict[pd.Timestamp, pd.Series] | None = None,
) -> tuple[pd.DataFrame, dict]:
    d = data.copy()
    if date_start is not None:
        d = d[d["day"] >= date_start]
    if date_end is not None:
        d = d[d["day"] <= date_end]
    if peer_map is None:
        peer_map = _row_by_time(peer)

    equity = params.start_equity
    peak = equity
    max_dd = 0.0
    worst_day = 0.0
    trades: list[dict] = []
    threshold = params.start_equity - 2_000.0
    breached = False

    end_h, end_m = _parse_hhmm(params.trade_end)
    trade_end_min = end_h * 60 + end_m
    or_bars = max(1, params.or_minutes // (15 if params.interval == "15m" else 5))

    for _, g0 in d.groupby("day", sort=True):
        g = g0.reset_index(drop=True)
        if len(g) <= or_bars + 2:
            continue
        or_part = g.iloc[:or_bars]
        or_hi = float(or_part["high"].max())
        or_lo = float(or_part["low"].min())
        breakout_side = 0
        trades_day = 0
        day_pnl = 0.0
        i = or_bars

        while i < len(g) - 1:
            row = g.iloc[i]
            minute = _minute_of_day(row["dt_ny"])
            if minute > trade_end_min:
                break
            if day_pnl <= -params.max_daily_loss or trades_day >= params.max_trades_day:
                break
            if not np.isfinite(row["vwap"]) or not np.isfinite(row["atr14"]):
                i += 1
                continue

            peer_row = _get_peer(peer_map, row["dt_utc"])

            if breakout_side == 0:
                long_break = (
                    row["close"] > or_hi
                    and row["close"] > row["vwap"]
                    and row["ema9"] >= row["ema20"]
                    and _peer_ok(peer_row, 1, params.require_peer)
                )
                short_break = (
                    row["close"] < or_lo
                    and row["close"] < row["vwap"]
                    and row["ema9"] <= row["ema20"]
                    and _peer_ok(peer_row, -1, params.require_peer)
                )
                if long_break:
                    breakout_side = 1
                elif short_break:
                    breakout_side = -1
                i += 1
                continue

            side = breakout_side
            tol = params.pullback_tol_atr * float(row["atr14"])
            if (
                _peer_ok(peer_row, side, params.require_peer)
                and _touch_pullback(row, _levels(row, or_hi, or_lo, side), side, tol)
                and _rejection(row, side)
            ):
                trade, exit_i = simulate_trade(g, i + 1, i, side, spec, params)
                if trade:
                    trades_day += 1
                    trade["equity_before"] = round(equity, 2)
                    equity += float(trade["pnl"])
                    trade["equity_after"] = round(equity, 2)
                    trades.append(trade)
                    day_pnl += float(trade["pnl"])
                    peak = max(peak, equity)
                    max_dd = min(max_dd, equity - peak)
                    if equity <= threshold:
                        breached = True
                    i = exit_i + 1
                    breakout_side = 0
                    continue
            i += 1

        worst_day = min(worst_day, day_pnl)
        eod_high = max(peak, equity)
        threshold = max(threshold, eod_high - 2_000.0)

    tr = pd.DataFrame(trades)
    metrics = summarize(tr, params.start_equity, equity, max_dd, worst_day, breached)
    return tr, metrics


def summarize(
    tr: pd.DataFrame,
    start_equity: float,
    final_equity: float,
    max_dd: float,
    worst_day: float,
    breached: bool,
) -> dict:
    if len(tr) == 0:
        return {
            "trades": 0,
            "final": round(final_equity, 2),
            "profit": round(final_equity - start_equity, 2),
            "return_pct": round((final_equity / start_equity - 1) * 100, 2),
            "win_rate": 0.0,
            "pf": 0.0,
            "max_dd": round(max_dd, 2),
            "worst_day": round(worst_day, 2),
            "passed_50k_eval": False,
            "breached": breached,
        }
    wins = tr[tr["pnl"] > 0]["pnl"].sum()
    losses = -tr[tr["pnl"] < 0]["pnl"].sum()
    pf = wins / losses if losses > 0 else float("inf")
    target_hit = final_equity >= start_equity + 3_000.0
    return {
        "trades": int(len(tr)),
        "final": round(final_equity, 2),
        "profit": round(final_equity - start_equity, 2),
        "return_pct": round((final_equity / start_equity - 1) * 100, 2),
        "win_rate": round((tr["pnl"] > 0).mean() * 100, 1),
        "pf": round(pf, 2) if np.isfinite(pf) else None,
        "max_dd": round(max_dd, 2),
        "worst_day": round(worst_day, 2),
        "passed_50k_eval": bool(target_hit and not breached and worst_day > -1_000.0),
        "breached": breached,
    }


def split_dates(df: pd.DataFrame) -> tuple[object, object]:
    days = sorted(df["day"].unique())
    mid = len(days) // 2
    return days[mid - 1], days[mid]


def score(m: dict) -> float:
    if m["trades"] < 5:
        return -1e9
    return m["profit"] + 0.25 * m["max_dd"] + 5.0 * m["pf"]


def format_metrics(name: str, m: dict) -> str:
    pf = "inf" if m["pf"] is None else f"{m['pf']:.2f}"
    passed = "YES" if m["passed_50k_eval"] else "no"
    return (
        f"{name:<24} final ${m['final']:>9,.2f}  profit ${m['profit']:>8,.2f} "
        f"({m['return_pct']:>6.2f}%)  trades {m['trades']:>3}  win {m['win_rate']:>5.1f}% "
        f"PF {pf:>5}  DD ${m['max_dd']:>8,.2f}  worstDay ${m['worst_day']:>7,.2f}  pass {passed}"
    )


def run(refresh: bool = False) -> None:
    raw = {
        key: {
            p.interval: fetch_yahoo(spec.symbol, p.interval, p.yahoo_range, refresh=refresh)
            for p in [Params(interval="5m"), Params(interval="15m")]
        }
        for key, spec in MARKETS.items()
    }
    data = {k: {tf: prepare(df) for tf, df in v.items()} for k, v in raw.items()}
    peer_maps = {k: {tf: _row_by_time(df) for tf, df in v.items()} for k, v in data.items()}

    print("Data coverage")
    for key in MARKETS:
        for tf, df in data[key].items():
            print(f"  {key} {tf}: {df['dt_ny'].iloc[0]} -> {df['dt_ny'].iloc[-1]} | {len(df)} RTH bars")
    print()

    grid = []
    for interval, risk_usd, rr_final, trail_r, max_stop_atr, tol, require_peer, trade_end in itertools.product(
        ["5m", "15m"],
        [200.0],
        [2.0, 2.5, 3.0],
        [1.0],
        [1.4, 1.8],
        [0.15, 0.25],
        [True],
        ["10:30", "11:30"],
    ):
        grid.append(
            Params(
                interval=interval,
                risk_usd=risk_usd,
                rr_final=rr_final,
                trail_r=trail_r,
                max_stop_atr=max_stop_atr,
                pullback_tol_atr=tol,
                require_peer=require_peer,
                trade_end=trade_end,
            )
        )

    rows = []
    selected = []
    for key, spec in MARKETS.items():
        peer_key = "MNQ" if key == "MES" else "MES"
        for p in grid:
            df = data[key][p.interval]
            peer = data[peer_key][p.interval]
            peer_map = peer_maps[peer_key][p.interval]
            train_end, hold_start = split_dates(df)
            tr_train, m_train = backtest_one(df, peer, spec, p, date_end=train_end, peer_map=peer_map)
            tr_hold, m_hold = backtest_one(df, peer, spec, p, date_start=hold_start, peer_map=peer_map)
            tr_all, m_all = backtest_one(df, peer, spec, p, peer_map=peer_map)
            rows.append(
                {
                    "market": key,
                    "interval": p.interval,
                    "risk": p.risk_usd,
                    "rr_final": p.rr_final,
                    "trail_r": p.trail_r,
                    "max_stop_atr": p.max_stop_atr,
                    "tol": p.pullback_tol_atr,
                    "trade_end": p.trade_end,
                    "train_profit": m_train["profit"],
                    "train_trades": m_train["trades"],
                    "train_pf": m_train["pf"] or 99.0,
                    "hold_profit": m_hold["profit"],
                    "hold_trades": m_hold["trades"],
                    "hold_pf": m_hold["pf"] or 99.0,
                    "all_profit": m_all["profit"],
                    "all_trades": m_all["trades"],
                    "params": p,
                    "train_metrics": m_train,
                    "hold_metrics": m_hold,
                    "all_metrics": m_all,
                }
            )
        market_rows = [r for r in rows if r["market"] == key and r["train_trades"] >= 5]
        if market_rows:
            best = max(market_rows, key=lambda r: score(r["train_metrics"]))
            selected.append(best)

    print("Selected by TRAIN only, then checked on HOLDOUT")
    for r in selected:
        p = r["params"]
        print(
            f"\n{r['market']} {p.interval} params: risk=${p.risk_usd:.0f}, RRfinal={p.rr_final}, "
            f"trail={p.trail_r}R, maxStop={p.max_stop_atr}ATR, pullTol={p.pullback_tol_atr}ATR, "
            f"end={p.trade_end}"
        )
        print("  " + format_metrics("TRAIN", r["train_metrics"]))
        print("  " + format_metrics("HOLDOUT", r["hold_metrics"]))
        print("  " + format_metrics("FULL", r["all_metrics"]))

    print("\nBest full-sample rows (cheating/overfit view, do not trust alone)")
    best_full = sorted(rows, key=lambda r: r["all_profit"], reverse=True)[:8]
    for r in best_full:
        p = r["params"]
        print(
            f"  {r['market']} {p.interval} risk={p.risk_usd:.0f} rr={p.rr_final} "
            f"trail={p.trail_r} maxStop={p.max_stop_atr} tol={p.pullback_tol_atr} "
            f"end={p.trade_end} | {format_metrics('FULL', r['all_metrics'])}"
        )

    run_one_contract_diagnostic(refresh=refresh)


def run_one_contract_diagnostic(refresh: bool = False) -> None:
    """Same strategy, but with one ES/NQ contract instead of micros.

    This is the only configuration that can realistically hit the 50K Apex target
    in the limited Yahoo 5m sample. It is also materially riskier.
    """
    print("\nOne-contract ES/NQ diagnostic (same rules, higher risk)")
    specs = {
        "ES": MarketSpec("ES=F", "E-mini S&P 500", 50.0),
        "NQ": MarketSpec("NQ=F", "E-mini Nasdaq-100", 20.0),
    }
    data = {
        key: prepare(fetch_yahoo(spec.symbol, "5m", "60d", refresh=refresh))
        for key, spec in specs.items()
    }
    cases = [("ES", "NQ"), ("NQ", "ES")]
    for key, peer_key in cases:
        spec = specs[key]
        peer_map = _row_by_time(data[peer_key])
        train_end, hold_start = split_dates(data[key])
        rows = []
        for rr_final, max_stop_atr, tol, trade_end in itertools.product(
            [2.0, 2.5, 3.0],
            [1.0, 1.4, 1.8],
            [0.15, 0.25],
            ["10:30", "11:30"],
        ):
            p = Params(
                interval="5m",
                risk_usd=1_000.0,
                max_daily_loss=1_000.0,
                max_contracts=1,
                rr_final=rr_final,
                trail_r=1.0,
                max_stop_atr=max_stop_atr,
                pullback_tol_atr=tol,
                trade_end=trade_end,
            )
            _, m_train = backtest_one(
                data[key], data[peer_key], spec, p, date_end=train_end, peer_map=peer_map
            )
            _, m_hold = backtest_one(
                data[key], data[peer_key], spec, p, date_start=hold_start, peer_map=peer_map
            )
            _, m_all = backtest_one(data[key], data[peer_key], spec, p, peer_map=peer_map)
            if m_train["trades"] >= 3:
                rows.append((score(m_train), p, m_train, m_hold, m_all))

        if not rows:
            print(f"  {key}: no train-qualified rows")
            continue

        _, p, m_train, m_hold, m_all = max(rows, key=lambda x: x[0])
        print(
            f"\n{key} selected by TRAIN: risk=$1000, one contract, RRfinal={p.rr_final}, "
            f"maxStop={p.max_stop_atr}ATR, pullTol={p.pullback_tol_atr}ATR, end={p.trade_end}"
        )
        print("  " + format_metrics("TRAIN", m_train))
        print("  " + format_metrics("HOLDOUT", m_hold))
        print("  " + format_metrics("FULL", m_all))

        if key == "ES":
            days = sorted(data[key]["day"].unique())
            last_day = days[-1]
            full_windows = 0
            pass_windows = 0
            best_profit = -1e18
            worst_profit = 1e18
            for start in days:
                end = (pd.Timestamp(start) + pd.Timedelta(days=30)).date()
                if end > last_day:
                    continue
                _, m = backtest_one(
                    data[key], data[peer_key], spec, p, date_start=start, date_end=end, peer_map=peer_map
                )
                full_windows += 1
                pass_windows += int(m["passed_50k_eval"])
                best_profit = max(best_profit, m["profit"])
                worst_profit = min(worst_profit, m["profit"])
            print(
                f"  ES rolling full 30-calendar-day windows: {pass_windows}/{full_windows} passed, "
                f"profit range ${worst_profit:,.2f} to ${best_profit:,.2f}"
            )


if __name__ == "__main__":
    run(refresh=False)
