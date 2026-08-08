import json
import os
import tempfile
import unittest
from unittest import mock

import pennystock_bot as research
import pennystock_paper as paper

# Redirect the append-only archive for the WHOLE module. Per-test patching missed the
# tests that build a bot through other helpers, and five TEST rows reached the real
# evidence archive before anyone noticed. A module-level redirect cannot be forgotten.
_ARCHIVE_TMP = tempfile.mkdtemp(prefix="penny-test-archive-")
paper.SIGNAL_ARCHIVE_PATH = os.path.join(_ARCHIVE_TMP, "archive.jsonl")
from research import penny_exit_structure as exit_structure
from research import penny_stats as stats


def isolated_bot(tmpdir):
    """A bot whose state AND append-only archive are both redirected.

    Patching STATE_PATH alone let tests append TEST rows into the production evidence
    archive - five of them reached it before this was noticed. Anything that writes must
    be redirected, not just the file a test happens to think about.
    """
    with mock.patch.object(paper, "STATE_PATH", os.path.join(tmpdir, "state.json")),          mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH",
                           os.path.join(tmpdir, "archive.jsonl")):
        return paper.PennyStockPaperBot()


def complete_dossier(**changes):
    values = dict(
        ticker="TEST", price=2.0, change_pct=12.0,
        avg_volume=2_000_000, volume=6_000_000, volume_surge=3.0,
        market_cap=80_000_000, float_shares=20_000_000,
        cash=20_000_000, debt=2_000_000, revenue=40_000_000,
        op_cashflow=-5_000_000, runway_quarters=16.0,
        cash_known=True, debt_known=True, revenue_known=True,
        op_cashflow_known=True, runway_known=True,
        shares_change_known=True, shares_change_pct=0.0,
        data_completeness=100.0, exchange="NMS", quote_type="EQUITY",
        market_state="REGULAR", bid=1.99, ask=2.01,
        spread_pct=1.0, spread_reliable=True, quote_age_min=1.0,
        technical_known=True, gap_pct=5.0, from_open_pct=6.0,
        close_location=0.85, return_5d_pct=10.0, return_20d_pct=15.0,
        sma20_distance_pct=8.0, high20_distance_pct=-1.0, atr_pct=8.0,
        fresh_news_count=2, latest_news_age_hours=5.0,
        news=[{
            "title": "Company reports a material operating update",
            "publisher": "Independent News", "when": "2026-08-08T14:00:00Z",
            "age_hours": 5.0,
        }],
        recent_filings=[{
            "date": "2026-08-08", "accepted_at": "2026-08-08T14:30:00Z",
            "type": "8-K", "title": "TEST", "age_hours": 4.5,
            "age_days": 0.2, "items": ["8.01"],
            "accessionNumber": "0000000000-26-000001", "official_sec": True,
        }],
        sec_8k_verified=True, latest_sec_8k_age_hours=4.5,
        recent_8k_items=["8.01"], adverse_8k_items=[],
    )
    values.update(changes)
    return research.Dossier(**values)


class PennyStockLogicTests(unittest.TestCase):
    def test_missing_fundamentals_are_not_scored_as_good(self):
        missing = complete_dossier(
            cash_known=False, debt_known=False, revenue_known=False,
            op_cashflow_known=False, runway_known=False,
            shares_change_known=False, data_completeness=0.0,
        )
        known = complete_dossier()
        missing_score, reasons = research.quality_score(missing)
        known_score, _ = research.quality_score(known)
        self.assertLess(missing_score, known_score)
        self.assertTrue(any("unknown" in x for x in reasons))
        self.assertIn("insufficient fundamental data", research.hard_risk_reason(missing))

    def test_stale_book_is_proxy_but_live_book_is_respected(self):
        closed = complete_dossier(
            market_state="CLOSED", spread_reliable=False,
            spread_pct=38.0, avg_volume=20_000_000,
        )
        spread, estimated = research.effective_spread(closed)
        self.assertTrue(estimated)
        self.assertLess(spread, 2.0)

        # wide but economically possible for a $4M-ADV name: take it at face value
        live = complete_dossier(spread_pct=4.5, spread_reliable=True)
        spread, estimated = research.effective_spread(live)
        self.assertFalse(estimated)
        self.assertEqual(spread, 4.5)
        self.assertIn("live spread", research.hard_risk_reason(live))

    def test_quote_contradicting_the_name_liquidity_is_treated_as_an_artifact(self):
        """A 38% book on a name doing $4M/day is a bad tick, not a real cost.

        Trusting it multiplied the composite by 0.3 and buried the strongest setups
        (see effective_spread). The fallback must be the ADV proxy AND flagged
        estimated, so needs_open_recheck stops anything trading on a guess.
        """
        artifact = complete_dossier(spread_pct=38.0, spread_reliable=True)
        spread, estimated = research.effective_spread(artifact)
        self.assertTrue(estimated)
        self.assertLess(spread, 4.0)
        self.assertFalse(research.trusted_execution_quote(artifact))

        # the rescue must not resurrect a name that is genuinely untradeable
        thin = complete_dossier(
            spread_pct=38.0, spread_reliable=True,
            price=0.5, avg_volume=200_000,
        )
        score, _ = research.tradeability(thin)
        self.assertLess(score, 40.0)

        # bid == ask is a locked market, not a free round trip
        locked = complete_dossier(spread_pct=0.0, spread_reliable=True)
        spread, estimated = research.effective_spread(locked)
        self.assertTrue(estimated)
        self.assertGreater(spread, 0.0)
        self.assertFalse(research.trusted_execution_quote(locked))

    def test_proxy_or_suspect_quote_cannot_reach_paper_fill(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        artifact = complete_dossier(spread_pct=38.0, spread_reliable=True)
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "strategy_id": research.LIVE_STRATEGY_ID,
        }):
            bot._open(
                artifact,
                {"verdict": "SPECULATIVE_BUY", "conviction": "high"},
                {"strategy_id": research.LIVE_STRATEGY_ID, "entry": artifact.price},
                rank=1,
            )
        self.assertEqual(bot.pos, {})
        self.assertTrue(any("book is suspect" in x["msg"] for x in bot.log))

    def test_ai_can_veto_but_never_promote_or_override_avoid(self):
        d = complete_dossier()
        rank = {
            "composite": 80.0, "hype": 75.0, "quality": 75.0,
            "tradeability": 95.0, "technical": 80.0, "catalyst": 70.0,
        }
        no_ai = research.signal_from(d, rank, None)
        self.assertEqual(no_ai["action"], "WATCH")

        avoid = research.signal_from(d, rank, {
            "verdict": "AVOID", "conviction": "high", "score": 99,
        })
        self.assertEqual(avoid["action"], "AVOID")

        unconfirmed = research.signal_from(d, rank, {
            "verdict": "WATCH", "conviction": "high", "score": 99,
        })
        self.assertEqual(unconfirmed["action"], "WATCH")

        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "selected_strategy": research.LIVE_STRATEGY_ID,
            "strategy_id": research.LIVE_STRATEGY_ID,
        }):
            confirmed = research.signal_from(d, rank, {
                "verdict": "SPECULATIVE_BUY", "conviction": "medium", "score": 55,
            })
        self.assertEqual(confirmed["action"], "STRONG BUY")

    def test_failed_edge_audit_converts_buy_to_tracked_research(self):
        d = complete_dossier()
        rank = {
            "composite": 80.0, "hype": 75.0, "quality": 75.0,
            "tradeability": 95.0, "technical": 80.0, "catalyst": 70.0,
        }
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "REJECTED", "auto_trade_allowed": False,
        }):
            signal = research.signal_from(d, rank, {
                "verdict": "SPECULATIVE_BUY", "conviction": "high", "score": 80,
            })
        self.assertEqual(signal["candidate_action"], "STRONG BUY")
        self.assertEqual(signal["action"], "RESEARCH")
        self.assertFalse(signal["auto_trade_allowed"])

    def test_a_validated_different_rule_cannot_authorize_this_live_scanner(self):
        """An earnings-event backtest must not unlock the unrelated live composite."""
        d = complete_dossier()
        rank = {
            "composite": 80.0, "hype": 75.0, "quality": 75.0,
            "tradeability": 95.0, "technical": 80.0, "catalyst": 70.0,
        }
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "selected_strategy": "profitable_earnings_beat",
            "strategy_id": "profitable_earnings_beat",
        }):
            signal = research.signal_from(d, rank, {
                "verdict": "SPECULATIVE_BUY", "conviction": "high", "score": 80,
            })
        self.assertEqual(signal["candidate_action"], "STRONG BUY")
        self.assertEqual(signal["action"], "RESEARCH")
        self.assertFalse(signal["auto_trade_allowed"])
        self.assertEqual(signal["strategy_id"], research.LIVE_STRATEGY_ID)

    def test_policy_loader_rejects_strategy_implementation_mismatch(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "policy.json")
            report_path = os.path.join(tmp, "report.json")
            common = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "VALIDATED",
                "auto_trade_allowed": True,
                "policy_hash": "same",
                "selected_strategy": "profitable_earnings_beat",
            }
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump(dict(common, strategy_id="profitable_earnings_beat"), f)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(common, f)
            with (
                mock.patch.object(research, "EDGE_POLICY_PATH", policy_path),
                mock.patch.object(research, "EDGE_REPORT_PATH", report_path),
                mock.patch.object(research, "_EDGE_POLICY_CACHE", (-1.0, {})),
            ):
                policy = research.edge_policy(force_refresh=True)
            self.assertEqual(policy["status"], "STRATEGY_MISMATCH")
            self.assertFalse(policy["auto_trade_allowed"])

    def test_edge_policy_fails_closed_when_report_and_policy_disagree(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "policy.json")
            report_path = os.path.join(tmp, "report.json")
            common = {
                "generated_at": "2026-08-07T12:00:00+00:00",
                "status": "VALIDATED",
                "auto_trade_allowed": True,
            }
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump(dict(common, policy_hash="policy"), f)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(dict(common, policy_hash="different"), f)
            with (
                mock.patch.object(research, "EDGE_POLICY_PATH", policy_path),
                mock.patch.object(research, "EDGE_REPORT_PATH", report_path),
                mock.patch.object(research, "_EDGE_POLICY_CACHE", (-1.0, {})),
            ):
                policy = research.edge_policy(force_refresh=True)
            self.assertFalse(policy["auto_trade_allowed"])
            self.assertEqual(policy["status"], "MISSING")

    def test_gap_fade_is_a_hard_rejection(self):
        d = complete_dossier(gap_pct=25.0, from_open_pct=-5.0)
        self.assertIn("gap", research.hard_risk_reason(d))

    def test_json_parser_handles_braces_inside_strings(self):
        raw = 'preface {"verdict":"WATCH","note":"literal { brace }"} suffix'
        parsed = research._extract_json(raw)
        self.assertEqual(parsed["note"], "literal { brace }")

    def test_ai_output_is_schema_checked(self):
        with self.assertRaises(ValueError):
            research._normalize_ai({"verdict": "BUY", "score": 90})
        normalized = research._normalize_ai({
            "verdict": "watch", "conviction": "strange", "score": 120,
            "key_risks": "dilution",
        })
        self.assertEqual(normalized["verdict"], "WATCH")
        self.assertEqual(normalized["conviction"], "low")
        self.assertEqual(normalized["score"], 100.0)
        self.assertEqual(normalized["key_risks"], ["dilution"])

    def test_screener_interleaves_independent_passes(self):
        class Query:
            def __init__(self, operator, operand):
                self.operator = operator
                self.operand = operand

        class FakeYF:
            EquityQuery = Query

            @staticmethod
            def screen(_query, size, sortField, sortAsc):
                names = {
                    "percentchange": ["GAIN1", "GAIN2"],
                    "dayvolume": ["VOL1", "VOL2"],
                    "short_percentage_of_float.value": ["SHORT1", "SHORT2"],
                }
                return {"quotes": [{"symbol": x} for x in names[sortField]]}

        with (
            mock.patch.object(research, "yf", FakeYF),
            mock.patch.object(
                research, "_current_penny_universe",
                return_value={"SEC1", "SEC2"},
            ),
            mock.patch.object(
                research.sec_edgar, "current_8k_tickers", return_value=["SEC1", "SEC2"]
            ),
        ):
            self.assertEqual(
                research.screen(6),
                ["SEC1", "GAIN1", "VOL1", "SHORT1", "SEC2", "GAIN2"],
            )

    def test_signal_log_deduplicates_same_ticker_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            with mock.patch.object(paper, "STATE_PATH", state):
                bot = isolated_bot(tempfile.mkdtemp())
                board = [{
                    "ticker": "TEST", "rank": 1, "price": 2.0,
                    "composite": 70, "hype": 70, "technical": 70,
                    "catalyst": 60, "quality": 60, "tradeability": 90,
                    "spread_pct": 1.0, "spread_estimated": False,
                    "market_state": "REGULAR", "quote_reliable": True,
                    "bid": 1.99, "ask": 2.01, "quote_age_min": 1.0,
                    "confirmation": {"confirmed": True, "observations": 2},
                    "signal": {"action": "BUY", "stop": 1.8,
                               "candidate_action": "BUY",
                               "target1": 2.3, "target2": 2.5},
                }]
                bot._record_signals(board)
                bot._record_signals(board)
                self.assertEqual(len(bot.signal_log), 1)

    def test_strategy_upgrade_drops_stale_dashboard_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            with open(state, "w", encoding="utf-8") as handle:
                json.dump({
                    "watchlist": [
                        {"ticker": "OLD", "signal": {"strategy_id": "old-rule"}},
                        {"ticker": "NEW", "signal": {
                            "strategy_id": research.LIVE_STRATEGY_ID,
                        }},
                    ],
                }, handle)
            # this test supplies its own state file, so only the archive is redirected
            with mock.patch.object(paper, "STATE_PATH", state), \
                 mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH",
                                   os.path.join(tmp, "archive.jsonl")):
                bot = paper.PennyStockPaperBot()
        self.assertEqual([row["ticker"] for row in bot.watchlist], ["NEW"])

    def test_price_breakout_without_dated_catalyst_stays_watch(self):
        d = complete_dossier(
            fresh_news_count=0, latest_news_age_hours=-1,
            days_to_earnings=-1, recent_filings=[],
        )
        rank = {"composite": 85, "hype": 85, "quality": 80,
                "tradeability": 95, "technical": 90, "catalyst": 0}
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "strategy_id": research.LIVE_STRATEGY_ID,
        }):
            signal = research.signal_from(d, rank, {
                "verdict": "SPECULATIVE_BUY", "conviction": "high", "score": 90,
            })
        self.assertEqual(signal["action"], "WATCH")
        self.assertIn("no dated catalyst", signal["why"])

    def test_future_earnings_date_is_risk_not_a_published_catalyst(self):
        d = complete_dossier(
            fresh_news_count=0, latest_news_age_hours=-1,
            days_to_earnings=2, recent_filings=[], sec_8k_verified=False,
        )
        self.assertFalse(research.has_dated_catalyst(d))

    def test_sec_item_without_aligned_headline_is_not_a_trade_catalyst(self):
        d = complete_dossier(
            fresh_news_count=0, latest_news_age_hours=-1, news=[],
            sec_8k_verified=True, latest_sec_8k_age_hours=2,
            recent_8k_items=["2.02", "9.01"], adverse_8k_items=[],
            recent_filings=[{
                "type": "8-K", "official_sec": True, "age_hours": 2,
                "items": ["2.02", "9.01"], "accessionNumber": "a",
            }],
        )
        self.assertFalse(research.has_dated_catalyst(d))
        score, reasons = research.catalyst_score(d)
        self.assertEqual(score, 0)
        self.assertTrue(any("direction not proven" in value for value in reasons))

    def test_headline_and_material_sec_event_must_be_time_aligned(self):
        d = complete_dossier(
            latest_news_age_hours=3,
            news=[{"title": "Earnings released", "publisher": "News",
                   "when": "2026-08-08T15:00:00Z", "age_hours": 3}],
            sec_8k_verified=True, latest_sec_8k_age_hours=4,
            recent_8k_items=["2.02", "9.01"], adverse_8k_items=[],
            recent_filings=[{
                "type": "8-K", "official_sec": True, "age_hours": 4,
                "items": ["2.02", "9.01"], "accessionNumber": "a",
                "accepted_at": "2026-08-08T14:00:00Z",
            }],
        )
        aligned = research.catalyst_alignment(d)
        self.assertTrue(aligned["aligned"])
        self.assertEqual(aligned["accession"], "a")
        self.assertTrue(research.has_dated_catalyst(d))
        score, reasons = research.catalyst_score(d)
        self.assertEqual(score, 45)
        self.assertTrue(any("official 8-K aligned" in value for value in reasons))

        d.news[0]["age_hours"] = 40
        self.assertFalse(research.has_dated_catalyst(d))

    def test_sec_discovery_cannot_spend_slots_on_non_penny_issuers(self):
        class FakeYF:
            class EquityQuery:
                def __init__(self, *args):
                    pass

            @staticmethod
            def screen(query, size=0, sortField="", sortAsc=False):
                return {"quotes": [{"symbol": f"{sortField[:2].upper()}1"}]}

        with (
            mock.patch.object(research, "yf", FakeYF),
            mock.patch.object(research, "_current_penny_universe", return_value={"PENNY"}),
            mock.patch.object(
                research.sec_edgar, "current_8k_tickers",
                return_value=["BIGCAP", "PENNY", "FUND-WT"],
            ),
        ):
            result = research.screen(4)
        self.assertEqual(result[0], "PENNY")
        self.assertNotIn("BIGCAP", result)
        self.assertNotIn("FUND-WT", result)

    def test_adverse_sec_item_is_a_hard_rejection(self):
        d = complete_dossier(
            fresh_news_count=0, latest_news_age_hours=-1,
            sec_8k_verified=True, latest_sec_8k_age_hours=1,
            recent_8k_items=["3.01", "8.01"], adverse_8k_items=["3.01"],
        )
        self.assertFalse(research.has_dated_catalyst(d))
        self.assertIn("3.01", research.hard_risk_reason(d))

    def test_setup_requires_two_separated_observations_and_resets_a_chase(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())

        def board(price):
            return [{
                "ticker": "TEST", "price": price, "catalyst_key": "news-1",
                "market_state": "REGULAR", "quote_reliable": True,
                "spread_estimated": False,
                "signal": {"action": "RESEARCH", "candidate_action": "BUY"},
            }]

        first = board(2.0)
        bot._update_setup_states(first, now=1_000)
        self.assertFalse(first[0]["confirmation"]["confirmed"])
        second = board(2.02)
        bot._update_setup_states(second, now=1_045)
        self.assertTrue(second[0]["confirmation"]["confirmed"])

        chased = board(2.20)
        bot._update_setup_states(chased, now=1_100)
        self.assertFalse(chased[0]["confirmation"]["confirmed"])
        self.assertEqual(chased[0]["confirmation"]["observations"], 1)
        self.assertIn("chased", chased[0]["confirmation"]["reset_reason"])

    def test_after_hours_or_proxy_quote_cannot_confirm_a_setup(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        board = [{
            "ticker": "TEST", "price": 2.0, "catalyst_key": "news-1",
            "market_state": "CLOSED", "quote_reliable": False,
            "spread_estimated": True,
            "signal": {"action": "RESEARCH", "candidate_action": "BUY"},
        }]
        bot._update_setup_states(board, now=1_000)
        bot._update_setup_states(board, now=1_100)
        self.assertFalse(board[0]["confirmation"]["confirmed"])
        self.assertEqual(board[0]["confirmation"]["observations"], 0)
        self.assertFalse(board[0]["confirmation"]["executable_observation"])
        self.assertIn("regular-session", board[0]["confirmation"]["reset_reason"])
        bot._record_signals(board)
        self.assertEqual(bot.signal_log, [])

    def test_research_scan_is_not_blocked_by_paused_entries_or_full_book(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        bot.enabled = False
        bot.pos = {str(i): object() for i in range(paper.MAX_OPEN)}
        bot.last_scan = 700
        bot.last_full_scan = 900
        self.assertEqual(bot.scan_plan(True, now=1_000), "pulse")

    def test_forward_stats_use_cost_adjusted_return(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"outcomes": {"5": {
            "return_pct": 1.0, "net_return_pct": -0.5,
            "net_excess_return_pct": -1.0, "target1_hit": False,
            "stop_hit": True,
        }}}]
        stats5 = bot.signal_stats()["5"]
        self.assertEqual(stats5["net_hit_rate"], 0.0)
        self.assertEqual(stats5["avg_net_return_pct"], -0.5)
        self.assertEqual(stats5["avg_net_excess_pct"], -1.0)
        self.assertEqual(stats5["stop_rate"], 100.0)

    def test_forward_validation_counts_signal_days_not_repeated_names(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = []
        for day in range(10):
            for name in range(6):
                bot.signal_log.append({
                    "ticker": f"T{name}", paper.SIGNAL_DAY_FIELD: f"2026-01-{day + 1:02d}",
                    "engine_version": paper.SIGNAL_ENGINE_VERSION,
                    "outcomes": {"5": {"net_return_pct": 2.0,
                                         "net_excess_return_pct": 1.0}},
                })
        verdict = bot.forward_validation()
        self.assertEqual(verdict["completed_signals"], 60)
        self.assertEqual(verdict["signal_days"], 10)
        self.assertEqual(verdict["status"], "COLLECTING")

    def test_forward_validation_never_unlocks_entries(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{
            "ticker": f"T{day % 35}", paper.SIGNAL_DAY_FIELD: f"day-{day:03d}",
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            "outcomes": {"5": {"net_return_pct": 2.0,
                                 "net_excess_return_pct": 1.0}},
        } for day in range(70)]
        verdict = bot.forward_validation()
        self.assertEqual(verdict["status"], "PROMISING_NOT_VALIDATED")
        self.assertFalse(verdict["auto_trade_allowed"])


if __name__ == "__main__":
    unittest.main()


class TestMarketRegime(unittest.TestCase):
    """The regime gate must only ever REDUCE risk, never create a signal."""

    def _fake_regime(self, score, label):
        import time as _t
        research._REGIME_CACHE.update({
            "t": _t.time(),
            "data": {"score": score, "label": label, "why": [], "known": True,
                     "iwm_5d": 0.0, "iwm_vs_sma20": 0.0},
        })

    def _strong_dossier(self):
        """A dossier that clears every hard-risk gate, so the test isolates the
        regime logic rather than accidentally re-testing the risk gates."""
        d = research.Dossier(ticker="TEST")
        d.price = 3.0
        d.exchange = sorted(research.LISTED_EXCHANGES)[0]
        d.quote_type = "EQUITY"
        d.avg_volume = 5_000_000
        d.volume = 8_000_000
        d.data_completeness = 90.0
        d.runway_known = True; d.runway_quarters = 8.0
        d.shares_change_known = True; d.shares_change_pct = 1.0
        d.spread_pct = 0.5; d.spread_reliable = True
        d.change_pct = 12.0; d.gap_pct = 3.0; d.from_open_pct = 4.0
        d.technical_known = True
        d.high20_distance_pct = -1.0; d.close_location = 0.85
        d.atr_pct = 8.0
        d.fresh_news_count = 1; d.latest_news_age_hours = 4.0
        d.news = [{"title": "Material update", "publisher": "News",
                   "when": "2026-08-08T14:00:00Z", "age_hours": 4.0}]
        d.sec_8k_verified = True; d.latest_sec_8k_age_hours = 4.5
        d.recent_8k_items = ["8.01"]
        d.recent_filings = [{
            "type": "8-K", "official_sec": True, "age_hours": 4.5,
            "items": ["8.01"], "accessionNumber": "regime-test",
        }]
        return d

    def test_riskoff_blocks_a_buy(self):
        d = self._strong_dossier()
        r = {"composite": 70, "hype": 55, "quality": 60, "tradeability": 90,
             "technical": 80, "catalyst": 30}
        ai = {"verdict": "SPECULATIVE_BUY", "conviction": "high", "score": 80}
        self._fake_regime(20, "risk-off")
        self.assertEqual(research.signal_from(d, r, ai)["action"], "WATCH")

    def test_riskon_allows_a_buy(self):
        d = self._strong_dossier()
        r = {"composite": 70, "hype": 55, "quality": 60, "tradeability": 90,
             "technical": 80, "catalyst": 30}
        ai = {"verdict": "SPECULATIVE_BUY", "conviction": "high", "score": 80}
        self._fake_regime(80, "risk-on")
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "selected_strategy": research.LIVE_STRATEGY_ID,
            "strategy_id": research.LIVE_STRATEGY_ID,
        }):
            self.assertIn(research.signal_from(d, r, ai)["action"], ("BUY", "STRONG BUY"))

    def test_regime_never_creates_a_signal(self):
        """A mechanically weak setup stays weak even in the best possible tape."""
        d = self._strong_dossier()
        r = {"composite": 20, "hype": 5, "quality": 10, "tradeability": 90,
             "technical": 30, "catalyst": 0}
        self._fake_regime(95, "risk-on")
        self.assertNotIn(research.signal_from(d, r, {})["action"], ("BUY", "STRONG BUY"))


class TestSectorConcentration(unittest.TestCase):
    def test_cap_blocks_third_position_in_one_sector(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.pos = {}
        for i, t in enumerate(("AAA", "BBB")):
            bot.pos[t] = paper.Position(
                id=str(i), ticker=t, name=t, qty=1, entry=1.0, mid_at_entry=1.0,
                stop=0.9, take_profit=1.2, high_water=1.0, opened_at=0.0,
                opened_day="", sector="Healthcare")
        self.assertTrue(bot.sector_full("Healthcare"))
        self.assertFalse(bot.sector_full("Technology"))


class TestSelectionAwareInference(unittest.TestCase):
    """The audit's newest gate: does the winner beat best-of-N noise, and could this
    sample have detected an edge at all?"""

    @staticmethod
    def _trades(daily_returns, start="2020-01-06"):
        import pandas as pd
        dates = pd.bdate_range(start, periods=len(daily_returns))
        return pd.DataFrame({"signal_date": dates, "net_return": list(daily_returns),
                             "ticker": ["AAA"] * len(daily_returns)})

    def test_search_penalty_is_never_smaller_than_the_naive_p_value(self):
        """Testing more strategies can only make a win less impressive, never more."""
        import numpy as np
        rng = np.random.default_rng(7)
        book = {f"s{i}": self._trades(rng.normal(0, 0.05, 400)) for i in range(8)}
        out = stats.reality_check(book, n_boot=600)
        self.assertTrue(out["applicable"])
        self.assertGreaterEqual(out["p_value_selection_aware"],
                                out["p_value_naive_single_test"])

    def test_pure_noise_is_not_called_significant(self):
        import numpy as np
        rng = np.random.default_rng(11)
        book = {f"s{i}": self._trades(rng.normal(0, 0.05, 400)) for i in range(8)}
        self.assertFalse(stats.reality_check(book, n_boot=600)["significant_at_5pct"])

    def test_a_real_edge_still_clears_the_bar(self):
        """The gate must not be unfalsifiable - a genuine edge has to survive it."""
        import numpy as np
        rng = np.random.default_rng(3)
        book = {f"s{i}": self._trades(rng.normal(0, 0.05, 400)) for i in range(5)}
        book["real"] = self._trades(rng.normal(0.02, 0.05, 400))
        out = stats.reality_check(book, n_boot=600)
        self.assertEqual(out["best_strategy"], "real")
        self.assertTrue(out["significant_at_5pct"])

    def test_underpowered_sample_reports_that_it_cannot_resolve_the_effect(self):
        import numpy as np
        rng = np.random.default_rng(5)
        thin = self._trades(rng.normal(0.01, 0.20, 12))   # huge dispersion, 12 days
        out = stats.power(thin, "thin")
        self.assertTrue(out["applicable"])
        self.assertFalse(out["sample_can_resolve_observed_edge"])
        self.assertGreater(out["min_detectable_edge_pct"], abs(out["mean_net_pct"]))

    def test_idle_sessions_are_not_compressed_into_adjacent_signal_events(self):
        import pandas as pd

        calendar = pd.bdate_range("2024-01-02", periods=80)
        sparse = pd.DataFrame({
            "signal_date": [calendar[2], calendar[60]],
            "net_return": [0.01, -0.01],
            "ticker": ["AAA", "AAA"],
        })
        matrix, names, market_sessions = stats.daily_matrix(
            {"sparse": sparse}, calendar=calendar
        )
        # The panel spans the real interval between first and last signal. The old
        # implementation returned 2 and treated events months apart as adjacent.
        self.assertEqual(names, ["sparse"])
        self.assertEqual(market_sessions, 59)
        self.assertEqual(int((matrix[:, 0] != 0).sum()), 2)

    def test_block_uncertainty_detects_overlapping_serial_dependence(self):
        import numpy as np

        rng = np.random.default_rng(13)
        # Five adjacent observations share one shock, mimicking overlapping outcomes.
        clustered = self._trades(np.repeat(rng.normal(0.005, 0.06, 50), 5))
        out = stats.power(clustered, "clustered", n_boot=900, block=20)
        self.assertGreater(
            out["standard_error_pct"], out["naive_standard_error_pct"] * 1.25
        )
        self.assertGreater(out["autocorrelation_inflation_vs_iid"], 1.25)

    def test_breadth_reduces_the_history_required(self):
        """The plan's whole point: holding more names beats collecting more decades."""
        import numpy as np
        rng = np.random.default_rng(9)
        plan = stats.detectability_plan(self._trades(rng.normal(0.005, 0.18, 200)))
        self.assertTrue(plan["applicable"])
        self.assertGreater(plan["names_per_day_needed_to_use_current_history"],
                           plan["current_names_per_signal_day"])

    def test_survivorship_bound_only_ever_hurts(self):
        out = stats.survivorship_sensitivity(2.0, hold_days=10)
        self.assertLess(out["survivorship_adjusted_mean_net_pct"], 2.0)
        self.assertLess(out["estimated_drag_pct"], 0.0)

    def test_plan_reports_when_the_per_day_cap_is_not_the_lever(self):
        """A signal-starved rule must not be told to raise a cap that never binds.

        Regression for a recommendation that measured as useless: the earnings rule
        gave identical results at caps of 3, 6, 10, 15 and 25.
        """
        import pandas as pd
        dates = pd.bdate_range("2020-01-06", periods=60)
        # the real rule's shape: a long tail below the maximum, which only 3 days reach
        counts = [1] * 50 + [2] * 7 + [3] * 3
        rows = []
        for day, k in zip(dates, counts):
            rows += [{"signal_date": day, "net_return": 0.01, "ticker": f"T{i}"}
                     for i in range(k)]
        starved = pd.DataFrame(rows)
        plan = stats.detectability_plan(starved)
        self.assertTrue(plan["breadth_is_signal_limited"])
        self.assertIn("not binding", plan["binding_lever"])

        # a cap that truly binds piles every day up against the same ceiling
        crowded = pd.DataFrame([
            {"signal_date": day, "net_return": 0.01, "ticker": t}
            for day in dates for t in "ABCDE"
        ])
        plan2 = stats.detectability_plan(crowded)
        self.assertFalse(plan2["breadth_is_signal_limited"])


class TestCostDecomposition(unittest.TestCase):
    """Net loss alone cannot say whether the signal is wrong or the costs are too big;
    those call for opposite responses."""

    @staticmethod
    def _book(gross, cost):
        import pandas as pd
        n = len(gross)
        return pd.DataFrame({
            "signal_date": pd.bdate_range("2020-01-06", periods=n),
            "gross_return": gross, "cost": [cost] * n,
            "net_return": [g - cost for g in gross],
            "ticker": ["AAA"] * n,
        })

    def test_zero_gross_edge_is_not_blamed_on_costs(self):
        """The live-rule finding: gross +0.006%, cost 1.55%. Cheaper execution cannot
        rescue a rule that has nothing to keep."""
        import numpy as np
        rng = np.random.default_rng(4)
        out = stats.cost_decomposition(self._book(rng.normal(0.0, 0.05, 400), 0.015))
        self.assertTrue(out["gross_indistinguishable_from_zero"])
        self.assertIn("cannot rescue", out["diagnosis"])

    def test_real_gross_edge_eaten_by_costs_points_at_execution(self):
        import numpy as np
        rng = np.random.default_rng(4)
        out = stats.cost_decomposition(self._book(rng.normal(0.03, 0.02, 400), 0.04))
        self.assertFalse(out["gross_indistinguishable_from_zero"])
        self.assertIn("execution", out["diagnosis"])

    def test_breakeven_cost_is_the_gross_expectancy(self):
        out = stats.cost_decomposition(self._book([0.02] * 50, 0.01))
        self.assertAlmostEqual(out["breakeven_cost_pct"], out["mean_gross_pct"], places=6)


class TestEdgarCatalystTiming(unittest.TestCase):
    """A catalyst dataset is only worth having if it cannot see the future."""

    @staticmethod
    def _cal(dates):
        import pandas as pd
        return pd.DatetimeIndex(pd.to_datetime(dates))

    def test_a_filing_after_the_signal_is_invisible(self):
        import pandas as pd
        from research import edgar_catalysts as ed
        cal = self._cal(["2024-03-01", "2024-06-15"])
        # standing on 2024-03-10, the June filing must not exist yet
        self.assertEqual(ed.days_since(cal, pd.Timestamp("2024-03-10")), 9.0)
        # standing before every filing, there is no catalyst at all
        self.assertEqual(ed.days_since(cal, pd.Timestamp("2024-01-01")), float("inf"))

    def test_same_day_filing_counts_as_zero_days_old(self):
        import pandas as pd
        from research import edgar_catalysts as ed
        cal = self._cal(["2024-03-01"])
        self.assertEqual(ed.days_since(cal, pd.Timestamp("2024-03-01")), 0.0)

    def test_empty_calendar_never_claims_a_catalyst(self):
        import pandas as pd
        from research import edgar_catalysts as ed
        self.assertEqual(ed.days_since(self._cal([]), pd.Timestamp("2024-03-01")),
                         float("inf"))
        self.assertEqual(ed.days_since(None, pd.Timestamp("2024-03-01")), float("inf"))

    def test_future_data_cannot_change_a_past_answer(self):
        """Append later filings; every earlier answer must be byte-identical."""
        import pandas as pd
        from research import edgar_catalysts as ed
        base = self._cal(["2023-01-10", "2023-05-02"])
        extended = self._cal(["2023-01-10", "2023-05-02", "2024-09-09", "2025-02-02"])
        for day in pd.bdate_range("2023-01-01", "2023-12-31", freq="7D"):
            self.assertEqual(ed.days_since(base, day), ed.days_since(extended, day))

    def test_item_codes_are_normalized_and_adverse_items_are_classified(self):
        from research import edgar_catalysts as ed
        value = ed.classify_8k_items("2.02, 9.01;3.01,2.02")
        self.assertEqual(value["items"], ["2.02", "3.01", "9.01"])
        self.assertTrue(value["earnings"])
        self.assertTrue(value["negative"])
        self.assertEqual(value["negative_items"], ["3.01"])

    def test_sec_column_arrays_remain_aligned(self):
        from research import edgar_catalysts as ed
        rows = ed._aligned_rows([{
            "form": ["8-K", "10-Q"],
            "filingDate": ["2026-01-02", "2026-01-03"],
            "acceptanceDateTime": ["2026-01-02T21:00:00Z"],
            "items": ["2.02,9.01", ""],
        }])
        self.assertEqual(rows[0]["items"], "2.02,9.01")
        self.assertEqual(rows[1]["acceptanceDateTime"], "")
        self.assertEqual(rows[1]["form"], "10-Q")

    def test_after_close_filing_waits_for_next_reaction_session(self):
        import pandas as pd
        from research.penny_event_drift import reaction_position
        index = pd.bdate_range("2026-01-05", periods=5)
        # 20:30 UTC is 15:30 ET in January: that day's close is observable.
        self.assertEqual(reaction_position(index, "2026-01-06T20:30:00Z"), 1)
        # 22:00 UTC is 17:00 ET: the next session contains the first reaction close.
        self.assertEqual(reaction_position(index, "2026-01-06T22:00:00Z"), 2)


class TestSecFundamentalTiming(unittest.TestCase):
    @staticmethod
    def _fact(tag, unit, values):
        return {tag: {"units": {unit: values}}}

    @staticmethod
    def _value(accession, start, end, value, filed):
        return {
            "accn": accession, "form": "10-Q", "start": start, "end": end,
            "val": value, "filed": filed, "fy": int(end[:4]), "fp": "Q2",
        }

    def test_comparative_column_cannot_replace_the_current_quarter(self):
        from research.sec_fundamentals import _standalone_quarters

        concept = {"units": {"USD": [
            self._value("NEW", "2023-04-01", "2023-06-30", 80, "2024-08-01"),
            self._value("NEW", "2024-04-01", "2024-06-30", 100, "2024-08-01"),
        ]}}
        selected = _standalone_quarters(concept, "USD")
        self.assertEqual(selected["NEW"]["end"], "2024-06-30")
        self.assertEqual(selected["NEW"]["value"], 100.0)

    def test_yoy_growth_uses_the_original_prior_accession(self):
        from research.sec_fundamentals import extract_events

        prior = self._value("OLD", "2023-04-01", "2023-06-30", 80, "2023-08-01")
        current = self._value("NEW", "2024-04-01", "2024-06-30", 100, "2024-08-01")
        comparative = self._value(
            "NEW", "2023-04-01", "2023-06-30", 999, "2024-08-01"
        )
        prior_income = dict(prior, val=4)
        current_income = dict(current, val=8)
        comparative_income = dict(comparative, val=999)
        facts = {"facts": {"us-gaap": {
            **self._fact("Revenues", "USD", [prior, comparative, current]),
            **self._fact("NetIncomeLoss", "USD", [
                prior_income, comparative_income, current_income
            ]),
        }}}
        filings = [
            {"form": "10-Q", "accessionNumber": "OLD", "filingDate": "2023-08-01",
             "acceptanceDateTime": "2023-08-01T20:00:00Z"},
            {"form": "10-Q", "accessionNumber": "NEW", "filingDate": "2024-08-01",
             "acceptanceDateTime": "2024-08-01T20:00:00Z"},
        ]
        events = extract_events(facts, filings)
        newest = next(event for event in events if event["accessionNumber"] == "NEW")
        self.assertEqual(newest["prior_revenue"], 80.0)
        self.assertEqual(newest["revenue_growth_pct"], 25.0)
        self.assertEqual(newest["prior_net_income"], 4.0)

    def test_entry_waits_if_the_filing_misses_the_opening_cutoff(self):
        import pandas as pd
        from research.penny_fundamental_drift import entry_position

        index = pd.bdate_range("2026-01-05", periods=5)
        self.assertEqual(entry_position(index, "2026-01-06T14:20:00Z"), 1)
        self.assertEqual(entry_position(index, "2026-01-06T14:30:00Z"), 2)


class TestItemCodeAudit(unittest.TestCase):
    """The marginal audit must match the deployed fail-closed item taxonomy."""

    def test_material_and_adverse_sets_are_canonical(self):
        from research import penny_item_code_test as T
        from research import edgar_catalysts as ed

        self.assertFalse(T.MATERIAL_ITEMS & T.ADVERSE_ITEMS)
        self.assertEqual(T.ADVERSE_ITEMS, ed.NEGATIVE_8K_ITEMS)
        self.assertIn("3.02", T.ADVERSE_ITEMS)

    def test_mixed_filing_is_adverse_before_material(self):
        from research import penny_item_code_test as T

        self.assertEqual(T.item_group("1.01|3.02|9.01"), "adverse")
        self.assertEqual(T.item_group("2.02|9.01"), "material_direction_unknown")
        self.assertEqual(T.item_group("5.07|9.01"), "neither")

    def test_submissions_z_timestamp_is_converted_from_utc(self):
        import pandas as pd
        from research.penny_event_drift import reaction_position

        index = pd.bdate_range("2026-05-11", periods=4)
        # The API's 11:07Z is 07:07 New York in May, so the May 12 close is the
        # first observable reaction close.  Treating 11:07 as Eastern is wrong.
        self.assertEqual(reaction_position(index, "2026-05-12T11:07:44Z"), 1)

    def test_eligibility_excludes_non_penny_and_illiquid_rows(self):
        import pandas as pd
        from research import penny_item_code_test as T

        rows = pd.DataFrame({
            "ticker": ["GOOD", "BIG", "THIN"],
            "signal_date": pd.to_datetime(["2025-01-02"] * 3),
            "items": ["2.02|9.01"] * 3,
            "raw_close": [2.0, 20.0, 2.0],
            "dollar_volume20": [2_000_000.0, 20_000_000.0, 100_000.0],
            "gross_5": [0.01, 0.02, 0.03],
            "reaction_pct": [5.0] * 3,
            "volume_ratio": [2.0] * 3,
            "close_location": [0.8] * 3,
            "atr_pct": [0.1] * 3,
            "max_ret20": [0.1] * 3,
            "dilution_age_days": [100.0] * 3,
        })
        with mock.patch.object(T.event_drift, "build", return_value=rows):
            selected = T.eligible_events()
        self.assertEqual(selected["ticker"].tolist(), ["GOOD"])

    def test_live_evidence_labels_item_result_as_a_reused_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            live_path = os.path.join(directory, "live.json")
            item_path = os.path.join(directory, "item.json")
            catalyst_path = os.path.join(directory, "missing-catalyst.json")
            with open(live_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "strategy_id": "price-core", "trades": 10,
                    "cost_decomposition": {"all": {}}, "splits": {"test": {}},
                }, handle)
            with open(item_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "status": "NO_STANDALONE_ITEM_CODE_EDGE",
                    "exact_live_rule_backtest": False,
                    "verdict": "no standalone edge",
                    "results": {"reaction_confirmed_proxy": {"post_2024_reused": {
                        "material_direction_unknown": {
                            "gross": {
                                "applicable": True, "events": 210,
                                "mean_signal_day_basket_net_pct": -0.0434,
                                "bootstrap_95_pct": [-1.77, 2.01],
                            },
                            "net_after_0_5pct_cost": {
                                "mean_signal_day_basket_net_pct": -0.5434,
                            },
                        }
                    }}},
                }, handle)
            with (
                mock.patch.object(research, "LIVE_AUDIT_PATH", live_path),
                mock.patch.object(research, "ITEM_AUDIT_PATH", item_path),
                mock.patch.object(research, "CATALYST_AUDIT_PATH", catalyst_path),
                mock.patch.object(
                    research, "_LIVE_AUDIT_CACHE", ((-1.0, -1.0, -1.0), {})
                ),
            ):
                evidence = research.live_rule_evidence()["item_gate"]
        self.assertFalse(evidence["exact_live_rule_backtest"])
        self.assertEqual(evidence["window"], "post_2024_reused_not_holdout")
        self.assertIn("not exact live rule", evidence["scope"])


class TestExitStructure(unittest.TestCase):
    """Exit hypotheses stay experimental until they match the deployed entry rule."""

    def test_fixed_target_and_trail_are_on_by_default(self):
        self.assertTrue(paper.USE_FIXED_TARGET)
        self.assertTrue(paper.USE_TRAILING_STOP)

    def test_target_can_be_disabled_only_by_explicit_environment(self):
        import importlib
        with mock.patch.dict(os.environ, {"PENNY_FIXED_TARGET": "0"}):
            reloaded = importlib.reload(paper)
            self.assertFalse(reloaded.USE_FIXED_TARGET)
        with mock.patch.dict(os.environ, {}, clear=True):
            importlib.reload(paper)
            self.assertTrue(paper.USE_FIXED_TARGET)

    def test_ten_days_means_calendar_days_not_ten_sessions(self):
        import pandas as pd
        sessions = pd.bdate_range("2026-08-03", periods=20)
        position = exit_structure._deadline_position(sessions, 0)
        self.assertEqual(sessions[position], pd.Timestamp("2026-08-13"))
        self.assertEqual(position, 8)

    def test_target_can_help_when_a_winner_reverses(self):
        """The old max-high test was wrong: uncapped exits do not sell at max high."""
        entry, target, final_close = 1.00, 1.25, 0.90
        capped_realized = target / entry - 1.0
        uncapped_realized = final_close / entry - 1.0
        self.assertGreater(capped_realized, uncapped_realized)

    def test_reaction_proxy_never_uses_future_returns(self):
        import pandas as pd
        row = {
            "negative_event": False, "raw_close": 2.0, "reaction_pct": 8.0,
            "volume_ratio": 2.0, "close_location": 0.8,
            "dollar_volume20": 8_000_000, "atr_pct": 0.1, "max_ret20": 0.1,
            "dilution_age_days": 200, "signal_date": pd.Timestamp("2024-01-02"),
        }
        first = dict(row, gross_5=-0.9, ticker="AAA")
        second = dict(row, gross_5=5.0, ticker="BBB")
        selected = exit_structure._reaction_confirmed_proxy(
            pd.DataFrame([first, second])
        )
        self.assertEqual(set(selected["ticker"]), {"AAA", "BBB"})


class TestFeasibilityGate(unittest.TestCase):
    """Estimate whether a verdict is practical without calling a proxy exact."""

    @staticmethod
    def _stream(n, sd, years, seed=3):
        import numpy as np, pandas as pd
        rng = np.random.default_rng(seed)
        dates = pd.to_datetime(
            pd.Series(pd.date_range("2017-01-01", periods=n,
                                    freq=pd.Timedelta(days=max(1, int(365.25*years/n))))))
        return pd.Series(rng.normal(0, sd, n)), dates

    def test_a_selective_rule_is_infeasible_within_the_horizon(self):
        from research import penny_feasibility as feas
        r, d = self._stream(112, 0.126, 9.5)          # the desk's own entry rate
        f = feas.feasibility(r, d, target_effect_pct=2.0, n_boot=600)
        self.assertFalse(f["verdict_reachable_within_patience"])
        self.assertEqual(f["status"], "INFEASIBLE_WITHIN_HORIZON")
        self.assertIn("INFEASIBLE WITHIN", feas.verdict_line(f))
        self.assertNotIn("UNPROVABLE", feas.verdict_line(f))

    def test_a_broad_rule_is_resolvable(self):
        from research import penny_feasibility as feas
        r, d = self._stream(4000, 0.126, 9.5)
        f = feas.feasibility(r, d, target_effect_pct=2.0, n_boot=600)
        self.assertTrue(f["already_resolvable"])

    def test_same_day_events_do_not_inflate_power(self):
        """Ten events on one day are one observation, not ten."""
        import pandas as pd
        from research import penny_feasibility as feas
        r = pd.Series([0.01, -0.02, 0.03] * 40)
        clustered = pd.Series(pd.to_datetime(["2020-01-06", "2020-01-06", "2020-01-06"] * 40))
        spread = pd.Series(pd.date_range("2020-01-06", periods=120, freq="B"))
        clustered_result = feas.feasibility(r, clustered, n_boot=300)
        spread_result = feas.feasibility(r, spread, n_boot=300,
                                         min_history_years=0.1)
        self.assertFalse(clustered_result["applicable"])
        self.assertIn("distinct signal days", clustered_result["reason"])
        self.assertEqual(spread_result["independent_signal_days"], 120)

    def test_time_remaining_is_not_total_history(self):
        from research import penny_feasibility as feas
        r, d = self._stream(112, 0.126, 9.5)
        f = feas.feasibility(r, d, target_effect_pct=2.0, n_boot=600)
        self.assertAlmostEqual(
            f["total_years_required"] - f["history_years"],
            f["additional_years_required"],
            delta=0.2,
        )

    def test_feasibility_is_not_evidence(self):
        """A resolvable rule is not thereby a profitable one."""
        from research import penny_feasibility as feas
        r, d = self._stream(4000, 0.126, 9.5)
        f = feas.feasibility(r, d, n_boot=600)
        self.assertNotIn("profitable", feas.verdict_line(f).lower())
        self.assertNotIn("edge", feas.verdict_line(f).lower())

    def test_proxy_scope_cannot_be_mislabeled_exact(self):
        from research import penny_feasibility as feas
        r, d = self._stream(120, 0.126, 9.5)
        result = feas.compare({"proxy": {
            "returns": r, "dates": d, "exact_live_rule": False,
            "scope": "historical proxy; not exact v3",
        }}, n_boot=300)["proxy"]
        self.assertFalse(result["exact_live_rule"])
        self.assertIn("not exact", result["scope"])

    def test_forward_tracker_exposes_feasibility_without_unlocking_trades(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            paper, "STATE_PATH", os.path.join(tmp, "state.json")
        ):
            bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = []
        result = bot.forward_validation()
        self.assertIn("feasibility", result)
        self.assertFalse(result["feasibility"]["applicable"])
        self.assertIn("not assessable", result["feasibility"]["summary"])
        self.assertFalse(result["auto_trade_allowed"])


class TestEvidenceClock(unittest.TestCase):
    """Filtering the signal log by engine version is correct, but each bump silently
    restarts collection. The cost should be visible before shipping another version."""

    @staticmethod
    def _production_row(day):
        """A row shaped the way the writer actually emits one.

        The first version of this test invented a "day" key while production wrote
        "signal_day", so the clock counted zero and the test passed anyway. Fixtures
        must use the same constant the writer uses, never a hand-typed key.
        """
        return {"engine_version": paper.SIGNAL_ENGINE_VERSION,
                paper.SIGNAL_DAY_FIELD: day, "ticker": "TEST"}

    def test_clock_counts_rows_in_the_production_format(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._production_row("2026-08-05"),
                          self._production_row("2026-08-06"),
                          self._production_row("2026-08-06")]
        clock = bot._evidence_clock()
        self.assertEqual(clock["signal_days_logged"], 2)      # deduped by session
        self.assertEqual(clock["discarded_by_next_version_bump"], 2)
        self.assertEqual(clock["completed_days_remaining"],
                         clock["completed_signal_days_required"])

    def test_a_renamed_session_key_cannot_pass_unnoticed(self):
        """Regression for the actual bug: any other key must count as nothing, so a
        future rename fails loudly instead of silently reading zero."""
        bot = isolated_bot(tempfile.mkdtemp())
        for wrong in ("day", "signal_date", "session"):
            bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                               wrong: "2026-08-05"}]
            self.assertEqual(bot._evidence_clock()["signal_days_logged"], 0,
                             f"{wrong!r} must not be mistaken for the session key")

    def test_prior_version_rows_do_not_count_as_evidence(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION - 1,
                           paper.SIGNAL_DAY_FIELD: "2026-01-01"}]
        self.assertEqual(bot._evidence_clock()["signal_days_logged"], 0)

    def test_clock_is_planning_not_evidence(self):
        bot = isolated_bot(tempfile.mkdtemp())
        clock = bot._evidence_clock()
        self.assertNotIn("edge", json.dumps(clock).lower())
        self.assertNotIn("profitable", json.dumps(clock).lower())
        self.assertIn("strategy_id", clock)


class TestProgressIsOneNumber(unittest.TestCase):
    """The clock and the verdict gate must never report different progress against the
    same log. Logged 60/60 while the gate said COLLECTING with nothing completed."""

    @staticmethod
    def _row(day, *, completed=False, benchmarked=False):
        row = {"engine_version": paper.SIGNAL_ENGINE_VERSION,
               paper.SIGNAL_DAY_FIELD: day, "ticker": "TEST"}
        if completed:
            outcome = {"net_return_pct": 1.0}
            if benchmarked:
                outcome["net_excess_return_pct"] = 0.5
            row["outcomes"] = {"5": outcome}
        return row

    def test_logged_days_are_not_counted_as_progress(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row(f"2026-06-{d:02d}") for d in range(1, 31)]
        p = bot.signal_day_progress()
        self.assertEqual(p["signal_days_logged"], 30)
        self.assertEqual(p["completed_signal_days"], 0)
        self.assertEqual(p["completed_days_remaining"], 60)
        self.assertEqual(p["incomplete_signal_days"], 30)
        self.assertEqual(p["unresolved_rows"], 30)

    def test_clock_and_gate_agree(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row(f"2026-06-{d:02d}") for d in range(1, 31)]
        clock = bot._evidence_clock()
        gate = bot.forward_validation()
        self.assertEqual(gate["status"], "COLLECTING")
        self.assertGreater(clock["completed_days_remaining"], 0)
        # the clock must not claim completion while the gate is still collecting
        self.assertFalse(clock["completed_days_remaining"] == 0
                         and gate["status"] == "COLLECTING")

    def test_benchmark_coverage_is_tracked_separately(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = ([self._row(f"2026-06-{d:02d}", completed=True) for d in range(1, 11)]
                          + [self._row(f"2026-07-{d:02d}", completed=True, benchmarked=True)
                             for d in range(1, 6)])
        p = bot.signal_day_progress()
        self.assertEqual(p["completed_signal_days"], 15)
        self.assertEqual(p["benchmarked_signal_days"], 5)
        self.assertLess(p["benchmarked_signal_days"], p["completed_signal_days"])

    def test_no_eta_survives_anywhere_in_the_clock(self):
        bot = isolated_bot(tempfile.mkdtemp())
        blob = json.dumps(bot._evidence_clock()).lower()
        for banned in ("five years", "years of an unchanged", "12 signal days"):
            self.assertNotIn(banned, blob)


class TestPartialBasketsAreExcluded(unittest.TestCase):
    """A day where one name resolves and another never does is not a smaller sample,
    it is a biased one: the unresolved names are the halted and delisted ones."""

    @staticmethod
    def _row(day, ticker, net=None, excess=None):
        row = {"engine_version": paper.SIGNAL_ENGINE_VERSION,
               paper.SIGNAL_DAY_FIELD: day, "ticker": ticker}
        if net is not None:
            outcome = {"net_return_pct": net}
            if excess is not None:
                outcome["net_excess_return_pct"] = excess
            row["outcomes"] = {"5": outcome}
        return row

    def test_a_winner_plus_an_unresolved_name_is_not_a_completed_day(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row("2026-06-01", "WIN", 10.0, 9.0),
                          self._row("2026-06-01", "HALTED")]
        p = bot.signal_day_progress()
        self.assertEqual(p["completed_signal_days"], 0)
        self.assertEqual(p["benchmarked_signal_days"], 0)
        self.assertEqual(p["incomplete_signal_days"], 1)
        self.assertEqual(p["unresolved_rows"], 1)

    def test_the_survivor_return_never_reaches_the_mean(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row("2026-06-01", "WIN", 10.0, 9.0),
                          self._row("2026-06-01", "HALTED")]
        self.assertEqual(bot.daily_baskets()["baskets"], [])
        self.assertNotEqual(bot.forward_validation().get("mean_net_pct"), 10.0)

    def test_a_wholly_resolved_day_still_counts(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row("2026-06-01", "A", 10.0, 9.0),
                          self._row("2026-06-01", "B", -4.0, -5.0)]
        book = bot.daily_baskets()
        self.assertEqual(len(book["baskets"]), 1)
        self.assertAlmostEqual(book["baskets"][0]["net_pct"], 3.0)   # equal-weight
        self.assertEqual(bot.signal_day_progress()["completed_signal_days"], 1)

    def test_one_unbenchmarked_member_blocks_the_benchmark(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row("2026-06-01", "A", 10.0, 9.0),
                          self._row("2026-06-01", "B", -4.0)]      # no excess
        p = bot.signal_day_progress()
        self.assertEqual(p["completed_signal_days"], 1)
        self.assertEqual(p["benchmarked_signal_days"], 0)

    def test_retention_keeps_whole_sessions_past_the_sixty_day_gate(self):
        """The old 400-row cap left ~57 days, so the 60-day gate was unreachable."""
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._row(f"2026-{m:02d}-{d:02d}", f"T{i}", 1.0, 0.5)
                          for m in (5, 6, 7) for d in range(1, 26) for i in range(7)]
        self.assertEqual(len(bot.signal_log), 75 * 7)      # 525 rows, 75 sessions
        kept = bot._retained_signals()
        days = {r[paper.SIGNAL_DAY_FIELD] for r in kept}
        self.assertEqual(len(days), 75)                    # every session survives
        self.assertGreater(len(days), 60)                  # the gate is reachable
        # the old row cap would have cut this below the threshold it feeds
        self.assertLess(len({r[paper.SIGNAL_DAY_FIELD] for r in bot.signal_log[:400]}), 60)
        for day in days:                       # no session is retained in part
            self.assertEqual(sum(1 for r in kept if r[paper.SIGNAL_DAY_FIELD] == day), 7)


class TestMissingnessBlocksVerdicts(unittest.TestCase):
    """Excluding a matured-but-unresolved day is not conservative: the days that fail to
    resolve hold the halted and delisted names, so dropping them removes losses."""

    @staticmethod
    def _done(day, ticker="A", net=2.0, excess=1.5):
        return {"engine_version": paper.SIGNAL_ENGINE_VERSION,
                paper.SIGNAL_DAY_FIELD: day, "ticker": ticker,
                "outcomes": {"5": {"net_return_pct": net, "net_excess_return_pct": excess}}}

    @staticmethod
    def _missing(day, ticker="HALTED"):
        return {"engine_version": paper.SIGNAL_ENGINE_VERSION,
                paper.SIGNAL_DAY_FIELD: day, "ticker": ticker}

    def _sixty_good_days(self):
        return [self._done(f"2026-0{1 + i // 28}-{1 + i % 28:02d}") for i in range(60)]

    def test_a_stale_missing_day_blocks_a_verdict_the_bound_cannot_survive(self):
        """A thin edge cannot absorb a name going to zero, so the verdict is withheld."""
        bot = isolated_bot(tempfile.mkdtemp())
        thin = [dict(row, outcomes={"5": {"net_return_pct": 0.5,
                                          "net_excess_return_pct": 0.4}})
                for row in self._sixty_good_days()]
        bot.signal_log = thin + [self._missing("2026-03-20", "HALTED")]
        verdict = bot.forward_validation()
        self.assertEqual(verdict["status"], "DATA_INCOMPLETE")
        self.assertFalse(verdict["auto_trade_allowed"])
        self.assertGreater(verdict["stale_incomplete_days"], 0)
        self.assertFalse(verdict["missing_outcome_bound"]["clears_zero"])

    def test_the_gate_is_passable_when_nothing_is_unresolved(self):
        """A bound that nothing can clear is a veto, not a bound. With no stale day the
        positive verdict still stands - and still never unlocks trading."""
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = self._sixty_good_days()
        verdict = bot.forward_validation()
        self.assertEqual(verdict["stale_incomplete_days"], 0)
        self.assertTrue(verdict["missing_outcome_bound"]["clears_zero"])
        self.assertEqual(verdict["status"], "PROMISING_NOT_VALIDATED")
        self.assertFalse(verdict["auto_trade_allowed"])

    def test_one_catastrophic_missing_day_defeats_a_sixty_day_edge(self):
        """Measured: a single -100% day widens the HAC interval past zero."""
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = self._sixty_good_days() + [self._missing("2026-03-20", "HALTED")]
        verdict = bot.forward_validation()
        self.assertEqual(verdict["status"], "DATA_INCOMPLETE")

    def test_a_pending_day_does_not_block_anything(self):
        """A horizon that has not elapsed yet is not evidence of anything."""
        import datetime as dt
        today = dt.datetime.now(paper.NY).date().isoformat()
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = self._sixty_good_days() + [self._missing(today, "TOOFRESH")]
        book = bot.daily_baskets()
        self.assertEqual(book["stale_incomplete_days"], 0)
        self.assertEqual(book["pending_days"], 1)

    def test_the_bound_assumes_missing_names_went_to_zero(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = self._sixty_good_days() + [self._missing("2026-03-20")]
        bound = bot.missing_outcome_bound()
        self.assertTrue(bound["applicable"])
        self.assertEqual(bound["assumed_missing_outcome_pct"], -100.0)
        self.assertLess(bound["bounded_mean_net_pct"], 2.0)

    def test_signal_stats_is_labelled_non_evidentiary(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._done("2026-01-01", "WIN", 10.0, 9.0),
                          self._missing("2026-01-01")]
        block = bot.signal_stats()["5"]
        self.assertFalse(block["evidentiary"])
        self.assertEqual(block["admitted_basket_days"], 0)   # the day is not admitted
        self.assertIn("survivor", block["basis"])

    def test_a_save_failure_is_surfaced_not_swallowed(self):
        bot = isolated_bot(tempfile.mkdtemp())
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._save()
        self.assertIn("state save failed", bot.state_save_error)


class TestBoundIsDependenceRobust(unittest.TestCase):
    """The bound must meet the same standard as the verdict it guards: a positive mean
    whose HAC interval spans zero has not survived the missing outcomes."""

    @staticmethod
    def _done(day, net=2.0, excess=1.5, ticker="A"):
        return {"engine_version": paper.SIGNAL_ENGINE_VERSION,
                paper.SIGNAL_DAY_FIELD: day, "ticker": ticker, "cost_pct": 1.0,
                "outcomes": {"5": {"net_return_pct": net, "net_excess_return_pct": excess,
                                   "benchmark_return_pct": 0.5}}}

    def test_a_positive_mean_with_an_interval_spanning_zero_does_not_clear(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._done(f"2026-0{1 + i // 28}-{1 + i % 28:02d}")
                          for i in range(60)]
        bot.signal_log.append({"engine_version": paper.SIGNAL_ENGINE_VERSION,
                               paper.SIGNAL_DAY_FIELD: "2026-03-20",
                               "ticker": "HALTED", "cost_pct": 1.0})
        bound = bot.missing_outcome_bound()
        self.assertGreater(bound["bounded_mean_net_pct"], 0)          # mean is positive
        self.assertLess(bound["bounded_net_hac_95_pct"][0], 0)        # interval is not
        self.assertFalse(bound["clears_zero"])
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_the_bound_weights_each_signal_day_equally(self):
        """Row weighting let a crowded day outvote a sparse one and understated the hit."""
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._done("2026-01-01", ticker=f"T{i}") for i in range(9)]
        bot.signal_log += [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                            paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "HALTED",
                            "cost_pct": 1.0}]
        bound = bot.missing_outcome_bound()
        self.assertEqual(bound["signal_days_included"], 2)            # days, not rows
        # one clean day at +2 and one day bounded to -101 -> about -49.5, not -8.3
        self.assertLess(bound["bounded_mean_net_pct"], -40)

    def test_costs_make_a_missing_outcome_worse_than_total_loss(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                           paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "H",
                           "cost_pct": 1.5}]
        bound = bot.missing_outcome_bound()
        self.assertLess(bound["bounded_mean_net_pct"], -100.0)

    def test_every_stale_day_is_bounded_not_only_the_first_twenty(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                           paper.SIGNAL_DAY_FIELD: f"2026-01-{d:02d}", "ticker": "H",
                           "cost_pct": 1.0} for d in range(1, 26)]
        book = bot.daily_baskets()
        self.assertEqual(len(book["stale_all"]), 25)
        self.assertEqual(len(book["stale_detail"]), 20)               # display cap only
        self.assertEqual(bot.missing_outcome_bound()["imputed_days"], 25)


class TestArchiveIsAuthoritative(unittest.TestCase):
    def test_outcomes_survive_a_restart_through_the_archive(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(paper, "STATE_PATH", os.path.join(tmp, "s.json")), \
             mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", os.path.join(tmp, "a.jsonl")):
            bot = paper.PennyStockPaperBot()
            bot._archive_event("signal", {"id": "sig1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                          "ticker": "A",
                                          "engine_version": paper.SIGNAL_ENGINE_VERSION})
            bot._archive_event("outcome", {"id": "sig1", "horizon": "5",
                                           "outcome": {"net_return_pct": 3.0},
                                           "resolved": True})
            restored = paper.PennyStockPaperBot()
        self.assertEqual(len(restored.signal_log), 1)
        self.assertEqual(restored.signal_log[0]["outcomes"]["5"]["net_return_pct"], 3.0)

    def test_a_torn_final_line_does_not_lose_earlier_events(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "a.jsonl")
        with mock.patch.object(paper, "STATE_PATH", os.path.join(tmp, "s.json")), \
             mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", path):
            bot = paper.PennyStockPaperBot()
            bot._archive_event("signal", {"id": "sig1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                          "engine_version": paper.SIGNAL_ENGINE_VERSION})
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"event": "signal", "id": "trunc"')     # no newline, no close
            self.assertEqual(len(paper.PennyStockPaperBot().signal_log), 1)

    def test_archive_and_state_health_are_tracked_separately(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.archive_error = "signal archive write failed: OSError"
        bot._save()                                   # a good save must not mask it
        self.assertEqual(bot.state_save_error, "")
        self.assertIn("archive write failed", bot.archive_error)
