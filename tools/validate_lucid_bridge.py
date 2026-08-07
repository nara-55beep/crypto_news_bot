"""
Validate optional Lucid local live-bridge CSV files.

This does not trade and does not modify bot state. It checks the disabled-by-
default local bridge path used when LUCID_LIVE_SOURCE=local_bridge.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import lucid_pass_paper as lucid


def _fmt_ts(ts) -> str:
    if ts is None:
        return "-"
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return f"{t.strftime('%Y-%m-%d %H:%M UTC')} / {t.tz_convert(lucid.TBILISI).strftime('%a %H:%M Tbilisi')}"


def _file_report(market: str, invalid_markets: set[str]) -> tuple[bool, str]:
    path = pathlib.Path(lucid._local_bridge_path(market))
    if not path.exists():
        return False, f"{market}: missing {path}"
    if market in invalid_markets:
        return False, f"{market}: invalid OHLC/timestamp data in {path}"
    try:
        df = lucid._duka_normalize_frame(pd.read_csv(path))
    except Exception as e:
        return False, f"{market}: invalid CSV {path} ({type(e).__name__}: {str(e)[:120]})"
    if df.empty:
        return False, f"{market}: empty {path}"
    latest = pd.Timestamp(df.iloc[-1]["dt_utc"])
    return True, f"{market}: rows={len(df)} latest={_fmt_ts(latest)} path={path}"


def main() -> int:
    ok = True
    family = str(getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "") or "")
    print(f"LUCID_LIVE_SOURCE={getattr(config, 'LUCID_LIVE_SOURCE', '')}")
    print(f"LUCID_LOCAL_BRIDGE_DIR={getattr(config, 'LUCID_LOCAL_BRIDGE_DIR', '')}")
    print(f"LUCID_LOCAL_BRIDGE_PREFIX={getattr(config, 'LUCID_LOCAL_BRIDGE_PREFIX', '')}")
    print(f"LUCID_LOCAL_BRIDGE_SOURCE_FAMILY={family or '(not set)'}")
    if family != lucid.BACKTEST_FEED_FAMILY:
        ok = False
        print(f"source: NOT READY - set LUCID_LOCAL_BRIDGE_SOURCE_FAMILY={lucid.BACKTEST_FEED_FAMILY}")
    else:
        print("source: exact-source declaration ok")

    invalid_markets = set(lucid._local_bridge_invalid_markets())
    for market in ("es", "nq", "cl"):
        good, line = _file_report(market, invalid_markets)
        ok = ok and good
        print(line)

    try:
        frames, status = lucid._load_local_bridge_component_data_all()
        print(f"status={status}")
        source_block = lucid._lucid_source_block_reason(status)
        if source_block:
            ok = False
            print(f"source_block={source_block}")
        bridge_block = lucid._lucid_local_bridge_block_reason(status)
        if bridge_block:
            ok = False
            print(f"bridge_block={bridge_block}")
        history_block = lucid._lucid_history_block_reason(frames)
        if history_block:
            ok = False
            print(f"history_block={history_block}")
        details = lucid._lucid_exact_source_freshness_details(frames)
        freshness_block = lucid._lucid_exact_source_freshness_block(frames)
        if freshness_block:
            ok = False
            print(f"freshness_block={freshness_block}")
        for d in details:
            state = "stale" if d.get("stale") else d.get("state", "unknown")
            print(
                f"{d['key']}: latest_closed={d.get('latest_closed_utc')} "
                f"lag={d.get('lag_sec')}s state={state}"
            )
    except Exception as e:
        ok = False
        print(f"bridge_load_error={type(e).__name__}: {str(e)[:160]}")

    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
