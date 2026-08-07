"""
Event-Driven Trend Day Continuation Model — MNQ, Apex 50K. Signal/backtest only.

Only trade CONFIRMED high-volatility trend days, 10:00-11:30 NY:
  DAY FILTER (any): opening 09:30-10:00 range > 1.2x 20d avg | gap > 0.5x 20d ATR |
                    opening volume > 1.2x 20d avg.  (manual news-day flag: not available here.)
  BIAS at 10:00: bull = 10:00>VWAP & EMA21>EMA50 & OR-close in upper 30% & 5m EMA9>EMA21; bear=mirror.
  ENTRY: after a pullback to VWAP/EMA21/OR-edge that HOLDS VWAP, break the pre-pullback swing ->
         stop beyond pullback extreme +2t (skip if >60t).  Exit modes A 1.5R / B 2R /
         C 50%@1R+trail / D trail after +0.75R; all flat by 11:30.
Reuses the verified Apex account-sim / trailing-DD / walk-forward / Monte-Carlo.
Data: 3y Dukascopy NQ 1m (=MNQ price), REGULAR SESSION only.
"""
from __future__ import annotations
import os, sys, traceback
from dataclasses import dataclass
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ict_sb_2022_backtester import (load_csv, atr, _mins, account_sim, stats,
                                     monte_carlo, walk_forward, verdict, save_outputs, CACHE)


@dataclass
class Cfg:
    symbol: str = "MNQ"; pv: float = 2.0; tick: float = 0.25
    comm_rt: float = 1.0; slip_ticks: float = 2.0
    start_bal: float = 50_000.0; profit_target: float = 3_000.0; trail_dd: float = 2_000.0
    risk_usd: float = 100.0; max_micros: int = 4; dd_lockout: float = 600.0
    max_stop_ticks: int = 60
    exit_mode: str = "B"           # A 1.5R | B 2R | C partial | D trail0.75
    rng_mult: float = 1.2; gap_mult: float = 0.5; vol_mult: float = 1.2


def _trade(g, ei_time, d, entry, stop, cfg, skips):
    sign = 1 if d == "long" else -1
    slip = cfg.slip_ticks * cfg.tick
    e = entry + sign * slip
    risk = abs(e - stop)
    if risk <= 0:
        skips["bad_levels"] += 1; return None
    if (risk / cfg.tick) > cfg.max_stop_ticks:
        skips["stop_wide"] += 1; return None
    contracts = int(max(1, min(cfg.max_micros, round(cfg.risk_usd / (risk * cfg.pv)))))
    risk_d = risk * cfg.pv * contracts
    r1 = e + sign * risk; r075 = e + sign * 0.75 * risk
    tgt = e + sign * (1.5 if cfg.exit_mode == "A" else 2.0) * risk
    mgmt = g[(g.index > ei_time) & (_mins(g.index) <= 690)]   # flat by 11:30
    reason = exit_px = None; half = False; cur = stop; best = e; realR = 0.0; trailing = False
    for t, row in mgmt.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        stopped = lo <= cur if d == "long" else hi >= cur
        if cfg.exit_mode in ("A", "B"):
            ht = hi >= tgt if d == "long" else lo <= tgt
            if stopped: exit_px, reason = cur, "SL"; break
            if ht: exit_px, reason = tgt, "TP"; break
        elif cfg.exit_mode == "C":                            # 50% at 1R, trail rest
            if stopped:
                realR += (0.5 if half else 1.0) * ((cur - e) / risk * sign); reason = "stop"; exit_px = cur; break
            h1 = hi >= r1 if d == "long" else lo <= r1
            if not half and h1:
                realR += 0.5; half = True; cur = e
            if half:
                if d == "long": best = max(best, hi); cur = max(cur, lo)
                else: best = min(best, lo); cur = min(cur, hi)
        elif cfg.exit_mode == "D":                            # full trail after +0.75R
            if stopped: exit_px, reason = cur, "trail/SL"; break
            if d == "long":
                best = max(best, hi)
                if (best - e) >= 0.75 * risk: trailing = True
                if trailing: cur = max(cur, e, lo)
            else:
                best = min(best, lo)
                if (e - best) >= 0.75 * risk: trailing = True
                if trailing: cur = min(cur, e, hi)
    if reason is None:
        last = mgmt.iloc[-1] if len(mgmt) else None
        exit_px = float(last["close"]) if last is not None else e; reason = "1130"
        if cfg.exit_mode == "C" and half:
            realR += 0.5 * ((exit_px - e) / risk * sign)
    if cfg.exit_mode == "C":
        gross = realR * risk_d
    else:
        ex = exit_px + (-sign) * slip
        gross = (ex - e) * contracts * cfg.pv * sign
    pnl = gross - 2 * cfg.comm_rt * contracts
    return {"day": str(g.index[0].date()), "direction": d, "entry": round(e, 2), "stop": round(stop, 2),
            "reason": reason, "contracts": contracts, "risk_usd": round(risk_d, 2),
            "R": round(pnl / risk_d, 3), "pnl": round(pnl, 2)}


def run_day(day, g, feats, cfg, ema5_bias, skips):
    m = _mins(g.index)
    od = g[(m >= 570) & (m <= 599)]                            # 09:30-10:00
    if len(od) < 20:
        skips["no_drive"] += 1; return None
    ORH = float(od["high"].max()); ORL = float(od["low"].min()); ORC = float(od["close"].iloc[-1])
    rng = ORH - ORL
    if rng <= 0:
        skips["no_drive"] += 1; return None
    # day filter (volatility / gap / volume)
    big_rng = feats["op_rng"] > cfg.rng_mult * feats["op_rng_avg"]
    big_gap = abs(feats["gap"]) > cfg.gap_mult * feats["atr_d"]
    big_vol = (feats["op_vol"] > cfg.vol_mult * feats["op_vol_avg"]) if np.isfinite(feats["op_vol_avg"]) else False
    if not (big_rng or big_gap or big_vol):
        skips["not_high_vol"] += 1; return None
    win = g[(m >= 600) & (m <= 690)]                          # 10:00-11:30
    if len(win) < 5:
        skips["no_window"] += 1; return None
    H = win["high"].values; L = win["low"].values; C = win["close"].values
    vwap = win["vwap"].values; e21 = win["ema21"].values; e50 = win["ema50"].values
    T = win.index; n = len(win)
    upper = ORC >= ORL + 0.7 * rng; lower = ORC <= ORL + 0.3 * rng
    p0 = C[0]
    bull = p0 > vwap[0] and e21[0] > e50[0] and upper and (ema5_bias == 1)
    bear = p0 < vwap[0] and e21[0] < e50[0] and lower and (ema5_bias == -1)
    if not (bull or bear):
        skips["no_bias"] += 1; return None
    d = "long" if bull else "short"; tk = cfg.tick
    peak = -1e18 if d == "long" else 1e18; pulled = False; pext = None
    for i in range(n):
        if d == "long":
            if not pulled:
                peak = max(peak, H[i])
                if (L[i] <= ORH or L[i] <= e21[i] or L[i] <= vwap[i]) and C[i] >= vwap[i]:
                    pulled = True; pext = L[i]
            else:
                if C[i] < vwap[i]:
                    skips["pullback_failed"] += 1; return None
                pext = min(pext, L[i])
                if H[i] > peak:
                    stop = pext - 2 * tk
                    if stop < peak:
                        return _trade(g, T[i], "long", peak, stop, cfg, skips)
        else:
            if not pulled:
                peak = min(peak, L[i])
                if (H[i] >= ORL or H[i] >= e21[i] or H[i] >= vwap[i]) and C[i] <= vwap[i]:
                    pulled = True; pext = H[i]
            else:
                if C[i] > vwap[i]:
                    skips["pullback_failed"] += 1; return None
                pext = max(pext, H[i])
                if L[i] < peak:
                    stop = pext + 2 * tk
                    if stop > peak:
                        return _trade(g, T[i], "short", peak, stop, cfg, skips)
    skips["no_entry"] += 1; return None


def backtest(df, cfg):
    df = df.copy(); df["date"] = df.index.normalize()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    if "volume" not in df:
        df["volume"] = 1.0
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    w = df["volume"].fillna(1.0).clip(lower=1.0)
    df["vwap"] = (tp * w).groupby(df["date"]).cumsum() / w.groupby(df["date"]).cumsum()
    g5 = df.resample("5min").agg({"close": "last"}).dropna()
    g5["e9"] = g5["close"].ewm(span=9, adjust=False).mean(); g5["e21"] = g5["close"].ewm(span=21, adjust=False).mean()
    g5["bias"] = np.where(g5["e9"] > g5["e21"], 1, -1)
    m = _mins(df.index)
    days = sorted(df["date"].unique())
    # per-day features
    daily = df.groupby("date").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"),
                                   open=("open", "first"))
    daily["atr_d"] = atr(daily["high"].values, daily["low"].values, daily["close"].values, 14)
    od = df[(m >= 570) & (m <= 599)].groupby("date").agg(orh=("high", "max"), orl=("low", "min"),
                                                          opvol=("volume", "sum"))
    od["op_rng"] = od["orh"] - od["orl"]
    od["op_rng_avg"] = od["op_rng"].rolling(20).mean().shift(1)
    od["op_vol_avg"] = od["opvol"].rolling(20).mean().shift(1)
    trades = []; skips = {k: 0 for k in ["no_drive", "not_high_vol", "no_window", "no_bias",
                          "pullback_failed", "no_entry", "bad_levels", "stop_wide"]}
    for di in range(1, len(days)):
        try:
            day = days[di]; g = df[df["date"] == day]
            if day not in od.index or np.isnan(od.loc[day, "op_rng_avg"]):
                continue
            feats = {"op_rng": od.loc[day, "op_rng"], "op_rng_avg": od.loc[day, "op_rng_avg"],
                     "op_vol": od.loc[day, "opvol"], "op_vol_avg": od.loc[day, "op_vol_avg"],
                     "gap": daily.loc[day, "open"] - daily.loc[days[di - 1], "close"],
                     "atr_d": daily.loc[days[di - 1], "atr_d"]}
            if not np.isfinite(feats["atr_d"]):
                continue
            b5 = g5[g5.index <= g.index[0] + pd.Timedelta(minutes=30)]
            eb = int(b5["bias"].iloc[-1]) if len(b5) else 0
            tr = run_day(day, g, feats, cfg, eb, skips)
            if tr:
                trades.append(tr)
        except Exception:
            traceback.print_exc()
    return pd.DataFrame(trades), skips


def run(symbol, df):
    print(f"\n{'#'*72}\n# {symbol}  {df.index[0].date()} -> {df.index[-1].date()}  ({len(df):,} 1m bars)\n{'#'*72}")
    print(f"{'exit':<6}{'DD':>6}{'trades':>7}{'win%':>6}{'PF':>6}{'net$':>9}{'avgR':>7}{'maxDD$':>8}"
          f"{'PFdev/oos':>12}{'MC%':>6}{'verdict':>9}")
    best = None
    for dd in (2000.0, 2500.0):
        for mode in ["A", "B", "C", "D"]:
            cfg = Cfg(symbol=symbol, trail_dd=dd, exit_mode=mode)
            trades, skips = backtest(df, cfg)
            s = stats(trades, cfg, f"{mode}/{int(dd)}")
            if not s.get("trades"):
                print(f"{mode:<6}{int(dd):>6}{0:>7}  (no trades) {skips}"); continue
            wf = walk_forward(trades, cfg); mc = monte_carlo(trades, cfg, n=400)
            ok, _ = verdict(s, wf, mc, cfg)
            print(f"{mode:<6}{int(dd):>6}{s['trades']:>7}{s['win_rate']:>5.0f}{(s['profit_factor'] or 0):>6.2f}"
                  f"{s['net_profit']:>+9.0f}{s['avg_R']:>7.3f}{s['max_dd_$']:>8.0f}"
                  f"{str(wf.get('pf_dev'))+'/'+str(wf.get('pf_oos')):>12}{(mc.get('p_pass') or 0):>5.0f}%"
                  f"{'PASS' if ok else 'FAIL':>9}")
            if best is None or (s['profit_factor'] or 0) > best[1]:
                best = (f"{mode}/{int(dd)}", s['profit_factor'] or 0, s, wf, mc, trades, cfg)
    if best:
        _, _, s, wf, mc, trades, cfg = best
        print(f"\n  best config {best[0]}: longs {s['longs']}/shorts {s['shorts']} "
              f"(win {s['long_winrate']}%/{s['short_winrate']}%), maxConsecL {s['max_consec_losses']}, "
              f"worst day ${s['worst_day']:.0f}, best ${s['best_day']:.0f}")
        save_outputs(symbol, trades, cfg, s, wf, mc)


def main():
    print("Event-Driven Trend Day Continuation — 3y MNQ. (regular-session Dukascopy NQ = MNQ price)")
    p = os.path.join(CACHE, "nq_1m_3y.csv")
    if not os.path.exists(p):
        print("data missing"); return
    run("MNQ", load_csv(p))


if __name__ == "__main__":
    main()
