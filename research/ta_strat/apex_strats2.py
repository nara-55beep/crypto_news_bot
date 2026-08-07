"""
apex_strats2.py — a BROAD batch of published, mechanical, NON-ICT day-trading strategies pulled
from the web, all coded to flat-by-session-close (Apex-compatible) and fed to the same R-unit Apex
engine. Each strategy -> list of R-records (pnl_R, mae_R, mfe_R) via walk_trade on 1-min bars.

Families: volatility breakout (Larry Williams, Dual Thrust, Crabel Stretch), narrow-range (NR7,
ID/NR4), mean reversion (opening-gap fade, floor-pivot fade, VWAP 2sigma fade, prior-day-level
sweep fade, first-hour reversion, Turtle Soup false-break, 80-20 reversal, sigma-spike fade).
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_lib import (load_fut, walk_trade, walk_trade_partial, trades_to_records,
                      cal_ordinal, TICK, NY)

MAX_HOLD_MIN = 480   # whole session


def sessions(df):
    """List of (dayord, start_idx, end_idx) per ET calendar day."""
    cal = cal_ordinal(df.index); n = len(cal); out = []; s = 0
    for i in range(1, n):
        if cal[i] != cal[i - 1]:
            out.append((int(cal[s]), s, i - 1)); s = i
    out.append((int(cal[s]), s, n - 1))
    return out


def daily_ohlc(df, sess):
    O, H, L, C = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    o = np.array([O[a] for (_, a, b) in sess]); c = np.array([C[b] for (_, a, b) in sess])
    h = np.array([H[a:b + 1].max() for (_, a, b) in sess]); l = np.array([L[a:b + 1].min() for (_, a, b) in sess])
    return o, h, l, c


def et_hours(df):
    et = df.index.tz_convert(NY)
    return et.hour.values, et.minute.values


def _walk(df, name, trades, manage="none"):
    return trades_to_records(trades, df, name)


def _exec(H, L, C, fi, d, entry, stop, target, end, manage):
    if manage == "partial":
        return walk_trade_partial(H, L, C, fi, d, entry, stop, target, end, p1=1.0, frac=0.5)
    return walk_trade(H, L, C, fi, d, entry, stop, target, end)


# ============================== VOLATILITY BREAKOUT ==============================

def _open_breakout(df, name, kf, stop_mode="opp", targ_R=2.0, buf_ticks=1,
                   day_filter=None, manage="none"):
    """Generic 'today open +/- level' straddle breakout, flat by session close.
    kf(i_day, o,h,l,c arrays) -> level (price distance) or None. stop_mode: 'opp'|'half'."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess); tick = TICK[name]
    trades = []
    for k in range(1, len(sess)):
        if day_filter is not None and not day_filter(k, o, dh, dl, dc):
            continue
        lvl = kf(k, o, dh, dl, dc)
        if lvl is None or lvl <= 0:
            continue
        _, a, b = sess[k]; op = o[k]
        buy = op + lvl; sell = op - lvl
        # scan session for first stop touched
        fi = d = entry = None
        for m in range(a, b + 1):
            if H[m] >= buy:
                fi, d, entry = m, +1, buy; break
            if L[m] <= sell:
                fi, d, entry = m, -1, sell; break
        if fi is None:
            continue
        if stop_mode == "opp":
            stop = sell if d > 0 else buy
        else:  # half
            stop = entry - d * lvl
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + d * targ_R * risk
        end = min(b + 1, fi + MAX_HOLD_MIN)
        r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
        if r:
            xb, rsn, p, ma, mf = r
            trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
    return _walk(df, name, trades)


def lw_breakout(df, name, k=0.5, targ_R=2.0, manage="none"):
    return _open_breakout(df, name, lambda i, o, h, l, c: k * (h[i - 1] - l[i - 1]),
                          stop_mode="half", targ_R=targ_R, manage=manage)


def dual_thrust(df, name, N=4, K=0.5, targ_R=2.0, manage="none"):
    def kf(i, o, h, l, c):
        if i < N:
            return None
        HH = h[i - N:i].max(); LL = l[i - N:i].min()
        HC = c[i - N:i].max(); LC = c[i - N:i].min()
        return K * max(HH - LC, HC - LL)
    return _open_breakout(df, name, kf, stop_mode="opp", targ_R=targ_R, manage=manage)


def stretch_orb(df, name, mult=1.0, targ_R=2.0, nr7_only=False, manage="none"):
    """Crabel stretch = mean(min(H-O,O-L),10) * mult, off today's open."""
    def kf(i, o, h, l, c):
        if i < 10:
            return None
        # noise from prior 10 days
        noise = np.minimum(h[i - 10:i] - o[i - 10:i], o[i - 10:i] - l[i - 10:i])
        return float(noise.mean()) * mult
    df_filter = None
    if nr7_only:
        def df_filter(k, o, h, l, c):
            if k < 7:
                return False
            rng = h[k - 1] - l[k - 1]
            return all(rng < (h[k - 1 - j] - l[k - 1 - j]) for j in range(1, 7))
    return _open_breakout(df, name, kf, stop_mode="opp", targ_R=targ_R, day_filter=df_filter, manage=manage)


def nr7_orb(df, name, targ_R=2.0, manage="none"):
    """Next session: break of the NR7 day's High/Low (NR7 = narrowest range of last 7)."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess); tick = TICK[name]
    trades = []
    for k in range(8, len(sess)):
        rng = dh[k - 1] - dl[k - 1]
        if not all(rng < (dh[k - 1 - j] - dl[k - 1 - j]) for j in range(1, 7)):
            continue
        hi, lo = dh[k - 1], dl[k - 1]
        _, a, b = sess[k]
        fi = d = entry = None
        for m in range(a, b + 1):
            if H[m] >= hi + tick:
                fi, d, entry = m, +1, hi + tick; break
            if L[m] <= lo - tick:
                fi, d, entry = m, -1, lo - tick; break
        if fi is None:
            continue
        stop = lo - tick if d > 0 else hi + tick
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + d * targ_R * risk
        end = min(b + 1, fi + MAX_HOLD_MIN)
        r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
        if r:
            xb, rsn, p, ma, mf = r
            trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
    return _walk(df, name, trades)


# ============================== MEAN REVERSION ==============================

def gap_fade(df, name, sma_n=100, stop_R=1.0, targ_R=0.4, manage="none"):
    """Fade the opening gap toward prior close, regime-filtered by 100-day SMA. Flat by close."""
    H, L, C, O = df["high"].values, df["low"].values, df["close"].values, df["open"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess)
    sma = pd.Series(dc).rolling(sma_n, min_periods=20).mean().values
    trades = []
    for k in range(1, len(sess)):
        if np.isnan(sma[k - 1]):
            continue
        _, a, b = sess[k]; op = o[k]; pc = dc[k - 1]; ph = dh[k - 1]; pl = dl[k - 1]
        if not (pl < op < ph):           # open inside prior range
            continue
        bull = dc[k - 1] > sma[k - 1]
        d = None
        if bull and op < pc:
            d = +1
        elif (not bull) and op > pc:
            d = -1
        if d is None:
            continue
        entry = op; fi = a
        risk = abs(op - pc) if abs(op - pc) > 0 else (ph - pl) * 0.1
        stop = entry - d * stop_R * risk        # default stop = 1x gap
        target = entry + d * targ_R * (ph - pl)  # small target (fraction of prior range)
        # better: target back to prior close
        target = pc
        end = min(b + 1, fi + MAX_HOLD_MIN)
        r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
        if r:
            xb, rsn, p, ma, mf = r
            trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
    return _walk(df, name, trades)


def pivot_fade(df, name, targ="P", manage="none"):
    """Floor pivots from prior day; fade R1->P (short) / S1->P (long). Stop beyond R2/S2."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess)
    trades = []
    for k in range(1, len(sess)):
        P = (dh[k - 1] + dl[k - 1] + dc[k - 1]) / 3
        rng = dh[k - 1] - dl[k - 1]
        R1 = 2 * P - dl[k - 1]; S1 = 2 * P - dh[k - 1]
        R2 = P + rng; S2 = P - rng
        _, a, b = sess[k]
        # arm both fades; first touch triggers
        done = False
        for m in range(a, b + 1):
            if not done and H[m] >= R1:
                d = -1; entry = R1; stop = R2; target = P
                fi = m; done = True
            elif not done and L[m] <= S1:
                d = +1; entry = S1; stop = S2; target = P
                fi = m; done = True
            if done:
                risk = abs(entry - stop)
                if risk <= 0:
                    break
                end = min(b + 1, fi + MAX_HOLD_MIN)
                r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
                if r:
                    xb, rsn, p, ma, mf = r
                    trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
                break
    return _walk(df, name, trades)


def vwap_fade(df, name, k_band=2.0, manage="none"):
    """Session-anchored VWAP +/- k*sigma fade back to VWAP. Flat by close."""
    H, L, C, V = df["high"].values, df["low"].values, df["close"].values, df["volume"].values
    TP = (H + L + C) / 3.0
    sess = sessions(df); trades = []
    for (_, a, b) in sess:
        cum_v = 0.0; cum_pv = 0.0; cum_p2v = 0.0
        in_trade = False
        for m in range(a, b + 1):
            v = V[m] if V[m] > 0 else 1.0
            cum_v += v; cum_pv += TP[m] * v; cum_p2v += TP[m] * TP[m] * v
            if cum_v <= 0:
                continue
            vwap = cum_pv / cum_v
            var = max(cum_p2v / cum_v - vwap * vwap, 0.0); sig = var ** 0.5
            if in_trade or sig <= 0 or (m - a) < 15:
                continue
            up = vwap + k_band * sig; dn = vwap - k_band * sig
            d = None
            if H[m] >= up:
                d = -1; entry = up; stop = vwap + (k_band + 1) * sig
            elif L[m] <= dn:
                d = +1; entry = dn; stop = vwap - (k_band + 1) * sig
            if d is None:
                continue
            target = vwap; risk = abs(entry - stop)
            if risk <= 0:
                continue
            end = min(b + 1, m + MAX_HOLD_MIN)
            r = _exec(H, L, C, m, d, entry, stop, target, end, manage)
            if r:
                xb, rsn, p, ma, mf = r
                trades.append((m, d, entry, stop, target, xb, rsn, p, ma, mf))
                in_trade = True   # one VWAP fade per session
    return _walk(df, name, trades)


def pdh_pdl_fade(df, name, buf_ticks=3, targ_mode="mid", manage="none"):
    """Sweep of prior-day high/low then close back inside -> fade. Flat by close."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess); tick = TICK[name]
    trades = []
    for k in range(1, len(sess)):
        ph, pl, pc = dh[k - 1], dl[k - 1], dc[k - 1]
        mid = (ph + pl) / 2
        _, a, b = sess[k]; busy = False
        for m in range(a, b + 1):
            if busy:
                break
            d = None
            if H[m] > ph and C[m] < ph:
                d = -1; stop = H[m] + buf_ticks * tick
                entry = C[m]; target = mid if targ_mode == "mid" else pc
            elif L[m] < pl and C[m] > pl:
                d = +1; stop = L[m] - buf_ticks * tick
                entry = C[m]; target = mid if targ_mode == "mid" else pc
            if d is None:
                continue
            fi = m + 1
            if fi > b:
                break
            entry = df["open"].values[fi]
            risk = abs(entry - stop)
            if risk <= 0 or not (min(entry, target) < entry < max(entry, target) or True):
                continue
            end = min(b + 1, fi + MAX_HOLD_MIN)
            r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
            if r:
                xb, rsn, p, ma, mf = r
                trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
                busy = True
    return _walk(df, name, trades)


def hourly_reversion(df, name, first_min=60, ext=0.0, manage="none"):
    """Fade a break of the first-hour range back to its midpoint. Flat by close."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); trades = []
    for (_, a, b) in sess:
        hb = min(a + first_min, b)
        rh = H[a:hb].max(); rl = L[a:hb].min(); mid = (rh + rl) / 2
        rng = rh - rl
        if rng <= 0:
            continue
        busy = False
        for m in range(hb, b + 1):
            if busy:
                break
            d = None
            if H[m] >= rh + ext * rng:
                d = -1; entry = rh + ext * rng; stop = rh + rng
            elif L[m] <= rl - ext * rng:
                d = +1; entry = rl - ext * rng; stop = rl - rng
            if d is None:
                continue
            target = mid; risk = abs(entry - stop)
            if risk <= 0:
                continue
            end = min(b + 1, m + MAX_HOLD_MIN)
            r = _exec(H, L, C, m, d, entry, stop, target, end, manage)
            if r:
                xb, rsn, p, ma, mf = r
                trades.append((m, d, entry, stop, target, xb, rsn, p, ma, mf))
                busy = True
    return _walk(df, name, trades)


def turtle_soup(df, name, lookback=20, recency=4, buf_ticks=8, targ_R=2.0, manage="none"):
    """False break of prior N-day extreme -> reversal. Intraday entry, flat by close."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess); tick = TICK[name]
    trades = []
    for k in range(lookback + recency, len(sess)):
        prior_low = dl[k - lookback:k].min(); prior_high = dh[k - lookback:k].max()
        # recency: the extreme being broken should be old (>= recency days ago)
        lo_age = k - 1 - int(np.argmin(dl[k - lookback:k]))   # bars since that low
        hi_age = k - 1 - int(np.argmax(dh[k - lookback:k]))
        _, a, b = sess[k]; busy = False
        for m in range(a, b + 1):
            if busy:
                break
            d = None
            if L[m] < prior_low and lo_age >= recency:
                d = +1; entry = prior_low + buf_ticks * tick; stop = L[a:m + 1].min() - tick
            elif H[m] > prior_high and hi_age >= recency:
                d = -1; entry = prior_high - buf_ticks * tick; stop = H[a:m + 1].max() + tick
            if d is None:
                continue
            # need price to come back through the level (limit on the reversal)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            target = entry + d * targ_R * risk
            # trigger entry when price returns to 'entry' within session
            fi = None
            for mm in range(m, b + 1):
                if (d > 0 and H[mm] >= entry) or (d < 0 and L[mm] <= entry):
                    fi = mm; break
            if fi is None:
                break
            end = min(b + 1, fi + MAX_HOLD_MIN)
            r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
            if r:
                xb, rsn, p, ma, mf = r
                trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
                busy = True
    return _walk(df, name, trades)


def eighty_twenty(df, name, buf_ticks=10, targ_R=2.0, manage="none"):
    """80-20 reversal: yesterday opened top20%/closed bottom20% (or mirror) -> fade back. Flat by close."""
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    sess = sessions(df); o, dh, dl, dc = daily_ohlc(df, sess); tick = TICK[name]
    trades = []
    for k in range(1, len(sess)):
        ry = dh[k - 1] - dl[k - 1]
        if ry <= 0:
            continue
        opened_top = o[k - 1] >= dh[k - 1] - 0.2 * ry
        closed_bot = dc[k - 1] <= dl[k - 1] + 0.2 * ry
        opened_bot = o[k - 1] <= dl[k - 1] + 0.2 * ry
        closed_top = dc[k - 1] >= dh[k - 1] - 0.2 * ry
        _, a, b = sess[k]
        d = None
        if opened_top and closed_bot:        # bullish reversal: buy-stop at yesterday low
            d = +1; level = dl[k - 1]
        elif opened_bot and closed_top:      # bearish: sell-stop at yesterday high
            d = -1; level = dh[k - 1]
        if d is None:
            continue
        # require trade beyond the level then snap back through it
        trig = level - d * buf_ticks * tick   # price pushes beyond by buf, then we enter at level
        fi = None; pushed = False
        for m in range(a, b + 1):
            if not pushed and ((d > 0 and L[m] <= trig) or (d < 0 and H[m] >= trig)):
                pushed = True
            if pushed and ((d > 0 and H[m] >= level) or (d < 0 and L[m] <= level)):
                fi = m; break
        if fi is None:
            continue
        entry = level
        stop = (L[a:fi + 1].min() - tick) if d > 0 else (H[a:fi + 1].max() + tick)
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + d * targ_R * risk
        end = min(b + 1, fi + MAX_HOLD_MIN)
        r = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
        if r:
            xb, rsn, p, ma, mf = r
            trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
    return _walk(df, name, trades)


def sigma_intraday(df, name, tf="60min", z=2.0, win=20, targ_R=1.0, stop_R=1.0, manage="none"):
    """Fade a bar whose return is <= -z sigma (long) / >= +z (short) on resampled RTH bars."""
    from bt_ict_sm_tf import resample
    r = resample(df, tf)
    rc = r["close"].values; rh = r["high"].values; rl = r["low"].values
    ret = np.concatenate([[0.0], np.diff(rc) / rc[:-1]])
    sig = pd.Series(ret).rolling(win, min_periods=10).std().shift(1).values
    z_sp = ret / np.where(sig > 0, sig, np.nan)
    # map resampled bar -> 1m fill at next-bar open
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    idx1 = df.index; n1 = len(C); trades = []
    cal = cal_ordinal(df.index)
    for i in range(win, len(r) - 1):
        if np.isnan(z_sp[i]):
            continue
        d = None
        if z_sp[i] <= -z:
            d = +1
        elif z_sp[i] >= z:
            d = -1
        if d is None:
            continue
        t_next = r.index[i] + (r.index[1] - r.index[0])
        fi = int(idx1.searchsorted(t_next))
        if fi >= n1:
            continue
        # flat by session close of fi's day
        day = cal[fi]; b = fi
        while b + 1 < n1 and cal[b + 1] == day:
            b += 1
        entry = df["open"].values[fi]
        risk = abs(rc[i] - rc[i - 1]) * stop_R
        if risk <= 0:
            continue
        stop = entry - d * risk; target = entry + d * targ_R * risk
        end = min(b + 1, fi + MAX_HOLD_MIN)
        rr = _exec(H, L, C, fi, d, entry, stop, target, end, manage)
        if rr:
            xb, rsn, p, ma, mf = rr
            trades.append((fi, d, entry, stop, target, xb, rsn, p, ma, mf))
    return _walk(df, name, trades)


STRATS = {
    "lw_breakout": lw_breakout, "dual_thrust": dual_thrust, "stretch_orb": stretch_orb,
    "nr7_orb": nr7_orb, "gap_fade": gap_fade, "pivot_fade": pivot_fade, "vwap_fade": vwap_fade,
    "pdh_pdl_fade": pdh_pdl_fade, "hourly_reversion": hourly_reversion, "turtle_soup": turtle_soup,
    "eighty_twenty": eighty_twenty, "sigma_intraday": sigma_intraday,
}


if __name__ == "__main__":
    from apex_lib import apex_eval
    df = load_fut("nq")
    for nm, fn in STRATS.items():
        recs, _ = fn(df, "nq")
        if not recs:
            print(f"{nm:<18} no trades"); continue
        e = float(np.mean([r["pnl_R"] for r in recs]))
        w = float(np.mean([r["pnl_R"] > 0 for r in recs]))
        print(f"{nm:<18} n={len(recs):>4} win={w*100:>3.0f}% expR={e:+.2f}")
