"""Estimate whether a candidate rule can be evaluated within a planning horizon.

This is a research-planning check, not evidence of profitability.  It estimates the
smallest signal-day basket effect the available sample can resolve and, under explicit
stationarity assumptions, how much additional calendar history an effect of a chosen
size would require.

The historical streams available in this repository are proxies.  In particular, the
reaction-confirmed 8-K filter is closer to the deployed v3 rule than the broad event
sample, but it still lacks point-in-time headlines, fundamentals, AI vetoes, two-scan
persistence, executable quotes, regime state and delisted companies.  It must never be
labelled the desk's exact entry population.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import penny_stats as stats   # noqa: E402

PLANNING_HORIZON_YEARS = 5.0
TARGET_EFFECT_PCT = 2.0
MIN_SIGNAL_DAYS = 5
MIN_RATE_HISTORY_YEARS = 1.0
V3_STRATEGY_ID = "live_sec_item_confirm_v3"
V3_ENGINE_VERSION = 5


def feasibility(
    returns: pd.Series,
    dates: pd.Series,
    target_effect_pct: float = TARGET_EFFECT_PCT,
    patience_years: float = PLANNING_HORIZON_YEARS,
    *,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
    block: int = 10,
    n_boot: int = 3000,
    min_history_years: float = MIN_RATE_HISTORY_YEARS,
) -> dict:
    """Dependence-adjusted power and time-to-sample-size estimate.

    Returns are averaged within signal day, then a circular market-calendar block
    bootstrap estimates uncertainty.  The future calculation assumes the observed
    signal-day rate and dependence-adjusted dispersion remain stationary and uncertainty
    continues to shrink with ``sqrt(n)``.  Those are planning assumptions, not facts.
    """
    if not np.isfinite(target_effect_pct) or target_effect_pct <= 0:
        return {"applicable": False, "reason": "target effect must be positive"}
    if not np.isfinite(patience_years) or patience_years < 0:
        return {"applicable": False, "reason": "planning horizon cannot be negative"}

    frame = pd.DataFrame(
        {
            "net_return": pd.to_numeric(returns, errors="coerce"),
            "signal_date": pd.to_datetime(dates, errors="coerce", format="mixed"),
        }
    ).dropna()
    events = len(frame)
    if events < MIN_SIGNAL_DAYS:
        return {"applicable": False, "reason": f"only {events} usable events"}
    frame["signal_date"] = frame["signal_date"].dt.tz_localize(None).dt.normalize()
    signal_days = int(frame["signal_date"].nunique())
    if signal_days < MIN_SIGNAL_DAYS:
        return {
            "applicable": False,
            "events": int(events),
            "independent_signal_days": signal_days,
            "reason": f"only {signal_days} distinct signal days",
        }

    first, last = frame["signal_date"].min(), frame["signal_date"].max()
    history_years = max((last - first).days / 365.25, 1.0 / 365.25)
    if history_years < min_history_years:
        return {
            "applicable": False,
            "events": int(events),
            "independent_signal_days": signal_days,
            "history_years": round(history_years, 3),
            "reason": (
                f"only {history_years:.2f} years of rate history; need "
                f"{min_history_years:.2f}"
            ),
        }

    trades = frame.copy()
    trades["ticker"] = "STREAM"
    power = stats.power(
        trades,
        label="feasibility_stream",
        calendar=calendar,
        n_boot=n_boot,
        block=block,
    )
    if not power.get("applicable"):
        return power

    n = int(power["signal_days"])
    bootstrap_se = float(power["standard_error_pct"]) / 100.0
    effective_sd = bootstrap_se * np.sqrt(n)
    target = target_effect_pct / 100.0
    critical = stats.Z_975 + stats.Z_80
    needed = (critical * effective_sd / target) ** 2
    needed_days = max(n, int(np.ceil(needed))) if np.isfinite(needed) else None
    day_rate = n / history_years
    event_rate = events / history_years
    additional_days = max(0, needed_days - n) if needed_days is not None else None
    total_years = needed_days / day_rate if needed_days is not None and day_rate else None
    additional_years = (
        additional_days / day_rate
        if additional_days is not None and day_rate else None
    )
    mde = float(power["min_detectable_edge_pct"])
    already = bool(mde <= target_effect_pct)
    reachable = bool(
        additional_years is not None and additional_years <= patience_years
    )
    status = (
        "RESOLVABLE_NOW"
        if already else
        "REACHABLE_WITHIN_HORIZON"
        if reachable else
        "INFEASIBLE_WITHIN_HORIZON"
    )

    return {
        "applicable": True,
        "status": status,
        "events": int(events),
        "independent_signal_days": n,
        "history_years": round(history_years, 2),
        "events_per_year": round(event_rate, 2),
        "signal_days_per_year": round(day_rate, 2),
        "daily_basket_sd_pct": power["daily_sd_pct"],
        "dependence_adjusted_sd_pct": round(effective_sd * 100, 4),
        "autocorrelation_inflation_vs_iid": power[
            "autocorrelation_inflation_vs_iid"
        ],
        "min_detectable_effect_pct": round(mde, 4),
        "target_effect_pct": float(target_effect_pct),
        "signal_days_needed": needed_days,
        "additional_signal_days_needed": additional_days,
        "total_years_required": round(total_years, 1) if total_years is not None else None,
        "additional_years_required": (
            round(additional_years, 1) if additional_years is not None else None
        ),
        "already_resolvable": already,
        "verdict_reachable_within_patience": reachable,
        "planning_horizon_years": float(patience_years),
        "confidence_pct": 95,
        "power_pct": 80,
        "standard_error_method": power["standard_error_method"],
        "block_days": power["block_days"],
        "assumptions": [
            "future signal-day rate remains equal to the historical rate",
            "dependence-adjusted dispersion remains stationary",
            "standard error continues to shrink with the square root of signal days",
        ],
    }


def verdict_line(result: dict) -> str:
    """One accurate planning sentence; never confuse feasibility with evidence."""
    if not result.get("applicable"):
        return f"not assessable: {result.get('reason')}"
    if result["status"] == "RESOLVABLE_NOW":
        return (
            f"power is sufficient now to resolve a {result['target_effect_pct']:.1f}% "
            f"signal-day effect; this says nothing about its sign or existence"
        )
    if result["status"] == "REACHABLE_WITHIN_HORIZON":
        return (
            f"power target is reachable in about "
            f"{result['additional_years_required']:.1f} additional years at "
            f"{result['signal_days_per_year']:.1f} signal days/yr"
        )
    return (
        f"INFEASIBLE WITHIN {result['planning_horizon_years']:.0f}Y: resolving a "
        f"{result['target_effect_pct']:.1f}% effect is estimated to require "
        f"{result['additional_years_required']:.1f} additional years; current MDE is "
        f"{result['min_detectable_effect_pct']:.2f}%"
    )


def compare(
    streams: dict[str, tuple[pd.Series, pd.Series] | dict],
    target_effect_pct: float = TARGET_EFFECT_PCT,
    **kwargs,
) -> dict:
    """Compare proxy or exact streams while preserving their scope metadata."""
    output = {}
    for name, stream in streams.items():
        if isinstance(stream, dict):
            returns, dates = stream["returns"], stream["dates"]
            metadata = {
                key: value for key, value in stream.items()
                if key not in {"returns", "dates"}
            }
        else:
            returns, dates = stream
            metadata = {}
        result = feasibility(
            returns, dates, target_effect_pct=target_effect_pct, **kwargs
        )
        result.update(metadata)
        output[name] = result
    return output


def _prospective_v3_stream() -> tuple[pd.Series, pd.Series]:
    """Completed 5-session outcomes recorded by the exact current signal engine."""
    path = ROOT / "data" / "pennystock_paper_state.json"
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return pd.Series(dtype=float), pd.Series(dtype="datetime64[ns]")
    returns, dates = [], []
    for signal in state.get("signal_log") or []:
        if (signal.get("strategy_id") != V3_STRATEGY_ID
                or signal.get("engine_version") != V3_ENGINE_VERSION):
            continue
        outcome = (signal.get("outcomes") or {}).get("5") or {}
        value = outcome.get("net_return_pct")
        day = signal.get("signal_day")
        if value is None or not day:
            continue
        returns.append(float(value) / 100.0)
        dates.append(day)
    return pd.Series(returns, dtype=float), pd.to_datetime(
        pd.Series(dates), errors="coerce", format="mixed"
    )


def main() -> int:
    from research import penny_edge_research as base
    from research import penny_exit_structure as exits
    from research import penny_harm_model as harm

    frame = harm.build()
    proxy = exits._reaction_confirmed_proxy(frame)
    live_returns, live_dates = _prospective_v3_stream()
    calendar = pd.DatetimeIndex(base.load_panel(refresh=False)["frames"]["IWM"].index)
    streams = {
        "broad 8-K proxy": {
            "returns": frame["gross_5"], "dates": frame["signal_date"],
            "exact_live_rule": False,
            "scope": "broad hypothesis-generation proxy",
        },
        "reaction-confirmed 8-K proxy": {
            "returns": proxy["gross_5"], "dates": proxy["signal_date"],
            "exact_live_rule": False,
            "scope": "closer historical proxy; still missing several v3 gates",
        },
        "prospective exact v3 signals": {
            "returns": live_returns, "dates": live_dates,
            "exact_live_rule": True,
            "scope": "current engine's recorded net five-session outcomes",
        },
    }
    results = compare(streams, calendar=calendar, block=10, n_boot=3000)
    print("CAN THIS RULE BE EVALUATED WITHIN FIVE YEARS?")
    print("Target: resolve a 2% signal-day effect with 95% confidence / 80% power.\n")
    for name, result in results.items():
        print(" ", name)
        print("    scope:", result["scope"])
        if result.get("applicable"):
            print(
                "    %s events / %s signal days over %.1f years (%.1f days/yr)"
                % (
                    format(result["events"], ","),
                    format(result["independent_signal_days"], ","),
                    result["history_years"],
                    result["signal_days_per_year"],
                )
            )
            print(
                "    block-adjusted MDE %.2f%%; estimated additional history %.1f years"
                % (
                    result["min_detectable_effect_pct"],
                    result["additional_years_required"],
                )
            )
        print("    ->", verdict_line(result), "\n")
    print("Feasibility is power planning, not evidence of a positive return.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
