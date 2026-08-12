import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import config
import lucid_continuous_paper as continuous
import lucid_pass_paper as lucid


def bot_in(directory: str) -> continuous.LucidContinuousPaperBot:
    with mock.patch.object(config, "DATA_DIR", directory):
        bot = continuous.LucidContinuousPaperBot()
    bot._enforce_live_open_guard = False
    bot.enabled = True
    return bot


def row(close=100.0, open_=99.0, high=120.0, low=80.0, minute=0):
    stamp = pd.Timestamp("2026-08-11 13:00:00", tz="UTC") + pd.Timedelta(minutes=minute)
    return pd.Series({
        "dt_utc": stamp,
        "day": "2026-08-11",
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100.0,
    })


def signal(key="NQ_TURTLE30", side="long", stop=95.0, target=110.0):
    c = lucid.COMPONENTS[key]
    return {
        "key": key,
        "symbol": c["symbol"],
        "label": c["label"],
        "strat": c["name"],
        "side": side,
        "entry": 90.0,
        "stop": stop,
        "target": target,
        "target_mode": "2r",
        "target_rr": 2.0,
        "note": "test",
        "spent": False,
    }


class LucidContinuousRealismTests(unittest.TestCase):
    def test_only_development_selected_causal_components_are_active(self):
        self.assertEqual(
            {c["kind"] for c in lucid.COMPONENTS.values()},
            {"morning_drive", "morning_gap_fill", "prior_breakout"},
        )
        self.assertEqual({c["symbol"] for c in lucid.COMPONENTS.values()}, {"ES=F", "NQ=F"})
        self.assertEqual(lucid.MAX_MICROS, 40)
        self.assertEqual(lucid.COMMISSION_RT, 1.0)
        self.assertEqual(lucid.SLIP_TICKS, 1.0)

    def test_old_optimistic_state_cannot_enter_the_new_evidence_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lucid_continuous_state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "strategy_version": "lucid_5basket_r200_realtime_guard_v18",
                    "balance": 99_999.0,
                    "history": [{"pnl": 49_999.0}],
                }, handle)
            bot = bot_in(tmp)

        self.assertEqual(bot.balance, lucid.START_BALANCE)
        self.assertEqual(bot.history, [])

    def test_completed_bar_close_is_used_instead_of_intrabar_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        frame = pd.DataFrame([row(close=100.0, open_=98.0, high=120.0, low=80.0)])
        bot._df = {"NQ_TURTLE30": frame}
        with (
            mock.patch.object(bot, "_can_open_key", return_value=True),
            mock.patch.object(bot, "_signal", return_value=signal()),
        ):
            bot._tick()

        position = bot.pos["NQ_TURTLE30"]
        self.assertEqual(position.entry, 100.25)
        self.assertNotEqual(position.entry, 90.25)
        self.assertEqual(position.last_managed_bar, int(frame.iloc[0]["dt_utc"].timestamp()))
        self.assertEqual(bot.history, [])  # the already-completed signal bar cannot exit it

    def test_size_is_integer_capped_and_includes_commission_and_slippage(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot._open(signal(), row(close=100.0), raw_entry=100.0)

        position = bot.pos["NQ_TURTLE30"]
        self.assertIsInstance(position.qty, int)
        self.assertLessEqual(position.qty, lucid.MAX_MICROS)
        self.assertEqual(position.cost_usd, position.qty * lucid.COMMISSION_RT)
        self.assertLessEqual(position.risk_usd, lucid.RISK_USD)

    def test_gap_through_stop_fills_at_worse_open_plus_exit_slippage(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot._open(signal(), row(close=100.0), raw_entry=100.0)
        bot._manage("NQ_TURTLE30", row(close=91.0, open_=90.0, high=92.0, low=89.0, minute=15))

        self.assertNotIn("NQ_TURTLE30", bot.pos)
        self.assertEqual(bot.history[0]["exit"], 89.75)
        self.assertLess(bot.history[0]["pnl"], -lucid.RISK_USD)

    def test_aggregate_cap_and_drawdown_reservation_can_refuse_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot.floor = bot.balance - 5.0
        bot._open(signal(), row(close=100.0), raw_entry=100.0)

        self.assertEqual(bot.pos, {})

    def test_continuous_mode_keeps_the_daily_loss_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot.day_pnl = -lucid.DAILY_LOSS_LIMIT

        self.assertTrue(bot._uses_daily_loss_guard())
        self.assertFalse(bot._can_open_key("NQ_TURTLE30", row()))

    def test_eod_trail_moves_and_locks_only_when_the_session_rolls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot.balance = 52_200.0
        bot._book(50.0)
        self.assertEqual(bot.floor, 48_000.0)

        bot._roll_day("2026-08-12")

        self.assertTrue(bot.locked)
        self.assertEqual(bot.floor, 50_100.0)

    def test_causal_signal_definitions_match_the_preselected_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        prior = pd.DataFrame([{
            "open": 100.0, "high": 110.0, "low": 90.0,
            "close": 100.0, "range": 20.0,
        }])

        nq = pd.DataFrame([row(close=105.1, open_=100.0, high=106.0, low=99.0)])
        drive = bot._morning_signal("NQ_VWAP3", nq, prior, 0)
        self.assertEqual(drive["side"], "long")
        self.assertEqual(drive["target_rr"], 2.0)

        es = pd.DataFrame([row(close=101.0, open_=104.0, high=105.0, low=100.0)])
        gap_fill = bot._morning_signal("ES_VWAP3", es, prior, 0)
        self.assertEqual(gap_fill["side"], "short")
        self.assertEqual(gap_fill["target_rr"], 1.5)

        breakout = pd.DataFrame([row(close=106.0, open_=100.0, high=107.0, low=99.0)])
        prior_move = bot._prior_breakout_signal("NQ_TURTLE30", breakout, prior, 0)
        self.assertEqual(prior_move["side"], "long")
        self.assertEqual(prior_move["target_rr"], 2.0)

    def test_dashboard_does_not_repeat_the_invalid_profit_claim(self):
        source = Path(continuous.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PF 3.23", source)
        self.assertNotIn("losing months 0/37", source)


if __name__ == "__main__":
    unittest.main()
