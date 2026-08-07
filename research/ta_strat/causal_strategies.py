"""
causal_strategies.py — strategies written against causal_engine.

Every `decide()` only ever sees COMPLETED bars, and the order it returns starts working
on the NEXT minute. It is structurally impossible to trigger on the bar that created it.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from causal_engine import Order, MARKETS

VWAP_K = 2.5
VWAP_MIN_BARS = 15
TURTLE_LOOKBACK = 10
TURTLE_BUF_TICKS = 8


def make_vwap_fade(market: str):
    """CONTROL STRATEGY: the fade that we proved was fake. Band is built from completed
    bars only; the order then rests for the next bar. If the engine is honest this should
    show ~no edge."""
    _pv, tick, _c, _s = MARKETS[market]

    def decide(prior, today, day):
        if len(today) < VWAP_MIN_BARS:
            return None
        tp = (today["high"].to_numpy() + today["low"].to_numpy() + today["close"].to_numpy()) / 3.0
        v = today["volume"].to_numpy(float)
        v = np.where(v > 0, v, 1.0)
        cum_v = v.sum(); cum_pv = (tp * v).sum(); cum_p2v = (tp * tp * v).sum()
        if cum_v <= 0:
            return None
        vwap = cum_pv / cum_v
        sig = math.sqrt(max(cum_p2v / cum_v - vwap * vwap, 0.0))
        if sig <= 0:
            return None
        up = vwap + VWAP_K * sig
        dn = vwap - VWAP_K * sig
        last = float(today["close"].iloc[-1])
        # rest a limit order at whichever band we are NOT already through
        if last < up:
            return Order(-1, "limit", up, vwap + (VWAP_K + 1.0) * sig, vwap, "vwap_fade_short")
        if last > dn:
            return Order(1, "limit", dn, vwap - (VWAP_K + 1.0) * sig, vwap, "vwap_fade_long")
        return None
    return decide


def make_nr7_breakout(market: str):
    """NR7: yesterday's range is the narrowest of the last 7 sessions -> stop order at
    yesterday's high/low. Levels come from COMPLETED prior sessions, so the signal was
    always causal; the engine now also makes the FILL honest (gap-aware)."""
    _pv, tick, _c, _s = MARKETS[market]

    def decide(prior, today, day):
        if prior.empty:
            return None
        d = prior
        if len(d) < 7:
            return None
        rng = d["range"].to_numpy()
        if rng[-1] >= rng[-7:-1].min():
            return None
        hi = float(d["high"].iloc[-1]); lo = float(d["low"].iloc[-1])
        # one working order per day: take the breakout side we are closest to
        last = float(today["close"].iloc[-1])
        if abs(hi - last) <= abs(last - lo):
            entry = hi + tick; stop = lo - tick
            return Order(1, "stop", entry, stop, entry + 2 * (entry - stop), "nr7_long")
        entry = lo - tick; stop = hi + tick
        return Order(-1, "stop", entry, stop, entry - 2 * (stop - entry), "nr7_short")
    return decide


def make_orb(market: str, open_bars: int = 6):
    """Opening-range breakout: after the first N completed bars, rest stop orders at the
    range extremes. Purely causal by construction."""
    _pv, tick, _c, _s = MARKETS[market]

    def decide(prior, today, day):
        if len(today) < open_bars:
            return None
        rng = today.iloc[:open_bars]
        hi = float(rng["high"].max()); lo = float(rng["low"].min())
        if hi <= lo:
            return None
        last = float(today["close"].iloc[-1])
        if last >= hi or last <= lo:
            return None                      # already broken out; do not chase
        if abs(hi - last) <= abs(last - lo):
            entry = hi + tick; stop = lo - tick
            return Order(1, "stop", entry, stop, entry + 2 * (entry - stop), "orb_long")
        entry = lo - tick; stop = hi + tick
        return Order(-1, "stop", entry, stop, entry - 2 * (stop - entry), "orb_short")
    return decide
