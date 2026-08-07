"""
fetch_lucid_10y.py — pull 2016-06-27 .. 2023-06-18 Dukascopy ticks for NQ (USATECHIDXUSD)
and CL (LIGHTCMDUSD), resample to 1m mid-price OHLCV (same as lucid_pass_paper's
_duka_rows_to_1m), save cache/{m}_1m_old7y.csv, then splice with the existing
{m}_1m_3y.csv into {m}_1m_10y.csv. ES already has es_1m_10y.csv.

Resumable: appends day-chunks to {m}_1m_old7y.part.csv and skips days already present.
"""
from __future__ import annotations
import os, sys, lzma, struct, time, threading
from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
HOURS = range(13, 21)
DIV = 1000.0
START = pd.Timestamp("2016-06-27")
END = pd.Timestamp("2023-06-18")
INSTS = [("USATECHIDXUSD", "nq"), ("LIGHTCMDUSD", "cl")]
_local = threading.local()


def _sess():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update({"User-Agent": "Mozilla/5.0"})
    return _local.s


def _ticks(inst, y, m0, d, h):
    url = f"https://datafeed.dukascopy.com/datafeed/{inst}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
    for attempt in range(5):
        try:
            r = _sess().get(url, timeout=30)
            if r.status_code in (404, 410):
                return []
            if r.status_code != 200:
                time.sleep(0.4 * (attempt + 1)); continue
            if not r.content:
                if attempt < 2:
                    time.sleep(0.2); continue
                return []
            raw = lzma.decompress(r.content, format=lzma.FORMAT_ALONE)
            base = int(pd.Timestamp(year=y, month=m0 + 1, day=d, hour=h, tz="UTC").timestamp() * 1000)
            out = []
            for i in range(0, len(raw), 20):
                ms, ask, bid, av, bv = struct.unpack(">IIIff", raw[i:i + 20])
                out.append((base + ms, ask, bid, av + bv))
            return out
        except lzma.LZMAError:
            time.sleep(0.4 * (attempt + 1))
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return []


def _to_1m(rows):
    df = pd.DataFrame(rows, columns=["ms", "ask", "bid", "vol"]).drop_duplicates("ms").sort_values("ms")
    df["price"] = (df["ask"] + df["bid"]) / 2.0 / DIV
    df["dt_utc"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    bars = df.set_index("dt_utc").resample("1min").agg(
        open=("price", "first"), high=("price", "max"),
        low=("price", "min"), close=("price", "last"),
        volume=("vol", "sum"),
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    return bars[["dt_utc", "open", "high", "low", "close", "volume"]]


def fetch_instrument(inst, market):
    part = os.path.join(CACHE, f"{market}_1m_old7y.part.csv")
    done_days = set()
    if os.path.exists(part):
        try:
            prev = pd.read_csv(part, usecols=["dt_utc"])
            done_days = set(pd.to_datetime(prev["dt_utc"], utc=True).dt.date)
            print(f"[{market}] resume: {len(done_days)} days already in part file", flush=True)
        except Exception:
            done_days = set()
    days = [d for d in pd.date_range(START, END, freq="D")
            if d.weekday() < 5 and d.date() not in done_days]
    print(f"[{market}] fetching {len(days)} weekdays x {len(list(HOURS))} hours", flush=True)
    done = [0]; lock = threading.Lock()
    t0 = time.time()

    def work(job):
        t = _ticks(*job)
        with lock:
            done[0] += 1
            if done[0] % 1000 == 0:
                rate = done[0] / max(time.time() - t0, 1)
                print(f"   [{market}] {done[0]} files, {rate:.1f}/s, "
                      f"eta {((len(days)*8-done[0])/max(rate,0.1))/60:.0f}min", flush=True)
        return t

    CHUNK = 20
    header_needed = not os.path.exists(part)
    with ThreadPoolExecutor(max_workers=12) as ex:
        for c0 in range(0, len(days), CHUNK):
            chunk = days[c0:c0 + CHUNK]
            jobs = [(inst, dt.year, dt.month - 1, dt.day, h) for dt in chunk for h in HOURS]
            rows = []
            for t in ex.map(work, jobs):
                rows.extend(t)
            if rows:
                bars = _to_1m(rows)
                bars.to_csv(part, mode="a", header=header_needed, index=False)
                header_needed = False
    # finalize: dedupe/sort -> old7y csv
    df = pd.read_csv(part)
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    df = df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)
    out = os.path.join(CACHE, f"{market}_1m_old7y.csv")
    df.to_csv(out, index=False)
    print(f"[{market}] old7y done: {len(df):,} bars {df['dt_utc'].iloc[0]} -> {df['dt_utc'].iloc[-1]}", flush=True)
    # splice with 3y
    new = pd.read_csv(os.path.join(CACHE, f"{market}_1m_3y.csv"))
    new["dt_utc"] = pd.to_datetime(new["dt_utc"], utc=True)
    new = new[["dt_utc", "open", "high", "low", "close", "volume"]]
    full = pd.concat([df, new], ignore_index=True).drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)
    full.to_csv(os.path.join(CACHE, f"{market}_1m_10y.csv"), index=False)
    print(f"[{market}] 10y spliced: {len(full):,} bars {full['dt_utc'].iloc[0]} -> {full['dt_utc'].iloc[-1]}", flush=True)


def main():
    which = sys.argv[1:] or ["nq", "cl"]
    for inst, market in INSTS:
        if market in which:
            fetch_instrument(inst, market)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
