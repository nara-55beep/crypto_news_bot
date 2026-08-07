"""Execution invariants for the causal GMM research lead."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_gmm_confluence_research as G
import lucid_gmm_regime_portfolio as R


def _day(day_i: int, *, penetration: bool = False) -> L.Day:
    session = date(2020, 1, 2) + timedelta(days=day_i)
    n = 390
    op = np.full(n, 100.0)
    hi = np.full(n, 105.0)
    lo = np.full(n, 95.0)
    cl = np.full(n, 100.0)
    if day_i == 15:
        # Prior ATR is 10, signal close is 100, and a 0.10 ATR pullback limit
        # is therefore 99.00.  A midpoint low of exactly 99.00 is not enough;
        # the penetrated case trades a full NQ tick through it.
        hi[:] = 100.5
        lo[:] = 99.5
        lo[5] = 98.75 if penetration else 99.00
    ts = pd.date_range(
        f"{session.isoformat()} 13:30:00+00:00",
        periods=n,
        freq="min",
    ).to_numpy()
    zeros = np.zeros(n)
    return L.Day(
        market="nq",
        day=session,
        ts=ts,
        minute=np.arange(n, dtype=np.int16),
        op=op,
        hi=hi,
        lo=lo,
        cl=cl,
        vol=np.ones(n),
        vwap=op.copy(),
        sigma=zeros.copy(),
        atr=np.ones(n),
        ema9=op.copy(),
        ema20=op.copy(),
    )


def _signal(days: list[L.Day], end_i: int = 4) -> tuple[G.BarRef, G.Signal]:
    bar = G.BarRef(
        day_i=15,
        start_i=end_i - 4,
        end_i=end_i,
        day=days[15].day,
        month=(days[15].day.year, days[15].day.month),
        ret=0.0,
        range_bps=0.0,
        log_volume=0.0,
    )
    return bar, G.Signal(0, 15, end_i, 1, 0.0)


def _trade(
    session: date,
    start_minute: int,
    end_minute: int,
    strategy: str,
) -> L.Trade:
    base = pd.Timestamp(f"{session.isoformat()} 13:30:00+00:00")
    return L.Trade(
        market="nq",
        strategy=strategy,
        day=session,
        entry_ts=base + pd.Timedelta(minutes=start_minute),
        exit_ts=base + pd.Timedelta(minutes=end_minute),
        side=1,
        entry=100.0,
        stop=99.0,
        target=math.nan,
        exit=101.0,
        reason="time",
        risk_per_micro=2.5,
        gross_per_micro=1.5,
    )


class GMMExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = G.GConfig(
            0.10, 0.25, "long", 13,
            start_minute=0,
            last_signal_minute=300,
            hold_bars=13,
            max_trades=1,
        )

    def test_midpoint_touch_does_not_fill_resting_limit(self) -> None:
        days = [_day(i, penetration=False) for i in range(16)]
        bar, signal = _signal(days)
        self.assertEqual(G.generate(days, [bar], [signal], self.cfg), [])

    def test_one_tick_penetration_fills_only_after_signal_bar(self) -> None:
        days = [_day(i, penetration=True) for i in range(16)]
        bar, signal = _signal(days)
        trades = G.generate(days, [bar], [signal], self.cfg)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_ts, pd.Timestamp(days[15].ts[5]))
        self.assertEqual(trades[0].exit_ts, pd.Timestamp(days[15].ts[70]))
        for price in (
            trades[0].entry,
            trades[0].stop,
            trades[0].exit,
        ):
            self.assertAlmostEqual(price / 0.25, round(price / 0.25))

    def test_signal_without_full_horizon_is_ineligible(self) -> None:
        days = [_day(i, penetration=True) for i in range(16)]
        bar, signal = _signal(days, end_i=329)
        late_cfg = G.GConfig(
            0.10, 0.25, "long", 13,
            start_minute=0,
            last_signal_minute=389,
            hold_bars=13,
            max_trades=1,
        )
        self.assertEqual(G.generate(days, [bar], [signal], late_cfg), [])

    def test_portfolio_never_accepts_overlapping_positions(self) -> None:
        session = date(2020, 1, 2)
        active = [
            _trade(session, 10, 20, "active_a"),
            _trade(session, 20, 30, "active_b"),
        ]
        bear = [_trade(session, 15, 25, "bear")]
        merged = R.merge_nonoverlap(
            active,
            bear,
            active_first=True,
            daily_cap=3,
        )
        self.assertEqual(
            [(t.entry_ts.minute, t.exit_ts.minute) for t in merged],
            [(40, 50), (50, 0)],
        )


if __name__ == "__main__":
    unittest.main()
