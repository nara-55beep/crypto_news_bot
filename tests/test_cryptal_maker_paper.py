import os
import tempfile
import time
import unittest
from pathlib import Path

import cryptal_maker_paper as maker


def _snapshot(trades=None, *, bid=64_000.0, ask=66_000.0,
              stable_bid=0.999, stable_ask=1.001,
              binance_bid=65_000.0, binance_ask=65_001.0,
              stamp=None, bid_levels=None, ask_levels=None):
    stamp = time.time() if stamp is None else stamp
    return {
        "received_at": time.time(),
        "cryptal_at": stamp,
        "stable_at": stamp,
        "binance_at": stamp,
        "bids": bid_levels or [(bid, 0.01), (bid - 100, 0.02)],
        "asks": ask_levels or [(ask, 0.01), (ask + 100, 0.02)],
        "stable_bids": [(stable_bid, 100.0)],
        "stable_asks": [(stable_ask, 100.0)],
        "binance_bid": binance_bid,
        "binance_ask": binance_ask,
        "trades": trades or [],
    }


class TestCryptalMakerPaper(unittest.TestCase):
    def _bot(self, directory):
        return maker.CryptalMakerPaperBot(
            state_path=os.path.join(directory, "cryptal-maker.json"))

    def test_fair_value_converts_binance_usdt_through_cryptal_usdt_usd(self):
        market = maker.CryptalMakerPaperBot._snapshot_market(_snapshot(
            stable_bid=0.98, stable_ask=1.00,
            binance_bid=65_000, binance_ask=65_002,
        ))
        self.assertAlmostEqual(market["stable_mid"], 0.99)
        self.assertAlmostEqual(market["fair_usd"], 65_001 * 0.99)
        self.assertGreater(market["cryptal_spread_bps"], 300)

    def test_startup_absorbs_trade_backlog_before_placing_a_bid(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            old = {"id": "100", "side": "ASK", "price": "63000",
                   "volume": "1", "timestamp": int(time.time() * 1000)}
            bot._tick(_snapshot([old]))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(bot.quote["side"], "BID")
            self.assertIn("100", bot._seen_trade_set)

    def test_new_sell_trade_fills_bid_and_immediately_creates_equal_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            fill = {"id": "101", "side": "ASK", "price": quote["price"],
                    "volume": quote["qty"], "timestamp": int(time.time() * 1000)}
            bot._tick(_snapshot([fill]))

            self.assertGreater(bot.spot_qty, 0)
            self.assertAlmostEqual(bot.spot_qty, bot.short_qty, places=10)
            self.assertLess(bot.short_entry_usdt, 65_000)  # slippage is adverse
            self.assertEqual(bot.quote["side"], "ASK")
            self.assertFalse(bot.live_trading_enabled)

    def test_crossing_price_is_authoritative_when_public_side_semantics_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            # Cryptal documents ASK/BID but not whether this is maker or aggressor.
            # A print through a uniquely improved best bid would fill it either way.
            trade = {"id": "side-ambiguous", "side": "BID",
                     "price": quote["price"], "volume": quote["qty"],
                     "timestamp": int(time.time() * 1000)}
            bot._tick(_snapshot([trade]))

            self.assertGreater(bot.spot_qty, 0)
            self.assertAlmostEqual(bot.spot_qty, bot.short_qty, places=10)

    def test_visible_queue_must_trade_before_the_paper_order_can_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            snapshot = _snapshot(
                [], bid=64_800, ask=66_000,
                bid_levels=[(64_800, 0.10), (64_500, 0.20), (64_000, 0.30)],
            )
            bot._tick(snapshot)
            self.assertGreater(bot.quote["queue_ahead_btc"], 0.1)
            trade = {"id": "102", "side": "ASK", "price": bot.quote["price"],
                     "volume": "0.05", "timestamp": int(time.time() * 1000)}
            bot._tick({**snapshot, "trades": [trade], "received_at": time.time(),
                       "cryptal_at": time.time(), "stable_at": time.time()})

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertGreater(bot.quote["queue_ahead_btc"], 0.0)

    def test_late_page_of_pre_quote_trades_cannot_create_a_phantom_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            old_trade = {
                "id": "late-page", "side": "ASK", "price": quote["price"],
                "volume": quote["qty"],
                "timestamp": int((quote["placed_at"] - 5) * 1000),
            }
            bot._tick(_snapshot([old_trade]))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)

    def test_completed_cycle_includes_both_spot_fees_and_both_hedge_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            buy_quote = dict(bot.quote)
            sell_into_bid = {
                "id": "201", "side": "ASK", "price": buy_quote["price"],
                "volume": buy_quote["qty"], "timestamp": int(time.time() * 1000),
            }
            bot._tick(_snapshot([sell_into_bid]))
            ask_quote = dict(bot.quote)
            buy_from_ask = {
                "id": "202", "side": "BID", "price": ask_quote["price"],
                "volume": ask_quote["qty"], "timestamp": int(time.time() * 1000) + 1,
            }
            bot._tick(_snapshot([sell_into_bid, buy_from_ask]))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(len(bot.history), 1)
            gross_spread = ask_quote["price"] - buy_quote["price"]
            realized_per_btc = bot.history[0]["pnl"] / buy_quote["qty"]
            self.assertGreater(bot.history[0]["pnl"], 0)
            self.assertLess(realized_per_btc, gross_spread)
            self.assertEqual(bot.quote["side"], "BID")

    def test_pausing_entries_keeps_the_risk_reducing_ask_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            bid = dict(bot.quote)
            fill = {"id": "pause-fill", "side": "ASK", "price": bid["price"],
                    "volume": bid["qty"], "timestamp": int(time.time() * 1000)}
            bot._tick(_snapshot([fill]))
            self.assertEqual(bot.quote["side"], "ASK")

            bot.set_enabled(False)
            bot._tick(_snapshot([fill]))

            self.assertFalse(bot.enabled)
            self.assertEqual(bot.quote["side"], "ASK")
            self.assertGreater(bot.spot_qty, 0)

    def test_expired_inventory_crosses_the_real_bid_and_books_the_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            bid = dict(bot.quote)
            fill = {"id": "old-cycle", "side": "ASK", "price": bid["price"],
                    "volume": bid["qty"], "timestamp": int(time.time() * 1000)}
            bot._tick(_snapshot([fill]))
            bot._cycle_opened_at = time.time() - maker.MAX_HEDGED_HOLD_SEC - 1

            bot._tick(_snapshot([fill], bid=63_000, ask=66_000))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(bot.history[0]["reason"], "24h_time_stop_crossed_spread")
            self.assertLess(bot.history[0]["pnl"], 0)

    def test_stale_exchange_timestamp_cancels_a_working_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            self.assertIsNotNone(bot.quote)
            bot._tick(_snapshot([], stamp=time.time() - maker.MAX_DATA_AGE_SEC - 1))

            self.assertIsNone(bot.quote)
            self.assertIn("stale", bot.data_error)

    def test_stale_hedge_book_also_cancels_a_working_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot._tick(_snapshot([]))
            stale = _snapshot([])
            stale["binance_at"] = time.time() - maker.MAX_DATA_AGE_SEC - 1
            bot._tick(stale)

            self.assertIsNone(bot.quote)
            self.assertIn("Binance hedge book is stale", bot.data_error)

    def test_results_never_authorize_live_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._bot(tmp)
            bot.history = [{"pnl": 100.0}] * maker.MIN_PAPER_CYCLES
            state = bot.state()

            self.assertEqual(state["validation"]["status"], "PAPER_RESULT_ONLY")
            self.assertFalse(state["validation"]["evidentiary"])
            self.assertFalse(state["live_trading_enabled"])
            self.assertEqual(state["mode"], "PAPER_ONLY")

    def test_dashboard_starts_and_exposes_the_paper_collector(self):
        dashboard = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(
            encoding="utf-8")
        self.assertIn("import cryptal_maker_paper", dashboard)
        self.assertIn('id="cryptalmaker-panel"', dashboard)
        self.assertIn(
            'class="bot cryptal-featured" id="cryptalmaker-panel"', dashboard)
        self.assertIn('cryptal-new-badge">NEW STRATEGY', dashboard)
        self.assertLess(
            dashboard.index('id="cryptalmaker-panel"'),
            dashboard.index('id="lucidcont-panel"'),
        )
        self.assertIn('web.get("/api/cryptalmaker/state"', dashboard)
        self.assertIn('web.post("/api/cryptalmaker/toggle"', dashboard)
        self.assertIn("asyncio.create_task(CRYPTALMAKER.manage_loop())", dashboard)
        self.assertIn("No private API or real-order code exists", dashboard)


if __name__ == "__main__":
    unittest.main()
