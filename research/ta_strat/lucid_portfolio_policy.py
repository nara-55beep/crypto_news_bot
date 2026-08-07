"""
State-aware LucidPro evaluation for development-selected causal signals.

This file is deliberately research-only.  It cannot enable either paper bot.

The signal set was selected without looking at 2024+ results:
  * NQ 15-minute opening drive
  * ES 15-minute gap fill
  * NQ prior-range breakout (lower-risk frequency sleeve)

Every signal is formed from completed bars and enters the following one-minute
open.  The imported causal engine applies one adverse tick to entries and exits,
gap-worse stop fills, stop-first ordering, integer micros, and $1 round-turn
commission.  This module adds portfolio-level concurrent-position accounting,
the 40-micro aggregate cap, prior-EOD trailing MLL, the soft DLL, and sizing that
reserves MLL room for the stop risk of all open positions.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date
from statistics import mean, median

import lucid_causal_rebuild as L
import lucid_predictive_research as P


@dataclass(frozen=True)
class Policy:
    morning_risk: float
    prior_risk: float
    drawdown_cut: float = -500.0
    drawdown_scale: float = 1.0
    profit_cut: float = 1_000.0
    profit_scale: float = 1.0
    disable_prior_below_zero: bool = False

    @property
    def label(self) -> str:
        return (
            f"m{self.morning_risk:g}_p{self.prior_risk:g}_"
            f"dd{self.drawdown_cut:g}x{self.drawdown_scale:g}_"
            f"up{self.profit_cut:g}x{self.profit_scale:g}_"
            f"prioroff{int(self.disable_prior_below_zero)}"
        )


@dataclass(frozen=True)
class AccountRules:
    name: str
    target_profit: float
    max_loss: float
    lock_trigger: float
    locked_floor: float
    daily_loss_limit: float | None
    max_micros: int


RULES_25K = AccountRules("25K", 1_250.0, 1_000.0, 1_100.0, 100.0, None, 20)
RULES_50K = AccountRules(
    "50K",
    L.TARGET_PROFIT,
    L.MAX_LOSS,
    L.LOCK_TRIGGER,
    L.LOCKED_FLOOR,
    L.DAILY_LOSS_LIMIT,
    L.MAX_MICROS,
)


@dataclass
class Position:
    trade: L.Trade
    qty: int

    @property
    def reserved_loss(self) -> float:
        return (self.trade.risk_per_micro + L.COMMISSION_RT) * self.qty


def selected_signals(days: dict[str, list[L.Day]]) -> list[L.Trade]:
    """Return only configurations chosen on 2016-2023 development data."""
    nq_morning = P.morning_regime(
        days["nq"],
        P.PConfig(
            "morning_regime",
            "nq",
            entry_minute=15,
            threshold=0.25,
            target_rr=2.0,
            stop_mode="open",
            mode="drive",
            location=0.80,
        ),
    )
    es_morning = P.morning_regime(
        days["es"],
        P.PConfig(
            "morning_regime",
            "es",
            entry_minute=15,
            threshold=0.10,
            target_rr=1.5,
            stop_mode="range",
            mode="gap_fill",
            location=0.80,
        ),
    )
    nq_prior = L.generate(
        days["nq"],
        L.Config(
            "prior_breakout",
            tf=15,
            k=0.25,
            stop_mode="bar",
            rr=2.0,
        ),
    )
    return sorted(
        nq_morning + es_morning + nq_prior,
        key=lambda t: (t.entry_ts, signal_priority(t), t.exit_ts),
    )


def signal_priority(trade: L.Trade) -> int:
    """Resolve simultaneous entries using the fixed development ranking."""
    if trade.market == "es":
        return 0
    if "morning" in trade.strategy:
        return 1
    return 2


def _base_risk(trade: L.Trade, policy: Policy, balance: float) -> float:
    morning = "morning" in trade.strategy
    if not morning and policy.disable_prior_below_zero and balance < 0:
        return 0.0
    risk = policy.morning_risk if morning else policy.prior_risk
    if balance < policy.drawdown_cut:
        risk *= policy.drawdown_scale
    elif balance > policy.profit_cut:
        risk *= policy.profit_scale
    return risk


def _entry_qty(
    trade: L.Trade,
    policy: Policy,
    balance: float,
    floor: float,
    open_positions: list[Position],
    rules: AccountRules = RULES_50K,
) -> int:
    all_in_risk = trade.risk_per_micro + L.COMMISSION_RT
    requested = int(math.floor(_base_risk(trade, policy, balance) / all_in_risk))
    cap_room = rules.max_micros - sum(p.qty for p in open_positions)
    reserved = sum(p.reserved_loss for p in open_positions)
    # Keep one cent of space so an exactly-equal MLL touch cannot pass this guard.
    mll_room = max(0.0, balance - floor - reserved - 0.01)
    floor_room = int(math.floor(mll_room / all_in_risk))
    return max(0, min(requested, cap_room, floor_room))


def simulate_window(
    trades_by_day: dict[date, list[L.Trade]],
    window_days: list[date],
    policy: Policy,
    rules: AccountRules = RULES_50K,
) -> tuple[str, int]:
    """Simulate one fresh evaluation window; exits are processed before same-time entries."""
    balance = 0.0
    eod_peak = 0.0
    floor = -rules.max_loss

    for used, session in enumerate(window_days, 1):
        day_pnl = 0.0
        positions: list[Position] = []
        entries = sorted(
            trades_by_day.get(session, []),
            key=lambda t: (t.entry_ts, signal_priority(t), t.exit_ts),
        )
        timestamps = sorted({t.entry_ts for t in entries} | {t.exit_ts for t in entries})
        for timestamp in timestamps:
            # Release positions entered on an earlier minute before allocating the
            # aggregate cap to new signals at this timestamp.
            exiting = [
                p for p in positions
                if p.trade.exit_ts == timestamp and p.trade.entry_ts < timestamp
            ]
            for match in exiting:
                positions.remove(match)
                pnl = (match.trade.gross_per_micro - L.COMMISSION_RT) * match.qty
                balance += pnl
                day_pnl += pnl
                if balance <= floor:
                    return "fail", used
                if balance >= rules.target_profit:
                    return "pass", used

            for trade in (t for t in entries if t.entry_ts == timestamp):
                if (
                    rules.daily_loss_limit is not None
                    and day_pnl <= -rules.daily_loss_limit
                ):
                    continue
                qty = _entry_qty(trade, policy, balance, floor, positions, rules)
                if qty > 0:
                    positions.append(Position(trade, qty))

            # A stop or target can legitimately occur during the entry minute.
            immediate = [
                p for p in positions
                if p.trade.exit_ts == timestamp and p.trade.entry_ts == timestamp
            ]
            for match in immediate:
                positions.remove(match)
                pnl = (match.trade.gross_per_micro - L.COMMISSION_RT) * match.qty
                balance += pnl
                day_pnl += pnl
                if balance <= floor:
                    return "fail", used
                if balance >= rules.target_profit:
                    return "pass", used

        if positions:
            raise AssertionError("All selected strategies must be flat intraday")
        eod_peak = max(eod_peak, balance)
        floor = (
            rules.locked_floor
            if eod_peak > rules.lock_trigger
            else eod_peak - rules.max_loss
        )
    return "undecided", len(window_days)


def evaluate(
    trades: list[L.Trade],
    all_days: list[date],
    policy: Policy,
    horizon: int,
    rules: AccountRules = RULES_50K,
) -> dict:
    by_day = {day: [] for day in all_days}
    for trade in trades:
        if trade.day in by_day:
            by_day[trade.day].append(trade)
    n_starts = max(0, len(all_days) - horizon + 1)
    outcomes = []
    used_days = []
    pass_days = []
    for i in range(n_starts):
        outcome, used = simulate_window(
            by_day, all_days[i:i + horizon], policy, rules
        )
        outcomes.append(outcome)
        used_days.append(used)
        if outcome == "pass":
            pass_days.append(used)
    passes = outcomes.count("pass")
    fails = outcomes.count("fail")
    # Conditional pass-time statistics are never an "average across starts".
    # The restricted mean treats every non-pass as consuming the full horizon,
    # making the censoring visible instead of silently dropping those windows.
    restricted_days = [
        used if outcome == "pass" else horizon
        for outcome, used in zip(outcomes, used_days)
    ]
    return {
        "starts": n_starts,
        "passes": passes,
        "fails": fails,
        "undecided": outcomes.count("undecided"),
        "pass_rate": passes / n_starts if n_starts else 0.0,
        "fail_rate": fails / n_starts if n_starts else 0.0,
        "median_days": median(pass_days) if pass_days else None,
        "mean_pass_days": mean(pass_days) if pass_days else None,
        "restricted_mean_days": (
            mean(restricted_days) if restricted_days else None
        ),
    }


def _slice_days(all_days: list[date], lo: date | None, hi: date | None) -> list[date]:
    return [
        d for d in all_days
        if (lo is None or d >= lo) and (hi is None or d <= hi)
    ]


def _slice_trades(trades: list[L.Trade], lo: date | None, hi: date | None) -> list[L.Trade]:
    return [
        t for t in trades
        if (lo is None or t.day >= lo) and (hi is None or t.day <= hi)
    ]


def policy_grid() -> list[Policy]:
    # A small, predeclared policy set limits another source of multiple testing.
    # Each base allocation is tested unchanged, with defense below -$500,
    # acceleration above +$1,000, and the combination of those two rules.
    adaptations = (
        (1.0, 1.0, False),
        (0.5, 1.0, True),
        (1.0, 1.5, False),
        (0.5, 1.5, True),
    )
    return [
        Policy(morning, prior, -500.0, dd_scale, 1_000.0, up_scale, prior_off)
        for morning in (600.0, 700.0, 800.0)
        for prior in (0.0, 100.0, 200.0)
        for dd_scale, up_scale, prior_off in adaptations
    ]


def _development_score(e12: dict, e30: dict) -> float:
    # Passing quickly matters, but a failure is materially worse than remaining active.
    # No test-set term is permitted here.
    return (
        2.0 * e12["pass_rate"]
        + e30["pass_rate"]
        - 1.5 * e12["fail_rate"]
        - 1.0 * e30["fail_rate"]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    days = {market: L.load_days(market) for market in ("nq", "es")}
    trades = selected_signals(days)
    all_days = sorted({d.day for rows in days.values() for d in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    prepared = {
        name: (
            _slice_trades(trades, lo, hi),
            _slice_days(all_days, lo, hi),
        )
        for name, (lo, hi) in periods.items()
    }

    rows = []
    grid = policy_grid()
    for n, policy in enumerate(grid, 1):
        development = {}
        for name in ("train", "valid"):
            period_trades, period_days = prepared[name]
            development[name] = {
                12: evaluate(period_trades, period_days, policy, 12),
                30: evaluate(period_trades, period_days, policy, 30),
            }
        score = min(
            _development_score(development["train"][12], development["train"][30]),
            _development_score(development["valid"][12], development["valid"][30]),
        )
        rows.append((score, policy, development))
        if n % 10 == 0:
            print(f"  policies {n}/{len(grid)}", flush=True)

    rows.sort(key=lambda row: row[0], reverse=True)
    finalists = rows[:args.top]
    print("\nPOLICIES SELECTED USING TRAIN + VALIDATION ONLY")
    for score, policy, development in finalists:
        test_trades, test_days = prepared["test"]
        test = {
            12: evaluate(test_trades, test_days, policy, 12),
            30: evaluate(test_trades, test_days, policy, 30),
        }
        print(f"\n{policy.label} score {score:+.3f}")
        for name, result in (("train", development["train"]), ("valid", development["valid"]), ("TEST", test)):
            e12, e30 = result[12], result[30]
            print(
                f"  {name:<5} 12d pass {e12['pass_rate']*100:5.1f}% "
                f"fail {e12['fail_rate']*100:5.1f}% | "
                f"30d pass {e30['pass_rate']*100:5.1f}% "
                f"fail {e30['fail_rate']*100:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
