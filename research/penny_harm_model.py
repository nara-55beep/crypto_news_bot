"""A predictor of harm, because that is the only thing this data supports predicting.

Every attempt to forecast *gains* in this universe has failed, and not narrowly: the
live composite's price/volume core measured a gross expectancy of exactly zero over
14,958 trades, the catalyst gate died out of sample, and item codes carry no standalone
direction (family-wise p=0.467). Published work agrees the well has run dry - Martineau
(2022) finds post-earnings drift gone even for microcaps, with prices absorbing surprises
on the announcement date.

One asymmetry survived all of it. Nothing forecasts gains, but several observable
features reliably precede *losses*: recent dilution filings, distress item codes,
lottery-like run-ups, sub-$1 prices. That is not a consolation prize. It matches the
academic record on floating-price "toxic" convertibles, which produce significant
negative abnormal returns after issuance in exactly this market-cap band, and it is the
concern that actually binds a small account - capital preservation, not stock picking.

Statistically it is also the easier problem. The harm effects run -1% to -2% per event
against positive effects that never cleared noise, so a modest sample can resolve them.

Method follows the corrected conventions rather than my earlier flawed ones: the live
price/liquidity floor is re-applied at *event* time instead of trusting panel
membership, rows arrive already deduped to one symbol/reaction-session, intervals come
from a market-calendar block bootstrap, and the feature search carries a family-wise
correction. The post-2024 window has been examined by earlier research, so it is labelled
reused, never held out.

This module cannot authorize a trade. It can only tell the desk what to refuse.
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

import pennystock_bot as live                       # noqa: E402
from research import edgar_catalysts as edgar       # noqa: E402
from research import penny_edge_research as base    # noqa: E402
from research import penny_event_drift as event_drift  # noqa: E402
from research import penny_stats as stats           # noqa: E402

REPORT_PATH = ROOT / "data" / "pennystock_harm_model.json"
METHOD_VERSION = "harm-v1-2026-08-08"
ADVERSE_ITEMS = frozenset(edgar.NEGATIVE_8K_ITEMS)

#: Pre-registered flags. Each is observable at the signal close and each has a stated
#: reason to expect harm; none was chosen by looking at its own outcome first.
FLAGS = {
    "recent_dilution_filing": (
        "an offering was filed within 90 days - supply is arriving",
        lambda f: f["dilution_age_days"] <= 90),
    "adverse_8k_item": (
        "the filing itself is distress: bankruptcy, default, delisting, "
        "impairment, restatement or unregistered equity sales",
        lambda f: f["item_set"].map(lambda s: bool(s & ADVERSE_ITEMS))),
    "lottery_runup": (
        "a >=35% single day in the last month - lottery-like names underperform",
        lambda f: f["max_ret20"] >= 0.35),
    "sub_dollar": (
        "below $1, where delisting risk and spread both jump",
        lambda f: f["raw_close"] < 1.00),
    "unstable_range": (
        "ATR above 22% of price - position sizing cannot control the outcome",
        lambda f: f["atr_pct"] > 0.22),
}


def _json_dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)


def build(refresh: bool = False) -> pd.DataFrame:
    """Eligible events with each harm flag and a 0-5 additive score."""
    frame = event_drift.build(refresh=refresh).copy()
    if frame.empty:
        return frame
    frame["item_set"] = frame["items"].map(
        lambda v: frozenset(
            str(x).strip() for x in str(v or "").replace(",", "|").split("|")
            if str(x).strip()))

    # Re-apply the LIVE universe floor at event time. Panel membership is today's
    # membership; a name now under $5 may have been a $30 stock when it filed.
    adv_proxy = frame["dollar_volume20"].div(
        frame["raw_close"].where(frame["raw_close"] > 0))
    frame = frame[
        frame["raw_close"].gt(live.MIN_PRICE)
        & frame["raw_close"].lt(live.MAX_PRICE)
        & adv_proxy.ge(live.MIN_AVG_VOLUME)
        & frame["gross_5"].notna()
    ].copy()

    for name, (_, fn) in FLAGS.items():
        frame[name] = fn(frame).astype(bool)
    frame["harm_score"] = frame[list(FLAGS)].sum(axis=1).astype(int)
    frame["gross_return"] = pd.to_numeric(frame["gross_5"], errors="coerce")
    frame["excess_return"] = pd.to_numeric(frame["excess_5"], errors="coerce")
    return frame.reset_index(drop=True)


def _cell(frame: pd.DataFrame, column: str, calendar) -> dict:
    if frame.empty:
        return {"applicable": False, "events": 0}
    trades = frame[["signal_date", "ticker", column]].rename(
        columns={column: "net_return"})
    out = stats.power(trades, label=column, calendar=calendar, n_boot=3000, block=10)
    if out.get("applicable"):
        out["events"] = int(len(frame))
        out["symbols"] = int(frame["ticker"].nunique())
    return out


def analyse(frame: pd.DataFrame, calendar, column: str = "excess_return") -> dict:
    """Per-flag effect, the score's monotonicity, and what refusing buys you."""
    dev = frame[frame["signal_date"] <= base.VALIDATION_END]
    out = {"column": column, "development_events": int(len(dev))}

    per_flag, book = {}, {}
    for name, (why, _) in FLAGS.items():
        hit = dev[dev[name]]
        miss = dev[~dev[name]]
        if len(hit) < 40:
            continue
        h, m = _cell(hit, column, calendar), _cell(miss, column, calendar)
        per_flag[name] = {
            "reason": why,
            "flagged": h, "unflagged": m,
            "difference_pct": (round(h["mean_net_pct"] - m["mean_net_pct"], 4)
                               if h.get("applicable") and m.get("applicable") else None),
        }
        # For the family-wise check the statistic must point the same way as the claim,
        # so a harm flag is scored by the NEGATIVE of what it selects.
        book[name] = hit[["signal_date", "ticker", column]].rename(
            columns={column: "net_return"}).assign(net_return=lambda d: -d["net_return"])
    out["per_flag"] = per_flag
    if len(book) >= 2:
        out["family_wise_harm"] = stats.reality_check(
            book, end=base.VALIDATION_END, calendar=calendar, n_boot=5000, block=10)

    out["by_score"] = {
        str(score): _cell(dev[dev["harm_score"] == score], column, calendar)
        for score in sorted(dev["harm_score"].unique())
    }
    clean, dirty = dev[dev["harm_score"] == 0], dev[dev["harm_score"] >= 2]
    out["refusal_test"] = {
        "clean (score 0)": _cell(clean, column, calendar),
        "refused (score >= 2)": _cell(dirty, column, calendar),
        "everything": _cell(dev, column, calendar),
    }
    return out


def run(refresh: bool = False) -> dict:
    frame = build(refresh=refresh)
    calendar = pd.DatetimeIndex(base.load_panel(refresh=False)["frames"]["IWM"].index)
    dev = analyse(frame, calendar, "excess_return")
    reused = frame[frame["signal_date"] > base.VALIDATION_END]

    reused_block = {
        "clean (score 0)": _cell(reused[reused["harm_score"] == 0],
                                 "excess_return", calendar),
        "refused (score >= 2)": _cell(reused[reused["harm_score"] >= 2],
                                      "excess_return", calendar),
    }
    ref = dev["refusal_test"]
    gap = None
    if ref["clean (score 0)"].get("applicable") and ref["refused (score >= 2)"].get("applicable"):
        gap = round(ref["clean (score 0)"]["mean_net_pct"]
                    - ref["refused (score >= 2)"]["mean_net_pct"], 4)
    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "method_version": METHOD_VERSION,
        "estimand": "5-session excess return vs IWM, per deduped event",
        "purpose": ("identify what to refuse; this can never authorize a trade because "
                    "avoiding losses is not the same as having an edge"),
        "development": dev,
        "post_2024_reused": reused_block,
        "clean_minus_refused_pct": gap,
        "eligible_events": int(len(frame)),
    }
    _json_dump(REPORT_PATH, report)
    return report


def main() -> int:
    rep = run()
    print("eligible events:", format(rep["eligible_events"], ","),
          "| development:", format(rep["development"]["development_events"], ","))
    print("\nPER-FLAG (5-session excess vs IWM, development window)")
    print("  %-24s %-9s %-11s %-11s %s" % ("flag", "n", "flagged", "unflagged", "difference"))
    for name, blk in rep["development"]["per_flag"].items():
        h, m = blk["flagged"], blk["unflagged"]
        if not h.get("applicable"):
            continue
        print("  %-24s %-9s %+10.3f%% %+10.3f%%   %+.3f%%" % (
            name, format(h["events"], ","), h["mean_net_pct"], m["mean_net_pct"],
            blk["difference_pct"] or 0.0))
    fw = rep["development"].get("family_wise_harm") or {}
    if fw.get("applicable"):
        print("\n  family-wise (is the WORST flag worse than chance?): p=%.3f -> %s" % (
            fw["p_value_selection_aware"],
            "SIGNIFICANT" if fw["significant_at_5pct"] else "not significant"))
    print("\nBY HARM SCORE")
    for score, cell in rep["development"]["by_score"].items():
        if cell.get("applicable"):
            print("  score %-3s n=%-8s excess %+7.3f%%  CI [%+.3f, %+.3f]" % (
                score, format(cell["events"], ","), cell["mean_net_pct"],
                *cell["bootstrap_95_pct"]))
    print("\nWHAT REFUSING BUYS YOU (development)")
    for k, cell in rep["development"]["refusal_test"].items():
        if cell.get("applicable"):
            print("  %-22s n=%-8s excess %+7.3f%%  CI [%+.3f, %+.3f]" % (
                k, format(cell["events"], ","), cell["mean_net_pct"],
                *cell["bootstrap_95_pct"]))
    print("  clean minus refused: %s%%" % rep["clean_minus_refused_pct"])
    print("\nPOST-2024 (reused data, not a held-out test)")
    for k, cell in rep["post_2024_reused"].items():
        if cell.get("applicable"):
            print("  %-22s n=%-8s excess %+7.3f%%  CI [%+.3f, %+.3f]" % (
                k, format(cell["events"], ","), cell["mean_net_pct"],
                *cell["bootstrap_95_pct"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
