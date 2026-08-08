"""Can a candidate rule ever be validated? Ask before deploying it, not after.

This project keeps repeating one failure. A new strategy version ships, is marked
COLLECTING, and waits for forward evidence that will never arrive - because nobody first
asked how many events the rule actually produces and how long resolving its effect would
take. v1, the catalyst gate, and v3 each went through that loop.

The same gap explains why the audits keep disagreeing. There are two populations, and
neither answers the question on its own:

* a **broad** population, statistically resolvable but not what the desk trades - a
  finding here need not transfer, and one of mine did not: removing the fixed profit
  target measured +1.861% CI [+1.256, +2.512] on broad 8-K events and -1.728% CI
  [-3.754, +0.443] on the desk's own entries;
* the **exact** population, which is what the desk trades but yields 112 events in nine
  and a half years - roughly 11.8 a year, against a 12.6% per-event standard deviation.

That combination resolves nothing below 3.33% per event, so a realistic 1-2% edge would
need 105 or 26 years of collection. The rule is not unproven; it is *unprovable* at its
current selectivity, and no amount of patience changes that.

This module turns that into a gate. Give it an event stream and a target effect and it
reports the minimum detectable effect, the events required, and the years implied - so a
rule that cannot be validated is identified before it is deployed rather than after.

Feasibility is not evidence. Passing here only means a verdict is reachable in principle;
the strategy still has to earn one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import penny_stats as stats   # noqa: E402

#: A rule needing longer than this to reach a verdict is not a research programme.
PATIENCE_YEARS = 5.0


def feasibility(returns: pd.Series, dates: pd.Series,
                target_effect_pct: float = 2.0,
                patience_years: float = PATIENCE_YEARS) -> dict:
    """Minimum detectable effect, and the calendar time a verdict would cost.

    Dispersion is measured per event. Where several events share a session they are not
    independent, so the effective count is the number of distinct signal days - using raw
    event counts would overstate power and is precisely the error that makes a thin
    sample look conclusive.
    """
    r = pd.to_numeric(returns, errors="coerce")
    d = pd.to_datetime(dates, errors="coerce")
    ok = r.notna() & d.notna()
    r, d = r[ok], d[ok]
    if len(r) < 5:
        return {"applicable": False, "reason": f"only {len(r)} usable events"}

    daily = pd.DataFrame({"d": d, "r": r}).groupby("d")["r"].mean()
    n_eff = len(daily)
    sd = float(daily.std(ddof=1))
    span_years = max((d.max() - d.min()).days / 365.25, 1e-9)
    rate = n_eff / span_years
    k = stats.Z_975 + stats.Z_80

    mde = k * sd / np.sqrt(n_eff) if n_eff else float("nan")
    target = target_effect_pct / 100.0
    need = (k * sd / target) ** 2 if target else float("inf")
    years = need / rate if rate else float("inf")

    return {
        "applicable": True,
        "events": int(len(r)),
        "independent_signal_days": int(n_eff),
        "history_years": round(span_years, 2),
        "events_per_year": round(rate, 2),
        "per_event_sd_pct": round(sd * 100, 3),
        "min_detectable_effect_pct": round(mde * 100, 3),
        "target_effect_pct": target_effect_pct,
        "signal_days_needed": int(np.ceil(need)) if np.isfinite(need) else None,
        "years_to_verdict": round(years, 1) if np.isfinite(years) else None,
        "already_resolvable": bool(np.isfinite(mde) and mde <= target),
        "verdict_reachable_within_patience": bool(
            np.isfinite(years) and years <= patience_years),
        "patience_years": patience_years,
    }


def verdict_line(f: dict) -> str:
    """One sentence a human can act on."""
    if not f.get("applicable"):
        return f"not assessable: {f.get('reason')}"
    if f["already_resolvable"]:
        return (f"resolvable now: {f['independent_signal_days']} signal days already "
                f"resolve {f['min_detectable_effect_pct']:.2f}%/event")
    if f["verdict_reachable_within_patience"]:
        return (f"reachable: about {f['years_to_verdict']:.1f} more years at "
                f"{f['events_per_year']:.1f} events/yr to resolve "
                f"{f['target_effect_pct']:.0f}%/event")
    return (f"UNPROVABLE at this selectivity: {f['events_per_year']:.1f} events/yr would "
            f"need ~{f['years_to_verdict']:.0f} years to resolve "
            f"{f['target_effect_pct']:.0f}%/event; the history resolves nothing below "
            f"{f['min_detectable_effect_pct']:.2f}%")


def compare(streams: dict[str, tuple[pd.Series, pd.Series]],
            target_effect_pct: float = 2.0) -> dict:
    """Feasibility for several candidate filters, so selectivity can be traded off."""
    return {name: feasibility(r, d, target_effect_pct)
            for name, (r, d) in streams.items()}


def main() -> int:
    from research import penny_exit_structure as exits
    from research import penny_harm_model as harm

    frame = harm.build()
    strict = exits._reaction_confirmed_proxy(frame)
    streams = {
        "broad 8-K eligible": (frame["gross_5"], frame["signal_date"]),
        "reaction-confirmed (the desk's own entries)": (strict["gross_5"],
                                                        strict["signal_date"]),
    }
    print("CAN THIS RULE EVER BE VALIDATED?  (target: a 2%/event edge, 80% power)\n")
    for name, f in compare(streams).items():
        print(f"  {name}")
        if not f.get("applicable"):
            print(f"    {verdict_line(f)}\n")
            continue
        print("    %s events over %.1f yrs (%.1f/yr) | per-event SD %.2f%%"
              % (format(f["events"], ","), f["history_years"],
                 f["events_per_year"], f["per_event_sd_pct"]))
        print("    resolves nothing below %.2f%%/event" % f["min_detectable_effect_pct"])
        print("    -> %s\n" % verdict_line(f))
    print("Feasibility is not evidence: it only says whether a verdict is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
