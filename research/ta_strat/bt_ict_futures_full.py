"""Full ICT SM / ICT-2022 backtest on ES1!, NQ1!, CL1! (3y, $50k, no leverage, 1m/3m/5m/15m).
Two configs per instrument: BASE (all session bars, 2R) and KILLZONE-refined (NY-AM gate, 2R)
— the killzone filter that boosted BTC's faster timeframes. Gross (0bps) and net (~2bps/side)."""
from __future__ import annotations
import os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_ict_sm_tf import resample
from bt_ict_2022 import run

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
        print(f"\n{'='*92}\n{disp}  —  $50k, no leverage, 3y ({df1.index[0].date()}->{df1.index[-1].date()}, {len(df1):,} 1m bars)\n{'='*92}")
        for tag, kz, bias, tr in [("BASE (session, 2R)", False, False, 2.0),
                                  ("KILLZONE NY-AM, 2R", True, False, 2.0)]:
            print(f"--- {tag} ---")
            print(f"{'TF':<5}{'trades':>8}{'win%':>7}{'ROI%(0bps)':>12}{'ROI%(2bps)':>12}{'maxDD%':>9}{'PF':>7}{'final$':>12}")
            for label, rule in TFS:
                d = resample(df1, rule)
                m0 = run(d, 0.0, tr, kz, bias); mc = run(d, 2.0, tr, kz, bias)
                if not mc:
                    print(f"{label:<5}   (no trades)"); continue
                print(f"{label:<5}{mc['n']:>8}{mc['win']*100:>6.0f}{m0['roi']*100:>11.0f}{mc['roi']*100:>12.0f}"
                      f"{mc['dd']*100:>9.0f}{mc['pf']:>7.2f}{mc['final']:>12,.0f}")
            print()


if __name__ == "__main__":
    main()
