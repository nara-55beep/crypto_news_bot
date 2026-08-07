from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite

import numpy as np
import pandas as pd

import config


START_BAL = 100.0
TIMEFRAME = "15m"
POLL_SEC = 60
BAR_SEC = 15 * 60
COST_BPS = 1.5
DAILY_STOP = 0.05


@dataclass(frozen=True)
class PatternBotConfig:
    key: str
    label: str
    source: str
    symbol: str
    leverage: float
    min_families: int
    conflict_ratio: float
    stop_mode: str
    rr: float
    max_bars_mode: str
    cooldown_bars: int
    profile: str


BOT_CONFIGS: dict[str, PatternBotConfig] = {
    "pattern_btc": PatternBotConfig(
        key="pattern_btc",
        label="All-Pattern Consensus BTC (paper optimized)",
        source="binance",
        symbol="BTC/USDT",
        leverage=10.0,
        min_families=4,
        conflict_ratio=0.0,
        stop_mode="median",
        rr=2.0,
        max_bars_mode="median",
        cooldown_bars=48,
        profile="btc",
    ),
    "pattern_esm": PatternBotConfig(
        key="pattern_esm",
        label="All-Pattern Consensus ESM2026 (paper optimized)",
        source="yahoo",
        symbol="ESM26.CME",
        leverage=1.0,
        min_families=3,
        conflict_ratio=0.0,
        stop_mode="median",
        rr=2.0,
        max_bars_mode="max",
        cooldown_bars=12,
        profile="futures",
    ),
    "pattern_nqm": PatternBotConfig(
        key="pattern_nqm",
        label="All-Pattern Consensus NMQ2026 (paper optimized)",
        source="yahoo",
        symbol="NQM26.CME",
        leverage=1.0,
        min_families=2,
        conflict_ratio=0.5,
        stop_mode="median",
        rr=2.0,
        max_bars_mode="max",
        cooldown_bars=12,
        profile="futures",
    ),
    "pattern_cl": PatternBotConfig(
        key="pattern_cl",
        label="All-Pattern Consensus CL1 (paper optimized)",
        source="yahoo",
        symbol="CL=F",
        leverage=1.0,
        min_families=2,
        conflict_ratio=0.0,
        stop_mode="median",
        rr=2.0,
        max_bars_mode="max",
        cooldown_bars=12,
        profile="futures",
    ),
}


def _series(df: pd.DataFrame):
    return (
        df["open"].to_numpy(float),
        df["high"].to_numpy(float),
        df["low"].to_numpy(float),
        df["close"].to_numpy(float),
        pd.Series(df["high"].to_numpy(float)),
        pd.Series(df["low"].to_numpy(float)),
        pd.Series(df["close"].to_numpy(float)),
    )


def _rhi(s_high: pd.Series, lb: int) -> np.ndarray:
    return s_high.shift(1).rolling(lb).max().to_numpy(float)


def _rlo(s_low: pd.Series, lb: int) -> np.ndarray:
    return s_low.shift(1).rolling(lb).min().to_numpy(float)


def _rr_targets(close: np.ndarray, sig: np.ndarray, stops: np.ndarray, rr: float) -> np.ndarray:
    t = np.full(len(close), np.nan)
    long = sig == 1
    short = sig == -1
    t[long] = close[long] + rr * (close[long] - stops[long])
    t[short] = close[short] - rr * (stops[short] - close[short])
    return t


def _add_candidate(cands: list, pat: str, sig: np.ndarray, stops: np.ndarray,
                   targets: np.ndarray, max_bars: int, weight: float) -> None:
    for i in np.flatnonzero(sig):
        if isfinite(stops[i]) and isfinite(targets[i]):
            cands.append((int(i), int(sig[i]), float(stops[i]), int(max_bars), pat, float(weight)))


def _pattern_params(profile: str):
    if profile == "btc":
        return {
            "rectangles": [(48, .010, .070, .003, .006, 1.0, 96, 1.10),
                           (64, .006, .040, .003, .006, 1.0, 96, 1.05)],
            "failed": [(96, .008, .070, .0005, .0008, 96, .85),
                       (192, .004, .040, .001, .0008, 64, .90)],
            "flags": [(32, 16, .008, .50, .0003, 2.0, 64, 1.35),
                      (32, 16, .018, .35, .001, 2.0, 32, 1.15),
                      (48, 12, .008, .35, .001, 1.5, 64, 1.15)],
            "sym": [(192, .45, .0003, "other", 2.0, 96, 1.20),
                    (192, .60, .0003, "mid", 2.0, 96, 1.00),
                    (96, .60, .001, "other", 1.5, 64, .90)],
            "asc": [(96, .006, .003, .001, 1.5, 64, 1.0),
                    (144, .008, .004, .001, 1.5, 96, 1.0)],
            "double": [(64, .003, .006, .002, .003, 2.0, 144, 1.05),
                       (64, .010, .006, .002, .001, 1.5, 144, .95)],
            "triple": [(32, .006, .008, .001, .002, 1.5, 144, .95),
                       (24, .008, .010, .001, .002, 1.5, 96, .90)],
            "hs": [(24, .020, .006, .0003, 2.0, 32, .85),
                   (32, .020, .012, .001, 1.5, 96, .80)],
            "wedge": [(96, .75, .001, 1.5, 96, .90),
                      (144, .70, .001, 1.5, 96, .90)],
            "channel": [(96, .001, 1.2, 64, .65),
                        (144, .001, 1.2, 96, .65)],
            "cup": [(48, .012, .018, .55, .001, 2.0, 144, .85),
                    (32, .015, .015, .60, .001, 1.5, 96, .75)],
            "broad": [(96, .001, 1.0, 64, .55),
                      (144, .001, 1.0, 96, .55)],
        }
    return {
        "rectangles": [(48, .004, .035, .0015, .004, 1.0, 64, 1.10),
                       (64, .006, .050, .002, .006, 1.0, 96, 1.05)],
        "failed": [(48, .004, .040, .0005, .0008, 64, .85),
                   (96, .006, .060, .001, .001, 96, .90)],
        "flags": [(24, 12, .006, .50, .0005, 1.5, 48, 1.25),
                  (32, 16, .010, .50, .0008, 2.0, 64, 1.15)],
        "sym": [(96, .60, .0008, "other", 1.5, 64, 1.0),
                (144, .55, .0008, "other", 2.0, 96, 1.1)],
        "asc": [(96, .006, .002, .0008, 1.5, 64, 1.0)],
        "double": [(32, .004, .006, .001, .002, 1.5, 96, 1.0),
                   (48, .006, .008, .001, .002, 2.0, 120, 1.0)],
        "triple": [(24, .006, .008, .001, .002, 1.5, 96, .90)],
        "hs": [(24, .020, .006, .0008, 1.5, 64, .85)],
        "wedge": [(96, .75, .001, 1.5, 96, .90)],
        "channel": [(96, .001, 1.2, 64, .65)],
        "cup": [(32, .015, .015, .60, .001, 1.5, 96, .75)],
        "broad": [(96, .001, 1.0, 64, .55)],
    }


def _build_candidates(df: pd.DataFrame, profile: str) -> list:
    o, h, l, c, s_h, s_l, s_c = _series(df)
    n = len(df)
    p = _pattern_params(profile)
    cands: list = []

    for lb, minw, maxw, br, stop_in, rr, mb, w in p["rectangles"]:
        hi, lo = _rhi(s_h, lb), _rlo(s_l, lb)
        width = (hi - lo) / c
        base = np.isfinite(hi) & np.isfinite(lo) & (width >= minw) & (width <= maxw)
        long, short = base & (c > hi * (1 + br)), base & (c < lo * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long] = hi[long] * (1 - stop_in); stops[short] = lo[short] * (1 + stop_in)
        _add_candidate(cands, "Rectangle", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, minw, maxw, sweep, pad, mb, w in p["failed"]:
        hi, lo = _rhi(s_h, lb), _rlo(s_l, lb)
        width = (hi - lo) / c
        base = np.isfinite(hi) & np.isfinite(lo) & (width >= minw) & (width <= maxw)
        long = base & (l < lo * (1 - sweep)) & (c > lo) & (c < lo + .55 * (hi - lo))
        short = base & (h > hi * (1 + sweep)) & (c < hi) & (c > lo + .45 * (hi - lo))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long] = l[long] * (1 - pad); stops[short] = h[short] * (1 + pad)
        targets = np.full(n, np.nan); targets[long] = hi[long]; targets[short] = lo[short]
        _add_candidate(cands, "FailedBreakout", sig, stops, targets, mb, w)

    for pole, cons, pct, retrace, br, rr, mb, w in p["flags"]:
        ps, pe = s_c.shift(cons + pole).to_numpy(float), s_c.shift(cons).to_numpy(float)
        chi = s_h.shift(1).rolling(cons).max().to_numpy(float)
        clo = s_l.shift(1).rolling(cons).min().to_numpy(float)
        mv = pe / ps - 1
        up = np.isfinite(mv) & (mv > pct) & (clo > pe - (pe - ps) * retrace)
        dn = np.isfinite(mv) & (mv < -pct) & (chi < pe + (ps - pe) * retrace)
        long, short = up & (c > chi * (1 + br)), dn & (c < clo * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long] = clo[long] * .999; stops[short] = chi[short] * 1.001
        _add_candidate(cands, "FlagPennant", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, contract, br, mode, rr, mb, w in p["sym"]:
        half = lb // 2
        rh = s_h.shift(1).rolling(half).max().to_numpy(float)
        rl = s_l.shift(1).rolling(half).min().to_numpy(float)
        eh = s_h.shift(half + 1).rolling(half).max().to_numpy(float)
        el = s_l.shift(half + 1).rolling(half).min().to_numpy(float)
        base = np.isfinite(eh) & (((rh - rl) / c) < ((eh - el) / c) * contract) & (rh < eh) & (rl > el)
        long, short = base & (c > rh * (1 + br)), base & (c < rl * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        mid = (rh + rl) / 2
        stops = np.full(n, np.nan)
        if mode == "mid":
            stops[long], stops[short] = mid[long], mid[short]
        else:
            stops[long], stops[short] = rl[long] * .999, rh[short] * 1.001
        _add_candidate(cands, "SymTriangle", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, tol, rise, br, rr, mb, w in p["asc"]:
        half = lb // 2
        eh = s_h.shift(half + 1).rolling(half).max().to_numpy(float)
        el = s_l.shift(half + 1).rolling(half).min().to_numpy(float)
        rh = s_h.shift(1).rolling(half).max().to_numpy(float)
        rl = s_l.shift(1).rolling(half).min().to_numpy(float)
        long = np.isfinite(rh) & (np.abs(rh - eh) / ((rh + eh) / 2) < tol) & (rl > el * (1 + rise)) & (c > np.maximum(rh, eh) * (1 + br))
        short = np.isfinite(rl) & (np.abs(rl - el) / ((rl + el) / 2) < tol) & (rh < eh * (1 - rise)) & (c < np.minimum(rl, el) * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long], stops[short] = rl[long] * .999, rh[short] * 1.001
        _add_candidate(cands, "AscDescTriangle", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for seg, tol, depth, br, pad, rr, mb, w in p["double"]:
        ll = s_l.shift(2 * seg + 1).rolling(seg).min().to_numpy(float)
        mh = s_h.shift(seg + 1).rolling(seg).max().to_numpy(float)
        rl = s_l.shift(1).rolling(seg).min().to_numpy(float)
        lh = s_h.shift(2 * seg + 1).rolling(seg).max().to_numpy(float)
        ml = s_l.shift(seg + 1).rolling(seg).min().to_numpy(float)
        rh = s_h.shift(1).rolling(seg).max().to_numpy(float)
        bottom = np.isfinite(ll) & (np.abs(rl - ll) / ((rl + ll) / 2) < tol) & (mh > np.maximum(ll, rl) * (1 + depth))
        top = np.isfinite(lh) & (np.abs(rh - lh) / ((rh + lh) / 2) < tol) & (ml < np.minimum(lh, rh) * (1 - depth))
        long, short = bottom & (c > mh * (1 + br)), top & (c < ml * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long] = np.minimum(ll, rl)[long] * (1 - pad); stops[short] = np.maximum(lh, rh)[short] * (1 + pad)
        _add_candidate(cands, "DoubleTopBottom", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for seg, tol, depth, br, pad, rr, mb, w in p["triple"]:
        l1 = s_l.shift(4 * seg + 1).rolling(seg).min().to_numpy(float)
        h2 = s_h.shift(3 * seg + 1).rolling(seg).max().to_numpy(float)
        l3 = s_l.shift(2 * seg + 1).rolling(seg).min().to_numpy(float)
        h4 = s_h.shift(seg + 1).rolling(seg).max().to_numpy(float)
        l5 = s_l.shift(1).rolling(seg).min().to_numpy(float)
        h1 = s_h.shift(4 * seg + 1).rolling(seg).max().to_numpy(float)
        l2 = s_l.shift(3 * seg + 1).rolling(seg).min().to_numpy(float)
        h3 = s_h.shift(2 * seg + 1).rolling(seg).max().to_numpy(float)
        l4 = s_l.shift(seg + 1).rolling(seg).min().to_numpy(float)
        h5 = s_h.shift(1).rolling(seg).max().to_numpy(float)
        lows, highs = np.vstack([l1, l3, l5]), np.vstack([h1, h3, h5])
        al, ah = lows.mean(axis=0), highs.mean(axis=0)
        tb = np.isfinite(al) & ((lows.max(axis=0) - lows.min(axis=0)) < al * tol) & (np.minimum(h2, h4) > al * (1 + depth))
        tt = np.isfinite(ah) & ((highs.max(axis=0) - highs.min(axis=0)) < ah * tol) & (np.maximum(l2, l4) < ah * (1 - depth))
        long, short = tb & (c > np.maximum(h2, h4) * (1 + br)), tt & (c < np.minimum(l2, l4) * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long] = lows.min(axis=0)[long] * (1 - pad); stops[short] = highs.max(axis=0)[short] * (1 + pad)
        _add_candidate(cands, "TripleTopBottom", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for seg, stol, hgap, br, rr, mb, w in p["hs"]:
        s1h = s_h.shift(4 * seg + 1).rolling(seg).max().to_numpy(float)
        s2l = s_l.shift(3 * seg + 1).rolling(seg).min().to_numpy(float)
        s3h = s_h.shift(2 * seg + 1).rolling(seg).max().to_numpy(float)
        s4l = s_l.shift(seg + 1).rolling(seg).min().to_numpy(float)
        s5h = s_h.shift(1).rolling(seg).max().to_numpy(float)
        s1l = s_l.shift(4 * seg + 1).rolling(seg).min().to_numpy(float)
        s2h = s_h.shift(3 * seg + 1).rolling(seg).max().to_numpy(float)
        s3l = s_l.shift(2 * seg + 1).rolling(seg).min().to_numpy(float)
        s4h = s_h.shift(seg + 1).rolling(seg).max().to_numpy(float)
        s5l = s_l.shift(1).rolling(seg).min().to_numpy(float)
        hs = np.isfinite(s1h) & (s3h > np.maximum(s1h, s5h) * (1 + hgap)) & (np.abs(s1h - s5h) / ((s1h + s5h) / 2) < stol)
        inv = np.isfinite(s1l) & (s3l < np.minimum(s1l, s5l) * (1 - hgap)) & (np.abs(s1l - s5l) / ((s1l + s5l) / 2) < stol)
        short, long = hs & (c < ((s2l + s4l) / 2) * (1 - br)), inv & (c > ((s2h + s4h) / 2) * (1 + br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[short], stops[long] = s5h[short] * 1.003, s5l[long] * .997
        _add_candidate(cands, "HeadShoulders", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, contract, br, rr, mb, w in p["wedge"]:
        third = lb // 3
        h1 = s_h.shift(2 * third + 1).rolling(third).max().to_numpy(float)
        l1 = s_l.shift(2 * third + 1).rolling(third).min().to_numpy(float)
        h3 = s_h.shift(1).rolling(third).max().to_numpy(float)
        l3 = s_l.shift(1).rolling(third).min().to_numpy(float)
        falling = np.isfinite(h1) & (h3 < h1 * .998) & (l3 < l1 * .998) & ((h3 - l3) < (h1 - l1) * contract)
        rising = np.isfinite(h1) & (h3 > h1 * 1.002) & (l3 > l1 * 1.002) & ((h3 - l3) < (h1 - l1) * contract)
        long, short = falling & (c > h3 * (1 + br)), rising & (c < l3 * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long], stops[short] = l3[long] * .999, h3[short] * 1.001
        _add_candidate(cands, "Wedge", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, br, rr, mb, w in p["channel"]:
        half = lb // 2
        eh = s_h.shift(half + 1).rolling(half).max().to_numpy(float)
        el = s_l.shift(half + 1).rolling(half).min().to_numpy(float)
        rh = s_h.shift(1).rolling(half).max().to_numpy(float)
        rl = s_l.shift(1).rolling(half).min().to_numpy(float)
        up = np.isfinite(eh) & (rh > eh * 1.003) & (rl > el * 1.003)
        dn = np.isfinite(eh) & (rh < eh * .997) & (rl < el * .997)
        long, short = up & (c > rh * (1 + br)), dn & (c < rl * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long], stops[short] = rl[long] * .999, rh[short] * 1.001
        _add_candidate(cands, "Channel", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for seg, tol, depth, hfrac, br, rr, mb, w in p["cup"]:
        lhi = s_h.shift(3 * seg + 1).rolling(seg).max().to_numpy(float)
        ml = s_l.shift(2 * seg + 1).rolling(seg).min().to_numpy(float)
        rhi = s_h.shift(seg + 1).rolling(seg).max().to_numpy(float)
        hlo = s_l.shift(1).rolling(seg).min().to_numpy(float)
        llo = s_l.shift(3 * seg + 1).rolling(seg).min().to_numpy(float)
        mh = s_h.shift(2 * seg + 1).rolling(seg).max().to_numpy(float)
        rlo = s_l.shift(seg + 1).rolling(seg).min().to_numpy(float)
        hhi = s_h.shift(1).rolling(seg).max().to_numpy(float)
        res, sup = np.maximum(lhi, rhi), np.minimum(llo, rlo)
        cup = np.isfinite(res) & (np.abs(lhi - rhi) / ((lhi + rhi) / 2) < tol) & (ml < res * (1 - depth)) & (hlo > res - (res - ml) * hfrac)
        inv = np.isfinite(sup) & (np.abs(llo - rlo) / ((llo + rlo) / 2) < tol) & (mh > sup * (1 + depth)) & (hhi < sup + (mh - sup) * hfrac)
        long, short = cup & (c > res * (1 + br)), inv & (c < sup * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        stops = np.full(n, np.nan); stops[long], stops[short] = hlo[long] * .999, hhi[short] * 1.001
        _add_candidate(cands, "CupHandle", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    for lb, br, rr, mb, w in p["broad"]:
        half = lb // 2
        eh = s_h.shift(half + 1).rolling(half).max().to_numpy(float)
        el = s_l.shift(half + 1).rolling(half).min().to_numpy(float)
        rh = s_h.shift(1).rolling(half).max().to_numpy(float)
        rl = s_l.shift(1).rolling(half).min().to_numpy(float)
        broad = np.isfinite(eh) & (rh > eh * 1.002) & (rl < el * .998)
        long, short = broad & (c > rh * (1 + br)), broad & (c < rl * (1 - br))
        sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
        mid = (rh + rl) / 2
        stops = np.full(n, np.nan); stops[long], stops[short] = mid[long], mid[short]
        _add_candidate(cands, "Broadening", sig, stops, _rr_targets(c, sig, stops, rr), mb, w)

    lb = 24
    ph, pl = _rhi(s_h, lb), _rlo(s_l, lb)
    prev, pc = s_c.shift(lb).to_numpy(float), s_c.shift(1).to_numpy(float)
    long = (c < prev * .994) & (l < pl * .9995) & (c > o) & (c > pc)
    short = (c > prev * 1.006) & (h > ph * 1.0005) & (c < o) & (c < pc)
    sig = np.zeros(n, dtype=np.int8); sig[long] = 1; sig[short] = -1
    stops = np.full(n, np.nan); stops[long], stops[short] = l[long] * .9992, h[short] * 1.0008
    _add_candidate(cands, "KeyReversal", sig, stops, _rr_targets(c, sig, stops, 1.5), 32, .45)
    return cands


def _family_groups(cands: list) -> dict[int, list]:
    raw: dict[int, list] = defaultdict(list)
    for x in cands:
        raw[x[0]].append(x)
    groups: dict[int, list] = {}
    for idx, rows in raw.items():
        fams: dict[str, list] = {}
        for _, side, stop, mb, pat, weight in rows:
            fams.setdefault(pat, []).append((side, stop, mb, weight))
        out = []
        for pat, arr in fams.items():
            score = sum(side * weight for side, _, _, weight in arr)
            if abs(score) < .2:
                continue
            side = 1 if score > 0 else -1
            agrees = [a for a in arr if a[0] == side]
            if agrees:
                out.append((pat, side, [a[1] for a in agrees], [a[2] for a in agrees]))
        groups[idx] = out
    return groups


def _event_for_bar(df: pd.DataFrame, cfg: PatternBotConfig, bar_idx: int) -> dict | None:
    if bar_idx < 0 or bar_idx >= len(df):
        return None
    close = df["close"].to_numpy(float)
    groups = _family_groups(_build_candidates(df, cfg.profile))
    fams = groups.get(bar_idx, [])
    long_votes = sum(1 for _, side, _, _ in fams if side == 1)
    short_votes = sum(1 for _, side, _, _ in fams if side == -1)
    agree, disagree = max(long_votes, short_votes), min(long_votes, short_votes)
    if agree < cfg.min_families:
        return None
    if agree and disagree / agree > cfg.conflict_ratio:
        return None
    side_i = 1 if long_votes > short_votes else -1 if short_votes > long_votes else 0
    if not side_i:
        return None
    use = [f for f in fams if f[1] == side_i]
    stops, max_bars, pats = [], [], []
    for pat, _, ss, mbs in use:
        stops.extend(ss)
        max_bars.extend(mbs)
        pats.append(pat)
    price = float(close[bar_idx])
    if side_i == 1:
        valid = [s for s in stops if isfinite(s) and s < price]
        if not valid:
            return None
        stop = max(valid) if cfg.stop_mode == "tight" else float(np.median(valid))
        target = price + cfg.rr * (price - stop)
        side = "long"
    else:
        valid = [s for s in stops if isfinite(s) and s > price]
        if not valid:
            return None
        stop = min(valid) if cfg.stop_mode == "tight" else float(np.median(valid))
        target = price - cfg.rr * (stop - price)
        side = "short"
    mb = max(max_bars) if cfg.max_bars_mode == "max" else int(np.median(max_bars))
    return {
        "side": side,
        "stop": float(stop),
        "target": float(target),
        "max_bars": int(mb),
        "families": sorted(set(pats)),
        "agree": int(agree),
        "disagree": int(disagree),
    }


class AllPatternPaperBot:
    def __init__(self, cfg: PatternBotConfig):
        self.cfg = cfg
        self.state_path = os.path.join(config.DATA_DIR, f"{cfg.key}_state.json")
        self.enabled = True
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.snap: dict = {}
        self.price = 0.0
        self.status = "starting..."
        self._ex = None
        self._last_signal_bar = None
        self.cooldown_until_bar = 0
        self.day_key = ""
        self.day_start_equity = START_BAL
        self.day_paused = False
        self.data_error = ""
        self._load()

    def attach(self, _market=None):
        return None

    def _save(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled,
                    "balance": self.balance,
                    "pos": self.pos,
                    "history": self.history,
                    "log": self.log[:80],
                    "last_signal_bar": self._last_signal_bar,
                    "cooldown_until_bar": self.cooldown_until_bar,
                    "day_key": self.day_key,
                    "day_start_equity": self.day_start_equity,
                    "day_paused": self.day_paused,
                }, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self.state_path):
                return
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self._last_signal_bar = d.get("last_signal_bar")
            self.cooldown_until_bar = int(d.get("cooldown_until_bar", 0))
            self.day_key = str(d.get("day_key", ""))
            self.day_start_equity = float(d.get("day_start_equity", self.balance))
            self.day_paused = bool(d.get("day_paused", False))
            print(f"[{self.cfg.key}] restored: bal ${self.balance:.2f}, {len(self.history)} trades")
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[{self.cfg.key}] {msg}")

    def _ensure_ex(self):
        if self._ex is None:
            import ccxt
            self._ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        return self._ex

    def _fetch_binance(self) -> pd.DataFrame:
        rows = self._ensure_ex().fetch_ohlcv(self.cfg.symbol, TIMEFRAME, limit=1500)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["bar_id"] = (df["ts"].astype("int64") // 1000 // BAR_SEC).astype(int)
        return df[["ts", "bar_id", "open", "high", "low", "close", "volume"]].drop_duplicates("bar_id").reset_index(drop=True)

    def _fetch_yahoo(self) -> pd.DataFrame:
        try:
            import yfinance as yf
            df = yf.download(self.cfg.symbol, period="60d", interval=TIMEFRAME, progress=False,
                             auto_adjust=False, threads=False)
        except ModuleNotFoundError:
            return self._fetch_yahoo_chart()
        if df.empty:
            return self._fetch_yahoo_chart()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [str(c[0]).lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        df = df.rename(columns={"adj close": "adj_close"}).dropna(subset=["open", "high", "low", "close"]).reset_index()
        date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        ts = pd.to_datetime(df[date_col], utc=True)
        df["ts"] = (ts.astype("int64") // 1_000_000).astype("int64")
        df["bar_id"] = (df["ts"] // 1000 // BAR_SEC).astype(int)
        return df[["ts", "bar_id", "open", "high", "low", "close", "volume"]].drop_duplicates("bar_id").reset_index(drop=True)

    def _fetch_yahoo_chart(self) -> pd.DataFrame:
        import requests
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self.cfg.symbol}"
        r = requests.get(
            url,
            params={"range": "60d", "interval": TIMEFRAME, "includePrePost": "true"},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Yahoo {r.status_code}: {r.text[:80]}")
        data = r.json()["chart"]["result"][0]
        q = data["indicators"]["quote"][0]
        df = pd.DataFrame({
            "ts": np.array(data["timestamp"], dtype="int64") * 1000,
            "open": q["open"],
            "high": q["high"],
            "low": q["low"],
            "close": q["close"],
            "volume": q.get("volume") or [0] * len(data["timestamp"]),
        }).dropna(subset=["open", "high", "low", "close"])
        df["bar_id"] = (df["ts"] // 1000 // BAR_SEC).astype(int)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df[["ts", "bar_id", "open", "high", "low", "close", "volume"]].drop_duplicates("bar_id").reset_index(drop=True)

    def _fetch(self) -> pd.DataFrame:
        if self.cfg.source == "binance":
            return self._fetch_binance()
        return self._fetch_yahoo()

    async def manage_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                df = await loop.run_in_executor(None, self._fetch)
                self.data_error = ""
                self._tick(df)
            except Exception as e:
                self.data_error = f"{type(e).__name__}: {str(e)[:90]}"
                self.status = f"data error: {self.data_error}"
                self._save()
            await asyncio.sleep(POLL_SEC)

    def _roll_day(self):
        now_key = time.strftime("%Y-%m-%d", time.gmtime())
        if now_key != self.day_key:
            self.day_key = now_key
            self.day_start_equity = self._equity()
            self.day_paused = False
        if self.day_start_equity > 0 and self._equity() / self.day_start_equity - 1 <= -DAILY_STOP:
            self.day_paused = True

    def _tick(self, df: pd.DataFrame):
        if len(df) < 260:
            self.status = "waiting for enough 15m candles..."
            return
        cur = df.iloc[-1]
        closed_idx = len(df) - 2
        closed = df.iloc[closed_idx]
        self.price = float(cur["close"])
        self._roll_day()
        event = _event_for_bar(df, self.cfg, closed_idx)
        self.snap = {
            "price": round(self.price, 4),
            "last_closed": round(float(closed["close"]), 4),
            "bar": int(closed["bar_id"]),
            "families": event["families"] if event else [],
            "agree": event["agree"] if event else 0,
            "disagree": event["disagree"] if event else 0,
            "signal": event["side"] if event else "",
            "config": {
                "min_families": self.cfg.min_families,
                "conflict_ratio": self.cfg.conflict_ratio,
                "stop_mode": self.cfg.stop_mode,
                "rr": self.cfg.rr,
                "max_bars_mode": self.cfg.max_bars_mode,
            },
        }
        if self.pos:
            self._manage(cur)
        elif self.enabled and event:
            self._maybe_enter(event, closed)
        self._set_status()
        self._save()

    def _set_status(self):
        parts = ["live" if self.enabled else "paused"]
        if self.day_paused:
            parts.append("daily stop active")
        if self.pos:
            parts.append("in trade")
        else:
            parts.append("flat")
        parts.append(f"{self.cfg.min_families}+ family consensus")
        if self.data_error:
            parts.append("data warning")
        self.status = " - ".join(parts)

    def _maybe_enter(self, event: dict, closed):
        bar_id = int(closed["bar_id"])
        if self._last_signal_bar == bar_id:
            return
        if self.day_paused or bar_id < self.cooldown_until_bar:
            return
        self._open(self.price, event, bar_id)

    def _open(self, price: float, event: dict, bar_id: int):
        eq = self._equity()
        if eq <= 0 or price <= 0:
            return
        side = event["side"]
        stop, target = float(event["stop"]), float(event["target"])
        if side == "long" and not (stop < price < target):
            return
        if side == "short" and not (target < price < stop):
            return
        notional = eq * self.cfg.leverage
        qty = notional / price
        entry_fee = notional * COST_BPS / 1e4
        self.balance -= entry_fee
        self.pos = {
            "id": uuid.uuid4().hex[:6],
            "side": side,
            "entry": price,
            "qty": qty,
            "notional": notional,
            "margin": eq,
            "stop": stop,
            "target": target,
            "bar_id": bar_id,
            "max_bars": int(event["max_bars"]),
            "families": event["families"],
            "agree": event["agree"],
            "disagree": event["disagree"],
            "opened_at": time.time(),
            "entry_fee": entry_fee,
        }
        self._last_signal_bar = bar_id
        fam = ", ".join(event["families"][:5])
        self._note(
            f"OPEN {side.upper()} @ {price:,.4f} - {self.cfg.leverage:g}x - "
            f"{event['agree']} families - {fam}",
            "open",
        )

    def _manage(self, cur):
        p = self.pos
        if not p:
            return
        hi, lo, price = float(cur["high"]), float(cur["low"]), float(cur["close"])
        cur_bar = int(cur["bar_id"])
        side = p["side"]
        exit_px = None
        reason = None
        if side == "long":
            if lo <= p["stop"]:
                exit_px, reason = p["stop"], "stop"
            elif hi >= p["target"]:
                exit_px, reason = p["target"], "target"
        else:
            if hi >= p["stop"]:
                exit_px, reason = p["stop"], "stop"
            elif lo <= p["target"]:
                exit_px, reason = p["target"], "target"
        if reason is None and cur_bar - int(p.get("bar_id", cur_bar)) >= int(p.get("max_bars", 0)):
            exit_px, reason = price, "time"
        if reason:
            self._close(float(exit_px), reason, cur_bar)

    def _close(self, price: float, reason: str, cur_bar: int):
        p = self.pos
        if not p:
            return
        side = p["side"]
        gross = p["qty"] * ((price - p["entry"]) if side == "long" else (p["entry"] - price))
        exit_fee = p["notional"] * COST_BPS / 1e4
        pnl = gross - exit_fee
        net_pnl = pnl - p.get("entry_fee", 0.0)
        self.balance = max(0.0, self.balance + pnl)
        risk = abs(p["entry"] - p["stop"]) * p["qty"]
        rec = {
            "side": side,
            "entry": round(p["entry"], 4),
            "exit": round(price, 4),
            "pnl": round(net_pnl, 2),
            "pnl_pct": round(net_pnl / max(p["margin"], 1e-9) * 100.0, 2),
            "rr": round(net_pnl / max(risk, 1e-9), 2),
            "reason": reason,
            "families": p.get("families", []),
            "opened_at": p.get("opened_at", time.time()),
            "closed_at": time.time(),
        }
        self.history.insert(0, rec)
        self.history = self.history[:200]
        self.pos = None
        if net_pnl < 0:
            self.cooldown_until_bar = cur_bar + self.cfg.cooldown_bars
        self._roll_day()
        self._note(
            f"CLOSE {side.upper()} @ {price:,.4f} - {reason} - P&L ${rec['pnl']:+.2f} ({rec['pnl_pct']:+.2f}%)",
            "win" if rec["pnl"] >= 0 else "loss",
        )

    def _equity(self) -> float:
        eq = self.balance
        if self.pos and self.price:
            p = self.pos
            eq += p["qty"] * ((self.price - p["entry"]) if p["side"] == "long" else (p["entry"] - self.price))
        return max(0.0, eq)

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("ENABLED" if self.enabled else "PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self.snap = {}
        self.price = 0.0
        self.status = "reset"
        self._last_signal_bar = None
        self.cooldown_until_bar = 0
        self.day_key = ""
        self.day_start_equity = START_BAL
        self.day_paused = False
        self.data_error = ""
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        pos = None
        if self.pos:
            p = self.pos
            upnl = p["qty"] * ((self.price - p["entry"]) if p["side"] == "long" else (p["entry"] - self.price))
            risk = abs(p["entry"] - p["stop"]) * p["qty"]
            pos = {
                "side": p["side"],
                "entry": round(p["entry"], 4),
                "qty": round(p["qty"], 6),
                "notional": round(p["notional"], 2),
                "stop": round(p["stop"], 4),
                "tp1": round(p["target"], 4),
                "mark": round(self.price, 4),
                "pnl": round(upnl, 2),
                "pnl_pct": round(upnl / max(p["margin"], 1e-9) * 100.0, 2),
                "pnl_R": round(upnl / max(risk, 1e-9), 2),
                "leverage": self.cfg.leverage,
                "families": p.get("families", []),
            }
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "running": True,
            "enabled": self.enabled,
            "key": self.cfg.key,
            "label": self.cfg.label,
            "status": self.status,
            "symbol": self.cfg.symbol,
            "timeframe": TIMEFRAME,
            "leverage": self.cfg.leverage,
            "start_balance": START_BAL,
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100, 2),
            "positions": [pos] if pos else [],
            "trades": len(self.history),
            "wins": wins,
            "history": self.history[:80],
            "log": self.log[:25],
            "snap": self.snap,
            "price": round(self.price, 4) if self.price else None,
            "daily_stop_pct": DAILY_STOP * 100,
            "day_paused": self.day_paused,
            "day_pnl_pct": round((eq / self.day_start_equity - 1) * 100, 2) if self.day_start_equity else 0.0,
            "cooldown_bars": self.cfg.cooldown_bars,
            "data_error": self.data_error,
            "config": {
                "min_families": self.cfg.min_families,
                "conflict_ratio": self.cfg.conflict_ratio,
                "stop_mode": self.cfg.stop_mode,
                "rr": self.cfg.rr,
                "max_bars_mode": self.cfg.max_bars_mode,
                "profile": self.cfg.profile,
            },
        }
