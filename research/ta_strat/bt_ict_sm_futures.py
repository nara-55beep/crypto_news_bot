"""
bt_ict_sm_futures.py — the EXACT same ICT SM Trades backtest as bt_ict_sm_tf.py (BTC), run on
ES1!, NQ1!, CL1! over 3 years across 1m / 3m / 5m / 15m. Same logic: grab -> MSS -> FVG entry,
stop beyond the grab candle, 2R target; $50,000, no leverage (1x notional per trade); reported
gross (0bps) and net (~2bps/side).

Futures note: futures are inherently leveraged, so "$50k / 1x notional" means sizing each trade
to $50k of notional (i.e., fractional/micro contracts) — the % ROI is the comparable edge metric.
"""
from __future__ import annotations
import os, sys, time
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_ict_sm_tf import resample, run_tf, metrics, START

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
INSTR = [("ES1!", "es"), ("NQ1!", "nq"), ("CL1!", "cl")]
TFS = [("1m", "1min"), ("3m", "3min"), ("5m", "5min"), ("15m", "15min")]


def load(name):
    p = os.path.join(CACHE, f"{name}_1m_3y.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    df["dt"] = pd.to_datetime(df["dt_utc"], utc=True)
    return df.set_index("dt")[["open", "high", "low", "close", "volume"]].sort_index()


def main():
    for disp, name in INSTR:
        df1 = load(name)
        if df1 is None or len(df1) < 1000:
            print(f"\n##### {disp}: no data #####"); continue
        print(f"\n{'#'*104}")
        print(f"# {disp}  —  ICT SM Trades, $50,000, NO leverage (1x notional), 3 years "
              f"({df1.index[0].date()} -> {df1.index[-1].date()}, {len(df1):,} 1m bars)")
        print('#' * 104)
        print(f"{'TF':<5}{'trades':>8}{'win%':>7}{'  ROI% (0bps)':>14}{'  ROI% (2bps)':>14}"
              f"{'maxDD%':>9}{'PF':>7}{'final$ (2bps)':>15}")
        print("-" * 104)
        for label, rule in TFS:
            d = resample(df1, rule)
            tr0, eq0, eqc0 = run_tf(d, 0.0)
            trc, eqc_, eqcc = run_tf(d, 2.0)
            m0 = metrics(tr0, eqc0, eq0); mc = metrics(trc, eqcc, eqc_)
            if not mc:
                print(f"{label:<5} no trades"); continue
            print(f"{label:<5}{mc['n']:>8}{mc['win']*100:>6.0f}{m0['roi']*100:>13.0f}{mc['roi']*100:>14.0f}"
                  f"{mc['dd']*100:>9.0f}{mc['pf']:>7.2f}{mc['final']:>15,.0f}")
        print("-" * 104)
    print("\nROI(0bps)=gross, ROI(2bps)=net of ~2bps/side round-trip. Same strategy + sizing as the BTC test.")


if __name__ == "__main__":
    main()
