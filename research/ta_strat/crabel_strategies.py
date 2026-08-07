"""
crabel_strategies.py — Toby Crabel's volatility-compression patterns, written against
causal_engine (look-ahead impossible by construction).

From "Day Trading with Short Term Price Patterns and Opening Range Breakout":

  STRETCH   = N-day average of min(open - low, high - open).  Crabel's signature ORB
              places a buy stop at today's open + stretch and a sell stop at open - stretch.
  NR4 / NR7 = today's range is the narrowest of the last 4 / 7 sessions -> next session
              breaks out of it.
  ID/NR4    = inside day AND NR4 (Crabel's highest-conviction compression).
  2BarNR    = the narrowest 2-session combined range in N sessions.
  WS (narrow-spread open) = open near the middle, small prior range.

All levels come from COMPLETED prior sessions plus today's open, so every order can
genuinely be resting before it is triggered.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from causal_engine import Order, MARKETS


def _daycache(fn):
    """The setup depends only on PRIOR sessions, which never change intraday.
    Compute it once per day instead of on every 1-minute decision."""
    cache = {}
    def wrapped(prior, today, day):
        if day not in cache:
            cache.clear()
            cache[day] = fn(prior, day)
        setup = cache[day]
        if setup is None:
            return None
        return setup
    return wrapped


def _stretch(prior: pd.DataFrame, n: int) -> float:
    """Crabel stretch: mean of min(open-low, high-open) over the last n sessions."""
    d = prior.tail(n)
    if len(d) < n:
        return float("nan")
    a = (d["open"] - d["low"]).abs().to_numpy()
    b = (d["high"] - d["open"]).abs().to_numpy()
    return float(np.minimum(a, b).mean())


def _compression_ok(prior: pd.DataFrame, mode: str) -> bool:
    """Is the LAST completed session a compression day?"""
    if mode == "none":
        return True
    rng = prior["range"].to_numpy()
    hi = prior["high"].to_numpy(); lo = prior["low"].to_numpy()
    if mode == "nr4":
        return len(rng) >= 4 and rng[-1] < rng[-4:-1].min()
    if mode == "nr7":
        return len(rng) >= 7 and rng[-1] < rng[-7:-1].min()
    if mode == "id_nr4":
        if len(rng) < 4:
            return False
        inside = hi[-1] < hi[-2] and lo[-1] > lo[-2]
        return inside and rng[-1] < rng[-4:-1].min()
    if mode == "nr2":
        if len(rng) < 5:
            return False
        two = np.maximum(hi[1:], hi[:-1]) - np.minimum(lo[1:], lo[:-1])
        return two[-1] < two[-5:-1].min()
    return True


def make_crabel_orb(market: str, stretch_days: int = 10, compression: str = "none",
                    stop_mode: str = "stretch", rr: float = 2.0):
    """Crabel ORB: stop orders at today's open +/- stretch.
    compression: none | nr4 | nr7 | id_nr4 | nr2   (filter on the PRIOR session)
    stop_mode:   stretch (stop = other side of the stretch band) | opp (prior day extreme)
    """
    _pv, tick, _c, _s = MARKETS[market]

    setups = {}

    def decide(prior, today, day):
        if day not in setups:
            setups.clear()
            if len(prior) < max(stretch_days, 8) or not _compression_ok(prior, compression):
                setups[day] = None
            else:
                st = _stretch(prior, stretch_days)
                if not np.isfinite(st) or st <= 0:
                    setups[day] = None
                else:
                    setups[day] = (st, float(prior["low"].iloc[-1]) - tick,
                                   float(prior["high"].iloc[-1]) + tick)
        sp = setups[day]
        if sp is None:
            return None
        st, plow, phigh = sp
        o = float(today["open"].iat[0])
        last = float(today["close"].iat[-1])
        up, dn = o + st, o - st
        long_stop, short_stop = (dn, up) if stop_mode == "stretch" else (plow, phigh)
        if dn < last < up:
            if (up - last) <= (last - dn):
                return Order(1, "stop", up, long_stop, up + rr * (up - long_stop), "orb_long")
            return Order(-1, "stop", dn, short_stop, dn - rr * (short_stop - dn), "orb_short")
        return None

    return decide


def make_nr_breakout(market: str, n: int = 7, rr: float = 2.0):
    """Break yesterday's range after an NR(n) compression day."""
    _pv, tick, _c, _s = MARKETS[market]
    mode = {4: "nr4", 7: "nr7"}.get(n, "nr7")

    setups = {}

    def decide(prior, today, day):
        if day not in setups:
            setups.clear()
            if len(prior) < n + 1 or not _compression_ok(prior, mode):
                setups[day] = None
            else:
                hi = float(prior["high"].iat[-1]); lo = float(prior["low"].iat[-1])
                setups[day] = (hi, lo) if hi > lo else None
        sp = setups[day]
        if sp is None:
            return None
        hi, lo = sp
        last = float(today["close"].iat[-1])
        if last >= hi + tick or last <= lo - tick:
            return None
        if (hi - last) <= (last - lo):
            e, s_ = hi + tick, lo - tick
            return Order(1, "stop", e, s_, e + rr * (e - s_), f"nr{n}_long")
        e, s_ = lo - tick, hi + tick
        return Order(-1, "stop", e, s_, e - rr * (s_ - e), f"nr{n}_short")

    return decide


def make_id_nr4(market: str, rr: float = 2.0):
    """Crabel's ID/NR4: inside day AND narrowest range of 4 -> break yesterday's range."""
    _pv, tick, _c, _s = MARKETS[market]

    setups = {}

    def decide(prior, today, day):
        if day not in setups:
            setups.clear()
            if len(prior) < 5 or not _compression_ok(prior, "id_nr4"):
                setups[day] = None
            else:
                hi = float(prior["high"].iat[-1]); lo = float(prior["low"].iat[-1])
                setups[day] = (hi, lo) if hi > lo else None
        sp = setups[day]
        if sp is None:
            return None
        hi, lo = sp
        last = float(today["close"].iat[-1])
        if last >= hi + tick or last <= lo - tick:
            return None
        if (hi - last) <= (last - lo):
            e, s_ = hi + tick, lo - tick
            return Order(1, "stop", e, s_, e + rr * (e - s_), "idnr4_long")
        e, s_ = lo - tick, hi + tick
        return Order(-1, "stop", e, s_, e - rr * (s_ - e), "idnr4_short")
    return decide
