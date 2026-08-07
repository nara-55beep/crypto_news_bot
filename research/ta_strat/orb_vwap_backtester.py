"""
NY Opening Range Breakout Pullback + VWAP Trend Model — MNQ/MES, Apex-style 50K eval.
Signal/backtest engine only (manual copy).

OR = 09:30-09:45 high/low/mid. Window 09:46-11:00, 1 trade/day.
LONG: break OR-High by >=4 ticks & close above -> (filters) -> pullback to OR-High/VWAP not closing
      below OR-Mid -> entry on reclaim above OR-High / bullish close > prior high -> stop below
      pullback-low or OR-Mid (tighter valid) -> target 2R (also 1.5R / partial / trail).  SHORT = mirror.
Filter configs tested: A none, B VWAP, C +EMA21/50, D +15m trend, E +volume.
Reuses the verified Apex account-sim / trailing-DD / walk-forward / Monte-Carlo from the SB engine.

DATA: 3y Dukascopy ES/NQ 1m (=MES/MNQ price), regular-session. Yahoo MNQ=F/MES=F 1m is ~weeks only
(too short for 3y) — warned. VWAP/OR are intraday so the regular-session limitation is minor here.
"""
from __future__ import annotations
import os, sys, traceback
from dataclasses import dataclass, asdict
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ict_sb_2022_backtester import (load_csv, atr, _mins, in_news, account_sim, stats,
                                     monte_carlo, walk_forward, verdict, save_outputs, CACHE)


@dataclass
class Cfg:
    symbol: str = "MNQ"; pv: float = 2.0; tick: float = 0.25
    comm_rt: float = 1.0; slip_ticks: float = 2.0
    start_bal: float = 50_000.0; profit_target: float = 3_000.0; trail_dd: float = 2_000.0
    risk_usd: float = 100.0; max_micros: int = 4; dd_lockout: float = 600.0
    rr: float = 2.0; target_mode: str = "2R"          # "2R"|"1.5R"|"partial"|"trail"
    max_stop_ticks: int = 50; f_maxstop: bool = True; f_news: bool = True
    f_vwap: bool = False; f_ema: bool = False; f_15m: bool = False; f_vol: bool = False


def _filt(cfg, d, close, vwap, e21, e50, b15, vol, volavg):
    if cfg.f_vwap and np.isfinite(vwap):
        if d == "long" and not close > vwap: return False
        if d == "short" and not close < vwap: return False
    if cfg.f_ema and np.isfinite(e21) and np.isfinite(e50):
        if d == "long" and not e21 > e50: return False
        if d == "short" and not e21 < e50: return False
    if cfg.f_15m and b15 is not None:
        if d == "long" and b15 <= 0: return False
        if d == "short" and b15 >= 0: return False
    if cfg.f_vol and vol is not None and np.isfinite(volavg) and volavg > 0:
        if not vol > 1.2 * volavg: return False
    return True


def _orb_trade(g, ei_time, d, entry, stop, cfg, skips):
    sign = 1 if d == "long" else -1
    slip = cfg.slip_ticks * cfg.tick
    e = entry + sign * slip
    risk = abs(e - stop)
    if risk <= 0:
        skips["bad_levels"] += 1; return None
    if cfg.f_maxstop and (risk / cfg.tick) > cfg.max_stop_ticks:
        skips["stop_wide"] += 1; return None
    contracts = int(max(1, min(cfg.max_micros, round(cfg.risk_usd / (risk * cfg.pv)))))
    risk_d = risk * cfg.pv * contracts
    r1 = e + sign * risk; tgt = e + sign * cfg.rr * risk
    tgt15 = e + sign * 1.5 * risk
    mgmt = g[g.index > ei_time]
    exit_px = reason = None; half = False; bestp = e; cur_stop = stop; realizedR = 0.0
    for t, row in mgmt.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        hit_stop = lo <= cur_stop if d == "long" else hi >= cur_stop
        if cfg.target_mode in ("2R", "1.5R"):
            T = tgt if cfg.target_mode == "2R" else tgt15
            hit_t = hi >= T if d == "long" else lo <= T
            if hit_stop: exit_px, reason = cur_stop, "SL"; break
            if hit_t: exit_px, reason = T, "TP"; break
        elif cfg.target_mode == "partial":
            if hit_stop:
                realizedR += (0.5 if half else 1.0) * ((cur_stop - e) / risk * sign)
                reason = "SL/BE"; exit_px = cur_stop; break
            hit1 = hi >= r1 if d == "long" else lo <= r1
            hit2 = hi >= tgt if d == "long" else lo <= tgt
            if not half and hit1:
                realizedR += 0.5 * 1.0; half = True; cur_stop = e   # ½ off, runner to BE
            if half and hit2:
                realizedR += 0.5 * cfg.rr; reason = "TP2"; exit_px = tgt; break
        elif cfg.target_mode == "trail":
            if hit_stop: exit_px, reason = cur_stop, "trail/SL"; break
            if d == "long":
                bestp = max(bestp, hi)
                if (bestp - e) >= risk: cur_stop = max(cur_stop, e, lo)  # after +1R: BE then trail prior low
            else:
                bestp = min(bestp, lo)
                if (e - bestp) >= risk: cur_stop = min(cur_stop, e, hi)
    if reason is None and cfg.target_mode == "partial" and half:
        last = mgmt.iloc[-1]; realizedR += 0.5 * ((float(last["close"]) - e) / risk * sign); reason = "EOD"
        exit_px = float(last["close"])
    if reason is None:
        last = mgmt.iloc[-1] if len(mgmt) else None
        exit_px = float(last["close"]) if last is not None else e; reason = "EOD"
    if cfg.target_mode == "partial":
        R = realizedR; gross = R * risk_d
    else:
        ex = exit_px + (-sign) * slip
        gross = (ex - e) * contracts * cfg.pv * sign; R = gross / risk_d
    pnl = gross - 2 * cfg.comm_rt * contracts
    R = pnl / risk_d
    return {"day": str(g.index[0].date()), "direction": d, "entry": round(e, 2), "stop": round(stop, 2),
            "target": round(tgt, 2), "reason": reason, "contracts": contracts, "risk_usd": round(risk_d, 2),
            "R": round(R, 3), "pnl": round(pnl, 2)}


def run_day(day, g, prev, cfg, b15, skips):
    m = _mins(g.index)
    orb = g[(m >= 570) & (m <= 584)]                 # 09:30-09:44
    if len(orb) < 10:
        skips["no_or"] += 1; return None
    ORH = float(orb["high"].max()); ORL = float(orb["low"].min()); ORM = (ORH + ORL) / 2
    win = g[(m >= 586) & (m < 660)]                  # 09:46-10:59
    if len(win) < 5:
        skips["no_window"] += 1; return None
    H = win["high"].values; L = win["low"].values; C = win["close"].values; O = win["open"].values
    vwap = win["vwap"].values; e21 = win["ema21"].values; e50 = win["ema50"].values
    vol = win["volume"].values if "volume" in win else None; va = win["volavg20"].values
    T = win.index; n = len(win); tk4 = 4 * cfg.tick
    bdir = None; ext = None; pulled = False
    for i in range(n):
        if cfg.f_news and in_news(_mins(T[i:i + 1])[0]):
            continue
        if bdir is None:
            if H[i] > ORH + tk4 and C[i] > ORH and _filt(cfg, "long", C[i], vwap[i], e21[i], e50[i], b15, (vol[i] if vol is not None else None), va[i]):
                bdir = "long"; ext = L[i]
            elif L[i] < ORL - tk4 and C[i] < ORL and _filt(cfg, "short", C[i], vwap[i], e21[i], e50[i], b15, (vol[i] if vol is not None else None), va[i]):
                bdir = "short"; ext = H[i]
            continue
        # after breakout: track pullback, then entry
        if bdir == "long":
            if C[i] < ORM:
                skips["invalidated"] += 1; return None
            ext = min(ext, L[i])
            if not pulled and (L[i] <= ORH or L[i] <= vwap[i]):
                pulled = True
            if pulled and (C[i] > ORH or (i > 0 and C[i] > H[i - 1] and C[i] > O[i])):
                stop = max(ext, ORM) - 2 * cfg.tick
                if stop < C[i]:
                    return _orb_trade(g, T[i], "long", C[i], stop, cfg, skips)
        else:
            if C[i] > ORM:
                skips["invalidated"] += 1; return None
            ext = max(ext, H[i])
            if not pulled and (H[i] >= ORL or H[i] >= vwap[i]):
                pulled = True
            if pulled and (C[i] < ORL or (i > 0 and C[i] < L[i - 1] and C[i] < O[i])):
                stop = min(ext, ORM) + 2 * cfg.tick
                if stop > C[i]:
                    return _orb_trade(g, T[i], "short", C[i], stop, cfg, skips)
    skips["no_entry"] += 1; return None


def backtest(df, cfg):
    df = df.copy(); df["date"] = df.index.normalize()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["atr14"] = atr(df["high"].values, df["low"].values, df["close"].values, 14)
    if "volume" in df:
        df["volavg20"] = df["volume"].rolling(20).mean()
    else:
        df["volume"] = np.nan; df["volavg20"] = np.nan
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    w = df["volume"].fillna(1.0).clip(lower=1.0)
    df["vwap"] = (tp * w).groupby(df["date"]).cumsum() / w.groupby(df["date"]).cumsum()
    g15 = df.resample("15min").agg({"close": "last"}).dropna()
    g15["ema50"] = g15["close"].ewm(span=50, adjust=False).mean()
    g15["bias"] = np.where(g15["close"] > g15["ema50"], 1, -1)
    days = sorted(df["date"].unique())
    daily = df.groupby("date").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    trades = []; skips = {k: 0 for k in ["no_or", "no_window", "invalidated", "no_entry", "bad_levels", "stop_wide"]}
    for di in range(1, len(days)):
        try:
            g = df[df["date"] == days[di]]
            b15row = g15[g15.index <= g.index[0]]
            b15 = int(b15row["bias"].iloc[-1]) if len(b15row) else None
            tr = run_day(days[di], g, daily.loc[days[di - 1]], cfg, b15, skips)
            if tr:
                trades.append(tr)
        except Exception:
            traceback.print_exc()
    return pd.DataFrame(trades), skips


def run_symbol(symbol, df, pv, maxstop):
    print(f"\n{'#'*70}\n# {symbol}  {df.index[0].date()} -> {df.index[-1].date()}  ({len(df):,} 1m bars)\n{'#'*70}")
    base = dict(symbol=symbol, pv=pv, max_stop_ticks=maxstop)
    configs = [("A none", {}), ("B VWAP", dict(f_vwap=True)),
               ("C +EMA", dict(f_vwap=True, f_ema=True)),
               ("D +15m", dict(f_vwap=True, f_ema=True, f_15m=True)),
               ("E +vol", dict(f_vwap=True, f_ema=True, f_15m=True, f_vol=True))]
    print(f"{'config':<10}{'trades':>7}{'win%':>6}{'PF':>6}{'net$':>9}{'avgR':>7}{'maxDD$':>9}"
          f"{'PFdev/oos':>12}{'MC pass%':>9}{'verdict':>9}")
    for cname, ov in configs:
        cfg = Cfg(**{**base, **ov})
        trades, skips = backtest(df, cfg)
        s = stats(trades, cfg, cname)
        if not s.get("trades"):
            print(f"{cname:<10}{0:>7}  (no trades) skips={skips}"); continue
        wf = walk_forward(trades, cfg); mc = monte_carlo(trades, cfg, n=400)
        ok, _ = verdict(s, wf, mc, cfg)
        print(f"{cname:<10}{s['trades']:>7}{s['win_rate']:>5.0f}{(s['profit_factor'] or 0):>6.2f}"
              f"{s['net_profit']:>+9.0f}{s['avg_R']:>7.3f}{s['max_dd_$']:>9.0f}"
              f"{str(wf.get('pf_dev'))+'/'+str(wf.get('pf_oos')):>12}{(mc.get('p_pass') or 0):>8.0f}%"
              f"{'PASS' if ok else 'FAIL':>9}")
        if cname == "E +vol":
            save_outputs(symbol, trades, cfg, s, wf, mc)
    # target-mode sweep on config D (best-balanced filter)
    print(f"\n  target-mode sweep on config D (VWAP+EMA+15m), {symbol}:")
    for tm in ["1.5R", "2R", "partial", "trail"]:
        cfg = Cfg(**{**base, **dict(f_vwap=True, f_ema=True, f_15m=True, target_mode=tm)})
        trades, skips = backtest(df, cfg)
        s = stats(trades, cfg, "D-" + tm)
        if not s.get("trades"):
            print(f"    {tm:<8} no trades"); continue
        wf = walk_forward(trades, cfg); mc = monte_carlo(trades, cfg, n=400)
        ok, _ = verdict(s, wf, mc, cfg)
        print(f"    {tm:<8} trades {s['trades']} win {s['win_rate']:.0f}% PF {s['profit_factor']} "
              f"net ${s['net_profit']:+.0f} avgR {s['avg_R']} | PF {wf.get('pf_dev')}/{wf.get('pf_oos')} "
              f"MC {mc.get('p_pass')}% -> {'PASS' if ok else 'FAIL'}")


def main():
    print("ORB Pullback + VWAP Trend — 3y MNQ/MES. (Yahoo 1m too short for 3y; using Dukascopy CSV.)")
    for symbol, fname, pv, maxstop in [("MNQ", "nq_1m_3y.csv", 2.0, 50), ("MES", "es_1m_3y.csv", 5.0, 30)]:
        path = os.path.join(CACHE, fname)
        if not os.path.exists(path):
            print(f"{symbol}: data missing"); continue
        run_symbol(symbol, load_csv(path), pv, maxstop)


if __name__ == "__main__":
    main()
