"""
robustness.py — is the new V2 (fixed-2R exit) robust, or just a lucky window?

Uses the SAME live V2 entries + exit (calls run_backtest). All slices are built from
the per-trade 'r' (R-multiple), with a FIXED 0.5% risk per trade (non-compounded) so
segments are additive and comparable. No parameter optimization here.
"""

from __future__ import annotations

import json
import numpy as np
import pandas as pd

from backend.config import Settings
from backend.data.fetcher import fetch_history_df
from backend.exchange import make_exchange
from backend.strategy import indicators as ind
from backend.backtest.engine import run_backtest

DAYS = 180
RISK = 0.005
START = 10_000.0
SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
_EX = make_exchange(Settings())


def get_trades(sym, fee_mult=1.0):
    s = Settings(); s.backtest_days = DAYS
    s.fee_rate *= fee_mult; s.slippage *= fee_mult
    res = run_backtest(s, sym, exchange=_EX, save=False)
    for t in res.get("trades", []):
        t["symbol"] = sym
        t["entry_dt"] = pd.to_datetime(t["entry_time"], utc=True)
        t["exit_dt"] = pd.to_datetime(t["exit_time"], utc=True)
        t["pnl_fixed"] = t["r"] * RISK * START
    return res.get("trades", [])


def M(trades):
    """Metrics from a trade list (fixed-risk basis)."""
    if not trades:
        return {"n": 0, "ret%": 0, "PF": None, "win%": 0, "maxDD%": 0, "exp_R": 0, "avgWin_R": 0, "avgLoss_R": 0}
    ts = sorted(trades, key=lambda t: t["exit_dt"])
    r = np.array([t["r"] for t in ts])
    pnl = np.array([t["pnl_fixed"] for t in ts])
    eq = START + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min()
    w, l = r[r > 0], r[r < 0]
    return {
        "n": len(r), "ret%": round(pnl.sum() / START * 100, 2),
        "PF": round(float(w.sum() / -l.sum()), 3) if l.sum() < 0 else None,
        "win%": round(len(w) / len(r) * 100, 1), "maxDD%": round(float(dd) * 100, 2),
        "exp_R": round(float(r.mean()), 4),
        "avgWin_R": round(float(w.mean()), 2) if len(w) else 0,
        "avgLoss_R": round(float(l.mean()), 2) if len(l) else 0,
    }


def spark(vals, width=64):
    blocks = " .:-=+*#"          # ASCII-safe (Windows cp1252 console can't print block glyphs)
    if not len(vals):
        return ""
    v = np.array(vals, dtype=float)
    idx = np.linspace(0, len(v) - 1, min(width, len(v))).astype(int)
    v = v[idx]
    lo, hi = v.min(), v.max()
    if hi - lo < 1e-9:
        return blocks[0] * len(v)
    return "".join(blocks[int((x - lo) / (hi - lo) * 7)] for x in v)


def regime_series_btc():
    df = fetch_history_df(_EX, "BTC/USDT", "4h", DAYS)
    ema200 = ind.ema(df["close"], 200)
    slope = ema200.diff(10)
    reg = pd.Series("chop", index=df.index)
    reg[(df["close"] > ema200) & (slope > 0)] = "bull"
    reg[(df["close"] < ema200) & (slope < 0)] = "bear"
    return reg


def run():
    # ---------- exact window ----------
    btc15 = fetch_history_df(_EX, "BTC/USDT", "15m", DAYS)
    start, end = btc15.index[0], btc15.index[-1]
    print("================= BACKTEST WINDOW =================")
    print(f"start: {start}")
    print(f"end:   {end}")
    print(f"calendar days: {(end - start).days}  ({len(btc15)} x 15m bars)")

    trades = {s: get_trades(s) for s in SYMS}
    trades2x = {s: get_trades(s, fee_mult=2.0) for s in SYMS}
    allt = sum(trades.values(), [])

    # ---------- 1. per symbol ----------
    print("\n================= 1) PER SYMBOL (fixed-risk basis) =================")
    for s in SYMS:
        print(f"{s:9} {M(trades[s])}")

    # ---------- 2/3/4. portfolios ----------
    print("\n================= 2-4) PORTFOLIOS =================")
    print(f"BTC only      {M(trades['BTC/USDT'])}")
    print(f"SOL only      {M(trades['SOL/USDT'])}")
    print(f"BTC+SOL (noETH){M(trades['BTC/USDT'] + trades['SOL/USDT'])}")
    print(f"all three      {M(allt)}")

    # ---------- 5. 2x fees/slippage ----------
    print("\n================= 5) 2x FEES + SLIPPAGE =================")
    for s in SYMS:
        print(f"{s:9} {M(trades2x[s])}")
    print(f"BTC+SOL 2x    {M(trades2x['BTC/USDT'] + trades2x['SOL/USDT'])}")
    print(f"all three 2x  {M(sum(trades2x.values(), []))}")

    # ---------- 6. in-sample / out-of-sample (60/40 by calendar) ----------
    print("\n================= 6) IN-SAMPLE (first 60%) vs OUT-OF-SAMPLE (last 40%) =================")
    split = start + (end - start) * 0.6
    print(f"split at {split}")
    for label, sel in (("BTC+SOL", trades['BTC/USDT'] + trades['SOL/USDT']), ("all three", allt)):
        IS = [t for t in sel if t["entry_dt"] < split]
        OOS = [t for t in sel if t["entry_dt"] >= split]
        print(f"{label:10} IS  {M(IS)}")
        print(f"{label:10} OOS {M(OOS)}")

    # ---------- 7. monthly ----------
    print("\n================= 7) MONTHLY RETURN % (fixed-risk) =================")
    def monthly(ts):
        d = {}
        for t in ts:
            m = t["exit_dt"].strftime("%Y-%m")
            d[m] = d.get(m, 0) + t["pnl_fixed"]
        return {k: round(v / START * 100, 2) for k, v in sorted(d.items())}
    for s in SYMS:
        print(f"{s:9} {monthly(trades[s])}")
    print(f"BTC+SOL   {monthly(trades['BTC/USDT'] + trades['SOL/USDT'])}")

    # ---------- 8. long vs short ----------
    print("\n================= 8) LONG vs SHORT =================")
    for label, sel in (("BTC+SOL", trades['BTC/USDT'] + trades['SOL/USDT']), ("all three", allt)):
        print(f"{label:10} long  {M([t for t in sel if t['side']=='long'])}")
        print(f"{label:10} short {M([t for t in sel if t['side']=='short'])}")

    # ---------- 9. by market regime (BTC 4H) ----------
    print("\n================= 9) BY MARKET REGIME (BTC 4H: bull/bear/chop) =================")
    reg = regime_series_btc()
    rdf = reg.reset_index(); rdf.columns = ["dt", "regime"]
    rdf["dt"] = pd.to_datetime(rdf["dt"], utc=True).dt.as_unit("ns")
    def label_regime(ts):
        td = pd.DataFrame({"dt": [t["entry_dt"] for t in ts]})
        td["dt"] = td["dt"].dt.as_unit("ns")
        merged = pd.merge_asof(td.sort_values("dt"), rdf.sort_values("dt"), on="dt", direction="backward")
        return list(merged["regime"])
    labels = label_regime(allt)
    for rg in ("bull", "bear", "chop"):
        sub = [t for t, lb in zip(sorted(allt, key=lambda t: t["entry_dt"]), labels) if lb == rg]
        print(f"{rg:5} {M(sub)}")

    # ---------- 10. equity + drawdown curve (BTC+SOL) ----------
    print("\n================= 10) EQUITY + DRAWDOWN CURVE (BTC+SOL, fixed-risk) =================")
    port = sorted(trades['BTC/USDT'] + trades['SOL/USDT'], key=lambda t: t["exit_dt"])
    pnl = np.array([t["pnl_fixed"] for t in port])
    eq = START + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    ddc = (eq - peak) / peak * 100
    print(f"equity   {eq[0]:.0f} -> {eq[-1]:.0f}   {spark(eq)}")
    print(f"drawdown 0% .. {ddc.min():.1f}%        {spark(-ddc)}  (deeper = bigger DD)")

    # save full curves + a machine-readable summary
    out = {
        "window": {"start": str(start), "end": str(end), "days": (end - start).days},
        "per_symbol": {s: M(trades[s]) for s in SYMS},
        "btc_sol": M(trades['BTC/USDT'] + trades['SOL/USDT']),
        "equity_curve": [[str(t["exit_dt"]), round(float(e), 2)] for t, e in zip(port, eq)],
    }
    with open("robustness_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nsaved -> robustness_results.json")


if __name__ == "__main__":
    run()
