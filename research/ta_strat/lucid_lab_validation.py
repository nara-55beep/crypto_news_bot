"""Generate the tracked Lucid Strategy Lab evidence artifact from real local data.

The raw cache is intentionally gitignored.  This command reads it, applies the
causal signal and portfolio engines already audited in this repository, then
writes only a compact, source-hashed JSON report for the website.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import random
from statistics import mean, median
from typing import Iterable

import numpy as np

import lucid_causal_rebuild as L
import lucid_portfolio_policy as P
import lucid_predictive_research as R


RESEARCH_VERSION = "lucid_lab_conservative_proxy_v2"
SEED = 20260814
TEST_START = date(2024, 1, 1)
SAFETY_RESERVE = 100.0
MAX_TRADES_PER_DAY = 3
MAX_LOSSES_PER_DAY = 2
DAILY_PROFIT_LOCK = 600.0


PRESETS = {
    "normal": {"label": "Normal conservative", "spread": 1.0, "slippage": 1.0, "stop_extra": 0.0, "missed": 0.0},
    "spread_50": {"label": "Spread +50%", "spread": 1.5, "slippage": 1.0, "stop_extra": 0.0, "missed": 0.0},
    "spread_2x": {"label": "Spread doubled", "spread": 2.0, "slippage": 1.0, "stop_extra": 0.0, "missed": 0.0},
    "slippage_50": {"label": "Slippage +50%", "spread": 1.0, "slippage": 1.5, "stop_extra": 0.0, "missed": 0.0},
    "slippage_2x": {"label": "Slippage doubled", "spread": 1.0, "slippage": 2.0, "stop_extra": 0.0, "missed": 0.0},
    "volatile_open": {"label": "Volatile open", "spread": 2.0, "slippage": 3.0, "stop_extra": 1.0, "missed": 0.05},
    "low_liquidity": {"label": "Low liquidity", "spread": 2.0, "slippage": 2.0, "stop_extra": 1.0, "missed": 0.10},
    "delayed_stop": {"label": "Delayed stop", "spread": 1.0, "slippage": 1.0, "stop_extra": 1.0, "missed": 0.0},
    "gap_event": {"label": "Gap-through-stop event", "spread": 1.0, "slippage": 1.0, "stop_extra": 4.0, "missed": 0.0},
    "missed_trades": {"label": "10% missed entries", "spread": 1.0, "slippage": 1.0, "stop_extra": 0.0, "missed": 0.10},
    "poor_starts": {"label": "Worst historical start quartile", "spread": 1.0, "slippage": 1.0, "stop_extra": 0.0, "missed": 0.0},
    "severe": {"label": "Combined severe", "spread": 2.0, "slippage": 3.0, "stop_extra": 4.0, "missed": 0.20},
}


RULES = {
    "25K": P.AccountRules("25K", 1_250.0, 1_000.0, 1_100.0, 100.0, None, 20),
    "50K": P.RULES_50K,
    "100K": P.AccountRules("100K", 6_000.0, 3_000.0, 3_100.0, 100.0, 1_800.0, 60),
    "150K": P.AccountRules("150K", 9_000.0, 4_500.0, 4_600.0, 100.0, 2_700.0, 100),
}


POLICIES = {
    "25K": P.Policy(400.0, 100.0),
    "50K": P.Policy(800.0, 200.0),
    "100K": P.Policy(1_200.0, 300.0),
    "150K": P.Policy(1_800.0, 450.0),
}


def _stable_fraction(trade: L.Trade, seed: int = SEED) -> float:
    raw = f"{seed}|{trade.day}|{trade.strategy}|{trade.entry_ts}|{trade.market}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / float(2**64)


def _stress_trade(trade: L.Trade, preset: dict) -> L.Trade | None:
    if _stable_fraction(trade) < float(preset["missed"]):
        return None
    tick = L.MARKETS[trade.market]["tick"]
    pv = L.MARKETS[trade.market]["pv"]
    extra_ticks = float(preset["spread"] + preset["slippage"] - 2.0)
    if trade.reason == "stop":
        extra_ticks += float(preset["stop_extra"])
    extra = extra_ticks * tick * pv
    return replace(
        trade,
        gross_per_micro=trade.gross_per_micro - extra,
        risk_per_micro=trade.risk_per_micro + max(0.0, extra),
    )


def _stressed_days(days: list[date], trades: list[L.Trade], preset: dict) -> list[list[L.Trade]]:
    by_day = {day: [] for day in days}
    for trade in trades:
        stressed = _stress_trade(trade, preset)
        if stressed is not None and stressed.day in by_day:
            by_day[stressed.day].append(stressed)
    return [sorted(by_day[day], key=lambda t: (t.entry_ts, P.signal_priority(t), t.exit_ts)) for day in days]


@dataclass
class PathResult:
    outcome: str
    used_days: int
    terminal_profit: float
    max_drawdown: float
    trades: int
    gross_profit: float
    commissions: float
    spread_cost: float
    slippage_cost: float
    contract_cap_limited: int
    risk_rejected: int
    dll_blocked: int
    strategy_blocked: int
    open_equity_checks: int
    minimum_equity: float
    breach_reason: str
    timeline: list[dict]


@dataclass(frozen=True)
class MinuteBar:
    low: float
    high: float


class MinutePathStore:
    """Immutable minute bars used to mark every open position.

    Keys are UTC nanoseconds.  Keeping the market in the key matters because an
    ES low and an NQ high in the same minute must both affect shared-account
    equity when both positions are open.
    """

    def __init__(self, days: dict[str, list[L.Day]]):
        self._bars: dict[tuple[str, date], dict[int, MinuteBar]] = {}
        self._timestamps: dict[date, set[int]] = {}
        for market, rows in days.items():
            for row in rows:
                key = (market, row.day)
                if key in self._bars:
                    raise ValueError(f"duplicate {market} session {row.day}")
                if len(row.ts) != 390 or not np.array_equal(
                    row.minute, np.arange(390, dtype=np.int16)
                ):
                    raise ValueError(f"incomplete {market} session {row.day}")
                bars: dict[int, MinuteBar] = {}
                previous = -1
                for stamp, op, hi, lo, close, volume in zip(
                    row.ts, row.op, row.hi, row.lo, row.cl, row.vol
                ):
                    timestamp = pd_timestamp_ns(stamp)
                    values = (float(op), float(hi), float(lo), float(close), float(volume))
                    if not all(math.isfinite(value) for value in values):
                        raise ValueError(f"non-finite {market} bar at {stamp}")
                    if float(lo) > min(float(op), float(close)) or float(hi) < max(float(op), float(close)):
                        raise ValueError(f"invalid OHLC ordering in {market} at {stamp}")
                    if float(volume) < 0 or timestamp <= previous or timestamp in bars:
                        raise ValueError(f"invalid chronology in {market} at {stamp}")
                    bars[timestamp] = MinuteBar(float(lo), float(hi))
                    previous = timestamp
                self._bars[key] = bars
                self._timestamps.setdefault(row.day, set()).update(bars)

    def timestamps(self, session: date, markets: set[str]) -> list[int]:
        values: set[int] = set()
        for market in markets:
            values.update(self._bars.get((market, session), {}))
        return sorted(values)

    def bar(self, market: str, session: date, timestamp: int) -> MinuteBar:
        try:
            return self._bars[(market, session)][timestamp]
        except KeyError as exc:
            raise ValueError(
                f"missing mark bar for {market} {session} at {timestamp}"
            ) from exc


def pd_timestamp_ns(value) -> int:
    """Normalize pandas/numpy timestamps without relying on host timezone."""
    if hasattr(value, "value"):
        return int(value.value)
    return int(np.asarray(value).astype("datetime64[ns]").astype(np.int64))


def simulate_sequence(
    sequence: list[list[L.Trade]],
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
    *,
    price_paths: MinutePathStore,
    capture: bool = False,
    session_labels: list[date],
) -> PathResult:
    """Replay one evaluation with conservative intraminute open-equity marks.

    Entries occur at the minute open while positions whose exit is somewhere in
    that same candle still consume risk and contract capacity.  We then mark the
    shared account at the adverse side of every open position's one-minute bar.
    For a position that exits in that candle, its modeled executable exit is the
    worst pre-exit mark: using the candle extreme after a guaranteed stop would
    invent a loss that the stop already closed.  A drawdown-floor touch wins over
    a target in the same minute, because one-minute OHLC cannot prove ordering.
    """
    if len(sequence) != len(session_labels):
        raise ValueError("every replayed session needs an explicit source date")
    balance = 0.0
    eod_peak = 0.0
    equity_peak = 0.0
    floor = -rules.max_loss
    max_dd = 0.0
    minimum_equity = 0.0
    open_equity_checks = 0
    breach_reason = ""
    count = 0
    gross_profit = commission = spread = slippage = 0.0
    contract_cap_limited = risk_rejected = dll_blocked = strategy_blocked = 0
    timeline: list[dict] = []
    daily_history: list[float] = []
    outcome = "unfinished"

    for used, day_trades in enumerate(sequence, 1):
        session = session_labels[used - 1]
        if any(trade.day != session for trade in day_trades):
            raise ValueError(f"trade/session mismatch in replay session {session}")
        day_pnl = 0.0
        day_trade_count = 0
        day_losses = 0
        start_balance = balance
        positions: list[P.Position] = []
        markets = {trade.market for trade in day_trades}
        timestamp_values = price_paths.timestamps(session, markets)
        event_values = {
            pd_timestamp_ns(stamp)
            for trade in day_trades for stamp in (trade.entry_ts, trade.exit_ts)
        }
        if event_values - set(timestamp_values):
            raise ValueError(f"trade event has no source minute in session {session}")
        if event_values:
            first_event, last_event = min(event_values), max(event_values)
            timestamp_values = [
                value for value in timestamp_values
                if first_event <= value <= last_event
            ]

        for timestamp in timestamp_values:
            # Entries happen at the bar open.  A prior position whose stop/target
            # occurs later in this candle still occupies the shared risk cap.
            for trade in (
                t for t in day_trades if pd_timestamp_ns(t.entry_ts) == timestamp
            ):
                if rules.daily_loss_limit is not None and day_pnl <= -rules.daily_loss_limit:
                    dll_blocked += 1
                    continue
                if (
                    day_trade_count >= MAX_TRADES_PER_DAY
                    or day_losses >= MAX_LOSSES_PER_DAY
                    or day_pnl >= DAILY_PROFIT_LOCK
                    or any(
                        p.trade.market == trade.market and p.trade.side != trade.side
                        for p in positions
                    )
                ):
                    strategy_blocked += 1
                    continue
                all_in_risk = trade.risk_per_micro + L.COMMISSION_RT
                requested = int(math.floor(P._base_risk(trade, policy, balance) / all_in_risk))
                cap_room = rules.max_micros - sum(position.qty for position in positions)
                if requested > max(0, cap_room):
                    contract_cap_limited += 1
                reserved = sum(position.reserved_loss for position in positions)
                floor_room_cash = max(
                    0.0,
                    balance - floor - SAFETY_RESERVE - reserved - 0.01,
                )
                floor_room = int(math.floor(floor_room_cash / all_in_risk))
                qty = max(0, min(requested, cap_room, floor_room))
                if qty > 0:
                    positions.append(P.Position(trade, qty))
                    day_trade_count += 1
                else:
                    risk_rejected += 1

            if positions:
                open_equity_checks += 1
                marked_equity = balance
                for position in positions:
                    trade = position.trade
                    bar = price_paths.bar(trade.market, session, timestamp)
                    adverse = bar.low if trade.side > 0 else bar.high
                    tick_value = L.MARKETS[trade.market]["tick"] * L.MARKETS[trade.market]["pv"]
                    extra_ticks = max(
                        0.0,
                        float(preset["spread"]) + float(preset["slippage"]) - 2.0,
                    )
                    adverse_mark = (
                        trade.side
                        * (adverse - trade.entry)
                        * L.MARKETS[trade.market]["pv"]
                        - (1.0 + extra_ticks) * tick_value
                        - L.COMMISSION_RT
                    )
                    if pd_timestamp_ns(trade.exit_ts) != timestamp:
                        marked_per_micro = adverse_mark
                    elif trade.reason == "stop":
                        # The modeled stop closes the position before any later
                        # candle extreme can affect account equity.
                        marked_per_micro = trade.gross_per_micro - L.COMMISSION_RT
                    else:
                        # A target or EOD close does not prove the favorable exit
                        # preceded this candle's adverse extreme.
                        marked_per_micro = min(
                            adverse_mark,
                            trade.gross_per_micro - L.COMMISSION_RT,
                        )
                    marked_equity += marked_per_micro * position.qty
                minimum_equity = min(minimum_equity, marked_equity)
                max_dd = min(max_dd, marked_equity - equity_peak)
                if marked_equity <= floor:
                    outcome = "breach"
                    breach_reason = "intraday_open_equity_touched_eod_floor"
                    break

            exiting = [
                p for p in positions if pd_timestamp_ns(p.trade.exit_ts) == timestamp
            ]
            for match in exiting:
                positions.remove(match)
                trade = match.trade
                qty = match.qty
                net = (trade.gross_per_micro - L.COMMISSION_RT) * qty
                tick_value = L.MARKETS[trade.market]["tick"] * L.MARKETS[trade.market]["pv"]
                full_spread = float(preset["spread"]) * tick_value * qty
                full_slip = float(preset["slippage"] + (preset["stop_extra"] if trade.reason == "stop" else 0.0)) * tick_value * qty
                commission += L.COMMISSION_RT * qty
                spread += full_spread
                slippage += full_slip
                gross_profit += net + L.COMMISSION_RT * qty + full_spread + full_slip
                balance += net
                day_pnl += net
                if net < 0:
                    day_losses += 1
                count += 1
                equity_peak = max(equity_peak, balance)
                max_dd = min(max_dd, balance - equity_peak)
                minimum_equity = min(minimum_equity, balance)
                if balance <= floor:
                    outcome = "breach"
                    breach_reason = "realized_balance_touched_eod_floor"
                    break
                if balance >= rules.target_profit:
                    outcome = "pass"
                    break
            if outcome != "unfinished":
                break

        if positions and outcome == "unfinished":
            raise AssertionError("selected strategy left a position open past its session")
        daily_history.append(day_pnl)
        eod_peak = max(eod_peak, balance)
        floor = rules.locked_floor if eod_peak > rules.lock_trigger else max(floor, eod_peak - rules.max_loss)
        if balance <= floor and outcome == "unfinished":
            outcome = "breach"
        if capture:
            timeline.append({
                "day": used,
                "session": str(session),
                "starting_balance": round(rules.target_profit * 0 + start_balance + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "ending_balance": round(balance + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "daily_net_pnl": round(day_pnl, 2),
                "largest_profitable_day": round(max((value for value in daily_history if value > 0), default=0.0), 2),
                "consistency_pct": (
                    round(max((value for value in daily_history if value > 0), default=0.0) / balance * 100, 2)
                    if balance > 0 else None
                ),
                "drawdown_floor": round(floor + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "remaining_drawdown": round(balance - floor, 2),
                "permitted_micros": rules.max_micros,
                "permitted_contracts": f"{rules.max_micros // 10} minis / {rules.max_micros} micros",
                "warning": "terminal path" if outcome != "unfinished" else "",
                "status": outcome.upper() if outcome != "unfinished" else "ACTIVE",
            })
        if outcome != "unfinished":
            return PathResult(
                outcome, used, balance, max_dd, count, gross_profit, commission,
                spread, slippage, contract_cap_limited, risk_rejected, dll_blocked,
                strategy_blocked, open_equity_checks, minimum_equity, breach_reason,
                timeline,
            )
    return PathResult(
        outcome, len(sequence), balance, max_dd, count, gross_profit, commission,
        spread, slippage, contract_cap_limited, risk_rejected, dll_blocked,
        strategy_blocked, open_equity_checks, minimum_equity, breach_reason,
        timeline,
    )


def _binomial_cdf(k: int, n: int, probability: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(
        math.comb(n, value)
        * probability**value
        * (1.0 - probability) ** (n - value)
        for value in range(k + 1)
    )


def _clopper_pearson(success: int, total: int, alpha: float = 0.05) -> list[float]:
    """Exact binomial interval for non-overlapping evaluation blocks."""
    if total <= 0:
        return [0.0, 1.0]

    def solve(k: int, target: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(90):
            mid = (lo + hi) / 2.0
            if _binomial_cdf(k, total, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lower = 0.0 if success == 0 else solve(success - 1, 1.0 - alpha / 2.0)
    upper = 1.0 if success == total else solve(success, alpha / 2.0)
    return [round(lower, 6), round(upper, 6)]


def evaluate_sequences(
    daily: list[list[L.Trade]],
    session_labels: list[date],
    horizon: int,
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
    price_paths: MinutePathStore,
    *,
    stride: int | None = None,
) -> tuple[dict, list[PathResult]]:
    if len(daily) != len(session_labels):
        raise ValueError("daily trade baskets and session labels must align")
    step = horizon if stride is None else stride
    starts = range(0, max(0, len(daily) - horizon + 1), step)
    paths = [
        simulate_sequence(
            daily[i:i + horizon], policy, rules, preset,
            price_paths=price_paths,
            session_labels=session_labels[i:i + horizon],
        )
        for i in starts
    ]
    counts = Counter(path.outcome for path in paths)
    passes = [path.used_days for path in paths if path.outcome == "pass"]
    n = len(paths)
    pct = lambda key: counts[key] / n if n else 0.0
    durations = {
        "fastest": min(passes) if passes else None,
        "p10": float(np.percentile(passes, 10)) if passes else None,
        "p25": float(np.percentile(passes, 25)) if passes else None,
        "median": float(np.median(passes)) if passes else None,
        "mean": float(np.mean(passes)) if passes else None,
        "p75": float(np.percentile(passes, 75)) if passes else None,
        "p90": float(np.percentile(passes, 90)) if passes else None,
        "restricted_mean": float(np.mean([p.used_days if p.outcome == "pass" else horizon for p in paths])) if paths else None,
        "conditional_on_pass": True,
    }
    return ({
        "windows": n,
        "passes": counts["pass"],
        "breaches": counts["breach"],
        "unfinished": counts["unfinished"],
        "pass_rate": round(pct("pass"), 6),
        "breach_rate": round(pct("breach"), 6),
        "unfinished_rate": round(pct("unfinished"), 6),
        "pass_exact_binomial_95": _clopper_pearson(counts["pass"], n),
        "window_stride_sessions": step,
        "windows_overlap": step < horizon,
        "duration": durations,
        "median_max_drawdown": round(float(np.median([p.max_drawdown for p in paths])), 2) if paths else None,
        "worst_max_drawdown": round(min((p.max_drawdown for p in paths), default=0.0), 2),
        "median_terminal_profit": round(float(np.median([p.terminal_profit for p in paths])), 2) if paths else None,
    }, paths)


def monte_carlo(
    daily: list[list[L.Trade]],
    session_labels: list[date],
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
    price_paths: MinutePathStore,
    *,
    horizon: int = 45,
    paths: int = 1000,
    block: int = 20,
) -> dict:
    """Circular block-resample historical sessions and replay account state."""
    rng = random.Random(SEED)
    outcomes: list[PathResult] = []
    for _ in range(paths):
        sequence: list[list[L.Trade]] = []
        labels: list[date] = []
        while len(sequence) < horizon:
            start = rng.randrange(len(daily))
            for offset in range(block):
                index = (start + offset) % len(daily)
                sequence.append(daily[index])
                labels.append(session_labels[index])
        outcomes.append(simulate_sequence(
            sequence[:horizon], policy, rules, preset,
            price_paths=price_paths,
            session_labels=labels[:horizon],
        ))
    counts = Counter(row.outcome for row in outcomes)
    terminal = np.array([row.terminal_profit for row in outcomes], dtype=float)
    pass_days = [row.used_days for row in outcomes if row.outcome == "pass"]
    counts_hist, edges_hist = np.histogram(terminal, bins=16)
    return {
        "method": "20-session circular block resampling with minute-marked shared-account replay",
        "seed": SEED,
        "paths": paths,
        "horizon_sessions": horizon,
        "pass_rate": round(counts["pass"] / paths, 6),
        "breach_rate": round(counts["breach"] / paths, 6),
        "unfinished_rate": round(counts["unfinished"] / paths, 6),
        "terminal_profit_p05": round(float(np.percentile(terminal, 5)), 2),
        "terminal_profit_p25": round(float(np.percentile(terminal, 25)), 2),
        "terminal_profit_median": round(float(np.median(terminal)), 2),
        "terminal_profit_p75": round(float(np.percentile(terminal, 75)), 2),
        "terminal_profit_p95": round(float(np.percentile(terminal, 95)), 2),
        "median_days_conditional_on_pass": float(np.median(pass_days)) if pass_days else None,
        "terminal_profit_histogram": [
            {"lo": round(float(edges_hist[i]), 2), "hi": round(float(edges_hist[i + 1]), 2), "count": int(counts_hist[i])}
            for i in range(len(counts_hist))
        ],
        "warning": "Resampling quantifies historical path variation; it cannot repair proxy-data or model error.",
    }


def _trade_stats(trades: list[L.Trade], policy: P.Policy, preset: dict) -> dict:
    rows = []
    commissions = spread = slippage = gross_signed = positive_gross = 0.0
    daily_net: dict[str, float] = {}
    monthly_net: dict[str, float] = {}
    yearly_trades: Counter[str] = Counter()
    for trade in trades:
        stressed = _stress_trade(trade, preset)
        if stressed is None:
            continue
        risk = policy.morning_risk if "morning" in trade.strategy else policy.prior_risk
        qty = min(20, int(math.floor(risk / (stressed.risk_per_micro + L.COMMISSION_RT))))
        if qty < 1:
            continue
        net = (stressed.gross_per_micro - L.COMMISSION_RT) * qty
        tick_value = L.MARKETS[trade.market]["tick"] * L.MARKETS[trade.market]["pv"]
        c = L.COMMISSION_RT * qty
        sp = float(preset["spread"]) * tick_value * qty
        sl = float(preset["slippage"] + (preset["stop_extra"] if trade.reason == "stop" else 0.0)) * tick_value * qty
        rows.append(net)
        day_key, month_key, year_key = str(trade.day), str(trade.day)[:7], str(trade.day)[:4]
        daily_net[day_key] = daily_net.get(day_key, 0.0) + net
        monthly_net[month_key] = monthly_net.get(month_key, 0.0) + net
        yearly_trades[year_key] += 1
        commissions += c
        spread += sp
        slippage += sl
        trade_gross = net + c + sp + sl
        gross_signed += trade_gross
        positive_gross += max(0.0, trade_gross)
    pos = sum(value for value in rows if value > 0)
    neg = -sum(value for value in rows if value <= 0)
    wins = [value for value in rows if value > 0]
    losses = [value for value in rows if value <= 0]
    curve = np.cumsum(rows) if rows else np.array([])
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1] if rows else np.array([])
    positive_days = [value for value in daily_net.values() if value > 0]
    positive_months = [value for value in monthly_net.values() if value > 0]
    return {
        "trades": len(rows),
        "net": round(sum(rows), 2),
        "expectancy": round(mean(rows), 2) if rows else 0.0,
        "profit_factor": round(pos / neg, 4) if neg else None,
        "win_rate": round(len(wins) / len(rows), 6) if rows else 0.0,
        "average_win": round(mean(wins), 2) if wins else None,
        "average_loss": round(mean(losses), 2) if losses else None,
        "payoff_ratio": round(mean(wins) / abs(mean(losses)), 4) if wins and losses else None,
        "max_drawdown": round(float(np.min(curve - peaks)), 2) if rows else 0.0,
        "commissions": round(commissions, 2),
        "spread_cost": round(spread, 2),
        "slippage_cost": round(slippage, 2),
        "gross_before_costs": round(gross_signed, 2),
        "positive_gross_profit": round(positive_gross, 2),
        "cost_pct_of_positive_gross": round((commissions + spread + slippage) / positive_gross * 100, 2) if positive_gross > 0 else None,
        "concentration": {
            "largest_winning_trade_share_pct": round(max(wins, default=0.0) / sum(wins) * 100, 2) if wins else None,
            "largest_positive_day_share_pct": round(max(positive_days, default=0.0) / sum(positive_days) * 100, 2) if positive_days else None,
            "largest_positive_month_share_pct": round(max(positive_months, default=0.0) / sum(positive_months) * 100, 2) if positive_months else None,
            "trades_by_year": dict(sorted(yearly_trades.items())),
        },
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _data_manifest(cache: Path, days: dict[str, list[L.Day]]) -> list[dict]:
    result = []
    for market in ("es", "nq"):
        files = []
        for suffix in ("1m_10y.csv", "1m_3y.csv", "1m_rth_repair.csv"):
            path = cache / f"{market}_{suffix}"
            if path.exists():
                files.append({"name": path.name, "bytes": path.stat().st_size, "sha256": _hash_file(path)})
        market_days = days[market]
        result.append({
            "market": market.upper(),
            "proxy_symbol": "USA500IDXUSD" if market == "es" else "USATECHIDXUSD",
            "first_session": str(market_days[0].day),
            "last_session": str(market_days[-1].day),
            "accepted_complete_sessions": len(market_days),
            "files": files,
        })
    return result


def build_report(cache: Path) -> dict:
    L.CACHE = str(cache)
    days = {market: L.load_days(market) for market in ("nq", "es")}
    price_paths = MinutePathStore(days)
    all_trades = P.selected_signals(days)
    all_days = sorted({row.day for rows in days.values() for row in rows})
    test_days = [day for day in all_days if day >= TEST_START]
    test_trades = [trade for trade in all_trades if trade.day >= TEST_START]
    normal_daily = _stressed_days(test_days, test_trades, PRESETS["normal"])

    account_comparison = []
    for name in ("25K", "50K", "100K", "150K"):
        rules, policy = RULES[name], POLICIES[name]
        row = {"account_size": name, "target_to_drawdown": rules.target_profit / rules.max_loss}
        for horizon in (20, 30, 45):
            result, _ = evaluate_sequences(
                normal_daily, test_days, horizon, policy, rules,
                PRESETS["normal"], price_paths,
            )
            row[f"h{horizon}"] = result
        account_comparison.append(row)

    selected_rules = RULES["25K"]
    selected_policy = POLICIES["25K"]
    horizons = {}
    normal_paths: list[PathResult] = []
    for horizon in (20, 30, 45):
        result, paths = evaluate_sequences(
            normal_daily, test_days, horizon, selected_policy, selected_rules,
            PRESETS["normal"], price_paths,
        )
        horizons[str(horizon)] = result
        if horizon == 45:
            normal_paths = paths
    rolling_result, rolling_paths = evaluate_sequences(
        normal_daily, test_days, 45, selected_policy, selected_rules,
        PRESETS["normal"], price_paths, stride=1,
    )
    horizons["45"]["overlapping_diagnostic"] = rolling_result

    split_results = {}
    for split_name, lo, hi in (
        ("development", None, L.TRAIN_END),
        ("validation", date(2022, 1, 1), L.VALID_END),
        ("chronological_test", TEST_START, None),
    ):
        split_days = [day for day in all_days if (lo is None or day >= lo) and (hi is None or day <= hi)]
        split_trades = [trade for trade in all_trades if (lo is None or trade.day >= lo) and (hi is None or trade.day <= hi)]
        split_daily = _stressed_days(split_days, split_trades, PRESETS["normal"])
        split_results[split_name], _ = evaluate_sequences(
            split_daily, split_days, 45, selected_policy, selected_rules,
            PRESETS["normal"], price_paths,
        )

    stresses = []
    for key, preset in PRESETS.items():
        stressed_daily = _stressed_days(test_days, test_trades, preset)
        result, paths = evaluate_sequences(
            stressed_daily, test_days, 45, selected_policy, selected_rules,
            preset, price_paths,
        )
        if key == "poor_starts" and paths:
            ranked = sorted(paths, key=lambda p: p.terminal_profit)
            poor = ranked[: max(1, len(ranked) // 4)]
            counts = Counter(path.outcome for path in poor)
            n = len(poor)
            result = {
                **result,
                "windows": n,
                "passes": counts["pass"],
                "breaches": counts["breach"],
                "unfinished": counts["unfinished"],
                "pass_rate": round(counts["pass"] / n, 6),
                "breach_rate": round(counts["breach"] / n, 6),
                "unfinished_rate": round(counts["unfinished"] / n, 6),
                "selection": "lowest terminal-profit quartile of normal historical starts",
            }
        stresses.append({"id": key, "label": preset["label"], **result})

    drive = [t for t in test_trades if t.market == "nq" and "morning" in t.strategy]
    gap = [t for t in test_trades if t.market == "es" and "morning" in t.strategy]
    prior = [t for t in test_trades if "prior_breakout" in t.strategy]
    subsets = {
        "nq_drive": drive,
        "es_gap_fill": gap,
        "nq_prior_breakout": prior,
        "selected_portfolio": test_trades,
    }
    candidates = []
    reasons = {
        "selected_portfolio": "Best balance of frequency and development-selected sleeves; still proxy evidence only.",
        "nq_drive": "Positive sparse sleeve, but too many unfinished evaluation windows alone.",
        "es_gap_fill": "Diversifies NQ but is too sparse alone.",
        "nq_prior_breakout": "Adds frequency at lower risk; weaker standalone payoff and cost sensitivity.",
    }
    for key, trades in subsets.items():
        daily = _stressed_days(test_days, trades, PRESETS["normal"])
        normal, _ = evaluate_sequences(
            daily, test_days, 45, selected_policy, selected_rules,
            PRESETS["normal"], price_paths,
        )
        severe_daily = _stressed_days(test_days, trades, PRESETS["severe"])
        severe, _ = evaluate_sequences(
            severe_daily, test_days, 45, selected_policy, selected_rules,
            PRESETS["severe"], price_paths,
        )
        stats = _trade_stats(trades, selected_policy, PRESETS["normal"])
        candidates.append({
            "id": key,
            "name": {
                "selected_portfolio": "Three-sleeve causal portfolio",
                "nq_drive": "MNQ 09:45 opening drive",
                "es_gap_fill": "MES 09:45 gap fill",
                "nq_prior_breakout": "MNQ prior-range breakout",
            }[key],
            "strategy_version": "lucid_lab_portfolio_v1" if key == "selected_portfolio" else f"{key}_standalone_v1",
            "instrument": "MES + MNQ" if key == "selected_portfolio" else ("MES" if key == "es_gap_fill" else "MNQ"),
            "account": "LucidPro 25K evaluation",
            "trades": stats["trades"],
            "windows": normal["windows"],
            "net_expectancy": stats["expectancy"],
            "profit_factor": stats["profit_factor"],
            "normal_pass_rate": normal["pass_rate"],
            "stressed_pass_rate": severe["pass_rate"],
            "breach_rate": normal["breach_rate"],
            "median_pass_days": normal["duration"]["median"],
            "max_drawdown": normal["worst_max_drawdown"],
            "parameter_stability": "moderate" if key == "selected_portfolio" else "limited",
            "cost_sensitivity": round(normal["pass_rate"] - severe["pass_rate"], 6),
            "validation_status": "NO_GO_PROXY" if key == "selected_portfolio" else "REJECTED_STANDALONE",
            "reason": reasons[key],
        })
    candidates.append({
        "id": "original_five_basket",
        "name": "Original VWAP/Turtle/NR7 five-basket",
        "strategy_version": "invalidated_legacy_v0",
        "instrument": "MES + MNQ + MCL",
        "account": "LucidPro 50K evaluation",
        "trades": 7839,
        "windows": None,
        "net_expectancy": None,
        "profit_factor": 1.02,
        "normal_pass_rate": 0.42,
        "stressed_pass_rate": None,
        "breach_rate": None,
        "median_pass_days": None,
        "max_drawdown": -86648.0,
        "parameter_stability": "failed causal rebuild",
        "cost_sensitivity": None,
        "validation_status": "INVALIDATED",
        "reason": "The attractive 98% result used future information and optimistic fills; causal repair collapsed it.",
        "source": "research/ta_strat/LUCID_CAUSAL_REBUILD_REPORT.md",
    })

    # Do not search nearby parameters on the already-inspected test era.  The old
    # report displayed 21 alternatives, which turned a confirmatory slice into
    # another implicit optimization set.  Preserve only the frozen specification.
    sensitivity = [{
        "morning_risk": 400.0,
        "prior_risk": 100.0,
        "pass_rate": horizons["45"]["pass_rate"],
        "breach_rate": horizons["45"]["breach_rate"],
        "unfinished_rate": horizons["45"]["unfinished_rate"],
        "selected": True,
        "note": "No alternatives rerun on the confirmatory test era.",
    }]
    frozen_stats = _trade_stats(test_trades, selected_policy, PRESETS["normal"])
    signal_sensitivity = [{
        "dimension": "frozen_three_sleeve_specification",
        "value": "v1",
        "selected": True,
        "trades": frozen_stats["trades"],
        "expectancy": frozen_stats["expectancy"],
        "pass_rate": horizons["45"]["pass_rate"],
        "breach_rate": horizons["45"]["breach_rate"],
        "unfinished_rate": horizons["45"]["unfinished_rate"],
        "note": "Neighboring parameters deliberately not retested after the measurement repair.",
    }]

    walk_forward = []
    for year in range(2022, 2027):
        year_days = [day for day in all_days if day.year == year]
        year_trades = [trade for trade in all_trades if trade.day.year == year]
        year_daily = _stressed_days(year_days, year_trades, PRESETS["normal"])
        result, _ = evaluate_sequences(
            year_daily, year_days, 45, selected_policy, selected_rules,
            PRESETS["normal"], price_paths,
        )
        walk_forward.append({"year": year, **result})

    pass_paths = [p for p in normal_paths if p.outcome == "pass"]
    representative = min(pass_paths, key=lambda p: abs(p.used_days - (median([x.used_days for x in pass_paths]) if pass_paths else 0))) if pass_paths else None
    representative_timeline = []
    if representative is not None:
        index = normal_paths.index(representative) * 45
        representative = simulate_sequence(
            normal_daily[index:index + 45], selected_policy, selected_rules,
            PRESETS["normal"], price_paths=price_paths, capture=True,
            session_labels=test_days[index:index + 45],
        )
        representative_timeline = representative.timeline

    rolling = []
    width = 63
    for start in range(0, len(rolling_paths), width):
        batch = rolling_paths[start:start + width]
        if not batch:
            continue
        rolling.append({
            "start_session": str(test_days[start]),
            "end_session": str(test_days[min(len(test_days) - 1, start + len(batch) - 1)]),
            "pass_rate": round(sum(p.outcome == "pass" for p in batch) / len(batch), 6),
            "breach_rate": round(sum(p.outcome == "breach" for p in batch) / len(batch), 6),
        })

    duration_hist = Counter(path.used_days for path in pass_paths)
    terminal = [p.terminal_profit for p in normal_paths]
    terminal_bins = []
    if terminal:
        counts, edges = np.histogram(terminal, bins=12)
        terminal_bins = [{"lo": round(float(edges[i]), 2), "hi": round(float(edges[i + 1]), 2), "count": int(counts[i])} for i in range(len(counts))]

    stats = _trade_stats(test_trades, selected_policy, PRESETS["normal"])
    mc = monte_carlo(
        normal_daily, test_days, selected_policy, selected_rules,
        PRESETS["normal"], price_paths,
    )
    actual_gap_stops = 0
    for trade in test_trades:
        tick = L.MARKETS[trade.market]["tick"]
        if trade.reason != "stop":
            continue
        if (trade.side > 0 and trade.exit < trade.stop - tick) or (
            trade.side < 0 and trade.exit > trade.stop + tick
        ):
            actual_gap_stops += 1
    risk_controls = {
        "basis": "counts across non-overlapping 45-session primary test paths",
        "contract_cap_limits": sum(path.contract_cap_limited for path in normal_paths),
        "risk_rejections": sum(path.risk_rejected for path in normal_paths),
        "daily_loss_blocks": sum(path.dll_blocked for path in normal_paths),
        "strategy_rule_blocks": sum(path.strategy_blocked for path in normal_paths),
        "open_equity_checks": sum(path.open_equity_checks for path in normal_paths),
        "intraday_open_equity_breaches": sum(
            path.breach_reason == "intraday_open_equity_touched_eod_floor"
            for path in normal_paths
        ),
        "historical_gap_through_stops_in_raw_test_signals": actual_gap_stops,
    }
    validation_gates = [
        {
            "id": "minute_open_equity",
            "passed": True,
            "detail": "Every open position is marked each source minute; an equity touch of the prior-EOD floor is a breach.",
        },
        {
            "id": "non_overlapping_primary_windows",
            "passed": True,
            "detail": "Primary probabilities use disjoint 45-session blocks; overlapping starts are diagnostic only.",
        },
        {
            "id": "exchange_grade_market_data",
            "passed": False,
            "detail": "Dukascopy index/CFD proxy OHLC is not CME MES/MNQ bid/ask, trades, contract rolls, or queue data.",
        },
        {
            "id": "pristine_out_of_sample",
            "passed": False,
            "detail": "The 2024+ period and nearby parameter variants were already inspected before this rebuild.",
        },
        {
            "id": "observed_execution_costs",
            "passed": False,
            "detail": "Commission is sourced, but historical spread and slippage are modeled rather than observed fills.",
        },
        {
            "id": "point_in_time_event_filter",
            "passed": False,
            "detail": "No point-in-time economic-calendar archive is applied to historical signals.",
        },
        {
            "id": "decision_precision",
            "passed": (
                horizons["45"]["pass_exact_binomial_95"][1]
                - horizons["45"]["pass_exact_binomial_95"][0]
            ) <= 0.20,
            "detail": (
                f"Only {horizons['45']['windows']} non-overlapping blocks are available; "
                f"the exact pass interval is {horizons['45']['pass_exact_binomial_95'][0]:.1%}–"
                f"{horizons['45']['pass_exact_binomial_95'][1]:.1%}, wider than the 20-point decision-precision limit."
            ),
        },
    ]
    report = {
        "schema_version": 1,
        "research_version": RESEARCH_VERSION,
        "generated_at": "2026-08-15",
        "seed": SEED,
        "implementation_manifest": [
            {
                "name": path.name,
                "sha256": _hash_file(path),
            }
            for path in (
                Path(__file__).resolve(),
                Path(L.__file__).resolve(),
                Path(P.__file__).resolve(),
                Path(R.__file__).resolve(),
            )
        ],
        "status": "NO_GO",
        "status_label": "No-go — conservative proxy evidence is not decision-grade",
        "verdict": {
            "decision": "DO_NOT_BUY_OR_TRADE_FROM_THIS_BACKTEST",
            "reason": "The replay is now conservative about account-rule breaches, but the source and sample cannot validate a real Lucid pass edge.",
            "strategy_parameters_frozen_before_rebuild": True,
            "validation_gates": validation_gates,
            "failed_gate_count": sum(not row["passed"] for row in validation_gates),
        },
        "selected": {
            "strategy_id": "three_sleeve_causal_portfolio",
            "strategy_version": "lucid_lab_portfolio_v1",
            "program": "LucidPro",
            "stage": "evaluation",
            "account_size": 25000,
            "instrument": "MES + MNQ",
            "evaluation_horizon_sessions": 45,
            "morning_risk_usd": 400,
            "prior_breakout_risk_usd": 100,
            "safety_reserve_usd": 100,
            "maximum_trades_per_day": 3,
            "maximum_losing_trades_per_day": 2,
            "daily_profit_lock_usd": 600,
            "forced_close_ny": "16:00",
            "event_filter": "Not applied historically: no point-in-time high-impact calendar archive is available.",
            "why_selected": "Frozen pre-rebuild portfolio. It was not retuned after adding intraday open-equity accounting.",
            "limitations": [
                "Dukascopy CFD/index proxy bars, not CME futures bid/ask or queue data.",
                "The 2024+ period has been inspected in prior research and is confirmatory rather than pristine.",
                "Historical execution costs are conservative assumptions, not observed fills.",
                "Only disjoint 45-session blocks are used for the primary rate, leaving a small independent sample.",
                "The historical replay has no point-in-time economic-event filter.",
            ],
        },
        "strategy_rules": [
            {"sleeve": "MNQ opening drive", "setup": "At 09:44 NY, the first 15-minute close must move at least 25% of the prior RTH range from today's open and close in the outer 20% of today's opening range in that direction.", "entry": "Marketable entry at the 09:45 one-minute open plus modeled adverse execution.", "stop": "Today's 09:30 open.", "target": "2.0R; stop-first on an ambiguous one-minute bar."},
            {"sleeve": "MES gap fill", "setup": "At 09:44 NY, the overnight gap must be at least 10% of the prior RTH range, the first 15 minutes must move against the gap, and the close must be in the outer 20% of the opening range toward the fill.", "entry": "Marketable entry at the 09:45 one-minute open plus modeled adverse execution.", "stop": "One tick beyond the first 15-minute range extreme.", "target": "1.5R; stop-first on an ambiguous one-minute bar."},
            {"sleeve": "MNQ prior-range breakout", "setup": "On a completed clock-aligned 15-minute bar, close crosses beyond today's open ±25% of the prior RTH range.", "entry": "Next one-minute open plus modeled adverse execution.", "stop": "One tick beyond the signal 15-minute bar extreme.", "target": "2.0R; at most the first valid signal of the session."},
        ],
        "data": {
            "source": "Dukascopy one-minute CFD/index proxy cache",
            "timezone": "UTC input; America/New_York session",
            "test_start": str(test_days[0]),
            "test_end": str(test_days[-1]),
            "test_sessions": len(test_days),
            "test_trades_raw": len(test_trades),
            "resolution": "1 minute",
            "manifest": _data_manifest(cache, days),
            "integrity": {
                "complete_rth_minutes_required": 390,
                "ohlc_and_chronology_validated": True,
                "duplicate_session_rejected": True,
                "decision_grade": False,
                "reason": "Structurally valid proxy bars are still not exchange-grade futures execution data.",
            },
        },
        "account_comparison": account_comparison,
        "split_results": split_results,
        "horizons": horizons,
        "trade_statistics": stats,
        "risk_controls": risk_controls,
        "stresses": stresses,
        "monte_carlo": mc,
        "candidates": candidates,
        "sensitivity": sensitivity,
        "signal_sensitivity": signal_sensitivity,
        "walk_forward": walk_forward,
        "charts": {
            "representative_timeline": representative_timeline,
            "rolling_pass_probability": rolling,
            "pass_duration_histogram": [{"days": day, "count": duration_hist[day]} for day in sorted(duration_hist)],
            "terminal_profit_histogram": terminal_bins,
            "outcome_distribution": [
                {"name": "Pass", "count": horizons["45"]["passes"]},
                {"name": "Breach", "count": horizons["45"]["breaches"]},
                {"name": "Unfinished", "count": horizons["45"]["unfinished"]},
            ],
            "cost_breakdown": [
                {"name": "Commission", "value": stats["commissions"]},
                {"name": "Spread", "value": stats["spread_cost"]},
                {"name": "Slippage", "value": stats["slippage_cost"]},
            ],
        },
        "methodology": {
            "development": "first accepted session through 2021-12-31",
            "validation": "2022-01-01 through 2023-12-31",
            "test": f"2024-01-01 through {test_days[-1]}",
            "primary_windows": "non-overlapping 45-session blocks beginning at the first test session",
            "rolling_starts": "diagnostic only; never used as the headline probability",
            "no_trade_sessions_in_denominator": True,
            "fill": "completed signal bar; next-minute open; one adverse tick entry and exit; stop-first ambiguity; gap-worse stop; every open position marked at the adverse one-minute extreme; floor touch fails",
            "commission": "$0.50 per side per micro ($1.00 round turn)",
            "uncertainty": "Exact binomial interval on disjoint blocks plus 20-session circular block resampling; neither repairs proxy-data model risk.",
            "selection_warning": "The strategy and account-size choice predate this corrected replay, but the test era has already been inspected; this is confirmatory, not pristine OOS.",
        },
    }
    raw = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report["run_id"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path(L.CACHE))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.cache_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, args.output)
    print(json.dumps({
        "run_id": report["run_id"],
        "test_sessions": report["data"]["test_sessions"],
        "test_trades": report["data"]["test_trades_raw"],
        "pass_45": report["horizons"]["45"]["pass_rate"],
        "breach_45": report["horizons"]["45"]["breach_rate"],
        "status": report["status"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
