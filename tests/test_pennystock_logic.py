import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import penny_quotes

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
    return paper.PennyStockPaperBot(
        state_path=os.path.join(tmpdir, "state.json"),
        archive_path=os.path.join(tmpdir, "archive.jsonl"))


def seed_calendar(bot, sessions, source="alpaca"):
    """Give a bot a real exchange-calendar record for the sessions it will measure.

    Without one the schedule is a fallback guess, and an outcome resting on a guessed
    close is deliberately not evidence - so any test about the exit leg has to say which
    sessions the exchange actually reported.
    """
    bot._session_calendar = {
        "sessions": {day: {"open_minute": 9 * 60 + 30, "close_minute": close}
                     for day, close in sessions.items()},
        "covered": {"from": min(sessions), "to": max(sessions)},
        "fetched_on": "2026-01-01", "source": source}
    return bot


def alpaca_schedule(close_minute=16 * 60, **extra):
    row = {"close_minute": close_minute, "source": "alpaca",
           "evidentiary": True, "is_trading_day": True}
    row.update(extra)
    return row


def measured_outcome(net=2.0, excess=1.5, **extra):
    """An outcome measured the way production measures one: bought at the ask, sold at
    an OBSERVED horizon bid, under the current measurement schema.

    Fixtures that skip this are asserting an unobserved exit leg, and a positive verdict
    built on them is correctly blocked - so any basket test that is really about
    missingness or statistics must say plainly that its cost side was measured.
    """
    out = {"net_return_pct": net, "net_excess_return_pct": excess,
           "measurement_schema": paper.MEASUREMENT_SCHEMA_VERSION,
           "entry_basis": "ask", "exit_basis": "observed_bid",
           "entry_quote_feed": "sip", "exit_quote_feed": "sip",
           # BOTH legs. One observed side is not an observed round trip.
           "entry_cost_evidentiary": True, "exit_cost_evidentiary": True,
           "cost_evidentiary": True}
    out.update(extra)
    return out


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

    def test_a_suspect_quote_fails_closed_instead_of_being_repriced_cheaper(self):
        """A 38% book on a name doing $4M/day is probably a bad tick - but the ADV proxy
        is a hand-fitted ranking heuristic, not an observation.

        Letting it overrule the quote and then charging the SMALLER proxy cost was
        circular: the estimator being audited got to rule that the observation
        disagreeing with it was wrong, and the name was rescued into a signal at a cost
        nobody had seen. The book is distrusted, never made cheaper.
        """
        artifact = complete_dossier(spread_pct=38.0, spread_reliable=True)
        spread, estimated = research.effective_spread(artifact)
        self.assertTrue(estimated)                     # never executable
        self.assertGreaterEqual(spread, 38.0)          # and never cheaper than quoted
        self.assertFalse(research.trusted_execution_quote(artifact))

        # the proxy still governs where there is no usable quote to contradict
        stale = complete_dossier(spread_pct=38.0, spread_reliable=False)
        proxy, estimated = research.effective_spread(stale)
        self.assertTrue(estimated)
        self.assertLess(proxy, 4.0)

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
        # A suspect book is no longer repriced down to the proxy, so it now fails the
        # spread cap outright rather than the trusted-quote check. Either rejection is
        # correct; reaching a fill is not.
        self.assertTrue(any("exceeds the" in x["msg"] or "book is suspect" in x["msg"]
                            for x in bot.log))

        # and the trusted-quote branch still catches a name the proxy prices under the
        # cap: an unreliable book is not an executable one
        proxied = complete_dossier(spread_pct=38.0, spread_reliable=False)
        with mock.patch.object(research, "edge_policy", return_value={
            "status": "VALIDATED", "auto_trade_allowed": True,
            "strategy_id": research.LIVE_STRATEGY_ID,
        }):
            bot._open(proxied, {"verdict": "SPECULATIVE_BUY", "conviction": "high"},
                      {"strategy_id": research.LIVE_STRATEGY_ID, "entry": proxied.price},
                      rank=1)
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

    def test_deep_scan_deadline_is_ten_seconds_in_every_market_phase(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.last_scan_started = 1_000
        bot.last_scan = 1_004
        bot.last_full_scan = 1_000

        for is_open in (True, False):
            self.assertEqual(bot.scan_interval(is_open, now=1_009), 10)
            self.assertEqual(bot.scan_due_at(is_open, now=1_009), 1_010)
            self.assertIsNone(bot.scan_plan(is_open, now=1_009.999))
            self.assertEqual(bot.scan_plan(is_open, now=1_010), "pulse")

    def test_fast_deep_scan_consumes_the_latest_all_market_candidates(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot._universe_rows = [{"ticker": "WIDE1"}, {"ticker": "WIDE2"}]
        bot.watchlist = [{
            "ticker": "KEEP",
            "signal": {"candidate_action": "BUY"},
        }]
        with mock.patch.object(
            penny_quotes, "select_market_candidates", return_value=["WIDE1", "WIDE2"]
        ) as select:
            candidates = bot._pulse_candidates(["YAHOO1"], pool=24)

        select.assert_called_once_with(bot._universe_rows, 24)
        self.assertEqual(candidates, ["KEEP", "WIDE1", "YAHOO1", "WIDE2"])

    def test_state_exposes_absolute_deadline_for_a_smooth_client_countdown(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.last_scan_started = 1_000
        bot.last_scan = 1_004
        with mock.patch.object(bot, "market_open", return_value=True), \
             mock.patch.object(paper.time, "time", return_value=1_006):
            state = bot.state()

        self.assertEqual(state["scan_interval_sec"], 10)
        self.assertEqual(state["server_time"], 1_006)
        self.assertEqual(state["next_scan_at"], 1_010)
        self.assertEqual(state["next_scan_in_sec"], 4)

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
            "outcomes": {"5": measured_outcome(2.0, 1.0)},
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

    def test_current_feed_cache_matches_the_fast_scanner_latency(self):
        from research import edgar_catalysts as ed
        self.assertEqual(ed.CURRENT_FEED_TTL_SEC, 30)
        self.assertLessEqual(ed.CURRENT_FEED_TTL_SEC, 3 * paper.DEEP_SCAN_SEC)

    def test_a_transient_sec_failure_keeps_last_known_filings_visible(self):
        import pandas as pd
        from research import edgar_catalysts as ed
        now = pd.Timestamp.now(tz="UTC")
        old_event = {
            "symbol": "KEEP", "cik": "0000000001", "accessionNumber": "a",
            "accepted_at": (now - pd.Timedelta(minutes=5)).isoformat(),
            "age_hours": 0.0,
        }
        status = {
            "status": "COMPLETE", "last_attempt_at": 0.0,
            "last_success_at": 1.0, "error": "", "served_stale": False,
        }
        with mock.patch.object(ed, "_feed_cache", (1.0, [old_event])), \
             mock.patch.object(ed, "_feed_status", status), \
             mock.patch.object(ed, "ticker_to_cik", return_value={"KEEP": "0000000001"}), \
             mock.patch.object(ed, "_request", return_value=None):
            events = ed.current_8k_events(max_age_hours=72)
            health = ed.current_feed_status()

        self.assertEqual([row["symbol"] for row in events], ["KEEP"])
        self.assertGreater(events[0]["age_hours"], 0.0)
        self.assertEqual(health["status"], "DEGRADED")
        self.assertTrue(health["served_stale"])
        self.assertIn("unavailable", health["error"])

    def test_concurrent_current_feed_callers_share_one_sec_request(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import time as wall_time
        from research import edgar_catalysts as ed
        entry = {
            "cik": "0000000001", "company": "Test", "accepted_at": "",
            "accessionNumber": "a", "url": "", "items": "2.02",
        }
        import pandas as pd
        entry["accepted_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        status = {
            "status": "EMPTY", "last_attempt_at": 0.0,
            "last_success_at": 0.0, "error": "", "served_stale": False,
        }
        request_entered = threading.Event()
        release_request = threading.Event()

        def slow_request(_url):
            request_entered.set()
            release_request.wait(timeout=1)
            return b"feed"

        request = mock.Mock(side_effect=slow_request)
        with mock.patch.object(ed, "_feed_cache", (0.0, [])), \
             mock.patch.object(ed, "_feed_status", status), \
             mock.patch.object(ed, "ticker_to_cik", return_value={"TEST": "0000000001"}), \
             mock.patch.object(ed, "_request", request), \
             mock.patch.object(ed, "_feed_entries", return_value=[entry]):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(ed.current_8k_events)
                self.assertTrue(request_entered.wait(timeout=1))
                second = pool.submit(ed.current_8k_events)
                wall_time.sleep(0.03)
                release_request.set()
                results = [first.result(timeout=1), second.result(timeout=1)]

        self.assertEqual(request.call_count, 1)
        self.assertEqual([[row["symbol"] for row in result] for result in results],
                         [["TEST"], ["TEST"]])

    def test_a_valid_empty_sec_response_is_cached_instead_of_refetched(self):
        from research import edgar_catalysts as ed
        status = {
            "status": "EMPTY", "last_attempt_at": 0.0,
            "last_success_at": 0.0, "error": "", "served_stale": False,
        }
        request = mock.Mock(return_value=b"empty feed")
        with mock.patch.object(ed, "_feed_cache", (0.0, [])), \
             mock.patch.object(ed, "_feed_status", status), \
             mock.patch.object(ed, "ticker_to_cik", return_value={"TEST": "0000000001"}), \
             mock.patch.object(ed, "_request", request), \
             mock.patch.object(ed, "_feed_entries", return_value=[]):
            self.assertEqual(ed.current_8k_events(), [])
            self.assertEqual(ed.current_8k_events(), [])
            self.assertEqual(ed.current_feed_status()["status"], "COMPLETE")

        self.assertEqual(request.call_count, 1)

    def test_cached_event_age_is_recomputed_before_filtering(self):
        import pandas as pd
        from research import edgar_catalysts as ed
        now = pd.Timestamp.now(tz="UTC")
        expired = {
            "symbol": "OLD", "accepted_at": (now - pd.Timedelta(hours=73)).isoformat(),
            "age_hours": 0.0,
        }
        status = {
            "status": "COMPLETE", "last_attempt_at": 0.0,
            "last_success_at": 1.0, "error": "", "served_stale": False,
        }
        with mock.patch.object(ed, "_feed_cache", (ed.time.time(), [expired])), \
             mock.patch.object(ed, "_feed_status", status):
            self.assertEqual(ed.current_8k_events(max_age_hours=72), [])


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
        # wording changed: an empty sample is a normal pre-data state, not a fault
        summary = result["feasibility"]["summary"]
        self.assertFalse(result["feasibility"]["applicable"])
        self.assertIn("not yet measurable", summary)
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
                "outcomes": {"5": measured_outcome(net, excess)}}

    @staticmethod
    def _missing(day, ticker="HALTED"):
        return {"engine_version": paper.SIGNAL_ENGINE_VERSION,
                paper.SIGNAL_DAY_FIELD: day, "ticker": ticker}

    def _sixty_good_days(self):
        return [self._done(f"2026-0{1 + i // 28}-{1 + i % 28:02d}") for i in range(60)]

    def test_a_stale_missing_day_blocks_a_verdict_the_bound_cannot_survive(self):
        """A thin edge cannot absorb a name going to zero, so the verdict is withheld."""
        bot = isolated_bot(tempfile.mkdtemp())
        thin = [dict(row, outcomes={"5": measured_outcome(0.5, 0.4)})
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
                paper.SIGNAL_DAY_FIELD: day, "ticker": ticker, paper.SIGNAL_COST_FIELD: 1.0,
                "outcomes": {"5": measured_outcome(net, excess,
                                                   benchmark_return_pct=0.5)}}

    def test_a_positive_mean_with_an_interval_spanning_zero_does_not_clear(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [self._done(f"2026-0{1 + i // 28}-{1 + i % 28:02d}")
                          for i in range(60)]
        bot.signal_log.append({"engine_version": paper.SIGNAL_ENGINE_VERSION,
                               paper.SIGNAL_DAY_FIELD: "2026-03-20",
                               "ticker": "HALTED", paper.SIGNAL_COST_FIELD: 1.0})
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
                            paper.SIGNAL_COST_FIELD: 1.0}]
        bound = bot.missing_outcome_bound()
        self.assertEqual(bound["signal_days_included"], 2)            # days, not rows
        # one clean day at +2 and one day bounded to -101 -> about -49.5, not -8.3
        self.assertLess(bound["bounded_mean_net_pct"], -40)

    def test_costs_make_a_missing_outcome_worse_than_total_loss(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                           paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "H",
                           paper.SIGNAL_COST_FIELD: 1.5}]
        bound = bot.missing_outcome_bound()
        self.assertAlmostEqual(bound["bounded_mean_net_pct"], -101.5, places=3)
        self.assertLess(bound["bounded_mean_net_pct"], -100.0)

    def test_every_stale_day_is_bounded_not_only_the_first_twenty(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                           paper.SIGNAL_DAY_FIELD: f"2026-01-{d:02d}", "ticker": "H",
                           paper.SIGNAL_COST_FIELD: 1.0} for d in range(1, 26)]
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
        bot.archive_write_error = "signal archive write failed: OSError"
        bot._save()                                   # a good save must not mask it
        self.assertEqual(bot.state_save_error, "")
        self.assertIn("archive write failed", bot.archive_write_error)


class TestEvidenceIntegrity(unittest.TestCase):
    """The evidence file must be one population, durable, and honest about damage."""

    @staticmethod
    def _archive(tmp, rows):
        path = os.path.join(tmp, "a.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path

    def _sig(self, sid, day):
        return {"event": "signal", "id": sid, paper.SIGNAL_DAY_FIELD: day,
                "ticker": "A", "engine_version": paper.SIGNAL_ENGINE_VERSION}

    def test_production_cost_field_reaches_the_bound(self):
        """The writer's own field name, not one a fixture invented."""
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{"engine_version": paper.SIGNAL_ENGINE_VERSION,
                           paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "H",
                           paper.SIGNAL_COST_FIELD: 4.0}]
        self.assertAlmostEqual(
            bot.missing_outcome_bound()["bounded_mean_net_pct"], -104.0, places=3)

    def test_analytics_do_not_change_when_one_more_signal_arrives(self):
        tmp = tempfile.mkdtemp()
        rows = [self._sig(f"s{i}", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}")
                for i in range(181)]
        path = self._archive(tmp, rows)
        with mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", path), \
             mock.patch.object(paper, "STATE_PATH", os.path.join(tmp, "s.json")):
            bot = paper.PennyStockPaperBot()
            before = len({r[paper.SIGNAL_DAY_FIELD] for r in bot.evidence_rows()})
            bot.signal_log = bot._retained_signals()          # the UI cache trims
            after = len({r[paper.SIGNAL_DAY_FIELD] for r in bot.evidence_rows()})
        self.assertEqual(before, 181)
        self.assertEqual(before, after)      # analytics read the archive, not the cache

    def test_corruption_before_the_end_is_reported_and_blocks_verdicts(self):
        tmp = tempfile.mkdtemp()
        path = self._archive(tmp, [self._sig("a", "2026-01-01"),
                                   "{corrupt in the middle",
                                   self._sig("b", "2026-01-02")])
        with mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", path), \
             mock.patch.object(paper, "STATE_PATH", os.path.join(tmp, "s.json")):
            bot = paper.PennyStockPaperBot()
            self.assertIn("corrupt", bot.archive_error)
            self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_only_a_torn_final_line_is_tolerated(self):
        tmp = tempfile.mkdtemp()
        path = self._archive(tmp, [self._sig("a", "2026-01-01")])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"event": "signal", "id": "torn"')
        with mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", path), \
             mock.patch.object(paper, "STATE_PATH", os.path.join(tmp, "s.json")):
            bot = paper.PennyStockPaperBot()
            self.assertEqual(bot.archive_error, "")
            self.assertEqual(len(bot.signal_log), 1)

    def test_a_failed_append_is_retried_not_dropped(self):
        bot = isolated_bot(tempfile.mkdtemp())
        # The outcome must be a production-shaped event for a signal that was really
        # recorded. A bare {"id", "horizon"} is neither, and a fixture like that only
        # ever proved the queue accepted rubbish.
        self.assertTrue(bot._archive_event("signal", {
            "id": "x", paper.SIGNAL_DAY_FIELD: "2026-01-01",
            "engine_version": paper.SIGNAL_ENGINE_VERSION}))
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(bot._archive_event("outcome", {
                "id": "x", "horizon": "5", "outcome": {"net_return_pct": 4.0}}))
        self.assertEqual(len(bot._archive_outbox), 1)
        # Losing the disk after a real write lands on the torn-tail branch rather than
        # the append branch, so assert the property both must satisfy: the event is
        # retained and the failure is surfaced, not that one branch's wording appears.
        self.assertIn("1 event(s)", bot.archive_write_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
        self.assertTrue(bot._archive_event("signal", {
            "id": "y", paper.SIGNAL_DAY_FIELD: "2026-01-02",
            "engine_version": paper.SIGNAL_ENGINE_VERSION}))         # both land now
        self.assertEqual(bot._archive_outbox, [])
        self.assertEqual(len(bot._replay_archive()), 2)              # x and y
        self.assertEqual(bot.archive_error, "")                      # nothing orphaned

    def test_state_cache_and_archive_are_merged_by_id(self):
        """A final-horizon event can fail after the cache was written, so neither side
        is reliably newer - the outcomes are unioned."""
        tmp = tempfile.mkdtemp()
        path = self._archive(tmp, [self._sig("s1", "2026-01-02")])
        state = os.path.join(tmp, "s.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"signal_log": [{"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                       "engine_version": paper.SIGNAL_ENGINE_VERSION,
                                       "outcomes": {"10": {"net_return_pct": 7.0}}}]}, f)
        with mock.patch.object(paper, "SIGNAL_ARCHIVE_PATH", path), \
             mock.patch.object(paper, "STATE_PATH", state):
            bot = paper.PennyStockPaperBot()
        self.assertEqual(len(bot.signal_log), 1)
        self.assertIn("10", bot.signal_log[0]["outcomes"])   # cache-only outcome kept


class TestEvidenceStoreIsAuthoritative(unittest.TestCase):
    """Assertions go through the analytics surface, not signal_log: the reconciliation
    was correct at load and then discarded by a second replay."""

    @staticmethod
    def _write(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def test_a_cache_only_outcome_survives_into_the_analytics(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        self._write(ap, [{"event": "signal", "id": "s1",
                          paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "A",
                          "engine_version": paper.SIGNAL_ENGINE_VERSION}])
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"signal_log": [{"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                       "engine_version": paper.SIGNAL_ENGINE_VERSION,
                                       "outcomes": {"5": {"net_return_pct": 7.0}}}]}, f)
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        rows = bot.evidence_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("5", rows[0].get("outcomes") or {})       # not just signal_log
        self.assertEqual(bot.daily_baskets()["logged_rows"], 1)

    def test_the_outbox_survives_a_restart(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", {"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                      "engine_version": paper.SIGNAL_ENGINE_VERSION})
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("outcome", {"id": "s1", "horizon": "5",
                                           "outcome": {"net_return_pct": 4.0}})
        bot._persist_outbox()                       # the failed write left a queue
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(len(restarted._archive_outbox), 1)
        self.assertIn("awaiting retry", restarted.archive_error)
        self.assertEqual(restarted.forward_validation()["status"], "DATA_INCOMPLETE")
        restarted._archive_event("signal", {                    # writes recover
            "id": "s2", paper.SIGNAL_DAY_FIELD: "2026-01-03",
            "engine_version": paper.SIGNAL_ENGINE_VERSION})
        self.assertEqual(restarted._archive_outbox, [])

    def test_the_outbox_is_never_truncated(self):
        bot = isolated_bot(tempfile.mkdtemp())
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            for i in range(600):
                bot._archive_event("signal", {
                    "id": f"s{i}", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                    "engine_version": paper.SIGNAL_ENGINE_VERSION})
        self.assertEqual(len(bot._archive_outbox), 600)     # was capped at 500

    def test_a_newline_terminated_corrupt_final_record_is_not_a_tear(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        self._write(ap, [{"event": "signal", "id": "a",
                          paper.SIGNAL_DAY_FIELD: "2026-01-01",
                          "engine_version": paper.SIGNAL_ENGINE_VERSION},
                         "{corrupt but newline terminated"])
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertIn("corrupt", bot.archive_error)

    def test_an_unterminated_final_line_is_still_tolerated(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        self._write(ap, [{"event": "signal", "id": "a",
                          paper.SIGNAL_DAY_FIELD: "2026-01-01",
                          "engine_version": paper.SIGNAL_ENGINE_VERSION}])
        with open(ap, "a", encoding="utf-8") as f:
            f.write('{"event": "signal", "id": "torn"')     # no trailing newline
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertEqual(bot.archive_error, "")

    def test_both_benchmark_paths_agree(self):
        """The two anchors disagreed 36.4% vs 50.0% on the same event."""
        dates = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
        closes = [100.0, 110.0, 120.0, 130.0, 140.0]
        item = {paper.SIGNAL_DAY_FIELD: "2026-01-01", "benchmark_price": 100.0}
        leg = paper.PennyStockPaperBot._benchmark_leg(item, 3, dates, closes)
        self.assertAlmostEqual(leg, 30.0, places=3)   # 130/100 - 1, from the snapshot

    def test_derived_metrics_use_the_evidence_population(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot._evidence = {"s1": {"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                "ticker": "ONLY_IN_EVIDENCE",
                                "engine_version": paper.SIGNAL_ENGINE_VERSION,
                                "outcomes": {"5": {"net_return_pct": 1.0,
                                                   "net_excess_return_pct": 0.5}}}}
        bot.signal_log = []                            # the UI cache is empty
        self.assertEqual(bot.forward_validation()["unique_tickers"], 1)


class TestHealthFieldsAreIndependent(unittest.TestCase):
    """A successful write proves the write path works. It proves nothing about a corrupt
    record already on disk, and must not clear that."""

    @staticmethod
    def _corrupt_archive(tmp):
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "signal", "id": "a",
                                paper.SIGNAL_DAY_FIELD: "2026-01-01",
                                "engine_version": paper.SIGNAL_ENGINE_VERSION}) + "\n")
            f.write("{corrupt\n")
        return ap

    def test_a_successful_append_does_not_clear_corruption(self):
        tmp = tempfile.mkdtemp()
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=self._corrupt_archive(tmp))
        self.assertIn("corrupt", bot.archive_integrity_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
        self.assertTrue(bot._archive_event("signal", {
            "id": "z", paper.SIGNAL_DAY_FIELD: "2026-01-02",
            "engine_version": paper.SIGNAL_ENGINE_VERSION}))
        self.assertIn("corrupt", bot.archive_integrity_error)          # still blocking
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_only_a_clean_replay_clears_an_integrity_error(self):
        tmp = tempfile.mkdtemp()
        ap = self._corrupt_archive(tmp)
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertNotEqual(bot.archive_integrity_error, "")
        with open(ap, "w", encoding="utf-8") as f:                     # repaired on disk
            f.write(json.dumps({"event": "signal", "id": "a",
                                paper.SIGNAL_DAY_FIELD: "2026-01-01",
                                "engine_version": paper.SIGNAL_ENGINE_VERSION}) + "\n")
        bot._replay_archive()
        self.assertEqual(bot.archive_integrity_error, "")

    def test_memory_only_outbox_is_not_reported_as_durable(self):
        """Total disk failure cannot be made durable; it must not claim to be. This runs
        the PRODUCTION sequence - no manual call to a private recovery method."""
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", {"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                      "engine_version": paper.SIGNAL_ENGINE_VERSION})
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("outcome", {"id": "s1", "horizon": "5",
                                           "outcome": {"net_return_pct": 4.0}})
        self.assertEqual(len(bot._archive_outbox), 1)
        self.assertFalse(os.path.exists(ap + ".outbox"))    # honest: nothing reached disk
        self.assertIn("memory only", bot.outbox_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_a_normal_save_retries_a_stranded_outbox(self):
        """Recovery must not require another archive event to happen along."""
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", {"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                      "engine_version": paper.SIGNAL_ENGINE_VERSION})
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("outcome", {"id": "s1", "horizon": "5",
                                           "outcome": {"net_return_pct": 4.0}})
        bot._save()                                          # the ordinary save path
        # Recovery must COMPLETE, not merely make the queue durable: the event belongs
        # in the archive, the queue empty, its file gone and the errors cleared.
        self.assertTrue(os.path.exists(ap))
        self.assertEqual(bot._archive_outbox, [])
        self.assertFalse(os.path.exists(ap + ".outbox"))
        self.assertEqual(bot.archive_write_error, "")
        self.assertEqual(bot.outbox_error, "")
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(len(restarted._archive_outbox), 0)

    def test_a_corrupt_outbox_is_quarantined_and_blocks(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            f.write("{not json\n")
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        import glob
        self.assertIn("quarantined", bot.quarantine_error)
        # unique name: a second quarantine must not overwrite the first
        self.assertEqual(len(glob.glob(ap + ".outbox.corrupt.*")), 1)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

        # and the block must SURVIVE a restart - renaming the file once used to end it
        restarted = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                             archive_path=ap)
        self.assertIn("quarantine", restarted.quarantine_error)
        self.assertEqual(restarted.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_a_flushed_outbox_updates_the_store_without_a_restart(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", {"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                      "ticker": "A",
                                      "engine_version": paper.SIGNAL_ENGINE_VERSION})
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("outcome", {"id": "s1", "horizon": "5",
                                           "outcome": {"net_return_pct": 4.0},
                                           "resolved": True})
        bot._archive_event("signal", {                       # flushes the queue
            "id": "s2", paper.SIGNAL_DAY_FIELD: "2026-01-03",
            "engine_version": paper.SIGNAL_ENGINE_VERSION})
        rows = {r["id"]: r for r in bot.evidence_rows()}
        self.assertIn("5", rows["s1"].get("outcomes") or {})  # visible without restart

    def test_benchmark_legs_are_unioned_like_outcomes(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "signal", "id": "s1",
                                paper.SIGNAL_DAY_FIELD: "2026-01-02", "ticker": "A",
                                "engine_version": paper.SIGNAL_ENGINE_VERSION}) + "\n")
            f.write(json.dumps({"event": "benchmark", "id": "s1",
                                "legs": {"1": 0.1, "5": 0.5}}) + "\n")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"signal_log": [{"id": "s1", paper.SIGNAL_DAY_FIELD: "2026-01-02",
                                       "engine_version": paper.SIGNAL_ENGINE_VERSION,
                                       paper.SIGNAL_BENCHMARK_FIELD: {
                                           "1": 0.1, "5": 0.5, "10": 1.0}}]}, f)
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        legs = bot.evidence_rows()[0].get(paper.SIGNAL_BENCHMARK_FIELD) or {}
        self.assertEqual(sorted(legs), ["1", "10", "5"])   # cache-only 10 survives


class TestExecutionRealisticMeasurement(unittest.TestCase):
    """A long is bought at the ASK and sold at the BID.

    The old model observed neither side: it ran Yahoo's last TRADE to a future close and
    subtracted the entry-time spread once, as though the whole round trip had been paid
    on the way in. Half the cost was never charged and no exit book was ever looked at.
    """

    DAYS = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
            "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15",
            "2026-01-16", "2026-01-20"]
    HORIZON_5_DAY = DAYS[5]           # signal day is DAYS[0]; five sessions later

    @classmethod
    def _frame(cls, closes):
        import pandas as pd
        return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                             "Close": closes, "Volume": [1_000_000] * len(closes)},
                            index=pd.to_datetime(cls.DAYS))

    def _yf(self, closes):
        outer = self

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, **kw):
                if self.symbol == "IWM":
                    return outer._frame([100.0] * len(outer.DAYS))
                return outer._frame(closes)

        return mock.Mock(Ticker=FakeTicker)

    @staticmethod
    def _signal(**extra):
        """The exact snapshot from the report: last trade 1.00 on a 1.00/1.20 book."""
        half = 100 * 0.10 / 1.10
        row = {"id": "sig1", "t": 0.0,
               paper.SIGNAL_DAY_FIELD: TestExecutionRealisticMeasurement.DAYS[0],
               "ticker": "PENNY", "engine_version": paper.SIGNAL_ENGINE_VERSION,
               "measurement_schema": paper.MEASUREMENT_SCHEMA_VERSION,
               "price": 1.00, "entry_last_trade": 1.00,
               "entry_bid": 1.00, "entry_ask": 1.20, "entry_quote_mid": 1.10,
               "entry_half_spread_pct": half,
               # a venue book taken in the regular session, so the ENTRY leg is
               # evidentiary and the tests below isolate the exit leg
               # 10:00 NY on the signal session, received three seconds later
               "entry_quote_feed": "sip", "entry_quote_source": "alpaca",
               "entry_quote_captured_at": "2026-01-02T15:00:00+00:00",
               "entry_quote_received_at": "2026-01-02T15:00:03+00:00",
               paper.SIGNAL_COST_FIELD: 2 * half,
               "benchmark_ticker": "IWM", "benchmark_price": 100.0,
               "outcomes": {}, "resolved": False}
        row.update(extra)
        return row

    def _measure(self, row, closes=None):
        closes = closes or ([1.00] + [1.20] * (len(self.DAYS) - 1))
        bot = seed_calendar(isolated_bot(tempfile.mkdtemp()),
                            {day: 16 * 60 for day in self.DAYS})
        bot.signal_log = [row]
        bot._evidence[row["id"]] = row
        with mock.patch.object(research, "yf", self._yf(closes)):
            asyncio.run(bot._update_signal_outcomes())
        return bot, row

    def test_the_last_trade_entry_basis_flipped_a_loss_into_a_gain(self):
        """The reported reproduction, driven through the real outcome path.

        last trade $1.00, book $1.00/$1.20, future close $1.20, executable exit $1.19
        bid. The tracker booked +1.82% net; the ask->bid round trip is -0.83%.
        """
        _bot, row = self._measure(self._signal())
        out = row["outcomes"]["5"]
        executable = (1.19 / 1.20 - 1) * 100                   # -0.83%
        self.assertEqual(out["entry_basis"], "ask")
        self.assertAlmostEqual(out["entry_price"], 1.20, places=6)
        self.assertLess(out["net_return_pct"], 0.0)            # no longer a gain
        self.assertLessEqual(out["net_return_pct"], executable)  # and never optimistic
        # the specific wrong number must not come back
        self.assertNotAlmostEqual(out["net_return_pct"], 1.82, places=2)
        # gross is now a pure mid-to-close price move, charging nothing
        self.assertAlmostEqual(out["gross_return_pct"],
                               round((1.20 / 1.10 - 1) * 100, 2), places=2)

    # 16:00 New York on the horizon session, in January, is 21:00 UTC.
    CLOSING_STAMP = "2026-01-09T21:00:00+00:00"

    def _closing_quote(self, day_stamp=None, **extra):
        obs = {"session_day": self.HORIZON_5_DAY, "at": day_stamp or self.CLOSING_STAMP,
               "bid": 1.19, "ask": 1.21, "feed": "sip", "source": "alpaca"}
        obs.update(extra)
        return obs

    def test_an_observed_exit_bid_measures_the_real_round_trip(self):
        _bot, row = self._measure(
            self._signal(quote_observations=[self._closing_quote()]))
        out = row["outcomes"]["5"]
        self.assertEqual(out["exit_basis"], "observed_bid")
        self.assertAlmostEqual(out["exit_price"], 1.19, places=6)
        self.assertAlmostEqual(out["net_return_pct"],
                               round((1.19 / 1.20 - 1) * 100, 2), places=2)
        self.assertTrue(out["exit_cost_evidentiary"])
        self.assertTrue(out["entry_cost_evidentiary"])
        self.assertTrue(paper.PennyStockPaperBot.outcome_is_evidentiary(out))
        self.assertEqual(out["exit_quote_at"], self.CLOSING_STAMP)
        self.assertEqual(out["exit_session"], self.HORIZON_5_DAY)

    def test_a_prior_session_book_can_never_settle_this_horizon(self):
        """A Jan-8 close is another day's price. Accepting one at a one-session
        tolerance let it settle the Jan-9 horizon and be stamped evidentiary."""
        _bot, row = self._measure(self._signal(quote_observations=[
            self._closing_quote(session_day=self.DAYS[4],
                                day_stamp="2026-01-08T21:00:00+00:00")]))
        out = row["outcomes"]["5"]
        self.assertEqual(out["exit_basis"], "modeled_bound_on_close")
        self.assertFalse(out["exit_cost_evidentiary"])
        self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(out))

    def test_an_opening_quote_is_not_a_closing_exit(self):
        """09:31 is not the close, however well it is timestamped: the outcome it
        would be compared against is the daily Close of that session."""
        _bot, row = self._measure(self._signal(quote_observations=[
            self._closing_quote(day_stamp="2026-01-09T14:31:00+00:00")]))
        out = row["outcomes"]["5"]
        self.assertEqual(out["exit_basis"], "modeled_bound_on_close")
        self.assertFalse(out["exit_cost_evidentiary"])

    def test_a_crossed_exit_book_is_refused_by_the_leg_itself(self):
        """valid_event should stop this, and exit_leg must not depend on it having."""
        _bot, row = self._measure(self._signal(quote_observations=[
            self._closing_quote(bid=2.00, ask=1.00)]))
        out = row["outcomes"]["5"]
        self.assertEqual(out["exit_basis"], "modeled_bound_on_close")
        self.assertNotAlmostEqual(out["exit_price"], 2.00, places=6)
        self.assertFalse(out["exit_cost_evidentiary"])

    def test_a_yahoo_entry_book_is_not_evidence_however_good_the_exit(self):
        """Yahoo's freshness comes from the last TRADE timestamp, not the bid/ask, so
        it cannot establish what a purchase would have paid."""
        row = self._signal(entry_quote_feed="yfinance", entry_quote_source="yfinance",
                           quote_observations=[self._closing_quote()])
        _bot, row = self._measure(row)
        out = row["outcomes"]["5"]
        self.assertTrue(out["exit_cost_evidentiary"])       # the exit really was seen
        self.assertFalse(out["entry_cost_evidentiary"])     # the entry was not
        self.assertFalse(out["cost_evidentiary"])
        self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(out))

    def test_a_later_session_exit_is_its_own_horizon_not_this_one(self):
        """A Jan-12 book measures a longer hold. It is recorded under its own key so it
        can never be pooled into the 5-session basket it did not measure."""
        _bot, row = self._measure(self._signal(quote_observations=[
            self._closing_quote(session_day=self.DAYS[6],
                                day_stamp="2026-01-12T21:00:00+00:00")]))
        self.assertEqual(row["outcomes"]["5"]["exit_basis"], "modeled_bound_on_close")
        delayed = row["outcomes"]["5" + paper.DELAYED_EXIT_SUFFIX]
        self.assertEqual(delayed["horizon_basis"], "delayed_exit")
        self.assertEqual(delayed["exit_session"], self.DAYS[6])
        self.assertEqual(delayed["exit_delay_sessions"], 1)
        self.assertAlmostEqual(delayed["net_return_pct"],
                               round((1.19 / 1.20 - 1) * 100, 2), places=2)
        # the basket for horizon 5 looks up "5" exactly, so it cannot pick this up
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [row]
        book = bot.daily_baskets("5")
        self.assertEqual(book["cost_modeled_rows"], 1)

    def test_a_modeled_exit_is_conservative_and_not_evidentiary(self):
        _bot, row = self._measure(self._signal())
        out = row["outcomes"]["5"]
        self.assertEqual(out["exit_basis"], "modeled_bound_on_close")
        self.assertFalse(out["cost_evidentiary"])
        self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(out))
        # never a credit: at least the entry half-spread, which is the floor
        self.assertGreaterEqual(out["exit_cost_pct"],
                                row["entry_half_spread_pct"] - 1e-9)

    def test_a_row_with_no_ask_is_not_measured_at_all(self):
        """An unknown entry price is not a licence to fall back to the last trade."""
        row = self._signal(entry_ask=None, ask=None)
        _bot, row = self._measure(row)
        self.assertEqual(row["outcomes"], {})
        self.assertIn("no entry ask", row["measurement_blocked"])

    def test_a_legacy_row_is_measured_from_its_recorded_ask(self):
        """Rows predate entry_ask but did record the raw book: reconstruct, not discard."""
        legacy = {"id": "old1", "t": 0.0, paper.SIGNAL_DAY_FIELD: self.DAYS[0],
                  "ticker": "PENNY", "engine_version": paper.SIGNAL_ENGINE_VERSION,
                  "price": 1.00, "bid": 1.00, "ask": 1.20,
                  paper.SIGNAL_COST_FIELD: 2 * 100 * 0.10 / 1.10,
                  "benchmark_price": 100.0, "outcomes": {}, "resolved": False}
        _bot, row = self._measure(legacy)
        out = row["outcomes"]["5"]
        self.assertAlmostEqual(out["entry_price"], 1.20, places=6)
        self.assertEqual(out["measurement_schema"], paper.MEASUREMENT_SCHEMA_VERSION)

    def test_modeled_exit_costs_block_a_positive_verdict(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot.signal_log = [{
            "ticker": f"T{d % 35}", paper.SIGNAL_DAY_FIELD: f"day-{d:03d}",
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            "outcomes": {"5": measured_outcome(2.0, 1.0, cost_evidentiary=False,
                                               exit_cost_evidentiary=False,
                                               exit_basis="modeled_bound_on_close")},
        } for d in range(70)]
        verdict = bot.forward_validation()
        self.assertEqual(verdict["status"], "DATA_INCOMPLETE")
        self.assertIn("exit leg", verdict["reason"])
        self.assertFalse(verdict["auto_trade_allowed"])

        # the same baskets with an observed exit are promising - and still locked
        for row in bot.signal_log:
            row["outcomes"]["5"] = measured_outcome(2.0, 1.0)
        verdict = bot.forward_validation()
        self.assertEqual(verdict["status"], "PROMISING_NOT_VALIDATED")
        self.assertFalse(verdict["auto_trade_allowed"])

    def test_an_outcome_from_an_earlier_schema_is_never_evidentiary(self):
        """Repairing the measurement must not re-bless what a broken one produced.

        Schema 1 had ask-based arithmetic, so its numbers look plausible - but it took a
        prior session's book as a close, trusted a crossed quote and never checked the
        entry feed, so its stamp means nothing here.
        """
        unversioned = {"net_return_pct": 2.0, "cost_evidentiary": True}
        schema_one = {"net_return_pct": 2.0, "measurement_schema": 1,
                      "cost_evidentiary": True, "exit_cost_evidentiary": True,
                      "entry_cost_evidentiary": True}
        self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(unversioned))
        self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(schema_one))
        self.assertTrue(paper.PennyStockPaperBot.outcome_is_evidentiary(
            measured_outcome(2.0, 1.0)))

    def test_either_leg_alone_is_not_an_observed_round_trip(self):
        for missing in ("entry_cost_evidentiary", "exit_cost_evidentiary"):
            with self.subTest(missing=missing):
                self.assertFalse(paper.PennyStockPaperBot.outcome_is_evidentiary(
                    measured_outcome(2.0, 1.0, **{missing: False})))

    def test_only_completed_sessions_may_resolve_a_horizon(self):
        """The current bar moves until the close, and an outcome written from it is
        never revisited."""
        import datetime as dt
        days = ["2026-01-07", "2026-01-08", "2026-01-09"]
        during = dt.datetime(2026, 1, 9, 11, 0, tzinfo=paper.NY)
        after = dt.datetime(2026, 1, 9, 16, 25, tzinfo=paper.NY)
        just_closed = dt.datetime(2026, 1, 9, 16, 5, tzinfo=paper.NY)
        self.assertEqual(paper.PennyStockPaperBot.completed_sessions(days, during), 2)
        self.assertEqual(paper.PennyStockPaperBot.completed_sessions(days, after), 3)
        # the close alone is not enough; the provider still has to publish the bar
        self.assertEqual(
            paper.PennyStockPaperBot.completed_sessions(days, just_closed), 2)

    def test_a_partial_session_does_not_resolve_the_final_horizon(self):
        """Driven through the real path: with today's bar still forming, the horizon it
        would complete stays unmeasured rather than being fixed to a partial close."""
        import datetime as dt
        row = self._signal(quote_observations=[self._closing_quote()])
        mid_session = dt.datetime(2026, 1, 9, 11, 0, tzinfo=paper.NY)
        real = paper.PennyStockPaperBot.completed_sessions
        with mock.patch.object(paper.PennyStockPaperBot, "completed_sessions",
                               staticmethod(lambda dates, now=None:
                                            real(dates, mid_session))):
            _bot, row = self._measure(row)
        self.assertNotIn("5", row["outcomes"])          # Jan-9 bar is not final yet
        self.assertIn("1", row["outcomes"])             # earlier horizons still resolve

    def test_the_engine_version_is_untouched_by_a_measurement_repair(self):
        self.assertEqual(paper.SIGNAL_ENGINE_VERSION, 7)
        self.assertEqual(paper.MEASUREMENT_SCHEMA_VERSION, 2)


class TestQuoteTelemetryAndProxyAudit(unittest.TestCase):
    """Exit books must be captured for every tracked name, and the feed they came from
    is part of the observation."""

    @staticmethod
    def _quote(**extra):
        row = {"event": "quote", "id": "s1", "session_day": "2026-01-05",
               "at": "2026-01-05T20:00:00+00:00", "bid": 1.0, "ask": 1.1,
               "feed": "iex"}
        row.update(extra)
        return row

    def test_a_quote_event_requires_its_feed_identity(self):
        self.assertTrue(paper.PennyStockPaperBot.valid_event(self._quote()))
        self.assertFalse(paper.PennyStockPaperBot.valid_event(self._quote(feed="")))
        self.assertFalse(paper.PennyStockPaperBot.valid_event(self._quote(bid=0)))
        self.assertFalse(paper.PennyStockPaperBot.valid_event(self._quote(at="")))
        self.assertFalse(paper.PennyStockPaperBot.valid_event(
            self._quote(feed="some-vendor")))          # unrecognised feed

    def test_a_crossed_or_locked_book_is_not_a_valid_quote_event(self):
        """bid 2.00 / ask 1.00 is not a wide quote, it is a broken one - and it was
        taken as an exit price of 2.00 and stamped evidentiary."""
        self.assertFalse(paper.PennyStockPaperBot.valid_event(
            self._quote(bid=2.00, ask=1.00)))          # crossed
        self.assertFalse(paper.PennyStockPaperBot.valid_event(
            self._quote(bid=1.00, ask=1.00)))          # locked

    def test_a_quote_whose_stamp_disagrees_with_its_session_is_refused(self):
        """The session is what the EXCHANGE stamp says. A stale book relabelled into
        today is fabricated evidence - and it also convinced the capture that today was
        already done, so no real book was ever taken."""
        self.assertFalse(paper.PennyStockPaperBot.valid_event(
            self._quote(session_day="2026-08-08")))
        self.assertFalse(paper.PennyStockPaperBot.valid_event(
            self._quote(at="not-a-timestamp")))

    def test_session_times_are_read_in_market_time(self):
        # 21:00 UTC in January is 16:00 in New York
        self.assertEqual(penny_quotes.session_date("2026-01-09T21:00:00+00:00"),
                         "2026-01-09")
        self.assertTrue(penny_quotes.in_closing_window("2026-01-09T21:00:00+00:00"))
        self.assertFalse(penny_quotes.in_closing_window("2026-01-09T14:31:00+00:00"))
        self.assertTrue(penny_quotes.in_regular_session("2026-01-09T14:31:00+00:00"))
        # a stamp just past midnight UTC still belongs to the previous NY session
        self.assertEqual(penny_quotes.session_date("2026-01-10T00:30:00+00:00"),
                         "2026-01-09")
        # Alpaca stamps nanoseconds; the parser must not choke on them
        self.assertEqual(penny_quotes.session_date("2026-01-09T21:00:00.123456789Z"),
                         "2026-01-09")
        self.assertIsNone(penny_quotes.session_date("garbage"))
        self.assertIsNone(penny_quotes.session_date(""))

    def test_yahoo_is_not_an_execution_feed(self):
        self.assertTrue(penny_quotes.is_execution_feed("sip"))
        self.assertTrue(penny_quotes.is_execution_feed("iex"))
        self.assertFalse(penny_quotes.is_execution_feed("yfinance"))
        self.assertTrue(penny_quotes.is_known_feed("yfinance"))   # known, not executable

    def test_replaying_a_quote_twice_records_one_observation(self):
        store = {}
        fold = paper.PennyStockPaperBot._fold_evidence_event
        fold(store, {"event": "signal", "id": "s1",
                     paper.SIGNAL_DAY_FIELD: "2026-01-02",
                     "engine_version": paper.SIGNAL_ENGINE_VERSION})
        self.assertTrue(fold(store, self._quote()))
        self.assertTrue(fold(store, self._quote()))
        self.assertEqual(len(store["s1"]["quote_observations"]), 1)

    def test_a_quote_with_no_preceding_signal_is_an_orphan(self):
        self.assertFalse(paper.PennyStockPaperBot._fold_evidence_event(
            {}, self._quote()))

    def test_iex_is_never_recorded_as_the_nbbo(self):
        """Free Alpaca data is one venue. Calling it NBBO would misdescribe every
        spread measured from it."""
        self.assertFalse(penny_quotes.is_consolidated("iex"))
        self.assertTrue(penny_quotes.is_consolidated("sip"))
        described = penny_quotes.feed_description("iex")
        self.assertIn("NOT", described.upper())
        self.assertIn("single venue", described.lower())

    def test_a_one_sided_or_crossed_book_is_not_an_observation(self):
        good = penny_quotes._observation("ABCD", {"bp": 1.0, "ap": 1.1, "t": "T"}, "iex")
        self.assertIsNotNone(good)
        self.assertFalse(good["is_consolidated"])
        self.assertAlmostEqual(good["half_spread_pct"], 0.1 / 2 / 1.05 * 100, places=6)
        for bad in ({"bp": 0, "ap": 1.1, "t": "T"},        # one-sided
                    {"bp": 1.2, "ap": 1.1, "t": "T"},      # crossed
                    {"bp": 1.0, "ap": 1.1, "t": ""}):      # untimestamped
            self.assertIsNone(penny_quotes._observation("ABCD", bad, "iex"))

    def test_the_proxy_audit_reports_understatement_not_just_error(self):
        """Understating the cost is the dangerous direction: it admits a name at a
        price nobody could have traded."""
        records = [{"proxy_pct": 1.0, "observed_pct": 5.0, "price": 2.0,
                    "dollar_volume": 3_000_000, "feed": "sip",
                    "at": "2026-01-05T15:00:00+00:00"} for _ in range(10)]
        audit = penny_quotes.adv_proxy_audit(records, min_per_bucket=20)
        self.assertEqual(audit["observations"], 10)
        self.assertFalse(audit["calibrated"])
        self.assertAlmostEqual(audit["overall"]["median_bias_pts"], -4.0, places=6)
        self.assertEqual(audit["overall"]["understated_share"], 1.0)
        self.assertAlmostEqual(audit["overall"]["p95_understatement_pts"], 4.0, places=6)
        self.assertIn("$2-10M", audit["by_dollar_volume"])
        self.assertIn("sip", audit["by_feed"])
        self.assertTrue(audit["underpowered_buckets"])          # 10 observations < 20

    def test_the_audit_refuses_to_score_an_empty_sample(self):
        audit = penny_quotes.adv_proxy_audit([])
        self.assertEqual(audit["observations"], 0)
        self.assertFalse(audit["calibrated"])
        self.assertIn("assumption", audit["reason"])

    def test_the_bot_audit_reads_observations_off_the_evidence_store(self):
        bot = isolated_bot(tempfile.mkdtemp())
        bot._evidence = {"s1": {
            "id": "s1", "ticker": "PENNY", "price": 2.0, "dollar_volume": 3_000_000,
            "adv_proxy_pct": 1.20,
            "quote_observations": [{"at": "2026-01-05T15:00:00+00:00",
                                    "spread_pct": 6.0, "feed": "iex"}],
        }}
        audit = bot.adv_proxy_audit(min_per_bucket=1)
        self.assertEqual(audit["observations"], 1)
        self.assertAlmostEqual(audit["overall"]["median_bias_pts"], -4.8, places=6)

    def test_capture_without_alpaca_says_so_instead_of_silently_skipping(self):
        bot = isolated_bot(tempfile.mkdtemp())
        with mock.patch.object(penny_quotes, "configured", return_value=False):
            asyncio.run(bot._capture_exit_quotes())
        self.assertIn("not configured", bot.quote_capture_error)
        self.assertIn("modeled bound", bot.quote_capture_error)

    def test_capture_covers_tracked_names_that_left_the_board(self):
        """The names that stop ranking carry the losses; measuring only the survivors
        is how a strategy measures itself into looking good."""
        bot = isolated_bot(tempfile.mkdtemp())
        today = paper.datetime.now(paper.NY).strftime("%Y-%m-%d")
        bot._evidence = {
            "s1": {"id": "s1", "ticker": "OFFBOARD", paper.SIGNAL_DAY_FIELD: today,
                   "engine_version": paper.SIGNAL_ENGINE_VERSION,
                   "resolved": False, "outcomes": {}},
            "s2": {"id": "s2", "ticker": "DONE", paper.SIGNAL_DAY_FIELD: today,
                   "engine_version": paper.SIGNAL_ENGINE_VERSION,
                   "resolved": True, "outcomes": {}},
        }
        bot.watchlist = []                      # nothing is on the board any more
        asked = {}
        # A real closing stamp for TODAY. The earlier version of this test used a fixed
        # January timestamp while the session label came from the wall clock, so it
        # agreed with the very bug it was meant to cover.
        closing = paper.datetime.now(paper.NY).replace(
            hour=16, minute=0, second=0, microsecond=0).isoformat()

        async def fake_quotes(symbols):
            asked["symbols"] = list(symbols)
            return {"OFFBOARD": {"ticker": "OFFBOARD", "bid": 1.0, "ask": 1.1,
                                 "at": closing, "feed": "iex",
                                 "source": "alpaca"}}, ""

        with mock.patch.object(penny_quotes, "configured", return_value=True), \
                mock.patch.object(penny_quotes, "latest_quotes", fake_quotes):
            asyncio.run(bot._capture_exit_quotes())
        self.assertEqual(asked["symbols"], ["OFFBOARD"])       # resolved name skipped
        observations = bot._evidence["s1"]["quote_observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["session_day"], today)
        self.assertEqual(observations[0]["at"], closing)

        # the closing book for today exists, so a second pass adds nothing
        with mock.patch.object(penny_quotes, "configured", return_value=True), \
                mock.patch.object(penny_quotes, "latest_quotes", fake_quotes):
            asyncio.run(bot._capture_exit_quotes())
        self.assertEqual(len(bot._evidence["s1"]["quote_observations"]), 1)

    def test_a_stale_book_is_rejected_rather_than_relabelled_as_today(self):
        """The reported reproduction: a book stamped 2026-01-05T14:31:00Z was archived
        under today's date, and then blocked any further capture that session."""
        bot = isolated_bot(tempfile.mkdtemp())
        today = paper.datetime.now(paper.NY).strftime("%Y-%m-%d")
        bot._evidence = {"s1": {"id": "s1", "ticker": "PENNY",
                                paper.SIGNAL_DAY_FIELD: today,
                                "engine_version": paper.SIGNAL_ENGINE_VERSION,
                                "resolved": False, "outcomes": {}}}

        async def stale(symbols):
            return {"PENNY": {"ticker": "PENNY", "bid": 1.0, "ask": 1.1,
                              "at": "2026-01-05T14:31:00Z", "feed": "iex",
                              "source": "alpaca"}}, ""

        with mock.patch.object(penny_quotes, "configured", return_value=True), \
                mock.patch.object(penny_quotes, "latest_quotes", stale):
            asyncio.run(bot._capture_exit_quotes())
        self.assertEqual(bot._evidence["s1"].get("quote_observations", []), [])
        self.assertIn("stamped 2026-01-05", bot.quote_capture_error)

    def test_capture_keeps_trying_until_the_closing_book_exists(self):
        """An opening snapshot must not end the session's capture: it cannot serve as
        the closing exit, so stopping there leaves the horizon unmeasurable."""
        bot = isolated_bot(tempfile.mkdtemp())
        today = paper.datetime.now(paper.NY)
        day = today.strftime("%Y-%m-%d")
        bot._evidence = {"s1": {
            "id": "s1", "ticker": "PENNY", paper.SIGNAL_DAY_FIELD: day,
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            "resolved": False, "outcomes": {},
            "quote_observations": [{
                "session_day": day, "feed": "iex", "bid": 1.0, "ask": 1.1,
                "at": today.replace(hour=9, minute=45, second=0,
                                    microsecond=0).isoformat()}],
        }}
        closing = today.replace(hour=16, minute=0, second=0, microsecond=0).isoformat()

        async def at_close(symbols):
            return {"PENNY": {"ticker": "PENNY", "bid": 1.19, "ask": 1.21,
                              "at": closing, "feed": "iex", "source": "alpaca"}}, ""

        with mock.patch.object(penny_quotes, "configured", return_value=True), \
                mock.patch.object(penny_quotes, "latest_quotes", at_close):
            asyncio.run(bot._capture_exit_quotes())
        stamps = [o["at"] for o in bot._evidence["s1"]["quote_observations"]]
        self.assertEqual(len(stamps), 2)                # the open one was not enough
        self.assertIn(closing, stamps)


class TestClosingCaptureSchedule(unittest.TestCase):
    """The closing book exists for ten minutes a day and cannot be fetched afterwards.

    Riding the hourly outcome timer meant a pass at 15:54 and the next at 16:54 skipped
    the window entirely, and that loss is permanent.
    """

    import datetime as dt

    def _bot_with_tracked_signal(self, day=None, observations=None):
        bot = isolated_bot(tempfile.mkdtemp())
        day = day or paper.datetime.now(paper.NY).strftime("%Y-%m-%d")
        bot._evidence = {"s1": {
            "id": "s1", "ticker": "PENNY", paper.SIGNAL_DAY_FIELD: day,
            "engine_version": paper.SIGNAL_ENGINE_VERSION, "resolved": False,
            "outcomes": {}, "quote_observations": observations or []}}
        return bot

    def test_capture_fires_inside_a_window_the_hourly_timer_would_miss(self):
        """Hourly passes at 15:54 and 16:54 straddle the window; this must not."""
        bot = self._bot_with_tracked_signal(day="2026-01-09")
        close = 16 * 60
        missed = [self.dt.datetime(2026, 1, 9, 15, 54, tzinfo=paper.NY),
                  self.dt.datetime(2026, 1, 9, 16, 54, tzinfo=paper.NY)]
        for when in missed:
            self.assertFalse(
                penny_quotes.in_closing_window(when.isoformat(), close),
                f"{when:%H:%M} should be outside the window")
        # ... yet the capture scheduler still runs inside it
        for when in (self.dt.datetime(2026, 1, 9, 15, 56, tzinfo=paper.NY),
                     self.dt.datetime(2026, 1, 9, 16, 0, tzinfo=paper.NY)):
            with self.subTest(at=f"{when:%H:%M}"):
                self.assertTrue(bot.closing_capture_due(1000.0, when, close))

    def test_capture_starts_before_the_window_and_stops_after_it(self):
        bot = self._bot_with_tracked_signal(day="2026-01-09")
        close = 16 * 60
        # 15:41 is the load-bearing case: the lead runs from the CLOSE, so a 15-minute
        # lead starts at 15:45. Subtracting the window width as well started it at 15:40.
        cases = {"11:00": False, "15:39": False, "15:41": False, "15:44": False,
                 "15:45": True, "15:59": True, "16:05": True, "16:06": False,
                 "16:54": False}
        for clock, expected in cases.items():
            hour, minute = (int(x) for x in clock.split(":"))
            when = self.dt.datetime(2026, 1, 9, hour, minute, tzinfo=paper.NY)
            with self.subTest(at=clock):
                self.assertEqual(bot.closing_capture_due(1000.0, when, close), expected)

    def test_capture_retries_on_its_own_clock_not_the_outcome_timer(self):
        bot = self._bot_with_tracked_signal(day="2026-01-09")
        close, when = 16 * 60, self.dt.datetime(2026, 1, 9, 15, 58, tzinfo=paper.NY)
        self.assertTrue(bot.closing_capture_due(1000.0, when, close))
        bot._last_closing_capture = 1000.0
        # a few seconds later is too soon, but well inside the hourly timer
        self.assertFalse(bot.closing_capture_due(1010.0, when, close))
        self.assertTrue(bot.closing_capture_due(
            1000.0 + paper.CLOSING_CAPTURE_RETRY_SEC + 1, when, close))
        self.assertLessEqual(paper.CLOSING_CAPTURE_RETRY_SEC, 60)

    def test_capture_stops_once_every_tracked_name_has_its_closing_book(self):
        close = 16 * 60
        when = self.dt.datetime(2026, 1, 9, 15, 58, tzinfo=paper.NY)
        bot = self._bot_with_tracked_signal(day="2026-01-09", observations=[{
            "session_day": "2026-01-09", "at": "2026-01-09T21:00:00+00:00",
            "bid": 1.0, "ask": 1.1, "feed": "iex"}])
        self.assertFalse(bot.closing_capture_due(1000.0, when, close))
        # an opening quote does not count as the closing book
        bot = self._bot_with_tracked_signal(day="2026-01-09", observations=[{
            "session_day": "2026-01-09", "at": "2026-01-09T14:31:00+00:00",
            "bid": 1.0, "ask": 1.1, "feed": "iex"}])
        self.assertTrue(bot.closing_capture_due(1000.0, when, close))

    def test_an_early_close_session_uses_its_real_window(self):
        """US markets close at 13:00 on half days. A hardcoded 16:00 window would miss
        every one of those sessions."""
        half_day = 13 * 60
        self.assertTrue(penny_quotes.in_closing_window(
            "2026-11-27T18:00:00+00:00", half_day))          # 13:00 NY
        self.assertFalse(penny_quotes.in_closing_window(
            "2026-11-27T18:00:00+00:00", 16 * 60))           # missed at 16:00
        bot = self._bot_with_tracked_signal(day="2026-11-27")
        when = self.dt.datetime(2026, 11, 27, 12, 58, tzinfo=paper.NY)
        self.assertTrue(bot.closing_capture_due(1000.0, when, half_day))
        self.assertFalse(bot.closing_capture_due(1000.0, when, 16 * 60))

    def test_the_close_comes_from_the_provider_before_any_assumption(self):
        minute, source = penny_quotes.scheduled_close_minute(
            self.dt.date(2026, 11, 27), payload={"close": "2026-11-27T18:00:00+00:00"})
        self.assertEqual((minute, source), (13 * 60, "provider"))
        # A close belonging to the NEXT session is not this session's close. Taking it
        # turned a 13:00 half day into 16:00 and opened the window hours too late.
        self.assertEqual(
            penny_quotes.scheduled_close_minute(
                self.dt.date(2026, 11, 27),
                payload={"close": "2026-11-30T21:00:00+00:00"}),
            (13 * 60, "calendar"))
        # no payload: the recurring half-day rules, then 16:00
        self.assertEqual(
            penny_quotes.scheduled_close_minute(self.dt.date(2026, 11, 27)),
            (13 * 60, "calendar"))
        self.assertEqual(
            penny_quotes.scheduled_close_minute(self.dt.date(2026, 1, 9)),
            (16 * 60, "default"))

    def test_the_recurring_half_days_are_recognised(self):
        # 2026: Thanksgiving is 26 Nov, so the 27th closes early; Christmas Eve is a
        # Thursday; 3 July is a Friday with the 4th on a Saturday, so it is a full day
        self.assertTrue(penny_quotes.is_us_half_day(self.dt.date(2026, 11, 27)))
        self.assertTrue(penny_quotes.is_us_half_day(self.dt.date(2026, 12, 24)))
        self.assertFalse(penny_quotes.is_us_half_day(self.dt.date(2026, 7, 3)))
        self.assertTrue(penny_quotes.is_us_half_day(self.dt.date(2025, 7, 3)))
        self.assertFalse(penny_quotes.is_us_half_day(self.dt.date(2026, 1, 9)))
        # 2027-12-24 is a Friday with Christmas on the Saturday, so the 24th IS the
        # observed holiday - a full closure, not a 13:00 session. Calling it a half day
        # opens a capture window on a dark market.
        self.assertFalse(penny_quotes.is_us_half_day(self.dt.date(2027, 12, 24)))
        self.assertFalse(penny_quotes.is_us_half_day(self.dt.date(2021, 12, 24)))

    def test_a_half_day_closing_quote_is_accepted_as_the_exit(self):
        """The close recorded WITH the observation decides, not a global 16:00."""
        row = {"quote_observations": [{
            "session_day": "2026-11-27", "at": "2026-11-27T18:00:00+00:00",
            "bid": 1.19, "ask": 1.21, "feed": "sip",
            "believed_close_minute": 13 * 60}]}
        seen = paper.PennyStockPaperBot._closing_observation(
            row, "2026-11-27", alpaca_schedule(13 * 60))
        self.assertIsNotNone(seen)
        # the same quote against a 16:00 session would not be a close at all
        self.assertIsNone(paper.PennyStockPaperBot._closing_observation(
            row, "2026-11-27", alpaca_schedule(16 * 60)))

    def test_a_quote_cannot_certify_its_own_closing_time(self):
        """An 11:00 book declaring an 11:00 close became an evidentiary closing quote.
        The close is resolved from the stored exchange calendar, never from the quote."""
        row = {"quote_observations": [{
            "session_day": "2026-01-09", "at": "2026-01-09T16:00:00+00:00",  # 11:00 NY
            "bid": 1.19, "ask": 1.21, "feed": "sip",
            "believed_close_minute": 11 * 60}]}                # "we closed at 11:00"
        self.assertIsNone(paper.PennyStockPaperBot._closing_observation(
            row, "2026-01-09", alpaca_schedule(16 * 60)))
        # and with no calendar record at all there is no close to match against
        self.assertIsNone(paper.PennyStockPaperBot._closing_observation(
            row, "2026-01-09", None))

    def test_a_guessed_schedule_never_certifies_an_exit(self):
        """A fallback close may still drive capture, but it cannot make an outcome
        evidence: nothing confirmed the quote was taken at the real close."""
        row = {"entry_bid": 1.0, "entry_ask": 1.2, "entry_half_spread_pct": 9.09,
               "quote_observations": [{
                   "session_day": "2026-01-09", "at": "2026-01-09T21:00:00+00:00",
                   "bid": 1.19, "ask": 1.21, "feed": "sip"}]}
        guessed = alpaca_schedule(16 * 60, source="fallback:default",
                                  evidentiary=False)
        leg = paper.PennyStockPaperBot.exit_leg(row, "2026-01-09", 1.20,
                                                ["2026-01-09"], guessed)
        self.assertEqual(leg["exit_basis"], "modeled_bound_on_close")
        self.assertFalse(leg["cost_evidentiary"])
        # the identical quote under an exchange-confirmed schedule is evidence
        leg = paper.PennyStockPaperBot.exit_leg(row, "2026-01-09", 1.20,
                                                ["2026-01-09"], alpaca_schedule())
        self.assertEqual(leg["exit_basis"], "observed_bid")
        self.assertTrue(leg["cost_evidentiary"])

    def test_capture_does_not_run_on_a_non_trading_day(self):
        """A date absent from the exchange calendar is a holiday. Capture must not run
        against a dark market and then report that nothing had a book."""
        bot = self._bot_with_tracked_signal(day="2026-01-09")
        holiday = {"close_minute": None, "source": "alpaca-holiday",
                   "evidentiary": True, "is_trading_day": False}
        when = self.dt.datetime(2026, 1, 9, 15, 58, tzinfo=paper.NY)
        self.assertFalse(bot.closing_capture_due(1000.0, when, schedule=holiday))

    def test_a_missing_calendar_reports_a_holiday_only_inside_covered_dates(self):
        bot = seed_calendar(isolated_bot(tempfile.mkdtemp()),
                            {"2026-01-08": 16 * 60, "2026-01-12": 16 * 60})
        listed = bot.session_schedule("2026-01-08")
        self.assertTrue(listed["is_trading_day"])
        self.assertTrue(listed["evidentiary"])
        # inside the answered range but absent -> the exchange says it is not a session
        gap = bot.session_schedule("2026-01-09")
        self.assertFalse(gap["is_trading_day"])
        self.assertTrue(gap["evidentiary"])
        # outside the range the calendar never answered for -> a guess, not evidence
        outside = bot.session_schedule("2026-03-02")
        self.assertTrue(outside["is_trading_day"])
        self.assertFalse(outside["evidentiary"])
        self.assertTrue(outside["source"].startswith("fallback:"))

    def test_a_recorded_session_is_frozen_against_a_later_refresh(self):
        """A refresh after the close must not replace today's 13:00 with tomorrow's
        16:00."""
        bot = seed_calendar(isolated_bot(tempfile.mkdtemp()), {"2026-11-27": 13 * 60})
        stored = dict(bot._session_calendar["sessions"])

        async def newer(start, end):
            return {"2026-11-27": {"open_minute": 570, "close_minute": 16 * 60}}, ""

        with mock.patch.object(penny_quotes, "fetch_calendar", newer):
            asyncio.run(bot.refresh_session_calendar(force=True))
        self.assertEqual(bot._session_calendar["sessions"]["2026-11-27"],
                         stored["2026-11-27"])
        self.assertEqual(bot.session_schedule("2026-11-27")["close_minute"], 13 * 60)

    def test_the_calendar_is_read_from_alpaca_and_survives_a_restart(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)

        async def calendar(start, end):
            return {"2026-11-27": {"open_minute": 570, "close_minute": 13 * 60}}, ""

        with mock.patch.object(penny_quotes, "fetch_calendar", calendar):
            asyncio.run(bot.refresh_session_calendar(force=True))
        self.assertEqual(bot.session_schedule("2026-11-27"),
                         {"close_minute": 13 * 60, "source": "alpaca",
                          "evidentiary": True, "is_trading_day": True})
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(
            restarted.session_schedule("2026-11-27")["close_minute"], 13 * 60)

    def test_a_failed_calendar_fetch_leaves_schedules_non_evidentiary(self):
        bot = isolated_bot(tempfile.mkdtemp())

        async def broken(start, end):
            return {}, "HTTP 403: forbidden"

        with mock.patch.object(penny_quotes, "fetch_calendar", broken):
            asyncio.run(bot.refresh_session_calendar(force=True))
        self.assertIn("403", bot.calendar_error)
        self.assertFalse(bot.session_schedule("2026-01-09")["evidentiary"])

    def test_the_capture_check_is_the_same_validator_as_the_exit_leg(self):
        """A weaker check here let capture believe a crossed, non-venue book was the
        close and stop trying, while the exit leg correctly refused it."""
        schedule = alpaca_schedule()
        for bad in ({"bid": 2.00, "ask": 1.00, "feed": "sip"},     # crossed
                    {"bid": 1.19, "ask": 1.21, "feed": "yfinance"}):   # not a venue
            row = {"quote_observations": [dict(
                bad, session_day="2026-01-09", at="2026-01-09T21:00:00+00:00")]}
            with self.subTest(book=bad):
                self.assertFalse(paper.PennyStockPaperBot._has_closing_quote(
                    row, "2026-01-09", schedule))
                self.assertIsNone(paper.PennyStockPaperBot._closing_observation(
                    row, "2026-01-09", schedule))


class TestEntryQuoteFreshness(unittest.TestCase):
    """A regular-session clock time was the only test, so a book from a PREVIOUS
    session at 10:15 passed and became an evidentiary entry."""

    SIGNAL_DAY = "2026-01-09"
    AT = "2026-01-09T15:00:00+00:00"          # 10:00 NY on the signal session
    RECEIVED = "2026-01-09T15:00:03+00:00"    # three seconds later

    def _row(self, **extra):
        row = {paper.SIGNAL_DAY_FIELD: self.SIGNAL_DAY,
               "entry_bid": 1.00, "entry_ask": 1.20, "entry_quote_feed": "sip",
               "entry_quote_captured_at": self.AT,
               "entry_quote_received_at": self.RECEIVED}
        row.update(extra)
        return row

    def test_a_fresh_same_session_venue_book_is_evidentiary(self):
        self.assertTrue(paper.PennyStockPaperBot.entry_is_evidentiary(self._row()))

    def test_a_previous_session_book_is_refused(self):
        """The reported hole: right clock time, wrong day."""
        stale = self._row(entry_quote_captured_at="2026-01-08T15:00:00+00:00",
                          entry_quote_received_at="2026-01-08T15:00:03+00:00")
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(stale))

    def test_an_over_age_book_is_refused(self):
        old = self._row(entry_quote_received_at="2026-01-09T15:09:00+00:00")   # 9 min
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(old))
        self.assertLessEqual(paper.ENTRY_QUOTE_MAX_AGE_SEC, 300)

    def test_a_future_stamped_book_is_refused(self):
        ahead = self._row(entry_quote_captured_at="2026-01-09T15:05:00+00:00",
                          entry_quote_received_at="2026-01-09T15:00:03+00:00")
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(ahead))
        # ordinary sub-second clock skew is still tolerated
        skewed = self._row(entry_quote_captured_at="2026-01-09T15:00:04+00:00",
                           entry_quote_received_at="2026-01-09T15:00:03+00:00")
        self.assertTrue(paper.PennyStockPaperBot.entry_is_evidentiary(skewed))

    def test_a_book_with_no_receipt_time_cannot_be_aged(self):
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(
            self._row(entry_quote_received_at=None)))

    def test_an_out_of_session_or_yahoo_book_is_refused(self):
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(
            self._row(entry_quote_captured_at="2026-01-09T09:00:00+00:00",
                      entry_quote_received_at="2026-01-09T09:00:03+00:00")))  # pre-open
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(
            self._row(entry_quote_feed="yfinance")))

    def test_the_recorded_flag_is_never_trusted_over_the_stored_book(self):
        """A row written by an older, laxer version must not carry a stale book in."""
        lying = self._row(entry_quote_captured_at="2026-01-08T15:00:00+00:00",
                          entry_quote_received_at="2026-01-08T15:00:03+00:00",
                          entry_cost_evidentiary=True)
        self.assertFalse(paper.PennyStockPaperBot.entry_is_evidentiary(lying))

    def test_the_capture_path_records_both_clocks(self):
        book = paper.PennyStockPaperBot._entry_book(
            {"bid": 0.9, "ask": 1.3, "quote_age_min": 1.0,
             "entry_quote": {"bid": 1.00, "ask": 1.20, "feed": "sip",
                             "source": "alpaca", "at": self.AT,
                             "received_at": self.RECEIVED}},
            paper.datetime.fromisoformat(self.AT).timestamp())
        self.assertEqual(book["entry_quote_captured_at"], self.AT)
        self.assertEqual(book["entry_quote_received_at"], self.RECEIVED)
        self.assertAlmostEqual(book["entry_quote_age_sec"], 3.0, places=3)
        self.assertTrue(book["entry_cost_evidentiary"])
        self.assertEqual(book["entry_ask"], 1.20)      # the venue book, not Yahoo's

    def test_a_stale_venue_book_falls_back_to_yahoo_and_is_not_evidence(self):
        book = paper.PennyStockPaperBot._entry_book(
            {"bid": 0.9, "ask": 1.3, "quote_age_min": 1.0,
             "entry_quote": {"bid": 1.00, "ask": 1.20, "feed": "sip",
                             "source": "alpaca",
                             "at": "2026-01-08T15:00:00+00:00",
                             "received_at": self.RECEIVED}},
            paper.datetime.fromisoformat(self.AT).timestamp())
        self.assertFalse(book["entry_cost_evidentiary"])
        self.assertEqual(book["entry_quote_feed"], "yfinance")
        self.assertEqual(book["entry_ask"], 1.3)       # Yahoo's, and not evidence


class TestQuarantineCannotBeLost(unittest.TestCase):
    """The marker and the .outbox.corrupt.* artifact are INDEPENDENT records of the same
    quarantine, and the damaged file itself is evidence that must survive."""

    @staticmethod
    def _outbox(tmp, *lines):
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return ap

    @staticmethod
    def _bot(tmp, ap):
        return paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                        archive_path=ap)

    @staticmethod
    def _artifacts(ap):
        import glob
        return glob.glob(ap + ".outbox.corrupt.*")

    @staticmethod
    def _blocking_open(real_open):
        def guard(path, *a, **k):
            # the staging file counts: the marker is written tmp-then-replace
            if ".quarantine" in str(path):
                raise OSError("marker write failed")
            return real_open(path, *a, **k)
        return guard

    @staticmethod
    def _blocking_replace(real_replace):
        def guard(src, dst, *a, **k):
            if ".corrupt." in str(dst):
                raise OSError("move failed")
            return real_replace(src, dst, *a, **k)
        return guard

    def test_a_failed_marker_write_does_not_erase_the_quarantine(self):
        """Startup consulted only the marker, so a failed marker write made the whole
        quarantine disappear on the next run while the damage sat on disk."""
        import builtins
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, "{not json")
        with mock.patch("builtins.open",
                        side_effect=self._blocking_open(builtins.open)):
            first = self._bot(tmp, ap)
        self.assertFalse(os.path.exists(ap + ".quarantine"))     # marker never landed
        self.assertEqual(len(self._artifacts(ap)), 1)
        self.assertEqual(first.forward_validation()["status"], "DATA_INCOMPLETE")

        restarted = self._bot(tmp, ap)
        self.assertIn("quarantine", restarted.quarantine_error)  # the artifact alone
        self.assertEqual(restarted.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_the_marker_alone_blocks_when_the_artifact_is_gone(self):
        """The other half of the same independence: clearing the artifact is not
        resolution either."""
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, "{not json")
        self._bot(tmp, ap)
        for artifact in self._artifacts(ap):
            os.remove(artifact)
        restarted = self._bot(tmp, ap)
        self.assertIn("quarantine", restarted.quarantine_error)
        self.assertEqual(restarted.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_a_failed_quarantine_move_preserves_the_corrupt_source(self):
        """It reported "damaged file left in place" and then called _persist_outbox,
        which overwrote that file with the salvage alone."""
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, json.dumps(
            {"event": "signal", "id": "keep", paper.SIGNAL_DAY_FIELD: "2026-01-01",
             "engine_version": paper.SIGNAL_ENGINE_VERSION}), "{not json")
        with mock.patch("os.replace", side_effect=self._blocking_replace(os.replace)):
            bot = self._bot(tmp, ap)
            with open(ap + ".outbox", encoding="utf-8") as f:
                body = f.read()
            self.assertIn("{not json", body)         # the damaged bytes survive
            self.assertIn("keep", body)              # and so does the salvage inside it
            self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
            # nor may the queue be retired: the preserved file still holds these events,
            # so a drain would be replayed into the archive on the next start
            self.assertFalse(bot._flush_archive_outbox())
            self.assertEqual(len(bot._archive_outbox), 1)
            with open(ap + ".outbox", encoding="utf-8") as f:
                self.assertIn("{not json", f.read())
            self.assertFalse(os.path.exists(ap))     # nothing appended to the archive

    def test_a_failed_move_does_not_delete_an_unsalvageable_outbox(self):
        """With nothing to salvage, _persist_outbox removed the file outright - erasing
        the only copy of the corrupt evidence."""
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, "{not json")
        with mock.patch("os.replace", side_effect=self._blocking_replace(os.replace)):
            bot = self._bot(tmp, ap)
            self.assertTrue(os.path.exists(ap + ".outbox"))
            with open(ap + ".outbox", encoding="utf-8") as f:
                self.assertIn("{not json", f.read())
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_a_live_event_is_held_while_the_outbox_is_quarantined_in_place(self):
        """A failed quarantine move blocks the ARCHIVE too. The damaged outbox is still
        the only copy of the queue, so a new event may not be appended, may not repair
        the tail, and may not retire anything - it joins the queue in memory."""
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, json.dumps(
            {"event": "signal", "id": "keep", paper.SIGNAL_DAY_FIELD: "2026-01-01",
             "engine_version": paper.SIGNAL_ENGINE_VERSION}), "{not json")
        with mock.patch("os.replace", side_effect=self._blocking_replace(os.replace)):
            bot = self._bot(tmp, ap)
            self.assertEqual(len(bot._archive_outbox), 1)        # the salvaged signal
            self.assertFalse(bot._flush_archive_outbox())
            held = bot.archive_write_error
            self.assertTrue(held)

            self.assertFalse(bot._archive_event("signal", {
                "id": "s2", paper.SIGNAL_DAY_FIELD: "2026-01-03",
                "engine_version": paper.SIGNAL_ENGINE_VERSION}))
            self.assertFalse(os.path.exists(ap))                 # archive never created
            self.assertEqual(len(bot._archive_outbox), 2)        # whole queue held
            self.assertEqual([x.get("id") for x in bot._archive_outbox], ["keep", "s2"])
            self.assertIn("memory only", bot.outbox_error)
            self.assertEqual(bot.archive_write_error, held)      # not cleared
            with open(ap + ".outbox", encoding="utf-8") as f:
                self.assertIn("{not json", f.read())             # source still intact
            self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

        # and once the move can complete, the held queue lands in full
        self.assertTrue(bot._flush_archive_outbox())
        with open(ap, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("keep", body)
        self.assertIn("s2", body)
        self.assertIn("quarantine", bot.quarantine_error)        # still blocking

    def test_a_blocked_outbox_recovers_once_the_move_succeeds(self):
        """A degraded disk must not strand the path forever - but the block stays."""
        tmp = tempfile.mkdtemp()
        ap = self._outbox(tmp, json.dumps(
            {"event": "signal", "id": "keep", paper.SIGNAL_DAY_FIELD: "2026-01-01",
             "engine_version": paper.SIGNAL_ENGINE_VERSION}), "{not json")
        with mock.patch("os.replace", side_effect=self._blocking_replace(os.replace)):
            bot = self._bot(tmp, ap)
        self.assertTrue(bot._persist_outbox())                # os.replace works again
        self.assertEqual(len(self._artifacts(ap)), 1)
        with open(self._artifacts(ap)[0], encoding="utf-8") as f:
            self.assertIn("{not json", f.read())             # the damage is preserved
        self.assertIn("quarantine", bot.quarantine_error)    # and still blocks
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")


class TestEventsAreValidatedBeforeAnyWrite(unittest.TestCase):
    """Both checks run before a file write: the shape of each event, and its ordering
    after a signal. Writing first and finding the damage on the next restart is how the
    archive got corrupted by the code meant to protect it."""

    @staticmethod
    def _bot(tmp):
        return paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                        archive_path=os.path.join(tmp, "a.jsonl"))

    @staticmethod
    def _signal(sid, day="2026-01-02"):
        return {"id": sid, paper.SIGNAL_DAY_FIELD: day,
                "engine_version": paper.SIGNAL_ENGINE_VERSION}

    def test_a_thin_internally_generated_event_fails_closed(self):
        tmp = tempfile.mkdtemp()
        bot = self._bot(tmp)
        self.assertFalse(bot._archive_event("signal", {"id": "thin"}))
        self.assertIn("refused", bot.archive_write_error)
        self.assertEqual(bot._archive_outbox, [])           # not queued for later either
        self.assertFalse(os.path.exists(bot.archive_path))  # and nothing was written
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_an_outcome_with_no_signal_fails_closed(self):
        tmp = tempfile.mkdtemp()
        bot = self._bot(tmp)
        self.assertFalse(bot._archive_event("outcome", {
            "id": "ghost", "horizon": "5", "outcome": {"net_return_pct": 4.0}}))
        self.assertIn("no preceding signal", bot.archive_write_error)
        self.assertFalse(os.path.exists(bot.archive_path))

    def test_a_signal_earlier_in_the_same_batch_satisfies_ordering(self):
        """The signal need not already be on disk - only ordered before its outcome."""
        tmp = tempfile.mkdtemp()
        bot = self._bot(tmp)
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("signal", self._signal("s1"))     # held, not applied
        self.assertEqual(len(bot._archive_outbox), 1)
        self.assertNotIn("s1", bot._evidence)
        self.assertTrue(bot._archive_event("outcome", {
            "id": "s1", "horizon": "5", "outcome": {"net_return_pct": 4.0}}))
        rows = {r["id"]: r for r in bot._replay_archive()}
        self.assertIn("5", rows["s1"].get("outcomes") or {})
        self.assertEqual(bot.archive_error, "")

    def test_an_orphan_in_a_restored_outbox_is_quarantined_not_appended(self):
        """It passed valid_event, so it was written and only the NEXT restart found it."""
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "outcome", "id": "ghost", "horizon": "5",
                                "outcome": {"net_return_pct": 4.0}}) + "\n")
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
        bot._save()                                    # the ordinary flush path
        self.assertIn("quarantined", bot.quarantine_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
        body = ""
        if os.path.exists(ap):
            with open(ap, encoding="utf-8") as f:
                body = f.read()
        self.assertNotIn("ghost", body)                # never reached the archive

        restarted = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                             archive_path=ap)
        self.assertEqual(restarted.archive_integrity_error, "")   # no orphan on disk
        self.assertIn("quarantine", restarted.quarantine_error)   # still blocked
        self.assertEqual(restarted.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_an_unrecordable_quarantine_retires_nothing(self):
        """With neither the artifact nor the marker on disk, dropping the bad events
        would erase the evidence and the block together - the same erasure the
        quarantine exists to prevent."""
        import builtins
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        orphan = {"event": "outcome", "id": "ghost", "horizon": "5",
                  "outcome": {"net_return_pct": 4.0}}
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            f.write(json.dumps(orphan) + "\n")
        real_open = builtins.open

        def no_quarantine_records(path, *a, **k):
            if ".corrupt." in str(path) or ".quarantine" in str(path):
                raise OSError("disk full")
            return real_open(path, *a, **k)

        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertEqual(len(bot._archive_outbox), 1)
        with mock.patch("builtins.open", side_effect=no_quarantine_records):
            self.assertFalse(bot._flush_archive_outbox())
        self.assertEqual(len(bot._archive_outbox), 1)       # still held, not dropped
        self.assertFalse(os.path.exists(ap))                # and not appended either
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

        # once the records can be written, the quarantine completes and still blocks
        self.assertFalse(bot._flush_archive_outbox())
        self.assertEqual(bot._archive_outbox, [])
        self.assertIn("quarantined", bot.quarantine_error)
        self.assertEqual(len(TestQuarantineCannotBeLost._artifacts(ap)), 1)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_a_valid_ordered_queue_still_flushes(self):
        """The new gate must block bad evidence, not ordinary recovery."""
        tmp = tempfile.mkdtemp()
        bot = self._bot(tmp)
        bot._archive_event("signal", self._signal("s1"))
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            bot._archive_event("outcome", {"id": "s1", "horizon": "5",
                                           "outcome": {"net_return_pct": 4.0}})
        self.assertEqual(len(bot._archive_outbox), 1)
        self.assertTrue(bot._flush_archive_outbox())
        self.assertEqual(bot._archive_outbox, [])
        self.assertEqual(bot.archive_error, "")
        self.assertEqual(bot.quarantine_error, "")

    def test_the_engine_version_and_trading_block_are_untouched(self):
        tmp = tempfile.mkdtemp()
        bot = self._bot(tmp)
        self.assertEqual(paper.SIGNAL_ENGINE_VERSION, 7)
        self.assertFalse(bot.forward_validation()["auto_trade_allowed"])


class TestArchiveRepairAndSchema(unittest.TestCase):
    """A tolerated tear must not eat the next write, and JSON that parses is not
    thereby a valid event."""

    def _signal(self, sid, day="2026-01-01"):
        return {"event": "signal", "id": sid, paper.SIGNAL_DAY_FIELD: day,
                "ticker": "A", "engine_version": paper.SIGNAL_ENGINE_VERSION}

    def test_an_append_after_a_torn_tail_survives_a_restart(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._signal("s1")) + "\n")
            f.write('{"event": "signal", "id": "torn"')          # unterminated
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(bot.archive_error, "")                  # tear tolerated on read
        self.assertTrue(bot._archive_event("signal", self._signal("s2", "2026-01-02")))
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        ids = {r.get("id") for r in restarted.evidence_rows()}
        self.assertEqual(restarted.archive_error, "")            # no new corruption
        self.assertIn("s2", ids)                                 # the append survived
        self.assertIn("s1", ids)
        self.assertTrue(os.path.exists(ap + ".torn"))            # fragment preserved

    def test_json_that_parses_but_is_not_an_event_is_corruption(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._signal("s1")) + "\n")
            f.write("[]\n")                                      # valid JSON, not an event
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertIn("corrupt", bot.archive_integrity_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_event_schema_requires_type_id_and_payload(self):
        v = paper.PennyStockPaperBot.valid_event
        self.assertFalse(v([]))
        self.assertFalse(v({"event": "signal"}))                   # no id
        self.assertFalse(v({"event": "signal", "id": "a"}))        # no session/version
        self.assertFalse(v({"event": "outcome", "id": "a"}))       # no horizon/outcome
        self.assertFalse(v({"event": "benchmark", "id": "a", "legs": None}))
        self.assertFalse(v({"event": "nonsense", "id": "a"}))
        self.assertTrue(v({"event": "signal", "id": "a",
                           paper.SIGNAL_DAY_FIELD: "2026-01-01",
                           "engine_version": paper.SIGNAL_ENGINE_VERSION}))
        self.assertTrue(v({"event": "outcome", "id": "a", "horizon": "5",
                           "outcome": {"net_return_pct": 1.0}}))

    def test_replay_and_live_append_use_the_same_reducer(self):
        """Regression for repeated schema drift: two reducers meant two behaviours."""
        store = {}
        paper.PennyStockPaperBot._fold_evidence_event(store, self._signal("s1"))
        paper.PennyStockPaperBot._fold_evidence_event(
            store, {"event": "outcome", "id": "s1", "horizon": "5",
                    "outcome": {"net_return_pct": 2.0}, "resolved": True})
        self.assertEqual(store["s1"]["outcomes"]["5"]["net_return_pct"], 2.0)
        # an outcome for an unknown signal is ignored, not invented
        self.assertFalse(paper.PennyStockPaperBot._fold_evidence_event(
            store, {"event": "outcome", "id": "ghost", "horizon": "5",
                    "outcome": {}}))


class TestEnvOrRegistry(unittest.TestCase):
    """setx writes the registry and only reaches later processes, so a key can be
    installed and invisible at the same time."""

    def test_process_env_wins(self):
        import env_config
        with mock.patch.dict(os.environ, {"PENNY_TEST_KEY": "from-env"}):
            self.assertEqual(env_config.env_or_registry("PENNY_TEST_KEY"), "from-env")

    def test_registry_is_used_when_the_process_env_lacks_it(self):
        import env_config
        if os.name != "nt":
            self.skipTest("registry fallback is Windows-only")
        os.environ.pop("PENNY_TEST_KEY", None)
        fake = mock.MagicMock()
        fake.QueryValueEx.return_value = ("from-registry", 1)
        fake.OpenKey.return_value.__enter__.return_value = object()
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            self.assertEqual(env_config.env_or_registry("PENNY_TEST_KEY"), "from-registry")
        self.assertEqual(os.environ.get("PENNY_TEST_KEY"), "from-registry")
        os.environ.pop("PENNY_TEST_KEY", None)

    def test_a_missing_key_returns_the_default_not_an_error(self):
        import env_config
        self.assertEqual(
            env_config.env_or_registry("PENNY_DEFINITELY_UNSET_KEY", "fallback"),
            "fallback")

    def test_the_shipped_example_config_uses_the_helper(self):
        """The fix lived only in gitignored config.py, so a fresh clone never got it."""
        with open("config.example.py", encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("env_or_registry(\"GROQ_API_KEY\"", src)
        self.assertNotIn("os.getenv(\"GROQ_API_KEY\"", src)


class TestArchiveRepairIsNonDestructive(unittest.TestCase):
    """Repair must make the archive appendable without deleting real evidence."""

    def _sig(self, sid, day):
        return {"event": "signal", "id": sid, paper.SIGNAL_DAY_FIELD: day,
                "ticker": "A", "engine_version": paper.SIGNAL_ENGINE_VERSION}

    def test_a_valid_final_event_without_a_newline_is_kept(self):
        """A missing newline is not torn JSON. Truncating it deleted a real event."""
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._sig("s1", "2026-01-01")) + "\n")
            f.write(json.dumps(self._sig("s2", "2026-01-02")))       # valid, no newline
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", self._sig("s3", "2026-01-03"))
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(sorted(x["id"] for x in restarted.evidence_rows()),
                         ["s1", "s2", "s3"])
        self.assertEqual(restarted.archive_error, "")

    def test_an_invalid_final_fragment_is_still_quarantined(self):
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._sig("s1", "2026-01-01")) + "\n")
            f.write('{"event": "signal", "id": "tor')               # genuinely torn
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        bot._archive_event("signal", self._sig("s3", "2026-01-03"))
        restarted = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(sorted(x["id"] for x in restarted.evidence_rows()), ["s1", "s3"])
        self.assertTrue(os.path.exists(ap + ".torn"))

    def test_an_orphan_outcome_blocks_validation(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "outcome", "id": "ghost", "horizon": "5",
                                "outcome": {"net_return_pct": 1.0}}) + "\n")
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertIn("orphan", bot.archive_integrity_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_an_invalid_outbox_event_is_quarantined_not_queued(self):
        tmp = tempfile.mkdtemp()
        ap = os.path.join(tmp, "a.jsonl")
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            f.write("[]\n")                                   # parses, not an event
        bot = paper.PennyStockPaperBot(state_path=os.path.join(tmp, "s.json"),
                                       archive_path=ap)
        self.assertEqual(bot._archive_outbox, [])             # never queued
        self.assertIn("unusable", bot.quarantine_error)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")

    def test_salvage_cannot_clear_the_quarantine_block(self):
        """Draining the queue must not erase the record that evidence was lost."""
        tmp = tempfile.mkdtemp()
        ap, sp = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "s.json")
        with open(ap + ".outbox", "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "signal", "id": "ok",
                                paper.SIGNAL_DAY_FIELD: "2026-01-01",
                                "engine_version": paper.SIGNAL_ENGINE_VERSION}) + "\n")
            f.write("[]\n")
        bot = paper.PennyStockPaperBot(state_path=sp, archive_path=ap)
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")
        bot._save()                                           # drains the good event
        self.assertNotEqual(bot.quarantine_error, "")         # block survives salvage
        self.assertEqual(bot.forward_validation()["status"], "DATA_INCOMPLETE")


class TestAIIncrementalValueAudit(unittest.TestCase):
    """The model needs a measured control group before anyone can call it edge."""

    @staticmethod
    def _rank():
        return {
            "composite": 80.0, "hype": 75.0, "quality": 75.0,
            "tradeability": 95.0, "technical": 80.0, "catalyst": 70.0,
        }

    @staticmethod
    def _row(day, sid, selection, net, excess, outcome=True):
        role = (paper.PRIMARY_EVIDENCE_ROLE if selection == "approved"
                else paper.CONTROL_EVIDENCE_ROLE)
        return {
            "id": sid, paper.SIGNAL_DAY_FIELD: day,
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            "ticker": sid, paper.EVIDENCE_ROLE_FIELD: role,
            paper.AI_SELECTION_FIELD: selection,
            "ai_policy_version": paper.AI_DECISION_POLICY_VERSION,
            "ai_policy_id": paper.ai_decision_policy_id(),
            "outcomes": ({"5": measured_outcome(net, excess)} if outcome else {}),
            "resolved": outcome,
        }

    def test_ai_veto_preserves_the_pre_ai_mechanical_setup(self):
        signal = research.signal_from(
            complete_dossier(), self._rank(),
            {"verdict": "AVOID", "conviction": "high", "score": 10},
        )
        self.assertEqual(signal["mechanical_action"], "STRONG BUY")
        self.assertEqual(signal["candidate_action"], "AVOID")
        self.assertEqual(signal["action"], "AVOID")

    def test_prompt_change_gets_a_new_ai_policy_population(self):
        original = paper.ai_decision_policy_id()
        with mock.patch.object(research, "SYSTEM_PROMPT",
                               research.SYSTEM_PROMPT + "\nchanged"):
            changed = paper.ai_decision_policy_id()
        self.assertNotEqual(original, changed)

    def test_vetoed_setup_is_confirmed_for_measurement_but_never_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            signal = research.signal_from(
                complete_dossier(), self._rank(),
                {"verdict": "AVOID", "conviction": "high", "score": 10},
            )
            board = [{
                "ticker": "VETO", "price": 2.0, "quote_reliable": True,
                "market_state": "REGULAR", "spread_estimated": False,
                "catalyst_key": "same", "signal": signal,
            }]
            bot._update_setup_states(board, now=100.0)
            bot._update_setup_states(board, now=150.0)
        self.assertTrue(board[0]["confirmation"]["confirmed"])
        self.assertEqual(board[0]["signal"]["action"], "AVOID")

    def test_vetoed_setup_is_archived_as_control_not_live_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            signal = research.signal_from(
                complete_dossier(), self._rank(),
                {"verdict": "AVOID", "conviction": "high", "score": 10},
            )
            board = [{
                "ticker": "VETO", "rank": 1, "price": 2.0,
                "composite": 80.0, "hype": 75.0, "quality": 75.0,
                "tradeability": 95.0, "technical": 80.0, "catalyst": 70.0,
                "spread_pct": 1.0, "spread_estimated": False,
                "market_state": "REGULAR", "quote_reliable": True,
                "bid": 1.99, "ask": 2.01, "quote_age_min": 1.0,
                "confirmation": {"confirmed": True, "observations": 2},
                "signal": signal,
                "ai": {"verdict": "AVOID", "conviction": "high", "score": 10},
            }]
            bot._record_signals(board)
            self.assertEqual(len(bot.signal_log), 1)
            row = bot.signal_log[0]
            self.assertEqual(row[paper.EVIDENCE_ROLE_FIELD],
                             paper.CONTROL_EVIDENCE_ROLE)
            self.assertEqual(row[paper.AI_SELECTION_FIELD], "rejected")
            self.assertEqual(row["ai_score"], 10)
            self.assertEqual(bot.evidence_rows(), [])
            self.assertEqual(len(bot.mechanical_evidence_rows()), 1)

    def test_control_rows_do_not_contaminate_the_live_profitability_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            approved = self._row("2026-01-02", "APPROVED", "approved", 2.0, 1.5)
            rejected = self._row("2026-01-02", "REJECTED", "rejected", -50.0, -50.5)
            bot._evidence = {approved["id"]: approved, rejected["id"]: rejected}
            bot.signal_log = [approved, rejected]
            book = bot.daily_baskets("5")
        self.assertEqual(book["logged_rows"], 1)
        self.assertEqual(book["baskets"][0]["members"], 1)
        self.assertEqual(book["baskets"][0]["net_pct"], 2.0)

    def test_capital_aware_forward_audit_never_unlocks_trading(self):
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.extend((
                    self._row(day, f"A{index}", "approved", 2.0, 1.5),
                    self._row(day, f"R{index}", "rejected", -1.0, -1.5),
                ))
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            audit = bot.ai_value_audit("5")
        self.assertEqual(audit["status"], "AI_LIFT_PROMISING_NOT_VALIDATED")
        self.assertEqual(audit["comparison_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        # approved=+2, full mechanical basket=(+2-1)/2=+0.5: real lift +1.5,
        # not the old approved-minus-rejected headline of +3.0.
        self.assertEqual(audit["mean_ai_lift_pct"], 1.5)
        self.assertFalse(audit["auto_trade_allowed"])

    def test_group_sizes_cannot_exaggerate_ai_lift(self):
        """Nine approvals and one rejection are not two equal-sized portfolios.

        Old estimand: +1 - (-1) = +2. Correct capital effect: +1 versus the
        full mechanical basket at +0.8, only +0.2.
        """
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.extend(
                    self._row(day, f"A{index}-{member}", "approved", 1.0, 0.5)
                    for member in range(9)
                )
                rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.5))
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            audit = bot.ai_value_audit("5")
        self.assertEqual(audit["mean_ai_lift_pct"], 0.2)
        self.assertEqual(audit["ai_portfolio_mean_net_pct"], 1.0)
        self.assertEqual(audit["mechanical_mean_net_pct"], 0.8)

    def test_all_approved_days_are_zero_lift_not_discarded(self):
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.append(self._row(day, f"A{index}", "approved", 2.0, 1.5))
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            audit = bot.ai_value_audit("5")
        self.assertEqual(audit["comparison_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(audit["all_approved_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(audit["mean_ai_lift_pct"], 0.0)
        self.assertEqual(audit["status"], "NO_MEASURED_AI_EDGE")

    def test_all_skipped_days_measure_cash_against_the_mechanical_loss(self):
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.5))
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            audit = bot.ai_value_audit("5")
        self.assertEqual(audit["all_skipped_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(audit["ai_portfolio_mean_net_pct"], 0.0)
        self.assertEqual(audit["mechanical_mean_net_pct"], -1.0)
        self.assertEqual(audit["mean_ai_lift_pct"], 1.0)
        self.assertFalse(audit["auto_trade_allowed"])

    def test_mature_missing_control_blocks_a_positive_ai_claim(self):
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.extend((
                    self._row(day, f"A{index}", "approved", 2.0, 1.5),
                    self._row(day, f"R{index}", "rejected", -1.0, -1.5),
                ))
            stale_day = "2026-03-15"
            rows.extend((
                self._row(stale_day, "STALE-A", "approved", 2.0, 1.5),
                self._row(stale_day, "STALE-R", "rejected", 0.0, 0.0,
                          outcome=False),
            ))
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            later_sessions = [date(2026, 3, 16) + timedelta(days=i)
                              for i in range(20)]
            with mock.patch.object(bot, "_recent_sessions",
                                   return_value=later_sessions):
                audit = bot.ai_value_audit("5")
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")
        self.assertEqual(audit["stale_comparison_days"], 1)
        self.assertFalse(audit["auto_trade_allowed"])

    def test_ai_failures_cannot_select_a_pretty_available_sample(self):
        from datetime import date, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            bot = isolated_bot(tmp)
            rows = []
            start = date(2026, 1, 1)
            for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
                day = (start + timedelta(days=index)).isoformat()
                rows.extend((
                    self._row(day, f"A{index}", "approved", 2.0, 1.5),
                    self._row(day, f"R{index}", "rejected", -1.0, -1.5),
                ))
            # 31 failures beside 120 classified rows puts coverage below 80%.
            for index in range(31):
                row = self._row(
                    (start + timedelta(days=index)).isoformat(),
                    f"U{index}", "unavailable", 0.0, 0.0, outcome=False)
                rows.append(row)
            bot._evidence = {row["id"]: row for row in rows}
            bot.signal_log = rows
            audit = bot.ai_value_audit("5")
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")
        self.assertLess(audit["classification_coverage_pct"], 80.0)


class TestSelectorFingerprint(unittest.TestCase):
    """The policy id promises that a changed selector starts a new comparison
    population. It only kept that promise for the system prompt and the model chain.

    Everything else - the dossier the model actually reads, the normaliser that decides
    which replies are valid verdicts, the rule turning a verdict into an approval, the
    sampling, and how stale a reused decision may be - could be edited while two
    different selectors pooled into one population. Because the multiplicity correction
    counts distinct policy ids, an unfingerprinted edit was also a free untracked test.
    """

    def setUp(self):
        self.base = paper.ai_decision_policy_id()

    def _moves(self, patcher):
        with patcher:
            return paper.ai_decision_policy_id() != self.base

    def test_the_user_prompt_the_model_reads_is_fingerprinted(self):
        self.assertTrue(self._moves(mock.patch.object(
            research, "_dossier_text", lambda d: "a different dossier entirely")))

    def test_the_output_normalizer_is_fingerprinted(self):
        self.assertTrue(self._moves(mock.patch.object(
            research, "_normalize_ai", lambda value: {"verdict": "SPECULATIVE_BUY"})))

    def test_the_acceptance_rule_is_fingerprinted(self):
        self.assertTrue(self._moves(mock.patch.object(
            research, "signal_from", lambda *a, **k: {"action": "BUY"})))

    def test_sampling_parameters_are_fingerprinted(self):
        for field, value in (("temperature", 1.0), ("max_tokens", 200)):
            with self.subTest(field=field):
                self.assertTrue(self._moves(mock.patch.dict(
                    research.AI_SAMPLING, {field: value})))
        self.assertTrue(self._moves(mock.patch.object(
            research, "AI_TIMEOUT_SEC", 1.0)))

    def test_response_parsing_and_model_call_are_fingerprinted(self):
        self.assertTrue(self._moves(mock.patch.object(
            research, "_extract_json", lambda raw: {"verdict": "WATCH"})))
        self.assertTrue(self._moves(mock.patch.object(
            research, "_call_ai_long", lambda *args, **kwargs: "{}")))
        self.assertTrue(self._moves(mock.patch.object(
            research, "analyse_dossier", lambda dossier: {"ai": None})))

    def test_cache_thresholds_are_fingerprinted(self):
        """A longer TTL reuses a decision for a name whose book has moved, which
        changes which names get approved."""
        self.assertTrue(self._moves(mock.patch.object(paper, "AI_CACHE_SEC", 60)))
        self.assertTrue(self._moves(mock.patch.object(
            paper, "AI_ERROR_CACHE_SEC", 60)))
        self.assertTrue(self._moves(mock.patch.object(
            paper, "AI_MATERIAL_PRICE_PCT", 99.0)))
        self.assertTrue(self._moves(mock.patch.object(
            paper, "AI_MATERIAL_SCORE_POINTS", 99.0)))
        self.assertTrue(self._moves(mock.patch.object(
            paper, "AI_CACHE_MAX_NAMES", 10)))
        self.assertTrue(self._moves(mock.patch.object(
            paper, "AI_DEEP_DIVE", paper.AI_DEEP_DIVE + 1)))

    def test_cache_behavior_and_key_builder_are_fingerprinted(self):
        self.assertTrue(self._moves(mock.patch.object(
            paper.PennyStockPaperBot, "_cached_ai",
            lambda *args, **kwargs: (None, "", False, ""))))
        self.assertTrue(self._moves(mock.patch.object(
            paper.PennyStockPaperBot, "_catalyst_key", lambda dossier: "same")))
        self.assertTrue(self._moves(mock.patch.object(
            paper.PennyStockPaperBot, "_store_ai", lambda *args, **kwargs: None)))

    def test_the_original_prompt_and_model_chain_still_move_it(self):
        self.assertTrue(self._moves(mock.patch.object(
            research, "SYSTEM_PROMPT", research.SYSTEM_PROMPT + "\nchanged")))
        self.assertTrue(self._moves(mock.patch.object(
            research, "AI_MODEL_CHAIN", ["some/other-model"])))
        self.assertTrue(self._moves(mock.patch.object(
            research, "REQUIRE_AI_CONFIRM", not research.REQUIRE_AI_CONFIRM)))

    def test_the_id_is_stable_when_nothing_changes(self):
        self.assertEqual(paper.ai_decision_policy_id(), self.base)
        self.assertEqual(len(self.base), 16)

    def test_comments_and_docstrings_do_not_discard_a_population(self):
        """Over-sensitivity has a real cost: it resets the evidence clock. Only the
        semantics of a component may move its digest."""
        def original(a):
            """Doc."""
            # explain something
            return a + 1

        def recommented(a):
            """A completely different docstring."""

            # a completely different comment
            return a + 1

        def rewritten(a):
            """Doc."""
            return a + 2

        source = {original: '''
def f(a):
    """Doc."""
    # explain something
    return a + 1
''', recommented: '''
def f(a):
    """A completely different docstring."""

    # a completely different comment
    return a + 1
''', rewritten: '''
def f(a):
    """Doc."""
    return a + 2
'''}
        with mock.patch.object(paper.inspect, "getsource", lambda t: source[t]):
            paper._BEHAVIOUR_DIGESTS.clear()
            same = paper._behaviour_digest(original)
            paper._BEHAVIOUR_DIGESTS.clear()
            recomment = paper._behaviour_digest(recommented)
            paper._BEHAVIOUR_DIGESTS.clear()
            semantic = paper._behaviour_digest(rewritten)
        paper._BEHAVIOUR_DIGESTS.clear()
        self.assertEqual(same, recomment)
        self.assertNotEqual(same, semantic)

    def test_an_undigestible_component_fails_closed(self):
        """"We could not tell whether the selector changed" must not look like
        "it did not change"."""
        class Opaque:
            pass

        paper._BEHAVIOUR_DIGESTS.clear()
        with self.assertRaises(TypeError):
            paper._behaviour_digest(Opaque())

    def test_no_source_fallback_includes_constants_not_only_bytecode(self):
        def alpha():
            return "alpha"

        def beta():
            return "beta"

        self.assertEqual(alpha.__code__.co_code, beta.__code__.co_code)
        paper._BEHAVIOUR_DIGESTS.clear()
        with mock.patch.object(paper.inspect, "getsource",
                               side_effect=OSError("source unavailable")):
            self.assertNotEqual(paper._behaviour_digest(alpha),
                                paper._behaviour_digest(beta))
        paper._BEHAVIOUR_DIGESTS.clear()

    def test_whole_policy_id_is_stable_when_source_files_are_unavailable(self):
        paper._BEHAVIOUR_DIGESTS.clear()
        with mock.patch.object(paper.inspect, "getsource",
                               side_effect=OSError("source unavailable")):
            first = paper.ai_decision_policy_id()
            second = paper.ai_decision_policy_id()
            with mock.patch.object(paper, "AI_MATERIAL_PRICE_PCT", 99.0):
                changed = paper.ai_decision_policy_id()
        paper._BEHAVIOUR_DIGESTS.clear()
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_the_audit_reports_what_the_fingerprint_covers(self):
        covered = isolated_bot(tempfile.mkdtemp()).ai_value_audit(
            "5")["ai_policy_fingerprint_covers"]
        for component in ("system_prompt", "user_prompt_builder", "output_normalizer",
                          "acceptance_rule", "sampling", "cache_sec",
                          "error_cache_sec", "model_chain", "require_ai_confirm",
                          "response_extractor", "model_call", "analysis_pipeline",
                          "cache_lookup", "cache_key_builder", "cache_store",
                          "cache_material_price_pct", "cache_material_score_points",
                          "review_limit_per_scan"):
            self.assertIn(component, covered)

    def test_fingerprinting_does_not_touch_the_engine_or_unlock_trading(self):
        bot = isolated_bot(tempfile.mkdtemp())
        self.assertEqual(paper.SIGNAL_ENGINE_VERSION, 7)
        self.assertFalse(bot.ai_value_audit("5")["auto_trade_allowed"])
        self.assertFalse(bot.forward_validation("5")["auto_trade_allowed"])


class TestSelectorDigestIsStable(unittest.TestCase):
    """A behaviour digest must describe behaviour, never object identity.

    CPython's default repr embeds id(), so a selector component written with the
    ordinary sentinel-default idiom hashed differently in every process. The policy id
    would then change on each restart: the evidence population could never accumulate
    60 days, and the multiplicity count would grow without bound - silently, because
    nothing distinguishes "the selector changed" from "the object moved".
    """

    def setUp(self):
        paper._BEHAVIOUR_DIGESTS.clear()

    tearDown = setUp

    @staticmethod
    def _with_default(default):
        namespace = {}
        exec(compile("def build(d, override=None):\n    return d\n",
                     "<nosource>", "exec"), namespace)
        namespace["build"].__defaults__ = (default,)
        return namespace["build"]

    def test_a_sentinel_default_does_not_move_the_digest(self):
        class Missing:
            pass

        first = paper._behaviour_digest(self._with_default(Missing()))
        paper._BEHAVIOUR_DIGESTS.clear()
        second = paper._behaviour_digest(self._with_default(Missing()))
        self.assertEqual(first, second)

    def test_stateful_defaults_with_different_behavior_do_move_the_digest(self):
        class Options:
            pass

        low = Options()
        low.threshold = 1
        high = Options()
        high.threshold = 2
        first = paper._behaviour_digest(self._with_default(low))
        paper._BEHAVIOUR_DIGESTS.clear()
        second = paper._behaviour_digest(self._with_default(high))
        self.assertNotEqual(first, second)

    def test_a_custom_repr_still_distinguishes_values(self):
        """Dropping repr entirely would be over-correction: Decimal('1.5') and
        Decimal('2.5') are different behaviour, not different identity."""
        import decimal

        low = paper._semantic_value(decimal.Decimal("1.5"))
        high = paper._semantic_value(decimal.Decimal("2.5"))
        self.assertNotEqual(low, high)
        self.assertIn("1.5", low["repr"])

    def test_an_address_inside_a_custom_repr_is_scrubbed(self):
        class Addressed:
            def __repr__(self):
                return f"<Addressed at 0x{id(self):x}>"

        self.assertEqual(paper._semantic_value(Addressed()),
                         paper._semantic_value(Addressed()))
        self.assertNotIn("0x0", paper._semantic_value(Addressed())["repr"][3:])

    def test_meaningful_hexadecimal_state_is_not_scrubbed(self):
        class HexValue:
            def __init__(self, value):
                self.value = value

            def __repr__(self):
                return f"<HexValue 0x{self.value:x}>"

        low = paper._semantic_value(HexValue(0x1234))
        high = paper._semantic_value(HexValue(0x5678))
        self.assertNotEqual(low, high)
        self.assertEqual(low["state"]["attributes"]["value"], 0x1234)

    def test_cyclic_object_state_is_stable_without_using_identity(self):
        class Node:
            pass

        first = Node()
        first.self = first
        second = Node()
        second.self = second
        self.assertEqual(paper._semantic_value(first),
                         paper._semantic_value(second))

    def test_a_bare_object_is_described_by_type_alone(self):
        class Bare:
            pass

        described = paper._semantic_value(Bare())
        self.assertNotIn("repr", described)
        self.assertIn("Bare", described["type"])
        # a different class is still a different description
        class Other:
            pass
        self.assertNotEqual(described, paper._semantic_value(Other()))

    def test_the_policy_id_is_identical_in_a_fresh_interpreter(self):
        """The end-to-end property: restarting the process must not start a new
        comparison population."""
        import subprocess

        repo = os.path.dirname(os.path.abspath(paper.__file__))
        script = (f"import sys; sys.path.insert(0, r'{repo}')\n"
                  "import pennystock_paper as p; print(p.ai_decision_policy_id())")
        runs = {subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True).stdout.strip() for _ in range(2)}
        self.assertEqual(runs, {paper.ai_decision_policy_id()})

    def test_semantic_bytecode_still_reaches_constants_and_closures(self):
        """The stability fix must not weaken the fallback it sits inside."""
        namespace = {}
        exec(compile('def a():\n    return "ALPHA"\ndef b():\n    return "BETA"\n',
                     "<nosource>", "exec"), namespace)
        self.assertNotEqual(paper._behaviour_digest(namespace["a"]),
                            paper._behaviour_digest(namespace["b"]))
        maker = {}
        exec(compile("def mk(t):\n    def f(x, k=t):\n        return x + k\n"
                     "    return f\n", "<nosource>", "exec"), maker)
        first = paper._behaviour_digest(maker["mk"](1))
        paper._BEHAVIOUR_DIGESTS.clear()
        self.assertNotEqual(first, paper._behaviour_digest(maker["mk"](2)))

    def test_a_component_with_neither_source_nor_code_still_fails_closed(self):
        with self.assertRaises(TypeError):
            paper._behaviour_digest(object())


class TestSelectorFingerprintScope(unittest.TestCase):
    """The id must move for every operational selector change and for nothing else."""

    def setUp(self):
        self.base = paper.ai_decision_policy_id()

    def _moves(self, patcher):
        with patcher:
            return paper.ai_decision_policy_id() != self.base

    def test_cache_reuse_thresholds_and_capacity_are_tracked(self):
        for name, value in (("AI_MATERIAL_PRICE_PCT", 99.0),
                            ("AI_MATERIAL_SCORE_POINTS", 99.0),
                            ("AI_CACHE_MAX_NAMES", 3),
                            ("AI_DEEP_DIVE", 1)):
            with self.subTest(setting=name):
                self.assertTrue(self._moves(mock.patch.object(paper, name, value)))

    def test_the_cache_and_call_path_behaviour_is_tracked(self):
        cases = {
            "cache lookup": mock.patch.object(
                paper.PennyStockPaperBot, "_cached_ai",
                lambda self, d, score: (None, "", False, "")),
            "cache store": mock.patch.object(
                paper.PennyStockPaperBot, "_store_ai", lambda *a, **k: None),
            "cache key": mock.patch.object(
                paper.PennyStockPaperBot, "_catalyst_key",
                staticmethod(lambda d: "constant")),
            "response extractor": mock.patch.object(
                research, "_extract_json", lambda raw: {}),
            "model call": mock.patch.object(
                research, "_call_ai_long", lambda system, user: ""),
            "analysis pipeline": mock.patch.object(
                research, "analyse_dossier", lambda d: {}),
        }
        for label, patcher in cases.items():
            with self.subTest(component=label):
                self.assertTrue(self._moves(patcher))

    def test_prompt_inputs_are_tracked(self):
        """effective_spread and catalyst_alignment are printed into the dossier the
        model reads, so changing either changes the selector's input."""
        self.assertTrue(self._moves(mock.patch.object(
            research, "effective_spread", lambda d: (1.0, False))))
        self.assertTrue(self._moves(mock.patch.object(
            research, "catalyst_alignment", lambda d: {})))

    def test_measurement_logic_stays_out_of_the_selector_id(self):
        """Repairing how outcomes are MEASURED must not discard the selector's
        evidence population - it is a different question about the same rows."""
        cases = {
            "exit_leg": mock.patch.object(
                paper.PennyStockPaperBot, "exit_leg",
                classmethod(lambda cls, *a, **k: None)),
            "outcome_is_evidentiary": mock.patch.object(
                paper.PennyStockPaperBot, "outcome_is_evidentiary",
                staticmethod(lambda outcome: True)),
            "daily_baskets": mock.patch.object(
                paper.PennyStockPaperBot, "daily_baskets",
                lambda self, horizon="5": {}),
            "hac interval": mock.patch.object(
                paper.PennyStockPaperBot, "_hac_mean_ci",
                staticmethod(lambda values, max_lag=5: (0.0, 0.0, 0.0))),
            "adverse bound": mock.patch.object(
                paper.PennyStockPaperBot, "_minimum_selector_return",
                staticmethod(lambda approved, unavailable: 0.0)),
            "completed sessions": mock.patch.object(
                paper.PennyStockPaperBot, "completed_sessions",
                staticmethod(lambda dates, now_ny=None: 0)),
            "measurement schema": mock.patch.object(
                paper, "MEASUREMENT_SCHEMA_VERSION", 99),
        }
        for label, patcher in cases.items():
            with self.subTest(component=label):
                self.assertFalse(self._moves(patcher))


class TestPriorPolicyPopulationsStaySeparate(unittest.TestCase):
    """Evidence from a previous selector counts as a tested policy but never joins
    the current comparison."""

    @staticmethod
    def _row(day, sid, selection, net, excess, policy_id, version=None):
        role = (paper.PRIMARY_EVIDENCE_ROLE if selection == "approved"
                else paper.CONTROL_EVIDENCE_ROLE)
        return {
            "id": sid, paper.SIGNAL_DAY_FIELD: day, "ticker": sid,
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            paper.EVIDENCE_ROLE_FIELD: role,
            paper.AI_SELECTION_FIELD: selection,
            "ai_policy_version": (paper.AI_DECISION_POLICY_VERSION
                                  if version is None else version),
            "ai_policy_id": policy_id,
            "outcomes": {"5": measured_outcome(net, excess)}, "resolved": True,
        }

    def test_legacy_fingerprints_are_counted_but_not_pooled(self):
        current = paper.ai_decision_policy_id()
        rows = []
        for index in range(paper.AI_VALUE_MIN_COMPARISON_DAYS):
            day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
            rows.append(self._row(day, f"A{index}", "approved", 2.0, 1.0, current))
            rows.append(self._row(day, f"R{index}", "rejected", 0.0, -1.0, current))
        # rows recorded under three earlier fingerprints, and one earlier policy version
        rows += [self._row("2026-01-01", f"OLD{k}", "approved", 99.0, 99.0,
                           f"legacy-{k}") for k in range(3)]
        rows += [self._row("2026-01-01", "OLDV", "approved", 99.0, 99.0, current,
                           version=paper.AI_DECISION_POLICY_VERSION - 1)]
        bot = isolated_bot(tempfile.mkdtemp())
        bot._evidence = {row["id"]: row for row in rows}
        bot.signal_log = rows
        audit = bot.ai_value_audit("5")
        self.assertEqual(audit["ai_policies_tested"], 5)     # 1 current + 4 prior
        self.assertEqual(audit["comparison_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(audit["mean_ai_lift_pct"], 1.0)     # the +99% rows never enter
        self.assertFalse(audit["auto_trade_allowed"])


class TestMissingDecisionBound(unittest.TestCase):
    """The adverse bound must be the exact minimum over every assignment of the
    decisions that were never recorded - not a heuristic that happens to look low."""

    @staticmethod
    def _brute(approved, unavailable):
        import itertools
        best = None
        for mask in itertools.product([0, 1], repeat=len(unavailable)):
            held = list(approved) + [u for u, take in zip(unavailable, mask) if take]
            value = sum(held) / len(held) if held else 0.0
            best = value if best is None else min(best, value)
        return best

    CASES = [
        ([], [-5.0, 2.0, -1.0]),
        ([3.0], [-9.0, 4.0]),
        ([1.0, 2.0], [0.5, -0.5, -20.0]),
        ([], [4.0, 7.0]),                 # all positive: cash is the worst outcome
        ([-2.0], []),                     # nothing unknown
        ([], []),                         # nothing at all
        ([10.0], [-1.0, -1.0, -1.0, -1.0]),
        ([0.0, 0.0], [3.0, -3.0, 1.0, -8.0, 2.0]),
    ]

    def test_the_bound_equals_brute_force_enumeration(self):
        for approved, unavailable in self.CASES:
            with self.subTest(approved=approved, unavailable=unavailable):
                self.assertAlmostEqual(
                    paper.PennyStockPaperBot._minimum_selector_return(
                        approved, unavailable),
                    self._brute(approved, unavailable), places=12)

    def test_no_possible_assignment_beats_the_bound(self):
        import itertools
        for approved, unavailable in self.CASES:
            low = paper.PennyStockPaperBot._minimum_selector_return(
                approved, unavailable)
            for mask in itertools.product([0, 1], repeat=len(unavailable)):
                held = list(approved) + [u for u, t in zip(unavailable, mask) if t]
                realised = sum(held) / len(held) if held else 0.0
                with self.subTest(approved=approved, mask=mask):
                    self.assertGreaterEqual(realised, low - 1e-12)

    def test_cash_is_only_available_when_nothing_was_approved(self):
        """A recorded approval is a decision: the portfolio holds it and cannot be
        cash, even when every unknown name would drag the mean below zero."""
        self.assertEqual(
            paper.PennyStockPaperBot._minimum_selector_return([5.0], [8.0]), 5.0)
        self.assertEqual(
            paper.PennyStockPaperBot._minimum_selector_return([], [8.0]), 0.0)


class TestAIValueAuditSampleAndInference(unittest.TestCase):
    """The lift must come from decisions the model actually made, priced against the
    search that produced it."""

    START = date(2026, 1, 1)

    @staticmethod
    def _row(day, sid, selection, net, excess, policy_id=None):
        role = (paper.PRIMARY_EVIDENCE_ROLE if selection == "approved"
                else paper.CONTROL_EVIDENCE_ROLE)
        return {
            "id": sid, paper.SIGNAL_DAY_FIELD: day, "ticker": sid,
            "engine_version": paper.SIGNAL_ENGINE_VERSION,
            paper.EVIDENCE_ROLE_FIELD: role,
            paper.AI_SELECTION_FIELD: selection,
            "ai_policy_version": paper.AI_DECISION_POLICY_VERSION,
            "ai_policy_id": policy_id or paper.ai_decision_policy_id(),
            "outcomes": {"5": measured_outcome(net, excess)}, "resolved": True,
        }

    def _audit(self, rows, horizon="5"):
        bot = isolated_bot(tempfile.mkdtemp())
        bot._evidence = {row["id"]: row for row in rows}
        bot.signal_log = list(rows)
        return bot.ai_value_audit(horizon)

    def _days(self):
        return [(self.START + timedelta(days=i)).isoformat()
                for i in range(paper.AI_VALUE_MIN_COMPARISON_DAYS)]

    def test_a_total_outage_day_is_not_a_decision_to_hold_cash(self):
        """A model outage on a losing day used to be scored as the selector choosing
        cash, so an API failure during a bad stretch manufactured positive lift.

        The global 80% coverage gate cannot catch it: overall coverage stays at 90%
        while six whole days are dark.
        """
        rows = []
        for index, day in enumerate(self._days()):
            if index % 10 == 0:                      # 6 of 60 days: nothing classified
                rows.append(self._row(day, f"U{index}a", "unavailable", -8.0, -8.0))
                rows.append(self._row(day, f"U{index}b", "unavailable", -8.0, -8.0))
            else:
                rows.append(self._row(day, f"A{index}", "approved", 0.0, 0.0))
                rows.append(self._row(day, f"R{index}", "rejected", 0.0, 0.0))
        audit = self._audit(rows)
        self.assertGreater(audit["classification_coverage_pct"], 80.0)
        self.assertEqual(audit["unclassified_days"], 6)
        self.assertEqual(audit["unclassified_rows"], 12)
        self.assertEqual(audit["all_skipped_days"], 0)     # never a "cash" decision
        self.assertEqual(audit["comparison_days"], 54)
        self.assertEqual(audit["mean_ai_lift_pct"], 0.0)   # was +0.8 from the outages
        self.assertFalse(audit["auto_trade_allowed"])

    def test_an_unclassified_stale_day_cannot_disappear_before_integrity_checks(self):
        """Coverage filtering used to run before outcome checks, so an unresolved
        fully-dark day vanished and the surviving sample could still look promising."""
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 1.0, 0.5))
            rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.5))
        missing = self._row("2025-12-01", "MISSING", "unavailable", 0.0, 0.0)
        missing["outcomes"] = {}
        missing["resolved"] = False
        rows.append(missing)

        audit = self._audit(rows)
        self.assertGreater(audit["classification_coverage_pct"], 80.0)
        self.assertEqual(audit["unclassified_days"], 1)
        self.assertEqual(audit["stale_comparison_days"], 1)
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")
        self.assertIn("missing outcomes", audit["reason"])

    def test_completed_unclassified_days_enter_an_adverse_selection_bound(self):
        """Dropping a dark day still selects the sample. A missing decision on a
        +100% mechanical winner could have rejected that winner and held cash, so the
        positive point estimate must survive that adverse assignment."""
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 1.0, 0.5))
            rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.5))
        rows.append(self._row("2026-04-01", "UNKNOWN-WINNER", "unavailable",
                              100.0, 99.5))

        audit = self._audit(rows)
        bound = audit["classification_missing_bound"]
        self.assertGreater(audit["classification_coverage_pct"], 80.0)
        self.assertEqual(audit["mean_ai_lift_pct"], 1.0)
        self.assertEqual(audit["unclassified_bound_days"], 1)
        self.assertEqual(bound["days_bounded"], 1)
        self.assertEqual(bound["bounded_series_days"], 61)
        self.assertFalse(bound["clears_zero"])
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")
        self.assertIn("missing decisions", audit["reason"])

    def test_the_allowed_twenty_percent_missing_is_also_bounded(self):
        """The coverage floor is not permission to score missing decisions as
        rejections. At exactly 80%, avoiding one unavailable loser looks brilliant in
        the point estimate but fails the adverse assignment."""
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 1.0, 0.5))
            rows.extend(self._row(day, f"R{index}-{k}", "rejected", 0.0, -0.5)
                        for k in range(3))
            rows.append(self._row(day, f"U{index}", "unavailable", -100.0, -100.5))

        audit = self._audit(rows)
        bound = audit["classification_missing_bound"]
        self.assertEqual(audit["classification_coverage_pct"], 80.0)
        self.assertEqual(audit["unclassified_days"], 0)
        self.assertEqual(audit["comparison_days"], 60)
        self.assertGreater(audit["mean_ai_lift_pct"], 0)
        self.assertEqual(audit["missing_decision_bound_days"], 60)
        self.assertFalse(bound["clears_zero"])
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")

    def test_the_missing_decision_bound_is_not_a_permanent_veto(self):
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 10.0, 9.5))
            rows.append(self._row(day, f"R{index}", "rejected", -10.0, -10.5))
        rows.append(self._row("2026-04-01", "UNKNOWN-FLAT", "unavailable",
                              0.0, -0.5))

        audit = self._audit(rows)
        self.assertTrue(audit["classification_missing_bound"]["clears_zero"])
        self.assertEqual(audit["status"], "AI_LIFT_PROMISING_NOT_VALIDATED")
        self.assertFalse(audit["auto_trade_allowed"])

    def test_missing_decision_bound_uses_the_worst_possible_subset(self):
        self.assertEqual(paper.PennyStockPaperBot._minimum_selector_return(
            [4.0], [-10.0, 20.0]), -3.0)
        self.assertEqual(paper.PennyStockPaperBot._minimum_selector_return(
            [], [-5.0, 10.0]), -5.0)

    def test_an_unknown_selection_value_is_bounded_as_unavailable(self):
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 1.0, 0.5))
            rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.5))
        malformed = self._row("2026-04-01", "UNKNOWN-LABEL", "unavailable",
                              100.0, 99.5)
        malformed[paper.AI_SELECTION_FIELD] = ""
        rows.append(malformed)

        audit = self._audit(rows)
        self.assertEqual(audit["unavailable_rows"], 1)
        self.assertEqual(audit["missing_decision_bound_days"], 1)
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")

    def test_a_day_the_model_mostly_missed_is_excluded(self):
        """Five extra days at 20% coverage, while overall coverage stays above the
        global gate - so this isolates the per-day floor rather than the outer one."""
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 1.0, 0.5))
            rows.append(self._row(day, f"R{index}", "rejected", 0.0, -0.5))
        for index in range(5):
            day = (self.START + timedelta(days=500 + index)).isoformat()
            rows.append(self._row(day, f"PA{index}", "approved", 9.0, 8.5))
            rows.extend(self._row(day, f"PU{index}-{k}", "unavailable", 9.0, 8.5)
                        for k in range(4))           # 1 of 5 classified = 20%
        audit = self._audit(rows)
        self.assertGreater(audit["classification_coverage_pct"],
                           audit["minimum_classification_coverage_pct"])
        self.assertEqual(audit["unclassified_days"], 5)
        self.assertEqual(audit["comparison_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        # the excluded days' +9% approvals never reach the reported lift
        self.assertEqual(audit["mean_ai_lift_pct"], 0.5)

    def test_a_day_meeting_the_floor_still_counts_with_its_baseline_intact(self):
        """The unavailable name stays in the mechanical basket - it was in the
        opportunity set - it just cannot be an AI decision."""
        rows = []
        for day in self._days():
            rows.extend((
                self._row(day, f"A{day}", "approved", 3.0, 2.0),
                self._row(day, f"R{day}-1", "rejected", 0.0, -1.0),
                self._row(day, f"R{day}-2", "rejected", 0.0, -1.0),
                self._row(day, f"R{day}-3", "rejected", 0.0, -1.0),
                self._row(day, f"U{day}", "unavailable", -3.0, -4.0),
            ))
        audit = self._audit(rows)
        self.assertEqual(audit["comparison_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(audit["unclassified_days"], 0)
        # mechanical basket = mean(3, 0, 0, 0, -3) = 0.0, including the unavailable name
        self.assertEqual(audit["mechanical_mean_net_pct"], 0.0)
        self.assertEqual(audit["mean_ai_lift_pct"], 3.0)

    def test_the_iwm_leg_is_a_consistency_check_not_a_second_confirmation(self):
        """Members of a signal day share IWM's horizon return, so it cancels in a
        same-day portfolio difference: the two legs are one statistic."""
        rows = []
        for index, day in enumerate(self._days()):
            bench = 1.0 + (index % 5) * 0.5
            rows.append(self._row(day, f"A{index}", "approved", 3.0, 3.0 - bench))
            rows.append(self._row(day, f"R{index}", "rejected", -1.0, -1.0 - bench))
        audit = self._audit(rows)
        self.assertEqual(audit["mean_ai_lift_pct"], audit["mean_ai_excess_lift_pct"])
        self.assertEqual(audit["ai_lift_hac_95_pct"],
                         audit["ai_excess_lift_hac_95_pct"])
        self.assertIs(audit["benchmark_leg_is_independent"], False)
        self.assertIn("cancels", audit["benchmark_leg_note"])

    def test_members_disagreeing_about_the_benchmark_block_the_audit(self):
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 3.0, 2.0))
            rows.append(self._row(day, f"R{index}", "rejected", -1.0, -7.0))
        audit = self._audit(rows)
        self.assertEqual(audit["status"], "DATA_INCOMPLETE")
        self.assertEqual(audit["benchmark_inconsistent_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertIn("cancel", audit["reason"])

    def test_the_hac_lag_follows_the_horizon_overlap(self):
        rows = []
        for index, day in enumerate(self._days()):
            for sid, selection, net, excess in (
                    (f"A{index}", "approved", 3.0, 2.0),
                    (f"R{index}", "rejected", -1.0, -2.0)):
                row = self._row(day, sid, selection, net, excess)
                row["outcomes"] = {"10": measured_outcome(net, excess)}
                rows.append(row)
        self.assertEqual(self._audit(rows, "10")["hac_max_lag"], 9)
        self.assertEqual(self._audit(rows, "5")["hac_max_lag"], 5)

    def test_every_tried_policy_widens_the_bound_it_has_to_clear(self):
        """Searching over prompts and reporting the nominal 95% interval of the one
        that currently looks best is how a tuned selector becomes an 'edge'."""
        rows = []
        for index, day in enumerate(self._days()):
            swing = 2.0 + (index % 7) - 3.0
            rows.append(self._row(day, f"A{index}", "approved", swing, swing - 1.0))
            rows.append(self._row(day, f"R{index}", "rejected", 0.0, -1.0))
        single = self._audit(rows)
        self.assertEqual(single["ai_policies_tested"], 1)
        self.assertEqual(single["ai_lift_multiplicity_adjusted_low_pct"],
                         single["ai_lift_hac_95_pct"][0])

        abandoned = [dict(row, id=f"{row['id']}-p{k}", ai_policy_id=f"tried-{k}")
                     for k in range(4) for row in rows[:2]]
        searched = self._audit(rows + abandoned)
        self.assertEqual(searched["ai_policies_tested"], 5)
        self.assertLess(searched["ai_lift_multiplicity_adjusted_low_pct"],
                        searched["ai_lift_hac_95_pct"][0])
        self.assertFalse(searched["auto_trade_allowed"])

    def test_a_marginal_result_does_not_survive_the_policy_search(self):
        import random

        # Dispersed daily lifts whose nominal 95% bound clears zero by a whisker.
        rng = random.Random(7)
        rows = []
        for index, day in enumerate(self._days()):
            lift = round(rng.gauss(0, 1.0) + 0.35, 3)
            rows.append(self._row(day, f"A{index}", "approved",
                                  2 * lift, 2 * lift - 1.0))
            rows.append(self._row(day, f"R{index}", "rejected", 0.0, -1.0))
        clean = self._audit(rows)
        self.assertEqual(clean["status"], "AI_LIFT_PROMISING_NOT_VALIDATED")
        abandoned = [dict(row, id=f"{row['id']}-p{k}", ai_policy_id=f"tried-{k}")
                     for k in range(30) for row in rows[:2]]
        searched = self._audit(rows + abandoned)
        self.assertEqual(searched["status"], "NO_MEASURED_AI_EDGE")
        self.assertIn("policies tested", searched["reason"])
        self.assertFalse(searched["auto_trade_allowed"])

    def test_portfolio_concentration_is_reported_beside_the_lift(self):
        """A one-name selection measured against a twenty-name basket is a
        concentration change as much as a skill claim."""
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 4.0, 3.0))
            rows.extend(self._row(day, f"R{index}-{k}", "rejected", 0.0, -1.0)
                        for k in range(19))
        audit = self._audit(rows)
        concentration = audit["ai_portfolio_concentration"]
        self.assertEqual(concentration["mean_selected_names"], 1.0)
        self.assertEqual(concentration["mean_mechanical_names"], 20.0)
        self.assertEqual(concentration["single_name_days"],
                         paper.AI_VALUE_MIN_COMPARISON_DAYS)
        self.assertEqual(concentration["cash_days"], 0)

    def test_the_audit_never_unlocks_trading_or_changes_the_engine(self):
        rows = []
        for index, day in enumerate(self._days()):
            rows.append(self._row(day, f"A{index}", "approved", 5.0, 4.0))
            rows.append(self._row(day, f"R{index}", "rejected", -5.0, -6.0))
        audit = self._audit(rows)
        self.assertFalse(audit["auto_trade_allowed"])
        self.assertEqual(paper.SIGNAL_ENGINE_VERSION, 7)
