"""Tests for the airdrop radar.

The scanner makes two kinds of claim: a filtering claim (this protocol is a plausible
airdrop candidate) and a modelling claim (a small wallet might see this much). The
first is checkable, so it is tested strictly. The second is an assumption stack, so
these tests pin its *shape* - monotonic, bounded, conservative, and honest that a
small deposit earns less than a large one.
"""

import math
import re
import time
import unittest
from pathlib import Path

import airdrop_scanner as radar


REPO = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def _protocol(**overrides):
    base = {
        "name": "Testnet Protocol", "slug": "testnet-protocol",
        "symbol": "-", "gecko_id": None, "cmcId": None,
        "category": "Dexs", "chain": "Base", "chains": ["Base", "Arbitrum"],
        "tvl": 50_000_000.0, "audits": "2", "url": "https://example.org",
        "twitter": "example", "listedAt": NOW - 400 * 24 * 3600,
        "change_7d": 3.0,
    }
    base.update(overrides)
    return base


class TestCandidateFiltering(unittest.TestCase):
    def test_a_protocol_with_a_live_token_is_not_a_candidate(self):
        for tokened in ({"symbol": "UNI"}, {"gecko_id": "uniswap"}, {"cmcId": "7083"}):
            with self.subTest(tokened=tokened):
                self.assertFalse(radar.is_tokenless(_protocol(**tokened)))
                self.assertFalse(radar.is_eligible(_protocol(**tokened)))

    def test_a_tokenless_protocol_with_real_tvl_is_a_candidate(self):
        self.assertTrue(radar.is_tokenless(_protocol()))
        self.assertTrue(radar.is_eligible(_protocol()))

    def test_dust_tvl_is_rejected(self):
        self.assertFalse(radar.is_eligible(_protocol(tvl=100_000.0)))

    def test_categories_whose_users_are_customers_are_rejected(self):
        for category in ("CEX", "Bridge", "Canonical Bridge", "Payments",
                         "OTC Marketplace", "Liquid Staking"):
            with self.subTest(category=category):
                self.assertFalse(radar.is_eligible(_protocol(category=category)))

    def test_prediction_markets_and_restaking_remain_eligible(self):
        """Polymarket and Symbiotic are among the most credible open candidates."""
        for category in ("Prediction Market", "Collateral Management",
                         "Restaking", "Dexs", "Lending", "Derivatives"):
            with self.subTest(category=category):
                self.assertTrue(radar.is_eligible(_protocol(category=category)))


class TestLegitimacyScore(unittest.TestCase):
    def test_audits_tvl_and_age_all_raise_the_score(self):
        weak = _protocol(audits="0", tvl=2_500_000.0,
                         listedAt=NOW - 10 * 24 * 3600, chains=["Base"],
                         url="", twitter="")
        strong = _protocol()
        weak_score, _ = radar.legitimacy_score(weak, NOW)
        strong_score, _ = radar.legitimacy_score(strong, NOW)
        self.assertLess(weak_score, strong_score)
        self.assertLess(weak_score, radar.BAND_SPECULATIVE)

    def test_score_is_bounded_to_0_100(self):
        huge = _protocol(tvl=1e12, audits="9", listedAt=0.0)
        score, _ = radar.legitimacy_score(huge, NOW)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_a_tvl_spike_is_penalised_as_mercenary_capital(self):
        calm, _ = radar.legitimacy_score(_protocol(change_7d=5.0), NOW)
        spiked, spike_signals = radar.legitimacy_score(_protocol(change_7d=900.0), NOW)
        self.assertLess(spiked, calm)
        self.assertTrue(any("spiked" in s for s in spike_signals))

    def test_missing_audit_is_reported_not_hidden(self):
        _, signals = radar.legitimacy_score(_protocol(audits="0"), NOW)
        self.assertIn("no published audit", signals)

    def test_bands_are_ordered(self):
        self.assertEqual(radar.risk_band(95), "STRONG")
        self.assertEqual(radar.risk_band(55), "MODERATE")
        self.assertEqual(radar.risk_band(35), "SPECULATIVE")
        self.assertEqual(radar.risk_band(10), "HIGH RISK")


class TestPayoffModel(unittest.TestCase):
    def test_a_bigger_deposit_earns_more_but_sublinearly(self):
        """The logarithmic asymmetry is the whole reason this suits $100."""
        small = radar.deposit_factor(33.0)
        large = radar.deposit_factor(1_000.0)
        self.assertLess(small, large)
        # 30x the money must not buy anywhere near 30x the allocation.
        self.assertGreater(small / large, 1.0 / 8.0)

    def test_deposit_factor_is_bounded(self):
        self.assertEqual(radar.deposit_factor(0.0), 0.0)
        self.assertLessEqual(radar.deposit_factor(10_000_000.0), 1.0)
        self.assertGreaterEqual(radar.deposit_factor(1.0), 0.0)

    def test_probability_never_reaches_certainty(self):
        for score in (0, 50, 100):
            probability = radar.airdrop_probability(score)
            self.assertGreaterEqual(probability, radar.P_AIRDROP_MIN)
            self.assertLessEqual(probability, radar.P_AIRDROP_MAX)
        self.assertLess(radar.P_AIRDROP_MAX, 0.5)

    def test_expected_value_is_discounted_below_the_headline_allocation(self):
        score, _ = radar.legitimacy_score(_protocol(), NOW)
        payoff = radar.expected_value(_protocol(), score, 33.0)
        self.assertLess(payoff["value_if_paid_usd"],
                        payoff["gross_if_it_lands_usd"])
        self.assertLess(payoff["expected_usd"],
                        payoff["value_if_paid_usd"])

    def test_the_reported_outcomes_form_a_coherent_distribution(self):
        """Regression: the odds and the payout must describe the same world.

        Qualification was once applied twice - shrinking the payout AND the odds -
        so the page showed a 34% chance of something beside a 78% chance of nothing,
        summing to 112%. Expected value was right; the numbers a reader would act on
        were not. Qualification is a probability (a filtered wallet gets nothing at
        all), depreciation is a value effect, and each belongs in exactly one place.
        """
        score, _ = radar.legitimacy_score(_protocol(), NOW)
        payoff = radar.expected_value(_protocol(), score, 33.0)
        self.assertAlmostEqual(payoff["p_paid"] + payoff["p_nothing"], 1.0, places=12)
        self.assertAlmostEqual(
            payoff["p_paid"],
            payoff["p_protocol_airdrops"] * radar.QUALIFICATION_RATE, places=12)
        # Payout carries the depreciation haircut and nothing else.
        self.assertAlmostEqual(
            payoff["value_if_paid_usd"],
            payoff["gross_if_it_lands_usd"] * radar.VALUE_RETENTION, places=9)
        # Expected value is the product of the two coherent halves.
        self.assertAlmostEqual(
            payoff["expected_usd"],
            payoff["p_paid"] * payoff["value_if_paid_usd"], places=12)

    def test_qualification_is_not_double_counted(self):
        score, _ = radar.legitimacy_score(_protocol(), NOW)
        payoff = radar.expected_value(_protocol(), score, 33.0)
        naive_double = (payoff["p_protocol_airdrops"]
                        * payoff["gross_if_it_lands_usd"]
                        * radar.VALUE_RETENTION
                        * radar.QUALIFICATION_RATE)
        # The old and new decompositions agree on EV...
        self.assertAlmostEqual(payoff["expected_usd"], naive_double, places=9)
        # ...but the payout itself must NOT carry the qualification haircut.
        self.assertGreater(payoff["value_if_paid_usd"],
                           payoff["gross_if_it_lands_usd"]
                           * radar.VALUE_RETENTION * radar.QUALIFICATION_RATE)

    def test_the_most_likely_single_outcome_is_nothing(self):
        score, _ = radar.legitimacy_score(_protocol(), NOW)
        payoff = radar.expected_value(_protocol(), score, 33.0)
        self.assertGreater(payoff["p_nothing"], 0.5)

    def test_assumptions_are_disclosed_with_the_number(self):
        disclosed = radar.assumptions()
        for key in ("p_airdrop_range", "value_retention", "qualification_rate",
                    "allocation_range_usd", "caveat"):
            self.assertIn(key, disclosed)
        self.assertIn("not measured", disclosed["caveat"].lower())


class TestRankingAndPortfolio(unittest.TestCase):
    def test_ranking_splits_the_bankroll_across_farms(self):
        rows = radar.rank([_protocol()], bankroll_usd=90.0, now=NOW, wallets=3)
        self.assertEqual(rows[0]["expected"]["deposit_usd"], 30.0)

    def test_the_bankroll_is_a_total_not_a_per_farm_amount(self):
        """$100 means $100 across every farm, not $100 into each of them."""
        universe = [_protocol(name=f"p{i}", tvl=10_000_000.0) for i in range(9)]
        rows = radar.rank(universe, bankroll_usd=100.0, now=NOW, wallets=3)
        funded = [row for row in rows if row["funded"]]
        self.assertEqual(len(funded), 3)
        self.assertAlmostEqual(
            sum(row["expected"]["deposit_usd"] for row in funded), 100.0, places=9)

    def test_rows_the_bankroll_cannot_fund_are_flagged(self):
        """Every row is priced at the same deposit, so the expected column sums to
        far more than the bankroll can buy. Unfunded rows must be distinguishable
        or the list reads as an affordable shopping basket."""
        universe = [_protocol(name=f"p{i}", tvl=10_000_000.0) for i in range(9)]
        rows = radar.rank(universe, bankroll_usd=100.0, now=NOW, wallets=3)
        self.assertEqual([row["funded"] for row in rows],
                         [True, True, True, False, False, False, False, False, False])
        affordable = sum(r["expected"]["expected_usd"] for r in rows if r["funded"])
        listed = sum(r["expected"]["expected_usd"] for r in rows)
        self.assertLess(affordable, listed)

    def test_more_wallets_means_a_smaller_deposit_in_each(self):
        universe = [_protocol(name=f"p{i}") for i in range(6)]
        one = radar.rank(universe, bankroll_usd=100.0, now=NOW, wallets=1)
        five = radar.rank(universe, bankroll_usd=100.0, now=NOW, wallets=5)
        self.assertEqual(one[0]["expected"]["deposit_usd"], 100.0)
        self.assertEqual(five[0]["expected"]["deposit_usd"], 20.0)
        # Spreading the same money thinner lowers each farm's expected value.
        self.assertGreater(one[0]["expected"]["expected_usd"],
                           five[0]["expected"]["expected_usd"])

    def test_ranking_drops_ineligible_and_malformed_entries(self):
        rows = radar.rank(
            [_protocol(), _protocol(symbol="AAA"), _protocol(category="CEX"),
             "not a dict", None],
            bankroll_usd=100.0, now=NOW)
        self.assertEqual(len(rows), 1)

    def test_rows_are_ordered_most_reliable_first(self):
        rows = radar.rank(
            [_protocol(name="shaky", tvl=400_000_000.0, audits="0",
                       listedAt=NOW - 5 * 24 * 3600, url="", twitter="",
                       chains=["Base"]),
             _protocol(name="solid", tvl=20_000_000.0)],
            bankroll_usd=100.0, now=NOW)
        # The shaky protocol is 20x larger, but evidence quality leads the ranking.
        self.assertEqual([r["name"] for r in rows], ["solid", "shaky"])
        self.assertGreater(rows[0]["score"], rows[1]["score"])

    def test_scores_descend_across_the_whole_ranking(self):
        universe = [_protocol(name=f"p{i}", tvl=2_000_000.0 * (i + 1),
                              audits=str(i % 3), listedAt=NOW - i * 30 * 24 * 3600)
                    for i in range(12)]
        scores = [r["score"] for r in radar.rank(universe, 100.0, now=NOW)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_expected_value_breaks_ties_within_a_band(self):
        rows = radar.rank([_protocol(name="a", tvl=10_000_000.0),
                           _protocol(name="b", tvl=10_000_000.0)],
                          bankroll_usd=100.0, now=NOW)
        self.assertEqual(rows[0]["score"], rows[1]["score"])
        self.assertGreaterEqual(rows[0]["expected"]["expected_usd"],
                                rows[1]["expected"]["expected_usd"])

    def test_an_empty_universe_yields_an_empty_plan(self):
        plan = radar.build_plan([], 100.0)
        self.assertEqual(plan["farms"], 0)
        self.assertFalse(plan["affordable"])


class TestCostModel(unittest.TestCase):
    def test_a_multichain_protocol_is_costed_on_its_cheapest_chain(self):
        chain, gas = radar.cheapest_chain(
            _protocol(chains=["Ethereum", "Base", "Arbitrum"]))
        self.assertEqual(chain, "Base")
        self.assertLess(gas, radar.gas_budget_usd("Ethereum"))

    def test_an_ethereum_only_protocol_pays_ethereum_gas(self):
        chain, gas = radar.cheapest_chain(_protocol(chains=["Ethereum"]))
        self.assertEqual(chain, "Ethereum")
        self.assertEqual(gas, radar.CHAIN_GAS_BUDGET_USD["Ethereum"])

    def test_an_unknown_chain_gets_a_mid_range_estimate(self):
        chain, gas = radar.cheapest_chain(_protocol(chains=["SomeNewL2"]))
        self.assertEqual(gas, radar.DEFAULT_GAS_BUDGET_USD)
        self.assertEqual(chain, "SomeNewL2")

    def test_cost_separates_recoverable_deposit_from_spent_gas(self):
        cost = radar.cost_to_farm(_protocol(chains=["Base"]), 60.0)
        self.assertEqual(cost["deposit_usd"], 60.0)
        self.assertEqual(cost["recoverable_usd"], 60.0)
        self.assertEqual(cost["spent_usd"], cost["gas_usd"])
        self.assertAlmostEqual(cost["total_usd"],
                               cost["deposit_usd"] + cost["gas_usd"], places=9)

    def test_a_deposit_below_the_threshold_is_raised_to_it(self):
        cost = radar.cost_to_farm(_protocol(chains=["Base"]), 5.0)
        self.assertEqual(cost["deposit_usd"], radar.MIN_MEANINGFUL_DEPOSIT_USD)


class TestPlanner(unittest.TestCase):
    def _universe(self, count=20, chain="Base"):
        return [_protocol(name=f"p{i}", chains=[chain],
                          tvl=50_000_000.0 - i * 1_000_000.0) for i in range(count)]

    def test_an_amount_below_the_cheapest_entry_is_refused(self):
        rows = radar.rank(self._universe(), 5.0, now=NOW)
        plan = radar.build_plan(rows, 5.0)
        self.assertFalse(plan["affordable"])
        self.assertEqual(plan["farms"], 0)
        self.assertGreater(plan["shortfall_usd"], 0.0)
        self.assertGreater(plan["cheapest_entry_usd"], 5.0)

    def test_more_money_funds_more_farms(self):
        counts = []
        for amount in (30.0, 100.0, 200.0):
            rows = radar.rank(self._universe(), amount, now=NOW)
            counts.append(radar.build_plan(rows, amount)["farms"])
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], counts[-1])

    def test_the_plan_never_spends_more_than_the_amount(self):
        for amount in (30.0, 75.0, 150.0, 400.0, 5_000.0):
            rows = radar.rank(self._universe(), amount, now=NOW)
            plan = radar.build_plan(rows, amount)
            if not plan["affordable"]:
                continue
            committed = sum(r["cost"]["total_usd"] for r in plan["rows"])
            self.assertLessEqual(committed, amount + 1e-6,
                                 f"plan overspends at ${amount}")

    def test_farms_are_capped_by_time_not_only_money(self):
        rows = radar.rank(self._universe(40), 100_000.0, now=NOW)
        plan = radar.build_plan(rows, 100_000.0)
        self.assertEqual(plan["farms"], radar.MAX_PRACTICAL_FARMS)
        self.assertTrue(plan["capped_by_time"])
        self.assertIn("your time", plan["cap_note"])

    def test_the_steps_quote_the_same_deposit_the_plan_assigns(self):
        """Regression: steps were built from the whole amount, so a $100 plan of
        three farms told the reader to deposit $100 into each one."""
        rows = radar.rank(self._universe(), 100.0, now=NOW)
        plan = radar.build_plan(rows, 100.0)
        for row in plan["rows"]:
            deposit = row["cost"]["deposit_usd"]
            steps = " ".join(row["instructions"])
            self.assertIn(f"${deposit:,.2f} deposited", steps)
            self.assertNotIn("$100.00 deposited", steps)

    def test_only_gas_is_unrecoverable(self):
        rows = radar.rank(self._universe(), 200.0, now=NOW)
        plan = radar.build_plan(rows, 200.0)
        self.assertAlmostEqual(plan["recoverable_usd"],
                               200.0 - plan["gas_total_usd"], places=9)
        self.assertLess(plan["gas_total_usd"], 200.0)

    def test_expected_total_builds_on_the_recoverable_capital(self):
        rows = radar.rank(self._universe(), 200.0, now=NOW)
        plan = radar.build_plan(rows, 200.0)
        self.assertAlmostEqual(
            plan["expected_total_usd"],
            plan["recoverable_usd"] + plan["expected_airdrop_usd"], places=9)

    def test_probability_that_any_pays_rises_with_more_farms(self):
        small = radar.build_plan(radar.rank(self._universe(), 30.0, now=NOW), 30.0)
        big = radar.build_plan(radar.rank(self._universe(), 300.0, now=NOW), 300.0)
        self.assertGreater(big["probability_any_pays"], small["probability_any_pays"])
        for plan in (small, big):
            self.assertAlmostEqual(
                plan["probability_any_pays"] + plan["probability_of_nothing"],
                1.0, places=12)


class TestTimingHonesty(unittest.TestCase):
    def test_no_deadline_is_ever_invented(self):
        for months in (1.0, 8.0, 18.0, 40.0):
            timing = radar.timing_view(months)
            self.assertIsNone(timing["deadline"])
            self.assertIn("No end date exists", timing["deadline_note"])

    def test_stage_tracks_how_long_a_protocol_has_gone_without_a_token(self):
        self.assertEqual(radar.timing_view(1.0)["stage"], "early")
        self.assertEqual(radar.timing_view(8.0)["stage"], "building")
        self.assertEqual(radar.timing_view(18.0)["stage"], "mature")
        self.assertEqual(radar.timing_view(40.0)["stage"], "long overdue")


class TestProbabilityMath(unittest.TestCase):
    def test_the_formulas_reproduce_the_reported_numbers(self):
        score, _ = radar.legitimacy_score(_protocol(), NOW)
        math_view = radar.probability_math(score)
        payoff = radar.expected_value(_protocol(), score, 33.0)
        self.assertIn(f"{payoff['p_protocol_airdrops']:.3f}",
                      math_view["formula_drops"])
        self.assertIn(f"{payoff['p_paid']:.3f}", math_view["formula_paid"])

    def test_the_weakest_assumption_is_named(self):
        math_view = radar.probability_math(80.0)
        self.assertIn("assumption", math_view["caveat"].lower())
        self.assertIn("weakest", math_view["caveat"].lower())


class TestInstructions(unittest.TestCase):
    def test_phishing_and_sybil_warnings_are_always_present(self):
        steps = " ".join(radar.instructions(_protocol(), 100.0))
        self.assertIn("phishing", steps.lower())
        self.assertIn("seed phrase", steps.lower())
        self.assertIn("ONE wallet", steps)

    def test_the_official_url_is_quoted_so_users_do_not_search_for_it(self):
        steps = " ".join(radar.instructions(_protocol(url="https://example.org"), 100.0))
        self.assertIn("https://example.org", steps)

    def test_an_unaudited_protocol_gets_an_explicit_capital_warning(self):
        steps = " ".join(radar.instructions(_protocol(audits="0"), 100.0))
        self.assertIn("No audit is published", steps)

    def test_steps_quote_the_exact_cost_split_into_recoverable_and_spent(self):
        steps = " ".join(radar.instructions(_protocol(chains=["Base"]), 40.0))
        self.assertIn("$40.00 deposited (you get this back)", steps)
        self.assertIn("of gas (you do not)", steps)

    def test_steps_name_the_cheapest_chain_and_the_cost_of_ignoring_it(self):
        steps = " ".join(
            radar.instructions(_protocol(chains=["Ethereum", "Base"]), 40.0))
        self.assertIn("Farm it on Base", steps)
        self.assertIn("using Ethereum instead would burn", steps)

    def test_steps_state_there_is_no_deadline_to_race(self):
        steps = " ".join(radar.instructions(_protocol(), 40.0))
        self.assertIn("no deadline to race", steps)

    def test_steps_are_tailored_to_what_the_protocol_actually_does(self):
        lending = " ".join(radar.instructions(_protocol(category="Lending"), 40.0))
        dex = " ".join(radar.instructions(_protocol(category="Dexs"), 40.0))
        self.assertIn("supply an asset", lending)
        self.assertIn("add liquidity", dex)
        self.assertNotEqual(lending, dex)


class TestRadarCache(unittest.TestCase):
    def test_state_is_serviceable_before_the_first_scan(self):
        radar_instance = radar.AirdropRadar(100.0)
        state = radar_instance.state()
        self.assertTrue(state["running"])
        self.assertEqual(state["rows"], [])
        self.assertIn("plan", state)
        self.assertIn("assumptions", state)

    def test_bankroll_can_be_repriced_without_refetching(self):
        radar_instance = radar.AirdropRadar(100.0)
        radar_instance._universe = [_protocol(), _protocol(name="b"),
                                    _protocol(name="c")]
        radar_instance._reprice()
        at_100 = radar_instance.state()["plan"]

        radar_instance.set_bankroll(1_000.0)
        state = radar_instance.state()
        self.assertEqual(state["bankroll_usd"], 1_000.0)
        # More money funds more farms and a larger expected airdrop...
        self.assertGreaterEqual(state["plan"]["farms"], at_100["farms"])
        self.assertGreater(state["plan"]["expected_airdrop_usd"],
                           at_100["expected_airdrop_usd"])
        # ...but the deposit in each is what scales, and allocation is logarithmic,
        # so the return per dollar invested falls. That asymmetry is the whole
        # reason this strategy suits a small amount.
        big = state["plan"]["expected_airdrop_usd"] / state["plan"]["amount_usd"]
        small = at_100["expected_airdrop_usd"] / at_100["amount_usd"]
        self.assertLess(big, small)

    def test_a_nonsense_bankroll_falls_back_to_the_default(self):
        radar_instance = radar.AirdropRadar(100.0)
        for bad in (0, -50, "abc", None):
            self.assertEqual(radar_instance.set_bankroll(bad)["bankroll_usd"], 100.0)


class TestDashboardIntegration(unittest.TestCase):
    """Assert against the specific page constant, never the whole file.

    An earlier revision searched dashboard.py as one blob and passed while the button
    CSS had actually been inserted into the Tree of Alpha page, because
    'PAGE_HTML = r\"\"\"<!doctype html>' is a substring of 'TOA_PAGE_HTML = ...'.
    Extracting each literal by name is what makes these tests able to fail.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        source = (REPO / "dashboard.py").read_text(encoding="utf-8")
        cls.SOURCE = source
        cls.PAGES = {}
        for node in ast.parse(source).body:
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        cls.PAGES[target.id] = node.value.value

    def test_radar_is_constructed_and_looped(self):
        self.assertIn("import airdrop_scanner", self.SOURCE)
        self.assertIn("AIRDROPS = airdrop_scanner.AirdropRadar", self.SOURCE)
        self.assertIn("asyncio.create_task(AIRDROPS.manage_loop())", self.SOURCE)

    def test_endpoints_and_page_route_are_registered(self):
        for route in ('web.get("/airdrops", _airdrops_page)',
                      'web.get("/api/airdrops/state"',
                      'web.post("/api/airdrops/bankroll"',
                      'web.post("/api/airdrops/refresh"'):
            self.assertIn(route, self.SOURCE)

    def test_golden_button_is_on_the_main_page_and_links_to_airdrops(self):
        main = self.PAGES["PAGE_HTML"]
        self.assertIn('class="airdrop-btn" href="/airdrops"', main)
        self.assertIn("Airdrops</a>", main)

    def test_the_main_page_carries_the_gold_animation_css_itself(self):
        main = self.PAGES["PAGE_HTML"]
        for token in ("#fde68a", "#f59e0b", "@keyframes airdropSweep",
                      "@keyframes airdropGlow", "@keyframes airdropSheen",
                      "prefers-reduced-motion"):
            self.assertIn(token, main, f"{token} missing from the main page")

    def test_the_paper_page_no_longer_carries_any_airdrop_ui(self):
        paper = self.PAGES["PAPER_HTML"]
        for token in ("airdrop-btn", "airdrops-panel", "loadAirdrops",
                      "airdropSweep", "airdrop-featured"):
            self.assertNotIn(token, paper, f"{token} still on the paper page")

    def test_the_css_did_not_leak_into_another_page(self):
        for name, html in self.PAGES.items():
            if name in ("PAGE_HTML", "AIRDROPS_HTML"):
                continue
            self.assertNotIn("airdropSweep", html,
                             f"airdrop CSS leaked into {name}")

    def test_the_page_marks_which_rows_the_bankroll_actually_funds(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn("airdrops you can farm", page)
        self.assertIn("budget does not reach", page)
        self.assertIn("Not enough to start", page)

    def test_the_airdrops_page_ranks_by_reliability_and_discloses_risk(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn("candidates, not confirmed airdrops", page)
        self.assertIn("chance nothing pays", page)
        self.assertIn("How these numbers are built", page)
        self.assertIn("/api/airdrops/state", page)

    def test_the_airdrops_page_links_back_and_cannot_hijack_the_opener(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn('href="/"', page)
        # Every outbound protocol link on this page opens a new tab, so each one
        # must sever the opener reference.
        outbound = re.findall(r'target="_blank"[^>]*', page)
        self.assertGreaterEqual(len(outbound), 2)
        for tag in outbound:
            self.assertIn('rel="noopener noreferrer"', tag)


class TestNoTradingCapability(unittest.TestCase):
    SOURCE = (REPO / "airdrop_scanner.py").read_text(encoding="utf-8")

    def test_module_is_read_only(self):
        for verb in ("session.post", "session.put", "session.delete", ".post("):
            self.assertNotIn(verb, self.SOURCE)
        self.assertEqual(self.SOURCE.count("session.get("), 1)

    def test_no_credential_or_wallet_machinery(self):
        lowered = self.SOURCE.lower()
        for token in ("api_key", "apikey", "secret", "private_key", "mnemonic",
                      "web3", "eth_send", "getenv", "environ"):
            self.assertNotIn(token, lowered)

    def test_only_public_defillama_is_contacted(self):
        for url in re.findall(r"https?://[^\s\"']+", self.SOURCE):
            if "example.org" in url:
                continue
            self.assertTrue(url.startswith("https://api.llama.fi"),
                            f"unexpected endpoint {url}")


if __name__ == "__main__":
    unittest.main()
