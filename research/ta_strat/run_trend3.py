"""
run_trend3.py — final: stop-based Donchian L/S trend on daily BTC, with an optional
ADX regime filter (only trade when trend strength is real), tested across ALL years to
prove the filter isn't curve-fit to 2024's chop. Produces the final equity curve +
full metric suite for the best honest config.
"""
from __future__ import annotations
import sys, os, warnings, json
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind
from backtest import StratParams, run_backtest

DAILY = "../../crypto-trading-bot/data_cache/binanceusdm_BTCUSDT_1d_1300d.csv"


def load_daily():
    df = pd.read_csv(DAILY)
    df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df[~df.index.duplicated(keep="last")].sort_index()


def donchian_signal(df, n=20, trend_ma=200, adx_min=0):
    c = df["close"]
    hh = df["high"].rolling(n).max().shift(1)
    ll = df["low"].rolling(n).min().shift(1)
    ma = c.rolling(trend_ma).mean()
    adx_, _, _ = ind.adx(df["high"], df["low"], df["close"], 14)
    sig = np.zeros(len(df))
    long_ = (c > hh) & (c > ma)
    short_ = (c < ll) & (c < ma)
    if adx_min > 0:
        long_ = long_ & (adx_ > adx_min)
        short_ = short_ & (adx_ > adx_min)
    sig[long_.values] = 1
    sig[short_.values] = -1
    return sig


def per_year(tr):
    out = {}
    if len(tr) == 0:
        return out
    for y in sorted(set(tr["entry_time"].dt.year)):
        t = tr[tr["entry_time"].dt.year == y]
        e = 100.0
        for f in t["pnl_frac"].values:
            e *= (1 + f)
        out[y] = e / 100 - 1
    return out


def window(tr, start):
    t = tr[tr["entry_time"] >= start]
    if len(t) == 0:
        return None
    e = 100.0; curve = [100.0]
    for f in t["pnl_frac"].values:
        e *= (1 + f); curve.append(e)
    cs = pd.Series(curve)
    return dict(n=len(t), win=(t["pnl_usd"] > 0).mean(), roi=e/100-1,
                dd=(cs/cs.cummax()-1).min())


def main():
    df = load_daily()
    atr = ind.atr(df["high"], df["low"], df["close"], 14).values
    last_yr = df.index[-1] - pd.Timedelta(days=365)

    print(f"Daily BTC {df.index[0].date()} -> {df.index[-1].date()}\n")
    print("Stop-based Donchian20 L/S +200MA, risk 8%/trade, 2.5ATR stop, 4ATR trail, 3bps, ~2x eff\n")
    print(f"{'config':<26}| {'FULL':>22} || {'LAST 1y':>20} || per-year")
    print(f"{'':<26}| {'ROI%':>6}{'win%':>6}{'DD%':>6}{'n':>4} || {'ROI%':>6}{'win%':>6}{'DD%':>6} ||")
    print("-" * 120)

    configs = {
        "no filter (adx>0)":   0,
        "ADX>15 filter":       15,
        "ADX>20 filter":       20,
        "ADX>25 filter":       25,
    }
    best = None
    for cname, adxmin in configs.items():
        sig = donchian_signal(df, 20, 200, adxmin)
        p = StratParams(sizing="risk", risk_frac=0.08, lev_cap=5, stop_atr=2.5,
                        target_atr=0.0, trail_atr=4.0, slip_bps=3.0,
                        funding_bps_8h=1.0, bars_per_hour=1/24)
        tr, eq, m = run_backtest(df, sig, atr, p)
        yr = per_year(tr); ly = window(tr, last_yr)
        yrstr = " ".join(f"{y}:{v*100:>4.0f}" for y, v in yr.items())
        lystr = (f"{ly['roi']*100:>6.0f}{ly['win']*100:>6.0f}{ly['dd']*100:>6.0f}" if ly else "  --")
        pos_y = sum(1 for v in yr.values() if v > 0)
        print(f"{cname:<26}| {m['roi']*100:>6.0f}{m['win_rate']*100:>6.0f}{m['max_dd']*100:>6.0f}"
              f"{m['n_trades']:>4} || {lystr} || {yrstr}  [+yrs {pos_y}/{len(yr)}]")
        if cname == "ADX>20 filter":
            best = (cname, tr, eq, m, p, sig)

    # ---- final artifact for the best honest config ----
    cname, tr, eq, m, p, sig = best
    print(f"\n{'='*100}\nFINAL CONFIG: Donchian20 L/S +200MA, {cname}\n{'='*100}")
    print(json.dumps({k: m[k] for k in ['n_trades','win_rate','roi','profit_usd','final_equity',
          'max_dd','profit_factor','sharpe','expectancy_R','max_consec_loss','liquidations',
          'trades_per_day']}, indent=2, default=str))
    ly = window(tr, last_yr)
    print(f"\nLAST 1 YEAR (the requested window): trades {ly['n']}, win {ly['win']*100:.0f}%, "
          f"ROI {ly['roi']*100:.0f}%, maxDD {ly['dd']*100:.0f}%")
    # save equity curve + trades for plotting/writeup
    eq.to_csv("cache/final_equity.csv")
    tr.to_csv("cache/final_trades.csv", index=False)
    print("\nsaved cache/final_equity.csv, cache/final_trades.csv")


if __name__ == "__main__":
    main()
