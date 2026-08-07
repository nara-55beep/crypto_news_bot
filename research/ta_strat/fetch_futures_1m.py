"""Fetch 1-minute bars for ES (USA500IDXUSD), NQ (USATECHIDXUSD), CL/WTI (LIGHTCMDUSD) over 3y
from Dukascopy (US-session hours 13-20 UTC), resampled from ticks. For the ICT SM multi-TF test."""
import os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duka_fetch as D

CACHE = D.CACHE
HOURS = range(13, 21)          # US session (covers RTH for index futures + active crude hours)
DIV = 1000.0
INSTR = {"es": "USA500IDXUSD", "nq": "USATECHIDXUSD", "cl": "LIGHTCMDUSD"}


def resample_1m(rows):
    df = pd.DataFrame(rows, columns=["ms", "ask", "bid", "vol"]).drop_duplicates("ms").sort_values("ms")
    df["price"] = (df["ask"] + df["bid"]) / 2.0 / DIV
    df["dt"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df = df.set_index("dt")
    bars = df.resample("1min").agg(open=("price", "first"), high=("price", "max"),
                                   low=("price", "min"), close=("price", "last"),
                                   volume=("vol", "sum")).dropna()
    return bars.reset_index().rename(columns={"dt": "dt_utc"})


def fetch(code, days):
    done = [0]; lock = threading.Lock(); all_bars = []

    def work(job):
        t = D._ticks(*job)
        with lock:
            done[0] += 1
            if done[0] % 2000 == 0:
                print(f"   {code}: {done[0]} files", flush=True)
        return t

    CHUNK = 30
    with ThreadPoolExecutor(max_workers=10) as ex:
        for c0 in range(0, len(days), CHUNK):
            chunk = days[c0:c0 + CHUNK]
            jobs = [(code, dt.year, dt.month - 1, dt.day, h) for dt in chunk for h in HOURS]
            rows = []
            for t in ex.map(work, jobs):
                rows.extend(t)
            if rows:
                all_bars.append(resample_1m(rows))
    if not all_bars:
        return None
    bars = pd.concat(all_bars, ignore_index=True).drop_duplicates("dt_utc").sort_values("dt_utc")
    return bars[["dt_utc", "open", "high", "low", "close", "volume"]]


def main():
    end = pd.Timestamp.now("UTC").normalize().tz_localize(None)
    start = end - pd.Timedelta(days=365 * 3 + 5)
    days = [d for d in pd.date_range(start, end, freq="D") if d.weekday() < 5]
    print(f"1m fetch {start.date()}->{end.date()} ({len(days)} weekdays) for {list(INSTR)}", flush=True)
    for name, code in INSTR.items():
        t0 = time.time()
        bars = fetch(code, days)
        out = os.path.join(CACHE, f"{name}_1m_3y.csv")
        bars.to_csv(out, index=False)
        print(f"[{name}] {code} DONE {len(bars):,} 1m bars  {bars['dt_utc'].iloc[0]} -> "
              f"{bars['dt_utc'].iloc[-1]}  ({time.time()-t0:.0f}s) -> {out}", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
