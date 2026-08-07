"""Cost-aware, time-split research for the penny-stock scanner.

This module exists to keep the live bot honest.  It intentionally uses only
information available at the signal close, enters at the *next* session's open,
models bad stop/target ordering conservatively, and tests a locked rule on a
later time period.

The default Yahoo universe is useful for rejecting bad ideas, but it contains
only securities that exist today.  Consequently a passing run is labelled
PROVISIONAL, never VALIDATED.  Supply a point-in-time, delisted-inclusive panel
before allowing the report to set ``auto_trade_allowed``.

Usage::

    python research/penny_edge_research.py --refresh
    python research/penny_edge_research.py
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
import pickle
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import penny_stats  # noqa: E402  (needs ROOT on sys.path first)

CACHE_PATH = ROOT / "research" / "cache" / "penny_daily_current_universe.pkl"
EARNINGS_CACHE_PATH = ROOT / "research" / "cache" / "penny_earnings_current_universe.pkl"
REPORT_PATH = ROOT / "data" / "pennystock_edge_report.json"
POLICY_PATH = ROOT / "data" / "pennystock_edge_policy.json"

MIN_PRICE = 0.50
MAX_PRICE = 5.00
MIN_CURRENT_VOLUME = 300_000
MIN_CURRENT_MARKET_CAP = 10_000_000
LISTED_EXCHANGES = {"NMS", "NCM", "NGM", "NYQ", "ASE", "NAE"}
TRAIN_END = pd.Timestamp("2022-12-31")
VALIDATION_END = pd.Timestamp("2024-12-31")
RNG_SEED = 20260807
AUDIT_METHOD_VERSION = "calendar-block-v2-2026-08-07"


@dataclass(frozen=True)
class StrategySpec:
    name: str
    hold_days: int
    stop_atr: float
    reward_r: float
    max_entries_per_day: int = 3


SPECS = (
    StrategySpec("controlled_breakout", 5, 1.15, 2.0),
    StrategySpec("base_breakout", 5, 1.15, 2.0),
    StrategySpec("breakout_retest", 5, 1.00, 2.0),
    StrategySpec("quality_momentum", 10, 1.40, 2.5),
    StrategySpec("flush_reclaim", 3, 1.00, 1.5),
)


def _json_dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)


def _finite(value: float, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def current_universe() -> list[str]:
    """Return all currently eligible listed US symbols, not just top movers."""
    base = [
        yf.EquityQuery("gt", ["intradayprice", 0.10]),
        yf.EquityQuery("lt", ["intradayprice", MAX_PRICE]),
        yf.EquityQuery("gt", ["avgdailyvol3m", MIN_CURRENT_VOLUME]),
        yf.EquityQuery("gt", ["intradaymarketcap", MIN_CURRENT_MARKET_CAP]),
        yf.EquityQuery("eq", ["region", "us"]),
        yf.EquityQuery("is-in", ["exchange", *sorted(LISTED_EXCHANGES)]),
    ]
    query = yf.EquityQuery("and", base)
    symbols: list[str] = []
    seen: set[str] = set()
    for offset in range(0, 2_000, 250):
        response = yf.screen(
            query, offset=offset, size=250, sortField="ticker", sortAsc=True
        )
        quotes = (response or {}).get("quotes") or []
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").strip().upper()
            if symbol and symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
        if len(quotes) < 250:
            break
    return symbols


def _extract_symbol(batch: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if batch.empty:
        return pd.DataFrame()
    if isinstance(batch.columns, pd.MultiIndex):
        try:
            frame = batch.xs(symbol, axis=1, level=1).copy()
        except KeyError:
            return pd.DataFrame()
    else:
        frame = batch.copy()
    wanted = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    for col in wanted:
        if col not in frame:
            frame[col] = np.nan
    frame = frame[wanted].apply(pd.to_numeric, errors="coerce")
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    return frame.dropna(how="all")


def download_panel(period: str = "10y", chunk_size: int = 100) -> dict:
    symbols = current_universe()
    frames: dict[str, pd.DataFrame] = {}
    requested = symbols + ["IWM"]
    for start in range(0, len(requested), chunk_size):
        chunk = requested[start : start + chunk_size]
        batch = yf.download(
            chunk,
            period=period,
            interval="1d",
            auto_adjust=False,
            repair=True,
            actions=False,
            threads=True,
            progress=False,
            timeout=25,
            group_by="column",
        )
        for symbol in chunk:
            frame = _extract_symbol(batch, symbol)
            if len(frame) >= 160:
                frames[symbol] = frame

    payload = {
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "Yahoo Finance via yfinance",
            "universe": "current listed US stocks under $5 with current liquidity",
            "survivorship_free": False,
            "point_in_time_membership": False,
            "requested_symbols": len(symbols),
            "usable_symbols": len(frames) - int("IWM" in frames),
            "period": period,
        },
        "frames": frames,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, CACHE_PATH)
    return payload


def load_panel(refresh: bool = False) -> dict:
    if refresh or not CACHE_PATH.exists():
        return download_panel()
    with CACHE_PATH.open("rb") as f:
        return pickle.load(f)


def download_earnings_events(payload: dict, workers: int = 8) -> dict:
    """Cache historical reported/estimated EPS with release timestamps.

    Yahoo's earnings page is not a licensed point-in-time database.  These data
    are therefore useful for a falsification audit, but their presence never
    upgrades the report's data-quality gate to validated.
    """
    frames: dict[str, pd.DataFrame] = payload["frames"]
    symbols = []
    for symbol, raw in frames.items():
        if symbol == "IWM" or raw.empty:
            continue
        close = raw["Close"]
        dollar_volume = (close * raw["Volume"]).rolling(20).median()
        if (close.between(1.0, 5.0) & (dollar_volume >= 5_000_000)).any():
            symbols.append(symbol)

    def fetch(symbol: str) -> tuple[str, pd.DataFrame | None]:
        try:
            return symbol, yf.Ticker(symbol).get_earnings_dates(limit=50)
        except Exception:
            return symbol, None

    events: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(fetch, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, frame = future.result()
            if frame is not None and not frame.empty:
                events[symbol] = frame
    result = {"metadata": payload["metadata"], "events": events}
    EARNINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = EARNINGS_CACHE_PATH.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, EARNINGS_CACHE_PATH)
    return result


def load_earnings_events(payload: dict, refresh: bool = False) -> dict:
    if refresh or not EARNINGS_CACHE_PATH.exists():
        return download_earnings_events(payload)
    with EARNINGS_CACHE_PATH.open("rb") as f:
        return pickle.load(f)


def _features(symbol: str, raw: pd.DataFrame, iwm: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    close = raw["Close"].replace(0, np.nan)
    factor = raw["Adj Close"].div(close).replace([np.inf, -np.inf], np.nan)
    factor = factor.ffill().bfill().fillna(1.0)
    out["open"] = raw["Open"] * factor
    out["high"] = raw["High"] * factor
    out["low"] = raw["Low"] * factor
    out["close"] = raw["Adj Close"].fillna(close * factor)
    out["raw_close"] = close
    out["volume"] = raw["Volume"]

    ret1 = out["close"].pct_change(fill_method=None)
    out["ret1"] = ret1
    for days in (5, 20, 63, 126):
        out[f"ret{days}"] = out["close"].pct_change(days, fill_method=None)
    out["sma20"] = out["close"].rolling(20).mean()
    out["sma50"] = out["close"].rolling(50).mean()
    out["prior20_high"] = out["high"].shift(1).rolling(20).max()
    out["max_ret20"] = ret1.shift(1).rolling(20).max()
    prior_volume = out["volume"].shift(1).rolling(20).median()
    out["volume_ratio"] = out["volume"].div(prior_volume)
    out["dollar_volume20"] = (
        (out["raw_close"] * out["volume"]).shift(1).rolling(20).median()
    )
    day_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["close_location"] = (out["close"] - out["low"]).div(day_range).clip(0, 1)
    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_pct"] = true_range.rolling(20).mean().div(out["close"])

    iwm_close = iwm["Adj Close"].reindex(out.index).ffill()
    out["iwm_risk_on"] = iwm_close > iwm_close.rolling(50).mean()
    out["iwm_ret20"] = iwm_close.pct_change(20, fill_method=None)
    out["symbol"] = symbol
    return out


def build_feature_panel(payload: dict) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = payload["frames"]
    iwm = frames.get("IWM")
    if iwm is None or iwm.empty:
        raise RuntimeError("IWM history is required for the market-regime feature")
    return {
        symbol: _features(symbol, raw, iwm)
        for symbol, raw in frames.items()
        if symbol != "IWM" and not raw.empty
    }


def _base(frame: pd.DataFrame, min_dollar_volume: float) -> pd.Series:
    return (
        frame["raw_close"].between(MIN_PRICE, MAX_PRICE)
        & (frame["dollar_volume20"] >= min_dollar_volume)
        & frame["atr_pct"].between(0.02, 0.22)
        & frame["iwm_risk_on"].fillna(False)
        & (frame["iwm_ret20"] > -0.05)
    )


def signal_mask(frame: pd.DataFrame, name: str) -> pd.Series:
    base3 = _base(frame, 3_000_000)
    breakout = (
        base3
        & frame["ret1"].between(0.03, 0.15)
        & frame["volume_ratio"].between(1.5, 8.0)
        & (frame["close_location"] >= 0.75)
        & (frame["close"] >= frame["prior20_high"] * 0.98)
        & frame["ret20"].between(-0.05, 0.40)
        & (frame["max_ret20"] < 0.25)
        & (frame["atr_pct"] <= 0.18)
    )
    if name == "controlled_breakout":
        condition = breakout
    elif name == "base_breakout":
        condition = (
            base3
            & frame["ret1"].between(0.01, 0.10)
            & frame["volume_ratio"].between(1.2, 5.0)
            & (frame["close_location"] >= 0.70)
            & (frame["close"] >= frame["prior20_high"] * 0.99)
            & frame["ret20"].between(-0.10, 0.25)
            & (frame["max_ret20"] < 0.20)
            & (frame["atr_pct"] <= 0.15)
        )
    elif name == "breakout_retest":
        condition = (
            breakout.shift(1, fill_value=False)
            & frame["ret1"].between(-0.05, 0.03)
            & (frame["close_location"] >= 0.50)
            & (frame["close"] >= frame["prior20_high"] * 0.97)
            & (frame["volume_ratio"] <= 4.0)
        )
    elif name == "quality_momentum":
        condition = (
            _base(frame, 10_000_000)
            & frame["ret63"].between(0.10, 0.80)
            & frame["ret126"].between(0.15, 1.50)
            & frame["ret5"].between(-0.05, 0.15)
            & (frame["max_ret20"] < 0.20)
            & (frame["atr_pct"] <= 0.12)
            & (frame["close"] > frame["sma50"])
            & frame["volume_ratio"].between(0.5, 3.0)
        )
    elif name == "flush_reclaim":
        condition = (
            _base(frame, 7_500_000)
            & frame["ret1"].between(-0.15, -0.04)
            & frame["ret5"].between(-0.30, -0.08)
            & (frame["ret63"] > 0)
            & (frame["close_location"] >= 0.65)
            & (frame["volume_ratio"] >= 1.5)
            & (frame["atr_pct"] <= 0.18)
        )
    else:
        raise KeyError(name)

    # Enter only on the first day a setup becomes true.  Counting the same
    # ten-day condition as ten independent bets manufactures sample size.
    return condition.fillna(False) & ~condition.shift(1, fill_value=False)


def estimated_round_trip_cost(row: pd.Series) -> float:
    """Conservative daily-bar cost proxy (spread + slippage), as a return."""
    dollar_volume = max(_finite(row.get("dollar_volume20")), 100_000.0)
    price = max(_finite(row.get("raw_close")), 0.10)
    cost = 0.006 + 0.008 * math.sqrt(5_000_000 / dollar_volume)
    cost += 0.003 * math.sqrt(1.0 / price)
    return min(0.04, max(0.01, cost))


def _candidate_score(row: pd.Series, name: str) -> float:
    if name == "quality_momentum":
        return _finite(row.get("ret63")) - 0.5 * _finite(row.get("max_ret20"))
    if name == "flush_reclaim":
        return _finite(row.get("close_location")) + abs(_finite(row.get("ret5")))
    return (
        _finite(row.get("close_location"))
        + 0.15 * math.log1p(max(0.0, _finite(row.get("volume_ratio"))))
        - 0.5 * max(0.0, _finite(row.get("ret20")) - 0.25)
    )


def _simulate_one(
    frame: pd.DataFrame, signal_pos: int, spec: StrategySpec
) -> dict | None:
    entry_pos = signal_pos + 1
    end_pos = entry_pos + spec.hold_days - 1
    if end_pos >= len(frame):
        return None
    signal = frame.iloc[signal_pos]
    path = frame.iloc[entry_pos : end_pos + 1]
    if path[["open", "high", "low", "close"]].isna().any().any():
        return None
    entry = _finite(path.iloc[0]["open"])
    if entry <= 0:
        return None
    stop_pct = min(0.14, max(0.06, _finite(signal["atr_pct"]) * spec.stop_atr))
    stop = entry * (1.0 - stop_pct)
    target = entry * (1.0 + stop_pct * spec.reward_r)
    exit_price = _finite(path.iloc[-1]["close"])
    exit_reason = "time"
    held = spec.hold_days
    for day_no, (_, bar) in enumerate(path.iterrows(), start=1):
        opn, high, low = (_finite(bar[c]) for c in ("open", "high", "low"))
        if opn <= stop:
            exit_price, exit_reason, held = opn, "stop_gap", day_no
            break
        if low <= stop:
            # If both levels print in one daily bar, assume the stop happened first.
            exit_price, exit_reason, held = stop, "stop", day_no
            break
        if opn >= target or high >= target:
            exit_price, exit_reason, held = target, "target", day_no
            break
    gross = exit_price / entry - 1.0
    cost = estimated_round_trip_cost(signal)
    return {
        "ticker": str(signal["symbol"]),
        "signal_date": frame.index[signal_pos].strftime("%Y-%m-%d"),
        "entry_date": path.index[0].strftime("%Y-%m-%d"),
        "score": _candidate_score(signal, spec.name),
        "gross_return": gross,
        "cost": cost,
        "net_return": gross - cost,
        "stress_net_return": gross - 2.0 * cost,
        "held_days": held,
        "exit_reason": exit_reason,
        "entry": entry,
        "stop_pct": stop_pct,
    }


def simulate(panel: dict[str, pd.DataFrame], spec: StrategySpec) -> pd.DataFrame:
    trades: list[dict] = []
    for frame in panel.values():
        mask = signal_mask(frame, spec.name)
        last_exit = -1
        for signal_pos in np.flatnonzero(mask.to_numpy()):
            if signal_pos <= last_exit:
                continue
            trade = _simulate_one(frame, int(signal_pos), spec)
            if trade is not None:
                trades.append(trade)
                last_exit = int(signal_pos) + spec.hold_days
    if not trades:
        return pd.DataFrame()
    out = pd.DataFrame(trades)
    out["signal_date"] = pd.to_datetime(out["signal_date"])
    # Capacity guard: at most N new positions on one date.  The score is defined
    # before the future return and therefore does not leak the answer.
    out = (
        out.sort_values(["signal_date", "score"], ascending=[True, False])
        .groupby("signal_date", group_keys=False)
        .head(spec.max_entries_per_day)
        .reset_index(drop=True)
    )
    return out


def simulate_profitable_earnings_beats(
    panel: dict[str, pd.DataFrame], earnings_payload: dict
) -> pd.DataFrame:
    """Locked event rule selected using data through 2024 only.

    Rules: reported EPS > 0, estimate beaten by >=10%, $1-$5 at the prior
    close, >=$5m median dollar volume, ATR <=20%, and no >35% lottery day in
    the preceding month.  A premarket report enters that session's open; an
    after-close report enters the following session.  Exit is ten sessions
    later or at a 20% catastrophe stop.  At most three releases enter per day.
    """
    rows: list[dict] = []
    for symbol, events in (earnings_payload.get("events") or {}).items():
        frame = panel.get(symbol)
        if frame is None or frame.empty:
            continue
        for timestamp, event in events.iterrows():
            surprise = _finite(event.get("Surprise(%)"), default=float("nan"))
            estimate = _finite(event.get("EPS Estimate"), default=float("nan"))
            reported = _finite(event.get("Reported EPS"), default=float("nan"))
            if not all(math.isfinite(x) for x in (surprise, estimate, reported)):
                continue
            if surprise < 10.0 or reported <= estimate or reported <= 0:
                continue

            released = pd.Timestamp(timestamp)
            if released.tzinfo is not None:
                released = released.tz_convert("America/New_York")
                release_day = released.tz_localize(None).normalize()
            else:
                release_day = released.normalize()
            entry_mask = (
                frame.index >= release_day if released.hour < 9 else frame.index > release_day
            )
            positions = np.flatnonzero(entry_mask)
            if not len(positions):
                continue
            entry_pos = int(positions[0])
            signal_pos = entry_pos - 1
            exit_pos = entry_pos + 9
            if signal_pos < 126 or exit_pos >= len(frame):
                continue
            signal = frame.iloc[signal_pos]
            if not (
                1.0 <= signal["raw_close"] <= 5.0
                and signal["dollar_volume20"] >= 5_000_000
                and signal["atr_pct"] <= 0.20
                and signal["max_ret20"] < 0.35
            ):
                continue
            path = frame.iloc[entry_pos : exit_pos + 1]
            if path[["open", "high", "low", "close"]].isna().any().any():
                continue
            entry = _finite(path.iloc[0]["open"])
            if entry <= 0:
                continue
            stop = entry * 0.80
            exit_price = _finite(path.iloc[-1]["close"])
            reason = "time"
            for _, bar in path.iterrows():
                if _finite(bar["open"]) <= stop:
                    exit_price, reason = _finite(bar["open"]), "stop_gap"
                    break
                if _finite(bar["low"]) <= stop:
                    exit_price, reason = stop, "stop"
                    break
            cost = estimated_round_trip_cost(signal)
            gross = exit_price / entry - 1.0
            rows.append(
                {
                    "ticker": symbol,
                    "signal_date": release_day,
                    "entry_date": path.index[0],
                    "score": surprise,
                    "gross_return": gross,
                    "cost": cost,
                    "net_return": gross - cost,
                    "stress_net_return": gross - 2.0 * cost,
                    "held_days": len(path),
                    "exit_reason": reason,
                    "entry": entry,
                    "stop_pct": 0.20,
                }
            )
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values(["signal_date", "score"], ascending=[True, False])
        .groupby("signal_date", group_keys=False)
        .head(3)
        .reset_index(drop=True)
    )


def _bootstrap_ci(trades: pd.DataFrame, column: str = "net_return") -> list[float]:
    if trades.empty:
        return [0.0, 0.0]
    # Resample signal dates, not individual trades, to retain same-day clustering.
    daily = trades.groupby("signal_date")[column].mean().to_numpy(dtype=float)
    if len(daily) < 2:
        value = float(daily.mean())
        return [value, value]
    rng = np.random.default_rng(RNG_SEED)
    means = np.empty(5_000)
    for i in range(len(means)):
        means[i] = rng.choice(daily, size=len(daily), replace=True).mean()
    return [float(x) for x in np.quantile(means, [0.025, 0.975])]


def metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trades": 0,
            "mean_net_pct": 0.0,
            "median_net_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "stress_mean_net_pct": 0.0,
            "bootstrap_95_pct": [0.0, 0.0],
            "max_symbol_share_of_positive_pnl_pct": 100.0,
        }
    net = trades["net_return"].astype(float)
    gains = float(net[net > 0].sum())
    losses = abs(float(net[net < 0].sum()))
    by_symbol = trades.groupby("ticker")["net_return"].sum().clip(lower=0)
    positive_total = float(by_symbol.sum())
    concentration = (
        float(by_symbol.max()) / positive_total if positive_total > 0 and len(by_symbol) else 1.0
    )
    ci = _bootstrap_ci(trades)
    return {
        "trades": int(len(trades)),
        "symbols": int(trades["ticker"].nunique()),
        "signal_days": int(trades["signal_date"].nunique()),
        "mean_net_pct": round(float(net.mean()) * 100, 4),
        "median_net_pct": round(float(net.median()) * 100, 4),
        "win_rate_pct": round(float((net > 0).mean()) * 100, 2),
        "profit_factor": round(gains / losses, 4) if losses else 99.0,
        "stress_mean_net_pct": round(float(trades["stress_net_return"].mean()) * 100, 4),
        "bootstrap_95_pct": [round(x * 100, 4) for x in ci],
        "max_symbol_share_of_positive_pnl_pct": round(concentration * 100, 2),
        "avg_cost_pct": round(float(trades["cost"].mean()) * 100, 4),
    }


def split_metrics(trades: pd.DataFrame) -> dict:
    train = trades[trades["signal_date"] <= TRAIN_END]
    validation = trades[
        (trades["signal_date"] > TRAIN_END)
        & (trades["signal_date"] <= VALIDATION_END)
    ]
    test = trades[trades["signal_date"] > VALIDATION_END]
    return {
        "train": metrics(train),
        "validation": metrics(validation),
        "test": metrics(test),
    }


def _selection_score(result: dict) -> float:
    train = result["splits"]["train"]
    validation = result["splits"]["validation"]
    if train["trades"] < 40 or validation["trades"] < 25:
        return -999.0
    if train["mean_net_pct"] <= 0 or validation["mean_net_pct"] <= 0:
        return -999.0
    return min(train["mean_net_pct"], validation["mean_net_pct"]) + 0.10 * min(
        train["profit_factor"], validation["profit_factor"]
    )


def _numeric_gate(splits: dict) -> tuple[bool, list[str]]:
    train, validation, test = (splits[k] for k in ("train", "validation", "test"))
    checks = {
        "train has >=40 trades and positive expectancy":
            train["trades"] >= 40 and train["mean_net_pct"] > 0,
        "validation has >=25 trades and positive expectancy":
            validation["trades"] >= 25 and validation["mean_net_pct"] > 0,
        "untouched test has >=40 trades": test["trades"] >= 40,
        "untouched test mean net return >0": test["mean_net_pct"] > 0,
        "untouched test profit factor >=1.15": test["profit_factor"] >= 1.15,
        "untouched test clustered bootstrap lower bound >0":
            test["bootstrap_95_pct"][0] > 0,
        "untouched test survives 2x modeled costs": test["stress_mean_net_pct"] > 0,
        "no symbol supplies >30% of positive test PnL":
            test["max_symbol_share_of_positive_pnl_pct"] <= 30,
    }
    return all(checks.values()), [name for name, passed in checks.items() if not passed]


def run(refresh: bool = False, refresh_earnings: bool = False) -> dict:
    payload = load_panel(refresh=refresh)
    panel = build_feature_panel(payload)
    results = []
    all_trades: dict[str, pd.DataFrame] = {}
    for spec in SPECS:
        trades = simulate(panel, spec)
        all_trades[spec.name] = trades
        result = {
            "strategy": asdict(spec),
            "splits": split_metrics(trades),
        }
        result["selection_score"] = round(_selection_score(result), 6)
        results.append(result)
    earnings_payload = load_earnings_events(
        payload, refresh=(refresh or refresh_earnings)
    )
    earnings_trades = simulate_profitable_earnings_beats(panel, earnings_payload)
    earnings_result = {
        "strategy": {
            "name": "profitable_earnings_beat",
            "hold_days": 10,
            "stop_pct": 20.0,
            "minimum_surprise_pct": 10.0,
            "reported_eps_must_be_positive": True,
            "minimum_dollar_volume": 5_000_000,
            "max_entries_per_day": 3,
        },
        "splits": split_metrics(earnings_trades),
    }
    earnings_result["selection_score"] = round(_selection_score(earnings_result), 6)
    results.append(earnings_result)
    all_trades["profitable_earnings_beat"] = earnings_trades
    selected = max(results, key=lambda item: item["selection_score"])
    selected_name = selected["strategy"]["name"]
    numeric_pass, failures = _numeric_gate(selected["splits"])

    # Selection-aware inference. The numeric gate above asks "did the winner do well?";
    # this asks "would the best of N look this good with no edge at all?" and "could this
    # sample detect an edge even if one existed?". A rule that fails either question was
    # never evidence, whichever way the test period happened to break.
    market_calendar = pd.DatetimeIndex(payload["frames"]["IWM"].index)
    inference = penny_stats.summarise(
        all_trades,
        selected_name,
        TRAIN_END,
        VALIDATION_END,
        int(selected["strategy"].get("hold_days") or 10),
        calendar=market_calendar,
        winner_capacity_cap=selected["strategy"].get("max_entries_per_day"),
    )
    sel_test = inference.get("selection_test") or {}
    if sel_test.get("applicable") and not sel_test.get("significant_at_5pct"):
        failures.append(
            "selection-aware significance (calendar-block family-wise p="
            f"{sel_test['p_value_selection_aware']:.3f} over "
            f"{sel_test['strategies_searched']} searched strategies)"
        )
        numeric_pass = False
    meta = payload["metadata"]
    data_valid = bool(meta.get("survivorship_free") and meta.get("point_in_time_membership"))
    status = "VALIDATED" if numeric_pass and data_valid else "PROVISIONAL" if numeric_pass else "REJECTED"
    auto_trade_allowed = status == "VALIDATED"
    methodology = {
        "signal_timing": "features use data through close t; fill is next session open",
        "execution": "ATR stop, target, conservative stop-first ordering in ambiguous daily bars",
        "costs": "dynamic 1%-4% round-trip proxy; robustness test doubles it",
        "splits": {
            "train_end": str(TRAIN_END.date()),
            "validation_end": str(VALIDATION_END.date()),
            "test_start": str((VALIDATION_END + pd.Timedelta(days=1)).date()),
        },
        "selection": "strategy chosen using train and validation only; test is final audit",
        "inference": (
            "same-day trades form an equal-weight basket; uncertainty and the family-wise "
            "search test resample contiguous 20-session blocks on the IWM trading calendar"
        ),
        "reported_estimands": (
            "trade-weighted mean and equal-weight signal-day basket mean are both shown; "
            "neither is relabelled as the other"
        ),
        "known_limitations": [
            "Yahoo panel contains current survivors and is not point-in-time universe membership",
            "daily OHLC cannot reveal intrabar ordering, so stop is assumed first",
            "historical bid/ask and market impact are unavailable; conservative proxy is used",
            "price-pattern candidates exclude fundamentals/news because free point-in-time histories are unavailable",
            "earnings estimates are reconstructed today, not archived point-in-time snapshots",
        ],
    }
    policy_basis = {
        "audit_method_version": AUDIT_METHOD_VERSION,
        "selected_strategy": selected_name,
        "strategy": selected["strategy"],
        "splits": selected["splits"],
        "candidate_family": results,
        "inference": inference,
        "data_snapshot": {
            key: meta.get(key)
            for key in (
                "created_at", "source", "universe", "requested_symbols",
                "usable_symbols", "period", "survivorship_free",
                "point_in_time_membership",
            )
        },
        "numeric_gate_passed": numeric_pass,
        "data_gate_passed": data_valid,
    }
    policy_hash = hashlib.sha256(
        json.dumps(policy_basis, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_method_version": AUDIT_METHOD_VERSION,
        "status": status,
        "auto_trade_allowed": auto_trade_allowed,
        "policy_hash": policy_hash,
        "selected_strategy": selected_name,
        "failed_checks": failures,
        "data": meta,
        "methodology": methodology,
        "inference": inference,
        "candidates": results,
    }
    policy = {
        "generated_at": report["generated_at"],
        "audit_method_version": AUDIT_METHOD_VERSION,
        "status": status,
        "auto_trade_allowed": auto_trade_allowed,
        "policy_hash": policy_hash,
        "selected_strategy": selected_name,
        "strategy_id": selected_name,
        "strategy": selected["strategy"],
        "test_metrics": selected["splits"]["test"],
        "failed_checks": failures,
        "inference_verdicts": inference.get("verdicts") or [],
        "selection_p_value": (inference.get("selection_test") or {}).get(
            "p_value_selection_aware"
        ),
        "reason": (
            "All numerical checks passed, but only a survivorship-free point-in-time panel "
            "may authorize automatic trading."
            if numeric_pass and not data_valid
            else "One or more predeclared edge checks failed."
            if not numeric_pass
            else "All numerical and data-quality checks passed."
        ),
    }
    _json_dump(REPORT_PATH, report)
    _json_dump(POLICY_PATH, policy)
    return report


def _print_report(report: dict) -> None:
    print(f"EDGE STATUS: {report['status']}  auto-trade={report['auto_trade_allowed']}")
    print(f"Selected (without test): {report['selected_strategy']}")
    for candidate in report["candidates"]:
        print(f"\n{candidate['strategy']['name']}")
        for split in ("train", "validation", "test"):
            m = candidate["splits"][split]
            print(
                f"  {split:10s} n={m['trades']:4d} mean={m['mean_net_pct']:+.3f}% "
                f"win={m['win_rate_pct']:5.1f}% PF={m['profit_factor']:.2f} "
                f"2x-cost={m['stress_mean_net_pct']:+.3f}% "
                f"CI=[{m['bootstrap_95_pct'][0]:+.3f},{m['bootstrap_95_pct'][1]:+.3f}]"
            )
    inf = report.get("inference") or {}
    sel = inf.get("selection_test") or {}
    if sel.get("applicable"):
        print("\nSelection-aware inference")
        print(f"  best of {sel['strategies_searched']} strategies over "
              f"{sel['market_sessions']} market sessions "
              f"({sel['union_signal_days']} union signal days)")
        print(f"  p(naive, single test)   = {sel['p_value_naive_single_test']:.3f}")
        print(f"  p(corrected for search) = {sel['p_value_selection_aware']:.3f}"
              f"   -> {'SIGNIFICANT' if sel['significant_at_5pct'] else 'not significant'}")
    for key in ("power_selection_window", "power_test_window"):
        blk = inf.get(key) or {}
        if blk.get("applicable"):
            print(
                f"  {blk['label']:16s} basket {blk['mean_signal_day_basket_net_pct']:+.2f}% "
                f"vs trade-weighted {blk['trade_weighted_mean_net_pct']:+.2f}%, "
                f"block-t={blk['t_stat']:+.2f}, resolvable only above "
                f"{blk['min_detectable_edge_pct']:.2f}%"
            )
    plan = inf.get("detectability_plan") or {}
    if plan.get("applicable"):
        print(f"\nTo ever prove a +{plan['target_edge_pct']:.0f}% signal-day basket edge:")
        print(f"  keep {plan['current_names_per_signal_day']:.2f} names/day -> need "
              f"{plan['signal_days_needed_at_current_breadth']} signal days "
              f"(~{plan['years_of_history_needed_at_current_breadth']:.0f} years)")
        print(f"  or hold {plan['names_per_day_needed_to_use_current_history']} names/day "
              f"-> the existing history is already enough")
        print(f"  binding lever: {plan['binding_lever']}")
        if plan.get("breadth_is_signal_limited"):
            print(f"  (peaks at {plan['max_names_on_any_day']} names on only "
                  f"{plan['days_at_that_maximum']} of {plan['current_signal_days']} days)")
    if report["failed_checks"]:
        print("\nFailed edge checks:")
        for failure in report["failed_checks"]:
            print(f"  - {failure}")
    print(f"\nReport: {REPORT_PATH}")
    print(f"Policy: {POLICY_PATH}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="redownload the broad panel")
    parser.add_argument(
        "--refresh-earnings", action="store_true", help="redownload earnings events"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run(refresh=args.refresh, refresh_earnings=args.refresh_earnings)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
