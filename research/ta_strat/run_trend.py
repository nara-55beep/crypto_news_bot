"""
run_trend.py — test the ONE edge that prior research + this screen point to:
TREND-FOLLOWING on the higher timeframe (daily), long/short, which should profit in
BOTH directions of a trend — including the -39% bear year we're testing.

Uses the SAME realistic engine (leverage, liquidation, slippage, no-lookahead).
Trend trades ~monthly so cost is negligible; the live variable is LEVERAGE (wide
swing stops => high fixed leverage liquidates). We sweep leverage to show the cliff.

Reports: full multi-year, last-1yr (the requested window), and per-calendar-year.
"""
from __future__ import annotations
import sys, os, warnings
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


def donchian_ls(df, n=20, trend_ma=200, use_filter=True, long_short=True):
    c = df["close"]
    hh = df["high"].rolling(n).max().shift(1)
    ll = df["low"].rolling(n).min().shift(1)
    ma = c.rolling(trend_ma).mean()
    sig = np.zeros(len(df))
    long_ = (c > hh)
    short_ = (c < ll)
    if use_filter:
        long_ = long_ & (c > ma)
        short_ = short_ & (c < ma)
    sig[long_.values] = 1
    if long_short:
        sig[short_.values] = -1
    return sig


def supertrend_ls(df, n=10, mult=3.0, trend_ma=200, long_short=True):
    sdir, _ = ind.supertrend(df["high"], df["low"], df["close"], n, mult)
    ma = df["close"].rolling(trend_ma).mean()
    up = (sdir == 1) & (sdir.shift(1) == -1) & (df["close"] > ma)
    dn = (sdir == -1) & (sdir.shift(1) == 1) & (df["close"] < ma)
    sig = np.zeros(len(df))
    sig[up.values] = 1
    if long_short:
        sig[dn.values] = -1
    return sig


def per_period(trades, eq, start, end, start_eq=100.0):
    t = trades[(trades["entry_time"] >= start) & (trades["entry_time"] < end)]
    if len(t) == 0:
        return None
    e = start_eq
    for f in t["pnl_frac"].values:
        e *= (1 + f)
    wins = (t["pnl_usd"] > 0).mean()
    # drawdown within the window on a synthetic curve
    curve = [start_eq]; ee = start_eq
    for f in t["pnl_frac"].values:
        ee *= (1 + f); curve.append(ee)
    cs = pd.Series(curve)
    dd = (cs / cs.cummax() - 1).min()
    return dict(n=len(t), win=wins, roi=e/start_eq-1, dd=dd, liq=int((t["reason"]=="liq").sum()))


def main():
    df = load_daily()
    atr = ind.atr(df["high"], df["low"], df["close"], 14).values
    print(f"Daily BTC {df.index[0].date()} -> {df.index[-1].date()} ({len(df)} bars)\n")

    strategies = {
        "Donchian20 L/S +200MA": donchian_ls(df, 20, 200, True, True),
        "Donchian55 L/S +200MA": donchian_ls(df, 55, 200, True, True),
        "Donchian20 LONG-only":  donchian_ls(df, 20, 200, True, False),
        "Supertrend L/S +200MA": supertrend_ls(df, 10, 3.0, 200, True),
    }
    last_yr = df.index[-1] - pd.Timedelta(days=365)

    for sname, sig in strategies.items():
        print(f"\n{'='*100}\n{sname}\n{'='*100}")
        print(f"{'lever':>6} | {'trades':>6}{'win%':>6}{'ROI%':>9}{'maxDD%':>8}"
              f"{'Sharpe':>8}{'PF':>6}{'liq':>4}  ||  last-1yr: {'ROI%':>8}{'win%':>6}{'DD%':>7}{'n':>4}")
        for lev in [2, 3, 5, 10, 20]:
            p = StratParams(sizing="risk", risk_frac=0.10, lev_cap=lev,
                            stop_atr=2.5, target_atr=0.0, trail_atr=4.0,
                            slip_bps=3.0, funding_bps_8h=1.0, bars_per_hour=1/24,
                            allow_short=True)
            tr, eq, m = run_backtest(df, sig, atr, p)
            if m.get("n_trades", 0) == 0:
                print(f"{lev:>5}x | no trades"); continue
            ly = per_period(tr, eq, last_yr, df.index[-1] + pd.Timedelta(days=1))
            lystr = (f"{ly['roi']*100:>8.0f}{ly['win']*100:>6.0f}{ly['dd']*100:>7.0f}{ly['n']:>4}"
                     if ly else f"{'--':>8}")
            print(f"{lev:>5}x | {m['n_trades']:>6}{m['win_rate']*100:>6.0f}{m['roi']*100:>9.0f}"
                  f"{m['max_dd']*100:>8.0f}{m['sharpe']:>8}{str(m['profit_factor']):>6}"
                  f"{m['liquidations']:>4}  ||         {lystr}")

    # per-year breakdown for the best config (Donchian20 L/S at 3x)
    print(f"\n{'='*100}\nPER-YEAR — Donchian20 L/S +200MA @ 3x (risk 10%/trade, 2.5ATR stop, 4ATR trail)\n{'='*100}")
    sig = strategies["Donchian20 L/S +200MA"]
    p = StratParams(sizing="risk", risk_frac=0.10, lev_cap=3, stop_atr=2.5, target_atr=0.0,
                    trail_atr=4.0, slip_bps=3.0, funding_bps_8h=1.0, bars_per_hour=1/24)
    tr, eq, m = run_backtest(df, sig, atr, p)
    print(f"{'year':>6}{'trades':>8}{'win%':>7}{'ROI%':>9}{'maxDD%':>9}{'liq':>5}")
    for y in range(df.index[0].year, df.index[-1].year + 1):
        r = per_period(tr, eq, pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y+1}-01-01"))
        if r:
            print(f"{y:>6}{r['n']:>8}{r['win']*100:>7.0f}{r['roi']*100:>9.0f}{r['dd']*100:>9.0f}{r['liq']:>5}")
    print(f"\nFULL: {m['n_trades']} trades, win {m['win_rate']*100:.0f}%, ROI {m['roi']*100:.0f}%, "
          f"maxDD {m['max_dd']*100:.0f}%, Sharpe {m['sharpe']}, PF {m['profit_factor']}, "
          f"final ${m['final_equity']:.0f} from $100, liqs {m['liquidations']}")


if __name__ == "__main__":
    main()
