"""Baseline, stress, sensitivity, and improvement studies."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from .config import LadderConfig
from .engine import LadderBacktester, LadderResult
from .signals import BollingerRsiSmaSignal


STRESS_PERIODS = {
    "March 2020 crash": ("2020-03-01", "2020-04-01"),
    "May 2021 crash": ("2021-05-01", "2021-06-01"),
    "LUNA and 3AC deleveraging": ("2022-05-01", "2022-08-01"),
    "FTX": ("2022-11-01", "2022-12-01"),
    "August 2024 carry unwind": ("2024-08-01", "2024-09-01"),
}


def summary(result: LadderResult) -> dict[str, Any]:
    if not result.ok:
        return {"ok": False, "error": result.error}
    metrics = result.metrics
    worst = metrics.get("worst_cycle") or {}
    return {
        "ok": True,
        "total_return_pct": metrics.get("total_return_pct"),
        "cagr_pct": metrics.get("cagr_pct"),
        "profit_factor": metrics.get("profit_factor"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "sharpe": metrics.get("sharpe"),
        "max_equity_drawdown_pct": metrics.get("max_equity_drawdown_pct"),
        "max_equity_drawdown_usd": metrics.get("max_equity_drawdown_usd"),
        "entered_cycles": metrics.get("entered_cycles"),
        "liquidations": metrics.get("liquidations"),
        "deepest_floating_loss_usd": worst.get("deepest_floating_loss_usd"),
        "worst_duration_hours": worst.get("duration_hours"),
        "worst_recovered": worst.get("recovered"),
        "final_balance": metrics.get("final_balance"),
    }


def _period_run(frame: pd.DataFrame, signals: pd.Series, config: LadderConfig,
                start: str, end: str) -> LadderResult:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    warmup = start_ts - pd.Timedelta(days=7)
    window = frame[(frame.index >= warmup) & (frame.index < end_ts)]
    if len(window) < 200:
        return LadderResult(False, config.to_dict(), error="stress-period data unavailable")
    return LadderBacktester(config).run(
        window, signal_override=signals.reindex(window.index), start_trading_at=start_ts,
    )


def detect_large_moves(frame: pd.DataFrame, *, threshold: float = 0.20,
                       window_days: int = 30, limit: int = 5) -> list[dict[str, Any]]:
    daily = frame["close"].resample("1D").last().dropna()
    candidates: list[dict[str, Any]] = []
    for offset in range(window_days, len(daily)):
        first = float(daily.iloc[offset - window_days])
        last = float(daily.iloc[offset])
        change = last / first - 1.0 if first else 0.0
        if abs(change) >= threshold:
            candidates.append({
                "start": daily.index[offset - window_days],
                "end": daily.index[offset] + pd.Timedelta(days=1),
                "move_pct": round(change * 100.0, 2),
            })
    selected: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda row: abs(row["move_pct"]), reverse=True):
        overlaps = any(item["start"] < old["end"] and item["end"] > old["start"] for old in selected)
        if not overlaps:
            selected.append(item)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda row: row["start"])


def run_research(frame: pd.DataFrame, config: LadderConfig | None = None,
                 *, full: bool = True) -> dict[str, Any]:
    base = (config or LadderConfig()).validate()
    base_signals = BollingerRsiSmaSignal().generate(frame, base)
    baseline_result = LadderBacktester(base).run(frame, signal_override=base_signals)
    report: dict[str, Any] = {
        "baseline": baseline_result.to_dict(),
        "baseline_summary": summary(baseline_result),
        "stress_tests": [],
        "trigger_sensitivity": [],
        "improvements": [],
        "capacity": [],
        "walk_forward": {},
        "heatmap": [],
    }
    if not baseline_result.ok or not full:
        return report

    for distance in (500.0, 750.0, 800.0, 1000.0, 1250.0, 1500.0):
        result = baseline_result if distance == base.trigger_distance else LadderBacktester(
            replace(base, trigger_distance=distance),
        ).run(frame, signal_override=base_signals)
        report["trigger_sensitivity"].append({"trigger_distance": distance, **summary(result)})

    periods: list[tuple[str, str, str, float | None]] = [
        (name, start, end, None) for name, (start, end) in STRESS_PERIODS.items()
    ]
    for item in detect_large_moves(frame):
        periods.append((
            f"Detected 30-day move {item['move_pct']:+.2f}% ending {item['end'].date()}",
            item["start"].date().isoformat(), item["end"].date().isoformat(), item["move_pct"],
        ))
    for name, start, end, move in periods:
        result = _period_run(frame, base_signals, base, start, end)
        row = {"period": name, "start": start, "end": end, **summary(result)}
        row["move_pct"] = move
        row["recovered"] = (
            bool(result.ok)
            and int(result.metrics.get("entered_cycles", 0)) > 0
            and not bool(result.metrics.get("liquidations"))
            and all(cycle.get("recovered") for cycle in result.cycles if cycle.get("levels_reached"))
        )
        report["stress_tests"].append(row)

    variants = {
        "Regime filter": replace(base, regime_filter=True),
        "ATR-scaled distances": replace(base, distance_mode="atr"),
        "Flat level sizing": replace(base, level_multipliers=(1.0, 1.0, 1.0, 1.0)),
        "Shrinking level sizing": replace(base, level_multipliers=(1.0, 0.75, 0.5, 0.25)),
    }
    for name, variant in variants.items():
        variant_signals = (
            BollingerRsiSmaSignal().generate(frame, variant)
            if variant.regime_filter else base_signals
        )
        report["improvements"].append({
            "variant": name,
            **summary(LadderBacktester(variant).run(frame, signal_override=variant_signals)),
        })

    for capital in (10_000.0, 50_000.0, 100_000.0, 500_000.0, 1_000_000.0):
        variant = replace(base, starting_capital=capital)
        report["capacity"].append({
            "starting_capital": capital,
            **summary(LadderBacktester(variant).run(frame, signal_override=base_signals)),
        })

    split = int(len(frame) * 0.70)
    split_time = frame.index[split]
    train = frame.iloc[:split]
    test = frame.iloc[max(0, split - 500):]
    candidates = []
    for distance in (500.0, 800.0, 1100.0, 1500.0):
        result = LadderBacktester(replace(base, trigger_distance=distance)).run(
            train, signal_override=base_signals.reindex(train.index),
        )
        candidates.append({"trigger_distance": distance, **summary(result)})
    viable = [row for row in candidates if row.get("ok") and not row.get("liquidations")]
    ranked = viable or [row for row in candidates if row.get("ok")]
    best = max(ranked, key=lambda row: row.get("total_return_pct") or -10**12) if ranked else None
    out_of_sample = None
    if best is not None:
        out_of_sample = LadderBacktester(
            replace(base, trigger_distance=float(best["trigger_distance"])),
        ).run(
            test, signal_override=base_signals.reindex(test.index),
            start_trading_at=split_time,
        )
    report["walk_forward"] = {
        "split_time": split_time.isoformat(), "training_candidates": candidates,
        "selected_trigger_distance": best.get("trigger_distance") if best else None,
        "out_of_sample": summary(out_of_sample) if out_of_sample else None,
    }

    for trigger in (500.0, 800.0, 1100.0, 1500.0):
        for step in (250.0, 500.0, 750.0):
            variant = replace(base, trigger_distance=trigger, ladder_step=step)
            report["heatmap"].append({
                "trigger_distance": trigger, "ladder_step": step,
                **summary(LadderBacktester(variant).run(frame, signal_override=base_signals)),
            })
    return report
