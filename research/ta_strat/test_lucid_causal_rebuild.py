"""Focused invariants for the causal Lucid research harness."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lucid_causal_rebuild as L


def make_day(*, op, hi, lo, cl, minute=None) -> L.Day:
    n = len(cl)
    ts = pd.date_range("2026-01-05 14:30", periods=n, freq="min", tz="UTC").to_numpy()
    minute = np.arange(n, dtype=np.int16) if minute is None else np.asarray(minute, dtype=np.int16)
    x = np.asarray(cl, dtype=float)
    return L.Day(
        market="nq",
        day=date(2026, 1, 5),
        ts=ts,
        minute=minute,
        op=np.asarray(op, dtype=float),
        hi=np.asarray(hi, dtype=float),
        lo=np.asarray(lo, dtype=float),
        cl=x,
        vol=np.ones(n),
        vwap=x.copy(),
        sigma=np.ones(n),
        atr=np.ones(n),
        ema9=x.copy(),
        ema20=x.copy(),
    )


def sized(day: date, pnl: float, hour: int = 10) -> L.SizedTrade:
    ts = pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(hours=hour)
    trade = L.Trade(
        market="nq",
        strategy="test",
        day=day,
        entry_ts=ts,
        exit_ts=ts + pd.Timedelta(minutes=1),
        side=1,
        entry=100.0,
        stop=99.0,
        target=102.0,
        exit=100.0,
        reason="test",
        risk_per_micro=1.0,
        gross_per_micro=pnl,
    )
    return L.SizedTrade(trade, 1, pnl)


class CausalHarnessTests(unittest.TestCase):
    def test_complete_rth_minute_guard_rejects_internal_holes(self):
        complete = np.arange(390, dtype=np.int16)
        missing = np.delete(complete, 123)
        self.assertTrue(L._is_complete_rth_minutes(complete))
        self.assertFalse(L._is_complete_rth_minutes(missing))

    def test_entry_is_next_open_and_both_hit_is_stop_first(self):
        day = make_day(
            op=[100, 100, 100],
            hi=[100, 101, 103],
            lo=[100, 99.5, 98.5],
            cl=[100, 100, 100],
        )
        trade = L._make_trade(day, 0, 1, 99.0, rr=2.0, strategy="test")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_ts, pd.Timestamp(day.ts[1]))
        self.assertEqual(trade.exit_ts, pd.Timestamp(day.ts[2]))
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.entry, 100.25)
        self.assertAlmostEqual(trade.exit, 98.75)

    def test_stop_gap_uses_available_open_not_skipped_level(self):
        day = make_day(
            op=[100, 100, 98],
            hi=[100, 100.5, 98.5],
            lo=[100, 99.5, 97.5],
            cl=[100, 100, 98],
        )
        trade = L._make_trade(day, 0, 1, 99.0, rr=2.0, strategy="test")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.exit, 97.75)

    def test_clock_alignment_does_not_shift_when_0930_bar_is_missing(self):
        day = make_day(
            op=np.ones(9),
            hi=np.ones(9),
            lo=np.ones(9),
            cl=np.ones(9),
            minute=np.arange(1, 10),
        )
        idx = L._sample_indices(day, 3)
        self.assertEqual(day.minute[idx].tolist(), [2, 5, 8])

    def test_sizing_is_integer_capped_and_includes_round_turn_commission(self):
        day = make_day(
            op=[100, 100, 100],
            hi=[100, 101, 103],
            lo=[100, 99.5, 100],
            cl=[100, 100, 102],
        )
        trade = L._make_trade(day, 0, 1, 99.0, rr=2.0, strategy="test")
        result = L.size_trades([trade], risk_usd=10_000)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0].qty, int)
        self.assertEqual(result[0].qty, 40)
        self.assertAlmostEqual(
            result[0].pnl,
            trade.gross_per_micro * 40 - L.COMMISSION_RT * 40,
        )

    def test_pass_days_include_zero_trade_sessions(self):
        d0 = date(2026, 1, 5)
        days = [d0 + timedelta(days=i) for i in range(5)]
        result = L.eval_lucid([sized(days[0], 1500), sized(days[4], 1500)], days, horizon=5)
        self.assertEqual(result["starts"], 1)
        self.assertEqual(result["passes"], 1)
        self.assertEqual(result["median_days"], 5.0)

    def test_eod_floor_breach_uses_prior_closing_peak(self):
        d0 = date(2026, 1, 5)
        days = [d0, d0 + timedelta(days=1)]
        # After +$1,000 at the first EOD, the next-session floor is -$1,000
        # relative to the evaluation start. Reaching it is a breach.
        result = L.eval_lucid([sized(days[0], 1000), sized(days[1], -2000)], days, horizon=2)
        self.assertEqual(result["fails"], 1)
        self.assertEqual(result["passes"], 0)


if __name__ == "__main__":
    unittest.main()
