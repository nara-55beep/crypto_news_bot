"""
Opening Drive Compression Breakout — MNQ (MES secondary). Apex 50K. Signal/backtest only, no auto-trade.

Direction from the 09:30-10:00 opening DRIVE; entry from a tight VOLATILITY COMPRESSION box
(5-15 candles, 8-35 ticks, < 0.55xATR) that forms 10:00-11:00; stop sits just OUTSIDE the box
(box low -2t), so the stop is TIGHT regardless of how big the day's trend is. Optional shakeout
(sweep box edge then reclaim within 3 candles). Exit modes A-G. Forced flat 11:30. Max 2 trades/day,
stop-for-day after a loss. Step-6 fast-fail: out if not +0.25R within 6 candles.

NOTE: the spec header says "5-minute confirmation" but defines no explicit 5m rule in any step, so
none is fabricated (the model is fully defined on 1m). Data: 3y Dukascopy NQ=MNQ / ES=MES, 1m,
REGULAR SESSION only (no overnight).
"""
from __future__ import annotations
import os, sys, traceback
from dataclasses import dataclass, asdict
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ict_sb_2022_backtester import (load_csv, atr, account_sim, stats, monte_carlo,
                                     walk_forward, _mins, CACHE)


@dataclass
class Cfg:
    symbol: str = "MNQ"; pv: float = 2.0; tick: float = 0.25
    comm_rt: float = 1.0; slip_ticks: float = 2.0
    start_bal: float = 50_000.0; profit_target: float = 3_000.0; trail_dd: float = 2_500.0
    dd_lockout: float = 600.0; risk_usd: float = 100.0; max_micros: int = 4
    max_trades_day: int = 2; daily_profit_stop: float = 400.0
    exit_mode: str = "C"            # A 1R | B 1.25R | C 1.5R | D 70%@1R+2R | E 70%@1R+trail | F +0.5R/10c | G time
    use_vwap: bool = False; shakeout: bool = False; side: str = "both"   # both|long|short
    box_max_ticks: int = 30; max_stop_ticks: int = 35
    box_ref: str = "atr055"         # atr055 = spec literal (box<0.55xATR) | or055 = box<0.55x opening-range


def _simulate(k, direction, elv, stp, H, L, C, mw, cfg):
    sign = 1 if direction == "long" else -1
    tick = cfg.tick; slip = cfg.slip_ticks * tick
    e = elv + sign * slip
    risk = abs(e - stp)
    if risk <= 0:
        return None
    contracts = int(max(1, min(cfg.max_micros, round(cfg.risk_usd / (risk * cfg.pv)))))
    risk_d = risk * cfg.pv * contracts
    rr = lambda px: ((px - e) / risk) * sign
    n = len(H); mode = cfg.exit_mode
    pos = 1.0; realR = 0.0; cur = stp; half = False; best = e; reason = None; jx = k
    for j in range(k, n):
        if mw[j] > 690:                                   # 11:30 forced flat (handled after loop)
            jx = j - 1; break
        jx = j; cs = j - k
        hi, lo, cl = H[j], L[j], C[j]
        favx = (hi - e) if sign > 0 else (e - lo)
        if favx > (best - e) * sign:
            best = e + sign * favx
        bestR = ((best - e) / risk) * sign
        # 1) stop (conservative: checked first)
        if (lo <= cur) if sign > 0 else (hi >= cur):
            realR += pos * rr(cur - sign * slip); reason = "stop"; pos = 0; break
        # 2) profit management by mode
        if mode in ("A", "B", "C"):
            tR = {"A": 1.0, "B": 1.25, "C": 1.5}[mode]; tgt = e + sign * tR * risk
            if (hi >= tgt) if sign > 0 else (lo <= tgt):
                realR += pos * tR; reason = "target"; pos = 0; break
        elif mode == "D":
            if not half:
                t1 = e + sign * risk
                if (hi >= t1) if sign > 0 else (lo <= t1):
                    realR += 0.7; pos = 0.3; half = True
            if half:
                t2 = e + sign * 2 * risk
                if (hi >= t2) if sign > 0 else (lo <= t2):
                    realR += 0.3 * 2; reason = "runner2R"; pos = 0; break
        elif mode == "E":
            if not half:
                t1 = e + sign * risk
                if (hi >= t1) if sign > 0 else (lo <= t1):
                    realR += 0.7; pos = 0.3; half = True; cur = e
            if half and j > k:
                cur = max(cur, L[j - 1]) if sign > 0 else min(cur, H[j - 1])   # trail behind 1m swing
        # 3) fast-fail
        if mode != "F":
            if cs == 5 and bestR < 0.25:
                realR += pos * rr(cl - sign * slip); reason = "fail0.25"; pos = 0; break
        else:
            if cs == 9 and bestR < 0.5:
                realR += pos * rr(cl - sign * slip); reason = "failF0.5"; pos = 0; break
    if pos > 0:
        realR += pos * rr(C[jx] - sign * slip); reason = reason or "flat1130"
    pnl = realR * risk_d - 2 * cfg.comm_rt * contracts
    tr = {"direction": direction, "entry": round(e, 2), "stop": round(stp, 2), "reason": reason,
          "contracts": contracts, "risk_usd": round(risk_d, 2), "R": round(pnl / risk_d, 3),
          "pnl": round(pnl, 2)}
    return tr, jx


def _scan_entry(scan, H, L, O, C, vw, mw, direction, o930, or50, orv, atrv, cfg):
    n = len(H); tick = cfg.tick
    boxcap = 0.55 * atrv if cfg.box_ref == "atr055" else 0.55 * orv
    for start in range(scan, n):
        if mw[start] > 660:                               # box must form by 11:00
            return None
        bh, bl, end = H[start], L[start], start
        for k in range(start + 1, min(start + 15, n)):
            nh, nl = max(bh, H[k]), min(bl, L[k]); w = nh - nl
            if (H[k] - L[k]) > 0.75 * atrv or w > cfg.box_max_ticks * tick or w >= boxcap:
                break                                     # k is the breakout candle; box = [start,end]
            bh, bl, end = nh, nl, k
        length = end - start + 1
        if length < 5:
            continue
        width = bh - bl
        if width < 8 * tick:
            continue
        ov = sum(1 for k in range(start + 1, end + 1) if L[k] <= H[k - 1] and H[k] >= L[k - 1])
        if ov / (length - 1) < 0.6:
            continue
        if direction == "long":
            if bl < o930 or bl < or50 or (cfg.use_vwap and bl < vw[end]):
                continue
            elv, stp = bh + tick, bl - 2 * tick
        else:
            if bh > o930 or bh > or50 or (cfg.use_vwap and bh > vw[end]):
                continue
            elv, stp = bl - tick, bh + 2 * tick
        st_ticks = abs(elv - stp) / tick
        if st_ticks > cfg.max_stop_ticks or st_ticks < 8:
            continue
        armed = not cfg.shakeout; swept = False; sb = None
        for k in range(end + 1, n):
            if mw[k] > 675:                               # no entry after 11:15
                break
            if cfg.shakeout and not armed:
                if not swept:
                    if (direction == "long" and L[k] < bl - tick) or (direction == "short" and H[k] > bh + tick):
                        swept = True; sb = k
                else:
                    if k - sb > 3:
                        break
                    if (direction == "long" and C[k] >= bl) or (direction == "short" and C[k] <= bh):
                        armed = True
                if not armed:
                    continue
            if (direction == "long" and H[k] >= elv) or (direction == "short" and L[k] <= elv):
                res = _simulate(k, direction, elv, stp, H, L, C, mw, cfg)
                if res:
                    return res
                break
    return None


def run_day(g, atrv, or_avg, cfg):
    m = _mins(g.index)
    od = g[(m >= 570) & (m <= 599)]
    if len(od) < 20 or not np.isfinite(or_avg) or or_avg <= 0:
        return []
    o930 = float(od["open"].iloc[0]); c1000 = float(od["close"].iloc[-1])
    hi = float(od["high"].max()); lo = float(od["low"].min()); rng = hi - lo
    if rng <= 0:
        return []
    eff = abs(c1000 - o930) / rng
    okrng = 0.8 * or_avg <= rng <= 2.5 * or_avg
    bull = c1000 > o930 and eff >= 0.45 and c1000 >= lo + 0.65 * rng and okrng
    bear = c1000 < o930 and eff >= 0.45 and c1000 <= lo + 0.35 * rng and okrng
    direction = "long" if bull else ("short" if bear else None)
    if direction is None:
        return []
    if cfg.side != "both" and cfg.side != direction:
        return []
    win = g[(m >= 600) & (m <= 690)]
    if len(win) < 6 or not np.isfinite(atrv) or atrv <= 0:
        return []
    H, L, O, C = (win[c].values for c in ("high", "low", "open", "close"))
    vw = win["vwap"].values; mw = _mins(win.index); or50 = lo + 0.5 * rng
    day = str(g.index[0].date()); trades = []; scan = 0
    while len(trades) < cfg.max_trades_day:
        res = _scan_entry(scan, H, L, O, C, vw, mw, direction, o930, or50, rng, atrv, cfg)
        if res is None:
            break
        tr, jx = res; tr["day"] = day; trades.append(tr)
        if tr["pnl"] <= 0:
            break
        if sum(t["pnl"] for t in trades) >= cfg.daily_profit_stop:
            break
        scan = jx + 1
        if scan >= len(H):
            break
    return trades


def backtest(df, cfg):
    df = df.copy(); df["date"] = df.index.normalize()
    if "volume" not in df:
        df["volume"] = 1.0
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    w = df["volume"].fillna(1.0).clip(lower=1.0)
    df["vwap"] = (tp * w).groupby(df["date"]).cumsum() / w.groupby(df["date"]).cumsum()
    df["atr"] = atr(df["high"].values, df["low"].values, df["close"].values, 14)
    m = _mins(df.index)
    od = df[(m >= 570) & (m <= 599)].groupby("date").agg(orh=("high", "max"), orl=("low", "min"))
    od["op_rng"] = od["orh"] - od["orl"]
    od["op_rng_avg"] = od["op_rng"].rolling(20).mean().shift(1)
    atr10 = df[m >= 600].groupby("date")["atr"].first()
    days = sorted(df["date"].unique()); trades = []
    for day in days:
        try:
            if day not in od.index or np.isnan(od.loc[day, "op_rng_avg"]) or day not in atr10.index:
                continue
            trades += run_day(df[df["date"] == day], float(atr10.loc[day]),
                              float(od.loc[day, "op_rng_avg"]), cfg)
        except Exception:
            traceback.print_exc()
    return pd.DataFrame(trades)


def extra_metrics(trades):
    if len(trades) == 0:
        return {"months_profitable": 0, "months_total": 0, "max_month_frac": None}
    d = trades.copy(); d["ym"] = pd.to_datetime(d["day"]).dt.to_period("M")
    mp = d.groupby("ym")["pnl"].sum(); net = mp.sum()
    frac = (mp.max() / net) if net > 0 else None
    return {"months_profitable": int((mp > 0).sum()), "months_total": int(mp.shape[0]),
            "max_month_frac": (round(float(frac), 2) if frac is not None else None)}


def my_verdict(s, wf, mc, ex):
    c = {"DevPF>1.25": (wf.get("pf_dev") or 0) > 1.25, "OOSPF>1.25": (wf.get("pf_oos") or 0) > 1.25,
         "avgTrade>0": (s.get("avg_trade", -1) or -1) > 0, "MCpass>40%": (mc.get("p_pass", 0) or 0) > 40,
         "MCfailDD<25%": (mc.get("p_fail_dd", 100) or 100) < 25, "trades>=80": s.get("trades", 0) >= 80,
         "noSingleMonth>50%": (ex.get("max_month_frac") is not None and ex["max_month_frac"] < 0.5)}
    return all(c.values()), c


def line(tag, df, cfg):
    s = stats(df, cfg, tag)
    if not s.get("trades"):
        return f"  {tag:<26}  no trades", None
    wf = walk_forward(df, cfg); mc = monte_carlo(df, cfg, n=300); ex = extra_metrics(df)
    ok, _ = my_verdict(s, wf, mc, ex)
    return (f"  {tag:<26}{s['trades']:>5}{s['win_rate']:>6.0f}{(s['profit_factor'] or 0):>6.2f}"
            f"{s['net_profit']:>+8.0f}{s['avg_R']:>+7.3f}{s['max_dd_$']:>8.0f}"
            f"{str(wf.get('pf_dev'))+'/'+str(wf.get('pf_oos')):>11}{(mc.get('p_pass') or 0):>5.0f}%"
            f"{(mc.get('p_fail_dd') or 0):>5.0f}%{ex['months_profitable']:>3}/{ex['months_total']:<3}"
            f"{'  PASS' if ok else '  fail'}", (s, wf, mc, ex, df, cfg))


CONFIGS = [  # (name, use_vwap, shakeout, side)
    ("1 both/noVWAP/noShake", False, False, "both"),
    ("2 both/VWAP/noShake", True, False, "both"),
    ("3 both/noVWAP/shake", False, True, "both"),
    ("4 both/VWAP/shake", True, True, "both"),
    ("5 long/noVWAP/shake", False, True, "long"),
    ("6 long/VWAP/shake", True, True, "long"),
    ("7 short/noVWAP/shake", False, True, "short"),
    ("8 short/VWAP/shake", True, True, "short"),
]
HDR = (f"  {'config':<26}{'n':>5}{'win%':>6}{'PF':>6}{'net$':>8}{'avgR':>7}{'maxDD':>8}"
       f"{'PFdev/oos':>11}{'MCpass':>6}{'fDD':>6}{'mo+':>7}  verdict")


def run(symbol, df, pv, base, note):
    mk = lambda **kw: Cfg(symbol=symbol, pv=pv, **{**base, **kw})
    print(f"\n{'#'*86}\n# {symbol}  {df.index[0].date()} -> {df.index[-1].date()}  ({len(df):,} 1m bars)  [{note}]\n{'#'*86}")
    print(f"\n[1] STRUCTURAL CONFIGS 1-8  (risk $100, exit C=1.5R, trailDD 2500)\n{HDR}")
    best = None
    for nm, uv, sk, sd in CONFIGS:
        cfg = mk(use_vwap=uv, shakeout=sk, side=sd, exit_mode="C")
        ln, pack = line(nm, backtest(df, cfg), cfg)
        print(ln)
        if pack and (best is None or (pack[0]["profit_factor"] or 0) > best[0]):
            best = ((pack[0]["profit_factor"] or 0), nm, uv, sk, sd)
    if not best:
        print("  (no tradeable configs)"); return
    _, bnm, buv, bsk, bsd = best
    print(f"\n  -> best structural config by PF: {bnm}")
    print(f"\n[2] EXIT MODES A-G on best config  (risk $100)\n{HDR}")
    keep = None
    for mode in ["A", "B", "C", "D", "E", "F", "G"]:
        cfg = mk(use_vwap=buv, shakeout=bsk, side=bsd, exit_mode=mode)
        ln, pack = line(f"exit {mode}", backtest(df, cfg), cfg)
        print(ln)
        if pack and (keep is None or (pack[0]["profit_factor"] or 0) > keep[1]):
            keep = (mode, pack[0]["profit_factor"] or 0, pack)
    bmode = keep[0] if keep else "C"
    print(f"\n  -> best exit: {bmode}")
    print(f"\n[3] RISK & BOX-CEILING sweep on best config+exit ({bnm}, exit {bmode})\n{HDR}")
    for rk in (75, 100, 125, 150):
        cfg = mk(use_vwap=buv, shakeout=bsk, side=bsd, exit_mode=bmode, risk_usd=rk)
        print(line(f"risk ${rk}", backtest(df, cfg), cfg)[0])
    for bx in (50, 70, 90):
        cfg = mk(use_vwap=buv, shakeout=bsk, side=bsd, exit_mode=bmode, box_max_ticks=bx)
        print(line(f"box<= {bx}t", backtest(df, cfg), cfg)[0])
    # full detail + STEP 10 validation on best overall
    cfg = mk(use_vwap=buv, shakeout=bsk, side=bsd, exit_mode=bmode)
    df_t = backtest(df, cfg); s = stats(df_t, cfg, "BEST"); wf = walk_forward(df_t, cfg)
    mc = monte_carlo(df_t, cfg, n=600); ex = extra_metrics(df_t); ok, checks = my_verdict(s, wf, mc, ex)
    dtps = [account_sim(df_t, cfg, shuffle_seed=i)["days_to_pass"] for i in range(600)]
    dtps = [d for d in dtps if d]
    print(f"\n[4] BEST OVERALL: {symbol} {bnm} exit {bmode} — full report")
    print(f"    trades {s['trades']}  win {s['win_rate']}%  PF {s['profit_factor']}  net ${s['net_profit']:+.0f}"
          f"  avgTrade ${s['avg_trade']:+.2f}  avgR {s['avg_R']:+.3f}")
    print(f"    maxDD ${s['max_dd_$']:.0f}  maxConsecL {s['max_consec_losses']}  bestDay ${s['best_day']:+.0f}"
          f"  worstDay ${s['worst_day']:+.0f}")
    print(f"    long {s['longs']} (win {s['long_winrate']}%)  short {s['shorts']} (win {s['short_winrate']}%)")
    print(f"    DevPF {wf.get('pf_dev')} / OOSPF {wf.get('pf_oos')}  | MC pass {mc.get('p_pass')}% fail-DD {mc.get('p_fail_dd')}%")
    print(f"    days-to-pass: median {int(np.median(dtps)) if dtps else None}  mean "
          f"{round(float(np.mean(dtps)),1) if dtps else None}  (n_passed {len(dtps)}/600)")
    print(f"    months profitable {ex['months_profitable']}/{ex['months_total']}  maxMonthFrac {ex['max_month_frac']}")
    print(f"\n    STEP-10 VALIDATION:")
    for k, v in checks.items():
        print(f"      [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n    ===> {symbol}: {'PASSES' if ok else 'FAILS'} <===")
    return ok, df_t, cfg, (buv, bsk, bsd, bmode)


def diagnostic(symbol, df, pv, base, buv, bsk, bsd, bmode):
    """STEP 11 — MFE/MAE first-touch race on the raw breakout signal (if best config FAILS)."""
    print(f"\n[5] STEP-11 MFE/MAE DIAGNOSTIC — {symbol} (raw breakout entries, rest-of-day):")
    cfg = Cfg(symbol=symbol, pv=pv, **{**base, "use_vwap": buv, "shakeout": bsk, "side": bsd, "exit_mode": "G"})
    df = df.copy(); df["daystr"] = df.index.strftime("%Y-%m-%d")   # tz-safe day key (matches trade['day'])
    tr = backtest(df, cfg)
    if len(tr) == 0:
        print("    no signals"); return
    # re-walk each entry day to measure excursions vs the trade's own stop distance
    tick = cfg.tick; rows = []
    for _, t in tr.iterrows():
        dd = df[df["daystr"] == t["day"]]
        if len(dd) == 0:
            continue
        sign = 1 if t["direction"] == "long" else -1
        e = float(t["entry"]); stp = float(t["stop"]); Rp = abs(e - stp)
        if Rp <= 0:
            continue
        # approximate the entry bar as the first bar at/after 10:00 where price first crosses the entry
        after = dd[(_mins(dd.index) >= 600)]
        if len(after) == 0:
            continue
        H = after["high"].values; L = after["low"].values
        mfe = mae = 0.0; race = {x: None for x in (0.5, 1.0, 1.25, 1.5)}
        started = False
        for h, l in zip(H, L):
            if not started:
                if (h >= e) if sign > 0 else (l <= e):
                    started = True
                else:
                    continue
            fav = (h - e) if sign > 0 else (e - l); adv = (e - l) if sign > 0 else (h - e)
            mfe = max(mfe, fav); mae = max(mae, adv)
            if adv >= Rp:
                for x in race:
                    if race[x] is None:
                        race[x] = False
                break
            for x in race:
                if race[x] is None and fav >= x * Rp:
                    race[x] = True
        for x in race:
            if race[x] is None:
                race[x] = False
        rows.append(dict(direction=t["direction"], minute=int(_mins(after.index)[0]),
                         box_ticks=round(Rp / tick), mfe_R=mfe / Rp, mae_R=mae / Rp,
                         h05=race[0.5], h1=race[1.0], h125=race[1.25], h15=race[1.5]))
    D = pd.DataFrame(rows)
    if len(D) == 0:
        print("    no measurable signals"); return
    def blk(nm, d):
        if len(d) == 0:
            print(f"    {nm:<20} n=0"); return
        print(f"    {nm:<20} n={len(d):>4}  +0.5R {d.h05.mean()*100:>3.0f}%  +1R {d.h1.mean()*100:>3.0f}%  "
              f"+1.25R {d.h125.mean()*100:>3.0f}%  +1.5R {d.h15.mean()*100:>3.0f}%  | "
              f"medMFE {d.mfe_R.median():.2f}R medMAE {d.mae_R.median():.2f}R")
    blk("OVERALL", D)
    blk("long", D[D.direction == "long"]); blk("short", D[D.direction == "short"])
    blk("box 8-18t", D[D.box_ticks <= 18]); blk("box 19-30t", D[(D.box_ticks > 18) & (D.box_ticks <= 30)])
    blk("box 31-38t", D[D.box_ticks > 30])
    print(f"    P(+1R before -1R) overall = {D.h1.mean()*100:.0f}%  (>50% = raw edge); "
          f"+1.5R = {D.h15.mean()*100:.0f}% (>40% = 1.5R viable)")


def main():
    print("OPENING DRIVE COMPRESSION BREAKOUT — 3y MNQ primary / MES secondary "
          "(Dukascopy regular-session 1m).")
    LIT = dict(box_ref="atr055", box_max_ticks=35, max_stop_ticks=35)
    FEAS = dict(box_ref="or055", box_max_ticks=300, max_stop_ticks=120)
    for sym, fn, pv in [("MNQ", "nq_1m_3y.csv", 2.0), ("MES", "es_1m_3y.csv", 5.0)]:
        p = os.path.join(CACHE, fn)
        if not os.path.exists(p):
            print(f"  {sym}: data missing"); continue
        df = load_csv(p)
        print(f"\n\n{'='*86}\n=== {sym}  PART A: LITERAL SPEC (box 8-35t AND <0.55xATR, stop<=35t) ===\n{'='*86}")
        run(sym, df, pv, LIT, "LITERAL SPEC")
        print(f"\n\n{'='*86}\n=== {sym}  PART B: FEASIBLE IDEA-TEST (box<=0.55x opening-range, stop<=120t) ===\n"
              f"=== deviation: the fixed 8-35t box never forms on {sym} 1m; this tests the IDEA. ===\n{'='*86}")
        out = run(sym, df, pv, FEAS, "FEASIBLE")
        if out:
            ok, df_t, cfg, sel = out
            if not ok:
                diagnostic(sym, df, pv, FEAS, *sel)


if __name__ == "__main__":
    main()
