import json
import unittest
from unittest import mock

import cryptal_georgian_scanner as cryptal_geo
import cryptal_maker_paper as maker
import georgian_venue_scanner as venues


class FixedClock:
    def __init__(self, now=1_800_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now


def _market_books():
    spot = {"BTCUSDT": {"bid": 100.0, "ask": 101.0}}
    futures = {"BTCUSDT": {"bid": 100.2, "ask": 100.8}}
    settlement = {
        "USDT": {"bid": 1.0, "ask": 1.0, "pair": "USDT"},
        "USD": {"bid": 0.999, "ask": 1.001, "pair": "USDT-USD"},
        "GEL": {"bid": 2.69, "ask": 2.71, "pair": "USDT-GEL"},
    }
    return settlement, spot, futures


class TestOfficialRegistryScope(unittest.TestCase):
    def test_registry_has_all_42_active_head_offices_once(self):
        self.assertEqual(len(venues.REGISTERED_VASPS), 42)
        self.assertEqual(len(set(venues.REGISTERED_VASPS)), 42)
        names = {name for _registration, name in venues.REGISTERED_VASPS}
        self.assertTrue({"Cryptal", "Coinet", "Mycoins", "PLEX"} <= names)

    def test_capability_audit_does_not_call_global_books_local(self):
        audit = {row["name"]: row for row in venues._capability_rows()}
        self.assertEqual(
            audit["Cryptal"]["capability"], "public_orderbook_and_trades"
        )
        self.assertIn("fixed_quote", audit["Coinet"]["capability"])
        self.assertIn("no_verified_local_gel_book", audit["WhiteBIT"]["capability"])
        self.assertIn(
            "no_verified_local_gel_book",
            audit["Bybit Georgia Limited"]["capability"],
        )
        self.assertEqual(
            audit["Bitanica"]["capability"],
            "no_verified_public_machine_market_feed",
        )


class TestVenueParsers(unittest.TestCase):
    def test_coinet_uses_published_bid_ask_and_limits(self):
        rows = venues.GeorgianVenueOpportunityScanner.parse_coinet({
            "data": [{
                "currency1": "BTC",
                "currency2": "USDT",
                "sellRate": 95,
                "buyRate": 105,
                "limitCurrency": "USDT",
                "limitMin": 10,
                "limitMax": 3000,
            }]
        }, 123.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bid"], 95)
        self.assertEqual(rows[0]["ask"], 105)
        self.assertEqual((rows[0]["min_quote"], rows[0]["max_quote"]), (10, 3000))

    def test_mycoins_signalr_snapshot_is_parsed_and_size_fails_closed(self):
        message = json.dumps({
            "type": 1,
            "target": "UpdateCurrencyRate",
            "arguments": [[{
                "currency1_abbr": "BTC",
                "currency2_abbr": "GEL",
                "sell_rate": "260",
                "buy_rate": "270",
                "expiration_utc": "2027-01-01T00:00:00Z",
            }]],
        }) + "\x1e"
        snapshot = venues.MycoinsRateFeed.parse_signalr_message(message)
        rows = venues.GeorgianVenueOpportunityScanner.parse_mycoins(snapshot, 123.0)
        self.assertEqual(rows[0]["base"], "BTC")
        self.assertFalse(rows[0]["size_verified"])
        self.assertEqual(rows[0]["quote"], "GEL")

    def test_platforma_directed_route_applies_output_fee_and_stays_manual(self):
        payload = {"routes": [{
            "from": {"symbol": "GEL", "name": "Cash Tbilisi", "min": "20", "max": "0"},
            "to": {"symbol": "BTC", "name": "BTC"},
            "rate": {"in": 270, "out": 1, "outFeeAmount": 0.01},
            "payType": "manual",
            "routeId": "route-1",
            "isShowWeb": True,
        }]}
        row = venues.GeorgianVenueOpportunityScanner.parse_platforma(payload, 100.0)[0]
        self.assertEqual(row["side"], "local_buy")
        self.assertEqual(row["output_fee"], 0.01)
        self.assertTrue(row["manual"])

    def test_platforma_includes_direct_gel_usdt_routes(self):
        payload = {"routes": [{
            "from": {"symbol": "GEL", "name": "Cash GEL", "min": "50", "max": "0"},
            "to": {"symbol": "USDT", "name": "Tether"},
            "rate": {"in": 2.7, "out": 1, "outFeeAmount": 15},
            "payType": "manual",
            "routeId": "stable-1",
            "isShowWeb": True,
        }]}
        row = venues.GeorgianVenueOpportunityScanner.parse_platforma(payload, 100.0)[0]
        self.assertEqual((row["base"], row["quote"], row["side"]), ("USDT", "GEL", "local_buy"))


class TestConservativeOpportunityMath(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.scanner = venues.GeorgianVenueOpportunityScanner(
            mock.Mock(spec=maker.CryptalPublicDataHub), clock=self.clock
        )
        self.settlement, self.spot, self.futures = _market_books()

    def _coinet(self, **changes):
        row = {
            "venue": "Coinet",
            "registration_id": "0006-9404",
            "base": "BTC",
            "quote": "USDT",
            "bid": 80.0,
            "ask": 90.0,
            "min_quote": 0.1,
            "max_quote": 3000.0,
            "size_verified": True,
            "quote_kind": "account fixed quote",
            "workflow": "remote conversion after account/KYC",
            "captured_at": self.clock.now,
            "source": venues.COINET_URL,
        }
        row.update(changes)
        return row

    def test_positive_screen_pays_spot_hedge_transfer_and_operations_costs(self):
        results = self.scanner._evaluate_book(
            self._coinet(), self.settlement, self.spot, self.futures
        )
        local_buy = results[0]
        expected_gross = (100.0 - 90.0) / 90.0 * 1e4
        expected_basis = abs(100.5 - 100.5) / 100.5 * 1e4
        expected_cost = (
            venues.BINANCE_SPOT_TAKER_BPS
            + 2 * maker.BINANCE_TAKER_FEE_RATE * 1e4
            + 2 * maker.HEDGE_SLIPPAGE_BPS
            + max(maker.FUNDING_AND_BASIS_RESERVE_BPS, expected_basis)
            + venues.OPERATIONS_RESERVE_BPS
            + venues.TRANSFER_RESERVE_USD / venues.QUOTE_NOTIONAL_USD * 1e4
        )
        self.assertAlmostEqual(local_buy["gross_edge_bps"], expected_gross, places=1)
        self.assertAlmostEqual(local_buy["modeled_cost_bps"], expected_cost, places=1)
        self.assertAlmostEqual(
            local_buy["net_edge_bps"], expected_gross - expected_cost, places=1
        )
        self.assertTrue(local_buy["screen_candidate"])
        self.assertFalse(local_buy["paper_fill_allowed"])
        self.assertFalse(local_buy["live_trading_enabled"])

    def test_unknown_mycoins_limits_block_even_an_extreme_quote(self):
        result = self.scanner._evaluate_book(
            self._coinet(venue="Mycoins", ask=50, size_verified=False),
            self.settlement,
            self.spot,
            self.futures,
        )[0]
        self.assertFalse(result["screen_candidate"])
        self.assertIn("size not verified", result["reason"])

    def test_stale_quote_is_not_a_candidate(self):
        result = self.scanner._evaluate_book(
            self._coinet(captured_at=self.clock.now - 31),
            self.settlement,
            self.spot,
            self.futures,
        )[0]
        self.assertFalse(result["screen_candidate"])
        self.assertIn("stale public quote", result["reason"])

    def test_direct_usdt_gel_screen_uses_executable_cryptal_sides(self):
        row = self._coinet(base="USDT", quote="GEL", bid=2.50, ask=2.60)
        results = self.scanner._evaluate_book(row, self.settlement, {}, {})
        buy = results[0]
        gross = (2.69 - 2.60) / 2.60 * 1e4
        stable_cost = (
            maker.CRYPTAL_MAKER_FEE_RATE * 1e4
            + venues.OPERATIONS_RESERVE_BPS
            + venues.TRANSFER_RESERVE_USD / venues.QUOTE_NOTIONAL_USD * 1e4
        )
        self.assertAlmostEqual(buy["gross_edge_bps"], gross, places=1)
        self.assertAlmostEqual(buy["net_edge_bps"], gross - stable_cost, places=1)

    def test_expired_stablecoin_quote_cannot_clear_the_screen(self):
        row = self._coinet(
            base="USDT", quote="GEL", bid=1.90, ask=2.00,
            expires_at=self.clock.now - 1,
        )
        buy = self.scanner._evaluate_book(row, self.settlement, {}, {})[0]
        self.assertGreater(buy["net_edge_bps"], venues.MIN_SCREENED_EDGE_BPS)
        self.assertFalse(buy["screen_candidate"])
        self.assertIn("stale public quote", buy["reason"])

    def test_manual_platforma_route_never_becomes_an_automated_candidate(self):
        route = {
            "venue": "PlatformaEX / PLEX",
            "registration_id": "0017-9404",
            "base": "BTC",
            "quote": "USDT",
            "side": "local_buy",
            "rate_in": 50,
            "rate_out": 1,
            "output_fee": 0,
            "min_input": 1,
            "max_input": 0,
            "size_verified": True,
            "manual": True,
            "captured_at": self.clock.now,
            "quote_kind": "directed fixed route",
            "workflow": "manual route",
            "source": venues.PLATFORMA_URL,
        }
        result = self.scanner._evaluate_route(
            route, self.settlement, self.spot, self.futures
        )[0]
        self.assertGreater(result["net_edge_bps"], 0)
        self.assertFalse(result["screen_candidate"])
        self.assertIn("manual/cash workflow", result["reason"])


class TestScannerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_settlement_books_use_executable_sides_through_shared_hub(self):
        hub = mock.Mock(spec=maker.CryptalPublicDataHub)
        hub.get = mock.AsyncMock(side_effect=[
            {"bids": [{"price": "0.999", "volume": "10"}],
             "asks": [{"price": "1.001", "volume": "10"}]},
            {"bids": [{"price": "2.69", "volume": "10"}],
             "asks": [{"price": "2.71", "volume": "10"}]},
        ])
        scanner = venues.GeorgianVenueOpportunityScanner(hub)
        books = await scanner._settlement_books(None)
        self.assertEqual((books["USD"]["bid"], books["USD"]["ask"]), (0.999, 1.001))
        self.assertEqual((books["GEL"]["bid"], books["GEL"]["ask"]), (2.69, 2.71))

    async def test_production_shaped_scan_reports_sources_without_enabling_trades(self):
        clock = FixedClock()
        feed = venues.MycoinsRateFeed(clock=clock)
        feed.rates = [{
            "currency1_abbr": "BTC",
            "currency2_abbr": "GEL",
            "sell_rate": 260,
            "buy_rate": 270,
        }]
        feed.last_update_at = clock.now
        scanner = venues.GeorgianVenueOpportunityScanner(
            mock.Mock(spec=maker.CryptalPublicDataHub),
            clock=clock,
            mycoins_feed=feed,
        )
        responses = {
            venues.COINET_URL: {"data": [{
                "currency1": "BTC", "currency2": "USDT",
                "sellRate": 80, "buyRate": 90,
                "limitMin": 0.1, "limitMax": 3000,
            }]},
            venues.BINANCE_SPOT_BOOK_URL: [
                {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "101"}
            ],
            venues.BINANCE_FUTURES_BOOK_URL: [
                {"symbol": "BTCUSDT", "bidPrice": "100.2", "askPrice": "100.8"}
            ],
            venues.PLATFORMA_URL: {"routes": []},
        }

        async def fake_json(_session, url, **_kwargs):
            return responses[url]

        with mock.patch.object(scanner, "_json", side_effect=fake_json), mock.patch.object(
            scanner, "_settlement_books", return_value=_market_books()[0]
        ):
            await scanner.scan_once(None)

        state = scanner.state()
        self.assertEqual(state["registered_vasp_count"], 42)
        self.assertEqual(state["same_as_cryptal_count"], 1)
        self.assertGreater(state["scanned_quote_count"], 0)
        self.assertFalse(state["live_trading_enabled"])
        self.assertTrue(all(not row["paper_fill_allowed"] for row in state["rows"]))

    def test_cryptal_state_embeds_companion_venue_state(self):
        companion = mock.Mock()
        companion.state.return_value = {"registered_vasp_count": 42}
        scanner = cryptal_geo.CryptalGeorgianMarketScanner(
            mock.Mock(spec=maker.CryptalPublicDataHub), venue_scanner=companion
        )
        self.assertEqual(
            scanner.state()["georgian_venues"]["registered_vasp_count"], 42
        )


class TestDashboardIntegration(unittest.TestCase):
    def test_dashboard_starts_and_displays_multi_venue_scanner(self):
        from pathlib import Path

        source = Path("dashboard.py").read_text(encoding="utf-8")
        self.assertIn("GEORGIANVENUES.manage_loop()", source)
        self.assertIn("Other registered Georgian venues", source)
        self.assertIn("fixed-quote screen, no assumed fills", source)

    def test_scanner_contains_no_credentials_or_live_order_switch(self):
        from pathlib import Path

        source = Path("georgian_venue_scanner.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("api_key", source)
        self.assertNotIn("secret_key", source)
        self.assertNotIn("create_order", source)
        self.assertNotIn("place_order", source)
        state = venues.GeorgianVenueOpportunityScanner(
            mock.Mock(spec=maker.CryptalPublicDataHub)
        ).state()
        self.assertFalse(state["live_trading_enabled"])
        self.assertTrue(state["paper_only"])


if __name__ == "__main__":
    unittest.main()
