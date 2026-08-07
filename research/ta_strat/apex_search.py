"""
apex_search.py — hunt for a NON-indicator price-action strategy that passes the Apex $50k eval
on ES1!/NQ1!/CL1! over 3 years. No EMA/RSI/MACD/etc. — only price structure:
  * sweep_fade   — liquidity sweep of prior-day / rolling extreme, then close back = fade (the
                   "red X -> red box" reversal). Mean-reversion, high win rate.
  * ib_fade      — opening-range (initial balance) extension rejection -> fade back to IB mid.
  * orb_break    — opening-range breakout (momentum), target = R multiples of the IB range.
  * pdhl_break   — prior-day high/low break-and-go (momentum).
Each emits trades (R-units) -> Apex trailing-DD simulator sweeps per-trade $ risk.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import load_fut, walk_trade, walk_trade_partial, trades_to_records, apex_eval, day_arrays, TICK

MAX_HOLD = 240   # bars (minutes) max in a trade within the session


def session_levels(df, ib_min):
    """Per-bar: prior-day session H/L, today's IB H/L/mid (first ib_min bars), bars-since-day-open."""
    dayid, hh, mm, ndays = day_arrays(df.index)
    H, L = df["high"].values, df["low"].values
    n = len(H)
    # first bar index of each day
    day_first = np.zeros(ndays, np.int64)
    seen = np.full(ndays, False)
    for i in range(n):
        if not seen[dayid[i]]:
            seen[dayid[i]] = True; day_first[dayid[i]] = i
    bars_since_open = (np.arange(n) - day_first[dayid])
    # today running H/L per day, and prior-day final H/L
    day_hi = np.full(ndays, -1e18); day_lo = np.full(ndays, 1e18)
    for i in range(n):
        d = dayid[i]
        if H[i] > day_hi[d]: day_hi[d] = H[i]
        if L[i] < day_lo[d]: day_lo[d] = L[i]
    prior_hi = np.full(n, np.nan); prior_lo = np.full(n, np.nan)
    for i in range(n):
        d = dayid[i]
        if d > 0:
            prior_hi[i] = day_hi[d - 1]; prior_lo[i] = day_lo[d - 1]
    # IB high/low: max/min over first ib_min bars of each day
    ib_hi = np.full(ndays, np.nan); ib_lo = np.full(ndays, np.nan)
    for d in range(ndays):
        a = day_first[d]; b = a + ib_min
        b = min(b, n)
        if d + 1 < ndays:
            b = min(b, day_first[d + 1])
        if b > a:
            ib_hi[d] = H[a:b].max(); ib_lo[d] = L[a:b].min()
    ibh = ib_hi[dayid]; ibl = ib_lo[dayid]
    return dayid, day_first, bars_since_open, prior_hi, prior_lo, ibh, ibl, ndays


def rolling_extreme(H, L, win):
    # max/min of the PRIOR `win` bars (excludes current bar via shift)
    rh = pd.Series(H).rolling(win, min_periods=10).max().shift(1).values
    rl = pd.Series(L).rolling(win, min_periods=10).min().shift(1).values
    return rh, rl


def _emit(df, signals, name, manage="none"):
    """signals: list of (i, d, stop_px, target_px). Fill at next-bar open; walk to exit."""
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(C); out = []; busy_until = -1
    for (i, d, stop, target) in signals:
        fi = i + 1
        if fi >= n or fi <= busy_until:
            continue
        entry = O[fi]
        # validity: stop on correct side
        if (d > 0 and not (stop < entry < target)) or (d < 0 and not (target < entry < stop)):
            continue
        end = min(n, fi + MAX_HOLD)
        if manage == "partial":
            r = walk_trade_partial(H, L, C, fi, d, entry, stop, target, end, p1=1.0, frac=0.5)
        else:
            r = walk_trade(H, L, C, fi, d, entry, stop, target, end)
        if r is None:
            continue
        xb, rsn, pnlR, maeR, mfeR = r
        out.append((fi, d, entry, stop, target, xb, rsn, pnlR, maeR, mfeR))
        busy_until = xb
    return trades_to_records(out, df, name)


# ---------------- strategies: produce signal list (i, d, stop, target) ----------------

def sweep_fade(df, name, ref="pdhl", roll=60, targ_R=1.0, buf_ticks=2, ib_skip=15, manage="none"):
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    n = len(C); tick = TICK[name]
    dayid, day_first, bso, ph, pl, ibh, ibl, _ = session_levels(df, 30)
    if ref == "pdhl":
        rh, rl = ph, pl
    else:
        rh, rl = rolling_extreme(H, L, roll)
    sig = []
    for i in range(1, n):
        if bso[i] < ib_skip:
            continue
        # short: swept above ref high, closed back below
        if not np.isnan(rh[i]) and H[i] > rh[i] and C[i] < rh[i]:
            stop = H[i] + buf_ticks * tick
            entry_guess = C[i]
            target = entry_guess - targ_R * (stop - entry_guess)
            sig.append((i, -1, stop, target))
        elif not np.isnan(rl[i]) and L[i] < rl[i] and C[i] > rl[i]:
            stop = L[i] - buf_ticks * tick
            entry_guess = C[i]
            target = entry_guess + targ_R * (entry_guess - stop)
            sig.append((i, +1, stop, target))
    return _emit(df, sig, name, manage=manage)


def ib_fade(df, name, ib_min=30, ext=0.33, buf_ticks=2, ib_skip=None, manage="none"):
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    n = len(C); tick = TICK[name]
    dayid, day_first, bso, ph, pl, ibh, ibl, _ = session_levels(df, ib_min)
    if ib_skip is None: ib_skip = ib_min
    sig = []
    for i in range(1, n):
        if bso[i] < ib_skip or np.isnan(ibh[i]):
            continue
        ibr = ibh[i] - ibl[i]
        if ibr <= 0:
            continue
        mid = (ibh[i] + ibl[i]) / 2
        # extended above IB high by ext*range, closed back below IB high -> fade to mid
        if H[i] > ibh[i] + ext * ibr and C[i] < ibh[i]:
            stop = H[i] + buf_ticks * tick
            sig.append((i, -1, stop, mid))
        elif L[i] < ibl[i] - ext * ibr and C[i] > ibl[i]:
            stop = L[i] - buf_ticks * tick
            sig.append((i, +1, stop, mid))
    return _emit(df, sig, name, manage=manage)


def orb_break(df, name, ib_min=30, targ_R=1.0, buf_ticks=2):
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    n = len(C); tick = TICK[name]
    dayid, day_first, bso, ph, pl, ibh, ibl, ndays = session_levels(df, ib_min)
    sig = []; done_long = set(); done_short = set()
    for i in range(1, n):
        d = dayid[i]
        if bso[i] < ib_min or np.isnan(ibh[i]):
            continue
        ibr = ibh[i] - ibl[i]
        if ibr <= 0:
            continue
        if C[i] > ibh[i] and d not in done_long:
            done_long.add(d)
            stop = ibl[i] - buf_ticks * tick
            entry_guess = C[i]
            sig.append((i, +1, stop, entry_guess + targ_R * ibr))
        elif C[i] < ibl[i] and d not in done_short:
            done_short.add(d)
            stop = ibh[i] + buf_ticks * tick
            entry_guess = C[i]
            sig.append((i, -1, stop, entry_guess - targ_R * ibr))
    return _emit(df, sig, name)


def pdhl_break(df, name, targ_R=2.0, stop_frac=0.5, buf_ticks=2, ib_skip=15):
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    n = len(C); tick = TICK[name]
    dayid, day_first, bso, ph, pl, ibh, ibl, _ = session_levels(df, 30)
    sig = []; done_long = set(); done_short = set()
    for i in range(1, n):
        d = dayid[i]
        if bso[i] < ib_skip or np.isnan(ph[i]):
            continue
        rng = ph[i] - pl[i]
        if rng <= 0:
            continue
        if C[i] > ph[i] and d not in done_long:
            done_long.add(d)
            stop = ph[i] - stop_frac * rng * 0.0 - (C[i] - ph[i]) - buf_ticks * tick  # stop just below breakout
            stop = ph[i] - buf_ticks * tick
            entry_guess = C[i]
            risk = entry_guess - stop
            sig.append((i, +1, stop, entry_guess + targ_R * risk))
        elif C[i] < pl[i] and d not in done_short:
            done_short.add(d)
            stop = pl[i] + buf_ticks * tick
            entry_guess = C[i]
            risk = stop - entry_guess
            sig.append((i, -1, stop, entry_guess - targ_R * risk))
    return _emit(df, sig, name)


STRATS = {
    "sweep_fade": sweep_fade, "ib_fade": ib_fade, "orb_break": orb_break, "pdhl_break": pdhl_break,
}

CONFIGS = [
    # (strategy, kwargs, label)
    ("sweep_fade", dict(ref="pdhl", targ_R=1.0), "sweepfade pdhl 1R"),
    ("sweep_fade", dict(ref="pdhl", targ_R=1.5), "sweepfade pdhl 1.5R"),
    ("sweep_fade", dict(ref="pdhl", targ_R=2.0), "sweepfade pdhl 2R"),
    ("sweep_fade", dict(ref="roll", roll=60, targ_R=1.0), "sweepfade roll60 1R"),
    ("sweep_fade", dict(ref="roll", roll=120, targ_R=1.0), "sweepfade roll120 1R"),
    ("sweep_fade", dict(ref="roll", roll=60, targ_R=1.5), "sweepfade roll60 1.5R"),
    ("ib_fade", dict(ib_min=30, ext=0.33), "ibfade 30 ext.33"),
    ("ib_fade", dict(ib_min=30, ext=0.5), "ibfade 30 ext.50"),
    ("ib_fade", dict(ib_min=60, ext=0.33), "ibfade 60 ext.33"),
    ("orb_break", dict(ib_min=30, targ_R=1.0), "orb 30 t1xIB"),
    ("orb_break", dict(ib_min=30, targ_R=2.0), "orb 30 t2xIB"),
    ("orb_break", dict(ib_min=60, targ_R=1.0), "orb 60 t1xIB"),
    ("pdhl_break", dict(targ_R=2.0), "pdhl_break 2R"),
    ("pdhl_break", dict(targ_R=3.0), "pdhl_break 3R"),
]

RISKS = [150, 200, 250, 300, 400]


def main():
    import time
    t0 = time.time()
    results = []
    for name in ["es", "nq", "cl"]:
        df = load_fut(name)
        print(f"[{name}] {len(df):,} bars {df.index[0].date()}..{df.index[-1].date()}  ({time.time()-t0:.0f}s)", flush=True)
        for strat, kw, label in CONFIGS:
            recs, ndays = STRATS[strat](df, name, **kw)
            if len(recs) < 20:
                continue
            wins = sum(1 for r in recs if r["pnl_R"] > 0) / len(recs)
            best = None
            for R in RISKS:
                m = apex_eval(recs, R)
                if m is None:
                    continue
                m.update(market=name, label=label, win=wins)
                results.append(m)
                if best is None or m["pass_rate"] > best["pass_rate"]:
                    best = m
            if best:
                print(f"   {label:<22} n={best['n_trades']:>4} win={wins*100:>3.0f}% "
                      f"bestPass={best['pass_rate']*100:>3.0f}%@${best['R']} "
                      f"(med {best['med_days']}d)", flush=True)
    print(f"\n===== TOP CONFIGS BY APEX PASS RATE ({time.time()-t0:.0f}s) =====")
    results.sort(key=lambda r: r["pass_rate"], reverse=True)
    print(f"{'market':<7}{'strategy':<22}{'risk':>6}{'trades':>7}{'win%':>6}{'pass%':>7}{'fails':>7}"
          f"{'cens':>6}{'medDay':>7}{'bestDay%':>9}")
    for r in results[:25]:
        print(f"{r['market']:<7}{r['label']:<22}{('$'+str(r['R'])):>6}{r['n_trades']:>7}{r['win']*100:>5.0f}"
              f"{r['pass_rate']*100:>7.0f}{r['fails']:>7}{r['censored']:>6}"
              f"{(r['med_days'] or 0):>7.0f}{(r['med_bestday_share'] or 0)*100:>8.0f}%")


if __name__ == "__main__":
    main()
