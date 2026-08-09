import asyncio
import os
import tempfile
import unittest
from unittest import mock

import penny_quotes
import pennystock_paper as paper

research = paper.research


def _asset(symbol, exchange="NASDAQ", tradable=True, status="active"):
    return {
        "symbol": symbol,
        "name": f"{symbol} Incorporated",
        "class": "us_equity",
        "status": status,
        "exchange": exchange,
        "tradable": tradable,
    }


def _snapshot(price, prior=1.0, volume=1000, bid=None, ask=None):
    quote = {}
    if bid is not None:
        quote["bp"] = bid
    if ask is not None:
        quote["ap"] = ask
    return {
        "latestTrade": {"p": price, "t": "2026-08-07T19:59:00Z"},
        "latestQuote": quote,
        "dailyBar": {"c": price, "v": volume},
        "prevDailyBar": {"c": prior},
    }


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return ""


class _Session:
    def __init__(self, snapshots, calls):
        self.snapshots = snapshots
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        symbols = str(params.get("symbols") or "").split(",")
        return _Response({symbol: self.snapshots[symbol]
                          for symbol in symbols if symbol in self.snapshots})


class TestMarketWidePennyUniverse(unittest.TestCase):
    def setUp(self):
        penny_quotes._ASSET_CACHE = (0.0, [], {})
        penny_quotes._UNIVERSE_SCAN_CACHE = {}

    def test_master_list_counts_otc_and_nontradable_exclusions(self):
        eligible, counts = penny_quotes.eligible_listed_assets([
            _asset("KEEP"),
            _asset("OTCX", exchange="OTC", tradable=False),
            _asset("BLOCK", tradable=False),
            _asset("OLD", status="inactive"),
        ])
        self.assertEqual([row["symbol"] for row in eligible], ["KEEP"])
        self.assertEqual(counts["active_us_equities"], 3)
        self.assertEqual(counts["otc_excluded"], 1)
        self.assertEqual(counts["nontradable_or_unlisted_excluded"], 1)
        self.assertEqual(counts["active_listed_tradable"], 1)

    def test_snapshot_row_uses_real_prices_volume_change_and_book(self):
        row = penny_quotes.snapshot_screen_row(
            "PENNY", _snapshot(1.2, prior=1.0, volume=50_000, bid=1.18, ask=1.22),
            _asset("PENNY"))
        self.assertEqual(row["ticker"], "PENNY")
        self.assertAlmostEqual(row["change_pct"], 20.0)
        self.assertEqual(row["day_volume"], 50_000)
        self.assertAlmostEqual(row["dollar_volume"], 60_000.0)
        self.assertAlmostEqual(row["spread_pct"], 3.3333, places=4)
        self.assertEqual(row["feed"], "delayed_sip")

    def test_independent_candidate_views_are_interleaved(self):
        rows = [
            {"ticker": "FILING", "change_pct": -2, "day_volume": 10,
             "dollar_volume": 10, "spread_pct": 3},
            {"ticker": "GAIN", "change_pct": 80, "day_volume": 100,
             "dollar_volume": 100, "spread_pct": 2},
            {"ticker": "VOLUME", "change_pct": 1, "day_volume": 1_000_000,
             "dollar_volume": 1_000_000, "spread_pct": 1},
        ]
        selected = penny_quotes.select_market_candidates(
            rows, 3, catalysts={"FILING"})
        self.assertEqual(selected[0], "FILING")
        self.assertIn("GAIN", selected)
        self.assertIn("VOLUME", selected)

    def test_full_pass_requests_every_asset_in_batches_and_reports_each_stage(self):
        assets = [_asset("LOW"), _asset("HIGH"), _asset("TINY")]
        counts = {"active_listed_tradable": 3, "otc_excluded": 4}
        snapshots = {
            "LOW": _snapshot(1.25, prior=1.0),
            "HIGH": _snapshot(12.0, prior=10.0),
            "TINY": _snapshot(0.50, prior=0.60),
        }
        calls = []
        session = _Session(snapshots, calls)

        async def run():
            with (
                mock.patch.object(penny_quotes, "active_listed_assets",
                                  new=mock.AsyncMock(return_value=(assets, counts, ""))),
                mock.patch.object(penny_quotes, "UNIVERSE_BATCH_SIZE", 2),
                mock.patch.object(penny_quotes.aiohttp, "ClientSession",
                                  return_value=session),
                mock.patch.object(penny_quotes.aiohttp, "ClientTimeout",
                                  return_value=object()),
                mock.patch.object(penny_quotes.asyncio, "sleep",
                                  new=mock.AsyncMock()),
            ):
                return await penny_quotes.market_wide_penny_scan(0.10, 5.0, force=True)

        rows, coverage, error = asyncio.run(run())
        self.assertEqual(error, "")
        self.assertEqual({row["ticker"] for row in rows}, {"LOW", "TINY"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[1]["feed"] == "delayed_sip" for call in calls))
        self.assertEqual(coverage["symbols_requested"], 3)
        self.assertEqual(coverage["snapshots_returned"], 3)
        self.assertEqual(coverage["priced_assets"], 3)
        self.assertEqual(coverage["penny_price_matches"], 2)
        self.assertEqual(coverage["status"], "COMPLETE")

    def test_candidate_sources_deduplicate_without_starving_either_source(self):
        merged = paper.PennyStockPaperBot._interleave_candidates(
            ["Y1", "DUP", "Y2"], ["A1", "DUP", "A2"], limit=5)
        self.assertEqual(merged, ["Y1", "A1", "DUP", "Y2", "A2"])

    def test_coverage_survives_a_normal_state_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            archive = os.path.join(tmp, "archive.jsonl")
            first = paper.PennyStockPaperBot(state_path=state, archive_path=archive)
            first.universe_coverage = {
                "status": "COMPLETE", "symbols_requested": 13_000,
                "snapshots_returned": 12_900, "penny_price_matches": 900,
            }
            first._save()
            restored = paper.PennyStockPaperBot(state_path=state, archive_path=archive)
            self.assertEqual(restored.universe_coverage,
                             first.universe_coverage)

    def test_full_bot_scan_combines_market_wide_and_in_play_candidates(self):
        coverage = {
            "status": "COMPLETE", "symbols_requested": 13_026,
            "snapshots_returned": 13_025, "penny_price_matches": 1_979,
            "last_completed_at": 1.0,
        }
        market_rows = [{
            "ticker": "A1", "change_pct": 25, "day_volume": 1_000,
            "dollar_volume": 2_000, "spread_pct": 2,
        }]
        scored = {
            "composite": 50, "hype": 50, "technical": 50, "catalyst": 0,
            "quality": 50, "tradeability": 50, "hype_why": [],
            "quality_why": [], "technical_why": [], "catalyst_why": [],
            "trade_why": [],
        }
        signal = {
            "action": "NO TRADE", "candidate_action": "NO TRADE",
            "strategy_id": research.LIVE_STRATEGY_ID,
        }

        async def run(bot):
            with (
                mock.patch.object(research, "screen", return_value=["Y1"]),
                mock.patch.object(research.sec_edgar, "current_8k_tickers",
                                  return_value={"A1"}),
                mock.patch.object(research, "build_dossier",
                                  side_effect=lambda symbol: research.Dossier(
                                      ticker=symbol, price=1.0, name=symbol,
                                      avg_volume=1_000)),
                mock.patch.object(research, "rank_score", return_value=scored),
                mock.patch.object(research, "mechanical_setup", return_value=False),
                mock.patch.object(research, "signal_from", return_value=signal),
                mock.patch.object(research, "effective_spread",
                                  return_value=(1.0, True)),
                mock.patch.object(research, "catalyst_alignment", return_value={}),
                mock.patch.object(bot, "hard_reject", return_value="test rejection"),
                mock.patch.object(bot, "market_open", return_value=False),
                mock.patch.object(bot, "_capture_entry_quotes",
                                  new=mock.AsyncMock()),
                mock.patch.object(bot, "_record_signals"),
            ):
                await bot._scan_locked("full")

        with tempfile.TemporaryDirectory() as tmp:
            bot = paper.PennyStockPaperBot(
                state_path=os.path.join(tmp, "state.json"),
                archive_path=os.path.join(tmp, "archive.jsonl"))
            bot._universe_rows = market_rows
            bot.universe_coverage = coverage
            asyncio.run(run(bot))

        self.assertEqual(bot.universe_coverage["symbols_requested"], 13_026)
        self.assertEqual(bot.universe_coverage["deep_score_target"], 2)
        self.assertEqual(bot.universe_coverage["deep_scored"], 2)
        self.assertEqual({row["ticker"] for row in bot.watchlist}, {"Y1", "A1"})
        self.assertIn("13,026 listed requested", bot.status)
        self.assertEqual(bot.universe_coverage["engine_version"], 7)

    def test_continuous_refresh_uses_requested_feed_and_keeps_deep_telemetry(self):
        market_rows = [{
            "ticker": "LIVE", "price": 1.25, "change_pct": 5,
            "day_volume": 20_000, "dollar_volume": 25_000,
            "feed": "iex",
        }]
        coverage = {
            "status": "COMPLETE", "feed": "iex",
            "symbols_requested": 13_000, "snapshots_returned": 12_000,
            "penny_price_matches": 1, "last_completed_at": 100.0,
        }

        async def run(bot, market_scan):
            with mock.patch.object(
                penny_quotes, "market_wide_penny_scan", new=market_scan
            ):
                await bot._refresh_universe_once("iex")

        with tempfile.TemporaryDirectory() as tmp:
            bot = paper.PennyStockPaperBot(
                state_path=os.path.join(tmp, "state.json"),
                archive_path=os.path.join(tmp, "archive.jsonl"))
            bot.universe_coverage = {"deep_scored": 42, "continuous_passes": 7}
            market_scan = mock.AsyncMock(return_value=(market_rows, coverage, ""))
            asyncio.run(run(bot, market_scan))

        market_scan.assert_awaited_once_with(
            research.MIN_PRICE, research.MAX_PRICE, force=True, feed="iex")
        self.assertEqual([row["ticker"] for row in bot._universe_rows], ["LIVE"])
        self.assertEqual(bot.universe_coverage["continuous_passes"], 8)
        self.assertEqual(bot.universe_coverage["continuous_target_sec"], 30)
        self.assertEqual(bot.universe_coverage["deep_scored"], 42)

    def test_continuous_loop_runs_first_pass_before_its_first_sleep(self):
        calls, waits = [], []

        async def refresh(feed):
            calls.append(feed)

        async def stop_after_first_pass(delay):
            waits.append(delay)
            raise asyncio.CancelledError

        async def run(bot):
            with mock.patch.object(bot, "_refresh_universe_once", side_effect=refresh):
                with mock.patch.object(paper.asyncio, "sleep", side_effect=stop_after_first_pass):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot._continuous_universe_loop()

        with tempfile.TemporaryDirectory() as tmp:
            bot = paper.PennyStockPaperBot(
                state_path=os.path.join(tmp, "state.json"),
                archive_path=os.path.join(tmp, "archive.jsonl"))
            asyncio.run(run(bot))

        self.assertEqual(calls, ["delayed_sip"])
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)
        self.assertLessEqual(waits[0], paper.UNIVERSE_TARGET_CYCLE_SEC)

    def test_recent_full_tape_pass_rotates_next_continuous_pass_to_iex(self):
        calls = []

        async def refresh(feed):
            calls.append(feed)

        async def stop_after_first_pass(_delay):
            raise asyncio.CancelledError

        async def run(bot):
            with mock.patch.object(bot, "_refresh_universe_once", side_effect=refresh):
                with mock.patch.object(paper.asyncio, "sleep", side_effect=stop_after_first_pass):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot._continuous_universe_loop()

        with tempfile.TemporaryDirectory() as tmp:
            bot = paper.PennyStockPaperBot(
                state_path=os.path.join(tmp, "state.json"),
                archive_path=os.path.join(tmp, "archive.jsonl"))
            bot._last_delayed_universe_scan = paper.time.time()
            asyncio.run(run(bot))

        self.assertEqual(calls, ["iex"])


if __name__ == "__main__":
    unittest.main()
