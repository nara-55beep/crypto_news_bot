"""Execution invariants for the literal NQ 15-minute mean-reversion audit."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lucid_causal_rebuild as L
import lucid_nq15_mr_audit as A


def make_day(
    op: list[float],
    hi: list[float],
    lo: list[float],
    cl: list[float],
    minute: list[int] | None = None,
) -> L.Day:
    n = len(cl)
    x = np.asarray(cl, dtype=float)
    return L.Day(
        market="nq",
        day=date(2026, 1, 5),
        ts=pd.date_range("2026-01-05 14:30", periods=n, freq="min", tz="UTC").to_numpy(),
        minute=np.asarray(
            np.arange(n) if minute is None else minute, dtype=np.int16
        ),
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


class NQ15AuditTests(unittest.TestCase):
    def test_signal_enters_next_minute_and_stop_wins_ambiguous_bar(self):
        day = make_day(
            op=[100, 100, 100],
            hi=[100, 103, 100],
            lo=[100, 98, 100],
            cl=[100, 100, 100],
        )
        trade = A._make_trade(day, 0, "test", 1, 99, 102)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_ts, pd.Timestamp(day.ts[1]))
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.entry, 100.25)
        self.assertAlmostEqual(trade.final_exit, 98.75)

    def test_partial_is_integer_and_be_only_applies_on_later_minute(self):
        day = make_day(
            op=[100, 100, 100, 100],
            hi=[100, 101.5, 100.5, 100],
            lo=[100, 99.5, 100, 100],
            cl=[100, 101, 100, 100],
        )
        trade = A._make_trade(day, 0, "test", 1, 99.25, 103)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.reason, "be")
        self.assertEqual(trade.split_qty(3), (1, 2))
        self.assertIsInstance(trade.split_qty(3)[0], int)

    def test_incomplete_fifteen_minute_block_is_not_used(self):
        complete = make_day(
            op=[100] * 15,
            hi=[101] * 15,
            lo=[99] * 15,
            cl=[100] * 15,
        )
        self.assertEqual(len(A.bars15(complete)), 0)  # no following minute to enter
        gapped_minutes = list(range(16))
        gapped_minutes.remove(7)
        gapped = make_day(
            op=[100] * 15,
            hi=[101] * 15,
            lo=[99] * 15,
            cl=[100] * 15,
            minute=gapped_minutes,
        )
        self.assertEqual(len(A.bars15(gapped)), 0)

    def test_quantity_is_integer_and_capped(self):
        day = make_day(
            op=[100, 100, 100],
            hi=[100, 100.5, 100],
            lo=[100, 99.5, 100],
            cl=[100, 100, 100],
        )
        trade = A._make_trade(day, 0, "test", 1, 99, 102)
        self.assertIsNotNone(trade)
        qty = A.entry_qty(trade, risk=100_000)
        self.assertIsInstance(qty, int)
        self.assertEqual(qty, A.MAX_MICROS)


if __name__ == "__main__":
    unittest.main()
