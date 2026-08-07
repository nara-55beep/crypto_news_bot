"""
ICT NY Silver Bullet / 2022 Model Backtester  (MNQ / MES, Apex-style 50K eval)

Signal engine (manual-copy, not auto-trade):
  liquidity sweep -> MSS (break 1m swing) -> displacement (>=60% body & >=1.2*ATR14)
  -> FVG -> limit entry at 50% of FVG -> SL at sweep extreme -> 2R/next-liq/partial targets.
Filters (toggle): 15m EMA50 bias, VWAP, min-R, max-stop, news block.
Apex account sim: trailing drawdown done correctly, daily rules, costs. Walk-forward + Monte Carlo.

DATA: CSV (Datetime,Open,High,Low,Close,Volume) preferred. Yahoo MNQ=F/MES=F fallback is limited to
~weeks of 1m -> NOT enough for a 3y test (warned at runtime). This run uses 3y Dukascopy ES/NQ 1m
(=MES/MNQ price); it is REGULAR-SESSION ONLY, so overnight/premarket levels are approximated by the
pre-10:00 range + prior-day levels. Feed a 24h CSV to use true overnight/premarket liquidity.
"""
from __future__ import annotations
import json, os, sys, traceback
from dataclasses import dataclass, field, asdict
import numpy as np, pandas as pd

NY = "America/New_York"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sb2022_out")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


@dataclass
class Cfg:
    symbol: str = "MNQ"
    pv: float = 2.0                 # $/point (MNQ 2, MES 5)
    tick: float = 0.25
    comm_rt: float = 1.0           # commission round-turn per micro
    slip_ticks: float = 2.0        # per side
    start_bal: float = 50_000.0
    profit_target: float = 3_000.0
    trail_dd: float = 2_000.0      # default; also test 2_500
    risk_usd: float = 100.0
    max_micros: int = 4
    dd_lockout: float = 600.0      # no trade if within this of failure
    window: str = "SB"             # "SB"=10:00-11:00, "AM"=09:30-11:00
    target_mode: str = "2R"        # "2R" | "next_liq" | "partial"
    rr: float = 2.0
    f_bias: bool = True            # 15m EMA50
    f_vwap: bool = True
    f_minR: bool = True
    min_R: float = 1.5
    f_maxstop: bool = True
    max_stop_ticks: int = 60       # MNQ 60, MES 40
    f_news: bool = True
    retrace_cancel: int = 20       # cancel limit if no fill within N candles
    disp_body: float = 0.60        # displacement: body >= this * range
    disp_atr: float = 1.2          # displacement: range >= this * ATR(14)


def log(m): print(m, flush=True)


def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    tcol = next((c for c in df.columns if c in ("datetime", "timestamp", "dt_utc", "date", "time")), None)
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    df = df.set_index(tcol)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(NY)
    return df[["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])].sort_index()


def atr(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full(len(c), np.nan); a = tr[:n].mean()
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n; out[i] = a
    return out


def emas(series, n):
    return series.ewm(span=n, adjust=False).mean()


def swings(h, l, k=2):
    n = len(h); sh = np.zeros(n, bool); sl = np.zeros(n, bool)
    for i in range(k, n - k):
        if h[i] == max(h[i - k:i + k + 1]) and h[i] > h[i - 1] and h[i] > h[i + 1]: sh[i] = True
        if l[i] == min(l[i - k:i + k + 1]) and l[i] < l[i - 1] and l[i] < l[i + 1]: sl[i] = True
    return sh, sl


def _mins(idx):
    return idx.hour * 60 + idx.minute


NEWS = [(8 * 60 + 25, 8 * 60 + 45), (9 * 60 + 55, 10 * 60 + 5), (13 * 60 + 55, 14 * 60 + 15)]


def in_news(m):
    return any(a <= m <= b for a, b in NEWS)


def run_day(day, g, prev, cfg, ema15_at, vwap, skips):
    """Return a trade dict or None. g = one day's 1m bars (NY)."""
    m = _mins(g.index)
    win = (m >= 600) & (m < 660) if cfg.window == "SB" else (m >= 570) & (m < 660)
    sb = g[win]
    if len(sb) < 6:
        skips["no_window_data"] += 1; return None
    # liquidity levels available from regular-session data
    pre = g[m < (600 if cfg.window == "SB" else 570)]
    levels_hi = {"PDH": prev["high"], "PMH": (float(pre["high"].max()) if len(pre) else np.nan)}
    levels_lo = {"PDL": prev["low"], "PML": (float(pre["low"].min()) if len(pre) else np.nan)}
    # equal highs/lows in last 50 candles before the window
    last50 = g[m < (600 if cfg.window == "SB" else 570)].tail(50)
    if len(last50) >= 10:
        hh = last50["high"].values; ll = last50["low"].values
        levels_hi["EQH"] = float(np.median(hh[hh >= np.quantile(hh, 0.95)]))
        levels_lo["EQL"] = float(np.median(ll[ll <= np.quantile(ll, 0.05)]))
    hi_levels = {k: v for k, v in levels_hi.items() if np.isfinite(v)}
    lo_levels = {k: v for k, v in levels_lo.items() if np.isfinite(v)}

    H = sb["high"].values; L = sb["low"].values; C = sb["close"].values; O = sb["open"].values
    T = sb.index; n = len(sb)
    a14 = atr(H, L, C, 14)
    sh, sl = swings(H, L, 2)
    tk2 = 2 * cfg.tick

    # find first sweep of a valid level (bear: high>level; bull: low<level), close back within 1-5
    for i in range(n):
        if in_news(_mins(T[i:i + 1])[0]) and cfg.f_news:
            continue
        # BEARISH sweep of a high level
        for name, lvl in hi_levels.items():
            if H[i] > lvl + tk2:
                back = next((j for j in range(i, min(n, i + 6)) if C[j] < lvl), None)
                if back is None:
                    continue
                sweep_hi = float(H[i:back + 1].max())
                ref_lows = [j for j in range(back + 1) if sl[j]]
                if not ref_lows:
                    continue
                ref = min(L[j] for j in ref_lows)
                mss = next((k for k in range(back, n) if C[k] < ref), None)
                if mss is None:
                    continue
                tr = _trade(g, sb, i, back, mss, sweep_hi, "short", name, lvl, cfg, a14,
                            hi_levels, lo_levels, ema15_at, vwap, skips)
                if tr:
                    return tr
        # BULLISH sweep of a low level
        for name, lvl in lo_levels.items():
            if L[i] < lvl - tk2:
                back = next((j for j in range(i, min(n, i + 6)) if C[j] > lvl), None)
                if back is None:
                    continue
                sweep_lo = float(L[i:back + 1].min())
                ref_highs = [j for j in range(back + 1) if sh[j]]
                if not ref_highs:
                    continue
                ref = max(H[j] for j in ref_highs)
                mss = next((k for k in range(back, n) if C[k] > ref), None)
                if mss is None:
                    continue
                tr = _trade(g, sb, i, back, mss, sweep_lo, "long", name, lvl, cfg, a14,
                            hi_levels, lo_levels, ema15_at, vwap, skips)
                if tr:
                    return tr
    skips["no_setup"] += 1
    return None


def _trade(g, sb, sweep_i, back, mss, sweep_ext, direction, lvl_name, lvl, cfg, a14,
           hi_levels, lo_levels, ema15_at, vwap, skips):
    H = sb["high"].values; L = sb["low"].values; C = sb["close"].values; O = sb["open"].values; T = sb.index
    n = len(sb)
    # displacement = the MSS candle (or next) with body>=60% range and range>=1.2*ATR
    disp = None
    for k in (mss, mss + 1):
        if k >= n or not np.isfinite(a14[k]):
            continue
        rng = H[k] - L[k]; body = abs(C[k] - O[k])
        if rng <= 0:
            continue
        ok_dir = (C[k] > O[k]) if direction == "long" else (C[k] < O[k])
        if ok_dir and body >= cfg.disp_body * rng and rng >= cfg.disp_atr * a14[k]:
            disp = k; break
    if disp is None:
        skips["no_displacement"] += 1; return None
    # FVG around the displacement (3 candles: disp-1, disp, disp+1)
    if disp + 1 >= n:
        skips["no_fvg"] += 1; return None
    if direction == "long":
        if not (L[disp + 1] > H[disp - 1]):
            skips["no_fvg"] += 1; return None
        fvg_lo, fvg_hi = H[disp - 1], L[disp + 1]
    else:
        if not (H[disp + 1] < L[disp - 1]):
            skips["no_fvg"] += 1; return None
        fvg_lo, fvg_hi = H[disp + 1], L[disp - 1]
    entry = (fvg_lo + fvg_hi) / 2.0
    if direction == "long":
        stop = sweep_ext - 2 * cfg.tick
        if not (stop < entry):
            skips["bad_levels"] += 1; return None
    else:
        stop = sweep_ext + 2 * cfg.tick
        if not (stop > entry):
            skips["bad_levels"] += 1; return None
    risk_pts = abs(entry - stop)
    # target
    if cfg.target_mode == "next_liq":
        if direction == "long":
            ups = [v for v in hi_levels.values() if v > entry]
            tgt = min(ups) if ups else entry + cfg.rr * risk_pts
        else:
            dns = [v for v in lo_levels.values() if v < entry]
            tgt = max(dns) if dns else entry - cfg.rr * risk_pts
    else:
        tgt = entry + (1 if direction == "long" else -1) * cfg.rr * risk_pts
    rr_to_tgt = abs(tgt - entry) / risk_pts if risk_pts else 0

    # ---- filters ----
    if cfg.f_maxstop and (risk_pts / cfg.tick) > cfg.max_stop_ticks:
        skips["stop_too_wide"] += 1; return None
    if cfg.f_minR and rr_to_tgt < cfg.min_R:
        skips["below_minR"] += 1; return None
    if cfg.f_bias and ema15_at is not None:
        if direction == "long" and not (entry > ema15_at): skips["bias_block"] += 1; return None
        if direction == "short" and not (entry < ema15_at): skips["bias_block"] += 1; return None
    if cfg.f_vwap and np.isfinite(vwap):
        if direction == "long" and not (entry >= vwap): skips["vwap_block"] += 1; return None
        if direction == "short" and not (entry <= vwap): skips["vwap_block"] += 1; return None

    # ---- entry fill: limit at FVG 50%, cancel after retrace_cancel candles ----
    fill_i = None
    for f in range(disp + 1, min(n, disp + 1 + cfg.retrace_cancel)):
        if (direction == "long" and L[f] <= entry) or (direction == "short" and H[f] >= entry):
            fill_i = f; break
    if fill_i is None:
        skips["unfilled"] += 1; return None
    fill_t = T[fill_i]
    # ---- manage on full day after fill ----
    slip = cfg.slip_ticks * cfg.tick
    e_fill = entry + (1 if direction == "long" else -1) * slip   # adverse slippage on entry
    risk_pts2 = abs(e_fill - stop)
    contracts = int(max(1, min(cfg.max_micros, round(cfg.risk_usd / (risk_pts2 * cfg.pv)))))
    risk_dollars = risk_pts2 * cfg.pv * contracts
    mgmt = g[g.index >= fill_t]
    exit_t = exit_px = reason = None
    for t, row in mgmt.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if direction == "long":
            if lo <= stop: exit_px, reason = stop, "SL"; exit_t = t; break
            if hi >= tgt: exit_px, reason = tgt, "TP"; exit_t = t; break
        else:
            if hi >= stop: exit_px, reason = stop, "SL"; exit_t = t; break
            if lo <= tgt: exit_px, reason = tgt, "TP"; exit_t = t; break
    if reason is None:
        exit_t = mgmt.index[-1]; exit_px = float(mgmt.iloc[-1]["close"]); reason = "EOD"
    exit_px += (-1 if direction == "long" else 1) * slip          # adverse slippage on exit
    gross = (exit_px - e_fill) * contracts * cfg.pv * (1 if direction == "long" else -1)
    pnl = gross - 2 * cfg.comm_rt * contracts
    R = pnl / risk_dollars if risk_dollars else 0
    return {"day": str(g.index[0].date()), "direction": direction, "window": cfg.window,
            "liquidity": lvl_name, "sweep_px": round(sweep_ext, 2), "mss_px": round(C[mss], 2),
            "fvg_lo": round(fvg_lo, 2), "fvg_hi": round(fvg_hi, 2),
            "entry_time": fill_t, "exit_time": exit_t, "entry": round(e_fill, 2),
            "stop": round(stop, 2), "target": round(tgt, 2), "exit": round(exit_px, 2),
            "reason": reason, "contracts": contracts, "risk_usd": round(risk_dollars, 2),
            "rr_planned": round(rr_to_tgt, 2), "R": round(R, 3), "pnl": round(pnl, 2),
            "signal": f"{direction} {lvl_name} sweep->MSS->disp->FVG"}


def backtest(df, cfg):
    df = df.copy()
    df["date"] = df.index.normalize()
    # 15m EMA50 + session VWAP precompute
    g15 = df.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    g15["ema50"] = emas(g15["close"], 50)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    df["_dvwap"] = (tp.groupby(df["date"]).cumsum() / (pd.Series(1.0, index=df.index).groupby(df["date"]).cumsum()))
    days = sorted(df["date"].unique())
    daily = df.groupby("date").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    trades = []; skips = {k: 0 for k in ["no_window_data", "no_setup", "no_displacement", "no_fvg",
                          "bad_levels", "stop_too_wide", "below_minR", "bias_block", "vwap_block", "unfilled"]}
    for di in range(1, len(days)):
        day = days[di]
        try:
            g = df[df["date"] == day]
            prev = daily.loc[days[di - 1]]
            e15 = g15[g15.index <= g.index[0]]
            ema15_at = float(e15["ema50"].iloc[-1]) if len(e15) and np.isfinite(e15["ema50"].iloc[-1]) else None
            vwap = float(g["_dvwap"].iloc[len(g[_mins(g.index) < 600]) - 1]) if cfg.f_vwap else np.nan
            tr = run_day(day, g, prev, cfg, ema15_at, vwap, skips)
            if tr:
                trades.append(tr)
        except Exception:
            traceback.print_exc()
    return pd.DataFrame(trades), skips


# ---------------- Apex account sim (correct trailing DD) ----------------
def account_sim(trades, cfg, shuffle_seed=None):
    if len(trades) == 0:
        return {"passed": False, "failed_dd": False, "final": cfg.start_bal, "days_to_pass": None,
                "max_dd": 0.0, "equity": [cfg.start_bal]}
    R = trades["R"].values.copy(); risk = trades["risk_usd"].values.copy()
    pnl = R * risk
    if shuffle_seed is not None:
        rng = np.random.default_rng(shuffle_seed); idx = rng.permutation(len(pnl)); pnl = pnl[idx]
    bal = cfg.start_bal; peak = cfg.start_bal; thresh = cfg.start_bal - cfg.trail_dd
    eq = [bal]; passed = failed = False; dtp = None
    for i, p in enumerate(pnl):
        if (bal - thresh) < cfg.dd_lockout:               # within $600 of failure -> sit out
            continue
        bal += p; eq.append(bal)
        peak = max(peak, bal); thresh = max(thresh, peak - cfg.trail_dd)
        if bal <= thresh and not passed:
            failed = True; break
        if bal >= cfg.start_bal + cfg.profit_target and not passed:
            passed = True; dtp = i + 1; break
    e = np.array(eq); mdd = float((e - np.maximum.accumulate(e)).min())
    return {"passed": passed, "failed_dd": failed, "final": bal, "days_to_pass": dtp,
            "max_dd": mdd, "equity": eq}


def stats(trades, cfg, label):
    if len(trades) == 0:
        return {"label": label, "trades": 0}
    R = trades["R"]; pnl = trades["pnl"]; wins = trades[pnl > 0]; losses = trades[pnl <= 0]
    pf = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() != 0 else float("inf")
    eqc = pnl.cumsum().values; mdd = float((eqc - np.maximum.accumulate(eqc)).min())
    daily = trades.groupby("day")["pnl"].sum()
    streak = mx = 0
    for p in pnl:
        streak = streak + 1 if p <= 0 else 0; mx = max(mx, streak)
    acc = account_sim(trades, cfg)
    return {"label": label, "symbol": cfg.symbol, "trades": int(len(trades)),
            "win_rate": round((pnl > 0).mean() * 100, 1),
            "profit_factor": round(pf, 2) if np.isfinite(pf) else None,
            "net_profit": round(pnl.sum(), 2), "final_balance": round(cfg.start_bal + pnl.sum(), 2),
            "avg_trade": round(pnl.mean(), 2), "avg_R": round(R.mean(), 3),
            "avg_win": round(wins["pnl"].mean(), 2) if len(wins) else 0,
            "avg_loss": round(losses["pnl"].mean(), 2) if len(losses) else 0,
            "max_dd_$": round(mdd, 2), "worst_day": round(daily.min(), 2), "best_day": round(daily.max(), 2),
            "max_consec_losses": int(mx), "days_traded": int(daily.shape[0]),
            "longs": int((trades["direction"] == "long").sum()), "shorts": int((trades["direction"] == "short").sum()),
            "long_winrate": round((trades[trades.direction == "long"]["pnl"] > 0).mean() * 100, 1) if (trades.direction == "long").any() else None,
            "short_winrate": round((trades[trades.direction == "short"]["pnl"] > 0).mean() * 100, 1) if (trades.direction == "short").any() else None,
            "eval_passed": acc["passed"], "eval_failed_dd": acc["failed_dd"], "days_to_pass": acc["days_to_pass"]}


def monte_carlo(trades, cfg, n=400):
    if len(trades) < 5:
        return {}
    passes = fails = 0; finals = []; streaks = []; dtps = []
    for s in range(n):
        a = account_sim(trades, cfg, shuffle_seed=s)
        passes += int(a["passed"]); fails += int(a["failed_dd"]); finals.append(a["final"])
        if a["days_to_pass"]: dtps.append(a["days_to_pass"])
    finals = np.array(finals)
    return {"mc_runs": n, "p_pass": round(passes / n * 100, 1), "p_fail_dd": round(fails / n * 100, 1),
            "median_final": round(float(np.median(finals)), 2),
            "worst5pct_final": round(float(np.quantile(finals, 0.05)), 2),
            "best5pct_final": round(float(np.quantile(finals, 0.95)), 2),
            "median_days_to_pass": int(np.median(dtps)) if dtps else None}


def walk_forward(trades, cfg):
    if len(trades) < 10:
        return {"pass": False, "reason": "too few trades"}
    days = sorted(trades["day"].unique()); mid = days[len(days) // 2]
    dev = trades[trades["day"] < mid]; oos = trades[trades["day"] >= mid]
    def pf(t):
        w = t[t.pnl > 0]["pnl"].sum(); l = abs(t[t.pnl <= 0]["pnl"].sum())
        return (w / l) if l > 0 else float("inf")
    pf_dev, pf_oos = pf(dev), pf(oos)
    return {"pf_dev": round(pf_dev, 2), "pf_oos": round(pf_oos, 2),
            "avg_dev": round(dev["pnl"].mean(), 2), "avg_oos": round(oos["pnl"].mean(), 2),
            "n_dev": len(dev), "n_oos": len(oos),
            "both_pf_gt_1_25": bool(pf_dev > 1.25 and pf_oos > 1.25)}


def verdict(s, wf, mc, cfg):
    checks = {
        "PF>1.25 both halves": wf.get("both_pf_gt_1_25", False),
        "avg trade positive": (s.get("avg_trade", -1) or -1) > 0,
        ">=50 trades": s.get("trades", 0) >= 50,
        "max DD < 75% of allowed": abs(s.get("max_dd_$", 1e9)) < 0.75 * cfg.trail_dd,
        "MC pass prob > 60%": (mc.get("p_pass", 0) or 0) > 60,
        "MC fail-DD prob < 25%": (mc.get("p_fail_dd", 100) or 100) < 25,
    }
    return all(checks.values()), checks


def save_outputs(symbol, trades, cfg, s, wf, mc):
    os.makedirs(OUT, exist_ok=True); pre = os.path.join(OUT, symbol)
    if len(trades):
        trades.to_csv(pre + "_trades.csv", index=False)
        acc = account_sim(trades, cfg)
        pd.DataFrame({"equity": acc["equity"]}).to_csv(pre + "_equity_curve.csv", index=False)
        trades.groupby("day")["pnl"].sum().to_csv(pre + "_daily_stats.csv")
    json.dump({"summary": s, "walk_forward": wf, "monte_carlo": mc}, open(pre + "_summary.json", "w"), indent=2, default=str)


def run_symbol(symbol, df, base_cfg):
    log(f"\n{'#'*70}\n# {symbol}  —  {df.index[0].date()} -> {df.index[-1].date()}  ({len(df):,} 1m bars)\n{'#'*70}")
    results = {}
    for label, overrides in [("DEFAULT (all filters, 2R, SB 10-11)", {}),
                             ("NO FILTERS", dict(f_bias=False, f_vwap=False, f_minR=False, f_maxstop=False, f_news=False)),
                             ("AM window 09:30-11:00", dict(window="AM")),
                             ("RELAXED (no filters, disp .5/1.0xATR, leg, 2R)",
                              dict(f_bias=False, f_vwap=False, f_minR=False, f_maxstop=False, f_news=False,
                                   disp_body=0.5, disp_atr=1.0))]:
        cfg = Cfg(**{**asdict(base_cfg), **overrides})
        trades, skips = backtest(df, cfg)
        s = stats(trades, cfg, label)
        wf = walk_forward(trades, cfg) if len(trades) else {}
        mc = monte_carlo(trades, cfg) if len(trades) else {}
        log(f"\n----- {label} -----")
        log(f"  trades {s.get('trades',0)} | win {s.get('win_rate','-')}% | PF {s.get('profit_factor','-')} | "
            f"net ${s.get('net_profit',0):+,.0f} | avgR {s.get('avg_R','-')} | maxDD ${s.get('max_dd_$',0):,.0f}")
        if s.get("trades", 0):
            log(f"  longs/shorts {s['longs']}/{s['shorts']} (win {s['long_winrate']}%/{s['short_winrate']}%) | "
                f"maxConsecL {s['max_consec_losses']} | daysTraded {s['days_traded']} | worst day ${s['worst_day']:,.0f}")
            log(f"  EVAL: passed={s['eval_passed']} failed_dd={s['eval_failed_dd']} days_to_pass={s['days_to_pass']}")
            log(f"  walk-forward: PF dev {wf.get('pf_dev')} / oos {wf.get('pf_oos')} | both>1.25 {wf.get('both_pf_gt_1_25')}")
            log(f"  Monte Carlo: P(pass) {mc.get('p_pass')}% | P(fail-DD) {mc.get('p_fail_dd')}% | "
                f"median final ${mc.get('median_final')} | worst5% ${mc.get('worst5pct_final')}")
            log(f"  skip reasons: {skips}")
            ok, checks = verdict(s, wf, mc, cfg)
            log(f"  >>> VALIDATION: {'PASS' if ok else 'FAIL'}  {checks}")
            if label.startswith("DEFAULT"):
                save_outputs(symbol, trades, cfg, s, wf, mc)
        results[label] = s
    return results


def main():
    log("ICT NY Silver Bullet / 2022 Backtester — 3y. (Yahoo 1m is too short for 3y; using Dukascopy CSV.)")
    for symbol, fname, pv, maxstop in [("MNQ", "nq_1m_3y.csv", 2.0, 60), ("MES", "es_1m_3y.csv", 5.0, 40)]:
        path = os.path.join(CACHE, fname)
        if not os.path.exists(path):
            log(f"{symbol}: data missing ({path})"); continue
        df = load_csv(path)
        base = Cfg(symbol=symbol, pv=pv, max_stop_ticks=maxstop)
        run_symbol(symbol, df, base)


if __name__ == "__main__":
    main()
