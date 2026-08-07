"""
ict_sm_markers.py — compute the on-chart drawings for the "ICT SM Trades" method
(liquidity grab -> MSS -> FVG, killzones) from raw candles, to overlay on a Lightweight-Charts
page just like the TradingView indicator draws.

The TradingView script (tradingview.com/v/ZtoavozZ) is closed-source, so this is an ORIGINAL
implementation of the documented public ICT concepts it visualizes:
  * Pivots         — swing highs/lows (liquidity pools)            -> "Pivot" labels + diamonds
  * Liquidity grab — wick beyond a pivot then close back (sweep)   -> green ✕ (swept low) / red ✕ (swept high)
  * MSS            — a close back through the opposing structure   -> green/red "MSS" labels
  * FVG            — 3-candle fair-value gap in the displacement   -> green/red boxes
  * Killzone       — the session window (default 08:00-17:00 UTC)  -> shaded background

Input  : candles = [{"time": epoch_sec, "open","high","low","close"}...] (ascending)
Output : {"pivots":[...], "grabs":[...], "mss":[...], "fvgs":[...], "diamonds":[...], "killzones":[...]}
"""
from __future__ import annotations
import time as _time


def _pivots(H, L, left, right):
    n = len(H)
    ph = [False] * n; pl = [False] * n
    for i in range(left, n - right):
        seg_h = H[i - left:i + right + 1]; seg_l = L[i - left:i + right + 1]
        if H[i] == max(seg_h) and H[i] > H[i - 1] and H[i] >= H[i + 1]:
            ph[i] = True
        if L[i] == min(seg_l) and L[i] < L[i - 1] and L[i] <= L[i + 1]:
            pl[i] = True
    return ph, pl


def compute(candles, left=5, right=5, mss_window=20, fvg_min_frac=0.00035,
            kz_start_h=8, kz_end_h=17, fvg_extend=10, label_gap=9):
    n = len(candles)
    out = {"pivots": [], "grabs": [], "mss": [], "fvgs": [], "diamonds": [], "killzones": []}
    if n < left + right + 3:
        return out
    O = [c["open"] for c in candles]; H = [c["high"] for c in candles]
    L = [c["low"] for c in candles]; C = [c["close"] for c in candles]
    T = [int(c["time"]) for c in candles]
    ph, pl = _pivots(H, L, left, right)

    # pivots (confirmed swings) — thin the LABELS so they're readable (one per `label_gap` bars
    # per side); detection still uses every pivot below for the sweep/MSS logic.
    last_h = last_l = -10 ** 9
    for i in range(n):
        if ph[i] and i - last_h >= label_gap:
            out["pivots"].append({"time": T[i], "price": H[i], "kind": "high"}); last_h = i
        if pl[i] and i - last_l >= label_gap:
            out["pivots"].append({"time": T[i], "price": L[i], "kind": "low"}); last_l = i

    # liquidity grab -> MSS -> FVG. Scan bars; a pivot is "active liquidity" until swept.
    # one active sequence at a time (like the indicator's stepping), to avoid marker spam.
    i = right + 1
    while i < n:
        # most-recent confirmed pivots strictly before i (confirmed = index + right < i)
        ph_idx = [j for j in range(max(0, i - 80), i) if ph[j] and j + right < i]
        pl_idx = [j for j in range(max(0, i - 80), i) if pl[j] and j + right < i]
        made = False

        # --- bearish: sweep a pivot HIGH (wick above, close back below), THEN MSS down ---
        if ph_idx:
            phj = ph_idx[-1]; lvl = H[phj]
            if H[i] > lvl and C[i] < lvl:
                ref_low = L[pl_idx[-1]] if pl_idx else min(L[max(0, i - 10):i + 1])
                for k in range(i + 1, min(n, i + mss_window)):
                    if C[k] < ref_low:                                                   # MSS confirms the setup
                        out["grabs"].append({"time": T[i], "price": H[i], "dir": "bear"})    # red ✕ above the grab
                        out["diamonds"].append({"time": T[phj], "price": lvl, "dir": "bear"})# ◆ on the swept high
                        out["mss"].append({"time": T[k], "price": H[k], "dir": "bear"})
                        fvg = _find_fvg(H, L, T, i, k, "bear", fvg_min_frac, n, fvg_extend)
                        if fvg:
                            out["fvgs"].append(fvg)
                        i = k + 1; made = True
                        break
        # --- bullish: sweep a pivot LOW (wick below, close back above), THEN MSS up ---
        if not made and pl_idx:
            plj = pl_idx[-1]; lvl = L[plj]
            if L[i] < lvl and C[i] > lvl:
                ref_high = H[ph_idx[-1]] if ph_idx else max(H[max(0, i - 10):i + 1])
                for k in range(i + 1, min(n, i + mss_window)):
                    if C[k] > ref_high:                                                  # MSS confirms the setup
                        out["grabs"].append({"time": T[i], "price": L[i], "dir": "bull"})    # green ✕ below the grab
                        out["diamonds"].append({"time": T[plj], "price": lvl, "dir": "bull"})# ◆ on the swept low
                        out["mss"].append({"time": T[k], "price": L[k], "dir": "bull"})
                        fvg = _find_fvg(H, L, T, i, k, "bull", fvg_min_frac, n, fvg_extend)
                        if fvg:
                            out["fvgs"].append(fvg)
                        i = k + 1; made = True
                        break
        if not made:
            i += 1

    # killzone shaded bands (UTC session kz_start_h..kz_end_h), as contiguous time ranges
    bar = (T[1] - T[0]) if n > 1 else 60
    cur = None
    for i in range(n):
        hr = _time.gmtime(T[i]).tm_hour
        inkz = kz_start_h <= hr < kz_end_h
        if inkz and cur is None:
            cur = [T[i], T[i]]
        elif inkz:
            cur[1] = T[i] + bar
        elif cur is not None:
            out["killzones"].append({"start": cur[0], "end": cur[1]}); cur = None
    if cur is not None:
        out["killzones"].append({"start": cur[0], "end": cur[1]})
    return out


def _find_fvg(H, L, T, i, k, direction, min_frac, n, extend):
    """3-candle FVG inside the displacement leg [i,k]. bull: H[a-1] < L[a+1]; bear: L[a-1] > H[a+1]."""
    for a in range(i, k):
        if a + 1 >= n or a - 1 < 0:
            continue
        if direction == "bull":
            gap = L[a + 1] - H[a - 1]
            if gap > 0 and gap >= min_frac * L[a + 1]:
                end_t = T[min(n - 1, a + 1 + extend)]
                return {"start": T[a - 1], "end": end_t, "top": L[a + 1], "bottom": H[a - 1], "dir": "bull"}
        else:
            gap = L[a - 1] - H[a + 1]
            if gap > 0 and gap >= min_frac * H[a + 1]:
                end_t = T[min(n - 1, a + 1 + extend)]
                return {"start": T[a - 1], "end": end_t, "top": L[a - 1], "bottom": H[a + 1], "dir": "bear"}
    return None
