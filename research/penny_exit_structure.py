"""Is the exit logic, not the entry, what breaks this strategy?

The event distribution is violently right-skewed: over the development window the median
8-K event returns -0.55% across five sessions, only 46.7% are positive, and the top 1% of
events supply 68% of the total summed return.

A stop-and-target system is close to the worst possible harness for that shape. The live
desk exits at 2.5R, so the rare +60% event is truncated to roughly +25% while every
ordinary loser is kept in full. Cutting the tail that carries the distribution and
retaining the body is enough to turn a positive-mean process negative - which is exactly
the sign flip observed between the raw event returns and the simulated trades.

This module holds the entries fixed and varies only the exit, so any difference is
attributable to the harness rather than to signal quality.

The comparison is not a strategy proposal. Concentration is the reason: capturing a mean
that lives in 1% of events requires enduring the other 99%, and a survivor-only panel
inflates long-horizon buy-and-hold precisely where the tail lives. Both caveats are
quantified below rather than left as prose.
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
METHOD_VERSION = "exit-structure-v1-2026-08-08"
HOLD = desk.MAX_HOLD_DAYS


def _simulate(frame: pd.DataFrame, panel: dict, mode: str) -> pd.Series:
    """Replay each event's forward window under one exit rule. Stop-first throughout."""
    out = []
    idx_cache = {s: f.index for s, f in panel.items()}
    for _, e in frame.iterrows():
        symbol = e["ticker"]
        bars = panel.get(symbol)
        if bars is None or bars.empty:
            out.append(np.nan)
            continue
        entry_date = pd.Timestamp(e["entry_date"])
        loc = idx_cache[symbol].get_indexer([entry_date])[0]
        if loc < 0 or loc >= len(bars):
            out.append(np.nan)
            continue
        entry = float(bars["open"].iloc[loc])
        if not np.isfinite(entry) or entry <= 0:
            out.append(np.nan)
            continue
        atr = float(e["atr_pct"]) * 100
        risk = max(7.0, min(15.0, atr * 1.15 if atr > 0 else 10.0))
        stop = entry * (1 - risk / 100.0)
        target = entry * (1 + risk * 2.5 / 100.0)
        last = min(loc + HOLD, len(bars) - 1)

        if mode == "hold_only":
            out.append(float(bars["close"].iloc[last]) / entry - 1.0)
            continue

        high_water, trailing, exited = entry, False, None
        for i in range(loc, last + 1):
            o = float(bars["open"].iloc[i])
            hi = float(bars["high"].iloc[i])
            lo = float(bars["low"].iloc[i])
            if o <= stop:
                exited = o / entry - 1.0
                break
            if mode in ("stop_and_target", "live_current") and o >= target:
                exited = o / entry - 1.0
                break
            if lo <= stop:
                exited = stop / entry - 1.0
                break
            if mode in ("stop_and_target", "live_current") and hi >= target:
                exited = target / entry - 1.0
                break
            high_water = max(high_water, hi)
            if mode in ("live_current", "trail_no_target"):
                if not trailing and hi >= entry * (1 + desk.TRAIL_ARM_PCT / 100.0):
                    trailing = True
                    stop = max(stop, entry)
                if trailing:
                    stop = max(stop, high_water * (1 - desk.TRAIL_PCT / 100.0))
        out.append(exited if exited is not None
                   else float(bars["close"].iloc[last]) / entry - 1.0)
    return pd.Series(out, index=frame.index, dtype=float)


def _describe(returns: pd.Series, cost: pd.Series) -> dict:
    net = (returns - cost).dropna()
    if net.empty:
        return {"applicable": False}
    top1 = net.nlargest(max(1, len(net) // 100))
    total = float(net.sum())
    return {
        "applicable": True,
        "events": int(len(net)),
        "mean_net_pct": round(float(net.mean()) * 100, 4),
        "median_net_pct": round(float(net.median()) * 100, 4),
        "win_rate_pct": round(float((net > 0).mean()) * 100, 2),
        "skew": round(float(net.skew()), 2),
        "best_pct": round(float(net.max()) * 100, 2),
        "top_1pct_share_of_total": (round(100 * float(top1.sum()) / total, 1)
                                    if total > 0 else None),
    }


def run(refresh: bool = False) -> dict:
    frame = harm.build(refresh=refresh)
    payload = base.load_panel(refresh=False)
    panel = base.build_feature_panel(payload)   # benchmark is excluded from this panel
    dev = frame[frame["signal_date"] <= base.VALIDATION_END].copy()
    cost = pd.Series(0.005, index=dev.index)   # 0.50%, near measured live spreads

    modes = {
        "live_current (stop + 2.5R target + trail)": "live_current",
        "stop_and_target (no trail)": "stop_and_target",
        "stop_only (let winners run)": "stop_only",
        # the natural best-of-both: keep downside protection, remove the upside cap
        "trail_no_target (stop + trail, no cap)": "trail_no_target",
        "hold_only (no stop, no target)": "hold_only",
    }
    results, book = {}, {}
    for label, mode in modes.items():
        r = _simulate(dev, panel, mode)
        results[label] = _describe(r, cost)
        book[label] = pd.DataFrame({
            "signal_date": dev["signal_date"], "ticker": dev["ticker"],
            "net_return": (r - cost),
        }).dropna()

    calendar = pd.DatetimeIndex(payload["frames"]["IWM"].index)
    for label in results:
        if results[label].get("applicable"):
            ci = stats.power(book[label], label=label, calendar=calendar,
                             n_boot=3000, block=10)
            results[label]["bootstrap_95_pct"] = ci.get("bootstrap_95_pct")
            results[label]["mean_ci_excludes_zero"] = bool(
                ci.get("bootstrap_95_pct") and ci["bootstrap_95_pct"][0] > 0)

    family = stats.reality_check(book, end=base.VALIDATION_END, calendar=calendar,
                                 n_boot=5000, block=10) if len(book) >= 2 else {}
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "assumed_cost_pct": 0.5,
        "development_events": int(len(dev)),
        "by_exit_rule": results,
        "family_wise": family,
        "caveats": [
            "entries are held fixed; only the exit varies, so differences are the harness",
            "a survivor-only panel inflates long-horizon buy-and-hold, which is exactly "
            "where the right tail lives, so hold_only is the most biased row here",
            "concentration is the binding practical problem: a mean that lives in 1% of "
            "events requires surviving the other 99% to collect it",
        ],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(REPORT_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, allow_nan=False)
    os.replace(tmp, REPORT_PATH)
    return report


def main() -> int:
    rep = run()
    print("development events: %s   assumed cost %.2f%%" % (
        format(rep["development_events"], ","), rep["assumed_cost_pct"]))
    print("\n%-42s %-9s %-10s %-10s %-8s %-7s %s" % (
        "exit rule", "n", "mean", "median", "win%", "skew", "95% CI"))
    for label, r in rep["by_exit_rule"].items():
        if not r.get("applicable"):
            continue
        ci = r.get("bootstrap_95_pct") or [float("nan")] * 2
        print("%-42s %-9s %+9.3f%% %+9.3f%% %7.1f%% %6.2f  [%+.2f, %+.2f]%s" % (
            label, format(r["events"], ","), r["mean_net_pct"], r["median_net_pct"],
            r["win_rate_pct"], r["skew"], ci[0], ci[1],
            "  *" if r.get("mean_ci_excludes_zero") else ""))
    print("\n  * = mean interval excludes zero")
    for label, r in rep["by_exit_rule"].items():
        if r.get("applicable") and r.get("top_1pct_share_of_total") is not None:
            print("    %-40s best %+8.1f%%  top 1%% supply %5.1f%% of total" % (
                label, r["best_pct"], r["top_1pct_share_of_total"]))
    fw = rep.get("family_wise") or {}
    if fw.get("applicable"):
        print("\n  family-wise over %d exit rules: best=%s p=%.3f -> %s" % (
            fw["strategies_searched"], fw["best_strategy"],
            fw["p_value_selection_aware"],
            "SIGNIFICANT" if fw["significant_at_5pct"] else "not significant"))
    print("\ncaveats:")
    for c in rep["caveats"]:
        print("  -", c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
