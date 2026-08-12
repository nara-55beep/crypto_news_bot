import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import cryptal_maker_paper as maker


def gel_snapshot(trades=None, *, stamp=None):
    stamp = time.time() if stamp is None else stamp
    return {
        "received_at": time.time(),
        "cryptal_at": stamp,
        "stable_at": stamp,
        "settlement_at": stamp,
        "binance_at": stamp,
        "pair_label": "BTC-GEL",
        "stable_pair_label": "USDT-GEL",
        "settlement_pair_label": "USDT-USD",
        "quote_currency": "GEL",
        "bids": [(162_000.0, 0.01), (161_900.0, 0.02)],
        "asks": [(170_000.0, 0.01), (170_100.0, 0.02)],
        "stable_bids": [(2.70, 100.0)],
        "stable_asks": [(2.72, 100.0)],
        "settlement_bids": [(0.999, 100.0)],
        "settlement_asks": [(1.001, 100.0)],
        "binance_bid": 61_000.0,
        "binance_ask": 61_001.0,
        "trades": trades or [],
    }


def after(quote, seconds=1.0):
    return int((float(quote["activation_exchange_at"]) + seconds) * 1000)


class FakeResponse:
    def __init__(self, payload):
        self.status = 200
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResponse(self.payloads[url])


class TestCryptalGelMaker(unittest.TestCase):
    def bot(self, directory):
        return maker.CryptalGelMakerPaperBot(
            state_path=os.path.join(directory, "cryptal-gel.json"))

    def test_gel_ledger_is_separate_and_identifies_the_real_cryptal_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.bot(tmp)
            state = bot.state()

        self.assertEqual(bot.pair, "BTC-GEL")
        self.assertEqual(bot.stable_pair, "USDT-GEL")
        self.assertEqual(state["display_pair"], "BTC-TOGEL")
        self.assertEqual(state["quote_currency"], "GEL")
        self.assertNotEqual(maker.GEL_STATE_PATH, maker.STATE_PATH)
        self.assertFalse(state["live_trading_enabled"])

    def test_gel_fair_value_uses_usdt_gel_and_reports_usd_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            market = self.bot(tmp)._snapshot_market(gel_snapshot())

        self.assertAlmostEqual(market["stable_mid"], 2.71)
        self.assertAlmostEqual(market["settlement_mid"], 1.0)
        self.assertAlmostEqual(market["fair_quote"], 61_000.5 * 2.71)
        self.assertAlmostEqual(market["fair_usd_equivalent"], 61_000.5)

    def test_first_tick_prefunds_50_usd_equivalent_gel_and_caps_quote_at_40(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.bot(tmp)
            bot._tick(gel_snapshot())
            state = bot.state()

        self.assertEqual(state["equity"], 100.0)
        self.assertEqual(state["total_pnl"], 0.0)
        self.assertGreater(state["cryptal_cash_quote"], 100.0)
        self.assertEqual(state["starting_allocation"]["cryptal_usd_equivalent"], 50.0)
        self.assertEqual(state["starting_allocation"]["maximum_quote_notional_usd"], 40.0)
        quote = state["quote"]
        native_notional = quote["price"] * quote["qty"]
        usd_equivalent = native_notional / 2.72 * 0.999
        self.assertGreaterEqual(native_notional, 10.0)
        self.assertLessEqual(usd_equivalent, 40.0 + 1e-6)

    def test_fetch_uses_btc_gel_both_conversion_books_and_public_binance(self):
        now_ms = int(time.time() * 1000)
        root = maker.CRYPTAL_BASE + "/api/v1/public"
        book = {
            "timestamp": now_ms,
            "bids": [{"price": "1", "volume": "1"}],
            "asks": [{"price": "2", "volume": "1"}],
        }
        urls = {
            f"{root}/orderbook/BTC-GEL": {
                "timestamp": now_ms,
                "bids": [{"price": "162000", "volume": "0.01"}],
                "asks": [{"price": "170000", "volume": "0.01"}],
            },
            f"{root}/orderbook/USDT-GEL": {
                "timestamp": now_ms,
                "bids": [{"price": "2.70", "volume": "10"}],
                "asks": [{"price": "2.72", "volume": "10"}],
            },
            f"{root}/trades/BTC-GEL": [],
            maker.BINANCE_BOOK_URL: {
                "time": now_ms, "bidPrice": "61000", "askPrice": "61001",
            },
            f"{root}/orderbook/USDT-USD": {
                "timestamp": now_ms,
                "bids": [{"price": "0.999", "volume": "10"}],
                "asks": [{"price": "1.001", "volume": "10"}],
            },
        }
        session = FakeSession(urls)
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = asyncio.run(self.bot(tmp)._fetch_snapshot(session))

        self.assertEqual(len(session.calls), 5)
        self.assertEqual({url for url, _ in session.calls}, set(urls))
        self.assertEqual(snapshot["pair_label"], "BTC-GEL")
        self.assertEqual(snapshot["stable_pair_label"], "USDT-GEL")
        self.assertEqual(snapshot["settlement_pair_label"], "USDT-USD")

    def test_production_tick_fills_gel_bid_and_opens_an_equal_binance_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.bot(tmp)
            bot._tick(gel_snapshot())
            bid = dict(bot.quote)
            fill = {
                "id": "gel-buy-1", "side": "ASK", "price": bid["price"],
                "volume": bid["qty"], "timestamp": after(bid),
            }
            bot._tick(gel_snapshot([fill]))

            self.assertGreater(bot.spot_qty, 0)
            self.assertAlmostEqual(bot.spot_qty, bot.short_qty, places=10)
            self.assertEqual(bot.quote["side"], "ASK")
            self.assertEqual(
                bot.state()["starting_allocation"]["cryptal_quote_amount"],
                round(bot.starting_quote_cash, 4),
            )
            self.assertNotEqual(bot.cryptal_cash_usd, bot.starting_quote_cash)
            self.assertFalse(bot.live_trading_enabled)

    def test_completed_gel_cycle_reconciles_in_usd_equivalent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.bot(tmp)
            first = gel_snapshot()
            bot._tick(first)
            starting_equity = bot.initial_equity_usd
            bid = dict(bot.quote)
            buy = {
                "id": "gel-cycle-buy", "side": "ASK", "price": bid["price"],
                "volume": bid["qty"], "timestamp": after(bid),
            }
            bot._tick(gel_snapshot([buy]))
            ask = dict(bot.quote)
            sell = {
                "id": "gel-cycle-sell", "side": "BID", "price": ask["price"],
                "volume": ask["qty"], "timestamp": after(ask, 2.0),
            }
            bot._tick(gel_snapshot([buy, sell]))

            expected = bot._equity(bot.market) - starting_equity
            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(len(bot.history), 1)
            self.assertAlmostEqual(bot.history[0]["pnl"], expected, places=6)
            self.assertAlmostEqual(bot.state()["total_pnl"], expected, places=4)

    def test_restart_restores_only_the_gel_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-gel.json")
            first = maker.CryptalGelMakerPaperBot(state_path=path)
            first._tick(gel_snapshot())
            first.poll_count = 77
            first._save()
            resumed = maker.CryptalGelMakerPaperBot(state_path=path)
            wrong_market = maker.CryptalMakerPaperBot(state_path=path)

        self.assertEqual(resumed.poll_count, 77)
        self.assertEqual(resumed.pair, "BTC-GEL")
        self.assertGreater(resumed.cryptal_cash_usd, 100.0)
        self.assertEqual(wrong_market.cryptal_cash_usd, maker.START_CRYPTAL_USD)
        self.assertIn("state for BTC-GEL ignored", wrong_market.persistence_error)

    def test_dashboard_runs_both_ledgers_and_exposes_independent_controls(self):
        dashboard = Path("dashboard.py").read_text(encoding="utf-8")
        self.assertIn("CRYPTAL_DATA = cryptal_maker_paper.CryptalPublicDataHub(", dashboard)
        self.assertIn("CRYPTALGELMAKER = cryptal_maker_paper.CryptalGelMakerPaperBot(", dashboard)
        self.assertIn("data_hub=CRYPTAL_DATA", dashboard)
        self.assertIn('id="cryptalgelmaker-panel"', dashboard)
        self.assertIn('web.get("/api/cryptalgelmaker/state"', dashboard)
        self.assertIn('web.post("/api/cryptalgelmaker/toggle"', dashboard)
        self.assertIn('web.post("/api/cryptalgelmaker/reset"', dashboard)
        self.assertIn("asyncio.create_task(CRYPTALGELMAKER.manage_loop())", dashboard)
        self.assertIn("loadCryptalGelMaker()", dashboard)

    def test_market_audit_fails_closed_for_non_clob_georgian_services(self):
        audit = maker.GEORGIAN_MARKET_AUDIT
        report = Path(audit["report"]).read_text(encoding="utf-8")

        self.assertEqual(audit["registered_vasps_in_scope"], 42)
        self.assertEqual(
            audit["included"],
            ["Cryptal public market catalog (81 observed; 60 hedgeable when checked)"],
        )
        self.assertIn("WhiteBIT Georgia", audit["excluded"])
        self.assertIn("Bybit Georgia", audit["excluded"])
        self.assertIn("no Georgia-isolated order book or GEL spot pair", report)
        self.assertIn("no public central limit order book plus timestamped trade tape", report)


if __name__ == "__main__":
    unittest.main()
