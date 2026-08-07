"""Fetch FULL 24h ES (S&P) 5m for 3y from Dukascopy — overnight included, for faithful ICT
(overnight high/low, Asian range, London/NY killzones). Reuses duka_fetch's machinery."""
import os, sys, time
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duka_fetch as D

D.HOURS = range(0, 24)        # full session
CACHE = D.CACHE


def main():
    end = pd.Timestamp.now("UTC").normalize().tz_localize(None)
    start = end - pd.Timedelta(days=365 * 3 + 5)
    days = [d for d in pd.date_range(start, end, freq="D") if d.weekday() < 5]
    print(f"24h ES {start.date()} -> {end.date()} ({len(days)} weekdays x 24h)", flush=True)
    t0 = time.time()
    bars = D.fetch_instrument("USA500IDXUSD", days)
    out = os.path.join(CACHE, "apex3yr_es_24h.csv")
    bars.to_csv(out, index=False)
    print(f"DONE {len(bars):,} 5m bars {bars['dt_ny'].iloc[0]} -> {bars['dt_ny'].iloc[-1]} "
          f"-> {out} ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
