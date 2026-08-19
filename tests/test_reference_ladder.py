from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pandas as pd

import nq_mr_15m_paper as nq
from reference_ladder.config import LadderConfig
from reference_ladder.data import BinanceMinuteLoader
from reference_ladder.engine import LadderBacktester
from reference_ladder.page import REFERENCE_LADDER_HTML
from reference_ladder.research import run_research
from reference_ladder.signals import BollingerRsiSmaSignal, MultiTimeframeDipSignal
from reference_ladder.web import routes


ROOT = Path(__file__).resolve().parents[1]


def frame(rows: list[tuple[float, float, float, float]], start: str = "2024-01-02") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame({
        "open": [row[0] for row in rows],
        "high": [row[1] for row in rows],
        "low": [row[2] for row in rows],
        "close": [row[3] for row in rows],
        "volume": [100.0] * len(rows),
    }, index=index)


def signal_for(data: pd.DataFrame, positions: dict[int, int]) -> pd.Series:
    out = pd.Series(0, index=data.index, dtype=int)
    for index, direction in positions.items():
        out.iloc[index] = direction
    return out


def clean_config(**overrides) -> LadderConfig:
    base = LadderConfig(
        starting_capital=1_000_000.0,
        distance_mode="fixed",
        sizing_mode="fixed",
        fixed_ladder_sizes=(1.0, 1.0, 1.0, 1.0),
        spread_round_turn_usd=0.0,
        commission_rate=0.0,
        base_slippage_usd=0.0,
        slippage_usd_per_btc=0.0,
        funding_rate_8h=0.0,
        max_loss_per_cycle_pct=None,
        max_cycle_duration_hours=None,
        curve_every_bars=1,
    )
    return base.with_overrides(overrides)


class LadderConfigTests(unittest.TestCase):
    def test_default_spec(self):
        config = LadderConfig().validate()
        self.assertEqual(config.symbol, "BTCUSDT")
        self.assertEqual(config.timeframe, "1m")
        self.assertEqual(config.trigger_distance, 800.0)
        self.assertEqual(config.ladder_step, 500.0)
        self.assertEqual(config.fixed_ladder_sizes, (10.0, 20.0, 40.0, 80.0))
        self.assertEqual(config.signal_name, "multitimeframe-dip")
        self.assertEqual(config.signal_timeframe, "4h")
        self.assertEqual(config.distance_mode, "percent")
        self.assertEqual(config.sizing_mode, "equity_fraction")
        self.assertEqual(config.level_multipliers, (1.0, 0.75, 0.5, 0.25))
        self.assertEqual(config.leverage, 5.0)
        self.assertEqual(config.equity_fraction_per_level, 0.20)
        self.assertEqual(config.max_loss_per_cycle_pct, 0.08)
        self.assertEqual(config.max_cycle_duration_hours, 336.0)

    def test_auto_sizing_scales_with_balance(self):
        config = LadderConfig(
            sizing_mode="auto", level_multipliers=(1.0, 2.0, 4.0, 8.0),
        )
        self.assertEqual(config.sizes_for_equity(1_000.0), (0.05, 0.1, 0.2, 0.4))

    def test_equity_fraction_sizing_is_price_invariant_not_fixed_btc(self):
        config = LadderConfig()
        for actual, expected in zip(
            config.sizes_for_equity(100_000.0, 50_000.0),
            (0.4, 0.3, 0.2, 0.1),
        ):
            self.assertAlmostEqual(actual, expected)

    def test_invalid_or_unknown_config_is_rejected(self):
        with self.assertRaises(ValueError):
            LadderConfig().with_overrides({"max_levels": 5})
        with self.assertRaises(ValueError):
            LadderConfig().with_overrides({
                "max_levels": 5,
                "fixed_ladder_sizes": [1, 1, 1, 1, 1],
                "level_multipliers": [1, 1, 1, 1, 1],
            })
        with self.assertRaises(ValueError):
            LadderConfig().with_overrides({"trading_start_hour_utc": 17,
                                           "trading_end_hour_utc": 9})
        with self.assertRaises(ValueError):
            LadderConfig().with_overrides({"commission_rate": -0.01})
        with self.assertRaises(ValueError):
            LadderConfig().with_overrides({"not_real": 1})


class ReferenceSignalTests(unittest.TestCase):
    def test_existing_bollinger_rsi_sma_entry_is_used(self):
        # Recent prices fall hard enough to be oversold while remaining above
        # the much lower 200-bar trend average.
        closes = [80.0] * 200 + [120.0] * 20 + list(
            pd.Series(range(120, 90, -1), dtype=float)
        )
        data = frame([(value, value + 1, value - 4, value) for value in closes])
        legacy = LadderConfig(signal_name="bollinger-rsi-sma", regime_filter=False)
        generated = BollingerRsiSmaSignal().generate(data, legacy)
        self.assertEqual(int(generated.iloc[-1]), 1)

    def test_short_side_is_off_by_default(self):
        closes = [100.0] * 200 + list(pd.Series(range(90, 110), dtype=float))
        data = frame([(value, value + 4, value - 1, value) for value in closes])
        legacy = LadderConfig(signal_name="bollinger-rsi-sma", regime_filter=False)
        generated = BollingerRsiSmaSignal().generate(data, legacy)
        self.assertFalse((generated < 0).any())

    def test_multitimeframe_signal_is_written_only_after_completed_bar(self):
        index = pd.date_range("2024-01-01", periods=6 * 24 * 60, freq="1min", tz="UTC")
        hourly = [100.0] * (4 * 24) + [110.0] * 40 + list(range(110, 85, -1))
        hourly = (hourly + [85.0] * 200)[: len(index) // 60]
        closes = pd.Series(hourly, index=pd.date_range(index[0], periods=len(hourly),
                                                       freq="1h", tz="UTC"))
        minute_close = closes.reindex(index, method="ffill")
        data = pd.DataFrame({
            "open": minute_close, "high": minute_close + 0.5,
            "low": minute_close - 2.0, "close": minute_close,
            "volume": 100.0,
        }, index=index)
        config = LadderConfig(
            signal_timeframe="1h", bb_length=5, rsi_length=2, rsi_oversold=40,
            trend_sma_length=2, regime_slope_lookback=1,
        )
        generated = MultiTimeframeDipSignal().generate(data, config)
        event_times = generated[generated > 0].index
        self.assertGreater(len(event_times), 0)
        self.assertTrue(all(timestamp.minute == 59 for timestamp in event_times))


class LadderEngineTests(unittest.TestCase):
    def test_percent_distances_scale_from_reference_price(self):
        config = clean_config(
            distance_mode="percent", trigger_reference_pct=0.02,
            step_reference_pct=0.01,
        )
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_010, 9_990, 10_000),
            (9_850, 9_900, 9_810, 9_850),
            (9_800, 9_850, 9_790, 9_800),
        ])
        result = LadderBacktester(config).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertEqual(result.cycles[0]["entries"][0]["raw_price"], 9_800.0)
    def test_waits_for_trigger_distance_before_first_real_entry(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_020, 9_980, 10_000),  # reference signal
            (9_500, 9_600, 9_300, 9_400),     # not yet $800 adverse
            (9_250, 9_300, 9_190, 9_250),     # crosses $9,200
        ])
        result = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.cycles[0]["levels_reached"], 1)
        self.assertEqual(result.cycles[0]["entries"][0]["raw_price"], 9_200.0)
        self.assertEqual(result.cycles[0]["entries"][0]["time"], data.index[3].isoformat())

    def test_gap_can_add_every_crossed_level_but_never_more_than_cap(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_010, 9_990, 10_000),
            (7_600, 8_000, 7_500, 7_800),
        ])
        result = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: 1}))
        cycle = result.cycles[0]
        self.assertEqual(cycle["levels_reached"], 4)
        self.assertEqual([entry["level"] for entry in cycle["entries"]], [1, 2, 3, 4])

    def test_entire_ladder_exits_at_reference_price(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_010, 9_990, 10_000),
            (9_250, 9_300, 9_100, 9_250),
            (9_900, 10_010, 9_850, 10_000),
        ])
        result = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: 1}))
        cycle = result.cycles[0]
        self.assertEqual(cycle["reason"], "reference_exit")
        self.assertTrue(cycle["recovered"])
        self.assertEqual(cycle["exit_price"], 10_000.0)
        self.assertGreater(cycle["net_pnl"], 0)

    def test_short_ladder_mirrors_levels_and_reference_exit(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_010, 9_990, 10_000),
            (10_750, 10_810, 10_700, 10_750),
            (10_100, 10_150, 9_990, 10_000),
        ])
        result = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: -1}))
        cycle = result.cycles[0]
        self.assertEqual(cycle["direction"], "short")
        self.assertEqual(cycle["entries"][0]["raw_price"], 10_800.0)
        self.assertEqual(cycle["reason"], "reference_exit")
        self.assertGreater(cycle["net_pnl"], 0)

    def test_same_bar_is_resolved_adverse_first_then_reference_exit(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000),
            (10_000, 10_010, 9_990, 10_000),
            (9_900, 10_010, 9_100, 9_950),
        ])
        result = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertEqual(result.cycles[0]["levels_reached"], 1)
        self.assertEqual(result.cycles[0]["reason"], "reference_exit")

    def test_margin_liquidation_is_modeled(self):
        config = clean_config(
            starting_capital=100.0, trigger_distance=10.0, ladder_step=10.0,
            fixed_ladder_sizes=(10.0, 10.0, 10.0, 10.0), leverage=100.0,
        )
        data = frame([
            (100, 101, 99, 100), (100, 101, 99, 100),
            (91, 92, 89, 91), (85, 86, 80, 82),
        ])
        result = LadderBacktester(config).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertTrue(result.cycles[0]["liquidated"])
        self.assertIn("liquidation", result.cycles[0]["reason"])
        self.assertEqual(result.metrics["liquidations"], 1)
        self.assertEqual(len(result.metrics["liquidation_dates"]), 1)

    def test_optional_max_loss_force_closes_cycle(self):
        config = clean_config(
            starting_capital=1_000.0, trigger_distance=10.0,
            fixed_ladder_sizes=(1.0, 1.0, 1.0, 1.0), max_loss_per_cycle_pct=0.005,
        )
        data = frame([
            (100, 101, 99, 100), (100, 101, 99, 100),
            (91, 92, 89, 91), (85, 86, 80, 82),
        ])
        result = LadderBacktester(config).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertEqual(result.cycles[0]["reason"], "max_cycle_loss")

    def test_duration_can_end_unfilled_reference_cycle(self):
        config = clean_config(max_cycle_duration_hours=2 / 60)
        data = frame([(100, 101, 99, 100)] * 5)
        result = LadderBacktester(config).run(data, signal_override=signal_for(data, {1: 1, 4: 1}))
        self.assertEqual(result.cycles[0]["reason"], "max_duration")
        self.assertEqual(result.cycles[0]["levels_reached"], 0)

    def test_only_one_reference_signal_is_active(self):
        data = frame([(10_000, 10_010, 9_990, 10_000)] * 6)
        result = LadderBacktester(clean_config()).run(
            data, signal_override=signal_for(data, {1: 1, 2: -1, 3: 1}),
        )
        self.assertEqual(len(result.cycles), 1)

    def test_execution_and_funding_costs_reduce_the_result(self):
        data = frame([
            (10_000, 10_010, 9_990, 10_000), (10_000, 10_010, 9_990, 10_000),
            (9_250, 9_300, 9_100, 9_250), (9_900, 10_010, 9_850, 10_000),
        ])
        free = LadderBacktester(clean_config()).run(data, signal_override=signal_for(data, {1: 1}))
        costly_config = clean_config(
            spread_round_turn_usd=10.0, commission_rate=0.001,
            base_slippage_usd=2.0, slippage_usd_per_btc=1.0, funding_rate_8h=0.01,
        )
        costly = LadderBacktester(costly_config).run(data, signal_override=signal_for(data, {1: 1}))
        self.assertLess(costly.cycles[0]["net_pnl"], free.cycles[0]["net_pnl"])
        self.assertGreater(costly.metrics["total_execution_cost"], 0)
        self.assertGreater(costly.metrics["total_funding_cost"], 0)

    def test_optional_profit_target_exits_from_average_entry(self):
        config = clean_config(profit_target_points=100.0)
        data = frame([
            (10_000, 10_010, 9_990, 10_000), (10_000, 10_010, 9_990, 10_000),
            (9_250, 9_300, 9_100, 9_250), (9_250, 9_350, 9_200, 9_300),
        ])
        result = LadderBacktester(config).run(data, signal_override=signal_for(data, {1: 1}))
        cycle = result.cycles[0]
        self.assertEqual(cycle["reason"], "profit_target")
        self.assertEqual(cycle["exit_price"], 9_300.0)

    def test_atr_mode_freezes_signal_bar_distances_for_the_cycle(self):
        rows = [(10_000, 10_010, 9_990, 10_000)] * 14
        rows += [(10_000, 10_020, 9_980, 10_000), (10_000, 10_010, 9_880, 9_900)]
        data = frame(rows)
        config = clean_config(
            distance_mode="atr", trigger_atr_multiple=5.0, step_atr_multiple=2.0,
        )
        result = LadderBacktester(config).run(
            data, signal_override=signal_for(data, {14: 1}),
        )
        self.assertEqual(result.cycles[0]["levels_reached"], 1)
        self.assertAlmostEqual(result.cycles[0]["entries"][0]["raw_price"], 9_892.857143, places=5)

    def test_session_and_weekend_filters_only_gate_new_cycles(self):
        weekday = frame([(100, 101, 99, 100)] * 3, start="2024-01-02 02:00")
        hours = clean_config(trading_start_hour_utc=9, trading_end_hour_utc=17)
        outside = LadderBacktester(hours).run(
            weekday, signal_override=signal_for(weekday, {1: 1}),
        )
        self.assertEqual(outside.metrics["cycles"], 0)

        weekend = frame([(100, 101, 99, 100)] * 3, start="2024-01-06 10:00")
        weekdays_only = clean_config(weekends=False)
        blocked = LadderBacktester(weekdays_only).run(
            weekend, signal_override=signal_for(weekend, {1: 1}),
        )
        self.assertEqual(blocked.metrics["cycles"], 0)


class BinanceDataTests(unittest.TestCase):
    @staticmethod
    def archive(timestamp: int) -> bytes:
        row = f"{timestamp},100,101,99,100,1,0,0,0,0,0,0\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("BTCUSDT-1m.csv", row)
        return buffer.getvalue()

    def test_archive_parser_accepts_milliseconds_and_microseconds(self):
        ms = BinanceMinuteLoader._parse_archive(self.archive(1_700_000_000_000))
        us = BinanceMinuteLoader._parse_archive(self.archive(1_700_000_000_000_000))
        self.assertEqual(ms.index[0], us.index[0])
        self.assertEqual(float(ms.iloc[0]["close"]), 100.0)


class NQBTCConversionTests(unittest.TestCase):
    def test_bot_is_btc_one_minute_100_dollars_at_20x(self):
        self.assertEqual(nq.SYMBOL, "BTCUSDT")
        self.assertEqual(nq.INTERVAL, "1m")
        self.assertEqual(nq.START_BALANCE, 100.0)
        self.assertEqual(nq.LEVERAGE, 20.0)

    def test_old_nq_state_is_never_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "nq_mr_15m_state.json").write_text(
                json.dumps({"balance": 50_000, "enabled": False}), encoding="utf-8",
            )
            with mock.patch.object(nq.config, "DATA_DIR", tmp):
                bot = nq.NQMR15PaperBot()
            self.assertEqual(bot.balance, 100.0)
            self.assertTrue(bot.enabled)

    def test_20x_position_uses_btc_quantity_not_micro_contracts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(nq.config, "DATA_DIR", tmp):
            bot = nq.NQMR15PaperBot()
            bar = pd.Series({"close": 10_000.0, "dt_utc": pd.Timestamp("2024-01-01", tz="UTC")})
            bot._open({"strat": "test", "side": "long", "stop": 9_900.0,
                       "target": 10_200.0, "note": "test"}, bar)
            self.assertAlmostEqual(bot.position.qty, 0.2)
            self.assertEqual(bot.position.margin, 100.0)


class IntegrationTests(unittest.TestCase):
    def test_research_grid_uses_reference_percentages(self):
        data = frame([(100, 101, 99, 100)] * 400)
        report = run_research(data, LadderConfig(
            bb_length=2, rsi_length=2, trend_sma_length=2,
            regime_slope_lookback=1, curve_every_bars=60,
        ))
        self.assertEqual(
            [row["trigger_reference_pct"] for row in report["trigger_sensitivity"]],
            [0.015, 0.02, 0.025, 0.03],
        )
        self.assertTrue(report["heatmap"])
        self.assertTrue(all("step_reference_pct" in row for row in report["heatmap"]))

    def test_dashboard_order_and_routes(self):
        dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
        self.assertLess(dashboard.index('id="lucidcont-panel"'),
                        dashboard.index('id="cryptalmaker-panel"'))
        self.assertIn("*reference_ladder_web.routes()", dashboard)
        self.assertIn("BTC 1m · $100 · 20x", dashboard)

    def test_page_exposes_required_outputs(self):
        for marker in (
            "Max equity DD", "Worst cycle", "Ladder reach", "stress windows",
            "Capacity study", "Walk-forward", "parameter heatmap", "Floating P&amp;L",
            "Trading start hour UTC", "Size slippage", "Maintenance margin rate",
            "Reference trigger (%)", "Equity notional / level (%)", "4h RSI oversold",
        ):
            self.assertIn(marker, REFERENCE_LADDER_HTML)

    def test_page_inline_script_parses_under_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        script = re.search(r"<script>(.*?)</script>", REFERENCE_LADDER_HTML, re.S).group(1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference-ladder.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 0, done.stderr[:800])

    def test_route_set_is_complete(self):
        rendered = "\n".join(str(route) for route in routes())
        for path in ("/reference-ladder", "/api/reference-ladder/config",
                     "/api/reference-ladder/run", "/api/reference-ladder/jobs/{job_id}"):
            self.assertIn(path, rendered)


if __name__ == "__main__":
    unittest.main()
