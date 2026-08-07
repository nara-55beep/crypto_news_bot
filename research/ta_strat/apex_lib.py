"""
apex_lib.py — Apex Trader Funding evaluation engine for NON-indicator price-action strategies.

Apex $50k eval rules modeled:
  * start $50,000; profit target +$3,000 (reach $53,000)
  * TRAILING max drawdown $2,500, trails the intraday equity PEAK (incl. open profit),
    and LOCKS once peak reaches $52,600 -> floor frozen at $50,100 ("+$100" buffer)
  * FAIL if equity ever touches the trailing floor; PASS when equity reaches $53,000 first
  * no time limit (current Apex) -> pass rate = P(hit +3k before the trailing floor)

We separate the strategy (which trades) from the sizing: every trade is recorded in R-units
(pnl_R, mae_R = worst adverse excursion, mfe_R = best favorable excursion), so we can sweep the
per-trade dollar risk R cheaply and feed it to the trailing-DD simulator.
"""
from __future__ import annotations
import os, bisect
import numpy as np, pandas as pd

NY = "America/New_York"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
TICK = {"es": 0.25, "nq": 0.25, "cl": 0.01}     # futures tick size (price)
PTVAL = {"es": 50.0, "nq": 20.0, "cl": 1000.0}  # $/point (e-mini) — reference only


def load_fut(name):
    df = pd.read_csv(os.path.join(CACHE, f"{name}_1m_3y.csv"))
    df["dt"] = pd.to_datetime(df["dt_utc"], utc=True)
    return df.set_index("dt")[["open", "high", "low", "close", "volume"]].sort_index()


def day_arrays(idx):
    """Return per-bar ET-date ordinal + first-bar-index lookup for sessions."""
    et = idx.tz_convert(NY)
    dord = et.year.values * 10000 + et.month.values * 100 + et.day.values
    # map to compact 0..D-1 day ids in order
    uniq, inv = np.unique(dord, return_inverse=True)
    return inv.astype(np.int64), et.hour.values, et.minute.values, len(uniq)


def walk_trade(H, L, C, fi, d, entry, stop, target, end):
    """Simulate one trade from fill bar fi (stop priority). d=+1 long / -1 short.
    Returns (exit_bar, reason, pnl_R, mae_R, mfe_R) or None."""
    risk = abs(entry - stop)
    if risk <= 0 or fi >= end:
        return None
    mae = 0.0; mfe = 0.0
    for m in range(fi, end):
        if d > 0:
            adv = entry - L[m]; fav = H[m] - entry
        else:
            adv = H[m] - entry; fav = entry - L[m]
        if adv > mae: mae = adv
        if fav > mfe: mfe = fav
        hit_stop = (L[m] <= stop) if d > 0 else (H[m] >= stop)
        hit_tgt = (H[m] >= target) if d > 0 else (L[m] <= target)
        if hit_stop:
            return m, "stop", -1.0, -mae / risk, mfe / risk
        if hit_tgt:
            return m, "target", d * (target - entry) / risk, -mae / risk, mfe / risk
    m = end - 1
    return m, "time", d * (C[m] - entry) / risk, -mae / risk, mfe / risk


def cal_ordinal(idx):
    """Per-bar ET calendar-day ordinal (days since 1970-01-01) — global across markets."""
    et = idx.tz_convert(NY).tz_localize(None)
    return et.normalize().values.astype("datetime64[D]").astype(np.int64)


def walk_trade_be(H, L, C, fi, d, entry, stop, target, end, be_at=None):
    """Like walk_trade but moves stop to breakeven once favorable reaches be_at*R (None=off).
    BE arms on a bar's high but only protects from the NEXT bar (no same-bar BE-stop)."""
    risk = abs(entry - stop)
    if risk <= 0 or fi >= end:
        return None
    mae = 0.0; mfe = 0.0; cur_stop = stop; moved = False
    for m in range(fi, end):
        if d > 0:
            adv = entry - L[m]; fav = H[m] - entry
        else:
            adv = H[m] - entry; fav = entry - L[m]
        if adv > mae: mae = adv
        if fav > mfe: mfe = fav
        hit_stop = (L[m] <= cur_stop) if d > 0 else (H[m] >= cur_stop)
        hit_tgt = (H[m] >= target) if d > 0 else (L[m] <= target)
        if hit_stop:
            pnl = d * (cur_stop - entry) / risk
            return m, ("be" if moved else "stop"), pnl, -mae / risk, mfe / risk
        if hit_tgt:
            return m, "target", d * (target - entry) / risk, -mae / risk, mfe / risk
        if be_at is not None and not moved and fav >= be_at * risk:
            cur_stop = entry; moved = True
    m = end - 1
    return m, "time", d * (C[m] - entry) / risk, -mae / risk, mfe / risk


def walk_trade_partial(H, L, C, fi, d, entry, stop, target, end, p1=1.0, frac=0.5):
    """Bank `frac` of the position at +p1*R, move runner stop to breakeven, runner exits at target.
    Tracks the EQUITY offset (in R, size-aware) for accurate trailing-DD mae/mfe."""
    risk = abs(entry - stop)
    if risk <= 0 or fi >= end:
        return None
    plevel = entry + d * p1 * risk
    cur_stop = stop; partial = False; banked = 0.0
    mae_eq = 0.0; mfe_eq = 0.0
    for m in range(fi, end):
        lo_px = L[m] if d > 0 else H[m]      # worst price for a long is the low
        hi_px = H[m] if d > 0 else L[m]
        if not partial:
            lo_off = d * (lo_px - entry) / risk
            hi_off = d * (hi_px - entry) / risk
        else:
            lo_off = banked + (1 - frac) * d * (lo_px - entry) / risk
            hi_off = banked + (1 - frac) * d * (hi_px - entry) / risk
        if lo_off < mae_eq: mae_eq = lo_off
        if hi_off > mfe_eq: mfe_eq = hi_off
        hit_stop = (L[m] <= cur_stop) if d > 0 else (H[m] >= cur_stop)
        hit_tgt = (H[m] >= target) if d > 0 else (L[m] <= target)
        if not partial:
            if hit_stop:
                return m, "stop", -1.0, mae_eq, mfe_eq
            if hit_tgt:
                return m, "target", d * (target - entry) / risk, mae_eq, mfe_eq
            hit_p1 = (H[m] >= plevel) if d > 0 else (L[m] <= plevel)
            if hit_p1:
                partial = True; banked = frac * p1; cur_stop = entry
                if hit_tgt:
                    return m, "target", banked + (1 - frac) * d * (target - entry) / risk, mae_eq, mfe_eq
        else:
            if hit_stop:
                return m, "be", banked + (1 - frac) * d * (cur_stop - entry) / risk, mae_eq, mfe_eq
            if hit_tgt:
                return m, "target", banked + (1 - frac) * d * (target - entry) / risk, mae_eq, mfe_eq
    m = end - 1
    tail = d * (C[m] - entry) / risk
    pnl = (banked + (1 - frac) * tail) if partial else tail
    return m, "time", pnl, mae_eq, mfe_eq


def trades_to_records(trades, df, name, cost_extra_R=0.05):
    """trades: list of (fi, d, entry, stop, target, exit_bar, reason, pnl_R, mae_R, mfe_R).
    Attach entry/exit CALENDAR day (global) + net pnl_R (minus ~1 tick/side slippage + comm)."""
    cal = cal_ordinal(df.index)
    tick = TICK[name]
    recs = []
    for (fi, d, entry, stop, target, xb, rsn, pnlR, maeR, mfeR) in trades:
        risk = abs(entry - stop)
        cost_R = 2 * tick / risk + cost_extra_R if risk > 0 else 0.0
        recs.append(dict(eday=int(cal[fi]), xday=int(cal[xb]), reason=rsn, mkt=name,
                         pnl_R=pnlR - cost_R, mae_R=maeR, mfe_R=mfeR))
    recs.sort(key=lambda r: (r["eday"], r["xday"]))
    return recs, len(np.unique(cal))


def apex_eval(recs, R, start_bal=50000.0, target=3000.0, dd=2500.0, buf=100.0,
              start_step=5, horizon_days=250, day_loss_limit=None, trail="intraday",
              big=None, small=None, switch_off=2000.0):
    """Rolling-start Apex sim. R = $ risked per trade. Returns dict with pass rate etc.
    start_step: begin a fresh eval every `start_step` trading days.
    day_loss_limit: $ — once a day's realized P&L hits -limit, skip that day's remaining trades.
    trail: 'intraday' = peak/DD-breach use intratrade equity (Apex eval, hard);
           'eod'      = peak/breach use realized end-of-trade equity (Topstep-style, lenient).
    big/small/switch_off: 'lock-the-trail sprint' dynamic sizing — risk `big` while peak <
           start_bal+switch_off (push to lock the trail), then drop to `small` to coast. If big
           is None, flat `R` is used."""
    if not recs:
        return None
    recs = sorted(recs, key=lambda r: (r["eday"], r["xday"]))
    edays = [r["eday"] for r in recs]
    lock_peak = start_bal + dd + buf      # 52,600
    locked_floor = start_bal + buf         # 50,100
    tgt_eq = start_bal + target            # 53,000
    passes = fails = censored = 0
    days_to_pass = []; best_day_share = []
    uniq_days = sorted(set(edays))
    starts = uniq_days[::start_step]
    for s in starts:
        i0 = bisect.bisect_left(edays, s)
        eq = peak = start_bal
        locked = False; floor = peak - dd
        outcome = None; pass_day = None
        day_pnl = {}
        cur_day = None; cur_day_pnl = 0.0
        for r in recs[i0:]:
            if r["eday"] - s > horizon_days:
                break
            if r["eday"] != cur_day:
                cur_day = r["eday"]; cur_day_pnl = 0.0
            if day_loss_limit is not None and cur_day_pnl <= -day_loss_limit:
                continue                      # day's loss limit hit -> sit out
            pre = eq
            Ruse = (big if peak < start_bal + switch_off else small) if big is not None else R
            hi = pre + r["mfe_R"] * Ruse
            lo = pre + r["mae_R"] * Ruse
            if trail == "intraday":
                if hi > peak:
                    peak = hi
                    if not locked:
                        if peak >= lock_peak:
                            locked = True; floor = locked_floor
                        else:
                            floor = peak - dd
                if lo <= floor:            # trailing DD breached intratrade
                    outcome = False; break
            if hi >= tgt_eq:               # profit target reached intratrade
                outcome = True; pass_day = r["xday"]; break
            eq = pre + r["pnl_R"] * Ruse
            cur_day_pnl += r["pnl_R"] * Ruse
            day_pnl[r["xday"]] = day_pnl.get(r["xday"], 0.0) + r["pnl_R"] * Ruse
            if eq > peak:
                peak = eq
                if not locked:
                    if peak >= lock_peak:
                        locked = True; floor = locked_floor
                    else:
                        floor = peak - dd
            if trail == "eod" and eq <= floor:   # breach on realized equity
                outcome = False; break
        if outcome is True:
            passes += 1
            days_to_pass.append(pass_day - s)
            if day_pnl:
                best_day_share.append(max(day_pnl.values()) / (sum(day_pnl.values()) or 1.0))
        elif outcome is False:
            fails += 1
        else:
            censored += 1
    decided = passes + fails
    return dict(R=R, n_evals=passes + fails + censored, passes=passes, fails=fails,
                censored=censored, pass_rate=passes / decided if decided else 0.0,
                med_days=float(np.median(days_to_pass)) if days_to_pass else None,
                med_bestday_share=float(np.median(best_day_share)) if best_day_share else None,
                n_trades=len(recs), days_list=days_to_pass)
