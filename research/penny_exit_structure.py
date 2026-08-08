"""Audit penny-stock exit rules without turning a broad proxy into live evidence.

The first version of this audit found that removing a fixed target improved the mean
return of a broad, survivor-only 8-K sample.  It then made two invalid leaps: it called
those 8-K events the live bot's entries, and it cited a test of the best rule versus
zero as if it were a paired test versus the current exit.  This version keeps that
result as a hypothesis and tests the actual difference directly.

Two scopes are reported.  ``broad_8k`` is useful only for hypothesis generation.
``reaction_confirmed_proxy`` applies already-declared reaction, liquidity, volatility,
and dilution filters and a three-entry daily cap.  Even the latter is not an exact v3
backtest: historical point-in-time headlines, fundamentals, AI vetoes, persistence,
live quotes, and delisted names are unavailable.  No exit change can be deployed from
this audit alone.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pennystock_paper as desk                      # noqa: E402
from research import penny_edge_research as base     # noqa: E402
from research import penny_harm_model as harm        # noqa: E402
from research import penny_stats as stats            # noqa: E402

REPORT_PATH = ROOT / "data" / "pennystock_exit_structure.json"
METHOD_VERSION = "exit-structure-v2-2026-08-08"
HOLD_CALENDAR_DAYS = desk.MAX_HOLD_DAYS
ASSUMED_COST = 0.005
BASELINE = "live_current"
MODES = {
    "live_current (stop + 2.5R target + trail)": BASELINE,
    "stop_and_target (no trail)": "stop_and_target",
    "trail_no_target (stop + trail, no target)": "trail_no_target",
    "stop_only (no target or trail)": "stop_only",
    "hold_only (time exit only)": "hold_only",
}


def _deadline_position(index: pd.DatetimeIndex, entry_pos: int) -> int:
    """First session on/after the live bot's calendar-time deadline."""
    deadline = pd.Timestamp(index[entry_pos]).normalize() + pd.Timedelta(
        days=HOLD_CALENDAR_DAYS
    )
    position = int(index.searchsorted(deadline, side="left"))
    return min(max(entry_pos, position), len(index) - 1)


def _simulate_details(
    frame: pd.DataFrame,
    panel: dict,
    mode: str,
    *,
    same_bar_trail: bool = True,
) -> pd.DataFrame:
    """Replay daily bars under one exit rule and retain exit dates/reasons.

    Stops win any bar containing both a stop and target.  A newly raised trailing stop
    is also assumed hit when the same daily bar trades through it.  Those assumptions
    are conservative but cannot reconstruct the true intraday path; that limitation is
    one reason this report cannot authorize an exit change.
    """
    records: list[dict] = []
    idx_cache = {symbol: bars.index for symbol, bars in panel.items()}
    fixed_target = mode in ("stop_and_target", BASELINE)
    use_trail = mode in (BASELINE, "trail_no_target")

    for event_index, event in frame.iterrows():
        symbol = event["ticker"]
        bars = panel.get(symbol)
        if bars is None or bars.empty:
            records.append({"event_index": event_index, "gross_return": np.nan})
            continue
        entry_date = pd.Timestamp(event["entry_date"])
        location = idx_cache[symbol].get_indexer([entry_date])[0]
        if location < 0 or location >= len(bars):
            records.append({"event_index": event_index, "gross_return": np.nan})
            continue
        entry = float(bars["open"].iloc[location])
        if not np.isfinite(entry) or entry <= 0:
            records.append({"event_index": event_index, "gross_return": np.nan})
            continue

        atr = float(event["atr_pct"]) * 100.0
        risk = max(7.0, min(15.0, atr * 1.15 if atr > 0 else 10.0))
        stop = entry * (1.0 - risk / 100.0)
        target = entry * (1.0 + risk * 2.5 / 100.0)
        last = _deadline_position(bars.index, location)
        exit_price = float(bars["close"].iloc[last])
        exit_pos = last
        reason = "time"

        if mode != "hold_only":
            high_water = entry
            trailing = False
            for pos in range(location, last + 1):
                open_price = float(bars["open"].iloc[pos])
                high = float(bars["high"].iloc[pos])
                low = float(bars["low"].iloc[pos])
                if not all(np.isfinite(value) for value in (open_price, high, low)):
                    continue
                if open_price <= stop:
                    exit_price, exit_pos = open_price, pos
                    reason = "trailing_gap" if trailing else "stop_gap"
                    break
                if fixed_target and open_price >= target:
                    exit_price, exit_pos, reason = open_price, pos, "target_gap"
                    break
                if low <= stop:
                    exit_price, exit_pos = stop, pos
                    reason = "trailing_stop" if trailing else "stop"
                    break
                if fixed_target and high >= target:
                    exit_price, exit_pos, reason = target, pos, "target"
                    break

                high_water = max(high_water, high)
                if use_trail:
                    if not trailing and high >= entry * (
                        1.0 + desk.TRAIL_ARM_PCT / 100.0
                    ):
                        trailing = True
                        stop = max(stop, entry)
                    if trailing:
                        stop = max(
                            stop, high_water * (1.0 - desk.TRAIL_PCT / 100.0)
                        )
                        if same_bar_trail and low <= stop:
                            exit_price, exit_pos, reason = stop, pos, "trailing_intrabar"
                            break

        records.append(
            {
                "event_index": event_index,
                "gross_return": exit_price / entry - 1.0,
                "entry": entry,
                "entry_date": pd.Timestamp(bars.index[location]),
                "exit_date": pd.Timestamp(bars.index[exit_pos]),
                "exit_reason": reason,
                "held_sessions": int(exit_pos - location + 1),
                "held_calendar_days": int(
                    (pd.Timestamp(bars.index[exit_pos])
                     - pd.Timestamp(bars.index[location])).days
                ),
            }
        )
    if not records:
        return pd.DataFrame(index=frame.index)
    return pd.DataFrame(records).set_index("event_index").reindex(frame.index)


def _simulate(frame: pd.DataFrame, panel: dict, mode: str) -> pd.Series:
    """Compatibility wrapper used by tests and ad-hoc research."""
    return _simulate_details(frame, panel, mode)["gross_return"]


def _reaction_confirmed_proxy(frame: pd.DataFrame) -> pd.DataFrame:
    """A stricter observable proxy, assembled only from pre-entry fields.

    Thresholds are the common envelope of the SEC reaction specifications already in
    ``penny_event_drift.py``.  They were not selected from this exit comparison.
    """
    selected = frame[
        ~frame["negative_event"]
        & frame["raw_close"].between(0.10, 5.00)
        & frame["reaction_pct"].between(3.0, 30.0)
        & frame["volume_ratio"].ge(1.5)
        & frame["close_location"].ge(0.65)
        & frame["dollar_volume20"].ge(5_000_000)
        & frame["atr_pct"].between(0.02, 0.20)
        & frame["max_ret20"].lt(0.35)
        & frame["dilution_age_days"].gt(90)
    ].copy()
    selected["proxy_score"] = (
        selected["reaction_pct"]
        * np.log1p(selected["volume_ratio"].clip(lower=0))
        * np.log1p(selected["dollar_volume20"].clip(lower=0) / 1_000_000)
    )
    return (
        selected.sort_values(["signal_date", "proxy_score"], ascending=[True, False])
        .groupby("signal_date", group_keys=False)
        .head(3)
    )


def _describe(returns: pd.Series, cost: float = ASSUMED_COST) -> dict:
    net = (returns - cost).dropna()
    if net.empty:
        return {"applicable": False}
    return {
        "applicable": True,
        "events": int(len(net)),
        "mean_net_pct": round(float(net.mean()) * 100, 4),
        "median_net_pct": round(float(net.median()) * 100, 4),
        "win_rate_pct": round(float((net > 0).mean()) * 100, 2),
        "skew": round(float(net.skew()), 2),
        "best_pct": round(float(net.max()) * 100, 2),
    }


def _paired_book(
    events: pd.DataFrame, alternative: pd.Series, baseline: pd.Series
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_date": events["signal_date"],
            "ticker": events["ticker"],
            "net_return": alternative - baseline,
        }
    ).dropna()


def _analyse_scope(
    events: pd.DataFrame,
    simulations: dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
) -> dict:
    levels: dict[str, dict] = {}
    paired: dict[str, dict] = {}
    family: dict[str, pd.DataFrame] = {}
    baseline = simulations[BASELINE].loc[events.index, "gross_return"]

    for label, mode in MODES.items():
        values = simulations[mode].loc[events.index, "gross_return"]
        levels[label] = _describe(values)
        if mode == BASELINE:
            continue
        book = _paired_book(events, values, baseline)
        result = stats.power(
            book, label=f"{mode}_minus_{BASELINE}", calendar=calendar,
            n_boot=3000, block=10,
        )
        if result.get("applicable"):
            result["trade_weighted_difference_pct"] = round(
                float(book["net_return"].mean()) * 100, 4
            )
            result["positive_event_pct"] = round(
                float((book["net_return"] > 0).mean()) * 100, 2
            )
            result["negative_event_pct"] = round(
                float((book["net_return"] < 0).mean()) * 100, 2
            )
        paired[mode] = result
        family[mode] = book

    return {
        "events": int(len(events)),
        "signal_days": int(events["signal_date"].nunique()),
        "by_exit_rule": levels,
        "paired_vs_live_current": paired,
        "family_wise_paired_vs_live_current": stats.reality_check(
            family, calendar=calendar, n_boot=5000, block=10
        ) if family else {"applicable": False},
    }


def _target_sensitivity(
    events: pd.DataFrame,
    simulations: dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
) -> dict:
    """Paired target-removal result under one intrabar trail convention."""
    baseline = simulations[BASELINE].loc[events.index, "gross_return"]
    alternative = simulations["trail_no_target"].loc[events.index, "gross_return"]
    return stats.power(
        _paired_book(events, alternative, baseline),
        label="trail_no_target_minus_live_current",
        calendar=calendar,
        n_boot=3000,
        block=10,
    )


def _capacity_summary(
    events: pd.DataFrame, simulations: dict[str, pd.DataFrame]
) -> dict:
    """Apply the live six-slot limit; exit timing determines when a slot reopens."""
    output: dict[str, dict] = {}
    ordered = events.sort_values(
        ["entry_date", "proxy_score"], ascending=[True, False]
    )
    for label, mode in MODES.items():
        details = simulations[mode]
        open_until: list[pd.Timestamp] = []
        accepted: list[int] = []
        for event_index, event in ordered.iterrows():
            entry_day = pd.Timestamp(event["entry_date"])
            open_until = [day for day in open_until if day >= entry_day]
            detail = details.loc[event_index]
            if len(open_until) >= desk.MAX_OPEN or pd.isna(detail.get("exit_date")):
                continue
            accepted.append(event_index)
            open_until.append(pd.Timestamp(detail["exit_date"]))
        returns = details.loc[accepted, "gross_return"] if accepted else pd.Series(dtype=float)
        result = _describe(returns)
        result["capacity_skips"] = int(len(events) - len(accepted))
        output[label] = result
    return output


def run(refresh: bool = False) -> dict:
    frame = harm.build(refresh=refresh)
    payload = base.load_panel(refresh=False)
    panel = base.build_feature_panel(payload)
    calendar = pd.DatetimeIndex(payload["frames"]["IWM"].index)
    proxy = _reaction_confirmed_proxy(frame)
    simulations = {
        mode: _simulate_details(frame, panel, mode) for mode in set(MODES.values())
    }
    # Daily bars do not reveal whether the low happened before or after a new high
    # tightened the trail. Re-run the two target-comparison paths while deferring that
    # newly tightened stop until the next bar. A deployable conclusion must survive
    # both conventions.
    deferred_trail = {
        mode: _simulate_details(frame, panel, mode, same_bar_trail=False)
        for mode in (BASELINE, "trail_no_target")
    }

    dev_mask = frame["signal_date"] <= base.VALIDATION_END
    post_mask = ~dev_mask
    dev_broad, post_broad = frame[dev_mask], frame[post_mask]
    dev_proxy = proxy[proxy["signal_date"] <= base.VALIDATION_END]
    post_proxy = proxy[proxy["signal_date"] > base.VALIDATION_END]

    scopes = {
        "broad_8k": {
            "development": _analyse_scope(dev_broad, simulations, calendar),
            "post_2024_reused": _analyse_scope(post_broad, simulations, calendar),
        },
        "reaction_confirmed_proxy": {
            "development": _analyse_scope(dev_proxy, simulations, calendar),
            "post_2024_reused": _analyse_scope(post_proxy, simulations, calendar),
            "development_six_slot_capacity": _capacity_summary(dev_proxy, simulations),
            "post_2024_six_slot_capacity": _capacity_summary(post_proxy, simulations),
        },
    }
    for scope, development, post in (
        (scopes["broad_8k"], dev_broad, post_broad),
        (scopes["reaction_confirmed_proxy"], dev_proxy, post_proxy),
    ):
        scope["development"]["target_removal_deferred_trail_sensitivity"] = (
            _target_sensitivity(development, deferred_trail, calendar)
        )
        scope["post_2024_reused"]["target_removal_deferred_trail_sensitivity"] = (
            _target_sensitivity(post, deferred_trail, calendar)
        )

    broad_target = scopes["broad_8k"]["development"][
        "paired_vs_live_current"
    ]["trail_no_target"]
    proxy_dev = scopes["reaction_confirmed_proxy"]["development"][
        "paired_vs_live_current"
    ]["trail_no_target"]
    proxy_post = scopes["reaction_confirmed_proxy"]["post_2024_reused"][
        "paired_vs_live_current"
    ]["trail_no_target"]
    broad_positive = bool(
        broad_target.get("bootstrap_95_pct")
        and broad_target["bootstrap_95_pct"][0] > 0
    )
    proxy_dev_deferred = scopes["reaction_confirmed_proxy"]["development"][
        "target_removal_deferred_trail_sensitivity"
    ]
    proxy_post_deferred = scopes["reaction_confirmed_proxy"]["post_2024_reused"][
        "target_removal_deferred_trail_sensitivity"
    ]
    replicated = bool(
        proxy_dev.get("bootstrap_95_pct")
        and proxy_dev["bootstrap_95_pct"][0] > 0
        and proxy_post.get("bootstrap_95_pct")
        and proxy_post["bootstrap_95_pct"][0] > 0
        and proxy_dev_deferred.get("bootstrap_95_pct")
        and proxy_dev_deferred["bootstrap_95_pct"][0] > 0
        and proxy_post_deferred.get("bootstrap_95_pct")
        and proxy_post_deferred["bootstrap_95_pct"][0] > 0
    )

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "assumed_round_trip_cost_pct": ASSUMED_COST * 100,
        "hold_rule": f"first session on/after {HOLD_CALENDAR_DAYS} calendar days",
        "scopes": scopes,
        "deployment_decision": {
            "status": "REJECTED_FOR_DEPLOYMENT",
            "keep_fixed_target_default": True,
            "broad_development_interval_above_zero": broad_positive,
            "reaction_proxy_robust_replication": replicated,
            "exact_live_rule_backtest": False,
            "delisted_inclusive_universe": False,
            "reason": (
                "Target removal is a broad-sample hypothesis, not a validated v3 "
                "exit improvement. It does not replicate with a positive 95% interval "
                "in the reaction-confirmed proxy, and no exact point-in-time v3 or "
                "delisted-inclusive panel exists."
            ),
        },
        "caveats": [
            "post-2024 data was previously examined and is reused, not a clean holdout",
            "the panel contains current survivors and therefore misses delisted failures",
            "daily OHLC bars cannot identify the order of stop, target and trail prints",
            "a constant 0.50% cost is not a substitute for historical executable quotes",
            "the reaction-confirmed scope still lacks v3 headlines, fundamentals, AI veto, "
            "two-scan persistence and market-regime state",
        ],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(REPORT_PATH.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
    os.replace(temporary, REPORT_PATH)
    return report


def _fmt_pair(result: dict) -> str:
    if not result.get("applicable"):
        return result.get("reason", "not applicable")
    interval = result.get("bootstrap_95_pct") or [float("nan"), float("nan")]
    return "%+.3f%% [%+.3f, %+.3f]" % (
        result["mean_net_pct"], interval[0], interval[1]
    )


def main() -> int:
    report = run()
    print("exit audit", report["method_version"])
    for scope_name, scope in report["scopes"].items():
        print("\n" + scope_name)
        for split in ("development", "post_2024_reused"):
            block = scope[split]
            print(" ", split, "events", block["events"], "days", block["signal_days"])
            for mode, result in block["paired_vs_live_current"].items():
                print("    %-22s %s" % (mode, _fmt_pair(result)))
            print("    %-22s %s" % (
                "target/deferred trail",
                _fmt_pair(block["target_removal_deferred_trail_sensitivity"]),
            ))
            family = block["family_wise_paired_vs_live_current"]
            if family.get("applicable"):
                print("    paired family-wise p=%.4f" % family["p_value_selection_aware"])
    decision = report["deployment_decision"]
    print("\ndeployment:", decision["status"])
    print(decision["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
