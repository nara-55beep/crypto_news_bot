"""
Causal daily-state research for ES/NQ.

This adds signal families not covered by the earlier intraday grid:

* prior-session Internal Bar Strength (close location) reversal;
* two-period daily RSI exhaustion with a long-term trend filter;
* consecutive daily-close exhaustion;
* interactions between yesterday's RTH return, today's known opening gap, and
  Monday, motivated by published Nasdaq-100 futures evidence.

Every feature is frozen before entry.  The strategy observes the completed 09:30
minute, enters the 09:31 open with one adverse tick, uses a stop scaled only by
the prior 20 completed RTH ranges, and exits no later than 15:59 New York.  The
test period is printed only after configurations are ranked on train+validation.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np

import lucid_causal_rebuild as L
import lucid_daily_research as D


@dataclass(frozen=True)
class StateConfig:
    market: str
    family: str
    threshold: float
    secondary: float
    trend_days: int
    side_mode: str
    stop_mult: float
    target_rr: float | None

    @property
    def label(self) -> str:
        target = "time" if self.target_rr is None else f"rr{self.target_rr:g}"
        return (
            f"{self.market}_{self.family}_a{self.threshold:g}_"
            f"b{self.secondary:g}_ma{self.trend_days}_"
            f"{self.side_mode}_s{self.stop_mult:g}_{target}"
        )


def _rsi(values: np.ndarray, length: int = 2) -> float:
    if len(values) < length + 2:
        return 50.0
    delta = np.diff(values.astype(float))
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)
    # Wilder's recursive average, computed only through the final completed close.
    gain = float(np.mean(gains[:length]))
    loss = float(np.mean(losses[:length]))
    for g, loss_i in zip(gains[length:], losses[length:]):
        gain = (gain * (length - 1) + float(g)) / length
        loss = (loss * (length - 1) + float(loss_i)) / length
    if loss <= 1e-12:
        return 100.0 if gain > 0 else 50.0
    rs = gain / loss
    return 100.0 - 100.0 / (1.0 + rs)


def _streak(closes: np.ndarray) -> int:
    if len(closes) < 2:
        return 0
    changes = np.diff(closes)
    if changes[-1] == 0:
        return 0
    sign = 1 if changes[-1] > 0 else -1
    count = 0
    for value in changes[::-1]:
        if value * sign <= 0:
            break
        count += 1
    return sign * count


def _allow_side(side: int, mode: str) -> bool:
    return mode == "both" or (mode == "long" and side > 0)


def _trend_allows(
    side: int,
    closes: np.ndarray,
    trend_days: int,
) -> bool:
    if trend_days <= 0:
        return True
    if len(closes) < trend_days:
        return False
    above = closes[-1] > float(np.mean(closes[-trend_days:]))
    return (side > 0 and above) or (side < 0 and not above)


_PREPARED: dict[int, list[dict | None]] = {}


def _prepare(days: list[L.Day]) -> list[dict | None]:
    cached = _PREPARED.get(id(days))
    if cached is not None and len(cached) == len(days):
        return cached
    closes = np.asarray([float(day.cl[-1]) for day in days], dtype=float)
    out: list[dict | None] = [None] * len(days)
    for i in range(205, len(days)):
        day = days[i]
        previous = days[i - 1]
        history = closes[:i]
        ph = float(np.max(previous.hi))
        pl = float(np.min(previous.lo))
        pr = ph - pl
        if pr <= 0:
            continue
        pc = float(previous.cl[-1])
        prior_ranges = np.asarray(
            [
                float(np.max(x.hi) - np.min(x.lo))
                for x in days[i - 20:i]
            ]
        )
        out[i] = {
            "ibs": (pc - pl) / pr,
            "prior_return": (pc - float(previous.op[0])) / pr,
            "gap": (float(day.op[0]) - pc) / pr,
            "rsi2": _rsi(history, 2),
            "streak": _streak(history),
            "above100": pc > float(np.mean(history[-100:])),
            "above200": pc > float(np.mean(history[-200:])),
            "scale": float(np.median(prior_ranges)),
        }
    _PREPARED[id(days)] = out
    return out


def _signal(
    days: list[L.Day],
    i: int,
    cfg: StateConfig,
    feature: dict,
) -> int:
    day = days[i]
    ibs = float(feature["ibs"])
    prior_return = float(feature["prior_return"])
    gap = float(feature["gap"])
    side = 0

    if cfg.family == "ibs":
        if ibs <= cfg.threshold:
            side = 1
        elif ibs >= 1.0 - cfg.threshold:
            side = -1
    elif cfg.family == "rsi2":
        value = float(feature["rsi2"])
        if value <= cfg.threshold:
            side = 1
        elif value >= 100.0 - cfg.threshold:
            side = -1
    elif cfg.family == "streak":
        value = int(feature["streak"])
        need = int(cfg.threshold)
        if value <= -need:
            side = 1
        elif value >= need:
            side = -1
    elif cfg.family == "ibs_gap":
        # Require both the prior close and the opening gap to be stretched in
        # the same direction, then fade the combined move.
        if ibs <= cfg.threshold and gap <= -cfg.secondary:
            side = 1
        elif ibs >= 1.0 - cfg.threshold and gap >= cfg.secondary:
            side = -1
    elif cfg.family.startswith("state_"):
        prior_sign = 1 if prior_return > cfg.secondary else -1 if prior_return < -cfg.secondary else 0
        gap_sign = 1 if gap > cfg.threshold else -1 if gap < -cfg.threshold else 0
        if prior_sign == 0 or gap_sign == 0:
            return 0
        agrees = prior_sign == gap_sign
        if cfg.family == "state_agree_reverse" and agrees:
            side = -gap_sign
        elif cfg.family == "state_disagree_gap_reverse" and not agrees:
            side = -gap_sign
        elif cfg.family == "state_disagree_prior" and not agrees:
            side = prior_sign
        elif cfg.family == "state_monday_reverse" and day.day.weekday() == 0:
            side = -gap_sign

    if not side or not _allow_side(side, cfg.side_mode):
        return 0
    if cfg.trend_days:
        above = bool(feature[f"above{cfg.trend_days}"])
        if (side > 0 and not above) or (side < 0 and above):
            return 0
    return side


def generate(days: list[L.Day], cfg: StateConfig) -> list[L.Trade]:
    out = []
    prepared = _prepare(days)
    for i in range(205, len(days)):
        feature = prepared[i]
        if feature is None:
            continue
        side = _signal(days, i, cfg, feature)
        if not side:
            continue
        scale = float(feature["scale"])
        if not math.isfinite(scale) or scale <= 0:
            continue
        trade = D._trade(
            days[i],
            side,
            cfg.stop_mult * scale,
            cfg.target_rr,
            cfg.label,
        )
        if trade is not None:
            out.append(trade)
    return out


def configs(markets: list[str]) -> list[StateConfig]:
    out = []
    execution = [
        (trend, side, stop, target)
        for trend in (0, 100, 200)
        for side in ("both", "long")
        for stop in (0.25, 0.50)
        for target in (None, 2.0)
    ]
    for market in markets:
        for threshold in (0.20, 0.25):
            for trend, side, stop, target in execution:
                out.append(
                    StateConfig(
                        market, "ibs", threshold, 0.0,
                        trend, side, stop, target,
                    )
                )
        for threshold in (5.0, 10.0, 15.0):
            for trend in (100, 200):
                for side in ("both", "long"):
                    for stop in (0.25, 0.50):
                        for target in (None, 2.0):
                            out.append(
                                StateConfig(
                                    market, "rsi2", threshold, 0.0,
                                    trend, side, stop, target,
                                )
                            )
        for threshold in (2.0, 3.0):
            for trend, side, stop, target in execution:
                out.append(
                    StateConfig(
                        market, "streak", threshold, 0.0,
                        trend, side, stop, target,
                    )
                )
        for ibs_threshold in (0.20, 0.25):
            for gap_threshold in (0.0005, 0.001):
                for trend, side, stop, target in execution:
                    out.append(
                        StateConfig(
                            market, "ibs_gap", ibs_threshold, gap_threshold,
                            trend, side, stop, target,
                        )
                    )
        for family in (
            "state_agree_reverse",
            "state_disagree_gap_reverse",
            "state_disagree_prior",
            "state_monday_reverse",
        ):
            for gap_threshold in (0.0005, 0.001, 0.002):
                for prior_threshold in (0.0, 0.20):
                    for stop in (0.25, 0.50):
                        for target in (None, 2.0):
                            out.append(
                                StateConfig(
                                    market, family, gap_threshold,
                                    prior_threshold, 0, "both", stop, target,
                                )
                            )
    return list(dict.fromkeys(out))


def _stats(
    trades: list[L.Trade],
    risk: float,
    lo: date | None,
    hi: date | None,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 50:
        return -1e9
    if min(train["pf"], valid["pf"]) <= 1.0:
        return -1e6 + min(train["pf"], valid["pf"])
    return (
        min(train["pf"], valid["pf"]) * math.log1p(min(train["n"], valid["n"]))
        + min(train["avg"], valid["avg"]) / 100.0
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["es", "nq"])
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    rows = []
    for market in args.markets:
        days = L.load_days(market)
        grid = configs([market])
        print(f"{market}: {len(grid)} daily-state configurations", flush=True)
        for number, cfg in enumerate(grid, 1):
            trades = generate(days, cfg)
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(
                trades, args.risk, date(2022, 1, 1), L.VALID_END
            )
            rows.append((_score(train, valid), cfg, trades, train, valid))
            if number % 100 == 0:
                print(f"  completed {number}/{len(grid)}", flush=True)
    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, args.risk, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {score:.3f}")
        for label, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {label:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
