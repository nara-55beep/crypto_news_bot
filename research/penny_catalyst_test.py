"""Does requiring a dated catalyst actually add edge?

The live desk now refuses to call anything a setup without a dated catalyst, which is a
sound instinct - the price/volume core on its own measured a gross expectancy of exactly
zero (see PENNY_EDGE.md). But the rule is currently unfalsifiable in practice: it is
marked COLLECTING and would need years of forward signals before anyone could tell.

EDGAR makes it testable today. An 8-K's filing date is contemporaneous and never
restated, so "was there a material event by this close?" can be answered for any past
session without look-ahead.

The comparison is done on **gross** return, before costs. Costs are identical whether or
not a catalyst exists, so gross isolates the only question that matters here: does the
catalyst filter select trades with better raw outcomes? If gross stays at zero, no
catalyst rule built on this data can pay a 1.5-2% round trip, and that is worth knowing
now rather than in three years.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import edgar_catalysts as E          # noqa: E402
from research import penny_edge_research as R      # noqa: E402
from research import penny_live_audit as A         # noqa: E402
from research import penny_stats as stats          # noqa: E402

CACHE = ROOT / "research" / "cache" / "penny_catalyst_crosssection.pkl"


def build(refresh: bool = False) -> pd.DataFrame:
    """One row per (symbol, session): live scores, catalyst ages, forward gross return."""
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)

    payload = R.load_panel(refresh=False)
    panel = R.build_feature_panel(payload)
    scored = A.score_sessions(panel)
    cal = E.calendars(E.load())

    idx_by_symbol = {s: f.index for s, f in panel.items()}
    rows = []
    for symbol, grp in scored.groupby("ticker"):
        frame = panel[symbol]
        locs = idx_by_symbol[symbol].get_indexer(grp["signal_date"])
        cals = cal.get(symbol) or {}
        news = cals.get("news")
        dilution = cals.get("dilution")
        for (_, p), loc in zip(grp.iterrows(), locs):
            if loc < 0 or loc + 1 >= len(frame):
                continue
            entry = float(frame["open"].iloc[loc + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue
            when = pd.Timestamp(p["signal_date"])
            risk = max(7.0, min(15.0, p["atr_pct"] * 1.15 if p["atr_pct"] > 0 else 10.0))
            gross = A._exit_return(frame, loc + 1, entry,
                                   entry * (1 - risk / 100.0),
                                   entry * (1 + risk * 2.5 / 100.0))
            rows.append({
                "signal_date": when, "ticker": symbol,
                "composite": p["composite"], "hype": p["hype"],
                "technical": p["technical"], "gross": gross,
                "cost": R.estimated_round_trip_cost(frame.iloc[loc]),
                "news_age_days": E.days_since(news, when),
                "dilution_age_days": E.days_since(dilution, when),
            })
    df = pd.DataFrame(rows)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(CACHE)
    return df


def _cell(sub: pd.DataFrame) -> dict:
    """Gross mean with a signal-date-clustered interval, so 'zero' is measured."""
    if sub.empty:
        return {"n": 0}
    daily = sub.groupby("signal_date")["gross"].mean()
    n_days = len(daily)
    sd = float(daily.std(ddof=1)) if n_days > 1 else float("nan")
    se = sd / np.sqrt(n_days) if n_days > 1 else float("nan")
    mean = float(sub["gross"].mean())
    lo = mean - stats.Z_975 * se if np.isfinite(se) else float("nan")
    hi = mean + stats.Z_975 * se if np.isfinite(se) else float("nan")
    return {
        "n": int(len(sub)),
        "signal_days": int(n_days),
        "gross_pct": round(mean * 100, 4),
        "ci_pct": [round(lo * 100, 4), round(hi * 100, 4)],
        "beats_cost": bool(np.isfinite(lo) and lo > float(sub["cost"].mean())),
        "mean_cost_pct": round(float(sub["cost"].mean()) * 100, 4),
    }


def analyse(df: pd.DataFrame, end: pd.Timestamp | None = None) -> dict:
    """Explore on train+validation by default; the 2025+ test stays shut."""
    end = R.VALIDATION_END if end is None else end
    d = df[df["signal_date"] <= end]
    out = {"window_end": str(pd.Timestamp(end).date()), "rows": int(len(d))}

    out["baseline"] = _cell(d)
    out["news_age"] = {
        label: _cell(d[mask]) for label, mask in {
            "8-K today or yesterday": d["news_age_days"] <= 1,
            "8-K 2-3 days ago": d["news_age_days"].between(2, 3),
            "8-K 4-7 days ago": d["news_age_days"].between(4, 7),
            "8-K 8-30 days ago": d["news_age_days"].between(8, 30),
            "no 8-K in 30 days": d["news_age_days"] > 30,
        }.items()
    }
    out["dilution"] = {
        label: _cell(d[mask]) for label, mask in {
            "offering filed within 30d": d["dilution_age_days"] <= 30,
            "offering filed 31-90d ago": d["dilution_age_days"].between(31, 90),
            "no offering in 90d": d["dilution_age_days"] > 90,
        }.items()
    }
    # the live rule's actual shape: a hot tape AND a fresh dated catalyst
    hot = d["hype"] >= d["hype"].quantile(0.80)
    out["live_rule_shape"] = {
        "hot tape, no fresh 8-K": _cell(d[hot & (d["news_age_days"] > 3)]),
        "hot tape + 8-K within 3d": _cell(d[hot & (d["news_age_days"] <= 3)]),
        "hot tape + 8-K within 1d": _cell(d[hot & (d["news_age_days"] <= 1)]),
        "hot tape + 8-K 1d + no recent offering": _cell(
            d[hot & (d["news_age_days"] <= 1) & (d["dilution_age_days"] > 90)]),
    }
    return out


def by_liquidity(df: pd.DataFrame, end: pd.Timestamp | None = None) -> dict:
    """The catalyst signal is real but smaller than a 2% round trip. Cost is the one
    input here that is *chosen* rather than discovered: it falls with liquidity, because
    the cost proxy is a step function of dollar volume. So the question stops being "is
    there signal" and becomes "is there a liquidity tier where the signal outruns its own
    cost". This slices the best catalyst cell by cost tier and compares like with like.
    """
    end = R.VALIDATION_END if end is None else end
    d = df[df["signal_date"] <= end]
    hot = d["hype"] >= d["hype"].quantile(0.80)
    best = d[hot & (d["news_age_days"] <= 1) & (d["dilution_age_days"] > 90)]

    tiers = {}
    for label, lo, hi in (("cheapest (<=0.30%)", 0.0, 0.0030),
                          ("cheap (0.30-0.60%)", 0.0030, 0.0060),
                          ("mid (0.60-1.50%)", 0.0060, 0.0150),
                          ("dear (1.50-3.00%)", 0.0150, 0.0300),
                          ("dearest (>3.00%)", 0.0300, 9.0)):
        sub = best[(best["cost"] > lo) & (best["cost"] <= hi)]
        cell = _cell(sub)
        if cell.get("n"):
            cell["net_pct"] = round(cell["gross_pct"] - cell["mean_cost_pct"], 4)
            cell["net_ci_lower_pct"] = round(cell["ci_pct"][0] - cell["mean_cost_pct"], 4)
            cell["profitable_after_cost"] = bool(cell["net_ci_lower_pct"] > 0)
        tiers[label] = cell
    return {"catalyst_cell_rows": int(len(best)), "tiers": tiers}


#: Pre-registered before the 2025+ period was opened. Fixed on train+validation
#: evidence: hot tape (top hype quintile), a dated 8-K filed within one day, and no
#: offering filing in the previous 90 days. No parameter is fitted below this line.
RULE = {
    "hype_quantile": 0.80,
    "max_news_age_days": 1,
    "min_dilution_age_days": 90,
}


def apply_rule(d: pd.DataFrame, hype_cut: float) -> pd.DataFrame:
    return d[(d["hype"] >= hype_cut)
             & (d["news_age_days"] <= RULE["max_news_age_days"])
             & (d["dilution_age_days"] > RULE["min_dilution_age_days"])]


def confirm_on_test(df: pd.DataFrame, costs=(0.0035, 0.005, 0.010, 0.020)) -> dict:
    """One look at the untouched period, at several assumed round-trip costs.

    Cost is varied rather than fixed because the research proxy has a hard 1% floor and
    charges roughly 4.6x the spreads the live scanner actually observes on these names.
    Which cost is right is an execution question, not a backtest question, so the
    honest output is the whole curve plus the breakeven point - not one number chosen
    to make the answer come out well.

    The hype threshold is taken from the *development* window so the test period cannot
    inform its own filter.
    """
    dev = df[df["signal_date"] <= R.VALIDATION_END]
    hype_cut = float(dev["hype"].quantile(RULE["hype_quantile"]))
    test = df[df["signal_date"] > R.VALIDATION_END]
    sel = apply_rule(test, hype_cut)
    cell = _cell(sel)
    if not cell.get("n"):
        return {"applicable": False, "reason": "no qualifying test-period setups"}

    gross = cell["gross_pct"]
    lo = cell["ci_pct"][0]
    out = {
        "applicable": True,
        "rule": dict(RULE, hype_threshold=round(hype_cut, 3)),
        "test_setups": cell["n"],
        "test_signal_days": cell["signal_days"],
        "gross_pct": gross,
        "gross_ci_pct": cell["ci_pct"],
        "modelled_cost_pct": cell["mean_cost_pct"],
        "breakeven_cost_pct": gross,
        "confident_breakeven_cost_pct": lo,
        "net_at_cost": {},
    }
    for c in costs:
        pct = c * 100
        out["net_at_cost"][f"{pct:.2f}%"] = {
            "net_pct": round(gross - pct, 4),
            "net_ci_lower_pct": round(lo - pct, 4),
            "confidently_profitable": bool(lo - pct > 0),
        }
    return out


def _print(block: dict, title: str) -> None:
    print(f"\n{title}")
    print("  %-42s %-9s %-10s %-22s %s" % ("bucket", "n", "gross%", "95% CI", "beats cost?"))
    for label, cell in block.items():
        if not cell.get("n"):
            print("  %-42s (empty)" % label)
            continue
        print("  %-42s %-9s %+9.3f%%  [%+7.3f, %+7.3f]   %s" % (
            label, format(cell["n"], ","), cell["gross_pct"],
            cell["ci_pct"][0], cell["ci_pct"][1],
            "YES" if cell["beats_cost"] else "no"))


def main() -> int:
    df = build()
    print("cross-section rows:", format(len(df), ","),
          "| with an 8-K within 1 day:",
          format(int((df["news_age_days"] <= 1).sum()), ","))
    res = analyse(df)
    base = res["baseline"]
    print(f"\nBASELINE (train+validation, {format(res['rows'], ',')} rows)")
    print("  gross %+.3f%%  CI [%+.3f, %+.3f]  vs mean cost %.3f%%"
          % (base["gross_pct"], base["ci_pct"][0], base["ci_pct"][1], base["mean_cost_pct"]))
    _print(res["news_age"], "BY 8-K RECENCY")
    _print(res["dilution"], "BY OFFERING RECENCY")
    _print(res["live_rule_shape"], "THE LIVE RULE'S SHAPE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
