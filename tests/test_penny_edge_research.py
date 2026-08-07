import unittest

import numpy as np
import pandas as pd

from research import penny_edge_research as edge


class PennyEdgeResearchTests(unittest.TestCase):
    def test_cost_proxy_penalizes_illiquid_low_price_names(self):
        liquid = pd.Series({"dollar_volume20": 50_000_000, "raw_close": 4.0})
        illiquid = pd.Series({"dollar_volume20": 500_000, "raw_close": 0.5})
        self.assertGreater(
            edge.estimated_round_trip_cost(illiquid),
            edge.estimated_round_trip_cost(liquid),
        )
        self.assertGreaterEqual(edge.estimated_round_trip_cost(liquid), 0.01)
        self.assertLessEqual(edge.estimated_round_trip_cost(illiquid), 0.04)

    def test_ambiguous_daily_bar_assumes_stop_before_target(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        frame = pd.DataFrame(index=dates)
        frame["open"] = frame["high"] = frame["low"] = frame["close"] = 10.0
        frame["raw_close"] = 2.0
        frame["dollar_volume20"] = 20_000_000.0
        frame["atr_pct"] = 0.08
        frame["symbol"] = "TEST"
        # Entry is day 21 at 10.  On the same bar both an 8% stop and 16% target
        # print; a daily bar cannot establish order, so the engine must lose.
        frame.iloc[21, frame.columns.get_loc("low")] = 8.0
        frame.iloc[21, frame.columns.get_loc("high")] = 12.0
        spec = edge.StrategySpec("controlled_breakout", 5, 1.0, 2.0)
        trade = edge._simulate_one(frame, 20, spec)
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertLess(trade["gross_return"], 0)

    def test_negative_test_period_cannot_pass_edge_gate(self):
        good = {
            "trades": 100,
            "mean_net_pct": 1.0,
            "profit_factor": 1.5,
            "bootstrap_95_pct": [0.1, 2.0],
            "stress_mean_net_pct": 0.5,
            "max_symbol_share_of_positive_pnl_pct": 10.0,
        }
        bad = dict(good, mean_net_pct=-0.1, profit_factor=0.9)
        passed, failures = edge._numeric_gate(
            {"train": good, "validation": good, "test": bad}
        )
        self.assertFalse(passed)
        self.assertTrue(any("mean net return" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
