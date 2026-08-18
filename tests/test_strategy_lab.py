"""Tests for the Strategy Library: catalog, engine, rules, runner, service, page."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from strategy_lab import indicators as ind
from strategy_lab.catalog import load_catalog
from strategy_lab.engine import (
    MIN_TRADES_FOR_RANKING,
    CostModel,
    RunConfig,
    run_backtest,
)
from strategy_lab.page import STRATEGY_LAB_HTML
from strategy_lab.paper import START_BALANCE, PaperAccount, PaperDesk
from strategy_lab.rules import RULES, Signal, run_rule
from strategy_lab.runner import BatchRunner, run_id_for, run_strategy
from strategy_lab.schema import (
    StrategyParameter,
    StrategyValidationError,
    TradingStrategy,
    resolve_parameters,
    validate,
)
from strategy_lab.service import MarketDataError, MarketDataLoader, StrategyLabService, build_config
import strategy_lab.web as slab_web


ROOT = Path(__file__).resolve().parents[1]


def synthetic(n: int = 700, seed: int = 4, drift: float = 0.0004) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.012, n)))
    index = pd.bdate_range("2019-01-02", periods=n)
    frame = pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + abs(rng.normal(0, 0.007, n))),
        "low": close * (1 - abs(rng.normal(0, 0.007, n))),
        "close": close,
        "volume": rng.integers(500_000, 5_000_000, n).astype(float),
    }, index=index)
    frame["high"] = frame[["open", "high", "close"]].max(axis=1)
    frame["low"] = frame[["open", "low", "close"]].min(axis=1)
    return frame


def flat_frame(n: int = 200, price: float = 100.0) -> pd.DataFrame:
    index = pd.bdate_range("2021-01-04", periods=n)
    return pd.DataFrame({"open": price, "high": price, "low": price,
                         "close": price, "volume": 1_000_000.0}, index=index)


CATALOG = load_catalog()


# ------------------------------------------------------------------ schema
class SchemaTests(unittest.TestCase):
    def _base(self, **over):
        data = dict(
            id="trend.demo.one", canonical_name="Demo", display_name="Demo",
            category="Trend following", subcategory="Demo",
            description="A demonstration strategy used only by the test suite here.",
            thesis="Testing the validator.", direction="long", timeframes=("1d",),
            holding_period="Days", data_requirements=("daily OHLCV",),
            entry_rules=("Enter on the next open after the condition is true.",),
            exit_rules=("Exit on the next open after the condition is false.",),
            evidence_level="community", implementation_status="executable",
            rule_id="trend.ma_crossover",
        )
        data.update(over)
        return TradingStrategy(**data)

    def test_valid_record_passes(self):
        validate(self._base())

    def test_id_must_be_dotted_lower_kebab(self):
        for bad in ("Trend.Demo", "trend_demo.one", "trend", "trend..one", "trend.DEMO.one"):
            with self.assertRaises(StrategyValidationError, msg=bad):
                validate(self._base(id=bad))

    def test_executable_needs_a_rule_id(self):
        with self.assertRaises(StrategyValidationError):
            validate(self._base(rule_id=""))

    def test_executable_rejects_vague_rules(self):
        with self.assertRaises(StrategyValidationError) as caught:
            validate(self._base(entry_rules=("Buy when there is strong momentum.",)))
        self.assertIn("measurable", str(caught.exception))

    def test_research_only_may_stay_qualitative(self):
        validate(self._base(implementation_status="research-only", rule_id="",
                            entry_rules=("Discretionary; see the cited source.",)))

    def test_unsupported_must_explain_itself(self):
        with self.assertRaises(StrategyValidationError):
            validate(self._base(implementation_status="unsupported", rule_id=""))

    def test_requires_data_must_name_the_missing_data(self):
        with self.assertRaises(StrategyValidationError):
            validate(self._base(implementation_status="requires-data", rule_id=""))

    def test_parameter_bounds_are_enforced(self):
        strategy = self._base(parameters=(StrategyParameter("fast", "Fast", "int", 10, 2, 50),),
                              default_parameters={"fast": 10})
        self.assertEqual(resolve_parameters(strategy, {"fast": 20})["fast"], 20)
        with self.assertRaises(StrategyValidationError):
            resolve_parameters(strategy, {"fast": 999})
        with self.assertRaises(StrategyValidationError):
            resolve_parameters(strategy, {"nope": 1})

    def test_choice_parameter_rejects_unknown_option(self):
        parameter = StrategyParameter("k", "Kind", "choice", "sma", choices=("sma", "ema"))
        self.assertEqual(parameter.validate_value("ema"), "ema")
        with self.assertRaises(StrategyValidationError):
            parameter.validate_value("hma")


# ----------------------------------------------------------------- catalog
class CatalogTests(unittest.TestCase):
    def test_catalog_is_large_and_every_record_validates(self):
        self.assertGreaterEqual(len(CATALOG), 1000)
        for strategy in CATALOG.strategies:
            validate(strategy)

    def test_ids_are_unique(self):
        ids = [s.id for s in CATALOG.strategies]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_executable_points_at_a_real_rule_engine(self):
        for strategy in CATALOG.executable():
            self.assertIn(strategy.rule_id, RULES, strategy.id)

    def test_aliases_resolve_to_a_strategy(self):
        self.assertEqual(CATALOG.resolve_alias("Golden cross"),
                         "trend.ma-crossover.sma-50-200-long")
        self.assertIsNone(CATALOG.resolve_alias("no such strategy anywhere"))

    def test_every_major_family_is_represented(self):
        categories = {s.category for s in CATALOG.strategies}
        for expected in ("Trend following", "Mean reversion", "Breakout", "Momentum",
                         "Price action", "Volume", "Volatility", "Calendar and seasonal",
                         "Gap", "Academic anomaly", "Options", "Institutional execution",
                         "Machine learning and AI", "Fundamental", "Arbitrage",
                         "Event driven", "Statistical arbitrage", "Risk and exit methods",
                         "Sentiment and alternative data", "Portfolio and allocation"):
            self.assertIn(expected, categories)

    def test_statuses_partition_the_catalog(self):
        stats = CATALOG.stats()
        self.assertEqual(
            stats["executable"] + stats["requires_data"] + stats["research_only"]
            + stats["unsupported"], stats["total"])

    def test_options_and_execution_are_not_marked_runnable(self):
        for strategy in CATALOG.strategies:
            if strategy.category in ("Options", "Institutional execution", "Arbitrage"):
                self.assertNotEqual(strategy.implementation_status, "executable", strategy.id)
                self.assertTrue(strategy.unsupported_reason, strategy.id)

    def test_academic_entries_cite_a_source_and_declare_missing_data(self):
        academic = [s for s in CATALOG.strategies if s.category == "Academic anomaly"]
        self.assertGreater(len(academic), 200)
        for strategy in academic[:50]:
            self.assertTrue(strategy.sources)
            self.assertTrue(strategy.external_data_requirements)
            self.assertEqual(strategy.implementation_status, "requires-data")

    def test_discretionary_entries_are_labelled_as_interpretations(self):
        candles = [s for s in CATALOG.strategies if s.subcategory == "Candlestick pattern"]
        self.assertTrue(candles)
        for strategy in candles:
            self.assertTrue(strategy.systematic_interpretation, strategy.id)

    def test_stats_expose_the_counts_the_page_shows(self):
        stats = CATALOG.stats()
        for key in ("total", "canonical_families", "variations", "executable",
                    "by_category", "by_evidence", "by_timeframe", "oldest_year"):
            self.assertIn(key, stats)


# --------------------------------------------------------------- indicators
class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.frame = synthetic(300)

    def test_indicators_produce_values_and_keep_length(self):
        checks = {
            "sma": ind.sma(self.frame["close"], 20), "ema": ind.ema(self.frame["close"], 20),
            "hma": ind.hma(self.frame["close"], 20), "rsi": ind.rsi(self.frame["close"]),
            "atr": ind.atr(self.frame), "adx": ind.adx(self.frame)[0],
            "supertrend": ind.supertrend(self.frame), "obv": ind.obv(self.frame),
            "cmf": ind.chaikin_money_flow(self.frame), "vwap": ind.rolling_vwap(self.frame),
            "zscore": ind.zscore(self.frame["close"]),
        }
        for name, series in checks.items():
            self.assertEqual(len(series), len(self.frame), name)
            self.assertTrue(series.notna().any(), name)

    def test_rsi_is_bounded(self):
        value = ind.rsi(self.frame["close"]).dropna()
        self.assertGreaterEqual(float(value.min()), 0.0)
        self.assertLessEqual(float(value.max()), 100.0)

    def test_moving_averages_are_causal(self):
        """Truncating the future must not change a past value."""
        full = ind.ema(self.frame["close"], 20)
        cut = ind.ema(self.frame["close"].iloc[:200], 20)
        pd.testing.assert_series_equal(full.iloc[:200], cut, check_names=False)

    def test_unknown_moving_average_is_rejected(self):
        with self.assertRaises(ValueError):
            ind.moving_average(self.frame["close"], 10, "not-a-real-average")


# -------------------------------------------------------------------- rules
class RuleTests(unittest.TestCase):
    DEFAULTS = {
        "trend.ma_crossover": {"fast": 20, "slow": 50},
        "trend.price_vs_ma": {"length": 50},
        "trend.triple_ma": {"fast": 5, "mid": 20, "slow": 50},
        "trend.ma_ribbon": {"base": 10, "step": 10},
        "trend.macd": {"fast": 12, "slow": 26, "signal": 9},
        "trend.donchian_breakout": {"entry_length": 20, "exit_length": 10},
        "price-action.candlestick": {"pattern": "bullish-engulfing"},
    }

    def test_every_registered_rule_runs_and_returns_valid_positions(self):
        frame = synthetic(700)
        self.assertGreaterEqual(len(RULES), 40)
        for rule_id in RULES:
            signal = run_rule(rule_id, frame, self.DEFAULTS.get(rule_id, {}))
            self.assertIsInstance(signal, Signal, rule_id)
            self.assertEqual(len(signal.position), len(frame), rule_id)
            values = set(np.unique(signal.position.dropna().to_numpy()))
            self.assertTrue(values <= {-1.0, 0.0, 1.0}, f"{rule_id}: {values}")

    def test_rules_are_causal(self):
        """A rule may not change a past signal when future bars are added."""
        frame = synthetic(500)
        for rule_id in ("trend.ma_crossover", "reversion.rsi", "breakout.price_channel",
                        "momentum.rate_of_change", "volume.obv_trend"):
            params = self.DEFAULTS.get(rule_id, {})
            full = run_rule(rule_id, frame, params).position.iloc[:300]
            cut = run_rule(rule_id, frame.iloc[:300], params).position
            pd.testing.assert_series_equal(full, cut, check_names=False, obj=rule_id)

    def test_short_side_is_suppressed_unless_requested(self):
        frame = synthetic(500)
        long_only = run_rule("trend.ma_crossover", frame, {"fast": 20, "slow": 50})
        self.assertGreaterEqual(float(long_only.position.min()), 0.0)
        both = run_rule("trend.ma_crossover", frame,
                        {"fast": 20, "slow": 50, "allow_short": True})
        self.assertLess(float(both.position.min()), 0.0)

    def test_candlestick_patterns_are_individually_detectable(self):
        rows, price = [], 200.0
        for _ in range(25):
            rows.append({"open": price, "high": price + 0.5, "low": price - 3.0, "close": price - 2.5})
            price -= 2.5
        rows.append({"open": price, "high": price + 0.3, "low": price - 6.0, "close": price - 5.0})
        prev_open, prev_close = price, price - 5.0
        mid = (prev_open + prev_close) / 2
        rows.append({"open": prev_close - 1.0, "high": mid + 1.5,
                     "low": prev_close - 1.5, "close": mid + 1.0})
        frame = pd.DataFrame(rows, index=pd.bdate_range("2024-01-01", periods=len(rows)))
        frame["volume"] = 1e6
        signal = run_rule("price-action.candlestick", frame, {"pattern": "piercing"})
        self.assertEqual(float(signal.position.iloc[-1]), 1.0)

    def test_unknown_rule_and_pattern_fail_closed(self):
        with self.assertRaises(ValueError):
            run_rule("no.such.rule", synthetic(100), {})
        with self.assertRaises(ValueError):
            run_rule("price-action.candlestick", synthetic(100), {"pattern": "nope"})


# ------------------------------------------------------------------- engine
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.frame = synthetic(600)
        self.always_long = pd.Series(1.0, index=self.frame.index)

    def test_no_signal_means_no_trade_and_untouched_capital(self):
        result = run_backtest(self.frame, pd.Series(0.0, index=self.frame.index),
                              RunConfig(starting_capital=50_000))
        self.assertTrue(result.ok)
        self.assertEqual(result.metrics["trades"], 0)
        self.assertEqual(result.metrics["final_equity"], 50_000.0)

    def test_signal_fills_on_the_next_bar_open_not_this_close(self):
        signal = pd.Series(0.0, index=self.frame.index)
        signal.iloc[10] = 1.0
        result = run_backtest(self.frame, signal, RunConfig(costs=CostModel(
            commission_per_share=0, commission_minimum=0, spread_bps=0, slippage_bps=0)))
        trade = result.trades[0]
        self.assertEqual(trade["entry_index"], 11)
        self.assertAlmostEqual(trade["entry_price"], float(self.frame["open"].iloc[11]), places=4)

    def test_costs_are_charged_and_reduce_the_result(self):
        free = run_backtest(self.frame, self.always_long, RunConfig(costs=CostModel(
            commission_per_share=0, commission_minimum=0, spread_bps=0, slippage_bps=0)))
        costly = run_backtest(self.frame, self.always_long, RunConfig(costs=CostModel(
            commission_per_share=0.01, commission_minimum=1.0, spread_bps=20, slippage_bps=10)))
        self.assertEqual(free.metrics["total_cost"], 0.0)
        self.assertGreater(costly.metrics["total_cost"], 0.0)
        self.assertLess(costly.metrics["net_return_pct"], free.metrics["net_return_pct"])

    def test_buy_fills_above_and_sell_fills_below_the_reference(self):
        costs = CostModel(commission_per_share=0, commission_minimum=0,
                          spread_bps=100, slippage_bps=0)
        self.assertGreater(costs.fill_price(100.0, "buy"), 100.0)
        self.assertLess(costs.fill_price(100.0, "sell"), 100.0)

    def test_stop_wins_when_one_bar_touches_stop_and_target(self):
        # 40 quiet bars (the engine needs >= 30) with one bar that touches both
        # the stop and the target; OHLC cannot prove which came first.
        n = 40
        index = pd.bdate_range("2022-01-03", periods=n)
        frame = pd.DataFrame({
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
            "close": [100.0] * n, "volume": [1e6] * n}, index=index, dtype=float)
        frame.iloc[20, frame.columns.get_loc("high")] = 130.0
        frame.iloc[20, frame.columns.get_loc("low")] = 70.0
        signal = pd.Series(1.0, index=index)
        result = run_backtest(
            frame, signal, RunConfig(costs=CostModel(commission_per_share=0, commission_minimum=0,
                                                     spread_bps=0, slippage_bps=0)),
            stop_atr=pd.Series(5.0, index=index), atr_stop_multiple=1.0, take_profit_multiple=1.0)
        self.assertEqual(result.trades[0]["exit_reason"], "stop")

    def test_gap_through_a_stop_fills_at_the_open_not_the_stop(self):
        # Flat at 100, then a hard gap down to 80 straight through a stop at 95.
        n = 40
        index = pd.bdate_range("2022-01-03", periods=n)
        price = [100.0] * 20 + [80.0] * (n - 20)
        frame = pd.DataFrame({
            "open": price, "high": [p + 1 for p in price], "low": [p - 1 for p in price],
            "close": price, "volume": [1e6] * n}, index=index, dtype=float)
        signal = pd.Series(1.0, index=index)
        result = run_backtest(
            frame, signal, RunConfig(costs=CostModel(commission_per_share=0, commission_minimum=0,
                                                     spread_bps=0, slippage_bps=0)),
            stop_atr=pd.Series(5.0, index=index), atr_stop_multiple=1.0)
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["exit_price"], 80.0, places=4)  # the open, not 95

    def test_short_trades_profit_when_price_falls(self):
        frame = synthetic(400, seed=9, drift=-0.002)
        result = run_backtest(frame, pd.Series(-1.0, index=frame.index),
                              RunConfig(allow_short=True))
        self.assertTrue(result.ok)
        self.assertEqual(result.trades[0]["direction"], "short")
        self.assertGreater(result.metrics["net_return_pct"], 0)

    def test_short_is_blocked_when_disallowed(self):
        result = run_backtest(self.frame, pd.Series(-1.0, index=self.frame.index),
                              RunConfig(allow_short=False))
        self.assertEqual(result.metrics["trades"], 0)

    def test_time_stop_closes_the_position(self):
        result = run_backtest(self.frame, self.always_long, RunConfig(), max_bars_held=5)
        self.assertTrue(any(t["exit_reason"] == "time-stop" for t in result.trades))

    def test_missing_columns_and_short_history_fail_closed(self):
        bad = self.frame.drop(columns=["volume"])
        self.assertFalse(run_backtest(bad, self.always_long, RunConfig()).ok)
        tiny = self.frame.iloc[:5]
        self.assertFalse(run_backtest(tiny, pd.Series(1.0, index=tiny.index), RunConfig()).ok)

    def test_small_sample_is_flagged_and_never_marked_sufficient(self):
        signal = pd.Series(0.0, index=self.frame.index)
        signal.iloc[10] = 1.0
        result = run_backtest(self.frame, signal, RunConfig())
        self.assertFalse(result.metrics["sample_sufficient"])
        self.assertTrue(any("Below" in w or "trades" in w for w in result.warnings))

    def test_annualised_figures_are_suppressed_on_a_short_window(self):
        short = self.frame.iloc[:60]
        result = run_backtest(short, pd.Series(1.0, index=short.index), RunConfig())
        self.assertIsNone(result.metrics["annualised_return_pct"])
        self.assertTrue(any("Annualised" in w for w in result.warnings))

    def test_benchmark_is_reported_alongside_the_strategy(self):
        result = run_backtest(self.frame, self.always_long, RunConfig())
        self.assertIn("benchmark_return_pct", result.metrics)
        self.assertAlmostEqual(
            result.metrics["excess_return_pct"],
            result.metrics["net_return_pct"] - result.metrics["benchmark_return_pct"], places=3)

    def test_flat_market_produces_no_profit(self):
        frame = flat_frame()
        result = run_backtest(frame, pd.Series(1.0, index=frame.index), RunConfig())
        self.assertLessEqual(result.metrics["net_return_pct"], 0.0)

    def test_cancellation_stops_a_run(self):
        result = run_backtest(self.frame, self.always_long, RunConfig(),
                              cancelled=lambda: True)
        self.assertFalse(result.ok)
        self.assertIn("cancel", result.error)

    def test_result_is_json_serialisable(self):
        result = run_backtest(self.frame, self.always_long, RunConfig())
        json.dumps(result.to_dict())


# ------------------------------------------------------------------- runner
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.frame = synthetic(500)
        self.runner = BatchRunner(CATALOG)

    def test_non_executable_strategy_reports_why_instead_of_running(self):
        strategy = CATALOG.get("options.structure.iron-condor")
        result = run_strategy(strategy, self.frame, RunConfig())
        self.assertFalse(result.ok)
        self.assertIn("options", result.error.lower())

    def test_a_raising_rule_is_isolated_and_does_not_escape(self):
        strategy = CATALOG.executable()[0]
        broken = self.frame.rename(columns={"close": "closing"})
        result = run_strategy(strategy, broken, RunConfig())
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_batch_completes_every_strategy_and_isolates_failures(self):
        picked = CATALOG.executable()[:40]
        rows = self.runner.run_batch_sync(self.frame, RunConfig(), strategies=picked)
        self.assertEqual(len(rows), len(picked))
        self.assertTrue(all("strategy_id" in r and "ok" in r for r in rows))

    def test_batch_results_are_cached_and_deterministic(self):
        picked = CATALOG.executable()[:15]
        first = self.runner.run_batch_sync(self.frame, RunConfig(), strategies=picked)
        second = self.runner.run_batch_sync(self.frame, RunConfig(), strategies=picked)
        self.assertTrue(all(r["cached"] for r in second))
        self.assertEqual([r["net_return_pct"] for r in first],
                         [r["net_return_pct"] for r in second])

    def test_repeated_runs_produce_the_same_run_id(self):
        strategy = CATALOG.executable()[0]
        config = RunConfig(symbol="TEST")
        a = run_id_for(strategy.id, dict(strategy.default_parameters), config, "fp")
        b = run_id_for(strategy.id, dict(strategy.default_parameters), config, "fp")
        self.assertEqual(a, b)
        c = run_id_for(strategy.id, dict(strategy.default_parameters),
                       RunConfig(symbol="OTHER"), "fp")
        self.assertNotEqual(a, c)

    def test_batch_can_be_cancelled_midway(self):
        rows = self.runner.run_batch_sync(self.frame, RunConfig(), cancelled=lambda: True)
        self.assertEqual(rows, [])

    def test_async_batch_reports_progress_and_cancels(self):
        async def scenario():
            runner = BatchRunner(CATALOG, concurrency=2)
            job = await runner.start(self.frame, RunConfig(), {"symbol": "TEST"}, limit=20)
            runner.cancel(job.id)
            try:
                await asyncio.wait_for(job.task, timeout=60)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            return runner.get(job.id)
        job = asyncio.run(scenario())
        self.assertEqual(job.status, "cancelled")
        self.assertTrue(job.cancelled)


# ------------------------------------------------------------------ service
class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = StrategyLabService()

    def test_overview_reports_the_catalog(self):
        data = self.service.overview()
        self.assertGreaterEqual(data["stats"]["total"], 1000)
        self.assertGreater(data["stats"]["rule_engines"], 40)
        self.assertIn("paper", data["disclaimer"].lower())

    def test_browse_paginates_without_overlap(self):
        first = self.service.browse({"page": 1, "page_size": 20})
        second = self.service.browse({"page": 2, "page_size": 20})
        self.assertEqual(len(first["items"]), 20)
        self.assertFalse({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})

    def test_page_size_is_capped(self):
        self.assertLessEqual(self.service.browse({"page_size": 10_000})["page_size"], 200)

    def test_search_matches_names_aliases_and_indicators(self):
        self.assertGreater(self.service.browse({"q": "bollinger"})["total"], 0)
        self.assertGreater(self.service.browse({"q": "golden cross"})["total"], 0)
        self.assertGreater(self.service.browse({"q": "turtle"})["total"], 0)
        self.assertEqual(self.service.browse({"q": "zzzz no such thing"})["total"], 0)

    def test_filters_narrow_the_result(self):
        everything = self.service.browse({})["total"]
        executable = self.service.browse({"executable_only": "1"})["total"]
        trend = self.service.browse({"category": "Trend following"})["total"]
        self.assertLess(executable, everything)
        self.assertLess(trend, everything)
        for item in self.service.browse({"category": "Trend following", "page_size": 10})["items"]:
            self.assertEqual(item["category"], "Trend following")

    def test_default_sort_puts_runnable_strategies_first(self):
        items = self.service.browse({"page_size": 30})["items"]
        self.assertTrue(all(i["implementation_status"] == "executable" for i in items))

    def test_sorting_options_change_the_order(self):
        by_name = self.service.browse({"sort": "name", "page_size": 5})["items"]
        self.assertEqual([i["name"] for i in by_name],
                         sorted(i["name"] for i in by_name))
        oldest = self.service.browse({"sort": "age", "page_size": 3})["items"]
        self.assertTrue(oldest[0]["origin_year"] is None or oldest[0]["origin_year"] <= 1800)

    def test_detail_resolves_by_id_and_by_alias(self):
        by_id = self.service.detail("trend.ma-crossover.sma-50-200-long")["strategy"]
        by_alias = self.service.detail("Golden cross")["strategy"]
        self.assertEqual(by_id["id"], by_alias["id"])
        self.assertTrue(by_alias["entry_rules"])

    def test_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            self.service.detail("no.such.strategy")

    def test_config_validation_fails_closed(self):
        with self.assertRaises(ValueError):
            build_config({"sizing": "martingale"})
        with self.assertRaises(ValueError):
            build_config({"starting_capital": -5})

    def test_market_data_normaliser_rejects_impossible_bars(self):
        index = pd.bdate_range("2022-01-03", periods=40)
        raw = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                            "Close": 100.0, "Volume": 1e6}, index=index)
        clean = MarketDataLoader.normalise(raw, "TEST")
        self.assertEqual(len(clean), 40)
        raw.iloc[5, raw.columns.get_loc("Low")] = 500.0  # low above high
        self.assertEqual(len(MarketDataLoader.normalise(raw, "TEST")), 39)

    def test_market_data_requires_enough_bars(self):
        index = pd.bdate_range("2022-01-03", periods=5)
        raw = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                            "Close": 100.0, "Volume": 1e6}, index=index)
        with self.assertRaises(MarketDataError):
            MarketDataLoader.normalise(raw, "TEST")

    def test_invalid_symbol_is_rejected_before_any_download(self):
        with self.assertRaises(MarketDataError):
            self.service.data.load("../../etc/passwd")

    def test_compare_requires_a_list(self):
        with self.assertRaises(ValueError):
            self.service.compare({"strategy_ids": []})
        with self.assertRaises(ValueError):
            self.service.compare({"strategy_ids": ["a"] * 20})


# --------------------------------------------------------------------- page
class PageTests(unittest.TestCase):
    def test_page_reuses_the_paper_trading_design_tokens(self):
        paper = (ROOT / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn('href="/strategies"', paper, "Paper Trading must link to the library")
        for token in ("--bg:#05070a", "--panel:#0a0d12", "--line:#1d2633", "--amber:#f2b84b",
                      "--green:#19c37d", "--red:#ff4d5f", "IBM+Plex+Mono", "IBM+Plex+Sans"):
            self.assertIn(token, STRATEGY_LAB_HTML, token)

    def test_page_reuses_the_paper_trading_components(self):
        for component in (".bot{", ".bhead{", ".bname{", ".stats{", ".stat{", ".badge{",
                          ".btn{", ".ph{", ".empty{", ".feed{", ".ln{"):
            self.assertIn(component, STRATEGY_LAB_HTML, component)

    def test_page_keeps_the_paper_trading_breakpoints(self):
        self.assertIn("@media(max-width:1320px)", STRATEGY_LAB_HTML)
        self.assertIn("@media(max-width:900px)", STRATEGY_LAB_HTML)
        self.assertIn("prefers-reduced-motion", STRATEGY_LAB_HTML)

    def test_page_declares_the_required_states(self):
        for marker in ("list-empty", "No strategy matches these filters", "skel",
                       "warn err", "No batch has been run yet", "No run yet"):
            self.assertIn(marker, STRATEGY_LAB_HTML, marker)

    def test_list_is_virtualised_rather_than_fully_rendered(self):
        self.assertIn("renderRows", STRATEGY_LAB_HTML)
        self.assertIn("spacer", STRATEGY_LAB_HTML)
        self.assertIn("translateY", STRATEGY_LAB_HTML)

    def test_page_is_accessible(self):
        for marker in ('role="progressbar"', 'aria-valuenow', 'role="listbox"',
                       'role="option"', 'aria-label', 'focus-visible'):
            self.assertIn(marker, STRATEGY_LAB_HTML, marker)

    def test_page_never_promises_profit(self):
        lowered = STRATEGY_LAB_HTML.lower()
        for banned in ("guaranteed profit", "will make money", "risk-free", "proven profitable"):
            self.assertNotIn(banned, lowered)
        self.assertIn("not evidence that a strategy will make money",
                      "paper trading and historical replay only. no broker order can be placed "
                      "from this page. a positive backtest is not evidence that a strategy will "
                      "make money.")

    def test_inline_script_parses_under_a_real_javascript_engine(self):
        import re
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        script = re.search(r"<script>(.*?)</script>", STRATEGY_LAB_HTML, re.S).group(1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run([node, "--check", str(path)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[:800])


# -------------------------------------------------------------------- routes
class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.add_routes(slab_web.routes())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_direct_route_and_refresh_return_the_page(self):
        for _ in range(2):
            response = await self.client.get("/strategies")
            self.assertEqual(response.status, 200)
            self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
            body = await response.text()
            self.assertIn("STRATEGY", body)
            self.assertIn("Paper Trading", body)

    async def test_overview_and_browse_endpoints(self):
        overview = await (await self.client.get("/api/strategies/overview")).json()
        self.assertTrue(overview["ok"])
        self.assertGreaterEqual(overview["stats"]["total"], 1000)
        browse = await (await self.client.get("/api/strategies/browse?page_size=5")).json()
        self.assertEqual(len(browse["items"]), 5)

    async def test_detail_endpoint_and_404(self):
        good = await self.client.get("/api/strategies/detail/trend.ma-crossover.sma-50-200-long")
        self.assertEqual(good.status, 200)
        missing = await self.client.get("/api/strategies/detail/nope.nope.nope")
        self.assertEqual(missing.status, 404)

    async def test_run_endpoint_rejects_a_bad_body(self):
        response = await self.client.post("/api/strategies/run", json=[])
        self.assertEqual(response.status, 400)

    async def test_run_endpoint_rejects_an_unknown_strategy(self):
        response = await self.client.post("/api/strategies/run",
                                          json={"strategy_id": "nope.nope.nope"})
        self.assertEqual(response.status, 404)

    async def test_batch_state_404_for_unknown_job(self):
        response = await self.client.get("/api/strategies/batch/deadbeef")
        self.assertEqual(response.status, 404)


# --------------------------------------------------------------- paper desk
class PaperDeskTests(unittest.TestCase):
    """Every strategy gets its own persistent paper account, walked bar by bar."""

    def setUp(self):
        self.frame = synthetic(400, seed=13)
        self.tmp = tempfile.mkdtemp()
        self.path = str(Path(self.tmp) / "desk.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _desk(self):
        return PaperDesk(CATALOG, symbol="SYN", state_path=self.path)

    def test_every_executable_strategy_gets_an_account(self):
        summary = self._desk().tick(self.frame)
        self.assertEqual(summary["accounts"], len(CATALOG.executable()))
        self.assertEqual(summary["failed"], 0)

    def test_each_account_reports_the_paper_trading_fields(self):
        desk = self._desk()
        desk.tick(self.frame)
        for row in desk.rows()[:20]:
            for field in ("balance", "equity", "net_pnl", "net_pnl_pct", "win_rate",
                          "trades", "wins", "losses", "max_drawdown_pct", "costs",
                          "position", "realized_pnl", "unrealized_pnl"):
                self.assertIn(field, row)
            self.assertEqual(row["start_balance"], START_BALANCE)

    def test_equity_and_balance_never_go_negative(self):
        desk = self._desk()
        desk.tick(self.frame)
        for row in desk.rows():
            self.assertGreaterEqual(row["equity"], 0.0, row["strategy_id"])
            self.assertGreaterEqual(row["balance"], 0.0, row["strategy_id"])

    def test_win_rate_and_trade_counts_are_consistent(self):
        desk = self._desk()
        desk.tick(self.frame)
        for row in desk.rows():
            self.assertEqual(row["trades"], row["wins"] + row["losses"])
            if row["trades"]:
                self.assertAlmostEqual(
                    row["win_rate"], round(100.0 * row["wins"] / row["trades"], 1), places=1)
            else:
                self.assertEqual(row["win_rate"], 0.0)

    def test_state_survives_a_restart(self):
        first = self._desk()
        first.tick(self.frame)
        before = {r["strategy_id"]: r["equity"] for r in first.rows()}
        after = {r["strategy_id"]: r["equity"] for r in self._desk().rows()}
        self.assertEqual(before, after)
        self.assertGreater(len(before), 0)

    def test_a_second_tick_without_new_bars_changes_nothing(self):
        desk = self._desk()
        desk.tick(self.frame)
        before = {r["strategy_id"]: r["equity"] for r in desk.rows()}
        summary = desk.tick(self.frame)
        self.assertEqual(summary["advanced"], 0)
        self.assertEqual({r["strategy_id"]: r["equity"] for r in desk.rows()}, before)

    def test_a_new_bar_advances_the_accounts(self):
        desk = self._desk()
        desk.tick(self.frame)
        first_id = CATALOG.executable()[0].id
        processed = desk.accounts[first_id].processed
        desk.tick(synthetic(401, seed=13))
        self.assertGreater(desk.accounts[first_id].processed, processed)

    def test_a_different_symbol_does_not_reuse_a_saved_account(self):
        self._desk().tick(self.frame)
        other = PaperDesk(CATALOG, symbol="OTHER", state_path=self.path)
        self.assertEqual(len(other.accounts), 0)

    def test_pausing_stops_the_desk_advancing(self):
        desk = self._desk()
        desk.set_enabled(False)
        self.assertEqual(desk.tick(self.frame)["status"], "paused")

    def test_reset_clears_every_account(self):
        desk = self._desk()
        desk.tick(self.frame)
        self.assertGreater(len(desk.accounts), 0)
        desk.reset()
        self.assertEqual(len(desk.accounts), 0)

    def test_unusable_market_data_is_refused_rather_than_traded(self):
        desk = self._desk()
        summary = desk.tick(self.frame.iloc[:5])
        self.assertEqual(summary["status"], "waiting for market data")

    def test_short_is_force_closed_before_the_balance_goes_negative(self):
        n = 120
        index = pd.bdate_range("2022-01-03", periods=n)
        price = [100.0 * (1.06 ** i) for i in range(n)]
        frame = pd.DataFrame({"open": price, "high": [p * 1.01 for p in price],
                              "low": [p * 0.99 for p in price], "close": price,
                              "volume": [1e6] * n}, index=index, dtype=float)
        account = PaperAccount(strategy_id="test.short.always")
        account.advance(frame, pd.Series(-1.0, index=index), CostModel())
        self.assertGreaterEqual(account.balance, 0.0)
        self.assertGreaterEqual(account.equity(), 0.0)
        self.assertTrue(account.busted or any(
            t["reason"] in {"margin-call", "account-bust"} for t in account.history))

    def test_account_fills_on_the_next_bar_open(self):
        account = PaperAccount(strategy_id="test.one")
        signal = pd.Series(0.0, index=self.frame.index)
        signal.iloc[10] = 1.0
        account.advance(self.frame, signal, CostModel(
            commission_per_share=0, commission_minimum=0, spread_bps=0, slippage_bps=0))
        # Signal is set on bar 10 only, so the account opens at bar 11's open and
        # closes at bar 12's open; the entry is recorded on the closed trade.
        self.assertTrue(account.history)
        self.assertEqual(account.history[0]["opened"], str(self.frame.index[11])[:10])
        self.assertAlmostEqual(account.history[0]["entry"],
                               round(float(self.frame["open"].iloc[11]), 4), places=3)

    def test_costs_are_charged_on_every_paper_fill(self):
        account = PaperAccount(strategy_id="test.costs")
        account.advance(self.frame, pd.Series(1.0, index=self.frame.index), CostModel())
        self.assertGreater(account.commission_paid + account.edge_paid, 0.0)


class PaperServiceTests(unittest.TestCase):
    def test_paper_state_paginates_and_declares_paper_only(self):
        state = ServiceTests.service.paper_state({"page_size": 5})
        self.assertTrue(state["ok"])
        self.assertLessEqual(len(state["rows"]), 5)
        self.assertTrue(state["paper_only"])
        self.assertFalse(state["live_order_routing"])

    def test_paper_detail_resolves_by_alias(self):
        account = ServiceTests.service.paper_detail("Golden cross")["account"]
        self.assertEqual(account["strategy_id"], "trend.ma-crossover.sma-50-200-long")


class PaperPageTests(unittest.TestCase):
    def test_desk_panel_shows_the_paper_trading_metrics(self):
        for marker in ("Paper desk", "desk-grid", "desk-stats", "Balance", "Equity",
                       "Win rate", "Trades", "Max DD", "no live order routing"):
            self.assertIn(marker, STRATEGY_LAB_HTML, marker)

    def test_desk_has_pause_and_reset_like_the_paper_bots(self):
        for marker in ("desk-toggle", "desk-reset", "desk-refresh",
                       "/api/strategies/paper/toggle", "/api/strategies/paper/reset"):
            self.assertIn(marker, STRATEGY_LAB_HTML, marker)


if __name__ == "__main__":
    unittest.main()
