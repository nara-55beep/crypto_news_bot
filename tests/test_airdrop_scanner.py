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
        self.assertLess(payoff["realistic_if_it_lands_usd"],
                        payoff["gross_if_it_lands_usd"])
        self.assertLess(payoff["expected_usd"],
                        payoff["realistic_if_it_lands_usd"])

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

    def test_portfolio_treats_the_bankroll_as_deposited_not_spent(self):
        rows = radar.rank([_protocol()], bankroll_usd=100.0, now=NOW)
        view = radar.portfolio_view(rows, 100.0, wallets=1)
        self.assertAlmostEqual(view["expected_total_usd"],
                               100.0 + view["expected_airdrop_usd"], places=9)
        self.assertGreater(view["expected_multiple"], 1.0)
        self.assertIn("withdrawable", view["capital_note"])

    def test_portfolio_states_a_real_chance_of_nothing(self):
        rows = radar.rank([_protocol(), _protocol(name="b"), _protocol(name="c")],
                          bankroll_usd=100.0, now=NOW)
        view = radar.portfolio_view(rows, 100.0, wallets=3)
        self.assertGreater(view["probability_of_nothing"], 0.2)
        self.assertLess(view["probability_of_nothing"], 1.0)

    def test_an_empty_universe_yields_an_empty_plan(self):
        view = radar.portfolio_view([], 100.0)
        self.assertEqual(view["farms"], 0)
        self.assertEqual(view["expected_airdrop_usd"], 0.0)
        self.assertEqual(view["probability_of_nothing"], 1.0)


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

    def test_deposit_guidance_scales_with_the_bankroll(self):
        small = " ".join(radar.instructions(_protocol(), 100.0))
        self.assertIn("$33", small)


class TestRadarCache(unittest.TestCase):
    def test_state_is_serviceable_before_the_first_scan(self):
        radar_instance = radar.AirdropRadar(100.0)
        state = radar_instance.state()
        self.assertTrue(state["running"])
        self.assertEqual(state["rows"], [])
        self.assertIn("portfolio", state)
        self.assertIn("assumptions", state)

    def test_bankroll_can_be_repriced_without_refetching(self):
        radar_instance = radar.AirdropRadar(100.0)
        radar_instance._universe = [_protocol(), _protocol(name="b"),
                                    _protocol(name="c")]
        radar_instance._reprice()
        at_100 = radar_instance.state()["portfolio"]["expected_airdrop_usd"]

        radar_instance.set_bankroll(1_000.0)
        state = radar_instance.state()
        self.assertEqual(state["bankroll_usd"], 1_000.0)
        self.assertGreater(state["portfolio"]["expected_airdrop_usd"], at_100)
        # More money buys a bigger drop but a WORSE multiple - the whole reason
        # this strategy suits a small bankroll.
        self.assertLess(state["portfolio"]["expected_multiple"],
                        radar.portfolio_view(
                            radar.rank(radar_instance._universe, 100.0),
                            100.0)["expected_multiple"])

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

    def test_the_airdrops_page_ranks_by_reliability_and_discloses_risk(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn("Ranked most reliable", page)
        self.assertIn("candidates, not confirmed airdrops", page)
        self.assertIn("chance of nothing", page)
        self.assertIn("How these numbers are built", page)
        self.assertIn("/api/airdrops/state", page)

    def test_the_airdrops_page_links_back_and_cannot_hijack_the_opener(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn('href="/"', page)
        marker = 'class="pname" href="'
        self.assertIn(marker, page)
        self.assertIn('rel="noopener noreferrer"',
                      page[page.index(marker):][:240])


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
