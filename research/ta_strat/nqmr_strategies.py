"""
nqmr_strategies.py — the "NQ 15m Mean Reversion (paper)" bundle, rebuilt causally.

Original (nq_mr_15m_paper.py) components, NY cash session 09:30-15:55, $600 risk:
  * VWAP 2-sigma fade back to session VWAP
  * Turtle Soup false break of the prior 20-session extreme
  * 80-20 reversal from the prior session

THE ORIGINAL'S BIAS: _vwap_signal builds VWAP/sigma from bars[0..last_i] INCLUDING the
current bar, then tests that same bar's high/low against the resulting band. That level
is unknowable until the bar closes, so no order could have been resting at it.

Here the band is built from COMPLETED bars only and the order rests for the next bar,
which is what a real resting limit order can actually do.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from causal_engine import Order, MARKETS

VWAP_K = 2.0
VWAP_MIN_BARS = 15
TURTLE_LOOKBACK = 20
TURTLE_BUF_TICKS = 8
EIGHTY_BUF_TICKS = 10


def make_vwap_fade(market: str = "nq", k: float = VWAP_K, rr_to_vwap: bool = True):
    """Fade a k-sigma stretch back to session VWAP. Band from CLOSED bars only."""
    _pv, tick, _c, _s = MARKETS[market]

    def decide(prior, today, day):
        n = len(today)
        if n < VWAP_MIN_BARS:
            return None
        tp = (today["high"].to_numpy() + today["low"].to_numpy() + today["close"].to_numpy()) / 3.0
        v = today["volume"].to_numpy(float)
        v = np.where(v > 0, v, 1.0)
        cum_v = v.sum()
        if cum_v <= 0:
            return None
        vwap = float((tp * v).sum() / cum_v)
        sig = math.sqrt(max(float(((tp * tp) * v).sum() / cum_v) - vwap * vwap, 0.0))
        if sig <= 0:
            return None
        up, dn = vwap + k * sig, vwap - k * sig
        last = float(today["close"].iat[-1])
        # rest a limit at the band we have not already passed
        if last < up:
            return Order(-1, "limit", up, vwap + (k + 1.0) * sig, vwap, "vwap_fade_short")
        if last > dn:
            return Order(1, "limit", dn, vwap - (k + 1.0) * sig, vwap, "vwap_fade_long")
        return None
    return decide


def make_turtle_soup(market: str = "nq", lookback: int = TURTLE_LOOKBACK, rr: float = 2.0):
    """False break of the prior N-session extreme: price pokes through, then reverses back
    inside. The recovery level is known from PRIOR sessions, so the order can rest there."""
    _pv, tick, _c, _s = MARKETS[market]
    setups = {}

    def decide(prior, today, day):
        if day not in setups:
            setups.clear()
            if len(prior) < lookback:
                setups[day] = None
            else:
                look = prior.tail(lookback)
                setups[day] = (float(look["low"].min()), float(look["high"].max()))
        sp = setups[day]
        if sp is None:
            return None
        prior_low, prior_high = sp
        lo_today = float(today["low"].min())
        hi_today = float(today["high"].max())
        last = float(today["close"].iat[-1])
        # long: swept below the prior low -> rest a buy back above it
        if lo_today < prior_low:
            entry = prior_low + TURTLE_BUF_TICKS * tick
            stop = lo_today - tick
            if entry > stop and last < entry:
                return Order(1, "stop", entry, stop, entry + rr * (entry - stop), "turtle_long")
        if hi_today > prior_high:
            entry = prior_high - TURTLE_BUF_TICKS * tick
            stop = hi_today + tick
            if stop > entry and last > entry:
                return Order(-1, "stop", entry, stop, entry - rr * (stop - entry), "turtle_short")
        return None
    return decide


def make_eighty_twenty(market: str = "nq", rr: float = 2.0):
    """Crabel/Taylor 80-20: prior session opened in the top 20% and closed in the bottom
    20% (or vice versa) -> fade back through the prior extreme. All levels from the
    COMPLETED prior session."""
    _pv, tick, _c, _s = MARKETS[market]
    setups = {}

    def decide(prior, today, day):
        if day not in setups:
            setups.clear()
            if len(prior) < 1:
                setups[day] = None
            else:
                y = prior.iloc[-1]
                rng = float(y["high"]) - float(y["low"])
                if rng <= 0:
                    setups[day] = None
                else:
                    o, c = float(y["open"]), float(y["close"])
                    hi, lo = float(y["high"]), float(y["low"])
                    if o >= hi - 0.2 * rng and c <= lo + 0.2 * rng:
                        setups[day] = (1, lo)        # opened high, closed low -> buy back up
                    elif o <= lo + 0.2 * rng and c >= hi - 0.2 * rng:
                        setups[day] = (-1, hi)       # opened low, closed high -> sell back down
                    else:
                        setups[day] = None
        sp = setups[day]
        if sp is None:
            return None
        d, level = sp
        trig = level - d * EIGHTY_BUF_TICKS * tick   # must first push beyond
        last = float(today["close"].iat[-1])
        pushed = (float(today["low"].min()) <= trig) if d > 0 else (float(today["high"].max()) >= trig)
        if not pushed:
            return None
        entry = level
        stop = (float(today["low"].min()) - tick) if d > 0 else (float(today["high"].max()) + tick)
        r = abs(entry - stop)
        if r <= 0:
            return None
        if (d > 0 and last >= entry) or (d < 0 and last <= entry):
            return None                              # already recovered; do not chase
        return Order(d, "stop", entry, stop, entry + d * rr * r, "8020")
    return decide
