"""
Smoke-test Lucid local bridge mode without touching production data.

This writes synthetic current 1m bars into a temporary bridge directory and runs
the same loader/gates used by LUCID_LIVE_SOURCE=local_bridge. It proves the
local bridge plumbing can become READY when a real exact-source producer writes
fresh ES/NQ/CL rows. It does not trade and does not modify bot state.
"""
from __future__ import annotations

import pathlib
import os
import sys
import tempfile

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import lucid_pass_paper as lucid
from tools import lucid_bridge_receiver


def _session_now() -> pd.Timestamp:
    now = pd.Timestamp.now(tz="UTC")
    minute = now.hour * 60 + now.minute
    if lucid.BACKTEST_SESSION_START_UTC <= minute < lucid.BACKTEST_SESSION_END_UTC:
        return now
    day = now.floor("D")
    if minute < lucid.BACKTEST_SESSION_START_UTC:
        day -= pd.Timedelta(days=1)
    for _ in range(8):
        candidate = day + pd.Timedelta(minutes=lucid.BACKTEST_SESSION_START_UTC + 90)
        if candidate.tz_convert(lucid.NY).weekday() < 5 and candidate <= now:
            return candidate
        day -= pd.Timedelta(days=1)
    return now - pd.Timedelta(minutes=90)


def _write_market(store: lucid_bridge_receiver.BridgeStore, market: str,
                  now_utc: pd.Timestamp, start_px: float) -> None:
    start = (now_utc.floor("min") - pd.Timedelta(minutes=90)).floor("min")
    px = start_px
    for i in range(90):
        dt = start + pd.Timedelta(minutes=i)
        px += 0.01
        store.put_bar(market, {
            "dt_utc": dt.isoformat(),
            "open": px,
            "high": px + 0.05,
            "low": px - 0.05,
            "close": px + 0.01,
            "volume": 1.0,
        })


def main() -> int:
    old_dir = getattr(config, "LUCID_LOCAL_BRIDGE_DIR", "")
    old_prefix = getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "")
    old_family = getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
    old_source = getattr(config, "LUCID_LIVE_SOURCE", "")
    old_future = getattr(config, "LUCID_BRIDGE_MAX_FUTURE_SEC", 120)
    old_future_env = os.environ.get("LUCID_BRIDGE_MAX_FUTURE_SEC")
    try:
        with tempfile.TemporaryDirectory() as td:
            config.LUCID_LOCAL_BRIDGE_DIR = td
            config.LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
            config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = lucid.BACKTEST_FEED_FAMILY
            config.LUCID_LIVE_SOURCE = "local_bridge"
            config.LUCID_BRIDGE_MAX_FUTURE_SEC = 259200
            os.environ["LUCID_BRIDGE_MAX_FUTURE_SEC"] = "259200"

            store = lucid_bridge_receiver.BridgeStore()
            now_utc = _session_now()
            _write_market(store, "es", now_utc, 7500.0)
            _write_market(store, "nq", now_utc, 29500.0)
            _write_market(store, "cl", now_utc, 70.0)

            frames, status = lucid._load_local_bridge_component_data_all()
            source_block = lucid._lucid_source_block_reason(status)
            bridge_block = lucid._lucid_local_bridge_block_reason(status)
            history_block = lucid._lucid_history_block_reason(frames)
            freshness_block = lucid._lucid_exact_source_freshness_block(frames, now_utc=now_utc)
            details = lucid._lucid_exact_source_freshness_details(frames, now_utc=now_utc)
            ready_status = store.status(include_ready=True)

            print(f"temp_dir={td}")
            print(f"status={status}")
            print(f"receiver_ready={ready_status.get('ready')} exact_realtime_ready={ready_status.get('exact_realtime_ready')}")
            for detail in details:
                print(
                    f"{detail['key']}: latest_closed={detail.get('latest_closed_utc')} "
                    f"state={detail.get('state')} lag={detail.get('lag_sec')}s"
                )
            problems = [p for p in (source_block, bridge_block, history_block, freshness_block) if p]
            if not ready_status.get("ready"):
                problems.append("receiver /ready did not pass: " + "; ".join(map(str, ready_status.get("problems", []))))
            if problems:
                for p in problems:
                    print(f"block={p}")
                print("SMOKE NOT READY")
                return 1
            print("SMOKE READY")
            return 0
    finally:
        config.LUCID_LOCAL_BRIDGE_DIR = old_dir
        config.LUCID_LOCAL_BRIDGE_PREFIX = old_prefix
        config.LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = old_family
        config.LUCID_LIVE_SOURCE = old_source
        config.LUCID_BRIDGE_MAX_FUTURE_SEC = old_future
        if old_future_env is None:
            os.environ.pop("LUCID_BRIDGE_MAX_FUTURE_SEC", None)
        else:
            os.environ["LUCID_BRIDGE_MAX_FUTURE_SEC"] = old_future_env


if __name__ == "__main__":
    raise SystemExit(main())
