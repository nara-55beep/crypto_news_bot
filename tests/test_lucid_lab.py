from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lucid_lab.engine import (
    D,
    LucidAccount,
    PositionSizeInput,
    TradeFill,
    WorkingLimitOrder,
    calculate_position_size,
    marketable_fill_price,
    new_york_time,
    resolve_bar_exit,
    validate_market_data,
)
from lucid_lab.page import LUCID_LAB_HTML
from lucid_lab.rules import (
    INSTRUMENTS,
    RULES_LAST_CHECKED,
    SOURCES,
    get_account_rules,
    official_sources,
    public_evaluation_options,
)
from lucid_lab.service import EvidenceStore, LucidLabService, SimulationRegistry
import lucid_lab.web as lab_web


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "research" / "ta_strat" / "results" / "lucid_lab_validation.json"


def fill(
    session: str,
    net: str,
    *,
    instrument: str = "MNQ",
    quantity: int = 1,
    commission: str = "0",
    spread: str = "0",
    slippage: str = "0",
    forced: bool = False,
    peak: str | None = None,
    low: str | None = None,
) -> TradeFill:
    costs = D(commission) + D(spread) + D(slippage)
    return TradeFill(
        session=session,
        instrument=instrument,
        quantity=quantity,
        gross_pnl=D(net) + costs,
        commission=D(commission),
        spread_cost=D(spread),
        slippage_cost=D(slippage),
        forced_liquidation=forced,
        intraday_peak_equity=None if peak is None else D(peak),
        intraday_low_equity=None if low is None else D(low),
    )


class TestVerifiedRuleSelection(unittest.TestCase):
    def test_every_public_option_round_trips_through_typed_selector(self):
        options = public_evaluation_options()
        self.assertEqual({row["id"] for row in options}, {"lucidpro", "lucidflex", "lucidblack", "luciddaily"})
        for option in options:
            for size in option["sizes"]:
                rules = get_account_rules(option["id"], "evaluation", size)
                self.assertEqual(rules.account_size, size)
                self.assertGreater(rules.max_micros, 0)
                self.assertTrue(rules.source_keys)

    def test_program_rules_are_not_mixed(self):
        pro = get_account_rules("lucidpro", "evaluation", 50_000)
        flex = get_account_rules("lucidflex", "evaluation", 50_000)
        black = get_account_rules("lucidblack", "evaluation", 50_000)
        daily = get_account_rules("luciddaily", "evaluation", 50_000, daily_drawdown="intraday", daily_loss_enabled=False)
        self.assertEqual(pro.daily_loss_limit, Decimal("1200"))
        self.assertIsNone(pro.consistency_limit_pct)
        self.assertIsNone(flex.daily_loss_limit)
        self.assertEqual(flex.consistency_limit_pct, Decimal("50"))
        self.assertEqual(black.consistency_limit_pct, Decimal("60"))
        self.assertEqual(daily.drawdown_type, "intraday")
        self.assertIsNone(daily.daily_loss_limit)

    def test_luciddaily_25k_uses_its_own_600_dollar_dll(self):
        daily = get_account_rules(
            "luciddaily", "evaluation", 25_000,
            daily_drawdown="eod", daily_loss_enabled=True,
        )
        self.assertEqual(daily.daily_loss_limit, Decimal("600"))

    def test_unverified_combinations_fail_closed(self):
        with self.assertRaises(ValueError):
            get_account_rules("lucidblack", "evaluation", 150_000)
        with self.assertRaises(ValueError):
            get_account_rules("luciddirect", "evaluation", 50_000)
        with self.assertRaises(ValueError):
            get_account_rules("lucidpro", "funded", 50_000)

    def test_major_rules_have_official_traceability(self):
        self.assertEqual(RULES_LAST_CHECKED, "2026-08-14")
        for source in official_sources():
            self.assertTrue(source["url"].startswith("https://"))
            self.assertIn("lucidtrading.com", source["url"])
            self.assertEqual(source["retrieved_at"], RULES_LAST_CHECKED)

    def test_account_ratios_are_computed_from_rules(self):
        ratios = {
            size: Decimal(get_account_rules("lucidpro", "evaluation", size).to_dict()["target_to_drawdown"])
            for size in (25_000, 50_000, 100_000, 150_000)
        }
        self.assertEqual(ratios, {25_000: D("1.25"), 50_000: D("1.50"), 100_000: D("2.00"), 150_000: D("2.00")})

    def test_each_displayed_rule_carries_exact_scope_and_source(self):
        for option in public_evaluation_options():
            rules = get_account_rules(option["id"], "evaluation", option["sizes"][0])
            for key in (
                "profit_target", "maximum_loss", "drawdown_type", "daily_loss_limit",
                "consistency_limit", "maximum_contracts", "scaling", "minimum_trading_days",
                "forced_close", "overnight", "weekend", "news", "commission_micro",
            ):
                item = rules.rule_metadata[key]
                self.assertEqual(item.program, rules.program_label)
                self.assertEqual(item.stage, "evaluation")
                self.assertEqual(item.account_size, rules.account_size)
                self.assertIn(item.source, [SOURCES[name] for name in rules.source_keys])

    def test_official_session_permission_is_distinct_from_flat_strategy_policy(self):
        self.assertTrue(get_account_rules("lucidpro", "evaluation", 25_000).overnight_allowed)
        self.assertTrue(get_account_rules("lucidflex", "evaluation", 25_000).overnight_allowed)
        self.assertFalse(get_account_rules("lucidblack", "evaluation", 25_000).overnight_allowed)
        self.assertIn("strategy", get_account_rules("lucidpro", "evaluation", 25_000).rule_metadata["overnight"].notes.lower())


class TestAccountStateMachine(unittest.TestCase):
    def test_one_cent_above_floor_survives_but_equality_breaches(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        safe = LucidAccount(rules)
        safe.process_fill(fill("2026-08-03", "-999.99"))
        self.assertFalse(safe.breached)
        breached = LucidAccount(rules)
        breached.process_fill(fill("2026-08-03", "-1000.00"))
        self.assertTrue(breached.breached)
        self.assertEqual(breached.balance, Decimal("24000.00"))

    def test_eod_floor_moves_only_at_session_end(self):
        rules = get_account_rules("lucidpro", "evaluation", 50_000)
        account = LucidAccount(rules)
        account.process_fill(fill("2026-08-03", "500"))
        self.assertEqual(account.floor, Decimal("48000.00"))
        row = account.end_day()
        self.assertEqual(row.drawdown_floor, Decimal("48500.00"))
        self.assertEqual(row.remaining_drawdown, Decimal("2000.00"))

    def test_lock_requires_strictly_exceeding_trigger(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        exact = LucidAccount(rules)
        exact.process_fill(fill("2026-08-03", "1100"))
        exact.end_day()
        self.assertFalse(exact.trail_locked)
        above = LucidAccount(rules)
        above.process_fill(fill("2026-08-03", "1100.01"))
        above.end_day()
        self.assertTrue(above.trail_locked)
        self.assertEqual(above.floor, Decimal("25100.00"))

    def test_daily_loss_is_soft_and_resets_next_session(self):
        rules = get_account_rules("lucidpro", "evaluation", 50_000)
        account = LucidAccount(rules)
        account.process_fill(fill("2026-08-03", "-1200"))
        self.assertTrue(account.restricted)
        self.assertFalse(account.breached)
        with self.assertRaises(RuntimeError):
            account.process_fill(fill("2026-08-03", "10"))
        account.end_day()
        account.start_day("2026-08-04")
        self.assertFalse(account.restricted)

    def test_consistency_blocks_then_later_allows_pass(self):
        rules = get_account_rules("lucidflex", "evaluation", 25_000)
        account = LucidAccount(rules)
        account.process_fill(fill("2026-08-03", "700")); account.end_day()
        account.process_fill(fill("2026-08-04", "550")); account.end_day()
        self.assertFalse(account.passed)
        self.assertIn("consistency", account.reason)
        account.process_fill(fill("2026-08-05", "210")); account.end_day()
        self.assertTrue(account.passed)
        self.assertLessEqual(account.consistency_pct, Decimal("50"))

    def test_intraday_peak_advances_floor_and_low_can_breach(self):
        rules = get_account_rules("luciddaily", "evaluation", 25_000, daily_drawdown="intraday", daily_loss_enabled=False)
        account = LucidAccount(rules)
        account.process_fill(fill("2026-08-03", "100", peak="25500", low="25000"))
        self.assertEqual(account.floor, Decimal("24500.00"))
        account.process_fill(fill("2026-08-03", "-200", peak="25500", low="24500"))
        self.assertTrue(account.breached)

    def test_cost_components_reconcile_exactly(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        account = LucidAccount(rules)
        account.process_fill(fill("2026-08-03", "95", commission="1", spread="2", slippage="2"))
        self.assertEqual(account.gross_pnl, Decimal("100.00"))
        self.assertEqual(account.balance, Decimal("25095.00"))
        self.assertEqual(account.state()["net_profit"], "95.00")

    def test_mini_micro_cap_and_forced_liquidation_warning(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        account = LucidAccount(rules)
        with self.assertRaises(ValueError):
            account.process_fill(fill("2026-08-03", "10", instrument="NQ", quantity=3))
        account.process_fill(fill("2026-08-03", "10", instrument="NQ", quantity=2, forced=True))
        row = account.end_day()
        self.assertTrue(any("force-closed" in warning for warning in row.warnings))

    def test_target_boundary_requires_the_last_cent(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        short = LucidAccount(rules)
        short.process_fill(fill("2026-08-03", "1249.99"))
        short.end_day()
        self.assertFalse(short.passed)
        exact = LucidAccount(rules)
        exact.process_fill(fill("2026-08-03", "1250.00"))
        exact.end_day()
        self.assertTrue(exact.passed)
        self.assertEqual(exact.scaling_tier, "evaluation-fixed")
        self.assertEqual(exact.remaining_micros, rules.max_micros)

    def test_open_exposure_is_aggregate_and_blocks_session_close(self):
        account = LucidAccount(get_account_rules("lucidpro", "evaluation", 25_000))
        account.start_day("2026-08-03")
        account.reserve_exposure("NQ", 1)
        account.reserve_exposure("MNQ", 9)
        self.assertEqual(account.open_micro_equivalents, 19)
        self.assertEqual(account.remaining_micros, 1)
        self.assertEqual(account.state()["open_exposure_by_instrument"], {"MNQ": 9, "NQ": 1})
        with self.assertRaises(ValueError):
            account.reserve_exposure("MNQ", 2)
        with self.assertRaises(ValueError):
            account.release_exposure("MES", 1)
        with self.assertRaises(RuntimeError):
            account.end_day()
        account.release_exposure("MNQ", 9)
        account.release_exposure("NQ", 1)
        row = account.end_day()
        self.assertEqual(row.open_micro_equivalents, 0)
        self.assertEqual(row.scaling_tier, "evaluation-fixed")

    def test_open_equity_can_breach_intraday_floor(self):
        account = LucidAccount(get_account_rules(
            "luciddaily", "evaluation", 25_000,
            daily_drawdown="intraday", daily_loss_enabled=False,
        ))
        account.start_day("2026-08-03")
        account.reserve_exposure("MNQ", 1)
        account.mark_to_market("500", observed_peak_equity="25500")
        self.assertEqual(account.floor, Decimal("24500.00"))
        account.mark_to_market("-500", observed_low_equity="24500")
        self.assertTrue(account.breached)
        self.assertIn("open equity", account.reason)


class TestExecutionAndSizing(unittest.TestCase):
    def test_stop_first_and_gap_worse_long(self):
        ambiguous = resolve_bar_exit(side="long", stop=95, target=105, bar_open=100, bar_high=106, bar_low=94, tick_size=.25)
        self.assertEqual(ambiguous.reason, "stop")
        self.assertEqual(ambiguous.price, Decimal("94.75"))
        gap = resolve_bar_exit(side="long", stop=95, target=105, bar_open=93, bar_high=94, bar_low=92, tick_size=.25)
        self.assertEqual(gap.price, Decimal("92.75"))

    def test_stop_first_and_gap_worse_short(self):
        gap = resolve_bar_exit(side="short", stop=105, target=95, bar_open=107, bar_high=108, bar_low=106, tick_size=.25)
        self.assertEqual(gap.reason, "stop")
        self.assertEqual(gap.price, Decimal("107.25"))

    def test_target_pays_adverse_exit_tick(self):
        result = resolve_bar_exit(side="long", stop=95, target=105, bar_open=100, bar_high=106, bar_low=99, tick_size=.25)
        self.assertEqual(result, type(result)("target", Decimal("104.75")))

    def test_mnq_size_includes_stop_spread_slippage_and_commission(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        result = calculate_position_size(PositionSizeInput(
            "MNQ", D("25000"), D("24000"), None, D("40"), D("400"), D("100"), 0, "normal"
        ), rules)
        self.assertEqual(result.risk_per_contract, Decimal("22.00"))
        self.assertEqual(result.maximum_by_risk, 18)
        self.assertEqual(result.final_quantity, 18)
        self.assertEqual(result.expected_cost, Decimal("36.00"))

    def test_account_cap_and_existing_usage_bind_quantity(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        mini = calculate_position_size(PositionSizeInput(
            "NQ", D("25000"), D("24000"), None, D("4"), D("900"), D("0"), 0, "normal"
        ), rules)
        self.assertEqual(mini.maximum_by_account_cap, 2)
        self.assertEqual(mini.final_quantity, 2)
        micro = calculate_position_size(PositionSizeInput(
            "MNQ", D("25000"), D("24000"), None, D("4"), D("900"), D("0"), 19, "normal"
        ), rules)
        self.assertEqual(micro.final_quantity, 1)

    def test_invalid_risk_inputs_fail_closed(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        with self.assertRaises(ValueError):
            calculate_position_size(PositionSizeInput("MNQ", D("24000"), D("24000"), None, D("1"), D("100"), D("0")), rules)

    def test_dst_conversion_uses_exchange_timezone(self):
        before = new_york_time("2026-03-06T14:30:00+00:00")
        after = new_york_time("2026-03-09T13:30:00+00:00")
        self.assertEqual((before.hour, before.minute), (9, 30))
        self.assertEqual((after.hour, after.minute), (9, 30))
        with self.assertRaises(ValueError):
            new_york_time("2026-03-09T13:30:00")

    def test_trade_print_order_lifecycle_is_timestamp_and_queue_aware(self):
        placed = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        order = WorkingLimitOrder(
            "o1", "buy", D("100"), 3, placed,
            latency_ms=10, queue_ahead=D("2"),
        )
        self.assertEqual(order.process_trade(timestamp=placed + timedelta(milliseconds=10), price=99, quantity=10), 0)
        self.assertEqual(order.process_trade(timestamp=placed + timedelta(milliseconds=11), price=101, quantity=10), 0)
        self.assertEqual(order.process_trade(timestamp=placed + timedelta(milliseconds=12), price=100, quantity=3), 1)
        self.assertEqual(order.status, "partial")
        self.assertEqual(order.process_trade(timestamp=placed + timedelta(milliseconds=13), price=99, quantity=1), 1)
        self.assertEqual(order.process_trade(timestamp=placed + timedelta(milliseconds=14), price=99, quantity=2), 1)
        self.assertEqual(order.status, "filled")
        self.assertEqual(sum(item.quantity for item in order.fills), 3)

    def test_cancelled_and_rejected_limits_never_fill(self):
        placed = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        cancelled = WorkingLimitOrder("o1", "sell", D("100"), 1, placed)
        cancelled.cancel(placed + timedelta(milliseconds=1))
        self.assertEqual(cancelled.process_trade(timestamp=placed + timedelta(seconds=1), price=101, quantity=1), 0)
        rejected = WorkingLimitOrder("o2", "buy", D("100"), 1, placed)
        rejected.reject("risk")
        self.assertEqual(rejected.process_trade(timestamp=placed + timedelta(seconds=1), price=99, quantity=1), 0)

    def test_marketable_fill_uses_the_executable_side_and_adverse_slippage(self):
        self.assertEqual(
            marketable_fill_price(side="buy", bid=100, ask=100.25, tick_size=.25, slippage_ticks=1),
            Decimal("100.50"),
        )
        self.assertEqual(
            marketable_fill_price(side="sell", bid=100, ask=100.25, tick_size=.25, slippage_ticks=1),
            Decimal("99.75"),
        )
        with self.assertRaises(ValueError):
            marketable_fill_price(side="buy", bid=100, ask=100, tick_size=.25, slippage_ticks=0)
        with self.assertRaises(ValueError):
            marketable_fill_price(side="sell", bid=.1, ask=.2, tick_size=.25, slippage_ticks=1)

    def test_size_result_exposes_auditable_cost_decomposition(self):
        result = calculate_position_size(PositionSizeInput(
            "MNQ", D("25000"), D("24000"), None, D("40"), D("400"), D("100"), 0, "normal"
        ), get_account_rules("lucidpro", "evaluation", 25_000))
        self.assertEqual(result.commission_round_trip, Decimal("1.00"))
        self.assertEqual(result.spread_cost_per_contract, Decimal("0.50"))
        self.assertEqual(result.slippage_cost_per_contract, Decimal("0.50"))
        self.assertEqual(result.usable_risk_buffer, Decimal("400.00"))

    def test_open_position_stop_risk_reduces_new_quantity_and_buffer(self):
        rules = get_account_rules("lucidpro", "evaluation", 25_000)
        clear = calculate_position_size(PositionSizeInput(
            "MNQ", D("25000"), D("24000"), None, D("40"), D("400"), D("100"), 0, "normal"
        ), rules)
        committed = calculate_position_size(PositionSizeInput(
            "MNQ", D("25000"), D("24000"), None, D("40"), D("400"), D("100"),
            0, "normal", D("800"),
        ), rules)
        self.assertEqual(clear.final_quantity, 18)
        self.assertEqual(committed.committed_stop_risk, Decimal("800.00"))
        self.assertLess(committed.final_quantity, clear.final_quantity)
        self.assertEqual(committed.usable_risk_buffer, Decimal("100.00"))


class TestMarketDataValidation(unittest.TestCase):
    def test_valid_production_shaped_csv(self):
        content = (
            "timestamp,open,high,low,close,volume,symbol,contract_expiration\n"
            "2026-08-03T13:30:00Z,100,101,99,100.5,12,MNQ,2026-09-18\n"
            "2026-08-03T13:31:00Z,100.5,102,100,101,8,MNQ,2026-09-18\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.duplicate_rows, 0)
        self.assertEqual(report.timezone, "UTC")

    def test_naive_duplicates_impossible_prices_and_symbol_fail(self):
        content = (
            "timestamp,open,high,low,close,volume,symbol\n"
            "2026-08-03 13:30:00,100,99,101,102,-1,NQ\n"
            "2026-08-03 13:30:00,100,101,99,100,1,NQ\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertFalse(report.ok)
        self.assertGreater(report.duplicate_rows, 0)
        self.assertGreater(report.invalid_price_rows, 0)
        self.assertGreater(report.invalid_volume_rows, 0)
        self.assertGreater(report.symbol_mismatch_rows, 0)
        self.assertTrue(any("timezone" in error for error in report.errors))

    def test_gap_is_reported_not_silently_repaired(self):
        content = (
            "timestamp,open,high,low,close,volume\n"
            "2026-08-03T13:30:00Z,100,101,99,100,1\n"
            "2026-08-03T13:35:00Z,100,101,99,100,1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertTrue(report.ok)
        self.assertEqual(report.missing_intervals, 1)
        self.assertTrue(any("gap" in warning for warning in report.warnings))

    def test_mixed_dst_offsets_are_parsed_as_one_utc_timeline(self):
        content = (
            "timestamp,open,high,low,close,volume,symbol,contract_expiration\n"
            "2026-03-06T09:30:00-05:00,100,101,99,100,1,MNQ,2026-03-20\n"
            "2026-03-09T09:30:00-04:00,100,101,99,100,1,MNQ,2026-03-20\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dst.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.timezone, "UTC")
        self.assertEqual(report.session_count, 2)

    def test_friday_evening_is_not_mistaken_for_a_live_futures_session(self):
        content = (
            "timestamp,open,high,low,close,volume,symbol,contract_expiration\n"
            "2026-08-07T22:00:00Z,100,101,99,100,1,MNQ,2026-09-18\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weekend.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertFalse(report.ok)
        self.assertEqual(report.outside_permitted_session_rows, 1)

    def test_expired_contract_and_rollover_are_visible(self):
        content = (
            "timestamp,open,high,low,close,volume,symbol,contract_expiration\n"
            "2026-08-03T13:30:00Z,100,101,99,100,0,MNQ,2026-06-19\n"
            "2026-08-03T13:31:00Z,100,101,99,100,1,MNQ,2026-09-18\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expiry.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertFalse(report.ok)
        self.assertEqual(report.expiration_before_bar_rows, 1)
        self.assertEqual(report.contract_rollovers, 1)
        self.assertEqual(report.zero_volume_rows, 1)

    def test_complete_rth_minute_session_is_not_flagged_incomplete(self):
        start = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        rows = ["timestamp,open,high,low,close,volume,symbol,contract_expiration"]
        for offset in range(390):
            stamp = (start + timedelta(minutes=offset)).isoformat().replace("+00:00", "Z")
            rows.append(f"{stamp},100,101,99,100,1,MNQ,2026-09-18")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "full-rth.csv"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            report = validate_market_data(path, expected_symbol="MNQ")
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.incomplete_rth_sessions, 0)
        self.assertEqual(report.session_count, 1)


class TestEvidenceArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = EvidenceStore(ARTIFACT).load()

    def test_real_source_and_sample_gate_are_visible(self):
        data = self.data
        self.assertEqual(data["status"], "NO_GO")
        self.assertEqual(
            data["verdict"]["decision"],
            "DO_NOT_BUY_OR_TRADE_FROM_THIS_BACKTEST",
        )
        self.assertNotIn("synthetic", data["data"]["source"].lower())
        self.assertGreaterEqual(data["trade_statistics"]["trades"], 200)
        self.assertGreaterEqual(data["horizons"]["45"]["windows"], 10)
        self.assertFalse(data["horizons"]["45"]["windows_overlap"])
        self.assertEqual(data["horizons"]["45"]["window_stride_sessions"], 45)
        self.assertEqual(data["data"]["test_start"], "2024-01-02")
        self.assertTrue(all(row["files"] for row in data["data"]["manifest"]))
        self.assertEqual(len(data["implementation_manifest"]), 4)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in data["implementation_manifest"]))

    def test_rates_partition_every_window(self):
        for horizon in self.data["horizons"].values():
            self.assertEqual(horizon["passes"] + horizon["breaches"] + horizon["unfinished"], horizon["windows"])
            self.assertAlmostEqual(horizon["pass_rate"] + horizon["breach_rate"] + horizon["unfinished_rate"], 1.0, places=5)

    def test_stress_and_monte_carlo_do_not_hide_degradation(self):
        rows = {row["id"]: row for row in self.data["stresses"]}
        self.assertLess(rows["severe"]["pass_rate"], rows["normal"]["pass_rate"])
        self.assertEqual(self.data["monte_carlo"]["seed"], 20260814)
        self.assertEqual(self.data["monte_carlo"]["paths"], 1000)
        self.assertAlmostEqual(
            self.data["monte_carlo"]["pass_rate"] + self.data["monte_carlo"]["breach_rate"] + self.data["monte_carlo"]["unfinished_rate"],
            1.0,
            places=5,
        )

    def test_candidate_comparison_keeps_invalidated_strategy(self):
        rows = {row["id"]: row for row in self.data["candidates"]}
        self.assertEqual(rows["selected_portfolio"]["validation_status"], "NO_GO_PROXY")
        self.assertEqual(rows["original_five_basket"]["validation_status"], "INVALIDATED")
        self.assertIn("future information", rows["original_five_basket"]["reason"])

    def test_costs_and_risk_controls_are_explicit(self):
        stats = self.data["trade_statistics"]
        self.assertGreater(stats["commissions"], 0)
        self.assertGreater(stats["spread_cost"], 0)
        self.assertGreater(stats["slippage_cost"], 0)
        self.assertIn("risk_rejections", self.data["risk_controls"])
        self.assertGreaterEqual(self.data["risk_controls"]["historical_gap_through_stops_in_raw_test_signals"], 1)

    def test_cost_ratio_uses_positive_gross_not_signed_gross(self):
        stats = self.data["trade_statistics"]
        modeled = stats["commissions"] + stats["spread_cost"] + stats["slippage_cost"]
        expected = round(modeled / stats["positive_gross_profit"] * 100, 2)
        self.assertEqual(stats["cost_pct_of_positive_gross"], expected)
        self.assertNotEqual(stats["positive_gross_profit"], stats["gross_before_costs"])

    def test_all_chart_distributions_reconcile(self):
        chart_outcomes = sum(row["count"] for row in self.data["charts"]["outcome_distribution"])
        self.assertEqual(chart_outcomes, self.data["horizons"]["45"]["windows"])
        mc_hist = sum(row["count"] for row in self.data["monte_carlo"]["terminal_profit_histogram"])
        self.assertEqual(mc_hist, self.data["monte_carlo"]["paths"])

    def test_confirmatory_period_is_not_reused_for_parameter_search(self):
        rows = self.data["signal_sensitivity"]
        self.assertEqual(len(rows), 1)
        selected = rows[0]
        self.assertEqual(selected["dimension"], "frozen_three_sleeve_specification")
        self.assertTrue(selected["selected"])
        self.assertIn("not retested", selected["note"])
        self.assertEqual(selected["trades"], self.data["trade_statistics"]["trades"])
        self.assertEqual(selected["expectancy"], self.data["trade_statistics"]["expectancy"])
        self.assertEqual(selected["pass_rate"], self.data["horizons"]["45"]["pass_rate"])
        self.assertEqual([row["year"] for row in self.data["walk_forward"]], [2022, 2023, 2024, 2025, 2026])

    def test_decision_gates_fail_closed_on_proxy_and_small_sample(self):
        gates = {row["id"]: row for row in self.data["verdict"]["validation_gates"]}
        self.assertTrue(gates["minute_open_equity"]["passed"])
        self.assertTrue(gates["non_overlapping_primary_windows"]["passed"])
        for key in (
            "exchange_grade_market_data", "pristine_out_of_sample",
            "observed_execution_costs", "point_in_time_event_filter",
            "decision_precision",
        ):
            self.assertFalse(gates[key]["passed"])
        self.assertFalse(self.data["data"]["integrity"]["decision_grade"])
        self.assertGreater(self.data["risk_controls"]["open_equity_checks"], 0)

    def test_candidates_are_versioned_and_concentration_is_reported(self):
        self.assertTrue(all(row["strategy_version"] for row in self.data["candidates"]))
        concentration = self.data["trade_statistics"]["concentration"]
        for key in (
            "largest_winning_trade_share_pct", "largest_positive_day_share_pct",
            "largest_positive_month_share_pct", "trades_by_year",
        ):
            self.assertIn(key, concentration)


class TestServiceAndJobs(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_marks_scope_mismatch(self):
        service = LucidLabService(ARTIFACT)
        pro = service.snapshot(program="lucidpro", size=25_000)
        daily = service.snapshot(program="luciddaily", size=25_000)
        self.assertTrue(pro["evidence_applies"])
        self.assertFalse(daily["evidence_applies"])
        self.assertIn("do not validate", daily["evidence_scope_note"])

    async def test_job_completes_from_immutable_artifact(self):
        service = LucidLabService(ARTIFACT)
        registry = SimulationRegistry(service)
        started = await registry.start({"execution_preset": "severe", "account_size": 25_000})
        async with asyncio.timeout(1):
            while True:
                state = registry.get(started["id"])
                if state["status"] in {"completed", "error", "cancelled"}:
                    break
                await asyncio.sleep(0)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["source_run_id"], service.evidence.load()["run_id"])
        self.assertTrue(state["result"]["reproducible"])

    async def test_job_can_be_cancelled_before_work_runs(self):
        service = LucidLabService(ARTIFACT)
        registry = SimulationRegistry(service)
        started = await registry.start({"execution_preset": "normal", "account_size": 25_000})
        state = registry.cancel(started["id"])
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["cancelled"])


class TestPageAndRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.add_routes(lab_web.routes())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_direct_route_and_refresh_return_complete_page(self):
        for _ in range(2):
            response = await self.client.get("/lucid-lab")
            self.assertEqual(response.status, 200)
            self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
            text = await response.text()
            self.assertIn("Lucid Strategy Lab", text)
            self.assertIn("Position-size calculator", text)
            self.assertIn("Forward paper account", text)
            self.assertIn("no live order routing", text)

    async def test_snapshot_and_calculator_endpoints(self):
        response = await self.client.get("/api/lucid-lab/snapshot?program=lucidpro&size=25000")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["evidence_applies"])
        self.assertEqual(data["account"]["rule_metadata"]["profit_target"]["account_size"], 25_000)
        self.assertIn("lucidtrading.com", data["account"]["rule_metadata"]["profit_target"]["source"]["url"])
        response = await self.client.post("/api/lucid-lab/position-size", json={
            "program": "lucidpro", "stage": "evaluation", "account_size": 25000,
            "instrument": "MNQ", "current_balance": 25000, "drawdown_floor": 24000,
            "stop_ticks": 40, "risk_budget": 400, "safety_reserve": 100,
            "execution_preset": "normal",
        })
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result["result"]["final_quantity"], 18)
        self.assertEqual(result["result"]["commission_round_trip"], "1.00")
        self.assertEqual(result["result"]["spread_cost_per_contract"], "0.50")
        self.assertIn("Committed stop risk defaulted", " ".join(result["result"]["warnings"]))

    async def test_paper_endpoint_fails_safe_when_runtime_is_not_attached(self):
        previous = lab_web.PAPER_BOT
        lab_web.PAPER_BOT = None
        try:
            response = await self.client.get("/api/lucid-lab/paper/state")
            self.assertEqual(response.status, 200)
            data = await response.json()
            self.assertFalse(data["running"])
            self.assertTrue(data["paper_only"])
            self.assertFalse(data["live_order_routing"])
        finally:
            lab_web.PAPER_BOT = previous

    async def test_bad_rule_selection_returns_visible_error(self):
        response = await self.client.get("/api/lucid-lab/snapshot?program=lucidblack&size=150000")
        self.assertEqual(response.status, 400)
        self.assertIn("no verified 150K", (await response.json())["error"])

    def test_homepage_navigation_is_present(self):
        dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn('href="/lucid-lab"', dashboard)
        self.assertIn("*lucid_lab_web.routes()", dashboard)

    def test_ui_has_required_states_charts_and_responsive_rules(self):
        for text in (
            "Loading verified rules", "No file selected", "notice error",
            "equity-chart", "drawdown-chart", "rolling-chart", "duration-bars",
            "outcome-bars", "terminal-bars", "cost-bars", "@media(max-width:820px)",
            "prefers-reduced-motion", "Rule-compliance timeline", "Strategy comparison",
            "signal-sensitivity", "selector-current", "selector-remaining",
            "Costs / positive gross", "largest_profitable_day", "permitted_contracts",
            "rule_metadata", "terminal_profit_histogram",
        ):
            self.assertIn(text, LUCID_LAB_HTML)
        self.assertNotIn("guaranteed profit", LUCID_LAB_HTML.lower())
        self.assertNotIn("proven profitable", LUCID_LAB_HTML.lower())
        self.assertNotIn("Wilson ", LUCID_LAB_HTML)
        self.assertIn("Exact small-sample interval", LUCID_LAB_HTML)
        self.assertIn("Load stored scenario", LUCID_LAB_HTML)
        self.assertIn("every open position marked each minute", LUCID_LAB_HTML)
        self.assertIn("DO_NOT_BUY_OR_TRADE_FROM_THIS_BACKTEST", ARTIFACT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
