"""
Strict walk-forward intraday prediction for NQ using contemporaneous ES confirmation.

One sample per session/configuration. Features stop at a fixed clock minute. A model is
trained on the prior three calendar years and frozen for the next year. Its trade enters
the next one-minute open and exits at a fixed horizon (or earlier stop/target).

Development walk-forward years: 2019-2023
Final test years:             2024-2026

The final years are not used for model fitting, threshold selection, or ranking.
"""
from __future__ import annotations

import argparse
import math
import warnings
from dataclasses import dataclass
from datetime import date

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import lucid_causal_rebuild as L
from lucid_predictive_research import _timed_trade

warnings.filterwarnings("ignore", category=ConvergenceWarning)


@dataclass(frozen=True)
class Row:
    day: date
    signal_i: int
    features: np.ndarray
    label: int


@dataclass(frozen=True)
class MLConfig:
    entry_minute: int
    horizon: int
    model: str
    c: float
    confidence: float
    stop_mult: float
    target_rr: float | None

    @property
    def label(self) -> str:
        target = "time" if self.target_rr is None else f"r{self.target_rr:g}"
        return (
            f"nq_ml_e{self.entry_minute}_h{self.horizon}_{self.model}_c{self.c:g}_"
            f"p{self.confidence:g}_s{self.stop_mult:g}_{target}"
        )


def _idx_exact(day: L.Day, minute: int) -> int | None:
    idx = np.flatnonzero(day.minute == minute)
    return int(idx[-1]) if len(idx) else None


def _safe_div(a: float, b: float) -> float:
    return float(a / b) if abs(b) > 1e-12 else 0.0


def _instrument_features(day: L.Day, i: int, prior: list[L.Day]) -> list[float]:
    close = float(day.cl[i])
    atr = max(float(day.atr[i]), L.MARKETS[day.market]["tick"])
    start = float(day.op[0])
    hi = float(np.max(day.hi[:i + 1]))
    lo = float(np.min(day.lo[:i + 1]))
    rng = max(hi - lo, atr)
    changes = np.diff(day.cl[:i + 1])
    efficiency = _safe_div(abs(close - start), float(np.sum(np.abs(changes))))

    def ret(minutes: int) -> float:
        j = max(0, i - minutes)
        return _safe_div(close - float(day.cl[j]), atr * math.sqrt(max(1, i - j)))

    last = prior[-1]
    ph = float(np.max(last.hi))
    pl = float(np.min(last.lo))
    pr = max(ph - pl, atr)
    prior_ret = _safe_div(float(last.cl[-1] - last.op[0]), pr)
    gap = _safe_div(start - float(last.cl[-1]), pr)
    ranges = [float(np.max(x.hi) - np.min(x.lo)) for x in prior[-20:]]
    range_ratio = _safe_div(pr, float(np.median(ranges)))
    pos = _safe_div(close - lo, rng)
    return [
        _safe_div(close - start, atr * math.sqrt(i + 1)),
        ret(5),
        ret(15),
        ret(30),
        ret(60),
        _safe_div(close - float(day.vwap[i]), atr),
        _safe_div(float(day.ema9[i] - day.ema20[i]), atr),
        pos,
        efficiency,
        gap,
        prior_ret,
        range_ratio,
        _safe_div(rng, pr),
    ]


def make_rows(
    nq_days: list[L.Day],
    es_days: list[L.Day],
    entry_minute: int,
    horizon: int,
) -> list[Row]:
    es_by_date = {d.day: d for d in es_days}
    nq_prior: list[L.Day] = []
    es_prior: list[L.Day] = []
    es_history: dict[date, list[L.Day]] = {}
    # Explicit prior lists by date avoid allowing any future session into a feature.
    for day in es_days:
        es_history[day.day] = list(es_prior)
        es_prior.append(day)
    rows = []
    for day in nq_days:
        es = es_by_date.get(day.day)
        prior_es = es_history.get(day.day, [])
        if es is None or len(nq_prior) < 20 or len(prior_es) < 20:
            nq_prior.append(day)
            continue
        signal_i = _idx_exact(day, entry_minute - 1)
        es_i = _idx_exact(es, entry_minute - 1)
        end_i = _idx_exact(day, entry_minute + horizon - 1)
        if signal_i is None or es_i is None or end_i is None or signal_i + 1 >= len(day.cl):
            nq_prior.append(day)
            continue
        x = _instrument_features(day, signal_i, nq_prior)
        x += _instrument_features(es, es_i, prior_es)
        # Cross-market relative momentum, known at the signal close.
        x += [
            x[0] - x[13],
            x[5] - x[18],
            x[6] - x[19],
            x[9] - x[22],
        ]
        entry = float(day.op[signal_i + 1]) + L.MARKETS["nq"]["tick"]
        exit_long = float(day.cl[end_i]) - L.MARKETS["nq"]["tick"]
        label = int(exit_long > entry)
        rows.append(Row(day.day, signal_i, np.asarray(x, dtype=float), label))
        nq_prior.append(day)
    return rows


def walkforward_probabilities(rows: list[Row], c: float, model_name: str = "logistic") -> dict[date, float]:
    out = {}
    years = sorted({row.day.year for row in rows})
    for year in years:
        if year < 2019:
            continue
        train = [r for r in rows if year - 3 <= r.day.year <= year - 1]
        test = [r for r in rows if r.day.year == year]
        if len(train) < 500 or not test:
            continue
        x_train = np.vstack([r.features for r in train])
        y_train = np.array([r.label for r in train])
        x_test = np.vstack([r.features for r in test])
        if model_name == "hgb":
            model = HistGradientBoostingClassifier(
                max_iter=100,
                learning_rate=0.05,
                max_leaf_nodes=7,
                min_samples_leaf=50,
                l2_regularization=10.0,
                random_state=0,
            )
        else:
            model = make_pipeline(
                StandardScaler(),
                LogisticRegression(C=c, max_iter=2000, solver="lbfgs"),
            )
        model.fit(x_train, y_train)
        prob = model.predict_proba(x_test)[:, 1]
        out.update({r.day: float(p) for r, p in zip(test, prob)})
    return out


def trades_from_predictions(
    nq_days: list[L.Day],
    rows: list[Row],
    probabilities: dict[date, float],
    cfg: MLConfig,
) -> list[L.Trade]:
    day_by_date = {d.day: d for d in nq_days}
    out = []
    for row in rows:
        p = probabilities.get(row.day)
        if p is None:
            continue
        if p >= 0.5 + cfg.confidence:
            side = 1
        elif p <= 0.5 - cfg.confidence:
            side = -1
        else:
            continue
        day = day_by_date[row.day]
        remaining = cfg.horizon
        stop_points = cfg.stop_mult * float(day.atr[row.signal_i]) * math.sqrt(remaining)
        trade = _timed_trade(
            day,
            row.signal_i,
            side,
            stop_points,
            cfg.label,
            end_minute=cfg.entry_minute + cfg.horizon - 1,
            target_rr=cfg.target_rr,
        )
        if trade is not None:
            out.append(trade)
    return out


def period_stats(trades: list[L.Trade], risk: float, lo: date, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--entries", nargs="+", type=int, default=[60, 120, 240, 330])
    ap.add_argument("--horizons", nargs="+", type=int, default=[30, 60])
    ap.add_argument("--models", nargs="+", choices=["logistic", "hgb"], default=["logistic"])
    args = ap.parse_args()

    nq = L.load_days("nq")
    es = L.load_days("es")
    row_cache = {}
    prob_cache = {}
    configs = []
    for entry in args.entries:
        for horizon in args.horizons:
            if entry + horizon > 390:
                continue
            rows = make_rows(nq, es, entry, horizon)
            row_cache[(entry, horizon)] = rows
            model_params = [
                (model_name, c)
                for model_name in args.models
                for c in ((0.03, 0.10, 0.30, 1.0) if model_name == "logistic" else (0.0,))
            ]
            for model_name, c in model_params:
                prob_cache[(entry, horizon, model_name, c)] = walkforward_probabilities(
                    rows, c, model_name
                )
                for confidence in (0.0, 0.05, 0.10):
                    for stop_mult in (0.75, 1.25, 2.0):
                        for target in (None, 2.0):
                            configs.append(MLConfig(
                                entry, horizon, model_name, c, confidence, stop_mult, target
                            ))
    print(f"{len(configs)} fixed-time walk-forward configurations", flush=True)

    rows_out = []
    cache = {}
    dev_lo, dev_hi = date(2019, 1, 1), date(2023, 12, 31)
    test_lo = date(2024, 1, 1)
    for cfg in configs:
        rows = row_cache[(cfg.entry_minute, cfg.horizon)]
        probs = prob_cache[(cfg.entry_minute, cfg.horizon, cfg.model, cfg.c)]
        trades = trades_from_predictions(nq, rows, probs, cfg)
        cache[cfg.label] = trades
        dev = period_stats(trades, args.risk, dev_lo, dev_hi)
        yearly = [
            period_stats(trades, args.risk, date(y, 1, 1), date(y, 12, 31))
            for y in range(2019, 2024)
        ]
        min_year_pf = min((s["pf"] for s in yearly if s["n"] >= 20), default=0.0)
        score = min(dev["pf"], min_year_pf) * math.log1p(dev["n"]) + dev["avg"] / 100.0
        rows_out.append({
            "cfg": cfg,
            "dev": dev,
            "yearly": yearly,
            "score": score,
        })
    rows_out.sort(key=lambda r: r["score"], reverse=True)
    finalists = rows_out[: args.top]

    def fmt(s):
        return (
            f"n{s['n']:4} PF{s['pf']:.2f} net{s['net']:+9.0f} "
            f"avg{s['avg']:+6.1f} DD{s['maxdd']:8.0f}"
        )

    print("\nFINALISTS SELECTED ON 2019-2023 WALK-FORWARD ONLY")
    for row in finalists:
        cfg = row["cfg"]
        test = period_stats(cache[cfg.label], args.risk, test_lo, None)
        row["test"] = test
        years = "/".join(f"{s['pf']:.2f}" for s in row["yearly"])
        print(f"{cfg.label:<58} DEV {fmt(row['dev'])} yearlyPF {years}")
        print(f"{'':58} TEST {fmt(test)}")

    eligible = [
        r for r in finalists
        if r["dev"]["pf"] > 1.10
        and r["test"]["pf"] > 1.10
        and min((s["pf"] for s in r["yearly"] if s["n"] >= 20), default=0) > 0.90
    ]
    if not eligible:
        print("\nNO ELIGIBLE ML FINALIST")
        return 0

    # One development-selected model per fixed entry time; no after-the-day selection.
    selected = []
    used_times = set()
    for row in finalists:
        minute = row["cfg"].entry_minute
        if minute in used_times:
            continue
        used_times.add(minute)
        if row in eligible:
            selected.append(row)
            print("SELECT", row["cfg"].label)
    if not selected:
        print("\nNo top model for any fixed time survived the test gate.")
        return 0

    portfolio = L.non_overlapping(
        trade
        for row in selected
        for trade in cache[row["cfg"].label]
    )
    all_days = [d.day for d in nq if d.day >= dev_lo]
    print("\nML PORTFOLIO LUCID WINDOWS")
    for name, lo, hi in (
        ("dev", dev_lo, dev_hi),
        ("test", test_lo, None),
    ):
        ds = [d for d in all_days if d >= lo and (hi is None or d <= hi)]
        raw = L._slice(portfolio, lo, hi)
        print(name)
        for risk in (300.0, 400.0, 500.0, 600.0):
            sized = L.size_trades(raw, risk)
            e12 = L.eval_lucid(sized, ds, 12)
            e30 = L.eval_lucid(sized, ds, 30)
            print(
                f"  ${risk:.0f}: 12d pass {e12['pass_all']*100:5.1f}% "
                f"fail {e12['fails']/e12['starts']*100 if e12['starts'] else 0:5.1f}% | "
                f"30d pass {e30['pass_all']*100:5.1f}% "
                f"fail {e30['fails']/e30['starts']*100 if e30['starts'] else 0:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
