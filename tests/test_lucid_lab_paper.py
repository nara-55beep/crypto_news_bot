from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from lucid_lab.paper import (
    INITIAL_FLOOR,
    MARKETS,
    NY,
    STARTING_BALANCE,
    TARGET_BALANCE,
    LucidLabPaperBot,
    PaperSignal,
    _quote_epoch,
)


UTC = ZoneInfo("UTC")


def raw_rows(day: date, rows: int, *, first: float = 100.0, last: float | None = None) -> pd.DataFrame:
    start = pd.Timestamp(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)).tz_convert("UTC")
    closes = [first] * rows
    if last is not None and rows:
        step = (last - first) / max(1, rows - 1)
        closes = [first + step * index for index in range(rows)]
    return pd.DataFrame({
        "dt_utc": pd.date_range(start, periods=rows, freq="1min"),
        "open": [first] + closes[:-1],
        "high": [value + 0.25 for value in closes],
        "low": [value - 0.25 for value in closes],
        "close": closes,
        "volume": [100.0] * rows,
    })


def complete_prior(day: date, *, low: float = 90.0, high: float = 110.0, close: float = 100.0) -> pd.DataFrame:
    frame = raw_rows(day, 390, first=close)
    frame["low"] = low
    frame["high"] = high
    frame["close"] = close
    return frame


class TestLucidLabPaperSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = LucidLabPaperBot(Path(self.tmp.name) / "paper.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_opening_drive_uses_only_completed_opening_bars(self):
        prior = self.bot._normalise_frame(complete_prior(date(2026, 8, 13)))
        current = self.bot._normalise_frame(raw_rows(date(2026, 8, 14), 15, first=100, last=106))
        signal = self.bot._morning_signal("nq_opening_drive", current, prior)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "long")
        self.assertEqual(Decimal(signal.stop), Decimal("100.0"))

    def test_gap_fill_classification_is_directional_and_next_minute(self):
        prior = self.bot._normalise_frame(complete_prior(date(2026, 8, 13)))
        current = raw_rows(date(2026, 8, 14), 15, first=103, last=100.5)
        current["high"] = [103.5] * 15
        current["low"] = [100.0] * 15
        signal = self.bot._morning_signal("es_gap_fill", self.bot._normalise_frame(current), prior)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "short")
        self.assertEqual(Decimal(signal.stop), Decimal("103.75"))

    def test_prior_range_breakout_requires_a_completed_clock_aligned_block(self):
        prior = self.bot._normalise_frame(complete_prior(date(2026, 8, 13)))
        current = self.bot._normalise_frame(raw_rows(date(2026, 8, 14), 15, first=100, last=106))
        signal = self.bot._prior_breakout_signal(current, prior, 585)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "long")
        incomplete = current.drop(index=7).reset_index(drop=True)
        self.assertIsNone(self.bot._prior_breakout_signal(incomplete, prior, 585))

    def test_startup_primes_without_backfilling_a_historical_signal(self):
        day = date(2026, 8, 14)
        prior_day = date(2026, 8, 13)
        es = pd.concat([complete_prior(prior_day), raw_rows(day, 16, first=103, last=100.5)], ignore_index=True)
        nq = pd.concat([complete_prior(prior_day), raw_rows(day, 16, first=100, last=106)], ignore_index=True)
        now = datetime(2026, 8, 14, 9, 45, 5, tzinfo=NY).astimezone(UTC)
        self.bot.set_calendar({day.isoformat(): {"open_minute": 570, "close_minute": 960}})
        status = "TradingView websocket (realtime_streaming)"
        quote_at = now.timestamp()
        self.bot.ingest_update("es", es, {"bid": 100, "ask": 100.25, "at": quote_at}, status, now=now)
        self.bot.ingest_update("nq", nq, {"bid": 106, "ask": 106.25, "at": quote_at}, status, now=now)
        self.assertTrue(self.bot._primed)
        self.assertEqual(self.bot.positions, {})
        self.assertTrue(all("primed" in value for value in self.bot.sleeve_status.values()))


class TestLucidLabPaperRiskAndFills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.json"
        self.bot = LucidLabPaperBot(self.path)
        self.bot.feed_realtime = True
        self.bot.feed_error = ""
        self.bot.current_session = "2026-08-14"
        self.now = datetime(2026, 8, 14, 10, 0, tzinfo=NY).astimezone(UTC).timestamp()
        self.bot._quotes["nq"] = {"bid": 100.0, "ask": 100.25, "at": self.now}

    def tearDown(self):
        self.tmp.cleanup()

    def signal(self, sleeve: str = "nq_opening_drive", side: str = "long") -> PaperSignal:
        return PaperSignal(sleeve, "nq", side, "2026-08-14T13:59:00+00:00", "95", "2", "test")

    def test_shared_integer_cap_and_committed_stop_risk_are_enforced(self):
        current = pd.Timestamp(datetime.fromtimestamp(self.now, UTC))
        self.assertTrue(self.bot._open_signal(self.signal(), current, self.now))
        position = next(iter(self.bot.positions.values()))
        self.assertIsInstance(position.quantity, int)
        self.assertEqual(position.quantity, self.bot.rules.max_micros)
        self.assertGreater(Decimal(position.risk_reserved), Decimal("0"))
        self.assertEqual(self.bot.state()["open_micros"], self.bot.rules.max_micros)
        second = self.signal("nq_prior_breakout")
        self.assertFalse(self.bot._open_signal(second, current, self.now))
        self.assertEqual(len(self.bot.positions), 1)

    def test_quote_at_activation_cannot_trigger_a_fill_but_later_quote_can(self):
        current = pd.Timestamp(datetime.fromtimestamp(self.now, UTC))
        self.assertTrue(self.bot._open_signal(self.signal(), current, self.now))
        position = next(iter(self.bot.positions.values()))
        target = Decimal(position.target)
        equal = {"bid": float(target + 1), "ask": float(target + Decimal("1.25")), "at": self.now}
        self.bot._quotes["nq"] = equal
        self.bot._manage_quote("nq", equal)
        self.assertEqual(len(self.bot.positions), 1)
        later = dict(equal, at=self.now + 0.001)
        self.bot._quotes["nq"] = later
        self.bot._manage_quote("nq", later)
        self.assertEqual(self.bot.positions, {})
        self.assertEqual(len(self.bot.history), 1)
        self.assertEqual(self.bot.history[0]["reason"], "target")
        self.assertFalse(self.bot.history[0]["evidentiary"])
        self.assertIn("event filter", self.bot.history[0]["evidence_note"])

    def test_persistence_round_trip_and_reset_guard(self):
        current = pd.Timestamp(datetime.fromtimestamp(self.now, UTC))
        self.bot._open_signal(self.signal(), current, self.now)
        with self.assertRaises(RuntimeError):
            self.bot.reset()
        restored = LucidLabPaperBot(self.path)
        self.assertEqual(len(restored.positions), 1)
        self.assertEqual(restored.balance, STARTING_BALANCE)
        self.assertTrue(restored.state()["paper_only"])
        self.assertFalse(restored.state()["live_order_routing"])

    def test_pass_is_only_declared_at_session_end(self):
        self.bot.balance = TARGET_BALANCE
        self.bot.day_trades = 1
        self.bot._manage_quote("nq", {"bid": 100, "ask": 100.25, "at": self.now + 1})
        self.assertFalse(self.bot.passed)
        self.bot._end_session(date(2026, 8, 14))
        self.assertTrue(self.bot.passed)
        self.assertFalse(self.bot.enabled)

    def test_completed_account_cannot_be_reenabled_without_reset(self):
        self.bot.passed = True
        self.bot.enabled = False
        with self.assertRaises(ValueError):
            self.bot.set_enabled(True)

    def test_timestamp_units_normalise_to_the_same_exchange_second(self):
        expected = 1_786_303_013.054
        self.assertAlmostEqual(_quote_epoch(expected), expected)
        self.assertAlmostEqual(_quote_epoch(expected * 1_000), expected)
        self.assertAlmostEqual(_quote_epoch(expected * 1_000_000), expected)

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("not json", encoding="utf-8")
        bot = LucidLabPaperBot(self.path)
        self.assertFalse(bot.enabled)
        self.assertIn("failed closed", bot.persistence_error)
        self.assertEqual(bot.floor, INITIAL_FLOOR)


class TestLucidLabPaperPresentation(unittest.TestCase):
    def test_state_exposes_all_three_sleeves_and_honest_evidence_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = LucidLabPaperBot(Path(tmp) / "paper.json").state()
        self.assertEqual(len(state["sleeves"]), 3)
        self.assertEqual({row["instrument"] for row in state["sleeves"]}, {"MES", "MNQ"})
        self.assertEqual(state["forward_evidence"]["status"], "COLLECTING")
        self.assertIn("not proof", state["forward_evidence"]["note"])
        self.assertEqual(Decimal(state["starting_balance"]), Decimal("25000.00"))


if __name__ == "__main__":
    unittest.main()
