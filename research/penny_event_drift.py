"""Item-aware SEC 8-K drift audit for the penny-stock scanner.

This is a new strategy family, not another set of weights for the rejected live
composite.  It uses the SEC acceptance timestamp and 8-K item codes, waits for the
first full market close after dissemination, and enters at the following session's
open.  That timing makes the initial reaction, volume and closing location observable
before the decision and prevents after-hours filings from leaking into the signal.

The historical universe still contains today's survivors, and the 2025+ period has
already been viewed by earlier broad catalyst research.  Consequently this audit can
reject an idea or label it promising for forward collection, but it can never authorize
automatic trading.
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


CACHE_PATH = ROOT / "research" / "cache" / "penny_event_drift_crosssection.pkl"
REPORT_PATH = ROOT / "data" / "pennystock_event_drift_report.json"
METHOD_VERSION = "sec-item-drift-v1-2026-08-08"
NY = ZoneInfo("America/New_York")
HORIZONS = (3, 5, 10)
BASE_COST = 0.005   # observed live spread (~0.335%) plus a slippage allowance
STRESS_COST = 0.010


ITEM_GROUPS = {
    "earnings": frozenset({"2.02"}),
    "agreement": frozenset({"1.01", "2.01"}),
    "fd_or_other": frozenset({"7.01", "8.01"}),
    "material": frozenset({"1.01", "1.05", "2.01", "2.02", "7.01", "8.01"}),
}


@dataclass(frozen=True)
class EventSpec:
    name: str
    item_group: str
    hold_sessions: int
    min_reaction_pct: float
    max_reaction_pct: float
    min_volume_ratio: float
    min_close_location: float
    min_dollar_volume: float
    max_entries_per_day: int = 3


# Economic hypotheses are declared before the item-level results are examined:
# earnings drift, agreement/transaction drift, and Regulation-FD/other disclosures.
# Two horizons for the two strongest prior hypotheses keep the searched family small.
SPECS = (
    EventSpec("sec_earnings_drift_5d", "earnings", 5, 3.0, 30.0, 1.5, 0.65, 5_000_000),
    EventSpec("sec_earnings_drift_10d", "earnings", 10, 3.0, 30.0, 1.5, 0.65, 5_000_000),
    EventSpec("sec_agreement_drift_3d", "agreement", 3, 4.0, 30.0, 1.5, 0.65, 5_000_000),
    EventSpec("sec_agreement_drift_5d", "agreement", 5, 4.0, 30.0, 1.5, 0.65, 5_000_000),
    EventSpec("sec_fd_other_drift_3d", "fd_or_other", 3, 4.0, 30.0, 1.5, 0.65, 5_000_000),
    EventSpec("sec_material_drift_5d", "material", 5, 4.0, 25.0, 2.0, 0.70, 10_000_000),
)


def _json_dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)


def reaction_position(index: pd.DatetimeIndex, accepted_at: str) -> int:
    """First session whose close can contain the filing's market reaction.

    A filing accepted before 16:00 ET can affect that session's close.  After-close,
    weekend and holiday filings use the next session.  The strategy enters one session
    *after* this returned position, so it never fills at a price observed only later.
    """
    accepted = pd.to_datetime(accepted_at, utc=True, errors="coerce")
    if pd.isna(accepted):
        return -1
    eastern = accepted.tz_convert(NY)
    day = pd.Timestamp(eastern.date())
    side = "left" if eastern.time() < wall_time(16, 0) else "right"
    position = int(index.searchsorted(day, side=side))
    return position if 0 <= position < len(index) else -1


def _adjusted_iwm(payload: dict) -> pd.DataFrame:
    raw = payload["frames"].get("IWM")
    if raw is None or raw.empty:
        return pd.DataFrame()
    return base._features("IWM", raw, raw)


def _event_groups(payload: dict, panel: dict[str, pd.DataFrame]) -> dict[tuple[str, pd.Timestamp], dict]:
    groups: dict[tuple[str, pd.Timestamp], dict] = {}
    for symbol, frame in panel.items():
        for filing in edgar.filing_rows(payload, symbol):
            if str(filing.get("form") or "").upper() not in edgar.NEWS_FORMS:
                continue
            position = reaction_position(frame.index, str(filing.get("acceptanceDateTime") or ""))
            if position < 126 or position + max(HORIZONS) >= len(frame):
                continue
            signal_date = pd.Timestamp(frame.index[position])
            key = (symbol, signal_date)
            group = groups.setdefault(key, {
                "symbol": symbol,
                "signal_date": signal_date,
                "position": position,
                "items": set(),
                "accessions": set(),
                "accepted_at": [],
            })
            group["items"].update(str(x) for x in (filing.get("items") or []))
            if filing.get("accessionNumber"):
                group["accessions"].add(str(filing["accessionNumber"]))
            if filing.get("acceptanceDateTime"):
                group["accepted_at"].append(str(filing["acceptanceDateTime"]))
    return groups


def build(refresh: bool = False) -> pd.DataFrame:
    panel_payload = base.load_panel(refresh=False)
    edgar_payload = edgar.load()
    schema = int((edgar_payload.get("metadata") or {}).get("schema_version") or 1)
    if schema < edgar.SCHEMA_VERSION:
        raise RuntimeError(
            "EDGAR cache lacks acceptance timestamps/item codes; run with --refresh-edgar"
        )

    snapshot = {
        "method": METHOD_VERSION,
        "panel_created_at": panel_payload["metadata"].get("created_at"),
        "edgar_created_at": edgar_payload["metadata"].get("created_at"),
    }
    if CACHE_PATH.exists() and not refresh:
        cached = pd.read_pickle(CACHE_PATH)
        if isinstance(cached, dict) and cached.get("snapshot") == snapshot:
            return cached["rows"]

    panel = base.build_feature_panel(panel_payload)
    iwm = _adjusted_iwm(panel_payload)
    dilution = edgar.calendars(edgar_payload)
    groups = _event_groups(edgar_payload, panel)
    rows: list[dict] = []

    for (symbol, signal_date), event in groups.items():
        frame = panel[symbol]
        pos = int(event["position"])
        signal = frame.iloc[pos]
        prior_close = float(frame.iloc[pos - 1]["close"])
        reaction_close = float(signal["close"])
        if not (math.isfinite(prior_close) and prior_close > 0
                and math.isfinite(reaction_close) and reaction_close > 0):
            continue
        entry_pos = pos + 1
        entry = float(frame.iloc[entry_pos]["open"])
        if not math.isfinite(entry) or entry <= 0:
            continue
        items = set(event["items"])
        cals = dilution.get(symbol) or {}
        row = {
            "ticker": symbol,
            "signal_date": signal_date,
            "entry_date": pd.Timestamp(frame.index[entry_pos]),
            "accepted_at": max(event["accepted_at"]) if event["accepted_at"] else "",
            "accessions": "|".join(sorted(event["accessions"])),
            "items": "|".join(sorted(items)),
            "negative_event": bool(items & edgar.NEGATIVE_8K_ITEMS),
            "reaction_pct": (reaction_close / prior_close - 1.0) * 100.0,
            "volume_ratio": float(signal.get("volume_ratio") or 0.0),
            "close_location": float(signal.get("close_location") or 0.0),
            "raw_close": float(signal.get("raw_close") or 0.0),
            "dollar_volume20": float(signal.get("dollar_volume20") or 0.0),
            "atr_pct": float(signal.get("atr_pct") or 0.0),
            "max_ret20": float(signal.get("max_ret20") or 0.0),
            "dilution_age_days": edgar.days_since(cals.get("dilution"), signal_date),
        }
        for horizon in HORIZONS:
            exit_pos = entry_pos + horizon - 1
            exit_close = float(frame.iloc[exit_pos]["close"])
            gross = exit_close / entry - 1.0 if exit_close > 0 else float("nan")
            row[f"gross_{horizon}"] = gross

            excess = float("nan")
            if not iwm.empty:
                entry_day = frame.index[entry_pos]
                exit_day = frame.index[exit_pos]
                if entry_day in iwm.index and exit_day in iwm.index:
                    bench_entry = float(iwm.loc[entry_day, "open"])
                    bench_exit = float(iwm.loc[exit_day, "close"])
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


def _has_group(items: str, group: str) -> bool:
    observed = frozenset(str(items or "").split("|"))
    return bool(observed & ITEM_GROUPS[group])


def apply_spec(events: pd.DataFrame, spec: EventSpec) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    mask = events["items"].map(lambda value: _has_group(value, spec.item_group))
    mask &= ~events["negative_event"]
    mask &= events["raw_close"].between(1.0, 5.0)
    mask &= events["reaction_pct"].between(spec.min_reaction_pct, spec.max_reaction_pct)
    mask &= events["volume_ratio"] >= spec.min_volume_ratio
    mask &= events["close_location"] >= spec.min_close_location
    mask &= events["dollar_volume20"] >= spec.min_dollar_volume
    mask &= events["atr_pct"].between(0.02, 0.20)
    mask &= events["max_ret20"] < 0.35
    mask &= events["dilution_age_days"] > 90
    selected = events[mask].copy()
    if selected.empty:
        return pd.DataFrame()

    selected["score"] = (
        selected["reaction_pct"]
        * np.log1p(selected["volume_ratio"].clip(lower=0))
        * np.log1p(selected["dollar_volume20"].clip(lower=0) / 1_000_000)
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
    family_test = inference.get("selection_test") or {}
    if family_test.get("applicable") and not family_test.get("significant_at_5pct"):
        numeric_pass = False
        failures.append(
            "selection-aware family test failed "
            f"(p={float(family_test.get('p_value_selection_aware')):.3f})"
        )

    limitations = [
        "Yahoo universe contains current survivors and excludes historical delistings",
        "historical bid/ask and market impact are unavailable",
        "2025+ was seen by prior broad catalyst research and is not a pristine holdout",
        "SEC item codes identify disclosure type, not whether its content is positive",
    ]
    status = "PROMISING_NOT_VALIDATED" if numeric_pass else "REJECTED"
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "status": status,
        "auto_trade_allowed": False,
        "selected_strategy": selected_name,
        "failed_checks": failures,
        "event_rows": int(len(events)),
        "cost_model": {
            "base_round_trip_pct": BASE_COST * 100,
            "stress_round_trip_pct": STRESS_COST * 100,
            "basis": "observed live spread plus slippage allowance; stress doubles base",
        },
        "timing": (
            "SEC acceptance -> first fully observable reaction close -> next-session open entry"
        ),
        "candidate_family": results,
        "inference": inference,
        "limitations": limitations,
        "reason": (
            "Candidate family passed numerical screens but requires genuinely forward, "
            "delisted-inclusive evidence before trading."
            if numeric_pass else
            "No predeclared item-aware event rule survived the numerical and search-aware gates."
        ),
    }
    _json_dump(REPORT_PATH, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-edgar", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh_edgar:
        panel_payload = base.load_panel(refresh=False)
        symbols = [x for x in panel_payload["frames"] if x != "IWM"]
        edgar.download(symbols)
        args.rebuild = True
    report = run(refresh=args.rebuild)
    print("status:", report["status"], "| selected:", report["selected_strategy"])
    for result in report["candidate_family"]:
        test = result["splits"]["test"]
        print(
            f"  {result['strategy']['name']:<30} test n={test['trades']:>4} "
            f"net={test['mean_net_pct']:+.3f}% PF={test['profit_factor']:.2f}"
        )
    print("auto trade allowed: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
