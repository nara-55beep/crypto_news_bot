"""
Walk-forward GMM/Markov confluence research inspired by Mesfin (2026).

This is NOT a reproduction of the paper's positive-control strategy.  The paper
does not disclose its GMM feature vector, component-label mapping, or explicit
trade direction.  This file supplies a transparent, independently testable
specification:

  * clock-aligned completed NQ five-minute bars;
  * causal z-scores of return, range, and volume;
  * three-component GMM refit at each month using only earlier bars;
  * components canonicalized as bear / active / bull by mean return;
  * prior-200-bar active-to-bull transition probability;
  * signal when current state is active, P(active->bull) > 0.15, and the
    completed bar's rolling-50 volume z-score > 0.5;
  * a resting ATR-scaled pullback order placed after the signal bar closes;
  * a fixed 13-five-minute-bar horizon and a protective ATR stop.

The local Dukascopy USATECH index volume is not CME MNQ volume.  Even a strong
result here would be a research lead requiring a CME-data replication.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E


@dataclass(frozen=True)
class BarRef:
    day_i: int
    start_i: int
    end_i: int
    day: date
    month: tuple[int, int]
    ret: float
    range_bps: float
    log_volume: float


@dataclass(frozen=True)
class Signal:
    bar_i: int
    day_i: int
    end_i: int
    side_hint: int
    transition: float


@dataclass(frozen=True)
class GConfig:
    pullback_atr: float
    stop_atr: float
    direction: str
    wait_bars: int
    start_minute: int = 0
    last_signal_minute: int = 300
    hold_bars: int = 13
    max_trades: int = 1

    @property
    def label(self) -> str:
        return (
            f"nq_gmm_markov_pb{self.pullback_atr:g}_"
            f"st{self.stop_atr:g}_{self.direction}_w{self.wait_bars}_"
            f"h{self.hold_bars}_"
            f"m{self.start_minute}-{self.last_signal_minute}_"
            f"mx{self.max_trades}"
        )


def configs() -> list[GConfig]:
    return [
        GConfig(pullback, stop, direction, wait)
        for pullback in (0.05, 0.10, 0.15)
        for stop in (0.25, 0.50)
        for direction in ("long", "bar")
        for wait in (3, 13)
    ]


def _bars(days: list[L.Day], tf: int = 5) -> list[BarRef]:
    out = []
    for day_i, day in enumerate(days):
        # Use clock alignment directly so the helper also supports sessions
        # before 09:30, whose minute offsets are negative.
        ends = np.flatnonzero(
            (day.minute.astype(np.int64) % tf == tf - 1)
            & (np.arange(len(day.minute)) < len(day.minute) - 1)
        )
        for end_i in ends:
            start_i = int(end_i) - tf + 1
            if start_i < 0:
                continue
            op = float(day.op[start_i])
            close = float(day.cl[end_i])
            if op <= 0:
                continue
            out.append(
                BarRef(
                    day_i=day_i,
                    start_i=start_i,
                    end_i=int(end_i),
                    day=day.day,
                    month=(day.day.year, day.day.month),
                    ret=(close / op - 1.0) * 10_000.0,
                    range_bps=(
                        float(np.max(day.hi[start_i:end_i + 1]))
                        - float(np.min(day.lo[start_i:end_i + 1]))
                    ) / op * 10_000.0,
                    log_volume=float(
                        np.log1p(np.sum(day.vol[start_i:end_i + 1]))
                    ),
                )
            )
    return out


def _features(bars: list[BarRef]) -> np.ndarray:
    raw = pd.DataFrame(
        {
            "ret": [bar.ret for bar in bars],
            "range": [bar.range_bps for bar in bars],
            "volume": [bar.log_volume for bar in bars],
        }
    )
    out = np.full((len(raw), 3), np.nan)
    for column, window, target in (
        ("ret", 200, 0), ("range", 200, 1), ("volume", 50, 2)
    ):
        series = raw[column]
        mean = series.rolling(window).mean().shift(1)
        std = series.rolling(window).std(ddof=1).shift(1)
        out[:, target] = ((series - mean) / std.replace(0.0, np.nan)).to_numpy()
    return out


def walkforward_states(
    bars: list[BarRef],
    x: np.ndarray,
    *,
    train_bars: int = 5_000,
    random_state: int = 20260731,
    n_init: int = 3,
) -> np.ndarray:
    """Canonical bear=0 / active=1 / bull=2 states from prior-only monthly fits.

    ``x`` must keep return as its first column because component labels are
    canonicalized by that coordinate.  The optional arguments exist so the
    frozen strategy can be tested against benign model-specification changes;
    they are not optimized inside this function.
    """
    if x.ndim != 2 or x.shape[0] != len(bars) or x.shape[1] < 1:
        raise ValueError("x must have one row per bar and return in column zero")
    if train_bars < 1_000:
        raise ValueError("train_bars must be at least 1,000")
    states = np.full(len(bars), -1, dtype=np.int8)
    months = []
    for bar in bars:
        if not months or months[-1] != bar.month:
            months.append(bar.month)

    month_indices = {
        month: np.asarray(
            [i for i, bar in enumerate(bars) if bar.month == month], dtype=int
        )
        for month in months
    }
    for month in months:
        indices = month_indices[month]
        start = int(indices[0])
        prior_good = np.flatnonzero(
            np.all(np.isfinite(x[:start]), axis=1)
        )
        if len(prior_good) < 1_000:
            continue
        train_idx = prior_good[-train_bars:]
        model = GaussianMixture(
            n_components=3,
            covariance_type="full",
            reg_covar=1e-4,
            n_init=n_init,
            max_iter=300,
            random_state=random_state,
        )
        model.fit(x[train_idx])

        means = model.means_[:, 0]
        bull_raw = int(np.argmax(means))
        bear_raw = int(np.argmin(means))
        active_raw = int(({0, 1, 2} - {bull_raw, bear_raw}).pop())
        mapping = {bear_raw: 0, active_raw: 1, bull_raw: 2}
        finite = np.all(np.isfinite(x[indices]), axis=1)
        if np.any(finite):
            predicted = model.predict(x[indices[finite]])
            states[indices[finite]] = np.asarray(
                [mapping[int(value)] for value in predicted], dtype=np.int8
            )
    return states


def walkforward_signals(
    bars: list[BarRef],
    x: np.ndarray,
    *,
    transition_threshold: float = 0.15,
    volume_threshold: float = 0.50,
) -> list[Signal]:
    states = walkforward_states(bars, x)
    signals: list[Signal] = []
    for bar_i in range(len(bars)):
        if states[bar_i] != 1:
            continue
        previous = states[max(0, bar_i - 201):bar_i]
        if len(previous) < 50:
            continue
        active = previous[:-1] == 1
        denominator = int(active.sum())
        transition = (
            float(np.sum(active & (previous[1:] == 2))) / denominator
            if denominator else 0.0
        )
        if (
            transition <= transition_threshold
            or x[bar_i, 2] <= volume_threshold
        ):
            continue
        sign = int(bars[bar_i].ret > 0) - int(bars[bar_i].ret < 0)
        signals.append(
            Signal(
                bar_i=bar_i,
                day_i=bars[bar_i].day_i,
                end_i=bars[bar_i].end_i,
                side_hint=sign,
                transition=transition,
            )
        )
    return signals


def _prior_atr(days: list[L.Day], length: int = 14) -> np.ndarray:
    tr = np.full(len(days), np.nan)
    for i, day in enumerate(days):
        high = float(np.max(day.hi))
        low = float(np.min(day.lo))
        if i == 0:
            tr[i] = high - low
        else:
            close = float(days[i - 1].cl[-1])
            tr[i] = max(high - low, abs(high - close), abs(low - close))
    out = np.full(len(days), np.nan)
    for i in range(length, len(days)):
        out[i] = float(np.mean(tr[i - length:i]))
    return out


def _tick_down(price: float, tick: float) -> float:
    return math.floor((price + 1e-10) / tick) * tick


def _tick_up(price: float, tick: float) -> float:
    return math.ceil((price - 1e-10) / tick) * tick


def generate(
    days: list[L.Day],
    bars: list[BarRef],
    signals: list[Signal],
    cfg: GConfig,
    market: str = "nq",
) -> list[L.Trade]:
    out = []
    atr = _prior_atr(days)
    tick = L.MARKETS[market]["tick"]
    pv = L.MARKETS[market]["pv"]
    day_state: dict[date, tuple[int, int]] = {}

    for signal in signals:
        day = days[signal.day_i]
        completed, last_exit_i = day_state.get(day.day, (0, -1))
        if completed >= cfg.max_trades or signal.end_i < last_exit_i:
            continue
        signal_minute = int(day.minute[signal.end_i])
        if not (
            cfg.start_minute <= signal_minute <= cfg.last_signal_minute
        ):
            continue
        daily_atr = float(atr[signal.day_i])
        if not math.isfinite(daily_atr) or daily_atr <= tick:
            continue
        side = (
            1 if cfg.direction == "long"
            else -1 if cfg.direction == "short"
            else signal.side_hint
        )
        if side == 0:
            continue
        signal_close = float(day.cl[signal.end_i])
        raw_trigger = signal_close - side * cfg.pullback_atr * daily_atr
        # A buy limit is rounded down and a sell limit up so the executable
        # order never becomes more aggressive than the specified pullback.
        trigger = (
            _tick_down(raw_trigger, tick)
            if side > 0 else _tick_up(raw_trigger, tick)
        )
        final_matches = np.flatnonzero(day.minute >= 389)
        if not len(final_matches):
            continue
        final_i = int(final_matches[0])
        exit_i = signal.end_i + 1 + cfg.hold_bars * 5
        # "Exit at bar 13" is a fixed horizon, not an instruction to shorten
        # late-day observations.  A signal without the full horizon is ineligible.
        if exit_i > final_i:
            continue
        deadline = min(
            exit_i - 1,
            signal.end_i + cfg.wait_bars * 5,
        )
        fill_i = None
        entry = math.nan
        for i in range(signal.end_i + 1, deadline + 1):
            open_price = float(day.op[i])
            touched = (
                (
                    open_price <= trigger - tick
                    or float(day.lo[i]) <= trigger - tick
                )
                if side > 0 else (
                    open_price >= trigger + tick
                    or float(day.hi[i]) >= trigger + tick
                )
            )
            if not touched:
                continue
            fill_i = i
            # The source is a midpoint proxy, so one full tick of penetration
            # is required before a resting order is considered executable.
            # A favorable gap is charged one tick of spread but never fills
            # worse than the resting limit.
            entry = (
                min(trigger, _tick_up(open_price + tick, tick))
                if side > 0 else
                max(trigger, _tick_down(open_price - tick, tick))
            )
            break
        if fill_i is None:
            continue

        raw_stop = entry - side * cfg.stop_atr * daily_atr
        # Round away from the entry so tick normalization cannot silently
        # tighten the configured protective distance.
        stop = (
            _tick_down(raw_stop, tick)
            if side > 0 else _tick_up(raw_stop, tick)
        )
        raw_exit = float(day.op[exit_i])
        reason = "time"
        actual_exit_i = exit_i
        for i in range(fill_i, exit_i):
            stopped = (
                float(day.lo[i]) <= stop
                if side > 0 else float(day.hi[i]) >= stop
            )
            if stopped:
                raw_exit = (
                    min(stop, float(day.op[i]))
                    if side > 0 else max(stop, float(day.op[i]))
                )
                actual_exit_i = i
                reason = "stop"
                break
        adverse_exit = raw_exit - side * tick
        exit_px = (
            _tick_down(adverse_exit, tick)
            if side > 0 else _tick_up(adverse_exit, tick)
        )
        out.append(
            L.Trade(
                market=market,
                strategy=(
                    cfg.label if market == "nq"
                    else f"{market}_{cfg.label}"
                ),
                day=day.day,
                entry_ts=pd.Timestamp(day.ts[fill_i]),
                exit_ts=pd.Timestamp(day.ts[actual_exit_i]),
                side=side,
                entry=float(entry),
                stop=float(stop),
                target=math.nan,
                exit=float(exit_px),
                reason=reason,
                risk_per_micro=(abs(entry - stop) + tick) * pv,
                gross_per_micro=side * (exit_px - entry) * pv,
            )
        )
        day_state[day.day] = (completed + 1, actual_exit_i)
    return sorted(out, key=lambda trade: (trade.entry_ts, trade.exit_ts))


def _stats(
    trades: list[L.Trade],
    lo: date | None,
    hi: date | None,
    risk: float,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=500.0)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument(
        "--signal-mode",
        choices=("paper_inspired", "active_only"),
        default="paper_inspired",
        help=(
            "paper_inspired adds the disclosed Markov/volume gates; active_only "
            "tests the independently specified canonical active-state signal"
        ),
    )
    args = ap.parse_args()

    days = L.load_days("nq")
    bars = _bars(days)
    x = _features(bars)
    if args.signal_mode == "active_only":
        signals = walkforward_signals(
            bars, x, transition_threshold=-1.0, volume_threshold=-999.0
        )
    else:
        signals = walkforward_signals(bars, x)
    print(
        f"sessions={len(days)} five_minute_bars={len(bars)} "
        f"signal_mode={args.signal_mode} walkforward_signals={len(signals)}",
        flush=True,
    )
    rows = []
    for cfg in configs():
        trades = generate(days, bars, signals, cfg)
        train = _stats(trades, None, L.TRAIN_END, args.risk)
        valid = _stats(
            trades, date(2022, 1, 1), L.VALID_END, args.risk
        )
        score = E.development_score(train, valid)
        rows.append((score, cfg, trades, train, valid))
    rows.sort(key=lambda row: row[0], reverse=True)

    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, date(2024, 1, 1), None, args.risk)
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
