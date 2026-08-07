"""
apex_ict.py — the ICT structural setup (liquidity sweep -> MSS -> FVG entry, stop beyond the grab)
as an Apex building block. Setups are DETECTED on the chosen timeframe but each trade is FILLED and
walked on 1-MINUTE bars, so the mae/mfe (and thus the trailing-DD model) are accurate intrabar.
Optional breakeven management. Returns R-unit records for the Apex simulator.
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_ict_sm_tf import resample, pivots, find_fvg
from apex_lib import walk_trade_be, walk_trade_partial, trades_to_records, TICK, NY

LEFT = RIGHT = 5
MSS_WINDOW = 20
ENTRY_EXPIRY_TF = 16     # bars (in TF units) the FVG limit stays live
MAX_HOLD_MIN = 240       # minutes max in a trade


def in_killzone_et(hr):
    return (2 <= hr < 5) or (7 <= hr < 11)


def detect_setups(tf, target_r, killzone, bias_on):
    """Return list of (k_bar, d, entry, stop, target) — k_bar = TF bar after which FVG limit is live."""
    H, L, C = tf["high"].values, tf["low"].values, tf["close"].values
    n = len(C)
    if n < 50:
        return []
    et_hr = tf.index.tz_convert(NY).hour.values if killzone else np.zeros(n, int)
    if bias_on:
        dc = tf["close"].resample("1D").last().dropna()
        ema = dc.ewm(span=20, adjust=False).mean()
        bser = pd.Series(np.where(dc > ema, 1, -1), index=dc.index).shift(1)
        bmap = {d.date(): int(v) for d, v in bser.dropna().items()}
        bias = np.array([bmap.get(d, 0) for d in tf.index.tz_convert(NY).date])
    else:
        bias = np.zeros(n, int)
    ph, pl = pivots(H, L, LEFT, RIGHT)
    last_ph = np.full(n, -1); last_pl = np.full(n, -1); lph = lpl = -1
    for i in range(n):
        j = i - RIGHT
        if j >= 0:
            if ph[j]: lph = j
            if pl[j]: lpl = j
        last_ph[i] = lph; last_pl[i] = lpl
    setups = []; i = RIGHT + 1
    while i < n - 2:
        if killzone and not in_killzone_et(et_hr[i]):
            i += 1; continue
        lphj, lplj = last_ph[i], last_pl[i]
        al = (not bias_on) or bias[i] >= 0
        ash = (not bias_on) or bias[i] <= 0
        setup = None
        if al and lplj >= 0 and L[i] < L[lplj] and C[i] > L[lplj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lphj >= 0 and C[k] > H[lphj]:
                    f = find_fvg(H, L, i, k, "bull")
                    if f:
                        stop = L[i] * (1 - 0.0002); R = f - stop
                        if R > 0: setup = (k, +1, f, stop, f + target_r * R)
                    break
        if setup is None and ash and lphj >= 0 and H[i] > H[lphj] and C[i] < H[lphj]:
            for k in range(i + 1, min(n, i + MSS_WINDOW)):
                if lplj >= 0 and C[k] < L[lplj]:
                    f = find_fvg(H, L, i, k, "bear")
                    if f:
                        stop = H[i] * (1 + 0.0002); R = stop - f
                        if R > 0: setup = (k, -1, f, stop, f - target_r * R)
                    break
        if setup:
            setups.append(setup); i = setup[0] + 1; continue
        i += 1
    return setups


def ict_records(df1m, name, tf_rule, target_r=2.0, killzone=False, bias_on=False, be_at=None, manage="none"):
    tf = resample(df1m, tf_rule)
    setups = detect_setups(tf, target_r, killzone, bias_on)
    if not setups:
        return [], 0
    tfmin = int(pd.Timedelta(tf.index[1] - tf.index[0]).total_seconds() // 60) if len(tf) > 1 else 1
    tfmin = max(tfmin, 1)
    idx1 = df1m.index
    O1, H1, L1, C1 = df1m["open"].values, df1m["high"].values, df1m["low"].values, df1m["close"].values
    n1 = len(C1)
    tf_times = tf.index
    trades = []; busy_until = -1
    for (k, d, entry, stop, target) in setups:
        t_valid = tf_times[k] + pd.Timedelta(minutes=tfmin)   # after MSS bar closes
        start1 = int(idx1.searchsorted(t_valid))
        if start1 >= n1:
            continue
        # limit fill within ENTRY_EXPIRY_TF TF-bars (=*tfmin minutes); stop-out before fill voids it
        win = ENTRY_EXPIRY_TF * tfmin
        fi = None
        for m in range(start1, min(n1, start1 + win)):
            if (d > 0 and L1[m] <= stop) or (d < 0 and H1[m] >= stop):
                break
            if (d > 0 and L1[m] <= entry) or (d < 0 and H1[m] >= entry):
                fi = m; break
        if fi is None or fi <= busy_until:
            continue
        end = min(n1, fi + MAX_HOLD_MIN)
        if manage == "partial":
            r = walk_trade_partial(H1, L1, C1, fi, d, entry, stop, target, end, p1=1.0, frac=0.5)
        else:
            r = walk_trade_be(H1, L1, C1, fi, d, entry, stop, target, end, be_at=be_at)
        if r is None:
            continue
        xb, rsn, pnlR, maeR, mfeR = r
        trades.append((fi, d, entry, stop, target, xb, rsn, pnlR, maeR, mfeR))
        busy_until = xb
    return trades_to_records(trades, df1m, name)


if __name__ == "__main__":
    from apex_lib import load_fut, apex_eval
    df = load_fut("cl")
    for tf_rule, kz in [("15min", True), ("5min", True), ("15min", False)]:
        recs, _ = ict_records(df, "cl", tf_rule, killzone=kz)
        win = sum(1 for r in recs if r["pnl_R"] > 0) / max(len(recs), 1)
        m = apex_eval(recs, 300)
        print(f"cl {tf_rule} kz={kz}: n={len(recs)} win={win*100:.0f}% "
              f"pass={m['pass_rate']*100:.0f}% fails={m['fails']} cens={m['censored']} med={m['med_days']}")
