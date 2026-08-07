"""
Compare exact-replay gap signal days across the ten-year CFD proxy and the available
five-minute Yahoo continuous E-mini futures files.

This is only a recent cross-feed signal audit, not a substitute for a ten-year CME
execution backtest.  It intentionally compares dates and directions, not fills.
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_gap_research as G


ROOT = Path(__file__).resolve().parents[2]


def actual_signals(
    path: Path,
    entry_minute: int,
    threshold: float,
) -> dict:
    frame = pd.read_csv(path)
    frame["dt_utc"] = pd.to_datetime(frame["dt_utc"], utc=True)
    ny = frame["dt_utc"].dt.tz_convert(L.NY)
    mask = (ny.dt.time >= time(9, 30)) & (ny.dt.time < time(16, 0))
    frame = frame.loc[mask].copy()
    frame["day"] = ny.loc[mask].dt.date
    frame["clock_minute"] = ny.loc[mask].dt.hour * 60 + ny.loc[mask].dt.minute
    groups = [
        (session, rows.sort_values("dt_utc"))
        for session, rows in frame.groupby("day", sort=True)
    ]
    out = {}
    signal_clock = 9 * 60 + 30 + entry_minute - 5
    for i in range(1, len(groups)):
        session, current = groups[i]
        _, prior = groups[i - 1]
        signal = current[current["clock_minute"] == signal_clock]
        if signal.empty:
            continue
        opening = float(current.iloc[0]["open"])
        prior_close = float(prior.iloc[-1]["close"])
        gap = opening - prior_close
        if gap == 0 or abs(gap) / prior_close < threshold:
            continue
        drive = float(signal.iloc[0]["close"]) - opening
        if np.sign(drive) != -np.sign(gap):
            continue
        out[session] = int(-np.sign(gap))
    return out


def proxy_signals(
    market: str,
    entry_minute: int,
    threshold: float,
    stop_mode: str,
) -> dict:
    cfg = G.GapConfig(
        market,
        "opening_gap",
        entry_minute,
        threshold,
        "reverse",
        "turn",
        stop_mode,
        "rr",
        2.0,
    )
    return {
        trade.day: trade.side
        for trade in G.generate(L.load_days(market), cfg)
    }


def main() -> int:
    cases = [
        (
            "nq",
            ROOT / "research" / "cache" / "apex_NQ_F_5m_60d.csv",
            15,
            0.002,
            "atr",
        ),
        (
            "es",
            ROOT / "research" / "cache" / "apex_ES_F_5m_60d.csv",
            30,
            0.001,
            "extreme",
        ),
    ]
    for market, path, minute, threshold, stop_mode in cases:
        actual = actual_signals(path, minute, threshold)
        proxy = proxy_signals(market, minute, threshold, stop_mode)
        if not actual:
            print(f"{market.upper()}: no actual-futures events")
            continue
        lo, hi = min(actual), max(actual)
        proxy = {
            session: side
            for session, side in proxy.items()
            if lo <= session <= hi
        }
        common = set(actual) & set(proxy)
        direction_agreement = sum(
            actual[session] == proxy[session] for session in common
        )
        print(
            f"{market.upper()} {lo}..{hi}: "
            f"actual={len(actual)} proxy={len(proxy)} common={len(common)} "
            f"direction_agreement={direction_agreement}/{len(common)}"
        )
        print(f"  actual-only: {sorted(set(actual) - set(proxy))}")
        print(f"  proxy-only: {sorted(set(proxy) - set(actual))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
