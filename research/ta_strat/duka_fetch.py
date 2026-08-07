"""
duka_fetch.py — pull 3 years of 5-minute S&P 500 (USA500IDXUSD = ES proxy) and Nasdaq 100
(USATECHIDXUSD = NQ peer) from Dukascopy's free tick datafeed (no auth), resample to 5m
OHLCV, and save in the format apex_vwap_paper._prepare() expects (dt_utc, dt_ny, OHLCV).

ES tracks the S&P 500 index ~1:1 in points, so USA500 maps directly to the bot's $50/pt.
Volume is Dukascopy tick volume (a consistent VWAP weight; not exchange volume — noted).
Only RTH-relevant UTC hours (13-20) are fetched; _prepare() does the exact 9:30-16:00 ET filter.
"""
from __future__ import annotations
import os, sys, lzma, struct, time, threading
from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
NY = "America/New_York"
HOURS = range(13, 21)                 # UTC hours 13-20 cover RTH+flat(15:55ET) in both EST and EDT
DIV = 1000.0
_local = threading.local()


def _sess():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update({"User-Agent": "Mozilla/5.0"})
    return _local.s


def _ticks(inst, y, m0, d, h):
    # Empty/bad responses are usually RATE-LIMIT throttling (the data IS there), so retry a
    # few times with backoff before accepting a slot as genuinely empty (holiday/closed hour).
    url = f"https://datafeed.dukascopy.com/datafeed/{inst}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
    for attempt in range(5):
        try:
            r = _sess().get(url, timeout=30)
            if not r.content:
                if attempt < 2:
                    time.sleep(0.2); continue
                return []              # still empty after retries -> genuinely no data
            raw = lzma.decompress(r.content, format=lzma.FORMAT_ALONE)
            base = int(pd.Timestamp(year=y, month=m0 + 1, day=d, hour=h, tz="UTC").timestamp() * 1000)
            out = []
            for i in range(0, len(raw), 20):
                ms, ask, bid, av, bv = struct.unpack(">IIIff", raw[i:i + 20])
                out.append((base + ms, ask, bid, av + bv))
            return out
        except lzma.LZMAError:
            time.sleep(0.4 * (attempt + 1)); continue   # bad body = throttle/HTML, retry
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return []


def _resample(rows):
    df = pd.DataFrame(rows, columns=["ms", "ask", "bid", "vol"]).drop_duplicates("ms").sort_values("ms")
    df["price"] = (df["ask"] + df["bid"]) / 2.0 / DIV
    df["dt"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df = df.set_index("dt")
    bars = df.resample("5min").agg(open=("price", "first"), high=("price", "max"),
                                   low=("price", "min"), close=("price", "last"),
                                   volume=("vol", "sum")).dropna()
    return bars.reset_index().rename(columns={"dt": "dt_utc"})


def fetch_instrument(inst, days):
    """Fetch + resample in day-chunks so we never hold all ticks in memory at once
    (Nasdaq has ~10x the S&P tick density -> a full 3y tick list would be many GB)."""
    print(f"[{inst}] {len(days)} weekdays x {len(list(HOURS))} hours ...", flush=True)
    all_bars = []
    done = [0]; lock = threading.Lock()

    def work(job):
        t = _ticks(*job)
        with lock:
            done[0] += 1
            if done[0] % 2000 == 0:
                print(f"   [{inst}] {done[0]} files fetched, {len(all_bars):,} bars so far", flush=True)
        return t

    CHUNK = 30
    with ThreadPoolExecutor(max_workers=10) as ex:
        for c0 in range(0, len(days), CHUNK):
            chunk = days[c0:c0 + CHUNK]
            jobs = [(inst, dt.year, dt.month - 1, dt.day, h) for dt in chunk for h in HOURS]
            rows = []
            for t in ex.map(work, jobs):
                rows.extend(t)
            if rows:
                all_bars.append(_resample(rows))
    if not all_bars:
        return None
    bars = pd.concat(all_bars, ignore_index=True).drop_duplicates("dt_utc").sort_values("dt_utc")
    bars["dt_ny"] = bars["dt_utc"].dt.tz_convert(NY)
    return bars[["dt_utc", "dt_ny", "open", "high", "low", "close", "volume"]]


def main():
    end = pd.Timestamp.utcnow().normalize().tz_localize(None)
    start = end - pd.Timedelta(days=365 * 3 + 5)
    days = [d for d in pd.date_range(start, end, freq="D") if d.weekday() < 5]
    print(f"range {start.date()} -> {end.date()}  ({len(days)} weekdays)\n", flush=True)
    for inst, name in (("USA500IDXUSD", "es"), ("USATECHIDXUSD", "nq")):
        t0 = time.time()
        bars = fetch_instrument(inst, days)
        out = os.path.join(CACHE, f"apex3yr_{name}.csv")
        bars.to_csv(out, index=False)
        print(f"[{inst}] DONE {len(bars):,} 5m bars  {bars['dt_ny'].iloc[0]} -> {bars['dt_ny'].iloc[-1]}  "
              f"-> {out}  ({time.time()-t0:.0f}s)\n", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
