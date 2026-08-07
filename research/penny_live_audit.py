"""Backtest of the rule the live desk actually runs: ``live_composite_v1``.

Every strategy the edge audit has tested so far - the breakout family and the
profitable-EPS-beat event rule - is something the live scanner does not run. The
scanner ranks by its own composite and manages with its own stop/target/trail. The
exact-strategy contract correctly refuses to let an unrelated result authorize it,
which leaves the deployed rule with no evidence in *either* direction. This module
supplies that missing measurement.

Faithfulness, and its limits
----------------------------
Scores come from calling ``pennystock_bot``'s own ``hype_score``,
``technical_score`` and ``tradeability`` on a Dossier rebuilt from historical bars,
and the composite uses the live weights. It is the live code path, not a
reimplementation that can drift.

Three live inputs have no free point-in-time history: fundamentals (``quality``),
news and filings (``catalyst``), and the AI review. They are held at the same
neutral/unknown defaults the live bot already falls back to when Yahoo omits them,
and the two scored components are pinned at ``NEUTRAL_UNAVAILABLE`` so the weights
still sum correctly. What is therefore under test is the **price/volume core**:
0.35 hype + 0.30 technical of the composite weight, scaled by tradeability. If that
core carries no edge, the composite is relying entirely on components this project
cannot validate.

The backtest also applies only the subset of ``hard_risk_reason`` gates that are
computable from bars, so it is if anything *more permissive* than the live desk.

This is one pre-registered configuration, not a search. Hold, stop, target, trail
and capacity are read from the deployed constants rather than chosen here, so there
is no parameter to tune and no selection penalty beyond adding one candidate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pennystock_bot as live                      # noqa: E402
import pennystock_paper as desk                    # noqa: E402
from research import penny_edge_research as R      # noqa: E402
from research import penny_stats as stats          # noqa: E402

NEUTRAL_UNAVAILABLE = 50.0   # quality/catalyst stand-in; neither helps nor hurts a name
TOP_PER_DAY = desk.MAX_OPEN  # live concurrent-position budget
HOLD_DAYS = desk.MAX_HOLD_DAYS
TRAIL_ARM = desk.TRAIL_ARM_PCT
TRAIL_GAP = desk.TRAIL_PCT
STRATEGY_ID = f"{live.LIVE_STRATEGY_ID}_price_core"


def _dossier_at(symbol: str, row: pd.Series, prev_close: float) -> live.Dossier:
    """Rebuild the live Dossier from one historical session.

    Unavailable fields keep their dataclass defaults, which is exactly the state the
    live bot is in when Yahoo returns nothing for them.
    """
    close = float(row["close"])
    open_ = float(row["open"])
    atr = float(row["atr_pct"]) if np.isfinite(row["atr_pct"]) else 0.0
    sma20 = float(row["sma20"]) if np.isfinite(row["sma20"]) else 0.0
    hi20 = float(row["prior20_high"]) if np.isfinite(row["prior20_high"]) else 0.0
    dv = float(row["dollar_volume20"]) if np.isfinite(row["dollar_volume20"]) else 0.0
    vol = float(row["volume"]) if np.isfinite(row["volume"]) else 0.0

    return live.Dossier(
        ticker=symbol,
        price=close,
        change_pct=float(row["ret1"] * 100) if np.isfinite(row["ret1"]) else 0.0,
        volume=vol,
        avg_volume=(dv / close) if close > 0 else 0.0,
        volume_surge=float(row["volume_ratio"]) if np.isfinite(row["volume_ratio"]) else 0.0,
        exchange="NMS",
        quote_type="EQUITY",
        market_state="REGULAR",
        # no historical book: force the ADV cost proxy rather than invent a spread
        spread_reliable=False,
        quote_age_min=-1.0,
        technical_known=True,
        from_open_pct=((close / open_ - 1) * 100) if open_ > 0 else 0.0,
        close_location=float(row["close_location"]) if np.isfinite(row["close_location"]) else 0.5,
        sma20_distance_pct=((close / sma20 - 1) * 100) if sma20 > 0 else 0.0,
        high20_distance_pct=((close / hi20 - 1) * 100) if hi20 > 0 else 0.0,
        return_5d_pct=float(row["ret5"] * 100) if np.isfinite(row["ret5"]) else 0.0,
        gap_pct=((open_ / prev_close - 1) * 100) if prev_close > 0 else 0.0,
        atr_pct=atr * 100,
    )


def _computable_gate(d: live.Dossier) -> str:
    """The subset of the live hard gates that bars alone can decide."""
    if not (live.MIN_PRICE <= d.price <= live.MAX_PRICE):
        return "outside the live price band"
    if d.avg_volume < live.MIN_AVG_VOLUME:
        return "below the live liquidity floor"
    if d.gap_pct >= 20 and d.from_open_pct < 0:
        return "large opening gap is already fading"
    if d.change_pct >= 35 and d.close_location < 0.5:
        return "extreme move is not holding near its high"
    return ""


def score_sessions(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Score every (symbol, session) with the live functions. Vectorised per symbol."""
    rows: list[dict] = []
    for symbol, frame in panel.items():
        if symbol == "IWM" or frame is None or frame.empty:
            continue
        f = frame.dropna(subset=["close", "open", "volume"])
        if len(f) < 60:
            continue
        prev_closes = f["close"].shift(1)
        for ts, row in f.iterrows():
            pc = prev_closes.get(ts, np.nan)
            if not np.isfinite(pc) or pc <= 0:
                continue
            d = _dossier_at(symbol, row, float(pc))
            if _computable_gate(d):
                continue
            h, _ = live.hype_score(d)
            t, _ = live.technical_score(d)
            trade, _ = live.tradeability(d)
            composite = (0.35 * h + 0.30 * t
                         + 0.20 * NEUTRAL_UNAVAILABLE
                         + 0.15 * NEUTRAL_UNAVAILABLE) * (trade / 100.0)
            rows.append({
                "signal_date": ts, "ticker": symbol, "composite": composite,
                "hype": h, "technical": t, "tradeability": trade,
                "close": d.price, "atr_pct": d.atr_pct,
            })
    return pd.DataFrame(rows)


def _exit_return(frame: pd.DataFrame, entry_idx: int, entry: float,
                 stop: float, target: float) -> float:
    """Live management on daily bars: stop, target, 12%-armed 8% trail, 10-day cap.

    Stop is taken before target inside one bar, and a gap through either level fills at
    the open. Both conventions cost the strategy money, which is the right direction for
    a bar-resolution approximation to err in.
    """
    high_water = entry
    trailing = False
    last = min(entry_idx + HOLD_DAYS, len(frame) - 1)
    for i in range(entry_idx, last + 1):
        o = float(frame["open"].iloc[i])
        hi = float(frame["high"].iloc[i])
        lo = float(frame["low"].iloc[i])
        if o <= stop:
            return o / entry - 1.0
        if o >= target:
            return o / entry - 1.0
        if lo <= stop:
            return stop / entry - 1.0
        if hi >= target:
            return target / entry - 1.0
        high_water = max(high_water, hi)
        if not trailing and hi >= entry * (1 + TRAIL_ARM / 100.0):
            trailing = True
            stop = max(stop, entry)                       # live moves to breakeven
        if trailing:
            stop = max(stop, high_water * (1 - TRAIL_GAP / 100.0))
    return float(frame["close"].iloc[last]) / entry - 1.0


def simulate(panel: dict[str, pd.DataFrame], scored: pd.DataFrame) -> pd.DataFrame:
    """Rank each session, take the live capacity, fill at the next session open."""
    positions: list[dict] = []
    index_by_symbol = {s: f.index for s, f in panel.items()}
    for signal_date, day in scored.groupby("signal_date"):
        picks = day.nlargest(TOP_PER_DAY, "composite")
        for _, p in picks.iterrows():
            symbol = p["ticker"]
            frame = panel[symbol]
            idx = index_by_symbol[symbol]
            loc = idx.get_indexer([signal_date])[0]
            if loc < 0 or loc + 1 >= len(frame):
                continue
            entry = float(frame["open"].iloc[loc + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue
            risk = max(7.0, min(15.0, p["atr_pct"] * 1.15 if p["atr_pct"] > 0 else 10.0))
            stop = entry * (1 - risk / 100.0)
            target = entry * (1 + risk * 2.5 / 100.0)
            gross = _exit_return(frame, loc + 1, entry, stop, target)
            cost = R.estimated_round_trip_cost(frame.iloc[loc])
            positions.append({
                "signal_date": signal_date, "ticker": symbol, "entry": entry,
                "gross_return": gross, "cost": cost,
                "net_return": gross - cost,
                "stress_net_return": gross - 2 * cost,
                "composite": p["composite"],
            })
    return pd.DataFrame(positions)


def rank_information(panel: dict[str, pd.DataFrame], scored: pd.DataFrame,
                     end: pd.Timestamp | None = None, buckets: int = 10) -> dict:
    """Does a higher score predict a better outcome anywhere in the cross-section?

    The top-N backtest only ever sees the names the rule would have bought, so it cannot
    distinguish "the ranking is uninformative" from "the ranking is informative but the
    whole asset class loses". This scores the *entire* cross-section and sorts it into
    score buckets. A rule with real content shows a monotone gradient; a flat profile
    means the score is decoration. An inverted gradient would at least be information.

    Restricted to train+validation by default: the test period is held out, and reading
    a gradient off it would spend evidence that has not been earned.
    """
    end = R.VALIDATION_END if end is None else end
    sub = scored[scored["signal_date"] <= end]
    if sub.empty:
        return {"applicable": False, "reason": "no scored rows in window"}

    idx_by_symbol = {s: f.index for s, f in panel.items()}
    rows = []
    for symbol, grp in sub.groupby("ticker"):
        frame = panel[symbol]
        locs = idx_by_symbol[symbol].get_indexer(grp["signal_date"])
        for (_, p), loc in zip(grp.iterrows(), locs):
            if loc < 0 or loc + 1 >= len(frame):
                continue
            entry = float(frame["open"].iloc[loc + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue
            risk = max(7.0, min(15.0, p["atr_pct"] * 1.15 if p["atr_pct"] > 0 else 10.0))
            gross = _exit_return(frame, loc + 1, entry,
                                 entry * (1 - risk / 100.0),
                                 entry * (1 + risk * 2.5 / 100.0))
            cost = R.estimated_round_trip_cost(frame.iloc[loc])
            rows.append({"composite": p["composite"], "hype": p["hype"],
                         "technical": p["technical"], "cost_hurdle": cost,
                         "net": gross - cost})
    df = pd.DataFrame(rows)
    if len(df) < buckets * 20:
        return {"applicable": False, "reason": f"only {len(df)} cross-section rows"}

    # A gradient only matters if it is bigger than what trading on it costs. Judging
    # direction alone would report "positive gradient" for a spread too small to fund a
    # single round trip, which reads as encouragement for a rule that cannot pay its way.
    hurdle = float(df["cost_hurdle"].mean()) * 100 if "cost_hurdle" in df else 1.5

    out = {"applicable": True, "cross_section_rows": int(len(df)),
           "buckets": buckets, "cost_hurdle_pct": round(hurdle, 4)}
    for col in ("composite", "hype", "technical"):
        b = pd.qcut(df[col].rank(method="first"), buckets, labels=False)
        means = (df.groupby(b)["net"].mean() * 100).round(4)
        rho = float(df[[col, "net"]].corr(method="spearman").iloc[0, 1])
        spread = float(means.get(buckets - 1) - means.get(0))
        all_neg = bool((means < 0).all())
        if abs(rho) < 0.05:
            verdict = "no usable ranking information"
        elif rho < 0:
            verdict = "inverted: high scores underperform"
        elif spread < hurdle:
            verdict = (f"gradient too small to trade: {spread:+.2f}% top-to-bottom "
                       f"against a {hurdle:.2f}% round trip")
        else:
            verdict = "positive gradient exceeding costs"
        if all_neg:
            verdict += "; every bucket loses money"
        out[col] = {
            "bucket_mean_net_pct": [means.get(i) for i in range(buckets)],
            "spearman_rho": round(rho, 4),
            "top_minus_bottom_pct": round(spread, 4),
            "all_buckets_negative": all_neg,
            "economically_tradeable": bool(spread >= hurdle and not all_neg),
            "verdict": verdict,
        }
    return out


def run(refresh: bool = False) -> dict:
    payload = R.load_panel(refresh=refresh)
    panel = R.build_feature_panel(payload)
    scored = score_sessions(panel)
    trades = simulate(panel, scored)
    if trades.empty:
        return {"strategy_id": STRATEGY_ID, "error": "no trades produced"}

    splits = R.split_metrics(trades)
    calendar = panel["IWM"].index if "IWM" in panel else None
    inference = stats.summarise(
        {STRATEGY_ID: trades}, STRATEGY_ID, R.TRAIN_END, R.VALIDATION_END, HOLD_DAYS,
    ) if hasattr(stats, "summarise") else {}
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    decomposition = {
        "all": stats.cost_decomposition(trades),
        "train_validation": stats.cost_decomposition(
            trades[trades["signal_date"] <= R.VALIDATION_END]),
        "test": stats.cost_decomposition(
            trades[trades["signal_date"] > R.VALIDATION_END]),
    }
    return {
        "strategy_id": STRATEGY_ID,
        "scored_rows": int(len(scored)),
        "trades": int(len(trades)),
        "splits": splits,
        "cost_decomposition": decomposition,
        "rank_information": rank_information(panel, scored),
        "inference": inference,
        "configuration": {
            "top_per_day": TOP_PER_DAY, "hold_days": HOLD_DAYS,
            "trail_arm_pct": TRAIL_ARM, "trail_pct": TRAIL_GAP,
            "unavailable_components": ["quality", "catalyst", "ai_review"],
            "neutral_value_used": NEUTRAL_UNAVAILABLE,
        },
        "calendar_sessions": int(len(calendar)) if calendar is not None else None,
    }


if __name__ == "__main__":
    import json
    out = run()
    print(json.dumps(out, indent=2, default=str))
