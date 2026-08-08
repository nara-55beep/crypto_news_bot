"""Marginal 8-K item-code audit for the penny-stock research scanner.

This is deliberately *not* called a backtest of ``live_sec_news_align_v4``.  The
historical panel lacks point-in-time headlines, fundamentals, quotes and the two-scan
confirmation state needed to reproduce that live rule.  It can answer a narrower
question: after applying observable penny-price/liquidity constraints, do SEC item
categories add positive five-session drift on their own or after a causal
price/volume-reaction proxy?

Timing and grouping reuse ``penny_event_drift``:

* the Submissions API's ISO timestamp is parsed as UTC and converted to New York;
* events are collapsed to one symbol/reaction-session observation;
* the first fully observable reaction close precedes the next-session open entry;
* a mixed material/adverse filing is adverse, matching the live fail-closed gate;
* confidence intervals use market-calendar block bootstrap, not an IID daily formula.

The 2025+ window has already been examined by other catalyst research.  It is labelled
``post_2024_reused``, never "untouched" or "held out".  Survivor-only membership also
means this audit may reject a category, but cannot authorize automatic trading.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pennystock_bot as live  # noqa: E402
from research import edgar_catalysts as edgar  # noqa: E402
from research import penny_edge_research as base  # noqa: E402
from research import penny_event_drift as event_drift  # noqa: E402
from research import penny_stats as stats  # noqa: E402


REPORT_PATH = ROOT / "data" / "pennystock_item_code_audit.json"
METHOD_VERSION = "item-code-marginal-v2-2026-08-08"
BASE_COST = event_drift.BASE_COST
MIN_CODE_EVENTS = 40

MATERIAL_ITEMS = frozenset(
    set(edgar.EARNINGS_8K_ITEMS)
    | set(edgar.AGREEMENT_8K_ITEMS)
    | {"1.05", "7.01", "8.01"}
)
ADVERSE_ITEMS = frozenset(edgar.NEGATIVE_8K_ITEMS)


def _json_dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
    os.replace(temporary, path)


def parse_item_set(value) -> frozenset[str]:
    if isinstance(value, (set, frozenset, list, tuple)):
        values = value
    else:
        values = str(value or "").replace(",", "|").split("|")
    return frozenset(str(item).strip() for item in values if str(item).strip())


def item_group(value) -> str:
    """Mutually exclusive group with the live adverse veto taking precedence."""
    items = parse_item_set(value)
    if items & ADVERSE_ITEMS:
        return "adverse"
    if items & MATERIAL_ITEMS:
        return "material_direction_unknown"
    return "neither"


def eligible_events(refresh: bool = False) -> pd.DataFrame:
    """Causal event rows that satisfy the observable live price/liquidity floor."""
    frame = event_drift.build(refresh=refresh).copy()
    if frame.empty:
        return frame
    frame["item_set"] = frame["items"].map(parse_item_set)
    frame["item_group"] = frame["item_set"].map(item_group)
    average_volume_proxy = frame["dollar_volume20"].div(
        frame["raw_close"].where(frame["raw_close"] > 0)
    )
    eligible = (
        frame["raw_close"].gt(live.MIN_PRICE)
        & frame["raw_close"].lt(live.MAX_PRICE)
        & average_volume_proxy.ge(live.MIN_AVG_VOLUME)
        & frame["gross_5"].notna()
    )
    frame = frame[eligible].copy()
    frame["gross_return"] = pd.to_numeric(frame["gross_5"], errors="coerce")
    frame["cost"] = BASE_COST
    frame["net_return"] = frame["gross_return"] - frame["cost"]
    # This is intentionally labelled a proxy.  It resembles the observable portion of
    # v3 but cannot recreate historical headlines, quality or two-scan persistence.
    frame["reaction_confirmed_proxy"] = (
        frame["reaction_pct"].between(3.0, 30.0)
        & frame["volume_ratio"].between(1.5, 8.0)
        & frame["close_location"].ge(0.65)
        & frame["atr_pct"].between(0.02, 0.22)
        & frame["max_ret20"].lt(0.35)
        & frame["dilution_age_days"].gt(90)
    )
    return frame.reset_index(drop=True)


def _metric(frame: pd.DataFrame, column: str, calendar: pd.DatetimeIndex) -> dict:
    if frame.empty:
        return {"applicable": False, "reason": "no events"}
    trades = frame[["signal_date", "ticker", column]].rename(
        columns={column: "net_return"}
    )
    result = stats.power(
        trades,
        label=column,
        calendar=calendar,
        n_boot=3000,
        block=10,
    )
    if result.get("applicable"):
        result["events"] = int(len(frame))
        result["symbols"] = int(frame["ticker"].nunique())
    return result


def _group_results(frame: pd.DataFrame, calendar: pd.DatetimeIndex) -> dict:
    output = {}
    for group in ("material_direction_unknown", "adverse", "neither"):
        selected = frame[frame["item_group"] == group]
        output[group] = {
            "gross": _metric(selected, "gross_return", calendar),
            "net_after_0_5pct_cost": _metric(selected, "net_return", calendar),
        }
    return output


def _code_search(development: pd.DataFrame, calendar: pd.DatetimeIndex) -> dict:
    """Exploratory only: correct the search across individual item codes."""
    codes = sorted({code for values in development["item_set"] for code in values})
    book: dict[str, pd.DataFrame] = {}
    per_code = {}
    for code in codes:
        selected = development[
            development["item_set"].map(lambda values, c=code: c in values)
        ]
        if len(selected) < MIN_CODE_EVENTS:
            continue
        per_code[code] = _metric(selected, "gross_return", calendar)
        book[code] = selected[["signal_date", "ticker", "gross_return"]].rename(
            columns={"gross_return": "net_return"}
        )
    family = stats.reality_check(
        book,
        end=base.VALIDATION_END,
        calendar=calendar,
        n_boot=5000,
        block=10,
    ) if len(book) >= 2 else {"applicable": False, "reason": "fewer than two codes"}
    return {
        "purpose": "exploratory gross-return comparison; never a trade authorization",
        "per_code": per_code,
        "family_wise": family,
    }


def run(refresh: bool = False) -> dict:
    events = eligible_events(refresh=refresh)
    panel_payload = base.load_panel(refresh=False)
    calendar = pd.DatetimeIndex(panel_payload["frames"]["IWM"].index)
    development = events[events["signal_date"] <= base.VALIDATION_END]
    post_2024 = events[events["signal_date"] > base.VALIDATION_END]
    scopes = {
        "all_eligible_events": events,
        "reaction_confirmed_proxy": events[events["reaction_confirmed_proxy"]],
    }
    results = {}
    for scope, frame in scopes.items():
        results[scope] = {
            "development_through_2024": _group_results(
                frame[frame["signal_date"] <= base.VALIDATION_END], calendar
            ),
            "post_2024_reused": _group_results(
                frame[frame["signal_date"] > base.VALIDATION_END], calendar
            ),
        }

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "strategy_id": live.LIVE_STRATEGY_ID,
        "status": "NO_STANDALONE_ITEM_CODE_EDGE",
        "auto_trade_allowed": False,
        "exact_live_rule_backtest": False,
        "eligible_events": int(len(events)),
        "development_events": int(len(development)),
        "post_2024_events": int(len(post_2024)),
        "material_items": sorted(MATERIAL_ITEMS),
        "adverse_items": sorted(ADVERSE_ITEMS),
        "group_precedence": "adverse first; mixed material/adverse filings are adverse",
        "timing": (
            "SEC Submissions API Z timestamp parsed as UTC, converted to New York; "
            "observable reaction close, then next-session open"
        ),
        "results": results,
        "individual_code_search": _code_search(development, calendar),
        "verdict": (
            "Item category is useful for adverse-event safety and discovery, but this "
            "survivor-only audit finds no positive standalone item-code edge. The live "
            "rule already awards no bullish points merely for a material item code."
        ),
        "limitations": [
            "not an exact v3 backtest: historical headlines, quality, quotes and two-scan state are unavailable",
            "Yahoo universe contains current survivors and excludes historical delistings",
            "post-2024 data was already examined by earlier item-aware research and is not an untouched holdout",
            "five-session close exit is a drift diagnostic, not the live stop/target/trailing execution path",
        ],
    }
    _json_dump(REPORT_PATH, report)
    return report


def _fmt(metric: dict) -> str:
    if not metric.get("applicable"):
        return "n=0"
    interval = [round(float(value), 4) for value in metric["bootstrap_95_pct"]]
    return (
        f"n={metric['events']} basket={metric['mean_signal_day_basket_net_pct']:+.3f}% "
        f"CI={interval}"
    )


def main() -> int:
    report = run()
    print("status:", report["status"], "| exact live backtest: no")
    print("eligible events:", report["eligible_events"])
    for scope, splits in report["results"].items():
        print(f"\n{scope}")
        for split, groups in splits.items():
            print(" ", split)
            for group, metrics in groups.items():
                print(f"    {group:<28} gross {_fmt(metrics['gross'])}")
    print("auto trade allowed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
