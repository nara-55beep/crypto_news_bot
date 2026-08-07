"""
Runtime invariants for the Lucid paper bots.

The historical parity verifier proves signal/session parity. This file covers
live-only behavior: feed gating, fresh-bar gating, exact-R sizing/accounting,
and catch-up management after a restart or websocket pause.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lucid_pass_paper as live
import lucid_continuous_paper as cont
import config
from tools import lucid_bridge_receiver


class RuntimeBot(live.LucidPassPaperBot):
    def __init__(self):
        self.enabled = True
        self.balance = live.START_BALANCE
        self.peak = live.START_BALANCE
        self.locked = False
        self.floor = live.FLOOR_LOCK
        self.day_key = "verify"
        self.day_pnl = 0.0
        self.passed = False
        self.failed = False
        self.daily_stopped_day = ""
        self.pos = {}
        self.fired_keys = set()
        self.warning_keys = set()
        self.history = []
        self.log = []
        self.prices = {}
        self.setups = {}
        self.status = ""
        self.data_error = ""
        self.telegram_enabled = False
        self._telegram_client = None
        self._telegram_target = "me"
        self._telegram_bot_token = ""
        self._telegram_chat_id = ""
        self._last_alert_error = ""
        self._alert_queue = None
        self._alert_worker_task = None
        self._df = {}
        self._last_bar_ts = {}
        self._last_bar_sig = {}
        self._primed_keys = set()
        self.live_feed_status = ""
        self.feed_details = []
        self._enforce_live_open_guard = False

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"kind": kind, "msg": msg})

    def _alert(self, text: str):
        return None

    def _save(self):
        return None


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_feed_gate() -> None:
    cases = {
        "TradingView websocket (delayed_streaming_600)": True,
        "TradingView websocket (feed_mode_unknown)": True,
        "ES_VWAP3 reconnecting: RuntimeError": True,
        "TradingView websocket stale for 20s": True,
        "TradingView websocket (permission_denied)": True,
        "TradingView websocket (permission_denied, streaming)": True,
        "TradingView websocket (unauthorized_streaming)": True,
        "TradingView websocket": True,
        "TradingView websocket (realtime_streaming)": False,
        "TradingView websocket (streaming)": False,
    }
    for status, should_block in cases.items():
        reason = live._lucid_feed_block_reason(status)
        _assert(bool(reason) == should_block, f"feed gate mismatch for {status!r}: {reason!r}")
    combined_unknown = live._combine_tradingview_statuses({
        "ES_VWAP3": "TradingView websocket (realtime_streaming)",
        "NQ_VWAP3": "TradingView websocket (feed_mode_unknown)",
    })
    _assert(
        not combined_unknown.startswith("TradingView websocket (TradingView websocket"),
        f"combined status should not double-wrap feed label: {combined_unknown}",
    )
    _assert("feed_mode_unknown" in combined_unknown, f"combined status lost unknown mode: {combined_unknown}")
    _assert(
        bool(live._lucid_feed_block_reason(combined_unknown)),
        f"combined unknown status should block: {combined_unknown}",
    )
    combined_realtime = live._combine_tradingview_statuses({
        "ES_VWAP3": "TradingView websocket (realtime_streaming)",
        "NQ_VWAP3": "TradingView websocket (streaming)",
        "CL_VWAP5": "TradingView websocket (realtime_streaming)",
    })
    _assert(
        combined_realtime == "TradingView websocket (realtime_streaming)",
        f"all-realtime statuses should remain explicitly realtime: {combined_realtime}",
    )
    _assert(
        live._lucid_feed_block_reason(combined_realtime) == "",
        f"combined realtime status should not block: {combined_realtime}",
    )
    combined_permission = live._combine_tradingview_statuses({
        "ES_VWAP3": "TradingView websocket (permission_denied)",
        "NQ_VWAP3": "TradingView websocket (realtime_streaming)",
    })
    _assert("permission_denied" in combined_permission, f"combined status should preserve bad mode: {combined_permission}")
    _assert(
        bool(live._lucid_feed_block_reason(combined_permission)),
        f"unrecognized TradingView mode should block: {combined_permission}",
    )
    combined_permission_streaming = live._combine_tradingview_statuses({
        "ES_VWAP3": "TradingView websocket (permission_denied, streaming)",
        "NQ_VWAP3": "TradingView websocket (realtime_streaming)",
    })
    _assert(
        "permission_denied" in combined_permission_streaming,
        f"combined status must not hide permission denial behind streaming: {combined_permission_streaming}",
    )
    _assert(
        bool(live._lucid_feed_block_reason(combined_permission_streaming)),
        f"combined permission+streaming status should block: {combined_permission_streaming}",
    )
    combined_reconnecting = live._combine_tradingview_statuses({
        "ES_VWAP3": "ES_VWAP3 reconnecting: RuntimeError",
        "NQ_VWAP3": "TradingView websocket (realtime_streaming)",
    })
    reason = live._lucid_feed_block_reason(combined_reconnecting)
    _assert("reconnecting/stale" in reason, f"reconnecting should win over connecting substring: {reason}")
    _assert(
        bool(live._lucid_fallback_block_reason(True)),
        "Yahoo fallback must be blocked when realtime feed is required",
    )
    _assert(
        live._lucid_fallback_block_reason(False) == "",
        "Yahoo fallback may only be unblocked when realtime requirement is explicitly disabled",
    )
    print("feed gate ok")


def verify_lucid_realtime_config() -> None:
    live_source = str(getattr(config, "LUCID_LIVE_SOURCE", ""))
    _assert(
        live_source == "dukascopy",
        f"Lucid exact-source mode should default to dukascopy, got {live_source!r}",
    )
    _assert(
        getattr(config, "LUCID_REQUIRE_BACKTEST_SOURCE_MATCH", True) is True,
        "Lucid must require the live candle source to match the saved 36/36 backtest source",
    )
    _assert(
        float(getattr(config, "LUCID_DUKASCOPY_POLL_SEC", 0)) > 0,
        "Lucid Dukascopy polling interval must be positive",
    )
    _assert(
        int(getattr(config, "LUCID_DUKASCOPY_CATCHUP_DAYS", 0)) >= 1,
        "Lucid Dukascopy catch-up window must cover at least one day",
    )
    _assert(
        float(getattr(config, "LUCID_LOCAL_BRIDGE_POLL_SEC", 0)) > 0,
        "Lucid local bridge polling interval must be positive",
    )
    print("Lucid exact-source realtime config ok")


def verify_lucid_strategy_identity() -> None:
    _assert(
        live.STRATEGY_VERSION == "lucid_5basket_r200_realtime_guard_v18",
        f"Lucid strategy version changed: {live.STRATEGY_VERSION}",
    )
    _assert(
        live.STRATEGY_FINGERPRINT == "905588aa24d45fab",
        f"Lucid strategy fingerprint changed: {live.STRATEGY_FINGERPRINT}",
    )
    _assert(
        live.STRATEGY_FINGERPRINT == live._lucid_strategy_fingerprint(),
        "Lucid strategy fingerprint must be generated from the live strategy contract",
    )
    expected_order = [
        "ES_VWAP3",
        "NQ_VWAP3",
        "CL_VWAP5",
        "NQ_TURTLE30",
        "CL_NR7_30",
    ]
    _assert(list(live.COMPONENTS) == expected_order, f"Lucid basket components changed: {list(live.COMPONENTS)}")
    expected = {
        "ES_VWAP3": ("ES=F", "vwap", "3min", 180),
        "NQ_VWAP3": ("NQ=F", "vwap", "3min", 180),
        "CL_VWAP5": ("CL=F", "vwap", "5min", 300),
        "NQ_TURTLE30": ("NQ=F", "turtle", "30min", 1800),
        "CL_NR7_30": ("CL=F", "nr7", "30min", 1800),
    }
    for key, (symbol, kind, resample, bar_sec) in expected.items():
        c = live.COMPONENTS[key]
        _assert(c["symbol"] == symbol, f"{key} symbol changed: {c}")
        _assert(c["kind"] == kind, f"{key} kind changed: {c}")
        _assert(c.get("resample") == resample, f"{key} resample changed: {c}")
        _assert(int(c["bar_sec"]) == bar_sec, f"{key} bar_sec changed: {c}")
    _assert(live.START_BALANCE == 50_000.0, f"Lucid start balance changed: {live.START_BALANCE}")
    _assert(live.TARGET_BALANCE == 53_000.0, f"Lucid target changed: {live.TARGET_BALANCE}")
    _assert(live.MAX_DRAWDOWN == 2_000.0, f"Lucid drawdown changed: {live.MAX_DRAWDOWN}")
    _assert(live.FLOOR_LOCK == 48_000.0, f"Lucid floor changed: {live.FLOOR_LOCK}")
    _assert(live.DAILY_LOSS_LIMIT == 1_200.0, f"Lucid daily loss changed: {live.DAILY_LOSS_LIMIT}")
    _assert(live.RISK_USD == 200.0, f"Lucid risk changed: {live.RISK_USD}")
    _assert(live.MAX_MICROS is None, f"Lucid exact virtual sizing changed: {live.MAX_MICROS}")
    _assert(live.COMMISSION_RT == 0.50, f"Lucid commission model changed: {live.COMMISSION_RT}")
    _assert(live.SLIP_TICKS == 0.0, f"Lucid slippage model changed: {live.SLIP_TICKS}")
    _assert(live.VWAP_K == 2.5, f"Lucid VWAP band changed: {live.VWAP_K}")
    _assert(live.VWAP_MIN_BARS == 15, f"Lucid VWAP warmup changed: {live.VWAP_MIN_BARS}")
    _assert(live.VWAP_WARN_SIGMA == 0.20, f"Lucid VWAP warning band changed: {live.VWAP_WARN_SIGMA}")
    _assert(live.TURTLE_LOOKBACK == 10 and live.TURTLE_RECENCY == 4, "Lucid Turtle parameters changed")
    _assert(live.TURTLE_BUF_TICKS == 8, f"Lucid Turtle buffer changed: {live.TURTLE_BUF_TICKS}")
    _assert(live.NR7_WARN_TICKS == 8, f"Lucid NR7 warning band changed: {live.NR7_WARN_TICKS}")
    _assert(live.ENTRY_BAR_LAG_GRACE_SEC == 45, f"Lucid live bar grace changed: {live.ENTRY_BAR_LAG_GRACE_SEC}")
    _assert(live.BACKTEST_SESSION_START_UTC == 13 * 60, "Lucid session start changed")
    _assert(live.BACKTEST_SESSION_END_UTC == 21 * 60, "Lucid session end changed")
    _assert(live.MARKETS["ES=F"] == (5.0, 0.25, "MES"), f"ES micro model changed: {live.MARKETS['ES=F']}")
    _assert(live.MARKETS["NQ=F"] == (2.0, 0.25, "MNQ"), f"NQ micro model changed: {live.MARKETS['NQ=F']}")
    _assert(live.MARKETS["CL=F"] == (100.0, 0.01, "MCL"), f"CL micro model changed: {live.MARKETS['CL=F']}")
    _assert(live.MIN_PRIOR_SESSIONS["NQ_TURTLE30"] == 14, f"Turtle history gate changed: {live.MIN_PRIOR_SESSIONS}")
    _assert(live.MIN_PRIOR_SESSIONS["CL_NR7_30"] == 7, f"NR7 history gate changed: {live.MIN_PRIOR_SESSIONS}")
    print("Lucid strategy identity ok")


def verify_lucid_source_identity() -> None:
    _assert(
        live.BACKTEST_FEED_FAMILY == "dukascopy_tick_proxy",
        f"Lucid backtest feed family changed: {live.BACKTEST_FEED_FAMILY}",
    )
    _assert(
        live.BACKTEST_SOURCE_SYMBOLS == {
            "ES=F": "USA500IDXUSD",
            "NQ=F": "USATECHIDXUSD",
            "CL=F": "LIGHTCMDUSD",
        },
        f"Lucid backtest source symbols changed: {live.BACKTEST_SOURCE_SYMBOLS}",
    )
    _assert(
        live.TV_SYMBOLS == {
            "ES=F": "CME_MINI:ES1!",
            "NQ=F": "CME_MINI:NQ1!",
            "CL=F": "NYMEX:CL1!",
        },
        f"Lucid TradingView live symbols changed: {live.TV_SYMBOLS}",
    )
    reason = live._lucid_source_block_reason(
        "TradingView websocket (realtime_streaming)",
        require_match=True,
    )
    _assert("Dukascopy" in reason and "TradingView CME/NYMEX" in reason, f"CME source mismatch should block: {reason}")
    _assert(
        live._lucid_source_block_reason(
            "Dukascopy exact-source polling (ES_VWAP3 20:15; NQ_VWAP3 20:15; CL_VWAP5 21:00)",
            require_match=True,
        ) == "",
        "exact Dukascopy source should not be blocked by source identity guard",
    )
    bridge_reason = live._lucid_source_block_reason(
        "Local Lucid live bridge (unverified_source; ES_VWAP3 13:51)",
        require_match=True,
    )
    _assert(
        "local bridge" in bridge_reason.lower() and live.BACKTEST_FEED_FAMILY in bridge_reason,
        f"unverified local bridge should block: {bridge_reason}",
    )
    _assert(
        live._lucid_source_block_reason(
            f"Local Lucid live bridge ({live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51)",
            require_match=True,
        ) == "",
        "declared exact local bridge should pass source identity guard",
    )
    spoofed_bridge = live._lucid_source_block_reason(
        f"Local Lucid live bridge (fake_{live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51)",
        require_match=True,
    )
    _assert(
        "local bridge must declare" in spoofed_bridge,
        f"substring-spoofed local bridge family should block: {spoofed_bridge}",
    )
    _assert(
        live._local_bridge_status_family(
            f"Local Lucid live bridge ({live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51)"
        ) == live.BACKTEST_FEED_FAMILY,
        "local bridge status family parser should extract exact family",
    )
    _assert(
        live._lucid_source_block_reason("TradingView websocket (realtime_streaming)", require_match=False) == "",
        "source identity guard should be disableable only by explicit config/test override",
    )
    print("Lucid source identity guard ok")


def verify_local_bridge_loader_and_source_status() -> None:
    old_dir = getattr(config, "LUCID_LOCAL_BRIDGE_DIR", "")
    old_prefix = getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "")
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    old_seed = live._duka_load_seed
    old_cache = dict(live._LOCAL_BRIDGE_SEED_CACHE)
    try:
        with tempfile.TemporaryDirectory() as td:
            config.LUCID_LOCAL_BRIDGE_DIR = td
            config.LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
            config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = live.BACKTEST_FEED_FAMILY
            live._LOCAL_BRIDGE_SEED_CACHE.clear()
            live._duka_load_seed = lambda market: live._duka_empty_frame()
            base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
            for market, px in {"es": 100.0, "nq": 200.0, "cl": 70.0}.items():
                rows = []
                for minute in range(60):
                    dt = base + pd.Timedelta(minutes=minute)
                    close = px + minute * 0.01
                    rows.append({
                        "dt_utc": dt.isoformat(),
                        "open": close,
                        "high": close + 0.05,
                        "low": close - 0.05,
                        "close": close,
                        "volume": 1.0,
                    })
                pd.DataFrame(rows).to_csv(pathlib.Path(td) / f"lucid_live_bridge_{market}_1m.csv", index=False)

            frames, status = live._load_local_bridge_component_data_all()
            _assert(live.LOCAL_BRIDGE_FEED_STATUS in status, f"bridge status missing label: {status}")
            _assert(live.BACKTEST_FEED_FAMILY in status, f"bridge status missing exact family: {status}")
            _assert(live._lucid_source_verified(status), f"declared exact bridge should verify source: {status}")
            _assert(live._lucid_source_block_reason(status, require_match=True) == "", f"exact bridge should not block: {status}")
            _assert(not frames["ES_VWAP3"].empty, "bridge should build ES component candles")
            _assert(not frames["NQ_TURTLE30"].empty, "bridge should build NQ 30m component candles")
    finally:
        config.LUCID_LOCAL_BRIDGE_DIR = old_dir
        config.LUCID_LOCAL_BRIDGE_PREFIX = old_prefix
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
        live._duka_load_seed = old_seed
        live._LOCAL_BRIDGE_SEED_CACHE.clear()
        live._LOCAL_BRIDGE_SEED_CACHE.update(old_cache)
    print("local bridge loader/source status ok")


def verify_local_bridge_missing_files_block() -> None:
    old_dir = getattr(config, "LUCID_LOCAL_BRIDGE_DIR", "")
    old_prefix = getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "")
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    old_seed = live._duka_load_seed
    old_cache = dict(live._LOCAL_BRIDGE_SEED_CACHE)
    try:
        with tempfile.TemporaryDirectory() as td:
            config.LUCID_LOCAL_BRIDGE_DIR = td
            config.LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
            config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = live.BACKTEST_FEED_FAMILY
            live._LOCAL_BRIDGE_SEED_CACHE.clear()
            live._duka_load_seed = lambda market: live._duka_empty_frame()
            frames, status = live._load_local_bridge_component_data_all()
            _assert("missing_files=es,nq,cl" in status, f"bridge status should report missing files: {status}")
            _assert(live._lucid_source_block_reason(status, require_match=True) == "", "declared exact bridge source should pass identity guard")
            reason = live._lucid_local_bridge_block_reason(status)
            _assert("missing producer CSV files" in reason and "es,nq,cl" in reason, f"missing bridge files should block: {reason}")
            _assert(all(d.empty for d in frames.values()), "missing bridge files with empty seeds should produce empty component frames")
    finally:
        config.LUCID_LOCAL_BRIDGE_DIR = old_dir
        config.LUCID_LOCAL_BRIDGE_PREFIX = old_prefix
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
        live._duka_load_seed = old_seed
        live._LOCAL_BRIDGE_SEED_CACHE.clear()
        live._LOCAL_BRIDGE_SEED_CACHE.update(old_cache)
    print("local bridge missing-files block ok")


def verify_local_bridge_receiver_writes_atomic_csvs() -> None:
    old_dir = getattr(config, "LUCID_LOCAL_BRIDGE_DIR", "")
    old_prefix = getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "")
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    try:
        with tempfile.TemporaryDirectory() as td:
            config.LUCID_LOCAL_BRIDGE_DIR = td
            config.LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
            config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = live.BACKTEST_FEED_FAMILY
            store = lucid_bridge_receiver.BridgeStore()
            bar_status = store.put_bar("es", {
                "dt_utc": "2026-07-06T13:00:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 2.0,
            })
            _assert(bar_status["markets"]["es"]["rows"] == 1, f"bar write status mismatch: {bar_status}")
            ready_status = store.status(include_ready=True)
            _assert(ready_status["ready"] is False, f"one-market bridge must not be ready: {ready_status}")
            _assert(
                any("missing producer CSV files" in str(p) for p in ready_status["problems"]),
                f"one-market bridge readiness should report missing markets: {ready_status}",
            )
            store.put_tick("es", {"dt_utc": "2026-07-06T13:01:05Z", "price": 101.0, "volume": 1.0})
            store.put_tick("es", {"dt_utc": "2026-07-06T13:01:25Z", "price": 102.0, "volume": 3.0})
            path = pathlib.Path(td) / "lucid_live_bridge_es_1m.csv"
            _assert(path.exists(), f"bridge receiver did not write {path}")
            _assert(not pathlib.Path(str(path) + ".tmp").exists(), "bridge receiver should atomically replace tmp file")
            df = live._duka_normalize_frame(pd.read_csv(path))
            _assert(len(df) == 2, f"bridge receiver should have two bars: {df}")
            last = df.iloc[-1]
            _assert(float(last["open"]) == 101.0, f"tick bar open mismatch: {last}")
            _assert(float(last["high"]) == 102.0, f"tick bar high mismatch: {last}")
            _assert(float(last["low"]) == 101.0, f"tick bar low mismatch: {last}")
            _assert(float(last["close"]) == 102.0, f"tick bar close mismatch: {last}")
            _assert(float(last["volume"]) == 4.0, f"tick bar volume mismatch: {last}")
            try:
                store.put_bar("es", {
                    "dt_utc": "2026-07-06T13:02:00Z",
                    "open": 100.0,
                    "high": 99.0,
                    "low": 98.0,
                    "close": 100.5,
                    "volume": 1.0,
                })
                raise AssertionError("invalid OHLC bridge bar should be rejected")
            except ValueError as e:
                _assert("ohlc" in str(e).lower(), f"unexpected invalid OHLC error: {e}")
            try:
                store.put_tick("es", {
                    "dt_utc": (pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=5)).isoformat(),
                    "price": 100.0,
                    "volume": 1.0,
                })
                raise AssertionError("far-future bridge tick should be rejected")
            except ValueError as e:
                _assert("future" in str(e).lower(), f"unexpected future tick error: {e}")
    finally:
        config.LUCID_LOCAL_BRIDGE_DIR = old_dir
        config.LUCID_LOCAL_BRIDGE_PREFIX = old_prefix
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
    print("local bridge receiver atomic csv writer ok")


def verify_local_bridge_ready_rejects_partial_stale_warning() -> None:
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    old_load = lucid_bridge_receiver.lucid._load_local_bridge_component_data_all
    old_history = lucid_bridge_receiver.lucid._lucid_history_block_reason
    old_fresh_block = lucid_bridge_receiver.lucid._lucid_exact_source_freshness_block
    old_details = lucid_bridge_receiver.lucid._lucid_exact_source_freshness_details
    old_warning = lucid_bridge_receiver.lucid._lucid_exact_source_freshness_warning
    try:
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = live.BACKTEST_FEED_FAMILY
        source_status = (
            f"{live.LOCAL_BRIDGE_FEED_STATUS} "
            f"({live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51; NQ_VWAP3 13:51; CL_VWAP5 13:55)"
        )
        stale_warning = (
            "Some exact Dukascopy components are stale; stale components are blocked "
            "but fresh components still scan: ES_VWAP3 latest closed 13:51 UTC"
        )
        lucid_bridge_receiver.lucid._load_local_bridge_component_data_all = lambda: ({}, source_status)
        lucid_bridge_receiver.lucid._lucid_history_block_reason = lambda frames: ""
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_block = lambda frames: ""
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_details = lambda frames: [{
            "key": "ES_VWAP3",
            "stale": True,
            "state": "stale",
            "latest_closed_utc": "13:51 UTC",
            "lag_sec": 999,
        }]
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_warning = lambda details: stale_warning

        store = lucid_bridge_receiver.BridgeStore()
        ready, report = store.ready_report()
        _assert(ready is False, f"partial stale bridge must not be ready: {report}")
        _assert(report["exact_realtime_ready"] is False, f"partial stale bridge must not be exact realtime: {report}")
        _assert(stale_warning in report["problems"], f"partial stale warning should be listed as a problem: {report}")
        status = store.status(include_ready=True)
        _assert(status["ready"] is False and status["ok"] is False, f"/ready status must be false: {status}")
    finally:
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
        lucid_bridge_receiver.lucid._load_local_bridge_component_data_all = old_load
        lucid_bridge_receiver.lucid._lucid_history_block_reason = old_history
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_block = old_fresh_block
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_details = old_details
        lucid_bridge_receiver.lucid._lucid_exact_source_freshness_warning = old_warning
    print("local bridge ready partial-stale guard ok")


def verify_local_bridge_invalid_files_block() -> None:
    old_dir = getattr(config, "LUCID_LOCAL_BRIDGE_DIR", "")
    old_prefix = getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "")
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    old_seed = live._duka_load_seed
    old_cache = dict(live._LOCAL_BRIDGE_SEED_CACHE)
    try:
        with tempfile.TemporaryDirectory() as td:
            config.LUCID_LOCAL_BRIDGE_DIR = td
            config.LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
            config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = live.BACKTEST_FEED_FAMILY
            live._LOCAL_BRIDGE_SEED_CACHE.clear()
            live._duka_load_seed = lambda market: live._duka_empty_frame()
            base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
            valid = {
                "dt_utc": base.isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            }
            for market in ("nq", "cl"):
                pd.DataFrame([valid]).to_csv(pathlib.Path(td) / f"lucid_live_bridge_{market}_1m.csv", index=False)
            invalid = dict(valid)
            invalid["high"] = 99.5
            pd.DataFrame([invalid]).to_csv(pathlib.Path(td) / "lucid_live_bridge_es_1m.csv", index=False)

            frames, status = live._load_local_bridge_component_data_all()
            _assert("invalid_files=es" in status, f"bridge status should report invalid ES file: {status}")
            reason = live._lucid_local_bridge_block_reason(status)
            _assert("invalid producer CSV data" in reason and "es" in reason, f"invalid bridge files should block: {reason}")
            _assert(frames["ES_VWAP3"].empty, "invalid bridge with empty seed should not provide usable ES candles")
    finally:
        config.LUCID_LOCAL_BRIDGE_DIR = old_dir
        config.LUCID_LOCAL_BRIDGE_PREFIX = old_prefix
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
        live._duka_load_seed = old_seed
        live._LOCAL_BRIDGE_SEED_CACHE.clear()
        live._LOCAL_BRIDGE_SEED_CACHE.update(old_cache)
    print("local bridge invalid-file block ok")


def verify_state_source_match_requires_verified_exact_source() -> None:
    bot = RuntimeBot()
    bot.live_feed_status = "starting..."
    s = bot.state()
    _assert(s["strategy_version"] == live.STRATEGY_VERSION, f"state version missing: {s}")
    _assert(s["strategy_fingerprint"] == live.STRATEGY_FINGERPRINT, f"state fingerprint missing: {s}")
    _assert(s["source_match"] is False, f"starting feed should not report source_match true: {s}")
    _assert(s["live_feed_family"] == "starting...", f"starting feed family should remain unverified: {s}")
    _assert(s["exact_realtime_ready"] is False, f"starting feed should not report exact realtime ready: {s}")

    bot.live_feed_status = "Dukascopy exact-source polling error"
    s = bot.state()
    _assert(s["source_match"] is False, f"Dukascopy error should not report verified source_match: {s}")
    _assert(s["exact_realtime_ready"] is False, f"Dukascopy error should not report exact realtime ready: {s}")

    bot.live_feed_status = "Dukascopy exact-source polling (ES_VWAP3 20:15; NQ_VWAP3 20:15; CL_VWAP5 21:00)"
    s = bot.state()
    _assert(s["source_match"] is True, f"exact Dukascopy polling should report source_match: {s}")
    _assert(
        s["live_feed_family"] == live.BACKTEST_FEED_FAMILY,
        f"exact Dukascopy polling should report backtest feed family: {s}",
    )
    _assert(s["exact_realtime_ready"] is False, f"public polling should not report exact realtime ready: {s}")
    _assert(
        "public Dukascopy polling" in s["exact_realtime_status"],
        f"public polling should explain it is not local realtime bridge: {s}",
    )
    bot.live_feed_status = f"Local Lucid live bridge ({live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51; missing_files=es)"
    s = bot.state()
    _assert(s["source_match"] is False, f"missing bridge files should not report source_match: {s}")
    _assert(s["exact_realtime_ready"] is False, f"missing bridge files should not report exact realtime ready: {s}")
    bot.live_feed_status = f"Local Lucid live bridge (fake_{live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51)"
    s = bot.state()
    _assert(s["source_match"] is False, f"spoofed bridge family should not report source_match: {s}")
    _assert(s["exact_realtime_ready"] is False, f"spoofed bridge family should not report exact realtime ready: {s}")
    bot.live_feed_status = f"Local Lucid live bridge ({live.BACKTEST_FEED_FAMILY}; ES_VWAP3 13:51)"
    s = bot.state()
    _assert(s["source_match"] is True, f"ready exact local bridge should report source_match: {s}")
    _assert(s["exact_realtime_ready"] is True, f"ready exact local bridge should report exact realtime ready: {s}")
    bot.data_error = "Some exact Dukascopy components are stale: ES_VWAP3"
    s = bot.state()
    _assert(s["source_match"] is True, f"stale exact local bridge still has matching source identity: {s}")
    _assert(s["exact_realtime_ready"] is False, f"stale exact local bridge should not report exact realtime ready: {s}")
    bot.data_error = ""
    print("state source-match verification ok")


def verify_exact_realtime_entry_gate_blocks_public_polling() -> None:
    old_required = getattr(config, "LUCID_REQUIRE_EXACT_REALTIME_ENTRY", True)
    try:
        config.LUCID_REQUIRE_EXACT_REALTIME_ENTRY = True
        now = pd.Timestamp("2026-07-06 13:03:10+00:00")
        cur = pd.Series({
            "dt_utc": pd.Timestamp("2026-07-06 13:00:00+00:00"),
            "dt_ny": pd.Timestamp("2026-07-06 13:00:00+00:00").tz_convert(live.NY),
            "day": pd.Timestamp("2026-07-06 13:00:00+00:00").tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        })

        class GateBot(RuntimeBot):
            def __init__(self):
                super().__init__()
                self._enforce_live_open_guard = True
                self.live_feed_status = "Dukascopy exact-source polling (ES_VWAP3 13:03)"
                self.realtime_entry_ready = False
                self.realtime_entry_status = "public polling is not the realtime local bridge"

            def _open_guard_now_utc(self):
                return now

        bot = GateBot()
        _assert(not bot._can_open_key("ES_VWAP3", cur), "public polling must not pass exact realtime entry gate")
        _assert(
            bot.setups["ES_VWAP3"]["status"] == "blocked - exact realtime bridge not ready",
            f"blocked setup status mismatch: {bot.setups}",
        )
        sig = {
            "key": "ES_VWAP3",
            "symbol": "ES=F",
            "label": "MES",
            "strat": "gate fixture",
            "side": "long",
            "entry": 100.5,
            "stop": 99.5,
            "target": 102.5,
            "note": "verify realtime gate",
        }
        bot._open(sig, cur)
        _assert("ES_VWAP3" not in bot.pos, "public polling must not open even with a valid signal")

        bot.realtime_entry_ready = True
        bot.realtime_entry_status = "Local Dukascopy/JForex bridge is ready."
        _assert(bot._can_open_key("ES_VWAP3", cur), "ready local bridge should pass exact realtime entry gate")
        bot._open(sig, cur)
        _assert("ES_VWAP3" in bot.pos, "ready local bridge should allow valid signal open")

        relaxed = GateBot()
        config.LUCID_REQUIRE_EXACT_REALTIME_ENTRY = False
        _assert(relaxed._can_open_key("ES_VWAP3", cur), "explicit delayed-data experiment should be able to relax entry gate")
    finally:
        config.LUCID_REQUIRE_EXACT_REALTIME_ENTRY = old_required
    print("exact realtime entry gate ok")


def verify_warning_alerts_blocked_without_exact_realtime_entry() -> None:
    old_required = getattr(config, "LUCID_REQUIRE_EXACT_REALTIME_ENTRY", True)
    try:
        config.LUCID_REQUIRE_EXACT_REALTIME_ENTRY = True

        class AlertCaptureBot(RuntimeBot):
            def __init__(self):
                super().__init__()
                self._enforce_live_open_guard = True
                self.day_key = "2026-07-06"
                self.telegram_enabled = True
                self.alerts = []
                self.realtime_entry_ready = False
                self.realtime_entry_status = "public polling is not the realtime local bridge"

            def _alert(self, text: str):
                self.alerts.append(text)

        bot = AlertCaptureBot()
        bot._warn_signal("ES_VWAP3", "long", 100.0, 99.0, 102.0, "verify warning blocked")
        _assert(not bot.alerts, f"warning must not alert without exact realtime entry: {bot.alerts}")
        _assert(not bot.warning_keys, f"blocked realtime warning should not consume warning keys: {bot.warning_keys}")

        bot.realtime_entry_ready = True
        bot._warn_signal("ES_VWAP3", "long", 100.0, 99.0, 102.0, "verify warning allowed")
        _assert(len(bot.alerts) == 1, f"warning should alert after exact realtime becomes ready: {bot.alerts}")
        _assert(bot.warning_keys, "allowed warning should consume warning key")
        live._GLOBAL_WARNING_ALERTS.clear()
    finally:
        config.LUCID_REQUIRE_EXACT_REALTIME_ENTRY = old_required
    print("exact realtime warning gate ok")


def verify_dukascopy_exact_source_helpers() -> None:
    base = int(pd.Timestamp("2026-07-06 13:00:00", tz="UTC").timestamp() * 1000)
    rows = [
        (base + 1_000, 100_100.0, 100_000.0, 2.0),
        (base + 20_000, 100_300.0, 100_100.0, 3.0),
        (base + 61_000, 100_200.0, 100_000.0, 4.0),
    ]
    bars = live._duka_rows_to_1m(rows)
    _assert(len(bars) == 2, f"Dukascopy tick rows should resample to 2 one-minute bars: {bars}")
    _assert(float(bars.iloc[0]["open"]) == 100.05, f"first mid open mismatch: {bars.iloc[0]}")
    _assert(float(bars.iloc[0]["high"]) == 100.2, f"first mid high mismatch: {bars.iloc[0]}")
    _assert(float(bars.iloc[0]["low"]) == 100.05, f"first mid low mismatch: {bars.iloc[0]}")
    _assert(float(bars.iloc[0]["close"]) == 100.2, f"first mid close mismatch: {bars.iloc[0]}")
    _assert(float(bars.iloc[0]["volume"]) == 5.0, f"first tick-volume mismatch: {bars.iloc[0]}")

    frames = {}
    now = pd.Timestamp("2026-07-06 13:06:30", tz="UTC")
    for key, c in live.COMPONENTS.items():
        bar_sec = int(c["bar_sec"])
        latest = now.floor(f"{bar_sec}s") - pd.Timedelta(seconds=bar_sec)
        frames[key] = pd.DataFrame([{
            "dt_utc": latest,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
            "dt_ny": latest.tz_convert(live.NY),
            "day": latest.tz_convert(live.NY).date(),
        }])
    _assert(
        live._lucid_exact_source_freshness_block(frames, now) == "",
        "fresh exact-source component frames should not block",
    )
    stale = {k: v.copy() for k, v in frames.items()}
    stale["ES_VWAP3"].loc[0, "dt_utc"] = pd.Timestamp("2026-07-06 12:57:00", tz="UTC")
    reason = live._lucid_exact_source_freshness_block(stale, now)
    _assert(reason == "", f"one stale exact-source component should not block whole basket: {reason}")
    warning = live._lucid_exact_source_freshness_warning(
        live._lucid_exact_source_freshness_details(stale, now)
    )
    _assert("ES_VWAP3" in warning and "stale" in warning.lower(), f"partial stale warning missing: {warning}")
    all_stale = {k: v.copy() for k, v in frames.items()}
    for k in all_stale:
        all_stale[k].loc[0, "dt_utc"] = pd.Timestamp("2026-07-06 12:00:00", tz="UTC")
    reason = live._lucid_exact_source_freshness_block(all_stale, now)
    _assert("ES_VWAP3" in reason and "stale" in reason.lower(), f"all-stale exact-source frame should block: {reason}")
    print("Dukascopy exact-source helpers ok")


def verify_dukascopy_live_cache_roundtrip() -> None:
    old_data_dir = config.DATA_DIR
    with tempfile.TemporaryDirectory() as td:
        config.DATA_DIR = td
        seed_last = pd.Timestamp("2026-06-22 20:59:00", tz="UTC")
        df = pd.DataFrame([
            {
                "dt_utc": seed_last - pd.Timedelta(minutes=1),
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.5,
                "volume": 1.0,
                "dt_ny": (seed_last - pd.Timedelta(minutes=1)).tz_convert(live.NY),
            },
            {
                "dt_utc": seed_last + pd.Timedelta(minutes=1),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 2.0,
                "dt_ny": (seed_last + pd.Timedelta(minutes=1)).tz_convert(live.NY),
            },
        ])
        live._duka_save_live_cache("es", df, seed_last)
        loaded = live._duka_load_live_cache("es")
        _assert(len(loaded) == 1, f"live cache should only keep rows after seed last ts: {loaded}")
        _assert(pd.Timestamp(loaded.iloc[0]["dt_utc"]) == seed_last + pd.Timedelta(minutes=1), "live cache kept wrong row")
        _assert(str(loaded.iloc[0]["dt_ny"].tzinfo), "live cache should restore dt_ny timezone")
    config.DATA_DIR = old_data_dir
    print("Dukascopy live cache roundtrip ok")


def verify_dukascopy_confirmed_empty_hours_roundtrip() -> None:
    old_data_dir = config.DATA_DIR
    old_fetched = set(live._DUKA_FETCHED_HOURS)
    old_confirmed = set(live._DUKA_CONFIRMED_EMPTY_HOURS)
    old_loaded = live._DUKA_EMPTY_HOURS_LOADED
    with tempfile.TemporaryDirectory() as td:
        config.DATA_DIR = td
        try:
            live._DUKA_FETCHED_HOURS.clear()
            live._DUKA_CONFIRMED_EMPTY_HOURS.clear()
            live._DUKA_EMPTY_HOURS_LOADED = False
            hk = ("USA500IDXUSD", 2026, 5, 19, 17)
            live._DUKA_CONFIRMED_EMPTY_HOURS.add(hk)
            live._duka_save_confirmed_empty_hours()

            live._DUKA_FETCHED_HOURS.clear()
            live._DUKA_CONFIRMED_EMPTY_HOURS.clear()
            live._DUKA_EMPTY_HOURS_LOADED = False
            live._duka_ensure_empty_hours_loaded()
            _assert(hk in live._DUKA_CONFIRMED_EMPTY_HOURS, "confirmed empty hour should reload from disk")
            _assert(hk in live._DUKA_FETCHED_HOURS, "confirmed empty hour should be skipped after reload")
        finally:
            config.DATA_DIR = old_data_dir
            live._DUKA_FETCHED_HOURS.clear()
            live._DUKA_FETCHED_HOURS.update(old_fetched)
            live._DUKA_CONFIRMED_EMPTY_HOURS.clear()
            live._DUKA_CONFIRMED_EMPTY_HOURS.update(old_confirmed)
            live._DUKA_EMPTY_HOURS_LOADED = old_loaded
    print("Dukascopy confirmed empty-hour cache roundtrip ok")


def verify_dukascopy_missing_hour_repair() -> None:
    rows = []
    for ts in [
        pd.Timestamp("2026-07-06 13:00:00+00:00"),
        pd.Timestamp("2026-07-06 13:01:00+00:00"),
        pd.Timestamp("2026-07-06 15:00:00+00:00"),
        pd.Timestamp("2026-07-06 15:01:00+00:00"),
    ]:
        rows.append({
            "dt_utc": ts,
            "dt_ny": ts.tz_convert(live.NY),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        })
    base = pd.DataFrame(rows)
    missing = live._duka_missing_session_hours(base, pd.Timestamp("2026-07-06 16:10:00+00:00"))
    missing_hours = {pd.Timestamp(h).strftime("%H:%M") for h in missing}
    _assert("14:00" in missing_hours, f"missing middle hour should be repaired: {missing_hours}")
    _assert("13:00" not in missing_hours and "15:00" not in missing_hours, f"present hours should not refetch: {missing_hours}")
    print("Dukascopy missing-hour repair ok")


def verify_dukascopy_missing_repair_is_throttled() -> None:
    old_limit = getattr(config, "LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL", 2)
    config.LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL = 2
    try:
        rows = []
        for ts in [
            pd.Timestamp("2026-07-06 13:00:00+00:00"),
            pd.Timestamp("2026-07-06 20:59:00+00:00"),
        ]:
            rows.append({
                "dt_utc": ts,
                "dt_ny": ts.tz_convert(live.NY),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            })
        base = pd.DataFrame(rows)
        now = pd.Timestamp("2026-07-06 21:10:00+00:00")
        hours = live._duka_update_hours(pd.Timestamp("2026-07-06 20:59:00+00:00"), now, base)
        labels = [pd.Timestamp(h).strftime("%H:%M") for h in hours]
        _assert("19:00" in labels and "20:00" in labels, f"recent hours should stay prioritized: {labels}")
        repair_labels = [x for x in labels if x not in {"19:00", "20:00"}]
        _assert(repair_labels == ["14:00", "15:00"], f"old missing repairs should be throttled to first 2: {labels}")
    finally:
        config.LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL = old_limit
    print("Dukascopy missing-hour repair throttle ok")


def verify_dukascopy_confirmed_empty_hours_do_not_starve_repair() -> None:
    old_limit = getattr(config, "LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL", 2)
    old_fetched = set(live._DUKA_FETCHED_HOURS)
    config.LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL = 2
    inst = "USA500IDXUSD"
    try:
        live._DUKA_FETCHED_HOURS.clear()
        live._DUKA_FETCHED_HOURS.add(live._duka_hour_key(inst, pd.Timestamp("2026-07-06 14:00:00+00:00")))
        live._DUKA_FETCHED_HOURS.add(live._duka_hour_key(inst, pd.Timestamp("2026-07-06 15:00:00+00:00")))
        rows = []
        for ts in [
            pd.Timestamp("2026-07-06 13:00:00+00:00"),
            pd.Timestamp("2026-07-06 20:59:00+00:00"),
        ]:
            rows.append({
                "dt_utc": ts,
                "dt_ny": ts.tz_convert(live.NY),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            })
        base = pd.DataFrame(rows)
        now = pd.Timestamp("2026-07-06 21:10:00+00:00")
        hours = live._duka_update_hours(pd.Timestamp("2026-07-06 20:59:00+00:00"), now, base, inst)
        labels = [pd.Timestamp(h).strftime("%H:%M") for h in hours]
        repair_labels = [x for x in labels if x not in {"19:00", "20:00"}]
        _assert(repair_labels == ["16:00", "17:00"], f"confirmed empty hours should not starve repair: {labels}")
    finally:
        config.LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL = old_limit
        live._DUKA_FETCHED_HOURS.clear()
        live._DUKA_FETCHED_HOURS.update(old_fetched)
    print("Dukascopy confirmed-empty repair starvation guard ok")


def verify_dukascopy_http_error_is_retryable_not_empty() -> None:
    class FakeResp:
        def __init__(self, status_code: int, content: bytes):
            self.status_code = status_code
            self.content = content

    class FakeSession:
        def __init__(self, resp: FakeResp):
            self.resp = resp
            self.calls = 0

        def get(self, url, timeout):
            self.calls += 1
            return self.resp

    had_session = hasattr(live._DUKA_LOCAL, "session")
    old_session = getattr(live._DUKA_LOCAL, "session", None)
    old_sleep = live.time.sleep
    try:
        live.time.sleep = lambda _: None
        bad = FakeSession(FakeResp(500, b""))
        live._DUKA_LOCAL.session = bad
        out = live._duka_ticks("USA500IDXUSD", pd.Timestamp("2026-07-06 14:00:00+00:00"))
        _assert(out is None, f"HTTP 500 should be retryable failure, not empty: {out}")
        _assert(bad.calls == 4, f"HTTP 500 should retry four times: {bad.calls}")

        missing = FakeSession(FakeResp(404, b"not found"))
        live._DUKA_LOCAL.session = missing
        out = live._duka_ticks("USA500IDXUSD", pd.Timestamp("2026-07-06 14:00:00+00:00"))
        _assert(out == [], f"HTTP 404 should classify as confirmed empty/missing hour: {out}")
        _assert(missing.calls == 1, f"HTTP 404 should not waste retries: {missing.calls}")
    finally:
        live.time.sleep = old_sleep
        if had_session:
            live._DUKA_LOCAL.session = old_session
        elif hasattr(live._DUKA_LOCAL, "session"):
            delattr(live._DUKA_LOCAL, "session")
    print("Dukascopy HTTP error retry classification ok")


def verify_dukascopy_fetch_failure_does_not_confirm_empty_hour() -> None:
    old_cache = {k: v.copy() for k, v in live._DUKA_RAW_CACHE.items()}
    old_fetched = set(live._DUKA_FETCHED_HOURS)
    old_confirmed = set(live._DUKA_CONFIRMED_EMPTY_HOURS)
    old_misses = dict(live._DUKA_EMPTY_HOUR_MISSES)
    old_loaded = live._DUKA_EMPTY_HOURS_LOADED
    old_ticks = live._duka_ticks
    try:
        live._DUKA_RAW_CACHE.clear()
        live._DUKA_FETCHED_HOURS.clear()
        live._DUKA_CONFIRMED_EMPTY_HOURS.clear()
        live._DUKA_EMPTY_HOUR_MISSES.clear()
        live._DUKA_EMPTY_HOURS_LOADED = True
        ts = pd.Timestamp("2026-07-06 13:01:00+00:00")
        live._DUKA_RAW_CACHE["es"] = pd.DataFrame([{
            "dt_utc": ts,
            "dt_ny": ts.tz_convert(live.NY),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }])
        live._duka_ticks = lambda inst, hour: None
        live._duka_update_market("ES=F", pd.Timestamp("2026-07-06 16:10:00+00:00"))
        _assert(not live._DUKA_EMPTY_HOUR_MISSES, f"failed fetch must not count as empty miss: {live._DUKA_EMPTY_HOUR_MISSES}")
        _assert(not live._DUKA_FETCHED_HOURS, f"failed fetch must remain retryable: {live._DUKA_FETCHED_HOURS}")
        _assert(not live._DUKA_CONFIRMED_EMPTY_HOURS, f"failed fetch must not persist as empty: {live._DUKA_CONFIRMED_EMPTY_HOURS}")
    finally:
        live._duka_ticks = old_ticks
        live._DUKA_RAW_CACHE.clear()
        live._DUKA_RAW_CACHE.update(old_cache)
        live._DUKA_FETCHED_HOURS.clear()
        live._DUKA_FETCHED_HOURS.update(old_fetched)
        live._DUKA_CONFIRMED_EMPTY_HOURS.clear()
        live._DUKA_CONFIRMED_EMPTY_HOURS.update(old_confirmed)
        live._DUKA_EMPTY_HOUR_MISSES.clear()
        live._DUKA_EMPTY_HOUR_MISSES.update(old_misses)
        live._DUKA_EMPTY_HOURS_LOADED = old_loaded
    print("Dukascopy failed fetch remains retryable ok")


def verify_feed_block_price_preservation() -> None:
    flat = RuntimeBot()
    flat.prices = {"ES_VWAP3": 101.0}
    live._apply_feed_block(flat, "blocked test")
    _assert(flat.prices == {}, f"flat blocked bot should clear stale prices: {flat.prices}")

    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    pos = live.LucidPos(
        id="px",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test price preserve",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()) - 1,
        last_close=100.75,
        last_day=str(base.tz_convert(live.NY).date()),
        partial_done=False,
        realized=0.0,
        note="verify price preserve",
    )
    bot.pos[pos.key] = pos
    bot.prices = {"ES_VWAP3": 100.75}
    live._apply_feed_block(bot, "blocked test")
    _assert(bot.prices == {"ES_VWAP3": 100.75}, f"open-position price should be preserved: {bot.prices}")
    _assert(bot.status == "blocked - futures feed not realtime", f"blocked status mismatch: {bot.status}")
    print("feed block price preservation ok")


def verify_feed_block_forces_expired_position_eod() -> None:
    bot = RuntimeBot()
    bot._enforce_live_open_guard = True
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = str(base.tz_convert(live.NY).date())
    bot.day_key = day
    bot._open_guard_now_utc = lambda: pd.Timestamp("2026-07-06 21:01:00+00:00")
    bot.pos["ES_VWAP3"] = live.LucidPos(
        id="feedblock",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test feed block eod",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()),
        last_close=100.5,
        last_day=day,
        partial_done=False,
        realized=0.0,
        note="verify feed block eod",
    )
    live._apply_feed_block(bot, "blocked test")
    _assert("ES_VWAP3" not in bot.pos, "feed block after forced-flat deadline must close open position")
    _assert(bot.history and bot.history[0]["reason"] == "eod", f"feed-block close should be eod: {bot.history}")
    print("feed block expired-position EOD guard ok")


def verify_feed_block_open_position_resumes_management() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = base.tz_convert(live.NY).date()
    pos = live.LucidPos(
        id="resume",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test feed resume",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()),
        last_close=100.25,
        last_day=str(day),
        partial_done=False,
        realized=0.0,
        note="verify feed resume",
    )
    bot.day_key = str(day)
    bot.pos[pos.key] = pos
    bot.prices = {"ES_VWAP3": 100.25}
    live._apply_feed_block(bot, "blocked test", "TradingView websocket (delayed_streaming_600)")
    _assert("ES_VWAP3" in bot.pos, "blocked feed must preserve open position")
    _assert(bot.history == [], f"blocked feed must not create synthetic exits: {bot.history}")
    _assert(bot.prices == {"ES_VWAP3": 100.25}, f"blocked feed should preserve last mark: {bot.prices}")

    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            {
                "dt_utc": base,
                "dt_ny": base.tz_convert(live.NY),
                "day": day,
                "open": 100.0,
                "high": 100.4,
                "low": 99.8,
                "close": 100.25,
                "volume": 1.0,
            },
            {
                "dt_utc": base + pd.Timedelta(minutes=3),
                "dt_ny": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY),
                "day": day,
                "open": 100.25,
                "high": 100.3,
                "low": 98.8,
                "close": 99.0,
                "volume": 1.0,
            },
        ])
    }
    bot.data_error = ""
    bot.live_feed_status = "TradingView websocket (realtime_streaming)"
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "position should resume management when realtime feed returns")
    _assert(len(bot.history) == 1 and bot.history[0]["reason"] == "stop",
            f"resumed feed should honor first missed stop exactly once: {bot.history}")
    print("feed-block open-position resume ok")


def verify_continuous_uses_shared_runtime_path() -> None:
    _assert(
        cont.LucidContinuousPaperBot._tick is live.LucidPassPaperBot._tick,
        "continuous bot must inherit the same production _tick scanner as pass bot",
    )
    _assert(
        cont.LucidContinuousPaperBot._manage_pending is live.LucidPassPaperBot._manage_pending,
        "continuous bot must inherit the same position catch-up manager as pass bot",
    )
    _assert(
        cont.LucidContinuousPaperBot._signal is live.LucidPassPaperBot._signal,
        "continuous bot must inherit the same strategy signal dispatcher as pass bot",
    )
    bot = cont.LucidContinuousPaperBot()
    bot._save = lambda: None
    bot.balance = live.TARGET_BALANCE + 1.0
    _assert(not bot._stops_after_target(), "continuous bot should not stop after pass target")
    print("continuous shared runtime path ok")


def verify_pass_and_continuous_target_guards() -> None:
    base = pd.Timestamp("2026-07-06 13:48:00", tz="UTC")
    cur = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "day": base.tz_convert(live.NY).date(),
    })

    pass_bot = RuntimeBot()
    pass_bot.balance = live.TARGET_BALANCE + 1.0
    _assert(
        not pass_bot._can_open_key("ES_VWAP3", cur),
        "pass bot must block new entries once target is reached",
    )
    _assert(pass_bot.passed, "pass bot should mark passed when target blocks a new entry")

    cont_bot = cont.LucidContinuousPaperBot()
    cont_bot._save = lambda: None
    cont_bot._entry_guard_ok = lambda key, row: True
    cont_bot.realtime_entry_ready = True
    cont_bot.balance = live.TARGET_BALANCE + 1.0
    _assert(
        cont_bot._can_open_key("ES_VWAP3", cur),
        "continuous bot must keep allowing entries after target is crossed",
    )
    print("pass/continuous target guards ok")


def verify_pass_and_continuous_shared_frame_independence() -> None:
    class SignalPassBot(RuntimeBot):
        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test shared frame",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify shared-frame independence",
                "spent": False,
            }

    class SignalContinuousBot(SignalPassBot):
        def _stops_after_target(self) -> bool:
            return False

        def _uses_daily_loss_guard(self) -> bool:
            return False

        def _can_open_key(self, key: str, cur) -> bool:
            if not self.enabled or self.failed:
                return False
            if self.equity() <= self.floor:
                self.failed = True
                return False
            if not self._entry_guard_ok(key, cur):
                return False
            return True

    base = pd.Timestamp("2026-07-06 13:48:00", tz="UTC")
    day = base.tz_convert(live.NY).date()
    shared = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "day": day,
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 1.0,
        }])
    }

    pass_bot = SignalPassBot()
    cont_bot = SignalContinuousBot()
    pass_bot.balance = live.TARGET_BALANCE + 1.0
    cont_bot.balance = live.TARGET_BALANCE + 1.0
    pass_bot._df = live._clone_component_frames(shared)
    cont_bot._df = live._clone_component_frames(shared)

    pass_bot._tick()
    cont_bot._tick()

    _assert("ES_VWAP3" not in pass_bot.pos, "pass bot should not open after target on shared frame")
    _assert("ES_VWAP3" in cont_bot.pos, "continuous bot should open after target on its own shared-frame clone")
    _assert(float(shared["ES_VWAP3"].loc[0, "close"]) == 100.0, "shared frame should remain untouched")
    print("pass/continuous shared-frame independence ok")


def verify_open_position_status_has_priority_over_pass_guard() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = str(base.tz_convert(live.NY).date())
    bot.passed = True
    bot.pos["ES_VWAP3"] = live.LucidPos(
        id="status",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test status",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()),
        last_close=100.0,
        last_day=day,
        partial_done=False,
        realized=0.0,
        note="verify status",
    )
    bot._set_status()
    _assert(
        bot.status.startswith("in trade:"),
        f"open position should not be hidden by pass status: {bot.status}",
    )
    print("open-position status priority ok")


def verify_daily_loss_blocks_new_entries_not_management() -> None:
    class DailyLossBot(RuntimeBot):
        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

    bot = DailyLossBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = base.tz_convert(live.NY).date()
    bot.day_key = str(day)
    bot.day_pnl = -live.DAILY_LOSS_LIMIT - 1.0
    cur = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "day": day,
    })
    _assert(
        not bot._can_open_key("ES_VWAP3", cur),
        "daily loss guard must block new entries",
    )
    _assert(
        bot.daily_stopped_day == bot.day_key,
        f"daily loss guard should mark stopped day: {bot.daily_stopped_day}",
    )

    pos = live.LucidPos(
        id="dl",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test daily guard management",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()) - 1,
        last_close=100.0,
        last_day=str(day),
        partial_done=False,
        realized=0.0,
        note="verify daily guard management",
    )
    bot.pos[pos.key] = pos
    stop_bar = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "day": day,
        "high": 100.2,
        "low": 98.8,
        "close": 99.0,
    })
    bot._manage("ES_VWAP3", stop_bar)
    _assert("ES_VWAP3" not in bot.pos, "daily loss guard must not prevent open-position management")
    _assert(bot.history and bot.history[0]["reason"] == "stop", f"daily guard management mismatch: {bot.history}")
    print("daily-loss new-entry guard keeps management ok")


def verify_daily_loss_guard_alert_semantics() -> None:
    class AlertCaptureBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.alerts = []

        def _alert(self, text: str):
            self.alerts.append(text)

    bot = AlertCaptureBot()
    bot.day_key = "2026-07-06"
    bot.day_pnl = -live.DAILY_LOSS_LIMIT - 1.0
    bot.realtime_entry_ready = True
    bot._warn_signal("ES_VWAP3", "long", 100.0, 99.0, 102.0, "verify daily stop warning")
    _assert(not bot.alerts, f"pass bot must not send warnings after daily loss guard: {bot.alerts}")
    _assert(not bot.warning_keys, f"blocked warning should not consume warning keys: {bot.warning_keys}")

    cont_bot = cont.LucidContinuousPaperBot()
    cont_bot._save = lambda: None
    cont_bot.enabled = True
    cont_bot.failed = False
    cont_bot.passed = False
    cont_bot.pos = {}
    cont_bot.balance = live.START_BALANCE
    cont_bot.day_pnl = -live.DAILY_LOSS_LIMIT - 1.0
    msg = cont_bot._next_signal_window()
    _assert("Daily loss guard" not in msg, f"continuous bot should not report daily guard: {msg}")
    print("daily-loss alert semantics ok")


def verify_vwap_open_does_not_duplicate_warning() -> None:
    class WarnCaptureBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.warn_calls = []

        def _warn_signal(self, key: str, side: str, entry: float, stop: float,
                         target: float, note: str):
            self.warn_calls.append((key, side, entry, stop, target, note))

    bot = WarnCaptureBot()
    base = pd.Timestamp("2026-07-06 13:00:00+00:00")
    rows = []
    for i in range(live.VWAP_MIN_BARS):
        dt = base + pd.Timedelta(minutes=3 * i)
        rows.append({
            "dt_utc": dt,
            "dt_ny": dt.tz_convert(live.NY),
            "day": dt.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1.0,
        })
    dt = base + pd.Timedelta(minutes=3 * live.VWAP_MIN_BARS)
    rows.append({
        "dt_utc": dt,
        "dt_ny": dt.tz_convert(live.NY),
        "day": dt.tz_convert(live.NY).date(),
        "open": 100.0,
        "high": 110.0,
        "low": 100.0,
        "close": 100.0,
        "volume": 1.0,
    })
    today = pd.DataFrame(rows)
    sig = bot._vwap_signal("ES_VWAP3", today, len(today) - 1)
    _assert(sig and sig["side"] == "short" and not sig.get("spent"), f"fixture should produce live VWAP short: {sig}")
    _assert(not bot.warn_calls, f"same-bar VWAP open must not send duplicate warning: {bot.warn_calls}")
    print("VWAP open duplicate-warning guard ok")


def verify_global_warning_dedup_does_not_consume_local_warning() -> None:
    class AlertCaptureBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.alerts = []
            self.telegram_enabled = True

        def _alert(self, text: str):
            self.alerts.append(text)

    bot = AlertCaptureBot()
    bot.day_key = "2026-07-06"
    bot.realtime_entry_ready = True
    entry = 100.0
    _, tick, _ = live.MARKETS[live.COMPONENTS["ES_VWAP3"]["symbol"]]
    level_key = int(round(entry / tick))
    warn_key = f"{bot.day_key}:ES_VWAP3:long:{level_key}"
    live._GLOBAL_WARNING_ALERTS.clear()
    live._GLOBAL_WARNING_ALERTS[warn_key] = live.time.time()

    bot._warn_signal("ES_VWAP3", "long", entry, 99.0, 102.0, "verify suppressed warning")
    _assert(not bot.alerts, f"globally suppressed warning should not alert: {bot.alerts}")
    _assert(warn_key not in bot.warning_keys, f"suppressed warning consumed local key: {bot.warning_keys}")

    live._GLOBAL_WARNING_ALERTS.clear()
    bot._warn_signal("ES_VWAP3", "long", entry, 99.0, 102.0, "verify real warning")
    _assert(len(bot.alerts) == 1, f"valid warning should alert once: {bot.alerts}")
    _assert(warn_key in bot.warning_keys, "valid warning should consume local warning key")
    live._GLOBAL_WARNING_ALERTS.clear()
    print("global warning dedup local-consumption guard ok")


def verify_atomic_state_save_roundtrip() -> None:
    class SaveBot(RuntimeBot):
        def __init__(self, path: str):
            super().__init__()
            self._path_value = path

        def _path(self) -> str:
            return self._path_value

        def _save(self):
            return live.LucidPassPaperBot._save(self)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(pathlib.Path(tmpdir) / "lucid_state.json")
        bot = SaveBot(path)
        bot.balance = 50123.45
        bot.fired_keys = {"verify:ES_VWAP3"}
        bot._save()
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        _assert(data["balance"] == 50123.45, f"atomic save balance mismatch: {data}")
        _assert(data["strategy_version"] == live.STRATEGY_VERSION, f"version missing after save: {data}")
        _assert(data["strategy_fingerprint"] == live.STRATEGY_FINGERPRINT, f"fingerprint missing after save: {data}")
        _assert(not pathlib.Path(path + ".tmp").exists(), "atomic save should replace and remove tmp file")
    print("atomic state save roundtrip ok")


def verify_persisted_open_position_roundtrip_catches_up() -> None:
    class PersistBot(RuntimeBot):
        def __init__(self, path: str):
            super().__init__()
            self._path_value = path

        def _path(self) -> str:
            return self._path_value

        def _save(self):
            return live.LucidPassPaperBot._save(self)

        def load_from_disk(self):
            return live.LucidPassPaperBot._load(self)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(pathlib.Path(tmpdir) / "lucid_state.json")
        base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
        day = base.tz_convert(live.NY).date()
        bot = PersistBot(path)
        bot.day_key = str(day)
        bot.balance = 50123.0
        bot.pos["ES_VWAP3"] = live.LucidPos(
            id="persist",
            key="ES_VWAP3",
            symbol="ES=F",
            label="MES",
            strat="test persisted restart",
            side="long",
            qty=40.0,
            qty0=40.0,
            entry=100.0,
            stop=99.0,
            stop0=99.0,
            tp1=101.0,
            target=102.0,
            r_points=1.0,
            micro_pv=5.0,
            tick=0.25,
            risk_usd=200.0,
            cost_usd=0.0,
            opened_at=0.0,
            opened_bar=int(base.timestamp()),
            best=100.0,
            last_managed_bar=int(base.timestamp()),
            last_close=100.1,
            last_day=str(day),
            partial_done=False,
            realized=0.0,
            note="verify persisted restart",
        )
        bot._save()

        restored = PersistBot(path)
        restored.load_from_disk()
        _assert("ES_VWAP3" in restored.pos, "persisted open position should reload")
        p = restored.pos["ES_VWAP3"]
        _assert(p.last_managed_bar == int(base.timestamp()), f"last managed bar lost: {p}")
        _assert(p.last_day == str(day), f"last day lost: {p}")
        restored._df = {
            "ES_VWAP3": pd.DataFrame([
                {
                    "dt_utc": base,
                    "dt_ny": base.tz_convert(live.NY),
                    "day": day,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.1,
                    "volume": 1.0,
                },
                {
                    "dt_utc": base + pd.Timedelta(minutes=3),
                    "dt_ny": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY),
                    "day": day,
                    "open": 100.1,
                    "high": 100.2,
                    "low": 98.8,
                    "close": 99.0,
                    "volume": 1.0,
                },
            ])
        }
        restored._tick()
        _assert("ES_VWAP3" not in restored.pos, "restored position should catch up and close")
        _assert(restored.history and restored.history[0]["reason"] == "stop",
                f"restored catch-up should close at stop: {restored.history}")
    print("persisted open-position restart catch-up ok")


def verify_tradingview_uses_1m_source_for_resampled_components() -> None:
    _assert(
        live.TV_USE_QUOTES_FOR_STRATEGY_CANDLES is False,
        "Lucid strategy candles must come from TradingView chart-series OHLC, not quote-built bars",
    )
    for key, c in live.COMPONENTS.items():
        if c.get("resample"):
            got = live._tv_interval(c)
            _assert(got == "1", f"{key} should request 1m TradingView source candles, got {got}")
    _assert(
        live.TV_COMPONENT_BARS["NQ_TURTLE30"] >= 30000
        and live.TV_COMPONENT_BARS["CL_NR7_30"] >= 30000,
        "30m strategies need enough 1m history for prior-session rules",
    )

    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    rows = []
    for i in range(120):
        dt = base + pd.Timedelta(minutes=i)
        px = 100.0 + i * 0.01
        rows.append({
            "dt_utc": dt,
            "dt_ny": dt.tz_convert(live.NY),
            "open": px,
            "high": px + 0.05,
            "low": px - 0.05,
            "close": px + 0.01,
            "volume": 1.0,
        })
    raw = pd.DataFrame(rows)
    frames = live._build_component_frames_from_raw({
        "CL_VWAP5": raw,
        "NQ_TURTLE30": raw,
        "CL_NR7_30": raw,
    }, drop_incomplete=False)
    _assert(
        frames["CL_VWAP5"]["dt_utc"].diff().dropna().eq(pd.Timedelta(minutes=5)).all(),
        "CL_VWAP5 must be resampled from 1m to 5m bars",
    )
    for key in ("NQ_TURTLE30", "CL_NR7_30"):
        _assert(
            frames[key]["dt_utc"].diff().dropna().eq(pd.Timedelta(minutes=30)).all(),
            f"{key} must be resampled from 1m to 30m bars",
        )
    print("TradingView 1m source resampling ok")


def verify_tradingview_row_parser_skips_malformed_rows() -> None:
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    rows = {
        0: [base.timestamp(), 100.0, 101.0, 99.0, 100.5, 10.0],
        1: [(base + pd.Timedelta(minutes=1)).timestamp(), 100.5, None, 100.0, 100.2, 8.0],
        2: [(base + pd.Timedelta(minutes=2)).timestamp(), 100.2, 101.2, 100.1, 101.0, None],
        3: ["bad-ts", 101.0, 102.0, 100.5, 101.5, 12.0],
    }
    out = live._tv_rows_to_frame(rows)
    _assert(len(out) == 2, f"malformed TradingView rows should be skipped, got {out}")
    _assert(list(out["close"].round(2)) == [100.5, 101.0], f"valid row closes mismatch: {out}")
    _assert(float(out.iloc[1]["volume"]) == 0.0, f"None volume should become 0.0: {out}")
    print("TradingView malformed-row parser ok")


def verify_component_frame_clone_isolates_bots() -> None:
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    shared = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "day": base.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }])
    }
    first = live._clone_component_frames(shared)
    second = live._clone_component_frames(shared)
    first["ES_VWAP3"].loc[0, "close"] = 999.0
    _assert(float(shared["ES_VWAP3"].loc[0, "close"]) == 100.5, "clone mutation leaked into shared frame")
    _assert(float(second["ES_VWAP3"].loc[0, "close"]) == 100.5, "clone mutation leaked into sibling bot frame")
    print("component frame clone isolation ok")


def verify_lucid_history_readiness_gate() -> None:
    def raw_1m_sessions(n: int) -> pd.DataFrame:
        rows = []
        days = pd.bdate_range("2026-06-15", periods=n)
        for day_i, day in enumerate(days):
            base = pd.Timestamp(day.date().isoformat() + " 13:00:00", tz="UTC")
            for minute in range(2):
                dt = base + pd.Timedelta(minutes=minute)
                px = 100.0 + day_i + minute * 0.01
                rows.append({
                    "dt_utc": dt,
                    "dt_ny": dt.tz_convert(live.NY),
                    "open": px,
                    "high": px + 0.05,
                    "low": px - 0.05,
                    "close": px + 0.01,
                    "volume": 1.0,
                })
        return pd.DataFrame(rows)

    empty_reason = live._lucid_history_block_reason({})
    _assert("has no candles" in empty_reason, f"missing history should block: {empty_reason}")

    short_raw = raw_1m_sessions(8)
    short = live._build_component_frames_from_raw({
        "NQ_TURTLE30": short_raw,
        "CL_NR7_30": short_raw,
    }, drop_incomplete=False)
    short_reason = live._lucid_history_block_reason(short)
    _assert(
        "NQ 30m Turtle Soup 10 needs 14 prior sessions" in short_reason,
        f"short Turtle history should block: {short_reason}",
    )

    enough_raw = raw_1m_sessions(16)
    enough = live._build_component_frames_from_raw({
        "NQ_TURTLE30": enough_raw,
        "CL_NR7_30": enough_raw,
    }, drop_incomplete=False)
    enough_reason = live._lucid_history_block_reason(enough)
    _assert(enough_reason == "", f"enough prior sessions should not block: {enough_reason}")
    print("Lucid history readiness gate ok")


def verify_fresh_bar_gate() -> None:
    bot = RuntimeBot()
    now = pd.Timestamp.now(tz="UTC")

    def row(minutes_old: float):
        dt = now - pd.Timedelta(minutes=minutes_old)
        return pd.Series({"dt_utc": dt, "dt_ny": dt.tz_convert(live.NY)})

    _assert(not bot._fresh_entry_bar_ok("ES_VWAP3", row(2.0)), "3m bar should not be tradable before it closes")
    _assert(bot._fresh_entry_bar_ok("ES_VWAP3", row(3.2)), "3m bar should be fresh just after close")
    _assert(not bot._fresh_entry_bar_ok("ES_VWAP3", row(8.0)), "3m bar should be stale at 8 minutes")
    _assert(not bot._fresh_entry_bar_ok("NQ_TURTLE30", row(20.0)), "30m bar should not be tradable before it closes")
    _assert(bot._fresh_entry_bar_ok("NQ_TURTLE30", row(30.2)), "30m bar should be fresh just after close")
    _assert(not bot._fresh_entry_bar_ok("NQ_TURTLE30", row(40.0)), "30m bar should be stale at 40 minutes")
    print("fresh-bar gate ok")


def verify_production_bot_enforces_live_open_guard_by_default() -> None:
    bot = live.LucidPassPaperBot()
    _assert(
        bot._enforce_open_wall_clock(),
        "production Lucid bot must enforce wall-clock freshness for live opens by default",
    )
    print("production live-open guard default ok")


def verify_incomplete_live_bar_is_dropped() -> None:
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    d = pd.DataFrame([
        {
            "dt_utc": base,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
            "dt_ny": base.tz_convert(live.NY),
            "day": base.tz_convert(live.NY).date(),
        },
        {
            "dt_utc": base + pd.Timedelta(minutes=3),
            "open": 100.5,
            "high": 110.0,
            "low": 100.0,
            "close": 109.0,
            "volume": 20.0,
            "dt_ny": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY),
            "day": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY).date(),
        },
    ])
    out = live._drop_incomplete_tail(d, 180, now=base + pd.Timedelta(minutes=5))
    _assert(len(out) == 1, f"incomplete final bar should be dropped: {out}")
    _assert(pd.Timestamp(out.iloc[-1]["dt_utc"]) == base, "closed bar should remain after drop")
    out2 = live._drop_incomplete_tail(d, 180, now=base + pd.Timedelta(minutes=6))
    _assert(len(out2) == 2, "final bar should remain once its close time has passed")

    burst = pd.concat([
        d,
        pd.DataFrame([{
            "dt_utc": base + pd.Timedelta(minutes=6),
            "open": 109.0,
            "high": 111.0,
            "low": 108.0,
            "close": 110.0,
            "volume": 30.0,
            "dt_ny": (base + pd.Timedelta(minutes=6)).tz_convert(live.NY),
            "day": (base + pd.Timedelta(minutes=6)).tz_convert(live.NY).date(),
        }]),
    ], ignore_index=True)
    out3 = live._drop_incomplete_tail(burst, 180, now=base + pd.Timedelta(minutes=5))
    _assert(len(out3) == 1, f"all incomplete burst bars should be dropped, got {len(out3)}")
    print("incomplete live bar drop ok")


def verify_session_entry_gate() -> None:
    cases = [
        ("2026-07-06 12:59:59+00:00", False),
        ("2026-07-06 13:00:00+00:00", True),
        ("2026-07-06 20:57:00+00:00", True),
        ("2026-07-06 20:59:59+00:00", True),
        ("2026-07-06 21:00:00+00:00", False),
        ("2026-07-06 21:11:12+00:00", False),
    ]
    for ts, expected in cases:
        got = live._in_backtest_session_utc(pd.Timestamp(ts))
        _assert(got is expected, f"session entry gate mismatch at {ts}: {got}")
    print("session entry gate ok")


def verify_completed_final_bar_entry_clock() -> None:
    def row(key: str, ts: str) -> pd.Series:
        dt = pd.Timestamp(ts)
        return pd.Series({"dt_utc": dt, "dt_ny": dt.tz_convert(live.NY)})

    es_final = row("ES_VWAP3", "2026-07-06 20:57:00+00:00")
    cl_final = row("CL_VWAP5", "2026-07-06 20:55:00+00:00")
    nq30_final = row("NQ_TURTLE30", "2026-07-06 20:30:00+00:00")

    _assert(
        live._entry_clock_ok(es_final, "ES_VWAP3", pd.Timestamp("2026-07-06 21:00:00+00:00")),
        "fresh completed ES/NQ final 3m bar should be tradable at 21:00 UTC",
    )
    _assert(
        live._entry_clock_ok(cl_final, "CL_VWAP5", pd.Timestamp("2026-07-06 21:00:00+00:00")),
        "fresh completed CL final 5m bar should be tradable at 21:00 UTC",
    )
    _assert(
        live._entry_clock_ok(nq30_final, "NQ_TURTLE30", pd.Timestamp("2026-07-06 21:00:00+00:00")),
        "fresh completed 30m final bar should be tradable at 21:00 UTC",
    )
    _assert(
        not live._entry_clock_ok(es_final, "ES_VWAP3", pd.Timestamp("2026-07-06 21:00:46+00:00")),
        "ES/NQ final 3m bar should become stale after close+45s",
    )
    _assert(
        not live._entry_clock_ok(cl_final, "CL_VWAP5", pd.Timestamp("2026-07-06 21:00:46+00:00")),
        "CL final 5m bar should become stale after close+45s",
    )
    _assert(
        not live._entry_clock_ok(nq30_final, "NQ_TURTLE30", pd.Timestamp("2026-07-06 21:00:46+00:00")),
        "30m final bar should become stale after close+45s",
    )
    _assert(
        not live._entry_clock_ok(es_final, "ES_VWAP3", pd.Timestamp("2026-07-06 21:11:12+00:00")),
        "stale after-session bar should not be tradable at 21:11 UTC",
    )
    stale_prior_day = row("ES_VWAP3", "2026-07-03 20:57:00+00:00")
    _assert(
        not live._entry_clock_ok(stale_prior_day, "ES_VWAP3", pd.Timestamp("2026-07-06 21:00:00+00:00")),
        "prior-session final bar should not be tradable on a later NY date",
    )
    print("completed final-bar entry clock ok")


def verify_open_wall_clock_guard_blocks_stale_entry() -> None:
    bot = RuntimeBot()
    bot._enforce_live_open_guard = True
    bot._open_guard_now_utc = lambda: pd.Timestamp("2026-07-06 21:11:12+00:00")
    cur = pd.Series({
        "dt_utc": pd.Timestamp("2026-07-06 20:57:00+00:00"),
        "dt_ny": pd.Timestamp("2026-07-06 20:57:00+00:00").tz_convert(live.NY),
        "day": pd.Timestamp("2026-07-06 20:57:00+00:00").tz_convert(live.NY).date(),
    })
    sig = {
        "key": "ES_VWAP3",
        "symbol": "ES=F",
        "label": "MES",
        "strat": "test stale guard",
        "side": "long",
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "note": "verify stale guard",
    }
    bot._open(sig, cur)
    _assert("ES_VWAP3" not in bot.pos, "stale after-session _open must be rejected")
    _assert(bot.log and "REJECT stale/out-of-session open" in bot.log[0]["msg"], f"reject note missing: {bot.log}")
    print("open wall-clock stale guard ok")


def verify_tick_blocks_0111_stale_entry() -> None:
    class StaleSignalBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self._enforce_live_open_guard = True
            self._open_guard_now_utc = lambda: pd.Timestamp("2026-07-06 21:11:12+00:00")
            self.signal_calls = 0

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            self.signal_calls += 1
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test stale tick",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify stale tick",
                "spent": False,
            }

    bot = StaleSignalBot()
    stale_final = pd.Timestamp("2026-07-06 20:57:00+00:00")
    bot._df = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": stale_final,
            "dt_ny": stale_final.tz_convert(live.NY),
            "day": stale_final.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }])
    }
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "01:11 Tbilisi stale final-session bar must not open")
    _assert(bot.signal_calls == 0, "stale 01:11 bar should be blocked before signal evaluation")
    print("01:11 stale tick entry block ok")


def verify_next_signal_window_uses_completed_bar_alerts() -> None:
    bot = RuntimeBot()
    session_open = pd.Timestamp("2026-07-06 13:00:00", tz="UTC").tz_convert(live.NY).to_pydatetime()
    before_open = pd.Timestamp("2026-07-06 12:55:00", tz="UTC").tz_convert(live.NY).to_pydatetime()
    late_session = pd.Timestamp("2026-07-06 20:59:00", tz="UTC").tz_convert(live.NY).to_pydatetime()

    es_first = bot._next_component_alert(session_open, before_open, 3, live.VWAP_MIN_BARS)
    cl_first = bot._next_component_alert(session_open, before_open, 5, live.VWAP_MIN_BARS)
    thirty_first = bot._next_component_alert(session_open, before_open, 30, 0)
    _assert(
        es_first.astimezone(live.TBILISI).strftime("%H:%M") == "17:48",
        f"ES/NQ first completed alert should be 17:48 Tbilisi: {es_first}",
    )
    _assert(
        cl_first.astimezone(live.TBILISI).strftime("%H:%M") == "18:20",
        f"CL first completed alert should be 18:20 Tbilisi: {cl_first}",
    )
    _assert(
        thirty_first.astimezone(live.TBILISI).strftime("%H:%M") == "17:30",
        f"30m first completed alert should be 17:30 Tbilisi: {thirty_first}",
    )

    es_final = bot._next_component_alert(session_open, late_session, 3, live.VWAP_MIN_BARS)
    cl_final = bot._next_component_alert(session_open, late_session, 5, live.VWAP_MIN_BARS)
    thirty_final = bot._next_component_alert(session_open, late_session, 30, 0)
    _assert(es_final.astimezone(live.TBILISI).strftime("%H:%M") == "01:00", f"ES final alert mismatch: {es_final}")
    _assert(cl_final.astimezone(live.TBILISI).strftime("%H:%M") == "01:00", f"CL final alert mismatch: {cl_final}")
    _assert(thirty_final.astimezone(live.TBILISI).strftime("%H:%M") == "01:00", f"30m final alert mismatch: {thirty_final}")
    print("next completed-bar alert timing ok")


def verify_next_signal_window_marks_stale_exact_source() -> None:
    bot = RuntimeBot()
    bot.data_error = (
        "Exact Dukascopy feed is stale during entry session: "
        "ES_VWAP3 latest closed 17:00 UTC"
    )
    msg = bot._next_signal_window()
    _assert("no Lucid signal will fire" in msg, f"stale exact-source message missing: {msg}")
    _assert("Strategy candle session" in msg, f"stale exact-source message should show session: {msg}")
    _assert("theoretical" in msg, f"stale exact-source message should mark checks theoretical: {msg}")

    bot.data_error = (
        "Some exact Dukascopy components are stale; stale components are blocked "
        "but fresh components still scan: ES_VWAP3 latest closed 19:00 UTC"
    )
    msg = bot._next_signal_window()
    _assert("only fresh components can fire" in msg, f"partial stale message missing fresh-only warning: {msg}")
    _assert("stale components are blocked" in msg.lower(), f"partial stale message missing blocked warning: {msg}")
    _assert("theoretical" in msg, f"partial stale message should mark stale checks theoretical: {msg}")
    print("stale exact-source next-signal status ok")


def verify_exact_source_freshness_details() -> None:
    frames = {}
    for key, c in live.COMPONENTS.items():
        if int(c["bar_sec"]) >= 1800:
            start = pd.Timestamp("2026-07-06 19:30:00+00:00")
        else:
            start = pd.Timestamp("2026-07-06 20:00:00+00:00")
        frames[key] = pd.DataFrame([{
            "dt_utc": start,
            "dt_ny": start.tz_convert(live.NY),
            "day": start.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }])

    in_session_now = pd.Timestamp("2026-07-06 20:34:00+00:00")
    details = live._lucid_exact_source_freshness_details(frames, in_session_now)
    _assert(all(d["stale"] for d in details), f"all old exact-source candles should be stale in-session: {details}")
    _assert(
        all(d.get("latest_closed_tbilisi") for d in details),
        f"freshness details should include Tbilisi timestamps: {details}",
    )
    _assert(
        bool(live._lucid_exact_source_freshness_block(frames, in_session_now)),
        "stale exact-source block should fire during the strategy session",
    )

    after_session_now = pd.Timestamp("2026-07-06 21:11:12+00:00")
    after = live._lucid_exact_source_freshness_details(frames, after_session_now)
    _assert(not any(d["stale"] for d in after), f"after-session stale candles should not be a live-entry block: {after}")
    _assert(
        all(d["state"] == "outside_session" for d in after),
        f"after-session delayed candles should be labeled outside_session: {after}",
    )
    session = live._lucid_session_text(pd.Timestamp("2026-07-06 13:00:00+00:00"))
    _assert("Mon 17:00-01:00 Tbilisi" in session, f"session text should match live strategy clock: {session}")
    print("exact-source freshness diagnostics ok")


def verify_partial_stale_exact_source_does_not_block_fresh_component() -> None:
    now = pd.Timestamp("2026-07-06 20:30:30+00:00")
    es_old = pd.Timestamp("2026-07-06 20:00:00+00:00")
    cl_fresh = pd.Timestamp("2026-07-06 20:25:00+00:00")
    frames = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": es_old,
            "dt_ny": es_old.tz_convert(live.NY),
            "day": es_old.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }]),
        "CL_VWAP5": pd.DataFrame([{
            "dt_utc": cl_fresh,
            "dt_ny": cl_fresh.tz_convert(live.NY),
            "day": cl_fresh.tz_convert(live.NY).date(),
            "open": 70.0,
            "high": 70.2,
            "low": 69.8,
            "close": 70.1,
            "volume": 1.0,
        }]),
    }
    details = live._lucid_exact_source_freshness_details(frames, now)
    stale_keys = {d["key"] for d in details if d.get("stale")}
    _assert("ES_VWAP3" in stale_keys, f"ES should be stale in this fixture: {details}")
    _assert("CL_VWAP5" not in stale_keys, f"CL should be fresh in this fixture: {details}")
    _assert(
        live._lucid_exact_source_freshness_block(frames, now) == "",
        "one stale component must not block the whole Lucid basket",
    )
    _assert(
        "ES_VWAP3" in live._lucid_exact_source_freshness_warning(details),
        "partial stale source should be a visible warning",
    )

    class PartialStaleBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self._df = frames
            self.signal_calls = []

        def _open_guard_now_utc(self):
            return now

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            self.signal_calls.append(key)
            return {
                "key": key,
                "symbol": live.COMPONENTS[key]["symbol"],
                "label": live.COMPONENTS[key]["label"],
                "strat": "partial stale fixture",
                "side": "long",
                "entry": float(today.iloc[-1]["close"]),
                "stop": float(today.iloc[-1]["close"]) - 0.5,
                "target": float(today.iloc[-1]["close"]) + 1.0,
                "note": "verify component freshness",
                "spent": False,
            }

    bot = PartialStaleBot()
    bot._tick()
    _assert("ES_VWAP3" not in bot.signal_calls, f"stale ES should not evaluate a signal: {bot.signal_calls}")
    _assert("CL_VWAP5" in bot.signal_calls, f"fresh CL should still evaluate: {bot.signal_calls}")
    _assert("CL_VWAP5" in bot.pos, "fresh CL component should be able to open while ES is stale")
    print("partial stale exact-source component handling ok")


def verify_weekend_next_session_does_not_skip_monday() -> None:
    bot = RuntimeBot()
    now_ny = pd.Timestamp("2026-07-05 00:08:00", tz="UTC").tz_convert(live.NY).to_pydatetime()
    session_open = bot._next_session_open(now_ny)
    got = session_open.astimezone(live.TBILISI).strftime("%a %H:%M")
    _assert(got == "Mon 17:00", f"weekend next session should be Monday 17:00 Tbilisi, got {got}")
    print("weekend next-session calendar ok")


def verify_exact_r_sizing_and_cost_close() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    sig = {
        "key": "ES_VWAP3",
        "symbol": "ES=F",
        "label": "MES",
        "strat": "test",
        "side": "long",
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "note": "verify",
    }
    cur = pd.Series({"dt_utc": base, "dt_ny": base.tz_convert(live.NY)})
    bot._open(sig, cur)
    pos = bot.pos["ES_VWAP3"]
    _assert(abs(pos.qty - 40.0) < 1e-9, f"exact ES qty mismatch: {pos.qty}")
    _assert(abs(pos.cost_usd - 110.0) < 1e-9, f"modeled cost mismatch: {pos.cost_usd}")
    _assert(abs(bot.balance - live.START_BALANCE) < 1e-9, "cost should not book at open")

    target_bar = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "high": 102.25,
        "low": 99.5,
        "close": 102.0,
    })
    bot._manage("ES_VWAP3", target_bar)
    _assert(len(bot.history) == 1, "target close should create one history row")
    hist = bot.history[0]
    _assert(hist["gross_exit_pnl"] == 400.0, f"gross mismatch: {hist}")
    _assert(hist["cost"] == 110.0, f"cost mismatch: {hist}")
    _assert(hist["pnl"] == 290.0, f"net pnl mismatch: {hist}")
    _assert(abs(bot.balance - (live.START_BALANCE + 290.0)) < 1e-9, "balance should book net pnl")
    print("exact-R sizing and close-cost accounting ok")


def verify_restart_catchup_exit() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    pos = live.LucidPos(
        id="t",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()) - 1,
        partial_done=False,
        realized=0.0,
        note="verify",
    )
    bot.pos[pos.key] = pos
    rows = pd.DataFrame([
        {
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "high": 100.2,
            "low": 99.8,
            "close": 100.1,
        },
        {
            "dt_utc": base + pd.Timedelta(minutes=3),
            "dt_ny": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY),
            "high": 100.1,
            "low": 98.8,
            "close": 99.0,
        },
        {
            "dt_utc": base + pd.Timedelta(minutes=6),
            "dt_ny": (base + pd.Timedelta(minutes=6)).tz_convert(live.NY),
            "high": 103.0,
            "low": 98.0,
            "close": 102.0,
        },
    ])
    bot._manage_pending("ES_VWAP3", rows)
    _assert(len(bot.history) == 1, "catch-up should close once")
    _assert(bot.history[0]["reason"] == "stop", f"catch-up should honor first missed stop: {bot.history[0]}")
    _assert("ES_VWAP3" not in bot.pos, "position should be closed after catch-up stop")
    print("restart catch-up exit ok")


def verify_restart_tick_catches_up_open_position() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = base.tz_convert(live.NY).date()
    bot.day_key = str(day)
    pos = live.LucidPos(
        id="rt",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test restart tick",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()),
        last_close=100.1,
        last_day=str(day),
        partial_done=False,
        realized=0.0,
        note="verify restart tick",
    )
    bot.pos[pos.key] = pos
    bot._last_bar_ts = {}
    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            {
                "dt_utc": base,
                "dt_ny": base.tz_convert(live.NY),
                "day": day,
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.1,
                "volume": 1.0,
            },
            {
                "dt_utc": base + pd.Timedelta(minutes=3),
                "dt_ny": (base + pd.Timedelta(minutes=3)).tz_convert(live.NY),
                "day": day,
                "open": 100.1,
                "high": 100.2,
                "low": 98.8,
                "close": 99.0,
                "volume": 1.0,
            },
        ])
    }
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "live tick should catch up an open position after restart")
    _assert(bot.history and bot.history[0]["reason"] == "stop", f"restart tick should honor stop: {bot.history}")
    print("restart tick open-position catch-up ok")


def verify_final_bar_tp1_closes_eod() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 20:57:00", tz="UTC")
    sig = {
        "key": "ES_VWAP3",
        "symbol": "ES=F",
        "label": "MES",
        "strat": "test",
        "side": "long",
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "note": "verify final bar",
    }
    cur = pd.Series({"dt_utc": base, "dt_ny": base.tz_convert(live.NY)})
    bot._open(sig, cur)
    final_bar = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "high": 101.1,
        "low": 99.5,
        "close": 100.5,
    })
    bot._manage("ES_VWAP3", final_bar)
    _assert("ES_VWAP3" not in bot.pos, "final-bar TP1 must still close runner at EOD")
    _assert(len(bot.history) == 1, "final-bar TP1/EOD should create one final history row")
    hist = bot.history[0]
    _assert(hist["reason"] == "eod", f"final-bar TP1 should finish as eod: {hist}")
    _assert(hist["gross_exit_pnl"] == 150.0, f"gross final-bar pnl mismatch: {hist}")
    _assert(hist["pnl"] == 40.0, f"net final-bar pnl mismatch after cost: {hist}")
    print("final-bar TP1 EOD close ok")


def verify_wall_clock_eod_closes_when_feed_stops() -> None:
    bot = RuntimeBot()
    bot._enforce_live_open_guard = True
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    day = base.tz_convert(live.NY).date()
    bot.day_key = str(day)
    pos = live.LucidPos(
        id="stale_eod",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test stale feed eod",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()),
        last_close=100.5,
        last_day=str(day),
        partial_done=False,
        realized=0.0,
        note="verify stale feed eod",
    )
    bot.pos[pos.key] = pos
    bot._df = {}
    bot._open_guard_now_utc = lambda: pd.Timestamp("2026-07-06 21:00:44+00:00")
    bot._tick()
    _assert("ES_VWAP3" in bot.pos, "position should not close before forced-flat grace deadline")

    bot._open_guard_now_utc = lambda: pd.Timestamp("2026-07-06 21:00:45+00:00")
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "stopped feed must not leave position open past forced-flat deadline")
    _assert(bot.history and bot.history[0]["reason"] == "eod", f"stale-feed close should be eod: {bot.history}")
    _assert(bot.history[0]["exit"] == 100.5, f"stale-feed EOD should use last known close: {bot.history}")
    print("wall-clock stale-feed EOD close ok")


def verify_stale_feed_eod_close_does_not_pollute_new_day_pnl() -> None:
    bot = RuntimeBot()
    bot._enforce_live_open_guard = True
    prior = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    next_day = pd.Timestamp("2026-07-07 13:00:00", tz="UTC")
    prior_day = str(prior.tz_convert(live.NY).date())
    current_day = str(next_day.tz_convert(live.NY).date())
    bot.day_key = current_day
    bot.day_pnl = -250.0
    pos = live.LucidPos(
        id="stale_new_day",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test stale feed new day",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(prior.timestamp()),
        best=100.0,
        last_managed_bar=int(prior.timestamp()),
        last_close=99.5,
        last_day=prior_day,
        partial_done=False,
        realized=0.0,
        note="verify stale feed new day",
    )
    bot.pos[pos.key] = pos
    bot._open_guard_now_utc = lambda: next_day + pd.Timedelta(minutes=1)
    bot._df = {
        "NQ_VWAP3": pd.DataFrame([{
            "dt_utc": next_day,
            "dt_ny": next_day.tz_convert(live.NY),
            "day": next_day.tz_convert(live.NY).date(),
            "open": 200.0,
            "high": 200.0,
            "low": 200.0,
            "close": 200.0,
            "volume": 1.0,
        }])
    }
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "missing old component feed should still force EOD close")
    _assert(bot.day_key == current_day, f"current day should be restored after stale EOD close: {bot.day_key}")
    _assert(bot.day_pnl == -250.0, f"old EOD close must not pollute current day pnl: {bot.day_pnl}")
    _assert(bot.history and bot.history[0]["reason"] == "eod", f"stale new-day close should be eod: {bot.history}")
    print("stale-feed EOD close new-day accounting ok")


def verify_day_change_closes_prior_day() -> None:
    bot = RuntimeBot()
    base = pd.Timestamp("2026-07-06 20:12:00", tz="UTC")
    sig = {
        "key": "ES_VWAP3",
        "symbol": "ES=F",
        "label": "MES",
        "strat": "test",
        "side": "long",
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "note": "verify day change",
    }
    cur = pd.Series({"dt_utc": base, "dt_ny": base.tz_convert(live.NY), "day": base.tz_convert(live.NY).date()})
    bot._open(sig, cur)
    last_bar = pd.Series({
        "dt_utc": base,
        "dt_ny": base.tz_convert(live.NY),
        "day": base.tz_convert(live.NY).date(),
        "high": 100.4,
        "low": 99.6,
        "close": 100.5,
    })
    bot._manage("ES_VWAP3", last_bar)
    _assert("ES_VWAP3" in bot.pos, "position should remain open before scheduled flat if no later bar exists")
    next_day = base + pd.Timedelta(days=1)
    next_bar = pd.Series({
        "dt_utc": next_day,
        "dt_ny": next_day.tz_convert(live.NY),
        "day": next_day.tz_convert(live.NY).date(),
        "high": 105.0,
        "low": 95.0,
        "close": 104.0,
    })
    bot._manage("ES_VWAP3", next_bar)
    _assert("ES_VWAP3" not in bot.pos, "new trading day must close prior-day position first")
    hist = bot.history[0]
    _assert(hist["reason"] == "eod", f"day-change close should be eod: {hist}")
    _assert(hist["exit"] == 100.5, f"day-change close should use prior close, not next bar: {hist}")
    print("day-change prior close EOD ok")


def verify_lagging_component_position_is_managed() -> None:
    class CaptureBookBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.book_day_keys = []

        def _book(self, pnl: float):
            self.book_day_keys.append(self.day_key)
            super()._book(pnl)

    bot = CaptureBookBot()
    bot._primed_keys = set(live.COMPONENTS)
    base = pd.Timestamp("2026-07-06 20:54:00", tz="UTC")
    prior_day_key = str(base.tz_convert(live.NY).date())
    bot.day_key = prior_day_key
    pos = live.LucidPos(
        id="lag",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test lagging component",
        side="long",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=99.0,
        stop0=99.0,
        tp1=101.0,
        target=102.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(base.timestamp()),
        best=100.0,
        last_managed_bar=int(base.timestamp()) - 1,
        last_close=100.0,
        last_day=str(base.tz_convert(live.NY).date()),
        partial_done=False,
        realized=0.0,
        note="verify lagging component",
    )
    bot.pos[pos.key] = pos
    es_final = base + pd.Timedelta(minutes=3)
    next_day = base + pd.Timedelta(days=1)
    bot._df = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": es_final,
            "dt_ny": es_final.tz_convert(live.NY),
            "day": es_final.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 100.4,
            "low": 99.6,
            "close": 100.25,
            "volume": 1.0,
        }]),
        "NQ_VWAP3": pd.DataFrame([{
            "dt_utc": next_day,
            "dt_ny": next_day.tz_convert(live.NY),
            "day": next_day.tz_convert(live.NY).date(),
            "open": 200.0,
            "high": 200.0,
            "low": 200.0,
            "close": 200.0,
            "volume": 1.0,
        }]),
    }
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "lagging component position should still be managed and closed")
    _assert(bot.history and bot.history[0]["reason"] == "eod", f"lagging close should be eod: {bot.history}")
    _assert(
        bot.book_day_keys == [prior_day_key],
        f"lagging EOD close should book before day rollover: {bot.book_day_keys}",
    )
    print("lagging component position management ok")


def verify_lagging_eod_close_allows_new_day_signal() -> None:
    class LagThenSignalBot(RuntimeBot):
        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            if key != "ES_VWAP3":
                return None
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test lag close then signal",
                "side": "long",
                "entry": 105.0,
                "stop": 104.0,
                "target": 107.0,
                "note": "verify lag close allows new signal",
                "spent": False,
            }

    bot = LagThenSignalBot()
    prior = pd.Timestamp("2026-07-06 20:12:00", tz="UTC")
    next_day = pd.Timestamp("2026-07-07 13:45:00", tz="UTC")
    bot.day_key = str(prior.tz_convert(live.NY).date())
    pos = live.LucidPos(
        id="lag2",
        key="ES_VWAP3",
        symbol="ES=F",
        label="MES",
        strat="test prior carry",
        side="short",
        qty=40.0,
        qty0=40.0,
        entry=100.0,
        stop=101.0,
        stop0=101.0,
        tp1=99.0,
        target=98.0,
        r_points=1.0,
        micro_pv=5.0,
        tick=0.25,
        risk_usd=200.0,
        cost_usd=0.0,
        opened_at=0.0,
        opened_bar=int(prior.timestamp()),
        best=100.0,
        last_managed_bar=int(prior.timestamp()) - 1,
        last_close=100.0,
        last_day=str(prior.tz_convert(live.NY).date()),
        partial_done=False,
        realized=0.0,
        note="verify carry",
    )
    bot.pos[pos.key] = pos
    bot._last_bar_ts = {"ES_VWAP3": int((prior - pd.Timedelta(minutes=3)).timestamp())}
    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            {
                "dt_utc": prior,
                "dt_ny": prior.tz_convert(live.NY),
                "day": prior.tz_convert(live.NY).date(),
                "open": 100.0,
                "high": 100.4,
                "low": 99.6,
                "close": 100.5,
                "volume": 1.0,
            },
            {
                "dt_utc": next_day,
                "dt_ny": next_day.tz_convert(live.NY),
                "day": next_day.tz_convert(live.NY).date(),
                "open": 105.0,
                "high": 105.5,
                "low": 104.5,
                "close": 105.25,
                "volume": 1.0,
            },
        ])
    }
    bot._tick()
    _assert(bot.history and bot.history[0]["reason"] == "eod", f"prior carry should close EOD: {bot.history}")
    _assert("ES_VWAP3" in bot.pos, "new-day signal should open after prior carry EOD close")
    _assert(
        bot.pos["ES_VWAP3"].strat == "test lag close then signal",
        f"wrong open after lag close: {bot.pos['ES_VWAP3']}",
    )
    print("lagging EOD close allows new-day signal ok")


def verify_new_entry_events_are_chronological() -> None:
    class EventOrderBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.events = []

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            self.events.append((key, str(pd.Timestamp(today.iloc[-1]["dt_utc"]))))
            return None

    bot = EventOrderBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")

    def row(dt, px):
        return {
            "dt_utc": dt,
            "dt_ny": dt.tz_convert(live.NY),
            "day": dt.tz_convert(live.NY).date(),
            "open": px,
            "high": px + 1.0,
            "low": px - 1.0,
            "close": px,
            "volume": 1.0,
        }

    bot._last_bar_ts = {
        "ES_VWAP3": int((base - pd.Timedelta(minutes=3)).timestamp()),
        "CL_VWAP5": int((base - pd.Timedelta(minutes=3)).timestamp()),
    }
    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            row(base + pd.Timedelta(minutes=3), 100.0),
            row(base + pd.Timedelta(minutes=6), 101.0),
        ]),
        "CL_VWAP5": pd.DataFrame([
            row(base + pd.Timedelta(minutes=5), 70.0),
        ]),
    }
    bot._tick()
    expected = [
        ("ES_VWAP3", "2026-07-06 13:03:00+00:00"),
        ("CL_VWAP5", "2026-07-06 13:05:00+00:00"),
        ("ES_VWAP3", "2026-07-06 13:06:00+00:00"),
    ]
    _assert(bot.events == expected, f"new live bars should be processed chronologically: {bot.events}")
    print("new entry event chronological ordering ok")


def verify_entry_management_does_not_look_ahead() -> None:
    class NoLookaheadBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.manage_through = []

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            if key == "ES_VWAP3" and len(today) == 1:
                return {
                    "key": key,
                    "symbol": "ES=F",
                    "label": "MES",
                    "strat": "test no lookahead",
                    "side": "long",
                    "entry": 100.0,
                    "stop": 99.0,
                    "target": 102.0,
                    "note": "verify no lookahead",
                    "spent": False,
                }
            return None

        def _manage_pending(self, key: str, d: pd.DataFrame, through_ts=None):
            self.manage_through.append(str(pd.Timestamp(through_ts)) if through_ts is not None else None)
            return super()._manage_pending(key, d, through_ts=through_ts)

    bot = NoLookaheadBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")

    def row(dt, high, low, close):
        return {
            "dt_utc": dt,
            "dt_ny": dt.tz_convert(live.NY),
            "day": dt.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
        }

    bot._last_bar_ts = {"ES_VWAP3": int((base - pd.Timedelta(minutes=3)).timestamp())}
    first = base + pd.Timedelta(minutes=3)
    second = base + pd.Timedelta(minutes=6)
    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            row(first, 100.5, 99.5, 100.2),
            row(second, 102.5, 100.0, 102.0),
        ])
    }
    bot._tick()
    _assert(
        bot.manage_through[:2] == [str(first), str(second)],
        f"entry management should advance one event at a time: {bot.manage_through}",
    )
    _assert(bot.history and bot.history[0]["reason"] == "target", f"second bar should close target: {bot.history}")
    print("entry management no-lookahead ok")


def verify_spent_signal_after_pause_marks_fired_without_open() -> None:
    class SpentSignalBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.signal_calls = []

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            self.signal_calls.append((key, len(today)))
            if key != "ES_VWAP3":
                return None
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test spent pause",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify spent pause",
                "spent": len(today) > 1,
            }

    bot = SpentSignalBot()
    base = pd.Timestamp("2026-07-06 13:00:00", tz="UTC")
    bot.day_key = str(base.tz_convert(live.NY).date())

    def row(dt, px):
        return {
            "dt_utc": dt,
            "dt_ny": dt.tz_convert(live.NY),
            "day": dt.tz_convert(live.NY).date(),
            "open": px,
            "high": px + 1.0,
            "low": px - 1.0,
            "close": px,
            "volume": 1.0,
        }

    first = base + pd.Timedelta(minutes=3)
    second = base + pd.Timedelta(minutes=6)
    bot._last_bar_ts = {"ES_VWAP3": int(first.timestamp())}
    bot._last_bar_sig = {"ES_VWAP3": (int(first.timestamp()), 101.0, 99.0, 100.0, 1.0)}
    bot._df = {
        "ES_VWAP3": pd.DataFrame([
            row(first, 100.0),
            row(second, 100.5),
        ])
    }
    bot._tick()
    fired = f"{bot.day_key}:ES_VWAP3"
    _assert("ES_VWAP3" not in bot.pos, "spent signal after pause must not open late")
    _assert(fired in bot.fired_keys, f"spent signal should mark component fired: {bot.fired_keys}")
    _assert(bot.signal_calls == [("ES_VWAP3", 2)], f"spent signal should be evaluated on latest context: {bot.signal_calls}")
    print("spent signal after pause fired-without-open ok")


def verify_paused_bar_is_not_replayed_after_enable() -> None:
    class PausedBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.signal_calls = 0

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            self.signal_calls += 1
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test paused no replay",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify paused no replay",
                "spent": False,
            }

    bot = PausedBot()
    bot.enabled = False
    base = pd.Timestamp("2026-07-06 13:03:00", tz="UTC")
    bot._last_bar_ts = {"ES_VWAP3": int((base - pd.Timedelta(minutes=3)).timestamp())}
    bot._df = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "day": base.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }])
    }
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "paused bot must not open")
    _assert(bot.signal_calls == 0, "paused bot should mark bar processed before signal evaluation")
    bot.enabled = True
    bot._tick()
    _assert("ES_VWAP3" not in bot.pos, "same paused bar must not replay after enable")
    _assert(bot.signal_calls == 0, f"same paused bar should not be re-evaluated: {bot.signal_calls}")
    print("paused bar no-replay after enable ok")


def verify_same_bar_revision_cannot_duplicate_open() -> None:
    class RevisionBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self.open_calls = 0

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test revision duplicate",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify same-bar duplicate",
                "spent": False,
            }

        def _open(self, sig: dict, cur: pd.Series):
            before = set(self.pos)
            super()._open(sig, cur)
            if sig.get("key") in self.pos and sig.get("key") not in before:
                self.open_calls += 1

    bot = RevisionBot()
    base = pd.Timestamp("2026-07-06 13:03:00", tz="UTC")

    def frame(high: float, close: float) -> pd.DataFrame:
        return pd.DataFrame([{
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "day": base.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": high,
            "low": 99.0,
            "close": close,
            "volume": 1.0,
        }])

    bot._last_bar_ts = {"ES_VWAP3": int((base - pd.Timedelta(minutes=3)).timestamp())}
    bot._df = {"ES_VWAP3": frame(101.0, 100.5)}
    bot._tick()
    _assert(bot.open_calls == 1, f"first completed bar should open once: {bot.open_calls}")
    bot._df = {"ES_VWAP3": frame(101.5, 100.75)}
    bot._tick()
    _assert(bot.open_calls == 1, f"same timestamp revision must not duplicate open: {bot.open_calls}")
    print("same-bar revision duplicate-open guard ok")


def verify_first_fresh_bar_can_open_after_startup() -> None:
    class FirstBarBot(RuntimeBot):
        def __init__(self):
            super().__init__()
            self._enforce_live_open_guard = False

        def _real_entry_window_ok(self, cur=None, key: str = "") -> bool:
            return True

        def _fresh_entry_bar_ok(self, key: str, cur, now_utc=None) -> bool:
            return True

        def _entry_guard_ok(self, key: str, cur) -> bool:
            return True

        def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame):
            return {
                "key": key,
                "symbol": "ES=F",
                "label": "MES",
                "strat": "test first fresh bar",
                "side": "long",
                "entry": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "note": "verify first bar",
                "spent": False,
            }

    bot = FirstBarBot()
    bot._primed_keys = set()
    base = pd.Timestamp("2026-07-06 13:48:00", tz="UTC")
    bot._df = {
        "ES_VWAP3": pd.DataFrame([{
            "dt_utc": base,
            "dt_ny": base.tz_convert(live.NY),
            "day": base.tz_convert(live.NY).date(),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1.0,
        }])
    }
    bot._tick()
    _assert("ES_VWAP3" in bot.pos, "fresh first bar after startup/reset should not be skipped")
    print("first fresh bar startup open ok")


def verify_reset_clears_priming() -> None:
    bot = RuntimeBot()
    bot._primed_keys = {"ES_VWAP3"}
    bot.prices = {"ES_VWAP3": 101.25}
    bot.reset()
    _assert(bot._primed_keys == set(), "reset should clear primed keys")
    _assert(bot.prices == {}, f"reset should clear stale component prices: {bot.prices}")
    print("reset priming clear ok")


def main() -> int:
    verify_feed_gate()
    verify_lucid_realtime_config()
    verify_lucid_strategy_identity()
    verify_lucid_source_identity()
    verify_local_bridge_loader_and_source_status()
    verify_local_bridge_missing_files_block()
    verify_local_bridge_receiver_writes_atomic_csvs()
    verify_local_bridge_ready_rejects_partial_stale_warning()
    verify_local_bridge_invalid_files_block()
    verify_state_source_match_requires_verified_exact_source()
    verify_exact_realtime_entry_gate_blocks_public_polling()
    verify_warning_alerts_blocked_without_exact_realtime_entry()
    verify_dukascopy_exact_source_helpers()
    verify_dukascopy_live_cache_roundtrip()
    verify_dukascopy_confirmed_empty_hours_roundtrip()
    verify_dukascopy_missing_hour_repair()
    verify_dukascopy_missing_repair_is_throttled()
    verify_dukascopy_confirmed_empty_hours_do_not_starve_repair()
    verify_dukascopy_http_error_is_retryable_not_empty()
    verify_dukascopy_fetch_failure_does_not_confirm_empty_hour()
    verify_feed_block_price_preservation()
    verify_feed_block_forces_expired_position_eod()
    verify_feed_block_open_position_resumes_management()
    verify_continuous_uses_shared_runtime_path()
    verify_pass_and_continuous_target_guards()
    verify_pass_and_continuous_shared_frame_independence()
    verify_open_position_status_has_priority_over_pass_guard()
    verify_daily_loss_blocks_new_entries_not_management()
    verify_daily_loss_guard_alert_semantics()
    verify_vwap_open_does_not_duplicate_warning()
    verify_global_warning_dedup_does_not_consume_local_warning()
    verify_atomic_state_save_roundtrip()
    verify_persisted_open_position_roundtrip_catches_up()
    verify_tradingview_uses_1m_source_for_resampled_components()
    verify_tradingview_row_parser_skips_malformed_rows()
    verify_component_frame_clone_isolates_bots()
    verify_lucid_history_readiness_gate()
    verify_fresh_bar_gate()
    verify_production_bot_enforces_live_open_guard_by_default()
    verify_incomplete_live_bar_is_dropped()
    verify_session_entry_gate()
    verify_completed_final_bar_entry_clock()
    verify_open_wall_clock_guard_blocks_stale_entry()
    verify_tick_blocks_0111_stale_entry()
    verify_next_signal_window_uses_completed_bar_alerts()
    verify_next_signal_window_marks_stale_exact_source()
    verify_exact_source_freshness_details()
    verify_partial_stale_exact_source_does_not_block_fresh_component()
    verify_weekend_next_session_does_not_skip_monday()
    verify_exact_r_sizing_and_cost_close()
    verify_restart_catchup_exit()
    verify_restart_tick_catches_up_open_position()
    verify_final_bar_tp1_closes_eod()
    verify_wall_clock_eod_closes_when_feed_stops()
    verify_stale_feed_eod_close_does_not_pollute_new_day_pnl()
    verify_day_change_closes_prior_day()
    verify_lagging_component_position_is_managed()
    verify_lagging_eod_close_allows_new_day_signal()
    verify_new_entry_events_are_chronological()
    verify_entry_management_does_not_look_ahead()
    verify_spent_signal_after_pause_marks_fired_without_open()
    verify_paused_bar_is_not_replayed_after_enable()
    verify_same_bar_revision_cannot_duplicate_open()
    verify_first_fresh_bar_can_open_after_startup()
    verify_reset_clears_priming()
    print("Lucid runtime verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
