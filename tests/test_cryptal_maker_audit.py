"""Independent audit of the Cryptal maker / Binance hedge paper strategy.

These tests are deliberately adversarial and production-shaped: they drive the real
`manage_loop` against a fake transport, reconcile realized P&L against independently
derived arithmetic rather than against the module's own helpers, and scan the shipped
source for any private-trading capability.
"""

import asyncio
import json
import math
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cryptal_maker_paper as maker


REPO = Path(__file__).resolve().parents[1]

_number = maker._number

F_MAKER = maker.CRYPTAL_MAKER_FEE_RATE
F_TAKER = maker.BINANCE_TAKER_FEE_RATE
SLIP = maker.HEDGE_SLIPPAGE_BPS / 1e4


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


def _bot(directory):
    return maker.CryptalMakerPaperBot(
        state_path=os.path.join(directory, "cryptal-maker.json"))


def _after(quote, seconds=1.0):
    """Exchange-time stamp strictly after `quote` became live.

    Both sides of the eligibility test are Cryptal timestamps and equality is
    rejected, so a print derived from the host clock can silently fail to fill when
    two ticks land inside the same millisecond. Anchoring to the quote's own
    activation watermark keeps every fill in these tests deterministic.
    """
    return int(round((_number(quote.get("activation_exchange_at")) + seconds) * 1000))


def _fill_quote(bot, snapshot_kwargs=None, trade_id="fill", side="ASK"):
    """Cross the bot's working quote with a fresh public print.

    The print is stamped relative to the quote's own exchange-time activation
    watermark, not the host clock. Deriving it from `time.time()` made this flaky:
    eligibility rejects `trade_at <= activation_at`, and two ticks can land inside
    the same millisecond on a warm run, so the fill silently vanished about a third
    of the time. Anchoring to the watermark keeps the ordering deterministic.
    """
    quote = dict(bot.quote)
    stamp_ms = int(round(_number(quote.get("activation_exchange_at")) * 1000)) + 1000
    trade = {"id": trade_id, "side": side, "price": quote["price"],
             "volume": quote["qty"], "timestamp": stamp_ms}
    bot._tick(_snapshot([trade], **(snapshot_kwargs or {})))
    return quote


# --------------------------------------------------------------------------
# 1. Bankroll starts and resets at exactly $100
# --------------------------------------------------------------------------
class TestBankrollNormalization(unittest.TestCase):
    RATES = [(0.90, 0.92), (0.98, 1.00), (0.999, 1.001), (1.00, 1.00), (1.04, 1.06)]

    def test_equity_opens_at_exactly_100_across_stable_rates(self):
        for stable_bid, stable_ask in self.RATES:
            with self.subTest(stable_bid=stable_bid), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                bot._tick(_snapshot([], stable_bid=stable_bid, stable_ask=stable_ask))
                state = bot.state()
                self.assertEqual(state["equity"], 100.0)
                self.assertEqual(state["balance"], 100.0)
                self.assertEqual(state["start_balance"], 100.0)
                self.assertEqual(state["total_pnl"], 0.0)

    def test_equity_reads_100_before_any_market_data_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _bot(tmp).state()
            self.assertEqual(state["equity"], 100.0)
            self.assertEqual(state["total_pnl"], 0.0)

    def test_reset_returns_to_exactly_100_across_stable_rates(self):
        for stable_bid, stable_ask in self.RATES:
            with self.subTest(stable_bid=stable_bid), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                bot._tick(_snapshot([], stable_bid=stable_bid, stable_ask=stable_ask))
                # Wreck the account, including open hedged inventory, then reset.
                bot.cryptal_cash_usd, bot.binance_balance_usdt = 3.0, 91.0
                bot.spot_qty = bot.short_qty = 0.004
                bot.spot_entry, bot.short_entry_usdt = 60_000.0, 61_000.0
                bot.history = [{"pnl": -12.0}]

                bot.reset()
                state = bot.state()

                self.assertEqual(state["equity"], 100.0)
                self.assertEqual(state["total_pnl"], 0.0)
                self.assertEqual(state["cryptal_cash_usd"], 50.0)
                self.assertEqual(state["binance_balance_usdt"], 50.0)
                self.assertEqual(state["inventory"]["spot_qty"], 0.0)
                self.assertEqual(state["inventory"]["short_qty"], 0.0)
                self.assertEqual(state["trades"], 0)

    def test_reset_after_a_realized_loss_does_not_inherit_old_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([], stable_bid=0.98, stable_ask=1.00))
            bot.cryptal_cash_usd -= 7.5           # realized damage
            self.assertLess(bot.state()["total_pnl"], 0)
            bot.reset()
            self.assertEqual(bot.state()["equity"], 100.0)
            self.assertEqual(bot.state()["total_pnl"], 0.0)


# --------------------------------------------------------------------------
# 2. $50 / $50 split with a $40 maximum quote
# --------------------------------------------------------------------------
class TestCapitalAllocation(unittest.TestCase):
    def test_declared_allocation_matches_the_100_dollar_plan(self):
        self.assertEqual(maker.PAPER_BANKROLL_USD, 100.0)
        self.assertEqual(maker.START_CRYPTAL_USD, 50.0)
        self.assertEqual(maker.START_BINANCE_USDT, 50.0)
        self.assertEqual(maker.ORDER_NOTIONAL_USD, 40.0)
        self.assertEqual(
            maker.START_CRYPTAL_USD + maker.START_BINANCE_USDT,
            maker.PAPER_BANKROLL_USD)

    def test_quote_notional_never_exceeds_40_across_a_price_sweep(self):
        for btc in (900.0, 25_000.0, 65_000.0, 140_000.0):
            with self.subTest(btc=btc), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                bot._tick(_snapshot(
                    [], bid=btc * 0.985, ask=btc * 1.015,
                    binance_bid=btc, binance_ask=btc * 1.00002))
                if bot.quote is None:
                    continue
                notional = bot.quote["price"] * bot.quote["qty"]
                self.assertLessEqual(notional, maker.ORDER_NOTIONAL_USD + 1e-6)
                self.assertLessEqual(notional, maker.START_CRYPTAL_USD)

    def test_held_inventory_never_exceeds_the_40_dollar_quote_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            for index in range(6):
                if bot.quote and bot.quote["side"] == "BID":
                    _fill_quote(bot, trade_id=f"top-up-{index}")
                else:
                    bot._tick(_snapshot([]))
                self.assertLessEqual(
                    bot.spot_qty * bot.spot_entry, maker.ORDER_NOTIONAL_USD + 1e-6)

    def test_a_bid_is_never_funded_beyond_the_cryptal_side_of_the_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot.cryptal_cash_usd = 6.0            # nearly spent Cryptal leg
            bot._tick(_snapshot([]))
            if bot.quote is not None:
                cost = bot.quote["price"] * bot.quote["qty"] * (1 + F_MAKER)
                self.assertLessEqual(cost, bot.cryptal_cash_usd + 1e-9)


# --------------------------------------------------------------------------
# 3. Legacy $200 state cannot be loaded
# --------------------------------------------------------------------------
class TestLegacyStateRejection(unittest.TestCase):
    def _write(self, tmp, payload):
        path = os.path.join(tmp, "cryptal-maker.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_unversioned_200_dollar_state_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {
                "cryptal_cash_usd": 100.0, "binance_balance_usdt": 100.0,
                "history": [{"pnl": 99.0}], "spot_qty": 0.01, "short_qty": 0.01,
            })
            bot = maker.CryptalMakerPaperBot(state_path=path)
            self.assertEqual(bot.cryptal_cash_usd, 50.0)
            self.assertEqual(bot.binance_balance_usdt, 50.0)
            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(bot.history, [])
            self.assertIn("legacy $200", bot.persistence_error)
            self.assertEqual(bot.state()["equity"], 100.0)

    def test_every_superseded_version_is_refused(self):
        for version in (0, 1, "1", None, "banana"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                path = self._write(tmp, {
                    "paper_account_version": version,
                    "cryptal_cash_usd": 100.0, "binance_balance_usdt": 100.0,
                })
                bot = maker.CryptalMakerPaperBot(state_path=path)
                self.assertEqual(bot.cryptal_cash_usd, 50.0)
                self.assertEqual(bot.binance_balance_usdt, 50.0)

    def test_corrupt_state_fails_closed_without_inventing_a_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-maker.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            bot = maker.CryptalMakerPaperBot(state_path=path)
            self.assertFalse(bot.enabled)
            self.assertIn("state load failed", bot.persistence_error)
            self.assertEqual(bot.cryptal_cash_usd, 50.0)
            self.assertEqual(bot.binance_balance_usdt, 50.0)

    def test_a_current_version_round_trip_preserves_the_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-maker.json")
            first = maker.CryptalMakerPaperBot(state_path=path)
            first._tick(_snapshot([]))
            _fill_quote(first, trade_id="persist")
            first._save()

            second = maker.CryptalMakerPaperBot(state_path=path)
            self.assertAlmostEqual(second.spot_qty, first.spot_qty, places=12)
            self.assertAlmostEqual(second.short_qty, first.short_qty, places=12)
            self.assertAlmostEqual(
                second.cryptal_cash_usd, first.cryptal_cash_usd, places=9)
            self.assertEqual(second.persistence_error, "")


# --------------------------------------------------------------------------
# 4. It polls fresh public data every 2 seconds
# --------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records every GET so cadence and endpoint freshness can be asserted."""

    def __init__(self, responder):
        self._responder = responder
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, dict(params or {}), time.time()))
        return _FakeResponse(*self._responder(url, params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StopLoop(Exception):
    pass


class TestPollingLoop(unittest.TestCase):
    def _responder(self, fail_on_poll=None):
        state = {"polls": 0}

        def respond(url, params):
            now = int(time.time() * 1000)
            if "orderbook/BTC-USD" in url:
                state["polls"] += 1
                if fail_on_poll and state["polls"] == fail_on_poll:
                    return {"error": "boom"}, 503
                return {"timestamp": now,
                        "bids": [{"price": "64000", "volume": "0.01"}],
                        "asks": [{"price": "66000", "volume": "0.01"}]}, 200
            if "orderbook/USDT-USD" in url:
                return {"timestamp": now,
                        "bids": [{"price": "0.999", "volume": "100"}],
                        "asks": [{"price": "1.001", "volume": "100"}]}, 200
            if "trades/" in url:
                return [], 200
            return {"bidPrice": "65000", "askPrice": "65001", "time": now}, 200

        return respond, state

    def _run_loop(self, bot, responder, stop_after=4):
        session = _FakeSession(responder)
        delays = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) >= stop_after:
                raise _StopLoop
            await real_sleep(0)

        fake_aiohttp = mock.MagicMock()
        fake_aiohttp.ClientTimeout = mock.MagicMock()
        fake_aiohttp.TCPConnector = mock.MagicMock()
        fake_aiohttp.ThreadedResolver = mock.MagicMock()
        fake_aiohttp.ClientSession = mock.MagicMock(return_value=session)

        async def drive():
            with mock.patch.object(maker, "aiohttp", fake_aiohttp), \
                    mock.patch.object(asyncio, "sleep", fake_sleep):
                try:
                    await bot.manage_loop()
                except _StopLoop:
                    pass

        asyncio.run(drive())
        return session, delays

    def test_poll_interval_is_two_seconds(self):
        self.assertEqual(maker.POLL_SEC, 2.0)

    def test_loop_requests_all_four_public_feeds_on_every_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            responder, _ = self._responder()
            session, delays = self._run_loop(bot, responder, stop_after=4)

            polls = len(delays)
            self.assertEqual(len(session.requests), polls * 4)
            for index in range(polls):
                urls = [row[0] for row in session.requests[index * 4:index * 4 + 4]]
                self.assertTrue(any("orderbook/BTC-USD" in u for u in urls))
                self.assertTrue(any("orderbook/USDT-USD" in u for u in urls))
                self.assertTrue(any("trades/BTC-USD" in u for u in urls))
                self.assertTrue(any("fapi.binance.com" in u for u in urls))
            self.assertEqual(bot.poll_count, polls)

    def test_each_poll_sleeps_the_remainder_of_the_two_second_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            responder, _ = self._responder()
            _, delays = self._run_loop(bot, responder, stop_after=4)
            for delay in delays:
                self.assertGreater(delay, 1.5)
                self.assertLessEqual(delay, maker.POLL_SEC)

    def test_a_failed_poll_cancels_the_quote_without_killing_the_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            responder, _ = self._responder(fail_on_poll=2)
            session, delays = self._run_loop(bot, responder, stop_after=4)

            # The loop survived the 503 and kept polling on cadence.
            self.assertEqual(len(delays), 4)
            self.assertEqual(len(session.requests), 4 * 4)
            self.assertGreater(bot.poll_count, 1)

    def test_live_snapshot_is_rejected_when_the_venue_clock_drifts(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            self.assertIsNotNone(bot.quote)
            bot._tick(_snapshot([], stamp=time.time() + 5))   # future-dated feed
            self.assertIsNone(bot.quote)
            self.assertIn("stale", bot.data_error)


# --------------------------------------------------------------------------
# 5. A bid cannot fill from backlog, an old print, or an unconsumed queue
# --------------------------------------------------------------------------
class TestFillDiscipline(unittest.TestCase):
    def test_startup_backlog_is_absorbed_and_can_never_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            backlog = [
                {"id": str(i), "side": "ASK", "price": "60000", "volume": "5",
                 "timestamp": int(time.time() * 1000)}
                for i in range(100)
            ]
            bot._tick(_snapshot(backlog))
            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(bot.fill_count, 0)
            # Replaying the identical backlog still cannot fill.
            bot._tick(_snapshot(backlog))
            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.fill_count, 0)

    def test_a_print_older_than_the_quote_cannot_fill_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            for age in (0.6, 5.0, 600.0):
                with self.subTest(age=age):
                    stale = {"id": f"old-{age}", "side": "ASK",
                             "price": quote["price"], "volume": quote["qty"],
                             "timestamp": int((quote["placed_at"] - age) * 1000)}
                    bot._tick(_snapshot([stale]))
                    self.assertEqual(bot.spot_qty, 0.0)
                    self.assertEqual(bot.short_qty, 0.0)

    def test_visible_queue_must_be_consumed_before_any_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            levels = [(64_800, 0.10), (64_500, 0.20)]
            snapshot = _snapshot([], bid=64_800, ask=66_000, bid_levels=levels)
            bot._tick(snapshot)
            queue0 = bot.quote["queue_ahead_btc"]
            self.assertGreater(queue0, 0.0)

            # Dribble the queue away in pieces; none of them may fill.
            consumed = 0.0
            for index in range(5):
                trade = {"id": f"q{index}", "side": "ASK",
                         "price": bot.quote["price"], "volume": "0.015",
                         "timestamp": _after(bot.quote)}
                bot._tick({**snapshot, "trades": [trade],
                           "received_at": time.time(), "cryptal_at": time.time(),
                           "stable_at": time.time(), "binance_at": time.time()})
                consumed += 0.015
                if consumed < queue0:
                    self.assertEqual(
                        bot.spot_qty, 0.0,
                        f"filled after only {consumed} of {queue0} BTC queue")

    def test_activation_watermark_is_recorded_in_exchange_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            stamp = time.time() + 0.36
            bot._tick(_snapshot([], stamp=stamp))
            self.assertAlmostEqual(
                bot.quote["activation_exchange_at"], stamp, places=6)
            # It tracks the venue clock, not the host clock.
            self.assertNotAlmostEqual(
                bot.quote["activation_exchange_at"], bot.quote["placed_at"], places=2)

    def test_a_print_with_no_timestamp_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            for stamp in (None, 0, ""):
                with self.subTest(stamp=stamp):
                    trade = {"id": f"nots-{stamp}", "side": "ASK",
                             "price": quote["price"], "volume": quote["qty"],
                             "timestamp": stamp}
                    bot._tick(_snapshot([trade]))
                    self.assertEqual(bot.spot_qty, 0.0)

    def test_a_resumed_quote_cannot_fill_until_it_is_reactivated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-maker.json")
            first = maker.CryptalMakerPaperBot(state_path=path)
            first._tick(_snapshot([]))
            quote = dict(first.quote)
            # Simulate state written before the watermark existed.
            first.quote.pop("activation_exchange_at")
            first._save()

            resumed = maker.CryptalMakerPaperBot(state_path=path)
            self.assertIsNone(resumed.quote.get("activation_exchange_at"))
            stale = {"id": "resume-stale", "side": "ASK", "price": quote["price"],
                     "volume": quote["qty"],
                     "timestamp": int((time.time() - 30) * 1000)}
            resumed._tick(_snapshot([stale]))

            self.assertEqual(resumed.spot_qty, 0.0)
            # The tick reactivates it so the collector keeps working afterwards.
            self.assertGreater(
                maker._number(resumed.quote.get("activation_exchange_at")), 0.0)

    def test_a_price_that_does_not_reach_the_bid_cannot_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            above = {"id": "above", "side": "ASK",
                     "price": quote["price"] + 1.0, "volume": quote["qty"],
                     "timestamp": _after(quote)}
            bot._tick(_snapshot([above]))
            self.assertEqual(bot.spot_qty, 0.0)


# --------------------------------------------------------------------------
# 5b. Fill eligibility must not depend on the host clock
# --------------------------------------------------------------------------
class TestClockSkewCannotManufactureFills(unittest.TestCase):
    """Regression for a phantom-fill window under venue/host clock skew.

    The eligibility test once compared a Cryptal trade timestamp against a local
    `placed_at` with a 0.5s tolerance. With Cryptal reading d seconds ahead of the
    host, a print that truly occurred up to (0.5 + d) seconds *before* placement
    still passed and booked a fill for an order that did not exist yet. At the
    +0.36s skew measured against the live venue that window was 0.86s.
    """

    # Every skew here survives the +/-2s..12s staleness gate, so a quote is placed.
    SKEWS = (0.0, 0.36, 1.0, 1.9, -1.0, -5.0)
    LEADS = (0.1, 0.3, 0.6, 0.8, 1.0, 1.5)

    def _place(self, bot, skew, **kwargs):
        bot._tick(_snapshot([], stamp=time.time() + skew, **kwargs))
        return dict(bot.quote)

    def _cross(self, bot, quote, skew, offset, volume=None, **kwargs):
        """Print stamped `offset` seconds from placement, on the Cryptal clock."""
        exchange_at = (quote["placed_at"] + offset) + skew
        trade = {"id": f"x{offset}", "side": "ASK", "price": quote["price"],
                 "volume": volume if volume is not None else quote["qty"],
                 "timestamp": int(exchange_at * 1000)}
        bot._tick(_snapshot([trade], stamp=time.time() + skew, **kwargs))

    def test_pre_placement_print_never_fills_at_any_skew(self):
        for skew in self.SKEWS:
            for lead in self.LEADS:
                with self.subTest(skew=skew, lead=lead), \
                        tempfile.TemporaryDirectory() as tmp:
                    bot = _bot(tmp)
                    quote = self._place(bot, skew)
                    self._cross(bot, quote, skew, -lead)
                    self.assertEqual(
                        bot.spot_qty, 0.0,
                        f"print {lead}s before placement filled at skew {skew}")
                    self.assertEqual(bot.short_qty, 0.0)

    def test_pre_reprice_print_never_fills_the_replacement_order(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                self._place(bot, skew, bid=64_000.0)
                second = self._place(bot, skew, bid=64_400.0)   # >8bps, so repriced
                self.assertGreater(second["price"], 64_000.0)
                # Enough volume to clear the queue, so only timing can block it.
                self._cross(bot, second, skew, -0.6,
                            volume=second["queue_ahead_btc"] + second["qty"],
                            bid=64_400.0)
                self.assertEqual(bot.spot_qty, 0.0)

    def test_genuine_post_placement_print_still_fills_at_any_skew(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                quote = self._place(bot, skew)
                self._cross(bot, quote, skew, +0.4)
                self.assertGreater(
                    bot.spot_qty, 0.0, f"genuine fill lost at skew {skew}")
                self.assertEqual(bot.spot_qty, bot.short_qty)

    def test_fill_outcome_is_invariant_to_the_host_clock(self):
        """Identical exchange-side data must yield identical fills at every skew."""
        for offset in (-0.6, +0.4):
            outcomes = []
            for skew in self.SKEWS:
                with tempfile.TemporaryDirectory() as tmp:
                    bot = _bot(tmp)
                    quote = self._place(bot, skew)
                    self._cross(bot, quote, skew, offset)
                    outcomes.append(bot.spot_qty > 0)
            self.assertEqual(len(set(outcomes)), 1,
                             f"offset {offset} gave skew-dependent fills: {outcomes}")
            self.assertEqual(outcomes[0], offset > 0)


# --------------------------------------------------------------------------
# 5c. The activation watermark boundary itself must be conservative
# --------------------------------------------------------------------------
def _exact_ms(value: float) -> float:
    """Truncate to a whole millisecond, the resolution Cryptal actually serves."""
    return math.floor(value * 1000) / 1000.0


class TestActivationWatermarkBoundary(unittest.TestCase):
    """`activation_exchange_at` is read from the snapshot that precedes the virtual
    quote, so a print stamped at exactly that millisecond is not proven to have
    occurred after the order existed. Cryptal stamps to the millisecond, so the
    boundary is reachable in production and equality must fail closed."""

    SKEWS = (0.0, 0.36, 1.0, 1.9, -1.0, -5.0)

    def _place(self, bot, skew, **kwargs):
        bot._tick(_snapshot([], stamp=_exact_ms(time.time() + skew), **kwargs))
        return dict(bot.quote)

    def _cross_at(self, bot, quote, skew, offset_ms, volume=None, **kwargs):
        """Print stamped offset_ms milliseconds from the quote's activation point."""
        stamp_ms = int(round(quote["activation_exchange_at"] * 1000)) + offset_ms
        trade = {"id": f"boundary{offset_ms}", "side": "ASK",
                 "price": quote["price"],
                 "volume": volume if volume is not None else quote["qty"],
                 "timestamp": stamp_ms}
        bot._tick(_snapshot([trade], stamp=_exact_ms(time.time() + skew), **kwargs))

    def test_exact_watermark_print_never_fills_a_new_quote(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                quote = self._place(bot, skew)
                self.assertEqual(quote["queue_ahead_btc"], 0.0)
                self._cross_at(bot, quote, skew, 0)
                self.assertEqual(bot.spot_qty, 0.0)
                self.assertEqual(bot.short_qty, 0.0)
                self.assertEqual(bot.fill_count, 0)

    def test_exact_watermark_print_never_fills_a_repriced_quote(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                self._place(bot, skew, bid=64_000.0)
                second = self._place(bot, skew, bid=64_400.0)
                self.assertGreater(second["price"], 64_000.0)
                self._cross_at(bot, second, skew, 0,
                               volume=second["queue_ahead_btc"] + second["qty"],
                               bid=64_400.0)
                self.assertEqual(bot.spot_qty, 0.0)
                self.assertEqual(bot.short_qty, 0.0)

    def test_one_millisecond_past_the_watermark_still_fills(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                quote = self._place(bot, skew)
                self._cross_at(bot, quote, skew, 1)
                self.assertGreater(
                    bot.spot_qty, 0.0, f"genuine +1ms fill lost at skew {skew}")
                self.assertEqual(bot.spot_qty, bot.short_qty)

    def test_one_millisecond_past_the_watermark_fills_after_a_reprice(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                self._place(bot, skew, bid=64_000.0)
                second = self._place(bot, skew, bid=64_400.0)
                self._cross_at(bot, second, skew, 1,
                               volume=second["queue_ahead_btc"] + second["qty"],
                               bid=64_400.0)
                self.assertGreater(bot.spot_qty, 0.0)
                self.assertEqual(bot.spot_qty, bot.short_qty)

    def test_one_millisecond_before_the_watermark_never_fills(self):
        for skew in self.SKEWS:
            with self.subTest(skew=skew), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                quote = self._place(bot, skew)
                self._cross_at(bot, quote, skew, -1)
                self.assertEqual(bot.spot_qty, 0.0)

    def test_boundary_outcome_is_invariant_to_the_host_clock(self):
        for offset_ms, should_fill in ((-1, False), (0, False), (1, True)):
            outcomes = []
            for skew in self.SKEWS:
                with tempfile.TemporaryDirectory() as tmp:
                    bot = _bot(tmp)
                    quote = self._place(bot, skew)
                    self._cross_at(bot, quote, skew, offset_ms)
                    outcomes.append(bot.spot_qty > 0)
            self.assertEqual(
                len(set(outcomes)), 1,
                f"offset {offset_ms}ms gave skew-dependent fills: {outcomes}")
            self.assertEqual(outcomes[0], should_fill)


# --------------------------------------------------------------------------
# 6. A valid bid fill immediately creates an equal Binance short
# --------------------------------------------------------------------------
class TestHedgeImmediacy(unittest.TestCase):
    def test_fill_creates_an_exactly_equal_short_in_the_same_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            quote = _fill_quote(bot, trade_id="hedge-1")

            self.assertGreater(bot.spot_qty, 0.0)
            self.assertEqual(bot.spot_qty, bot.short_qty)
            self.assertEqual(bot.state()["inventory"]["delta_btc"], 0.0)
            # Hedge sold into the Binance bid, slipped adversely.
            self.assertAlmostEqual(
                bot.short_entry_usdt, 65_000.0 * (1 - SLIP), places=6)
            self.assertEqual(bot.fill_count, 1)
            self.assertEqual(bot.quote["side"], "ASK")
            self.assertEqual(quote["side"], "BID")

    def test_partial_fill_still_leaves_a_flat_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            quote = dict(bot.quote)
            half = {"id": "half", "side": "ASK", "price": quote["price"],
                    "volume": quote["qty"] / 2.0,
                    "timestamp": _after(quote)}
            bot._tick(_snapshot([half]))
            self.assertGreater(bot.spot_qty, 0.0)
            self.assertEqual(bot.spot_qty, bot.short_qty)

    def test_delta_stays_flat_through_a_full_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            _fill_quote(bot, trade_id="cyc-open")
            self.assertEqual(bot.spot_qty, bot.short_qty)
            _fill_quote(bot, trade_id="cyc-close", side="BID")
            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)


# --------------------------------------------------------------------------
# 7. Fee, slippage and forced-exit accounting
# --------------------------------------------------------------------------
class TestCycleAccounting(unittest.TestCase):
    def test_realized_pnl_matches_independently_derived_arithmetic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            market = dict(stable_bid=0.999, stable_ask=1.001,
                          binance_bid=65_000.0, binance_ask=65_001.0)
            bot._tick(_snapshot([], **market))
            buy = _fill_quote(bot, market, trade_id="acct-open")
            sell = _fill_quote(bot, market, trade_id="acct-close", side="BID")

            qty = buy["qty"]
            hedge_open = 65_000.0 * (1 - SLIP)      # sell the bid, slipped down
            hedge_close = 65_001.0 * (1 + SLIP)     # buy the ask, slipped up

            spot_leg = qty * (sell["price"] * (1 - F_MAKER)
                              - buy["price"] * (1 + F_MAKER))
            hedge_usdt = qty * (hedge_open - hedge_close
                                - F_TAKER * (hedge_open + hedge_close))
            # Realized equity marks the whole USDT leg at the TOUSD bid, so the
            # hedge delta converts at the same rate the balance is marked at.
            expected = spot_leg + hedge_usdt * 0.999

            self.assertEqual(len(bot.history), 1)
            self.assertAlmostEqual(bot.history[0]["pnl"], expected, places=6)

    def test_projection_never_flatters_the_realized_result(self):
        """_projected_cycle_pnl converts a hedge loss at the TOUSD ask while the
        realized mark uses the bid. The gap is sub-basis-point and must stay
        conservative: the quote may never promise more than it books."""
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            buy = _fill_quote(bot, trade_id="proj-open")
            promised = bot.quote["edge_metric_bps"]
            sell = _fill_quote(bot, trade_id="proj-close", side="BID")

            notional = buy["qty"] * float(buy["price"])
            realized_bps = bot.history[0]["pnl"] / notional * 1e4
            self.assertGreaterEqual(realized_bps, promised - 1e-6)
            self.assertLess(realized_bps - promised, 1.0)   # gap under 1 bp
            self.assertGreater(sell["price"], buy["price"])

    def test_binding_exit_target_is_actually_delivered(self):
        """When the book is tight enough that the computed floor price binds, the
        realized cycle must still clear MIN_PROJECTED_NET_BPS plus the reserve."""
        target_bps = (maker.MIN_PROJECTED_NET_BPS
                      + maker.FUNDING_AND_BASIS_RESERVE_BPS)
        for book_ask in (64_100.0, 64_300.0, 64_600.0):
            with self.subTest(book_ask=book_ask), tempfile.TemporaryDirectory() as tmp:
                bot = _bot(tmp)
                bot._tick(_snapshot([]))
                buy = _fill_quote(bot, trade_id="bind-open")
                tight = _snapshot([], bid=63_990.0, ask=book_ask)
                bot._tick(tight)
                self.assertEqual(bot.quote["side"], "ASK")
                ask = dict(bot.quote)
                # The floor sits above the visible book, so real queue is ahead.
                self.assertGreater(ask["price"], book_ask)
                self.assertGreater(ask["queue_ahead_btc"], 0.0)

                trade = {"id": "bind-close", "side": "BID", "price": ask["price"],
                         "volume": ask["queue_ahead_btc"] + ask["qty"],
                         "timestamp": _after(ask)}
                bot._tick({**tight, "trades": [trade], "received_at": time.time(),
                           "cryptal_at": time.time(), "stable_at": time.time(),
                           "binance_at": time.time()})

                notional = buy["qty"] * float(buy["price"])
                realized_bps = bot.history[0]["pnl"] / notional * 1e4
                self.assertGreaterEqual(realized_bps, target_bps - 0.5)

    def test_total_pnl_equals_the_sum_of_booked_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            stables = dict(stable_bid=0.999, stable_ask=1.001)
            bot._tick(_snapshot([], **stables))
            for index in range(24):
                if bot.quote is None:
                    bot._tick(_snapshot([], **stables))
                    continue
                quote = dict(bot.quote)
                crossing = "ASK" if quote["side"] == "BID" else "BID"
                trade = {"id": f"conserve-{index}", "side": crossing,
                         "price": quote["price"], "volume": quote["qty"],
                         "timestamp": _after(quote)}
                bot._tick(_snapshot([trade], **stables))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertGreater(len(bot.history), 5)
            booked = sum(row["pnl"] for row in bot.history)
            self.assertAlmostEqual(bot.state()["total_pnl"], booked, places=3)

    def test_restart_with_open_inventory_preserves_the_hedged_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-maker.json")
            first = maker.CryptalMakerPaperBot(state_path=path)
            first._tick(_snapshot([]))
            _fill_quote(first, trade_id="restart-open")
            self.assertGreater(first.spot_qty, 0.0)

            resumed = maker.CryptalMakerPaperBot(state_path=path)
            self.assertEqual(resumed.spot_qty, resumed.short_qty)
            self.assertTrue(resumed._absorbed_backlog)
            resumed._tick(_snapshot([]))
            # Resumed equity must not jump; it is still the same hedged cycle.
            self.assertAlmostEqual(
                resumed.state()["equity"], first.state()["equity"], places=2)

    def test_all_four_fee_legs_are_charged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            buy = _fill_quote(bot, trade_id="fee-open")
            sell = _fill_quote(bot, trade_id="fee-close", side="BID")

            qty = buy["qty"]
            gross = qty * (sell["price"] - buy["price"])
            realized = bot.history[0]["pnl"]
            spot_fees = qty * F_MAKER * (buy["price"] + sell["price"])
            hedge_fees = qty * F_TAKER * (65_000.0 + 65_001.0)

            self.assertGreater(gross, 0.0)
            self.assertLess(realized, gross)
            # Every modelled cost must be visible in the shortfall.
            self.assertGreaterEqual(gross - realized, spot_fees + hedge_fees * 0.9)

    def test_a_flat_cycle_returns_the_account_to_two_cash_legs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            _fill_quote(bot, trade_id="flat-open")
            _fill_quote(bot, trade_id="flat-close", side="BID")

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            self.assertEqual(bot.spot_entry, 0.0)
            self.assertEqual(bot.short_entry_usdt, 0.0)
            self.assertEqual(bot.spot_cost_usd, 0.0)
            reported = bot.state()["total_pnl"]
            self.assertAlmostEqual(reported, round(bot.history[0]["pnl"], 4), places=3)

    def test_forced_exit_crosses_the_real_bid_and_books_the_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            buy = _fill_quote(bot, trade_id="stop-open")
            bot._cycle_opened_at = time.time() - maker.MAX_HEDGED_HOLD_SEC - 1

            bot._tick(_snapshot([], bid=63_000, ask=66_000))

            self.assertEqual(bot.spot_qty, 0.0)
            self.assertEqual(bot.short_qty, 0.0)
            record = bot.history[0]
            self.assertEqual(record["reason"], "24h_time_stop_crossed_spread")
            self.assertEqual(record["sell"], 63_000.0)   # the executable bid
            self.assertLess(record["pnl"], 0.0)
            self.assertLess(record["sell"], buy["price"])

    def test_inventory_is_not_force_exited_before_the_hold_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            _fill_quote(bot, trade_id="hold-open")
            bot._cycle_opened_at = time.time() - maker.MAX_HEDGED_HOLD_SEC + 60

            bot._tick(_snapshot([], bid=63_000, ask=66_000))

            self.assertGreater(bot.spot_qty, 0.0)
            self.assertEqual(bot.history, [])

    def test_hedge_absorbs_a_price_crash_while_inventory_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot._tick(_snapshot([]))
            _fill_quote(bot, trade_id="crash-open")
            equity_before = bot.state()["equity"]

            crashed = dict(bid=57_600.0, ask=59_400.0,
                           binance_bid=58_500.0, binance_ask=58_501.0)
            bot._tick(_snapshot([], **crashed))
            equity_after = bot.state()["equity"]

            # A 10% BTC move must not move a delta-hedged book by anything close to it.
            move = abs(equity_after - equity_before)
            self.assertLess(move, 0.10 * maker.ORDER_NOTIONAL_USD * 0.25)


# --------------------------------------------------------------------------
# 8. No private endpoints, credentials, or live-trading switch
# --------------------------------------------------------------------------
class TestNoLiveTradingCapability(unittest.TestCase):
    SOURCE = (REPO / "cryptal_maker_paper.py").read_text(encoding="utf-8")

    @classmethod
    def setUpClass(cls):
        # Audit executable code, not prose: docstrings legitimately discuss the
        # absence of private endpoints, which a raw substring scan would flag.
        import io
        import tokenize
        names = []
        for token in tokenize.generate_tokens(io.StringIO(cls.SOURCE).readline):
            if token.type in (tokenize.NAME, tokenize.OP):
                names.append(token.string)
        cls.CODE = " ".join(names)
        cls.STRINGS = [
            token.string for token in
            tokenize.generate_tokens(io.StringIO(cls.SOURCE).readline)
            if token.type == tokenize.STRING
        ]

    def test_module_only_ever_issues_public_gets(self):
        for verb in ("post", "put", "delete", "patch", "request"):
            self.assertNotIn(f"session . {verb}", self.CODE,
                             f"session.{verb} found in paper module")
        self.assertEqual(self.CODE.count("session . get"), 1)

    def test_no_credential_or_signing_machinery_exists(self):
        banned = ("api_key", "apikey", "api_secret", "secret", "token", "hmac",
                  "hashlib", "signature", "sign", "authorization", "bearer",
                  "headers", "auth", "listenkey", "getenv", "environ")
        lowered = self.CODE.lower()
        for name in banned:
            self.assertNotIn(name, lowered, f"{name} found in paper module code")

    def test_no_order_placement_path_appears_in_any_string_literal(self):
        banned = ("/order", "/openorders", "/allorders", "/userdatastream",
                  "/account", "/withdraw", "/transfer", "x-mbx-apikey")
        for literal in self.STRINGS:
            lowered = literal.lower()
            for fragment in banned:
                self.assertNotIn(fragment, lowered,
                                 f"{fragment} found in literal {literal[:60]}")

    def test_every_url_is_a_public_market_data_endpoint(self):
        urls = re.findall(r"https?://[^\s\"']+", self.SOURCE)
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(
                url.startswith("https://exchange.cryptal.com/exchange")
                or url.startswith("https://fapi.binance.com/fapi/v1/ticker"),
                f"unexpected endpoint {url}")
        # Every path actually requested must sit under a public namespace.
        for template in re.findall(r'f"\{CRYPTAL_BASE\}([^"]+)"', self.SOURCE):
            self.assertTrue(template.startswith("/api/v1/public/"),
                            f"non-public Cryptal path {template}")

    def test_live_trading_flag_is_false_and_can_never_be_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            self.assertFalse(bot.live_trading_enabled)
            self.assertFalse(bot.state()["live_trading_enabled"])
            self.assertEqual(bot.state()["mode"], "PAPER_ONLY")
            # set_enabled only gates paper quoting; it must not arm anything live.
            bot.set_enabled(True)
            self.assertFalse(bot.live_trading_enabled)
            bot.reset()
            self.assertFalse(bot.live_trading_enabled)
        # The flag is assigned exactly once, to False, in __init__.
        assignments = re.findall(r"live_trading_enabled\s*=\s*(\w+)", self.SOURCE)
        self.assertEqual(assignments, ["False"])

    def test_persisted_state_cannot_re_enable_live_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cryptal-maker.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "paper_account_version": maker.PAPER_ACCOUNT_VERSION,
                    "live_trading_enabled": True, "mode": "LIVE",
                }, handle)
            bot = maker.CryptalMakerPaperBot(state_path=path)
            self.assertFalse(bot.live_trading_enabled)
            self.assertEqual(bot.state()["mode"], "PAPER_ONLY")

    def test_results_are_never_marked_evidentiary(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = _bot(tmp)
            bot.history = [{"pnl": 5.0}] * (maker.MIN_PAPER_CYCLES * 10)
            validation = bot.state()["validation"]
            self.assertFalse(validation["evidentiary"])
            self.assertEqual(validation["status"], "PAPER_RESULT_ONLY")


if __name__ == "__main__":
    unittest.main()
