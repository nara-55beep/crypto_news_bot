import unittest
from html.parser import HTMLParser

from penny_page import PENNY_HTML


class _IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])


class TestPennyPage(unittest.TestCase):
    def test_page_has_no_duplicate_element_ids(self):
        parser = _IdCollector()
        parser.feed(PENNY_HTML)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_tables_are_rendered_inside_horizontal_scroll_shells(self):
        self.assertIn('class="table-wrap"', PENNY_HTML)
        self.assertIn("overflow-x:auto", PENNY_HTML)
        self.assertIn("max-width:100%", PENNY_HTML)
        self.assertIn("table.wide{min-width:1220px}", PENNY_HTML)

    def test_layout_has_desktop_tablet_and_phone_breakpoints(self):
        self.assertIn("@media(max-width:1240px)", PENNY_HTML)
        self.assertIn("@media(max-width:860px)", PENNY_HTML)
        self.assertIn("@media(max-width:520px)", PENNY_HTML)
        self.assertIn("grid-template-columns:1fr", PENNY_HTML)
        self.assertIn("grid-template-columns:minmax(0,1fr)", PENNY_HTML)
        self.assertIn(".actions{display:grid", PENNY_HTML)
        self.assertIn("width:100%;max-width:100%", PENNY_HTML)

    def test_zero_outcomes_are_explained_as_collection_not_failure(self):
        self.assertIn("Waiting for outcomes", PENNY_HTML)
        self.assertIn("Power needs completed returns", PENNY_HTML)
        self.assertIn("repeated scans are not independent evidence", PENNY_HTML)
        self.assertNotIn("fp.status||'not assessable'", PENNY_HTML)

    def test_evidence_progress_is_visible_before_diagnostic_tables(self):
        self.assertIn('id="evidence"', PENNY_HTML)
        self.assertIn("Forward strategy evidence", PENNY_HTML)
        self.assertIn("Incremental AI value", PENNY_HTML)
        self.assertIn("completed_signal_days_required", PENNY_HTML)

    def test_market_wide_coverage_is_visible_and_honest_about_deep_scoring(self):
        self.assertIn('id="universe"', PENNY_HTML)
        self.assertIn("Every active tradable non-OTC U.S. equity", PENNY_HTML)
        self.assertIn("Continuous all-symbol scanner", PENNY_HTML)
        self.assertIn("continuous_target_sec||30", PENNY_HTML)
        self.assertIn("Universe passes", PENNY_HTML)
        self.assertIn("Deep dossiers", PENNY_HTML)
        self.assertIn("OTC unavailable", PENNY_HTML)
        self.assertIn("Next deep scan", PENNY_HTML)
        self.assertIn("SEC 8-K feed", PENNY_HTML)
        self.assertIn("sec.cache_age_sec", PENNY_HTML)
        self.assertIn("transient SEC failure keeps the last known filings", PENNY_HTML)

    def test_deep_scan_countdown_ticks_locally_between_server_refreshes(self):
        self.assertIn('id="next-deep-scan"', PENNY_HTML)
        self.assertIn("next_scan_at", PENNY_HTML)
        self.assertIn("performance.now()", PENNY_HTML)
        self.assertIn("setInterval(paintDeepScanClock,250)", PENNY_HTML)
        self.assertIn("s.deep_scan_batch_size", PENNY_HTML)

    def test_page_contains_no_common_utf8_mojibake(self):
        self.assertNotIn("â", PENNY_HTML)
        self.assertNotIn("Â", PENNY_HTML)


if __name__ == "__main__":
    unittest.main()
