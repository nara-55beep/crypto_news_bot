"""Tests for the delta-neutral funding-carry scanner.

The scanner's whole purpose is to refuse to flatter a funding rate, so most of these
tests assert that a headline number is correctly cut down: by fees, by holding period,
or by the realized distribution behind it.
"""

import math
import re
import unittest
from pathlib import Path

import funding_carry_scan as carry


REPO = Path(__file__).resolve().parents[1]

FLOOR_RATE = 0.0001        # +0.01%/8h, where liquid majors normally sit
SPIKE_RATE = 0.001464      # a headline-grabbing print


class TestCostModel(unittest.TestCase):
    def test_round_trip_charges_all_four_legs(self):
        expected = (2 * carry.SPOT_TAKER_FEE_RATE
                    + 2 * carry.PERP_TAKER_FEE_RATE) * 1e4
        self.assertEqual(carry.round_trip_cost_bps(), expected)
        self.assertAlmostEqual(carry.round_trip_cost_bps(), 30.0, places=9)

    def test_annualization_uses_three_settlements_a_day(self):
        self.assertAlmostEqual(carry.PERIODS_PER_YEAR, 1095.0, places=9)
        self.assertAlmostEqual(
            carry.annualized_bps(FLOOR_RATE), FLOOR_RATE * 1095 * 1e4, places=6)


class TestBreakEven(unittest.TestCase):
    def test_floor_rate_needs_ten_days_just_to_repay_fees(self):
        hours = carry.break_even_hours(FLOOR_RATE)
        self.assertAlmostEqual(hours, 240.0, places=6)
        self.assertAlmostEqual(hours / 24, 10.0, places=6)

    def test_a_spike_repays_fees_within_a_day(self):
        self.assertLess(carry.break_even_hours(SPIKE_RATE), 24.0)

    def test_zero_or_adverse_funding_never_breaks_even(self):
        for rate in (0.0, -0.0001, -0.01):
            with self.subTest(rate=rate):
                self.assertEqual(carry.break_even_hours(rate), math.inf)

    def test_break_even_is_the_point_where_net_turns_positive(self):
        hours = carry.break_even_hours(FLOOR_RATE)
        self.assertLess(carry.net_annualized_bps(FLOOR_RATE, hours - 1), 0.0)
        self.assertGreater(carry.net_annualized_bps(FLOOR_RATE, hours + 1), 0.0)


class TestNetOfFees(unittest.TestCase):
    def test_a_short_hold_is_negative_at_the_floor_rate(self):
        self.assertLess(carry.net_annualized_bps(FLOOR_RATE, 24 * 7), 0.0)

    def test_a_long_hold_approaches_the_headline_rate(self):
        headline = carry.annualized_bps(FLOOR_RATE)
        long_hold = carry.net_annualized_bps(FLOOR_RATE, 24 * 365)
        self.assertLess(long_hold, headline)
        self.assertGreater(long_hold, headline * 0.95)

    def test_net_rises_monotonically_with_holding_period(self):
        nets = [carry.net_annualized_bps(FLOOR_RATE, days * 24)
                for days in carry.HOLD_SENSITIVITY_DAYS]
        self.assertEqual(nets, sorted(nets))

    def test_a_zero_length_hold_is_pure_cost(self):
        self.assertLess(carry.net_annualized_bps(FLOOR_RATE, 0), 0.0)

    def test_sensitivity_spans_negative_to_positive_at_the_floor_rate(self):
        rows = carry.hold_sensitivity(FLOOR_RATE, 67.0)
        self.assertEqual([row["days"] for row in rows],
                         list(carry.HOLD_SENSITIVITY_DAYS))
        self.assertLess(rows[0]["net_ann_bps"], 0.0)      # 7 days
        self.assertGreater(rows[-1]["net_ann_bps"], 0.0)  # 365 days
        for row in rows:
            self.assertAlmostEqual(
                row["dollars_per_year"], 67.0 * row["net_ann_bps"] / 1e4, places=9)


class TestStability(unittest.TestCase):
    def test_reports_the_realized_distribution(self):
        stats = carry.stability([0.0002, 0.0001, 0.0003, -0.0001])
        self.assertEqual(stats["samples"], 4)
        self.assertAlmostEqual(stats["mean_bps"], 1.25, places=9)
        self.assertAlmostEqual(stats["positive_fraction"], 0.75, places=9)
        self.assertAlmostEqual(stats["min_bps"], -1.0, places=9)

    def test_a_flipping_rate_is_visible_as_a_low_positive_fraction(self):
        flipping = carry.stability([0.002, -0.002, 0.002, -0.002])
        steady = carry.stability([0.0005] * 4)
        self.assertAlmostEqual(flipping["positive_fraction"], 0.5, places=9)
        self.assertAlmostEqual(steady["positive_fraction"], 1.0, places=9)
        self.assertGreater(flipping["stdev_bps"], steady["stdev_bps"])

    def test_empty_history_is_not_treated_as_a_yield(self):
        stats = carry.stability([])
        self.assertEqual(stats["samples"], 0)
        self.assertEqual(stats["mean_bps"], 0.0)
        self.assertEqual(stats["positive_fraction"], 0.0)

    def test_a_spike_does_not_survive_as_a_realized_mean(self):
        """One huge print among ordinary ones must not read as a durable yield."""
        history = [SPIKE_RATE] + [0.00005] * 29
        stats = carry.stability(history)
        self.assertLess(carry.annualized_bps(stats["mean_bps"] / 1e4),
                        carry.annualized_bps(SPIKE_RATE) * 0.15)


class TestSizing(unittest.TestCase):
    def test_spot_is_paid_in_full_so_notional_is_below_bankroll(self):
        self.assertAlmostEqual(carry.hedged_notional(100.0, 0.5), 100 / 1.5, places=9)
        self.assertLess(carry.hedged_notional(100.0), 100.0)

    def test_no_bankroll_supports_no_position(self):
        for bankroll in (0.0, -50.0):
            self.assertEqual(carry.hedged_notional(bankroll), 0.0)

    def test_a_hundred_dollar_bankroll_earns_single_digit_dollars_a_year(self):
        """The honest headline: the mechanism works, the size does not."""
        notional = carry.hedged_notional(100.0)
        net = carry.net_annualized_bps(FLOOR_RATE, 24 * 90)
        self.assertLess(notional * net / 1e4, 12.0)
        self.assertGreater(carry.dollars_per_month(net, notional), 0.0)
        self.assertLess(carry.dollars_per_month(net, notional), 1.0)


class TestRanking(unittest.TestCase):
    VOLUMES = {"AAAUSDT": 5e8, "BBBUSDT": 5e8, "CCCUSDT": 5e8, "THINUSDT": 1e6}
    SPOT = {"AAAUSDT", "BBBUSDT", "CCCUSDT", "THINUSDT"}

    def test_ranks_by_net_carry_not_headline(self):
        rows = carry.rank_candidates(
            {"AAAUSDT": 0.0002, "BBBUSDT": 0.0004}, self.VOLUMES, self.SPOT)
        self.assertEqual([row["symbol"] for row in rows], ["BBBUSDT", "AAAUSDT"])
        self.assertGreater(rows[0]["net_ann_bps"], rows[1]["net_ann_bps"])

    def test_a_perp_with_no_spot_leg_cannot_be_hedged_and_is_dropped(self):
        rows = carry.rank_candidates(
            {"AAAUSDT": 0.0002, "NOSPOTUSDT": 0.01}, self.VOLUMES | {"NOSPOTUSDT": 5e8},
            self.SPOT)
        self.assertEqual([row["symbol"] for row in rows], ["AAAUSDT"])

    def test_illiquid_perps_are_dropped(self):
        rows = carry.rank_candidates(
            {"THINUSDT": 0.01}, self.VOLUMES, self.SPOT)
        self.assertEqual(rows, [])

    def test_negative_funding_is_not_a_short_perp_carry(self):
        rows = carry.rank_candidates(
            {"AAAUSDT": -0.0005}, self.VOLUMES, self.SPOT)
        self.assertEqual(rows, [])

    def test_every_row_carries_the_fee_corrected_fields(self):
        rows = carry.rank_candidates({"AAAUSDT": FLOOR_RATE}, self.VOLUMES, self.SPOT)
        row = rows[0]
        self.assertLess(row["net_ann_bps"], row["headline_ann_bps"])
        self.assertAlmostEqual(row["break_even_hours"], 240.0, places=6)


class TestNoTradingCapability(unittest.TestCase):
    SOURCE = (REPO / "funding_carry_scan.py").read_text(encoding="utf-8")

    def test_module_only_issues_public_gets(self):
        for verb in ("session.post", "session.put", "session.delete", ".post("):
            self.assertNotIn(verb, self.SOURCE)
        self.assertEqual(self.SOURCE.count("session.get("), 1)

    def test_no_credential_machinery(self):
        lowered = self.SOURCE.lower()
        for token in ("api_key", "apikey", "secret", "hmac", "signature",
                      "x-mbx-apikey", "getenv", "environ", "/order"):
            self.assertNotIn(token, lowered)

    def test_every_endpoint_is_public_market_data(self):
        for url in re.findall(r"https?://[^\s\"']+", self.SOURCE):
            self.assertTrue(
                url.startswith("https://fapi.binance.com/fapi/v1")
                or url.startswith("https://api.binance.com/api/v3"),
                f"unexpected endpoint {url}")


if __name__ == "__main__":
    unittest.main()
