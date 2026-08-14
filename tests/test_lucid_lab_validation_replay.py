from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "ta_strat"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import lucid_causal_rebuild as L  # noqa: E402
import lucid_lab_validation as V  # noqa: E402
import lucid_portfolio_policy as P  # noqa: E402


def source_day(session: date, *, adverse_low: float = 99.0) -> L.Day:
    start = pd.Timestamp(f"{session} 13:30:00")
    stamps = np.array(
        [start.to_datetime64() + np.timedelta64(i, "m") for i in range(390)]
    )
    op = np.full(390, 100.0)
    hi = np.full(390, 101.0)
    lo = np.full(390, 99.0)
    close = np.full(390, 100.0)
    lo[2] = adverse_low
    zeros = np.zeros(390)
    return L.Day(
        market="nq", day=session, ts=stamps,
        minute=np.arange(390, dtype=np.int16),
        op=op, hi=hi, lo=lo, cl=close, vol=np.ones(390),
        vwap=close.copy(), sigma=zeros.copy(), atr=np.ones(390),
        ema9=close.copy(), ema20=close.copy(),
    )


def recovering_trade(day: L.Day) -> L.Trade:
    return L.Trade(
        market="nq", strategy="morning_regime_test", day=day.day,
        entry_ts=pd.Timestamp(day.ts[1]), exit_ts=pd.Timestamp(day.ts[3]),
        side=1, entry=100.0, stop=0.0, target=350.0, exit=350.0,
        reason="target", risk_per_micro=99.0, gross_per_micro=500.0,
    )


class TestMinuteMarkedLucidReplay(unittest.TestCase):
    def test_unrealized_floor_breach_wins_over_later_profitable_exit(self):
        day = source_day(date(2026, 8, 3), adverse_low=-100.0)
        trade = recovering_trade(day)
        result = V.simulate_sequence(
            [[trade]], P.Policy(400.0, 100.0), V.RULES["25K"],
            V.PRESETS["normal"], price_paths=V.MinutePathStore({"nq": [day]}),
            session_labels=[day.day],
        )
        self.assertEqual(result.outcome, "breach")
        self.assertEqual(
            result.breach_reason, "intraday_open_equity_touched_eod_floor"
        )
        self.assertEqual(result.trades, 0)
        self.assertLessEqual(result.minimum_equity, -1_000.0)
        self.assertGreater(result.open_equity_checks, 0)

    def test_recovered_trade_passes_when_every_open_mark_stays_above_floor(self):
        day = source_day(date(2026, 8, 3), adverse_low=90.0)
        result = V.simulate_sequence(
            [[recovering_trade(day)]], P.Policy(400.0, 100.0), V.RULES["25K"],
            V.PRESETS["normal"], price_paths=V.MinutePathStore({"nq": [day]}),
            session_labels=[day.day],
        )
        self.assertEqual(result.outcome, "pass")
        self.assertEqual(result.trades, 1)
        self.assertEqual(result.breach_reason, "")

    def test_target_candle_cannot_hide_an_ambiguous_floor_touch(self):
        day = source_day(date(2026, 8, 3), adverse_low=99.0)
        day.lo[3] = -100.0
        result = V.simulate_sequence(
            [[recovering_trade(day)]], P.Policy(400.0, 100.0), V.RULES["25K"],
            V.PRESETS["normal"], price_paths=V.MinutePathStore({"nq": [day]}),
            session_labels=[day.day],
        )
        self.assertEqual(result.outcome, "breach")
        self.assertEqual(result.trades, 0)
        self.assertEqual(
            result.breach_reason, "intraday_open_equity_touched_eod_floor"
        )

    def test_stop_fill_prevents_post_exit_candle_extreme_from_being_counted(self):
        day = source_day(date(2026, 8, 3), adverse_low=-100.0)
        trade = L.Trade(
            market="nq", strategy="morning_regime_test", day=day.day,
            entry_ts=pd.Timestamp(day.ts[1]), exit_ts=pd.Timestamp(day.ts[2]),
            side=1, entry=100.0, stop=50.0, target=200.0, exit=50.0,
            reason="stop", risk_per_micro=99.0, gross_per_micro=-100.0,
        )
        result = V.simulate_sequence(
            [[trade]], P.Policy(400.0, 100.0), V.RULES["25K"],
            V.PRESETS["normal"], price_paths=V.MinutePathStore({"nq": [day]}),
            session_labels=[day.day],
        )
        self.assertEqual(result.outcome, "unfinished")
        self.assertEqual(result.trades, 1)
        self.assertGreater(result.terminal_profit, -1_000.0)
        self.assertEqual(result.breach_reason, "")

    def test_primary_windows_are_disjoint_and_keep_no_trade_days(self):
        labels = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
        result, paths = V.evaluate_sequences(
            [[] for _ in labels], labels, 45, P.Policy(400.0, 100.0),
            V.RULES["25K"], V.PRESETS["normal"],
            V.MinutePathStore({"nq": [], "es": []}),
        )
        self.assertEqual(result["windows"], 2)
        self.assertEqual(result["window_stride_sessions"], 45)
        self.assertFalse(result["windows_overlap"])
        self.assertEqual(result["unfinished"], 2)
        self.assertEqual([path.used_days for path in paths], [45, 45])

    def test_minute_path_rejects_ohlc_that_could_not_exist(self):
        day = source_day(date(2026, 8, 3))
        day.hi[12] = 99.0
        with self.assertRaisesRegex(ValueError, "OHLC ordering"):
            V.MinutePathStore({"nq": [day]})

    def test_exact_interval_is_not_the_old_overlapping_window_interval(self):
        lo, hi = V._clopper_pearson(0, 13)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.20)
        self.assertLess(hi, 0.30)


if __name__ == "__main__":
    unittest.main()
