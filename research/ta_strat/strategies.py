"""
strategies.py — signal generators. Each returns a numpy signal array aligned to df
(+1 long / -1 short / 0 flat, decided at bar close) plus a dict of StratParams
overrides (exits/sizing appropriate to that strategy).

Grouped by family. The research verdict (what should survive vs. trap) is noted, but
we test ALL of them honestly and let the data decide.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import indicators as ind


def _blank(df):
    return np.zeros(len(df), dtype=float)


# ============================ TREND / BREAKOUT ===============================
def donchian_breakout(df, n=20, exit_n=10, trend_ema=200, allow_short=True):
    """Turtle-style: long on close>highest(n) when above trend EMA; short mirror.
    Robust-edge candidate (use HTF trend filter)."""
    c = df["close"]
    hh = df["high"].rolling(n).max().shift(1)
    ll = df["low"].rolling(n).min().shift(1)
    ema = ind.ema(c, trend_ema)
    sig = _blank(df)
    long_ = (c > hh) & (c > ema)
    short_ = (c < ll) & (c < ema)
    sig[long_.values] = 1
    if allow_short:
        sig[short_.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=0.0, trail_atr=3.0, allow_short=allow_short)


def supertrend_flip(df, n=10, mult=3.0, trend_ema=200, allow_short=True):
    """Enter on Supertrend direction flip, in HTF-trend direction. Trail via ATR."""
    sdir, _ = ind.supertrend(df["high"], df["low"], df["close"], n, mult)
    ema = ind.ema(df["close"], trend_ema)
    flip_up = (sdir == 1) & (sdir.shift(1) == -1) & (df["close"] > ema)
    flip_dn = (sdir == -1) & (sdir.shift(1) == 1) & (df["close"] < ema)
    sig = _blank(df)
    sig[flip_up.values] = 1
    if allow_short:
        sig[flip_dn.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=0.0, trail_atr=3.0, allow_short=allow_short)


def ema_cross(df, fast=9, slow=21, trend_ema=200, allow_short=True):
    """9/21 EMA cross with 200EMA filter. Research: TRAP intraday (fee farm)."""
    ef = ind.ema(df["close"], fast); es = ind.ema(df["close"], slow)
    et = ind.ema(df["close"], trend_ema)
    up = (ef > es) & (ef.shift(1) <= es.shift(1)) & (df["close"] > et)
    dn = (ef < es) & (ef.shift(1) >= es.shift(1)) & (df["close"] < et)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=1.5, target_atr=3.0, trail_atr=0.0, allow_short=allow_short)


def macd_cross(df, trend_ema=200, allow_short=True):
    """MACD line/signal cross with trend filter. Research: TRAP intraday."""
    line, signal_, _ = ind.macd(df["close"])
    et = ind.ema(df["close"], trend_ema)
    up = (line > signal_) & (line.shift(1) <= signal_.shift(1)) & (df["close"] > et)
    dn = (line < signal_) & (line.shift(1) >= signal_.shift(1)) & (df["close"] < et)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=1.5, target_atr=2.5, trail_atr=0.0, allow_short=allow_short)


def adx_trend(df, adx_min=20, allow_short=True):
    """DI cross gated by ADX>min and 200EMA. Trend-strength filtered momentum."""
    adx_, pdi, mdi = ind.adx(df["high"], df["low"], df["close"], 14)
    et = ind.ema(df["close"], 200)
    up = (pdi > mdi) & (pdi.shift(1) <= mdi.shift(1)) & (adx_ > adx_min) & (df["close"] > et)
    dn = (mdi > pdi) & (mdi.shift(1) <= pdi.shift(1)) & (adx_ > adx_min) & (df["close"] < et)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=0.0, trail_atr=2.5, allow_short=allow_short)


# ============================ OPENING-RANGE BREAKOUT =========================
def orb(df, or_minutes=30, anchor_hour=0, vol_mult=1.5, atr_stop=1.2,
        target_r=2.0, allow_short=True, bar_min=15):
    """Session-anchored ORB with a 'relative-volume' filter (Stocks-in-Play analog).
    Opening range = first `or_minutes` after anchor_hour UTC. Break of range high/low
    triggers entry, but only if that hour's volume > vol_mult * its trailing-14d
    same-hour average. EOD-flat. Research: best breakout pedigree, low frequency."""
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    idx = df.index
    or_bars = max(1, or_minutes // bar_min)
    sig = _blank(df)
    day = idx.normalize()
    hour = idx.hour
    # ---- pass 1: per-day opening-range high/low/VOLUME (all known when OR completes;
    #      using only OR-window volume avoids any intra-hour look-ahead) ----
    or_hi = {}; or_lo = {}; or_vol = {}
    for d, g in df.groupby(day):
        gh = g.index.hour
        sel = g[gh >= anchor_hour]
        if len(sel) <= or_bars:
            continue
        orng = sel.iloc[:or_bars]
        or_hi[d] = orng["high"].max(); or_lo[d] = orng["low"].min()
        or_vol[d] = float(orng["volume"].sum())
    # trailing 14-day average of OR-volume, PRIOR days only (shifted) -> no look-ahead
    ovol = pd.Series(or_vol).sort_index()
    trail_avg = ovol.rolling(14).mean().shift(1)
    # ---- pass 2: fire the breakout after the OR, gated by relative OR-volume ----
    for d, g in df.groupby(day):
        if d not in or_hi:
            continue
        hi = or_hi[d]; lo = or_lo[d]
        tav = trail_avg.get(d, np.nan)
        vol_ok = (not np.isfinite(tav)) or (or_vol[d] > vol_mult * tav)
        if not vol_ok:
            continue
        sel = g[g.index.hour >= anchor_hour]
        rest = sel.iloc[or_bars:]
        pos = df.index.get_indexer(rest.index)
        fired = False
        for k, row in zip(pos, rest.itertuples()):
            if fired:
                break
            if row.close > hi:
                sig[k] = 1; fired = True
            elif allow_short and row.close < lo:
                sig[k] = -1; fired = True
    return sig, dict(stop_atr=atr_stop, target_atr=target_r * atr_stop,
                     trail_atr=0.0, eod_flat=True, allow_short=allow_short)


# ============================ SESSION / TIME-OF-DAY =========================
def session_momentum(df, anchor_hour=0, first_min=30, last_min=30, bar_min=15,
                     allow_short=True):
    """Shen-Urquhart-Wang: sign of the FIRST `first_min` of the UTC session predicts
    the LAST `last_min`. Enter at the start of the last window in that direction,
    EOD-flat. ~1 trade/day. Research: strongest peer-reviewed intraday BTC signal."""
    idx = df.index; c = df["close"]
    day = idx.normalize()
    minutes = idx.hour * 60 + idx.minute
    sig = _blank(df)
    first_end = anchor_hour * 60 + first_min
    last_start = 24 * 60 - last_min
    # per-day first-window return (sign), known by first_end
    grp = df.groupby(day)
    last_bars = max(1, last_min // bar_min)
    for d, g in grp:
        gi = g.index
        gm = gi.hour * 60 + gi.minute
        fw = g[(gm >= anchor_hour * 60) & (gm < first_end)]
        if len(fw) < 1:
            continue
        fret = fw["close"].iloc[-1] / fw["open"].iloc[0] - 1
        if fret == 0:
            continue
        direction = 1 if fret > 0 else (-1 if allow_short else 0)
        if direction == 0:
            continue
        # fire at the bar just before last window begins -> entry next-open == last_start
        lw = g[gm >= last_start]
        if len(lw) == 0:
            continue
        first_lw = lw.index[0]
        k = df.index.get_indexer([first_lw])[0] - 1
        if k >= 0:
            sig[k] = direction
    return sig, dict(stop_atr=3.0, target_atr=0.0, trail_atr=0.0, eod_flat=True,
                     time_stop_bars=last_bars + 1, allow_short=allow_short)


def overnight_long(df, start_hour=22, bar_min=15):
    """Padysak overnight seasonality: long from start_hour to end of UTC day. Long-only."""
    idx = df.index
    sig = _blank(df)
    minutes = idx.hour * 60 + idx.minute
    target = start_hour * 60
    day = idx.normalize()
    for d, g in df.groupby(day):
        gm = g.index.hour * 60 + g.index.minute
        win = g[gm >= target]
        if len(win) == 0:
            continue
        k = df.index.get_indexer([win.index[0]])[0] - 1
        if k >= 0:
            sig[k] = 1
    return sig, dict(stop_atr=3.0, target_atr=0.0, trail_atr=0.0, eod_flat=True,
                     allow_short=False)


def intraday_tsmom(df, lookback=12, hold_bars=12, vwap_filter=True, trend_ema=200,
                   allow_short=True):
    """Generic intraday time-series momentum: if last `lookback`-bar return>0 and
    price>VWAP and >200EMA, go long for `hold_bars`. Captures session drift."""
    c = df["close"]
    mom = c.pct_change(lookback)
    et = ind.ema(c, trend_ema)
    vw = ind.vwap_session(df) if vwap_filter else c * 0
    up = (mom > 0) & (c > et)
    dn = (mom < 0) & (c < et)
    if vwap_filter:
        up = up & (c > vw); dn = dn & (c < vw)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=0.0, trail_atr=0.0,
                     time_stop_bars=hold_bars, allow_short=allow_short)


# ============================ MEAN REVERSION (traps) =========================
def rsi2_reversion(df, buy=10, sell=70, trend_ema=200, allow_short=False):
    """Connors RSI(2): long when RSI2<buy and price>200EMA. Research: TRAP on BTC."""
    r = ind.rsi(df["close"], 2); et = ind.ema(df["close"], trend_ema)
    up = (r < buy) & (df["close"] > et)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        dn = (r > 100 - buy) & (df["close"] < et)
        sig[dn.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=0.0, trail_atr=0.0, time_stop_bars=12,
                     allow_short=allow_short)


def bollinger_fade(df, n=20, k=2.0, allow_short=True):
    """Fade ±k sigma, target the mid band. Research: TRAP intraday."""
    bl, bm, bu = ind.bollinger(df["close"], n, k)
    up = df["close"] < bl
    dn = df["close"] > bu
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=2.0, target_atr=2.0, trail_atr=0.0, time_stop_bars=24,
                     allow_short=allow_short)


# ============================ CANDLESTICK PATTERN ============================
def engulfing_trend(df, trend_ema=50, allow_short=True):
    """Bullish/bearish engulfing in the direction of the 50EMA. Research: weak alone."""
    be = ind.bullish_engulfing(df).astype(bool)
    se = ind.bearish_engulfing(df).astype(bool)
    et = ind.ema(df["close"], trend_ema)
    up = be & (df["close"] > et)
    dn = se & (df["close"] < et)
    sig = _blank(df)
    sig[up.values] = 1
    if allow_short:
        sig[dn.values] = -1
    return sig, dict(stop_atr=1.5, target_atr=2.0, trail_atr=0.0, allow_short=allow_short)


# registry: name -> (callable, default kwargs)
REGISTRY = {
    "donchian20":   (donchian_breakout, dict(n=20, exit_n=10)),
    "donchian55":   (donchian_breakout, dict(n=55, exit_n=20)),
    "supertrend":   (supertrend_flip, dict()),
    "ema_cross":    (ema_cross, dict()),
    "macd_cross":   (macd_cross, dict()),
    "adx_trend":    (adx_trend, dict()),
    "orb":          (orb, dict()),
    "session_mom":  (session_momentum, dict()),
    "overnight":    (overnight_long, dict()),
    "tsmom":        (intraday_tsmom, dict()),
    "rsi2":         (rsi2_reversion, dict()),
    "bb_fade":      (bollinger_fade, dict()),
    "engulfing":    (engulfing_trend, dict()),
}
