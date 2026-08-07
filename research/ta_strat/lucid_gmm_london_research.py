"""
Independent causal London-session GMM transition research.

Inspired by Mesfin's disclosed high-level "Regime 0 -> Regime 2" positive
control, but not a reproduction because the source does not publish its feature
vector or fitted model.  This version uses the same transparent three features
as the local RTH model and monthly prior-only GMM fits:

  * normalized completed 15-minute return, range, and volume;
  * components canonicalized as bear=0, active=1, bull=2;
  * long only after a clean 0->2 transition with no active state in the prior
    two completed bars;
  * entry at the next one-minute open;
  * exit 45/60/75 minutes later or 08:30 New York, whichever comes first;
  * a protective stop scaled from the prior 14 completed London-session ranges.

The local USATECH data and volume are Dukascopy cash-index proxies, not CME MNQ.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, time as dtime

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E
import lucid_gmm_confluence_research as G


@dataclass(frozen=True)
class LondonConfig:
    stop_range: float
    hold_minutes: int

    @property
    def label(self) -> str:
        return f"nq_london_gmm_02_st{self.stop_range:g}_h{self.hold_minutes}"


def load_london_days(market: str = "nq") -> list[L.Day]:
    paths = [
        os.path.join(L.CACHE, f"{market}_1m_london_10y.csv"),
        os.path.join(L.CACHE, f"{market}_1m_10y.csv"),
        os.path.join(L.CACHE, f"{market}_1m_3y.csv"),
    ]
    frames = [
        pd.read_csv(
            path,
            usecols=["dt_utc", "open", "high", "low", "close", "volume"],
        )
        for path in paths if os.path.exists(path)
    ]
    if not frames:
        raise FileNotFoundError(f"no one-minute history for {market}")
    frame = pd.concat(frames, ignore_index=True)
    frame["dt_utc"] = pd.to_datetime(frame["dt_utc"], utc=True)
    frame = frame.dropna(
        subset=["dt_utc", "open", "high", "low", "close"]
    )
    frame = frame.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc")
    local = frame["dt_utc"].dt.tz_convert(L.NY)
    times = local.dt.time
    mask = (times >= dtime(3, 0)) & (times <= dtime(8, 30))
    frame = frame.loc[mask].copy()
    frame["day"] = local.loc[mask].dt.date
    out = []
    for session, group in frame.groupby("day", sort=True):
        group = group.reset_index(drop=True)
        stamp = group["dt_utc"].dt.tz_convert(L.NY)
        if (
            len(group) < 315
            or stamp.iloc[0].time() > dtime(3, 5)
            or stamp.iloc[-1].time() < dtime(8, 30)
        ):
            continue
        out.append(L._day_from_group(market, session, group))
    return out


def clean_transition_signals(
    bars: list[G.BarRef],
    states: np.ndarray,
) -> list[G.Signal]:
    out = []
    for i in range(2, len(bars)):
        if not (
            bars[i].day == bars[i - 1].day == bars[i - 2].day
            and states[i] == 2
            and states[i - 1] == 0
            and states[i - 2] != 1
        ):
            continue
        out.append(
            G.Signal(
                bar_i=i,
                day_i=bars[i].day_i,
                end_i=bars[i].end_i,
                side_hint=1,
                transition=1.0,
            )
        )
    return out


def _prior_range(days: list[L.Day], length: int = 14) -> np.ndarray:
    ranges = np.asarray([
        float(np.max(day.hi) - np.min(day.lo)) for day in days
    ])
    out = np.full(len(days), np.nan)
    for i in range(length, len(days)):
        out[i] = float(np.mean(ranges[i - length:i]))
    return out


def generate(
    days: list[L.Day],
    signals: list[G.Signal],
    cfg: LondonConfig,
) -> list[L.Trade]:
    out = []
    scale = _prior_range(days)
    tick = L.MARKETS["nq"]["tick"]
    pv = L.MARKETS["nq"]["pv"]
    used: set[date] = set()
    for signal in signals:
        day = days[signal.day_i]
        if day.day in used:
            continue
        distance = cfg.stop_range * float(scale[signal.day_i])
        if not math.isfinite(distance) or distance <= tick:
            continue
        entry_i = signal.end_i + 1
        final = np.flatnonzero(day.minute >= -60)
        if entry_i >= len(day.op) or not len(final):
            continue
        final_i = int(final[0])
        exit_i = min(entry_i + cfg.hold_minutes, final_i)
        if exit_i <= entry_i:
            continue
        entry = float(day.op[entry_i]) + tick
        stop = entry - distance
        raw_exit = float(day.op[exit_i])
        actual_exit_i = exit_i
        reason = "time" if exit_i < final_i else "0830"
        for i in range(entry_i, exit_i):
            if float(day.lo[i]) <= stop:
                raw_exit = min(stop, float(day.op[i]))
                actual_exit_i = i
                reason = "stop"
                break
        exit_px = raw_exit - tick
        out.append(
            L.Trade(
                market="nq",
                strategy=cfg.label,
                day=day.day,
                entry_ts=pd.Timestamp(day.ts[entry_i]),
                exit_ts=pd.Timestamp(day.ts[actual_exit_i]),
                side=1,
                entry=entry,
                stop=stop,
                target=math.nan,
                exit=exit_px,
                reason=reason,
                risk_per_micro=(entry - stop + tick) * pv,
                gross_per_micro=(exit_px - entry) * pv,
            )
        )
        used.add(day.day)
    return out


def main() -> int:
    days = load_london_days()
    bars = G._bars(days, 15)
    x = G._features(bars)
    states = G.walkforward_states(bars, x)
    signals = clean_transition_signals(bars, states)
    rows = []
    for stop in (0.25, 0.50, 0.75):
        for hold in (45, 60, 75):
            cfg = LondonConfig(stop, hold)
            trades = generate(days, signals, cfg)
            train = L.basic_stats(L.size_trades(
                L._slice(trades, None, L.TRAIN_END), 500.0
            ))
            valid = L.basic_stats(L.size_trades(
                L._slice(trades, date(2022, 1, 1), L.VALID_END), 500.0
            ))
            rows.append((
                E.development_score(train, valid),
                cfg, trades, train, valid,
            ))
    rows.sort(key=lambda row: row[0], reverse=True)
    print(
        f"London sessions={len(days)} bars={len(bars)} "
        f"clean_transitions={len(signals)}"
    )
    print("FINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows:
        test = L.basic_stats(L.size_trades(
            L._slice(trades, date(2024, 1, 1), None), 500.0
        ))
        print(f"\n{cfg.label} score {score:.3f}")
        for name, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {name:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
