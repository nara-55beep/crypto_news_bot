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


RESEARCH_VERSION = "lucid_lab_proxy_research_v1"
SEED = 20260814
TEST_START = date(2024, 1, 1)


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
    timeline: list[dict]


def simulate_sequence(
    sequence: list[list[L.Trade]],
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
    *,
    capture: bool = False,
    session_labels: list[date] | None = None,
) -> PathResult:
    """Portfolio simulation matching `P.simulate_window`, with audit details."""
    balance = 0.0
    eod_peak = 0.0
    equity_peak = 0.0
    floor = -rules.max_loss
    max_dd = 0.0
    count = 0
    gross_profit = commission = spread = slippage = 0.0
    contract_cap_limited = risk_rejected = dll_blocked = 0
    timeline: list[dict] = []
    outcome = "unfinished"

    for used, day_trades in enumerate(sequence, 1):
        day_pnl = 0.0
        start_balance = balance
        positions: list[P.Position] = []
        timestamps = sorted({t.entry_ts for t in day_trades} | {t.exit_ts for t in day_trades})
        for timestamp in timestamps:
            exiting = [p for p in positions if p.trade.exit_ts == timestamp and p.trade.entry_ts < timestamp]
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
                count += 1
                equity_peak = max(equity_peak, balance)
                max_dd = min(max_dd, balance - equity_peak)
                if balance <= floor:
                    outcome = "breach"
                    break
                if balance >= rules.target_profit:
                    outcome = "pass"
                    break
            if outcome != "unfinished":
                break

            for trade in (t for t in day_trades if t.entry_ts == timestamp):
                if rules.daily_loss_limit is not None and day_pnl <= -rules.daily_loss_limit:
                    dll_blocked += 1
                    continue
                all_in_risk = trade.risk_per_micro + L.COMMISSION_RT
                requested = int(math.floor(P._base_risk(trade, policy, balance) / all_in_risk))
                cap_room = rules.max_micros - sum(position.qty for position in positions)
                if requested > max(0, cap_room):
                    contract_cap_limited += 1
                qty = P._entry_qty(trade, policy, balance, floor, positions, rules)
                if qty > 0:
                    positions.append(P.Position(trade, qty))
                else:
                    risk_rejected += 1

            immediate = [p for p in positions if p.trade.exit_ts == timestamp and p.trade.entry_ts == timestamp]
            for match in immediate:
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
                count += 1
                equity_peak = max(equity_peak, balance)
                max_dd = min(max_dd, balance - equity_peak)
                if balance <= floor:
                    outcome = "breach"
                    break
                if balance >= rules.target_profit:
                    outcome = "pass"
                    break
            if outcome != "unfinished":
                break

        if positions and outcome == "unfinished":
            raise AssertionError("selected strategy left a position open past its session")
        eod_peak = max(eod_peak, balance)
        floor = rules.locked_floor if eod_peak > rules.lock_trigger else max(floor, eod_peak - rules.max_loss)
        if balance <= floor and outcome == "unfinished":
            outcome = "breach"
        if capture:
            session = (
                str(session_labels[used - 1])
                if session_labels is not None and used <= len(session_labels)
                else (str(day_trades[0].day) if day_trades else f"session-{used}")
            )
            timeline.append({
                "day": used,
                "session": session,
                "starting_balance": round(rules.target_profit * 0 + start_balance + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "ending_balance": round(balance + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "daily_net_pnl": round(day_pnl, 2),
                "drawdown_floor": round(floor + (25_000 if rules.name == "25K" else 50_000 if rules.name == "50K" else 100_000 if rules.name == "100K" else 150_000), 2),
                "remaining_drawdown": round(balance - floor, 2),
                "permitted_micros": rules.max_micros,
                "warning": "terminal path" if outcome != "unfinished" else "",
                "status": outcome.upper() if outcome != "unfinished" else "ACTIVE",
            })
        if outcome != "unfinished":
            return PathResult(
                outcome, used, balance, max_dd, count, gross_profit, commission,
                spread, slippage, contract_cap_limited, risk_rejected, dll_blocked,
                timeline,
            )
    return PathResult(
        outcome, len(sequence), balance, max_dd, count, gross_profit, commission,
        spread, slippage, contract_cap_limited, risk_rejected, dll_blocked,
        timeline,
    )


def _wilson(success: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    p = success / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)]


def evaluate_sequences(
    daily: list[list[L.Trade]],
    horizon: int,
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
) -> tuple[dict, list[PathResult]]:
    paths = [simulate_sequence(daily[i:i + horizon], policy, rules, preset) for i in range(max(0, len(daily) - horizon + 1))]
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
        "pass_wilson_95": _wilson(counts["pass"], n),
        "duration": durations,
        "median_max_drawdown": round(float(np.median([p.max_drawdown for p in paths])), 2) if paths else None,
        "worst_max_drawdown": round(min((p.max_drawdown for p in paths), default=0.0), 2),
        "median_terminal_profit": round(float(np.median([p.terminal_profit for p in paths])), 2) if paths else None,
    }, paths)


def _block_bootstrap_interval(values: list[int], *, block: int = 20, reps: int = 2000) -> list[float]:
    if not values:
        return [0.0, 0.0]
    rng = random.Random(SEED)
    n = len(values)
    means = []
    for _ in range(reps):
        sample: list[int] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + j) % n] for j in range(block))
        means.append(sum(sample[:n]) / n)
    return [round(float(np.percentile(means, 2.5)), 6), round(float(np.percentile(means, 97.5)), 6)]


def monte_carlo(
    daily: list[list[L.Trade]],
    policy: P.Policy,
    rules: P.AccountRules,
    preset: dict,
    *,
    horizon: int = 45,
    paths: int = 5000,
    block: int = 5,
) -> dict:
    """Circular block-resample historical sessions and replay account state."""
    rng = random.Random(SEED)
    outcomes: list[PathResult] = []
    for _ in range(paths):
        sequence: list[list[L.Trade]] = []
        while len(sequence) < horizon:
            start = rng.randrange(len(daily))
            sequence.extend(daily[(start + offset) % len(daily)] for offset in range(block))
        outcomes.append(simulate_sequence(sequence[:horizon], policy, rules, preset))
    counts = Counter(row.outcome for row in outcomes)
    terminal = np.array([row.terminal_profit for row in outcomes], dtype=float)
    pass_days = [row.used_days for row in outcomes if row.outcome == "pass"]
    return {
        "method": "5-session circular block resampling with full path-dependent account replay",
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
        "warning": "Resampling quantifies historical path variation; it cannot repair proxy-data or model error.",
    }


def _trade_stats(trades: list[L.Trade], policy: P.Policy, preset: dict) -> dict:
    rows = []
    commissions = spread = slippage = gross = 0.0
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
        commissions += c
        spread += sp
        slippage += sl
        gross += net + c + sp + sl
    pos = sum(value for value in rows if value > 0)
    neg = -sum(value for value in rows if value <= 0)
    wins = [value for value in rows if value > 0]
    losses = [value for value in rows if value <= 0]
    curve = np.cumsum(rows) if rows else np.array([])
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1] if rows else np.array([])
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
        "gross_before_costs": round(gross, 2),
        "cost_pct_of_positive_gross": round((commissions + spread + slippage) / gross * 100, 2) if gross > 0 else None,
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
            result, _ = evaluate_sequences(normal_daily, horizon, policy, rules, PRESETS["normal"])
            row[f"h{horizon}"] = result
        account_comparison.append(row)

    selected_rules = RULES["25K"]
    selected_policy = POLICIES["25K"]
    horizons = {}
    normal_paths: list[PathResult] = []
    for horizon in (20, 30, 45):
        result, paths = evaluate_sequences(normal_daily, horizon, selected_policy, selected_rules, PRESETS["normal"])
        horizons[str(horizon)] = result
        if horizon == 45:
            normal_paths = paths
            result["pass_block_bootstrap_95"] = _block_bootstrap_interval([1 if p.outcome == "pass" else 0 for p in paths])

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
            split_daily, 45, selected_policy, selected_rules, PRESETS["normal"]
        )

    stresses = []
    for key, preset in PRESETS.items():
        stressed_daily = _stressed_days(test_days, test_trades, preset)
        result, paths = evaluate_sequences(stressed_daily, 45, selected_policy, selected_rules, preset)
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
        normal, _ = evaluate_sequences(daily, 45, selected_policy, selected_rules, PRESETS["normal"])
        severe_daily = _stressed_days(test_days, trades, PRESETS["severe"])
        severe, _ = evaluate_sequences(severe_daily, 45, selected_policy, selected_rules, PRESETS["severe"])
        stats = _trade_stats(trades, selected_policy, PRESETS["normal"])
        candidates.append({
            "id": key,
            "name": {
                "selected_portfolio": "Three-sleeve causal portfolio",
                "nq_drive": "MNQ 09:45 opening drive",
                "es_gap_fill": "MES 09:45 gap fill",
                "nq_prior_breakout": "MNQ prior-range breakout",
            }[key],
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
            "validation_status": "EXPERIMENTAL_PROXY" if key == "selected_portfolio" else "REJECTED_STANDALONE",
            "reason": reasons[key],
        })
    candidates.append({
        "id": "original_five_basket",
        "name": "Original VWAP/Turtle/NR7 five-basket",
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

    sensitivity = []
    for morning in (300.0, 350.0, 400.0, 450.0, 500.0):
        for prior_risk in (75.0, 100.0, 125.0):
            policy = P.Policy(morning, prior_risk)
            result, _ = evaluate_sequences(normal_daily, 45, policy, selected_rules, PRESETS["normal"])
            sensitivity.append({
                "morning_risk": morning,
                "prior_risk": prior_risk,
                "pass_rate": result["pass_rate"],
                "breach_rate": result["breach_rate"],
                "unfinished_rate": result["unfinished_rate"],
            })

    pass_paths = [p for p in normal_paths if p.outcome == "pass"]
    representative = min(pass_paths, key=lambda p: abs(p.used_days - (median([x.used_days for x in pass_paths]) if pass_paths else 0))) if pass_paths else None
    representative_timeline = []
    if representative is not None:
        index = normal_paths.index(representative)
        representative = simulate_sequence(
            normal_daily[index:index + 45], selected_policy, selected_rules,
            PRESETS["normal"], capture=True,
            session_labels=test_days[index:index + 45],
        )
        representative_timeline = representative.timeline

    rolling = []
    width = 63
    for start in range(0, len(normal_paths), width):
        batch = normal_paths[start:start + width]
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
    mc = monte_carlo(normal_daily, selected_policy, selected_rules, PRESETS["normal"])
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
        "basis": "counts across overlapping 45-session rolling test paths",
        "contract_cap_limits": sum(path.contract_cap_limited for path in normal_paths),
        "risk_rejections": sum(path.risk_rejected for path in normal_paths),
        "daily_loss_blocks": sum(path.dll_blocked for path in normal_paths),
        "historical_gap_through_stops_in_raw_test_signals": actual_gap_stops,
    }
    report = {
        "schema_version": 1,
        "research_version": RESEARCH_VERSION,
        "generated_at": "2026-08-14",
        "seed": SEED,
        "status": "EXPERIMENTAL_PROXY",
        "status_label": "Experimental — proxy evidence, not validated",
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
            "event_filter": "No new entry from 2 minutes before through 2 minutes after scheduled US high-impact releases.",
            "why_selected": "25K has the best verified target-to-drawdown ratio; the three sleeves were selected on development/validation and improve frequency without martingale sizing.",
            "limitations": [
                "Dukascopy CFD/index proxy bars, not CME futures bid/ask or queue data.",
                "The 2024+ period has been inspected in prior research and is confirmatory rather than pristine.",
                "Historical execution costs are conservative assumptions, not observed fills.",
                "Most 45-session starts remain unfinished even in normal conditions.",
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
        "charts": {
            "representative_timeline": representative_timeline,
            "rolling_pass_probability": rolling,
            "pass_duration_histogram": [{"days": day, "count": duration_hist[day]} for day in sorted(duration_hist)],
            "terminal_profit_histogram": terminal_bins,
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
            "rolling_starts": True,
            "no_trade_sessions_in_denominator": True,
            "fill": "completed signal bar; next-minute open; one adverse tick entry and exit; stop-first ambiguity; gap-worse stop",
            "commission": "$0.50 per side per micro ($1.00 round turn)",
            "uncertainty": "Wilson interval plus circular block bootstrap over chronological rolling outcomes",
            "selection_warning": "Account-size comparison was added after prior inspection of the test era; do not call the period pristine OOS.",
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
