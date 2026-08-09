"""Tests for the confirmed-airdrop list.

This module makes factual claims about real projects, so the tests guard the two ways
such a list goes wrong: presenting a hand-checked snapshot as if it were live data, and
letting "confirmed" quietly imply "guaranteed" or "still available".
"""

import ast
import re
import unittest
from pathlib import Path

import confirmed_airdrops as confirmed


REPO = Path(__file__).resolve().parents[1]
DAY = 86_400.0


class TestEntryIntegrity(unittest.TestCase):
    def test_every_entry_is_fully_specified(self):
        for entry in confirmed.CONFIRMED_AIRDROPS:
            with self.subTest(name=entry.get("name")):
                for field in ("name", "token", "status", "chain", "tge_window",
                              "confirmed_by", "source_url", "why_it_matters", "risk"):
                    self.assertTrue(entry.get(field), f"{field} missing or empty")
                self.assertIn(entry["status"],
                              (confirmed.OPEN, confirmed.PENDING, confirmed.DISTRIBUTED))

    def test_every_claim_carries_a_checkable_source(self):
        """A factual claim about someone else's project needs a link, not a vibe."""
        for entry in confirmed.CONFIRMED_AIRDROPS:
            with self.subTest(name=entry["name"]):
                self.assertTrue(entry["source_url"].startswith("https://"))
                self.assertIn(".", entry["source_url"])

    def test_distributed_drops_are_not_presented_as_farmable(self):
        for entry in confirmed.by_status(confirmed.DISTRIBUTED):
            with self.subTest(name=entry["name"]):
                self.assertTrue(entry["snapshot_taken"])
                self.assertEqual(entry["qualify"], [])
                self.assertNotIn(entry, confirmed.farmable())

    def test_open_drops_have_an_actionable_path(self):
        for entry in confirmed.by_status(confirmed.OPEN):
            with self.subTest(name=entry["name"]):
                self.assertFalse(entry["snapshot_taken"])
                self.assertGreaterEqual(len(entry["qualify"]), 1)

    def test_lighter_is_recorded_as_already_gone(self):
        """The user's own example, and the reason the whole distinction exists."""
        lighter = next(e for e in confirmed.CONFIRMED_AIRDROPS if e["name"] == "Lighter")
        self.assertEqual(lighter["status"], confirmed.DISTRIBUTED)
        self.assertTrue(lighter["snapshot_taken"])
        self.assertIn("2025", lighter["tge_window"])
        self.assertNotIn(lighter, confirmed.farmable())


class TestFarmableFilter(unittest.TestCase):
    def test_farmable_is_open_and_not_snapshotted(self):
        for entry in confirmed.farmable():
            self.assertEqual(entry["status"], confirmed.OPEN)
            self.assertFalse(entry["snapshot_taken"])

    def test_farmable_is_a_strict_subset_of_the_list(self):
        self.assertLess(len(confirmed.farmable()), len(confirmed.CONFIRMED_AIRDROPS))
        self.assertGreater(len(confirmed.farmable()), 0)

    def test_overview_partitions_every_entry(self):
        view = confirmed.overview()
        total = len(view["open"]) + len(view["pending"]) + len(view["distributed"])
        self.assertEqual(total, len(confirmed.CONFIRMED_AIRDROPS))
        self.assertEqual(view["farmable_count"], len(confirmed.farmable()))


class TestHonesty(unittest.TestCase):
    def test_staleness_is_reported_not_hidden(self):
        fresh = confirmed.staleness(confirmed.VERIFIED_AT + 1 * DAY)
        old = confirmed.staleness(
            confirmed.VERIFIED_AT + (confirmed.STALE_AFTER_DAYS + 5) * DAY)
        self.assertFalse(fresh["stale"])
        self.assertTrue(old["stale"])
        self.assertGreater(old["days_old"], fresh["days_old"])
        self.assertIn("not a live feed", fresh["note"])

    def test_the_list_never_claims_a_guaranteed_payout(self):
        view = confirmed.overview()
        self.assertIn("not a confirmed payment", view["caveat"])
        self.assertIn("100%", view["caveat"])
        blob = " ".join(str(v) for e in confirmed.CONFIRMED_AIRDROPS
                        for v in e.values()).lower()
        # Match affirmative guarantees only. The bare word "guaranteed" is allowed
        # because it appears inside warnings ("expected rather than guaranteed"),
        # and banning it would push the copy toward vaguer, less honest phrasing.
        for pattern in (r"\bis guaranteed", r"\bare guaranteed", r"\bguaranteed (payout|return|allocation)",
                        r"\brisk[- ]free", r"\bsure thing", r"\b100% chance",
                        r"\bcertain to (pay|land|drop)", r"\bcan'?t lose"):
            self.assertIsNone(re.search(pattern, blob),
                              f"affirmative guarantee matched {pattern!r}")
        # And the warnings that use the word must actually be negating it.
        for match in re.finditer(r".{40}guaranteed", blob):
            self.assertRegex(match.group(0), r"(not|rather than|never|isn'?t)\s+\S*\s*$|"
                                             r"(not|rather than|never|isn'?t)[^.]*guaranteed")

    def test_the_confirmed_versus_farmable_tension_is_stated(self):
        lesson = confirmed.overview()["lesson"]
        self.assertIn("mutually exclusive", lesson)
        self.assertIn("Lighter", lesson)


class TestDashboardIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def test_the_confirmed_overview_is_served_with_the_state(self):
        self.assertIn("import confirmed_airdrops", self.SOURCE)
        self.assertIn('"confirmed": confirmed_airdrops.overview()', self.SOURCE)

    def test_confirmed_drops_render_above_the_speculative_plan(self):
        page = self.PAGES["AIRDROPS_HTML"]
        self.assertIn("Confirmed by the team", page)
        self.assertIn("Speculative candidates", page)
        self.assertLess(page.index("Confirmed by the team"),
                        page.index("Speculative candidates"))

    def test_the_page_shows_status_source_and_staleness(self):
        page = self.PAGES["AIRDROPS_HTML"]
        for token in ("st-OPEN", "st-PENDING", "st-DISTRIBUTED",
                      "Confirmed by:", "re-verify each source", "conf gone"):
            self.assertIn(token, page.replace("'+(gone?' gone':'')+'", " gone"))

    def test_source_links_cannot_hijack_the_opener(self):
        page = self.PAGES["AIRDROPS_HTML"]
        for tag in re.findall(r'target="_blank"[^>]*', page):
            self.assertIn('rel="noopener noreferrer"', tag)


class TestNoTradingCapability(unittest.TestCase):
    SOURCE = (REPO / "confirmed_airdrops.py").read_text(encoding="utf-8")

    def test_module_is_inert_data_only(self):
        for token in ("requests", "aiohttp", "urllib", "session.", "private_key",
                      "api_key", "secret", "eth_send"):
            self.assertNotIn(token, self.SOURCE.lower())


if __name__ == "__main__":
    unittest.main()
