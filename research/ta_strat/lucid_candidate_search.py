"""Pre-registered candidate search for a LucidPro 25K evaluation pass edge.

The point of this module is to make it impossible to quietly tune a strategy on
the data that is later used to justify it.  Three properties are enforced in
code rather than promised in a comment:

* The candidate families, their parameter grids and the portfolio-combination
  budget are frozen in ``PRE_REGISTRATION`` and fingerprinted.  Every stage
  asserts the fingerprint, so editing the grid after seeing a result changes the
  hash and fails the run.
* The chronological splits are frozen.  ``development`` selects, ``validation``
  confirms a single shortlist, and ``holdout`` is opened exactly once for one
  already-chosen candidate.  ``lock_holdout`` refuses to score more than one.
* Every reported probability comes from disjoint 45-session blocks replayed
  through the conservative minute-by-minute account engine in
  ``lucid_lab_validation``, and carries both a raw exact-binomial interval and a
  multiplicity-corrected interval over the full number of variants tested.

The search deliberately reports the *precision ceiling* alongside every result.
With complete-RTH sessions available in both ES and NQ, no split contains enough
independent 45-session blocks to resolve a pass rate to better than roughly
+/-27 points, which is wider than any plausible edge this search could find.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Sequence

import lucid_causal_rebuild as L
import lucid_lab_validation as V
import lucid_portfolio_policy as P


HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "lucid_candidate_search.json"

SEARCH_VERSION = "lucid_candidate_search_v1"
ACCOUNT = "25K"
HORIZON = 45

# Chronological, frozen before any candidate was scored.
DEVELOPMENT_END = date(2020, 12, 31)
VALIDATION_END = date(2023, 12, 31)

# The 2024+ era was already inspected by earlier research in this repository, so
# it is a confirmatory holdout, never a pristine out-of-sample period.
HOLDOUT_IS_PRISTINE = False

# How many development survivors may enter the validation stage, and how many
# sleeves a portfolio candidate may combine.  Declared up front because both
# choices change the multiplicity correction.
VALIDATION_SHORTLIST = 6
PORTFOLIO_POOL = 6
PORTFOLIO_SIZES = (2, 3)

# The pool takes at most one sleeve per (family, market).  The first frozen
# revision of this search ranked the pool on development trade count alone,
# which filled all six slots with near-duplicate ES trend-pullback variants and
# meant the portfolio stage never actually tested family diversity.  That defect
# is visible in the development ranking without looking at validation or
# holdout, so it was corrected and every stage re-run.  The holdout has
# therefore been opened twice, and HOLDOUT_OPENINGS feeds the multiplicity
# correction so the second look is paid for rather than hidden.
POOL_ONE_PER_FAMILY_MARKET = True
SUPERSEDED_PREREGISTRATION_SHA = "fe4afb0141d588a5"
HOLDOUT_OPENINGS = 2


def _grid(**axes: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Expand a named axis product into an ordered tuple of parameter dicts."""
    keys = sorted(axes)
    combos: list[dict[str, Any]] = [{}]
    for key in keys:
        combos = [dict(base, **{key: value}) for base in combos for value in axes[key]]
    return tuple(combos)


@dataclass(frozen=True)
class Family:
    """One causal signal family with an economic rationale and a frozen grid."""

    family: str
    markets: tuple[str, ...]
    rationale: str
    grid: tuple[dict[str, Any], ...]

    def variants(self) -> list[tuple[str, L.Config]]:
        return [
            (market, L.Config(self.family, **params))
            for market in self.markets
            for params in self.grid
        ]


# --------------------------------------------------------------------------
# Pre-registration.  Editing anything below changes PRE_REGISTRATION_SHA and
# every stage will refuse to run until the fingerprint is updated deliberately.
# --------------------------------------------------------------------------
PRE_REGISTRATION: tuple[Family, ...] = (
    Family(
        "or_break",
        ("es", "nq"),
        "Overnight information is impounded at the cash open; the first range "
        "break marks the side institutional flow continues to press.",
        _grid(tf=(15, 30), rr=(1.5, 2.0, 2.5), stop_mode=("bar", "open")),
    ),
    Family(
        "or_fade",
        ("es", "nq"),
        "An opening extension far beyond the opening range with no follow-through "
        "reverts as opening liquidity imbalance clears.",
        _grid(or_min=(30, 60), ext=(0.25, 0.33), rr=(1.5, 2.0)),
    ),
    Family(
        "prior_breakout",
        ("es", "nq"),
        "Prior-session high and low are published reference levels where resting "
        "stops cluster; a break triggers them.",
        _grid(k=(0.15, 0.25, 0.35), rr=(1.5, 2.0, 2.5), stop_mode=("bar",)),
    ),
    Family(
        "nr7",
        ("es", "nq"),
        "Realized volatility is persistent; an unusually narrow session compresses "
        "risk premium that is released on the next expansion.",
        _grid(rr=(1.5, 2.0, 2.5), stop_mode=("bar", "open")),
    ),
    Family(
        "turtle",
        ("es", "nq"),
        "A marginal break of a multi-day extreme that immediately fails indicates "
        "the break was stop-driven rather than information-driven.",
        _grid(lookback=(10, 20), buf_ticks=(4, 8), rr=(1.5, 2.0)),
    ),
    Family(
        "trend_pullback",
        ("es", "nq"),
        "Intraday trends persist within a session; a shallow retracement offers "
        "the same direction at a better price.",
        _grid(tol_atr=(0.15, 0.20, 0.25), rr=(1.5, 2.0)),
    ),
    Family(
        "eighty_twenty",
        ("es", "nq"),
        "A session opening in the extreme of the prior range and failing to extend "
        "reverts toward the prior value area.",
        _grid(buf_ticks=(4, 8), rr=(1.5, 2.0)),
    ),
)

# Families deliberately excluded before any test, because this data source
# cannot support them.  Recorded so the exclusion is auditable.
EXCLUDED_FAMILIES: tuple[dict[str, str], ...] = (
    {
        "family": "vwap_reversion",
        "reason": "The proxy feed carries Dukascopy liquidity-provider volume, not "
                  "CME contract volume, so any volume-weighted price is not VWAP.",
    },
    {
        "family": "overnight_momentum",
        "reason": "The cache holds regular-session minutes only; there are no "
                  "Globex bars to measure an overnight move.",
    },
    {
        "family": "event_filtered_breakout",
        "reason": "No point-in-time economic-calendar archive exists in this "
                  "repository, so an event filter cannot be tested honestly.",
    },
    {
        "family": "order_flow_imbalance",
        "reason": "Proxy OHLC carries no bid/ask depth, queue position or trade "
                  "prints.",
    },
    {
        "family": "calendar_roll_basis",
        "reason": "A cash-index CFD proxy has no contract roll or basis to trade.",
    },
)


def _preregistration_payload() -> str:
    return json.dumps(
        {
            "version": SEARCH_VERSION,
            "account": ACCOUNT,
            "horizon": HORIZON,
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "validation_shortlist": VALIDATION_SHORTLIST,
            "portfolio_pool": PORTFOLIO_POOL,
            "portfolio_sizes": list(PORTFOLIO_SIZES),
            "pool_one_per_family_market": POOL_ONE_PER_FAMILY_MARKET,
            "supersedes": SUPERSEDED_PREREGISTRATION_SHA,
            "holdout_openings": HOLDOUT_OPENINGS,
            "families": [
                {
                    "family": item.family,
                    "markets": list(item.markets),
                    "grid": [dict(sorted(params.items())) for params in item.grid],
                }
                for item in PRE_REGISTRATION
            ],
            "excluded": [dict(sorted(row.items())) for row in EXCLUDED_FAMILIES],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


PRE_REGISTRATION_SHA = hashlib.sha256(_preregistration_payload().encode()).hexdigest()[:16]


def single_variants() -> list[tuple[str, L.Config]]:
    out: list[tuple[str, L.Config]] = []
    for item in PRE_REGISTRATION:
        out.extend(item.variants())
    return out


def portfolio_combination_count(pool: int = PORTFOLIO_POOL) -> int:
    return sum(math.comb(pool, size) for size in PORTFOLIO_SIZES)


def total_variants_tested() -> int:
    """Every configuration whose result is looked at, for multiplicity.

    Multiplied by HOLDOUT_OPENINGS so the superseded first run is paid for
    rather than quietly dropped.
    """
    per_run = len(single_variants()) + portfolio_combination_count()
    return per_run * HOLDOUT_OPENINGS


def sidak_alpha(variants: int, alpha: float = 0.05) -> float:
    """Family-wise alpha for `variants` independent looks."""
    if variants <= 1:
        return alpha
    return 1.0 - (1.0 - alpha) ** (1.0 / variants)


def best_of_n_null_expectation(blocks: int, variants: int, probability: float = 0.5) -> float:
    """Expected best observed pass rate when every variant is pure noise.

    A search over many worthless variants still produces a flattering winner.
    This is the number a candidate must beat before it means anything.
    """
    if blocks <= 0 or variants <= 0:
        return 0.0
    best = 0.0
    for successes in range(blocks + 1):
        at_most = V._binomial_cdf(successes, blocks, probability)
        below = V._binomial_cdf(successes - 1, blocks, probability) if successes else 0.0
        best += (successes / blocks) * (at_most**variants - below**variants)
    return best


@dataclass(frozen=True)
class Split:
    name: str
    sessions: tuple[date, ...]

    @property
    def blocks(self) -> int:
        return len(self.sessions) // HORIZON

    def precision_halfwidth(self, probability: float = 0.5) -> float:
        if self.blocks <= 0:
            return 1.0
        return 1.96 * math.sqrt(probability * (1.0 - probability) / self.blocks)


def build_splits(sessions: Sequence[date]) -> dict[str, Split]:
    ordered = sorted(sessions)
    return {
        "development": Split(
            "development",
            tuple(day for day in ordered if day <= DEVELOPMENT_END),
        ),
        "validation": Split(
            "validation",
            tuple(day for day in ordered if DEVELOPMENT_END < day <= VALIDATION_END),
        ),
        "holdout": Split(
            "holdout",
            tuple(day for day in ordered if day > VALIDATION_END),
        ),
    }


def joint_complete_sessions(days: dict[str, list[L.Day]]) -> list[date]:
    """Sessions where every required market has a complete RTH session."""
    if not days:
        return []
    common: set[date] | None = None
    for rows in days.values():
        seen = {row.day for row in rows}
        common = seen if common is None else (common & seen)
    return sorted(common or set())


def daily_baskets(
    trades: Iterable[L.Trade],
    sessions: Sequence[date],
) -> list[list[L.Trade]]:
    """Group trades onto an explicit session list, keeping zero-trade sessions."""
    index = {day: position for position, day in enumerate(sessions)}
    baskets: list[list[L.Trade]] = [[] for _ in sessions]
    for trade in trades:
        position = index.get(trade.day)
        if position is not None:
            baskets[position].append(trade)
    return [
        sorted(basket, key=lambda t: (t.entry_ts, P.signal_priority(t), t.exit_ts))
        for basket in baskets
    ]


def select_pool(
    ranked: Sequence[dict[str, Any]],
    pool_size: int = PORTFOLIO_POOL,
    *,
    one_per_family_market: bool = POOL_ONE_PER_FAMILY_MARKET,
) -> list[str]:
    """Pick the sleeve pool, keeping at most one variant per family and market.

    Without the de-duplication a frequency ranking fills every slot with near
    identical variants of whichever family trades most often, so the portfolio
    stage compares a family against itself instead of against other families.
    """
    chosen: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in ranked:
        if one_per_family_market:
            key = (row["family"], row["market"])
            if key in seen:
                continue
            seen.add(key)
        chosen.append(row["candidate"])
        if len(chosen) >= pool_size:
            break
    return chosen


def score(
    trades: Sequence[L.Trade],
    split: Split,
    store: V.MinutePathStore,
    *,
    preset: str = "normal",
    variants_tested: int = 1,
) -> dict[str, Any]:
    """Replay one candidate over the disjoint blocks of one split."""
    if split.blocks <= 0:
        raise ValueError(f"split {split.name} has no complete {HORIZON}-session block")
    usable = split.sessions[: split.blocks * HORIZON]
    baskets = daily_baskets(trades, usable)
    summary, _ = V.evaluate_sequences(
        baskets,
        list(usable),
        HORIZON,
        V.POLICIES[ACCOUNT],
        V.RULES[ACCOUNT],
        V.PRESETS[preset],
        store,
    )
    alpha = sidak_alpha(variants_tested)
    summary["split"] = split.name
    summary["preset"] = preset
    summary["sessions_used"] = len(usable)
    summary["trades_in_split"] = sum(len(basket) for basket in baskets)
    summary["zero_trade_sessions"] = sum(1 for basket in baskets if not basket)
    summary["variants_tested"] = variants_tested
    summary["multiplicity_alpha"] = round(alpha, 8)
    summary["pass_multiplicity_95"] = V._clopper_pearson(
        summary["passes"], summary["windows"], alpha=alpha
    )
    summary["precision_halfwidth"] = round(split.precision_halfwidth(), 6)
    return summary


@dataclass
class SearchState:
    """Guards the one-look-at-the-holdout rule."""

    preregistration_sha: str = PRE_REGISTRATION_SHA
    holdout_opened_for: str | None = field(default=None)

    def check(self) -> None:
        if self.preregistration_sha != PRE_REGISTRATION_SHA:
            raise RuntimeError(
                "pre-registration fingerprint changed after the search started; "
                "the grid may not be edited mid-run"
            )

    def lock_holdout(
        self,
        candidate: str,
        trades: Sequence[L.Trade],
        split: Split,
        store: V.MinutePathStore,
        *,
        preset: str = "normal",
        variants_tested: int = 1,
    ) -> dict[str, Any]:
        self.check()
        if self.holdout_opened_for is not None:
            raise RuntimeError(
                f"holdout already opened for {self.holdout_opened_for!r}; it may "
                "not be reused to select or re-rank a candidate"
            )
        self.holdout_opened_for = candidate
        result = score(
            trades, split, store, preset=preset, variants_tested=variants_tested
        )
        result["candidate"] = candidate
        result["pristine"] = HOLDOUT_IS_PRISTINE
        return result


def cash_benchmark(split: Split, variants_tested: int = 1) -> dict[str, Any]:
    """The no-trade benchmark: never passes, never breaches, never finishes."""
    blocks = split.blocks
    return {
        "split": split.name,
        "candidate": "cash_no_trade",
        "windows": blocks,
        "passes": 0,
        "breaches": 0,
        "unfinished": blocks,
        "pass_rate": 0.0,
        "breach_rate": 0.0,
        "unfinished_rate": 1.0 if blocks else 0.0,
        "pass_exact_binomial_95": V._clopper_pearson(0, blocks),
        "pass_multiplicity_95": V._clopper_pearson(
            0, blocks, alpha=sidak_alpha(variants_tested)
        ),
        "window_stride_sessions": HORIZON,
        "windows_overlap": False,
    }


def feasibility_table(splits: dict[str, Split]) -> list[dict[str, Any]]:
    """Per-family expected cost and failure modes, plus the precision ceiling."""
    rows: list[dict[str, Any]] = []
    for item in PRE_REGISTRATION:
        rows.append({
            "family": item.family,
            "markets": list(item.markets),
            "parameter_variants": len(item.grid) * len(item.markets),
            "parameters_per_variant": len(item.grid[0]) if item.grid else 0,
            "rationale": item.rationale,
            "required_data": "complete RTH one-minute OHLC for the listed markets",
            "likely_failure_mode": (
                "one signal per session caps frequency, so a single sleeve rarely "
                "reaches the 25K $1,250 target inside 45 sessions"
            ),
        })
    return rows


def _load_market_days(markets: Sequence[str]) -> dict[str, list[L.Day]]:
    return {market: L.load_days(market) for market in markets}


def run(cache: Path | None = None) -> dict[str, Any]:
    """Execute the frozen three-stage search and return the report."""
    state = SearchState()
    state.check()

    markets = sorted({market for item in PRE_REGISTRATION for market in item.markets})
    days = _load_market_days(markets)
    sessions = joint_complete_sessions(days)
    trimmed = {
        market: [row for row in rows if row.day in set(sessions)]
        for market, rows in days.items()
    }
    store = V.MinutePathStore(trimmed)
    splits = build_splits(sessions)
    variants = total_variants_tested()

    # ---- Stage 1: development ------------------------------------------
    development = splits["development"]
    generated: dict[str, list[L.Trade]] = {}
    dev_rows: list[dict[str, Any]] = []
    for market, config in single_variants():
        key = f"{market}:{config.label}"
        trades = L.generate(trimmed[market], config)
        generated[key] = trades
        summary = score(trades, development, store, variants_tested=variants)
        dev_rows.append({
            "candidate": key,
            "family": config.family,
            "market": market,
            "trades_total": len(trades),
            **{name: summary[name] for name in (
                "windows", "passes", "breaches", "unfinished",
                "pass_rate", "trades_in_split", "zero_trade_sessions",
            )},
        })

    # Sleeves are ranked by development trade frequency inside the split,
    # because a single sleeve almost never passes on its own; the portfolio
    # stage is where a pass rate becomes measurable at all.
    ranked = sorted(
        dev_rows,
        key=lambda row: (-row["trades_in_split"], -row["pass_rate"], row["candidate"]),
    )
    pool = select_pool(ranked)

    portfolio_rows: list[dict[str, Any]] = []
    for size in PORTFOLIO_SIZES:
        for combo in combinations(pool, size):
            merged: list[L.Trade] = []
            for key in combo:
                merged.extend(generated[key])
            summary = score(merged, development, store, variants_tested=variants)
            portfolio_rows.append({
                "candidate": " + ".join(combo),
                "sleeves": list(combo),
                **{name: summary[name] for name in (
                    "windows", "passes", "breaches", "unfinished",
                    "pass_rate", "trades_in_split", "zero_trade_sessions",
                )},
            })

    development_ranking = sorted(
        portfolio_rows, key=lambda row: (-row["pass_rate"], row["candidate"])
    )
    shortlist = development_ranking[:VALIDATION_SHORTLIST]

    # ---- Stage 2: validation (single look at the shortlist) -------------
    validation = splits["validation"]
    validation_rows: list[dict[str, Any]] = []
    for row in shortlist:
        merged: list[L.Trade] = []
        for key in row["sleeves"]:
            merged.extend(generated[key])
        summary = score(merged, validation, store, variants_tested=variants)
        validation_rows.append({
            "candidate": row["candidate"],
            "sleeves": row["sleeves"],
            "development_pass_rate": row["pass_rate"],
            **{name: summary[name] for name in (
                "windows", "passes", "breaches", "unfinished", "pass_rate",
                "pass_exact_binomial_95", "pass_multiplicity_95",
                "trades_in_split", "zero_trade_sessions",
            )},
        })

    validation_ranking = sorted(
        validation_rows, key=lambda row: (-row["pass_rate"], row["candidate"])
    )
    winner = validation_ranking[0] if validation_ranking else None

    # ---- Stage 3: holdout (opened once, for the already-chosen winner) ---
    holdout = splits["holdout"]
    holdout_result: dict[str, Any] | None = None
    if winner is not None:
        merged = []
        for key in winner["sleeves"]:
            merged.extend(generated[key])
        holdout_result = state.lock_holdout(
            winner["candidate"], merged, holdout, store, variants_tested=variants
        )

    # ---- Benchmarks ------------------------------------------------------
    baseline_trades = P.selected_signals(trimmed)
    baseline = {
        name: score(baseline_trades, splits[name], store, variants_tested=1)
        for name in ("development", "validation", "holdout")
    }
    cash = {name: cash_benchmark(splits[name]) for name in splits}

    # ---- Stress on the winner -------------------------------------------
    stresses: list[dict[str, Any]] = []
    if winner is not None:
        merged = []
        for key in winner["sleeves"]:
            merged.extend(generated[key])
        for preset in ("normal", "spread_2x", "slippage_2x", "gap_event", "severe"):
            summary = score(
                merged, holdout, store, preset=preset, variants_tested=variants
            )
            stresses.append({
                "preset": preset,
                **{name: summary[name] for name in (
                    "pass_rate", "breach_rate", "unfinished_rate", "windows",
                )},
            })

    null_best = best_of_n_null_expectation(development.blocks, variants)

    report: dict[str, Any] = {
        "schema_version": 1,
        "search_version": SEARCH_VERSION,
        "preregistration_sha": PRE_REGISTRATION_SHA,
        "account": ACCOUNT,
        "horizon_sessions": HORIZON,
        "data": {
            "source": "Dukascopy one-minute CFD/index proxy cache",
            "exchange_grade": False,
            "decision_grade": False,
            "markets": markets,
            "joint_complete_sessions": len(sessions),
            "first_session": sessions[0].isoformat() if sessions else None,
            "last_session": sessions[-1].isoformat() if sessions else None,
        },
        "splits": {
            name: {
                "sessions": len(split.sessions),
                "blocks": split.blocks,
                "precision_halfwidth": round(split.precision_halfwidth(), 6),
                "first": split.sessions[0].isoformat() if split.sessions else None,
                "last": split.sessions[-1].isoformat() if split.sessions else None,
            }
            for name, split in splits.items()
        },
        "holdout_pristine": HOLDOUT_IS_PRISTINE,
        "variants_tested": variants,
        "single_variants": len(single_variants()),
        "portfolio_variants": portfolio_combination_count(),
        "multiplicity_alpha": round(sidak_alpha(variants), 8),
        "best_of_n_null_pass_rate": round(null_best, 6),
        "feasibility": feasibility_table(splits),
        "excluded_families": [dict(sorted(row.items())) for row in EXCLUDED_FAMILIES],
        "development_singles": ranked,
        "development_portfolios": development_ranking,
        "validation": validation_ranking,
        "holdout": holdout_result,
        "baseline": {name: baseline[name] for name in baseline},
        "cash_benchmark": cash,
        "stresses": stresses,
        "event_filter_applied": False,
        "event_filter_note": (
            "No point-in-time economic-calendar archive exists in this repository, "
            "so no news filter was applied or tested."
        ),
    }
    report["verdict"] = decide(report)
    raw = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report["run_id"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return report


def decide(report: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed verdict.  Only overwhelming evidence may return PASS."""
    gates: list[dict[str, Any]] = []

    gates.append({
        "id": "exchange_grade_data",
        "passed": bool(report["data"].get("exchange_grade")),
        "detail": "Dukascopy index/CFD proxy bars are not CME MES/MNQ execution data.",
    })
    gates.append({
        "id": "pristine_holdout",
        "passed": bool(report.get("holdout_pristine")),
        "detail": "The 2024+ era was inspected by earlier research before this search.",
    })
    gates.append({
        "id": "point_in_time_event_filter",
        "passed": bool(report.get("event_filter_applied")),
        "detail": report.get("event_filter_note", ""),
    })

    holdout = report.get("holdout") or {}
    interval = holdout.get("pass_multiplicity_95") or [0.0, 1.0]
    width = float(interval[1]) - float(interval[0])
    gates.append({
        "id": "decision_precision",
        "passed": width <= 0.20,
        "detail": (
            f"Multiplicity-corrected holdout interval spans {width * 100:.1f} points; "
            "a decision needs 20 points or less."
        ),
    })

    baseline_holdout = (report.get("baseline") or {}).get("holdout") or {}
    beats_baseline = float(holdout.get("pass_rate", 0.0)) > float(
        baseline_holdout.get("pass_rate", 0.0)
    )
    gates.append({
        "id": "beats_frozen_baseline",
        "passed": beats_baseline,
        "detail": (
            f"Candidate holdout pass rate {holdout.get('pass_rate')} vs frozen "
            f"baseline {baseline_holdout.get('pass_rate')}."
        ),
    })

    null_best = float(report.get("best_of_n_null_pass_rate", 0.0))
    gates.append({
        "id": "beats_search_noise",
        "passed": float(holdout.get("pass_rate", 0.0)) > null_best,
        "detail": (
            f"A search over {report.get('variants_tested')} worthless variants would "
            f"be expected to produce a best development pass rate of {null_best:.3f}."
        ),
    })

    gates.append({
        "id": "no_breaches_under_severe_stress",
        "passed": all(
            float(row.get("breach_rate", 1.0)) == 0.0
            for row in report.get("stresses", [])
        ) and bool(report.get("stresses")),
        "detail": "Every stress preset must leave the drawdown floor untouched.",
    })

    failed = [gate["id"] for gate in gates if not gate["passed"]]
    if not failed:
        decision = "PASS"
    elif failed == ["pristine_holdout"]:
        decision = "INCONCLUSIVE"
    else:
        decision = "NO_GO"

    return {
        "decision": decision,
        "failed_gate_count": len(failed),
        "failed_gates": failed,
        "gates": gates,
        "auto_trade_allowed": False,
        "reason": (
            "A positive backtest on proxy data with a wide interval is not evidence "
            "of a live edge; this verdict never enables order routing."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(RESULTS))
    args = parser.parse_args()

    report = run()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)

    print(json.dumps({
        "run_id": report["run_id"],
        "preregistration_sha": report["preregistration_sha"],
        "variants_tested": report["variants_tested"],
        "holdout_blocks": report["splits"]["holdout"]["blocks"],
        "holdout_pass_rate": (report.get("holdout") or {}).get("pass_rate"),
        "decision": report["verdict"]["decision"],
        "failed_gates": report["verdict"]["failed_gates"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
