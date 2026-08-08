"""Do 8-K item codes discriminate, or is v3 sorting noise into named buckets?

`live_sec_item_confirm_v3` treats some item codes as material (2.02 results, 1.01
agreements, 7.01 Reg FD, 8.01 other, 1.05 cyber) and hard-rejects others as adverse
(bankruptcy, delisting, default, impairment, restatement). Both halves are currently
assumptions: the rule shipped as COLLECTING, exactly as v1 did before it was measured
and found to have zero gross edge.

Item codes are, however, genuinely new information. The earlier catalyst test lumped
every 8-K together and found nothing; a 2.02 earnings release and a 5.07 shareholder-vote
result are not the same event, and averaging them is a good way to hide a real effect.
This module tests each code separately.

Two things make that dangerous, and both are handled here:

* **Searching.** Roughly twenty codes have enough events to test. Testing twenty and
  keeping the best is how noise gets promoted, so the family-wise reality check from
  ``penny_stats`` runs over the whole set rather than a per-code t-test.
* **Timestamp ambiguity.** EDGAR stamps ``acceptanceDateTime`` in Eastern time but
  suffixes it "Z". Reading it as UTC would shift events across the session boundary and
  could manufacture look-ahead. Rather than pick one reading, every result is computed
  under both an ET interpretation (pre-09:30 filings may trade that session's open) and a
  strictly conservative one (always the next session). If a finding survives only under
  the permissive convention, it is a timestamp artifact, not an edge.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import penny_edge_research as R      # noqa: E402
from research import penny_live_audit as A         # noqa: E402
from research import penny_stats as stats          # noqa: E402

EDGAR_CACHE = ROOT / "research" / "cache" / "edgar_filings.pkl"
CACHE = ROOT / "research" / "cache" / "penny_item_events.pkl"

#: v3's own classification, restated here so the test targets the deployed rule.
V3_MATERIAL = {"2.02", "1.01", "7.01", "8.01", "1.05"}
V3_ADVERSE = {"1.03", "3.01", "2.04", "2.06", "4.02"}
MIN_EVENTS = 150          # below this a per-code estimate is not worth reporting


def _events() -> pd.DataFrame:
    """One row per (symbol, 8-K), carrying its item codes and acceptance stamp."""
    with EDGAR_CACHE.open("rb") as f:
        payload = pickle.load(f)
    rows = []
    for symbol, rec in (payload.get("filings") or {}).items():
        for form, items, acc, fdate in zip(
            rec.get("form") or [], rec.get("items") or [],
            rec.get("acceptanceDateTime") or [], rec.get("filingDate") or [],
        ):
            if not str(form).upper().startswith("8-K") or not items:
                continue
            codes = tuple(c for c in str(items).replace(" ", "").split(",") if c)
            if not codes:
                continue
            stamp = pd.to_datetime(acc, errors="coerce", utc=True)
            rows.append({
                "ticker": symbol,
                "filing_date": pd.to_datetime(fdate, errors="coerce"),
                "accept_hour": (stamp.hour + stamp.minute / 60.0)
                               if pd.notna(stamp) else 12.0,
                "codes": codes,
            })
    return pd.DataFrame(rows).dropna(subset=["filing_date"])


def build(refresh: bool = False) -> pd.DataFrame:
    """Attach the forward outcome of trading each 8-K under both entry conventions."""
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)

    panel = R.build_feature_panel(R.load_panel(refresh=False))
    ev = _events()
    out = []
    for symbol, grp in ev.groupby("ticker"):
        frame = panel.get(symbol)
        if frame is None or frame.empty:
            continue
        idx = frame.index
        for _, e in grp.iterrows():
            pos = idx.searchsorted(e["filing_date"], side="left")
            if pos >= len(idx):
                continue
            same_session = idx[pos] == e["filing_date"]
            # ET reading: a filing accepted before the 09:30 open can take that open.
            et_entry = pos if (same_session and e["accept_hour"] < 9.5) else pos + 1
            safe_entry = pos + 1 if same_session else pos
            row = {"ticker": symbol, "signal_date": e["filing_date"],
                   "codes": e["codes"]}
            ok = False
            for label, loc in (("et", et_entry), ("safe", safe_entry)):
                if loc <= 0 or loc >= len(frame):
                    row[f"gross_{label}"] = np.nan
                    continue
                entry = float(frame["open"].iloc[loc])
                atr = float(frame["atr_pct"].iloc[loc - 1]) * 100
                if not np.isfinite(entry) or entry <= 0:
                    row[f"gross_{label}"] = np.nan
                    continue
                risk = max(7.0, min(15.0, atr * 1.15 if atr > 0 else 10.0))
                row[f"gross_{label}"] = A._exit_return(
                    frame, loc, entry,
                    entry * (1 - risk / 100.0), entry * (1 + risk * 2.5 / 100.0))
                row["cost"] = R.estimated_round_trip_cost(frame.iloc[loc - 1])
                ok = True
            if ok:
                out.append(row)
    df = pd.DataFrame(out)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(CACHE)
    return df


def _cell(sub: pd.DataFrame, col: str) -> dict:
    s = sub.dropna(subset=[col])
    if s.empty:
        return {"n": 0}
    daily = s.groupby("signal_date")[col].mean()
    n = len(daily)
    se = float(daily.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    mean = float(s[col].mean())
    lo = mean - stats.Z_975 * se if np.isfinite(se) else float("nan")
    hi = mean + stats.Z_975 * se if np.isfinite(se) else float("nan")
    return {"n": int(len(s)), "signal_days": int(n),
            "gross_pct": round(mean * 100, 4),
            "ci_pct": [round(lo * 100, 4), round(hi * 100, 4)],
            "mean_cost_pct": round(float(s["cost"].mean()) * 100, 4),
            "beats_cost": bool(np.isfinite(lo) and lo > float(s["cost"].mean()))}


def analyse(df: pd.DataFrame, col: str = "gross_et",
            end: pd.Timestamp | None = None) -> dict:
    end = R.VALIDATION_END if end is None else end
    d = df[df["signal_date"] <= end].copy()
    codes = sorted({c for tup in d["codes"] for c in tup})

    per_code, book = {}, {}
    for code in codes:
        mask = d["codes"].apply(lambda t, c=code: c in t)
        sub = d[mask]
        if len(sub.dropna(subset=[col])) < MIN_EVENTS:
            continue
        per_code[code] = _cell(sub, col)
        book[code] = sub.dropna(subset=[col]).rename(columns={col: "net_return"})[
            ["signal_date", "net_return"]].assign(ticker="x")

    material = d[d["codes"].apply(lambda t: bool(set(t) & V3_MATERIAL))]
    adverse = d[d["codes"].apply(lambda t: bool(set(t) & V3_ADVERSE))]
    neither = d[d["codes"].apply(
        lambda t: not (set(t) & V3_MATERIAL) and not (set(t) & V3_ADVERSE))]

    out = {
        "convention": col,
        "events": int(len(d)),
        "per_code": per_code,
        "v3_split": {
            "material (v3 trades these)": _cell(material, col),
            "adverse (v3 hard-rejects)": _cell(adverse, col),
            "neither": _cell(neither, col),
        },
    }
    if len(book) >= 2:
        out["family_wise"] = stats.reality_check(book, end=end, n_boot=2000)
    return out


def run(refresh: bool = False) -> dict:
    """Full audit plus one held-out check of v3's own pre-specified split.

    The family-wise correction covers *my* search for the best code. It does not apply
    to v3's material/adverse sets: the deployed rule declared those before any of this
    was measured, so testing them is a single pre-registered hypothesis, not a search.
    """
    df = build(refresh=refresh)
    test = df[df["signal_date"] > R.VALIDATION_END]
    held_out = {}
    for col in ("gross_et", "gross_safe"):
        held_out[col] = {
            "material": _cell(
                test[test["codes"].apply(lambda t: bool(set(t) & V3_MATERIAL))], col),
            "adverse": _cell(
                test[test["codes"].apply(lambda t: bool(set(t) & V3_ADVERSE))], col),
        }
    worst = {}
    for code in sorted(V3_MATERIAL):
        cell = _cell(test[test["codes"].apply(lambda t, c=code: c in t)], "gross_et")
        if cell.get("n"):
            worst[code] = cell
    material = held_out["gross_et"]["material"]
    return {
        "strategy_id": "live_sec_item_confirm_v3",
        "development": analyse(df, "gross_et"),
        "development_conservative": analyse(df, "gross_safe"),
        "held_out_v3_split": held_out,
        "held_out_material_codes": worst,
        "verdict": (
            "v3 buys the item codes that underperform: its material set returns "
            f"{material.get('gross_pct')}% gross out-of-sample, CI "
            f"{material.get('ci_pct')}, before any costs"
            if material.get("n") and material.get("ci_pct", [0])[1] < 0
            else "v3's material set is not distinguishable from zero"
        ),
    }


def main() -> int:
    df = build()
    print("8-K events with outcomes:", format(len(df), ","))
    for col, label in (("gross_et", "ET reading (pre-09:30 may take that open)"),
                       ("gross_safe", "conservative (always the next session)")):
        res = analyse(df, col)
        print(f"\n{'='*74}\n{label}\n{'='*74}")
        print("  %-8s %-9s %-10s %-22s %s" % ("item", "n", "gross%", "95% CI", "beats cost?"))
        for code, cell in sorted(res["per_code"].items(),
                                 key=lambda kv: -kv[1]["gross_pct"]):
            tag = " [v3 material]" if code in V3_MATERIAL else (
                  " [v3 adverse]" if code in V3_ADVERSE else "")
            print("  %-8s %-9s %+9.3f%%  [%+7.3f, %+7.3f]   %-4s%s" % (
                code, format(cell["n"], ","), cell["gross_pct"],
                cell["ci_pct"][0], cell["ci_pct"][1],
                "YES" if cell["beats_cost"] else "no", tag))
        print("\n  v3's own split:")
        for k, cell in res["v3_split"].items():
            if cell.get("n"):
                print("    %-28s n=%-8s gross %+7.3f%%  CI [%+.3f, %+.3f]" % (
                    k, format(cell["n"], ","), cell["gross_pct"], *cell["ci_pct"]))
        fw = res.get("family_wise") or {}
        if fw.get("applicable"):
            print("\n  family-wise over %d codes: best=%s  p=%.3f  -> %s" % (
                fw["strategies_searched"], fw["best_strategy"],
                fw["p_value_selection_aware"],
                "SIGNIFICANT" if fw["significant_at_5pct"] else "not significant"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
