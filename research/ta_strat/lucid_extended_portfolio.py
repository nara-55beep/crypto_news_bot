"""
Portfolio iteration that adds development-selected opening-gap reversal signals.

The candidate set is deliberately small:
  base       causal NQ/ES morning signals plus the NQ frequency sleeve
  nq_gap     base plus one NQ gap-reversal configuration
  both_gap   nq_gap plus one ES gap-reversal configuration

No correlated parameter neighbors are stacked.  Portfolio/risk-policy ranking uses
training and validation only; the chronological test is printed afterward.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date

import lucid_causal_rebuild as L
import lucid_gap_research as G
import lucid_jump_research as J
import lucid_portfolio_policy as S


def candidates(days: dict[str, list[L.Day]]) -> dict[str, list[L.Trade]]:
    base = S.selected_signals(days)
    nq_cfg = G.GapConfig(
        "nq", "opening_gap", 30, 0.002,
        "reverse", "turn", "atr", "rr", 2.0,
    )
    es_cfg = G.GapConfig(
        "es", "opening_gap", 30, 0.002,
        "reverse", "turn", "extreme", "rr", 2.0,
    )
    # Reuse the opening-risk sleeve in the state-aware simulator.
    nq_gap = [
        replace(t, strategy="morning_" + t.strategy)
        for t in G.generate(days["nq"], nq_cfg)
    ]
    es_gap = [
        replace(t, strategy="morning_" + t.strategy)
        for t in G.generate(days["es"], es_cfg)
    ]
    nq_jump_cfg = J.JumpConfig(
        "nq", 15, 1.5, 120, "continue", "atr", None, 60,
    )
    nq_jump = J.generate(days["nq"], nq_jump_cfg)
    return {
        "base": sorted(base, key=lambda t: (t.entry_ts, S.signal_priority(t))),
        "nq_gap": sorted(base + nq_gap, key=lambda t: (t.entry_ts, S.signal_priority(t))),
        "both_gap": sorted(
            base + nq_gap + es_gap,
            key=lambda t: (t.entry_ts, S.signal_priority(t)),
        ),
        "nq_gap_jump": sorted(
            base + nq_gap + nq_jump,
            key=lambda t: (t.entry_ts, S.signal_priority(t)),
        ),
        "both_gap_jump": sorted(
            base + nq_gap + es_gap + nq_jump,
            key=lambda t: (t.entry_ts, S.signal_priority(t)),
        ),
    }


def policies(rules: S.AccountRules) -> list[S.Policy]:
    adaptations = (
        (1.0, 1.0, False),
        (0.5, 1.0, True),
        (1.0, 1.5, False),
        (0.5, 1.5, True),
    )
    opening_risks = (300.0, 400.0, 500.0) if rules.name == "25K" else (600.0, 800.0, 1_000.0)
    prior_risks = (0.0, 50.0, 100.0) if rules.name == "25K" else (0.0, 100.0, 200.0)
    drawdown_cut = -250.0 if rules.name == "25K" else -500.0
    profit_cut = 500.0 if rules.name == "25K" else 1_000.0
    return [
        S.Policy(
            opening,
            prior,
            drawdown_cut,
            dd_scale,
            profit_cut,
            up_scale,
            prior_off,
        )
        for opening in opening_risks
        for prior in prior_risks
        for dd_scale, up_scale, prior_off in adaptations
    ]


def score(e12: dict, e30: dict) -> float:
    return (
        2.0 * e12["pass_rate"]
        + e30["pass_rate"]
        - 1.5 * e12["fail_rate"]
        - e30["fail_rate"]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", nargs="+", choices=["25K", "50K"], default=["25K", "50K"])
    args = ap.parse_args()
    account_map = {"25K": S.RULES_25K, "50K": S.RULES_50K}
    days = {market: L.load_days(market) for market in ("nq", "es")}
    portfolios = candidates(days)
    all_days = sorted({d.day for rows in days.values() for d in rows})
    periods = {
        "train": (None, L.TRAIN_END),
        "valid": (date(2022, 1, 1), L.VALID_END),
        "test": (date(2024, 1, 1), None),
    }
    prepared = {
        (portfolio_name, period_name): (
            S._slice_trades(trades, lo, hi),
            S._slice_days(all_days, lo, hi),
        )
        for portfolio_name, trades in portfolios.items()
        for period_name, (lo, hi) in periods.items()
    }

    rows = []
    for account_name in args.accounts:
        rules = account_map[account_name]
        for portfolio_name in portfolios:
            for policy in policies(rules):
                development = {}
                for period_name in ("train", "valid"):
                    trades, session_days = prepared[(portfolio_name, period_name)]
                    development[period_name] = {
                        12: S.evaluate(trades, session_days, policy, 12, rules),
                        30: S.evaluate(trades, session_days, policy, 30, rules),
                    }
                development_score = min(
                    score(development["train"][12], development["train"][30]),
                    score(development["valid"][12], development["valid"][30]),
                )
                rows.append((
                    development_score,
                    account_name,
                    rules,
                    portfolio_name,
                    policy,
                    development,
                ))
            print(f"completed {account_name} {portfolio_name}", flush=True)

    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for development_score, account_name, rules, portfolio_name, policy, development in rows[:15]:
        test_trades, test_days = prepared[(portfolio_name, "test")]
        test = {
            12: S.evaluate(test_trades, test_days, policy, 12, rules),
            30: S.evaluate(test_trades, test_days, policy, 30, rules),
        }
        print(
            f"\n{account_name} {portfolio_name} {policy.label} "
            f"score {development_score:+.3f}"
        )
        for period_name, result in (
            ("train", development["train"]),
            ("valid", development["valid"]),
            ("TEST", test),
        ):
            e12, e30 = result[12], result[30]
            print(
                f"  {period_name:<5} 12d pass {e12['pass_rate']*100:5.1f}% "
                f"fail {e12['fail_rate']*100:5.1f}% | "
                f"30d pass {e30['pass_rate']*100:5.1f}% "
                f"fail {e30['fail_rate']*100:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
