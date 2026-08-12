import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cryptal_georgian_scanner as geo
import cryptal_maker_paper as maker


class FakeResponse:
    def __init__(self, payload=None, *, status=200, body=""):
        self.status = status
        self.payload = payload
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        value = self.routes[url]
        if (isinstance(value, list) and value
                and isinstance(value[0], FakeResponse)):
            value = value.pop(0)
        return value if isinstance(value, FakeResponse) else FakeResponse(value)


def _pair(pair, base, quote, *, fee=0.0025, enabled=True):
    return {
        "pair": pair,
        "pairDisplayName": pair.replace("-", "-TO"),
        "baseCurrency": base,
        "quoteCurrency": quote,
        "baseScale": 4,
        "quoteScale": 4,
        "makerFee": fee,
        "takerFee": fee,
        "minCost": 1,
        "tradeEnabled": enabled,
    }


def _future(base, *, min_notional=5):
    return {
        "symbol": f"{base}USDT",
        "baseAsset": base,
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.0001"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "notional": str(min_notional)},
        ],
    }


def _book(bid, ask):
    now_ms = int(time.time() * 1000)
    return {
        "timestamp": now_ms,
        "bids": [{"price": str(bid), "volume": "1000"}],
        "asks": [{"price": str(ask), "volume": "1000"}],
    }


def _trades(*, age=1):
    now_ms = int((time.time() - age) * 1000)
    return [
        {"id": "t1", "price": "1", "volume": "10", "timestamp": now_ms},
        {"id": "t2", "price": "1", "volume": "10", "timestamp": now_ms - 1000},
    ]


def _routes(*, mana_fee=0.0025, old_mana=False):
    base = maker.CRYPTAL_BASE
    pairs = [
        _pair("MANA-GEL", "MANA", "GEL", fee=mana_fee),
        _pair("ETH-USD", "ETH", "USD"),
        _pair("NOHEDGE-GEL", "NOHEDGE", "GEL"),
        _pair("OFF-USD", "OFF", "USD", enabled=False),
    ]
    exchange_info = {"symbols": [_future("MANA"), _future("ETH")]}
    hedge_books = [
        {"symbol": "MANAUSDT", "bidPrice": "0.399", "askPrice": "0.401"},
        {"symbol": "ETHUSDT", "bidPrice": "1999", "askPrice": "2001"},
    ]
    return {
        f"{base}/api/v1/public/pairs": pairs,
        "https://fapi.binance.com/fapi/v1/exchangeInfo": exchange_info,
        "https://fapi.binance.com/fapi/v1/ticker/bookTicker": hedge_books,
        f"{base}/api/v1/public/orderbook/USDT-USD": _book(0.999, 1.001),
        f"{base}/api/v1/public/orderbook/USDT-GEL": _book(2.70, 2.72),
        f"{base}/api/v1/public/orderbook/USDT-EUR": _book(0.91, 0.92),
        f"{base}/api/v1/public/orderbook/MANA-GEL": _book(0.90, 1.30),
        f"{base}/api/v1/public/trades/MANA-GEL": _trades(
            age=geo.MAX_TRADE_AGE_SEC + 5 if old_mana else 1
        ),
        f"{base}/api/v1/public/orderbook/ETH-USD": _book(1900, 2100),
        f"{base}/api/v1/public/trades/ETH-USD": _trades(),
    }


def _meta(pair="MANA-GEL", score=500):
    base, quote = pair.split("-")
    return {
        "pair": pair,
        "display_pair": pair.replace("-", "-TO"),
        "base_asset": base,
        "quote_currency": quote,
        "hedge_symbol": f"{base}USDT",
        "stable_pair": f"USDT-{quote}",
        "maker_fee_rate": 0.0025,
        "maker_fee_bps": 25,
        "minimum_cost_quote": 1,
        "price_tick": 0.0001,
        "quantity_step": 0.0001,
        "screen_score": score,
        "conservative_net_bps": score,
        "qualified": True,
    }


class TestSharedCryptalPublicDataHub(unittest.TestCase):
    def test_duplicate_public_request_is_shared_between_consumers(self):
        url = f"{maker.CRYPTAL_BASE}/api/v1/public/orderbook/USDT-GEL"
        session = FakeSession({url: _book(2.70, 2.72)})
        hub = maker.CryptalPublicDataHub(cache_ttl_sec=5)

        async def run():
            first = await hub.get(session, url, {"limit": 25})
            second = await hub.get(session, url, {"limit": 25})
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(hub.state()["cache_hits"], 1)

    def test_403_is_sanitized_and_starts_process_wide_backoff(self):
        url = f"{maker.CRYPTAL_BASE}/api/v1/public/pairs"
        session = FakeSession({url: FakeResponse(
            status=403, body="<html><h1>403 Forbidden</h1></html>"
        )})
        hub = maker.CryptalPublicDataHub()

        async def run():
            with self.assertRaises(maker.CryptalRateLimitError) as first:
                await hub.get(session, url)
            with self.assertRaises(maker.CryptalRateLimitError):
                await hub.get(session, url)
            return str(first.exception)

        message = asyncio.run(run())
        self.assertNotIn("<html>", message)
        self.assertIn("shared collector backing off", message)
        self.assertEqual(len(session.calls), 1)
        self.assertGreater(hub.retry_delay(), 0)


class TestAllMarketScanner(unittest.TestCase):
    def test_scans_every_hedgeable_cryptal_pair_and_ranks_candidates(self):
        session = FakeSession(_routes())
        scanner = geo.CryptalGeorgianMarketScanner(
            maker.CryptalPublicDataHub()
        )
        asyncio.run(scanner.scan_once(session))

        self.assertEqual(scanner.catalog_count, 4)
        self.assertEqual(scanner.eligible_count, 2)
        self.assertEqual(scanner.scanned_count, 2)
        self.assertEqual({row["pair"] for row in scanner.markets}, {
            "MANA-GEL", "ETH-USD"
        })
        self.assertEqual(scanner.opportunities[0]["pair"], "MANA-GEL")
        called = {url for url, _params in session.calls}
        for pair in ("MANA-GEL", "ETH-USD"):
            self.assertIn(
                f"{maker.CRYPTAL_BASE}/api/v1/public/orderbook/{pair}", called
            )
            self.assertIn(
                f"{maker.CRYPTAL_BASE}/api/v1/public/trades/{pair}", called
            )

    def test_stale_flow_and_real_market_fee_fail_closed(self):
        stale = geo.CryptalGeorgianMarketScanner(maker.CryptalPublicDataHub())
        asyncio.run(stale.scan_once(FakeSession(_routes(old_mana=True))))
        mana = next(row for row in stale.markets if row["pair"] == "MANA-GEL")
        self.assertFalse(mana["qualified"])

        expensive = geo.CryptalGeorgianMarketScanner(maker.CryptalPublicDataHub())
        asyncio.run(expensive.scan_once(FakeSession(_routes(mana_fee=0.20))))
        mana = next(row for row in expensive.markets if row["pair"] == "MANA-GEL")
        self.assertEqual(mana["maker_fee_bps"], 2000)
        self.assertFalse(mana["qualified"])

    def test_state_is_explicitly_paper_only_and_non_evidentiary(self):
        scanner = geo.CryptalGeorgianMarketScanner(maker.CryptalPublicDataHub())
        state = scanner.state()
        self.assertFalse(state["evidentiary"])
        self.assertEqual(state["scan_interval_sec"], 60)
        self.assertIn("Every active Cryptal market", state["method"])


class TestSelectedMarketCollector(unittest.TestCase):
    def test_generic_pair_uses_its_fee_tick_size_and_hedge_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = maker.CryptalMakerPaperBot(
                state_path=os.path.join(tmp, "mana.json"),
                pair="MANA-GEL",
                stable_pair="USDT-GEL",
                display_pair="MANA-TOGEL",
                base_asset="MANA",
                quote_currency="GEL",
                hedge_symbol="MANAUSDT",
                price_tick=0.0001,
                quantity_step=0.01,
                maker_fee_rate=0.003,
                minimum_cost_quote=10,
            )
            stamp = time.time()
            snapshot = {
                "received_at": stamp,
                "cryptal_at": stamp,
                "stable_at": stamp,
                "settlement_at": stamp,
                "binance_at": stamp,
                "pair_label": "MANA-GEL",
                "stable_pair_label": "USDT-GEL",
                "settlement_pair_label": "USDT-USD",
                "quote_currency": "GEL",
                "bids": [(0.90, 1000)], "asks": [(1.30, 1000)],
                "stable_bids": [(2.70, 1000)], "stable_asks": [(2.72, 1000)],
                "settlement_bids": [(0.999, 1000)],
                "settlement_asks": [(1.001, 1000)],
                "binance_bid": 0.399, "binance_ask": 0.401,
                "trades": [],
            }
            bot._tick(snapshot)
            quote = dict(bot.quote)
            self.assertEqual(quote["price"], 0.9001)
            self.assertGreater(quote["qty"], 50)
            self.assertAlmostEqual(quote["qty"] / 0.01, round(quote["qty"] / 0.01))
            trade = {
                "id": "after", "side": "ASK", "price": quote["price"],
                "volume": quote["qty"],
                "timestamp": int((quote["activation_exchange_at"] + 1) * 1000),
            }
            bot._tick({**snapshot, "cryptal_at": stamp + 1, "trades": [trade]})
            self.assertGreater(bot.spot_qty, 0)
            self.assertEqual(bot.spot_qty, bot.short_qty)
            state = bot.state()
            self.assertEqual(state["base_asset"], "MANA")
            self.assertEqual(state["hedge"], "Binance MANAUSDT perpetual")
            self.assertEqual(state["fees"]["cryptal_maker_bps_each_fill"], 30)
            self.assertFalse(state["live_trading_enabled"])

    def test_supervisor_skips_fixed_btc_collectors_and_never_switches_inventory(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            geo, "SUPERVISOR_STATE_PATH", os.path.join(tmp, "supervisor.json")
        ), mock.patch.object(geo.config, "DATA_DIR", tmp):
            scanner = geo.CryptalGeorgianMarketScanner(maker.CryptalPublicDataHub())
            btc = _meta("BTC-GEL", 900)
            mana = _meta("MANA-GEL", 500)
            eth = _meta("ETH-USD", 700)
            scanner.opportunities = [btc, mana]
            scanner.markets = [btc, mana]
            supervisor = geo.CryptalBestGeorgianMarketPaperBot(
                scanner, scanner.data_hub
            )
            supervisor._choose()
            self.assertEqual(supervisor.active_pair, "MANA-GEL")
            supervisor.bot.spot_qty = supervisor.bot.short_qty = 1
            scanner.opportunities = [eth, mana]
            scanner.markets = [eth, mana]
            supervisor._choose()
            self.assertEqual(supervisor.active_pair, "MANA-GEL")

    def test_persisted_pair_must_still_qualify_before_it_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            geo, "SUPERVISOR_STATE_PATH", os.path.join(tmp, "supervisor.json")
        ), mock.patch.object(geo.config, "DATA_DIR", tmp):
            Path(geo.SUPERVISOR_STATE_PATH).write_text(
                '{"enabled": true, "active_pair": "ETH-USD"}', encoding="utf-8"
            )
            scanner = geo.CryptalGeorgianMarketScanner(maker.CryptalPublicDataHub())
            stale = {**_meta("ETH-USD", 900), "qualified": False}
            mana = _meta("MANA-GEL", 500)
            scanner.markets = [stale, mana]
            scanner.opportunities = [mana]
            supervisor = geo.CryptalBestGeorgianMarketPaperBot(
                scanner, scanner.data_hub
            )
            supervisor._choose()
            self.assertEqual(supervisor.active_pair, "MANA-GEL")

    def test_dashboard_exposes_one_separate_all_market_panel(self):
        dashboard = Path("dashboard.py").read_text(encoding="utf-8")
        self.assertIn('id="cryptalgeo-panel"', dashboard)
        self.assertIn('web.get("/api/cryptalgeo/state"', dashboard)
        self.assertIn('web.post("/api/cryptalgeo/scan"', dashboard)
        self.assertIn("CRYPTALGEOSCANNER.manage_loop()", dashboard)
        self.assertIn("CRYPTALGEOBOT.manage_loop()", dashboard)


if __name__ == "__main__":
    unittest.main()
