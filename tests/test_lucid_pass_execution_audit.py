import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import config
import lucid_continuous_paper as continuous
import lucid_pass_audited_paper as audited
import lucid_pass_paper as original


def bar(*, close=100.0, open_=99.0, high=120.0, low=80.0, minute=0):
    stamp = pd.Timestamp("2026-08-11 13:00:00", tz="UTC") + pd.Timedelta(minutes=minute)
    return pd.Series({
        "dt_utc": stamp,
        "dt_ny": stamp.tz_convert("America/New_York"),
        "day": stamp.tz_convert("America/New_York").date(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100.0,
    })


def signal(key="NQ_TURTLE30", side="long", stop=95.0, target=110.0):
    component = original.COMPONENTS[key]
    return {
        "key": key,
        "symbol": component["symbol"],
        "label": component["label"],
        "strat": component["name"],
        "side": side,
        "entry": 90.0,
        "stop": stop,
        "target": target,
        "note": "production-shaped test signal",
        "spent": False,
    }


def bot_in(directory: str):
    with mock.patch.object(config, "DATA_DIR", directory):
        bot = audited.AuditedLucidPassPaperBot()
    bot.enabled = True
    bot._enforce_live_open_guard = False
    return bot


class LucidPassExecutionAuditTests(unittest.TestCase):
    def test_signal_strategy_is_inherited_unchanged(self):
        cls = audited.AuditedLucidPassPaperBot
        self.assertIs(cls._signal, original.LucidPassPaperBot._signal)
        self.assertIs(cls._vwap_signal, original.LucidPassPaperBot._vwap_signal)
        self.assertIs(cls._turtle_signal, original.LucidPassPaperBot._turtle_signal)
        self.assertIs(cls._nr7_signal, original.LucidPassPaperBot._nr7_signal)
        self.assertEqual(
            {component["name"] for component in original.COMPONENTS.values()},
            {
                "ES 3m VWAP Fade 2.5s",
                "NQ 3m VWAP Fade 2.5s",
                "CL 5m VWAP Fade 2.5s",
                "NQ 30m Turtle Soup 10",
                "CL 30m NR7 Breakout",
            },
        )

    def test_continuous_bot_stays_on_original_execution(self):
        self.assertIs(continuous.LucidContinuousPaperBot._open, original.LucidPassPaperBot._open)
        self.assertFalse(continuous.LucidContinuousPaperBot()._uses_daily_loss_guard())
        self.assertIsNone(original.MAX_MICROS)
        self.assertEqual(original.SLIP_TICKS, 0.0)
        self.assertEqual(original.COMMISSION_RT, 0.50)
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("lucidcontbot = lucid_continuous_paper.LucidContinuousPaperBot()", source)
        self.assertIn("lucidpassbot = lucid_pass_audited_paper.AuditedLucidPassPaperBot()", source)

    def test_old_v18_pnl_does_not_enter_audited_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lucid_pass_state.json")
            Path(path).write_text(json.dumps({
                "strategy_version": original.STRATEGY_VERSION,
                "strategy_fingerprint": original.STRATEGY_FINGERPRINT,
                "balance": 52_489.83,
                "history": [{"pnl": 2_489.83}],
                "telegram_enabled": False,
            }), encoding="utf-8")
            bot = bot_in(tmp)
        self.assertEqual(bot.balance, original.START_BALANCE)
        self.assertEqual(bot.history, [])
        self.assertFalse(bot.telegram_enabled)

    def test_audited_ledger_survives_a_real_save_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "DATA_DIR", tmp):
                first = audited.AuditedLucidPassPaperBot()
                first.balance = 50_123.45
                first.peak = 50_200.0
                first.day_key = "2026-08-11"
                first.day_pnl = 123.45
                first.history = [{"pnl": 123.45, "reason": "target"}]
                first._save()
                second = audited.AuditedLucidPassPaperBot()

        self.assertEqual(second.balance, 50_123.45)
        self.assertEqual(second.peak, 50_200.0)
        self.assertEqual(second.day_key, "2026-08-11")
        self.assertEqual(second.day_pnl, 123.45)
        self.assertEqual(second.history, [{"pnl": 123.45, "reason": "target"}])
        self.assertEqual(second.state()["strategy_version"], audited.AUDIT_VERSION)

    def test_tick_enters_at_completed_close_and_never_reuses_signal_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        frame = pd.DataFrame([bar(close=100.0, open_=98.0, high=120.0, low=80.0)])
        bot._df = {"NQ_TURTLE30": frame}
        with (
            mock.patch.object(bot, "_can_open_key", return_value=True),
            mock.patch.object(bot, "_signal", return_value=signal()),
        ):
            bot._tick()
        position = bot.pos["NQ_TURTLE30"]
        self.assertEqual(position.entry, 100.25)
        self.assertNotEqual(position.entry, 90.0)
        self.assertEqual(position.last_managed_bar, int(frame.iloc[0]["dt_utc"].timestamp()))
        self.assertEqual(bot.history, [])

    def test_whole_contract_sizing_cap_and_actual_commission(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot._open(signal(stop=100.0), bar(close=100.0))
        position = bot.pos["NQ_TURTLE30"]
        self.assertIsInstance(position.qty, int)
        self.assertEqual(position.qty, audited.MAX_AUDITED_MICROS)
        self.assertEqual(position.cost_usd, position.qty * audited.COMMISSION_ROUND_TURN)
        self.assertLessEqual(position.risk_usd, original.RISK_USD)
        qty, _ = bot._entry_qty(100.25, 100.0, 2.0, 0.25)
        self.assertEqual(qty, 0)

    def test_gap_through_stop_uses_worse_open_then_exit_slippage(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot._open(signal(), bar(close=100.0))
        bot._manage(
            "NQ_TURTLE30",
            bar(close=91.0, open_=90.0, high=92.0, low=89.0, minute=30),
        )
        self.assertNotIn("NQ_TURTLE30", bot.pos)
        self.assertEqual(bot.history[0]["exit"], 89.75)
        self.assertLess(bot.history[0]["pnl"], -original.RISK_USD)

    def test_ambiguous_bar_resolves_to_stop_not_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-11"
        bot._open(signal(), bar(close=100.0))
        bot._manage(
            "NQ_TURTLE30",
            bar(close=100.0, open_=100.0, high=120.0, low=90.0, minute=30),
        )
        self.assertEqual(bot.history[0]["reason"], "stop")

    def test_daily_guard_and_eod_drawdown_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        bot.day_key = "2026-08-10"
        bot.day_pnl = -original.DAILY_LOSS_LIMIT
        self.assertFalse(bot._can_open_key("NQ_TURTLE30", bar()))

        bot.day_pnl = 0.0
        bot.balance = 52_000.0
        bot._roll_day("2026-08-11")
        self.assertEqual(bot.floor, 50_000.0)
        self.assertFalse(bot.locked)
        bot.balance = 52_200.0
        bot._roll_day("2026-08-12")
        self.assertEqual(bot.floor, audited.LOCKED_MLL_BALANCE)
        self.assertTrue(bot.locked)

    def test_state_and_dashboard_name_the_ledger_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bot_in(tmp)
        state = bot.state()
        self.assertTrue(state["execution_audited"])
        self.assertFalse(state["legacy_pnl_carried"])
        self.assertEqual(state["max_micros"], 40)
        self.assertIn("Same five v18 signal rules", state["backtest_note"])
        self.assertIn("proxy rather than a broker fill receipt", state["backtest_note"])
        dashboard = Path("dashboard.py").read_text(encoding="utf-8")
        self.assertIn(audited.NAME, dashboard)


if __name__ == "__main__":
    unittest.main()
