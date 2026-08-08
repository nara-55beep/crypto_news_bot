"""SEC-fundamental drift audit for profitable penny-stock earnings filings.

Unlike an Item 2.02 flag, quarterly XBRL facts say whether revenue grew and whether the
company actually earned money.  Signals use the value from its original 10-Q accession,
enter only at the first market open after SEC acceptance, and never use analyst estimates
reconstructed years later.

The current-survivor market-data universe remains a blocking limitation.  Passing output
is therefore forward-research evidence, never permission to trade automatically.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import time as wall_time
import json
import math
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import edgar_catalysts as edgar  # noqa: E402
from research import penny_edge_research as base  # noqa: E402
from research import penny_stats  # noqa: E402
from research import sec_fundamentals  # noqa: E402


CACHE_PATH = ROOT / "research" / "cache" / "penny_fundamental_drift_crosssection.pkl"
REPORT_PATH = ROOT / "data" / "pennystock_fundamental_drift_report.json"
METHOD_VERSION = "sec-quarterly-drift-v1-2026-08-08"
NY = ZoneInfo("America/New_York")
HORIZONS = (5, 10, 20)
BASE_COST = 0.005
STRESS_COST = 0.010


@dataclass(frozen=True)
class FundamentalSpec:
    name: str
    kind: str
    hold_sessions: int
    min_revenue_growth_pct: float
    min_dollar_volume: float = 5_000_000
    max_entries_per_day: int = 3


SPECS = (
    FundamentalSpec("profitable_growth_10d", "profitable_growth", 10, 10.0),
    FundamentalSpec("profitable_growth_20d", "profitable_growth", 20, 10.0),
    FundamentalSpec("fast_profitable_growth_10d", "profitable_growth", 10, 25.0),
    FundamentalSpec("fast_profitable_growth_20d", "profitable_growth", 20, 25.0),
    FundamentalSpec("profit_turnaround_10d", "turnaround", 10, 0.0),
    FundamentalSpec("profit_acceleration_10d", "acceleration", 10, 10.0),
)


def _json_dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
    os.replace(temporary, path)


def entry_position(index: pd.DatetimeIndex, accepted_at: str) -> int:
    """First regular-session open safely after SEC dissemination."""
    accepted = pd.to_datetime(accepted_at, utc=True, errors="coerce")
    if pd.isna(accepted):
        return -1
    eastern = accepted.tz_convert(NY)
    day = pd.Timestamp(eastern.date())
    # Require five minutes before the opening auction; anything later waits a session.
    side = "left" if eastern.time() <= wall_time(9, 25) else "right"
    position = int(index.searchsorted(day, side=side))
    return position if 0 <= position < len(index) else -1


def _adjusted_iwm(payload: dict) -> pd.DataFrame:
    raw = payload["frames"].get("IWM")
    return base._features("IWM", raw, raw) if raw is not None and not raw.empty else pd.DataFrame()


def build(refresh: bool = False) -> pd.DataFrame:
    panel_payload = base.load_panel(refresh=False)
    fundamentals = sec_fundamentals.load()
    snapshot = {
        "method": METHOD_VERSION,
        "panel_created_at": panel_payload["metadata"].get("created_at"),
        "facts_created_at": fundamentals["metadata"].get("created_at"),
    }
    if CACHE_PATH.exists() and not refresh:
        cached = pd.read_pickle(CACHE_PATH)
        if isinstance(cached, dict) and cached.get("snapshot") == snapshot:
            return cached["rows"]

    panel = base.build_feature_panel(panel_payload)
    iwm = _adjusted_iwm(panel_payload)
    filing_calendars = edgar.calendars(edgar.load())
    rows = []
    for symbol, events in (fundamentals.get("events") or {}).items():
        frame = panel.get(symbol)
        if frame is None or frame.empty:
            continue
        for event in events:
            growth = event.get("revenue_growth_pct")
            prior_income = event.get("prior_net_income")
            if growth is None or prior_income is None:
                continue
            position = entry_position(frame.index, str(event.get("accepted_at") or ""))
            if position < 126 or position + max(HORIZONS) > len(frame):
                continue
            prior = frame.iloc[position - 1]
            entry = float(frame.iloc[position]["open"])
            if not math.isfinite(entry) or entry <= 0:
                continue
            entry_date = pd.Timestamp(frame.index[position])
            calendars = filing_calendars.get(symbol) or {}
            row = {
                "ticker": symbol,
                "signal_date": pd.Timestamp(str(event["filing_date"])),
                "entry_date": entry_date,
                "accepted_at": event["accepted_at"],
                "accessionNumber": event["accessionNumber"],
                "period_end": event["period_end"],
                "revenue": float(event["revenue"]),
                "net_income": float(event["net_income"]),
                "prior_revenue": float(event["prior_revenue"]),
                "prior_net_income": float(prior_income),
                "revenue_growth_pct": float(growth),
                "net_income_growth_pct": (
                    float(event["net_income_growth_pct"])
                    if event.get("net_income_growth_pct") is not None else np.nan
                ),
                "eps_diluted": (
                    float(event["eps_diluted"])
                    if event.get("eps_diluted") is not None else np.nan
                ),
                "raw_close": float(prior.get("raw_close") or 0.0),
                "dollar_volume20": float(prior.get("dollar_volume20") or 0.0),
                "atr_pct": float(prior.get("atr_pct") or 0.0),
                "max_ret20": float(prior.get("max_ret20") or 0.0),
                "iwm_risk_on": bool(prior.get("iwm_risk_on")),
                "dilution_age_days": edgar.days_since(calendars.get("dilution"), entry_date),
            }
            for horizon in HORIZONS:
                exit_position = position + horizon - 1
                exit_close = float(frame.iloc[exit_position]["close"])
                gross = exit_close / entry - 1.0 if exit_close > 0 else float("nan")
                row[f"gross_{horizon}"] = gross
                excess = float("nan")
                if not iwm.empty:
                    exit_date = frame.index[exit_position]
                    if entry_date in iwm.index and exit_date in iwm.index:
                        bench_entry = float(iwm.loc[entry_date, "open"])
                        bench_exit = float(iwm.loc[exit_date, "close"])
                        if bench_entry > 0 and bench_exit > 0:
                            excess = gross - (bench_exit / bench_entry - 1.0)
                row[f"excess_{horizon}"] = excess
            rows.append(row)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["signal_date", "ticker"]).reset_index(drop=True)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle({"snapshot": snapshot, "rows": frame}, CACHE_PATH)
    return frame


def apply_spec(events: pd.DataFrame, spec: FundamentalSpec) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    mask = events["raw_close"].between(1.0, 5.0)
    mask &= events["dollar_volume20"] >= spec.min_dollar_volume
    mask &= events["atr_pct"].between(0.02, 0.20)
    mask &= events["max_ret20"] < 0.35
    mask &= events["dilution_age_days"] > 90
    mask &= events["revenue"] >= 5_000_000
    mask &= events["revenue_growth_pct"] >= spec.min_revenue_growth_pct
    mask &= events["net_income"] > 0
    if spec.kind == "turnaround":
        mask &= events["prior_net_income"] <= 0
    elif spec.kind == "acceleration":
        mask &= events["prior_net_income"] > 0
        mask &= events["net_income_growth_pct"] >= 20

    selected = events[mask].copy()
    if selected.empty:
        return pd.DataFrame()
    selected["score"] = (
        selected["revenue_growth_pct"].clip(upper=200)
        + 0.25 * selected["net_income_growth_pct"].fillna(0).clip(-100, 300)
        + 2.0 * np.log1p(selected["dollar_volume20"] / 1_000_000)
    )
    selected = (
        selected.sort_values(["signal_date", "score"], ascending=[True, False])
        .groupby("signal_date", group_keys=False)
        .head(spec.max_entries_per_day)
        .reset_index(drop=True)
    )
    gross = selected[f"gross_{spec.hold_sessions}"].astype(float)
    selected["gross_return"] = gross
    selected["cost"] = BASE_COST
    selected["net_return"] = gross - BASE_COST
    selected["stress_net_return"] = gross - STRESS_COST
    selected["held_days"] = spec.hold_sessions
    selected["exit_reason"] = "time"
    return selected


def run(refresh: bool = False) -> dict:
    events = build(refresh=refresh)
    candidates: dict[str, pd.DataFrame] = {}
    results = []
    for spec in SPECS:
        trades = apply_spec(events, spec)
        candidates[spec.name] = trades
        splits = base.split_metrics(trades)
        result = {"strategy": asdict(spec), "splits": splits}
        result["selection_score"] = round(base._selection_score(result), 6)
        results.append(result)
    selected = max(results, key=lambda value: value["selection_score"])
    selected_name = selected["strategy"]["name"]
    numeric_pass, failures = base._numeric_gate(selected["splits"])
    panel_payload = base.load_panel(refresh=False)
    inference = penny_stats.summarise(
        candidates,
        selected_name,
        base.TRAIN_END,
        base.VALIDATION_END,
        int(selected["strategy"]["hold_sessions"]),
        calendar=pd.DatetimeIndex(panel_payload["frames"]["IWM"].index),
        winner_capacity_cap=int(selected["strategy"]["max_entries_per_day"]),
    )
    selection = inference.get("selection_test") or {}
    if selection.get("applicable") and not selection.get("significant_at_5pct"):
        numeric_pass = False
        failures.append(
            "selection-aware family test failed "
            f"(p={float(selection.get('p_value_selection_aware')):.3f})"
        )
    status = "PROMISING_NOT_VALIDATED" if numeric_pass else "REJECTED"
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "status": status,
        "auto_trade_allowed": False,
        "selected_strategy": selected_name,
        "failed_checks": failures,
        "event_rows": int(len(events)),
        "candidate_family": results,
        "inference": inference,
        "cost_model": {
            "base_round_trip_pct": BASE_COST * 100,
            "stress_round_trip_pct": STRESS_COST * 100,
        },
        "limitations": [
            "Yahoo universe contains current survivors and excludes historical delistings",
            "Company Facts standard tags do not cover every issuer or custom taxonomy",
            "historical bid/ask and market impact are unavailable",
            "candidate family requires genuinely forward confirmation even if numerical gates pass",
        ],
        "reason": (
            "Numerically promising, but survivorship and execution data block authorization."
            if numeric_pass else
            "No predeclared SEC-fundamental rule survived the numerical and search-aware gates."
        ),
    }
    _json_dump(REPORT_PATH, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-facts", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh_facts:
        panel_payload = base.load_panel(refresh=False)
        symbols = [symbol for symbol in panel_payload["frames"] if symbol != "IWM"]
        sec_fundamentals.download(symbols)
        args.rebuild = True
    report = run(refresh=args.rebuild)
    print("status:", report["status"], "| selected:", report["selected_strategy"])
    print("eligible quarterly events:", report["event_rows"])
    for result in report["candidate_family"]:
        test = result["splits"]["test"]
        print(
            f"  {result['strategy']['name']:<32} test n={test['trades']:>4} "
            f"net={test['mean_net_pct']:+.3f}% PF={test['profit_factor']:.2f}"
        )
    print("auto trade allowed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
