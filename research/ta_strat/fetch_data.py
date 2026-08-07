"""
fetch_data.py — download ~420 days of BTC perp OHLCV at 1m/5m/15m from Binance USDM
(binanceusdm via ccxt), cache to research/ta_strat/cache/. 420d = 365d test window
plus warmup room for 200-period indicators on every timeframe.

Run: venv/Scripts/python.exe research/ta_strat/fetch_data.py
Idempotent: re-running extends/uses the cache.
"""
from __future__ import annotations
import os, time, sys
import ccxt
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)

SYMBOL = "BTC/USDT:USDT"      # linear perp on binanceusdm
DAYS = 420
TFS = ["15m", "5m", "1m"]    # fetch coarse->fine (fast feedback first)
MS_PER = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def fetch_tf(ex, tf):
    path = os.path.join(CACHE, f"BTC_{tf}.csv")
    now = ex.milliseconds()
    start = now - DAYS * 86_400_000
    since = start
    out = []
    n_req = 0
    last_ts = -1
    while since < now:
        try:
            batch = ex.fetch_ohlcv(SYMBOL, tf, since=since, limit=1000)
        except Exception as e:
            print(f"   retry {tf}: {type(e).__name__}: {e}", flush=True)
            time.sleep(1.5); continue
        if not batch:
            break
        if batch[-1][0] == last_ts:      # no forward progress -> reached the end
            break
        out += batch
        last_ts = batch[-1][0]
        since = batch[-1][0] + MS_PER[tf]
        n_req += 1
        if n_req % 25 == 0:
            got = pd.to_datetime(batch[-1][0], unit="ms")
            print(f"   {tf}: {n_req} reqs, {len(out):,} bars, at {got}", flush=True)
        time.sleep(ex.rateLimit / 1000.0)
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    df.to_csv(path)
    print(f"[done] {tf}: {len(df):,} bars  {df.index[0]} -> {df.index[-1]}  ({n_req} reqs)", flush=True)
    return df


def main():
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    for tf in TFS:
        path = os.path.join(CACHE, f"BTC_{tf}.csv")
        if os.path.exists(path):
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            span = (df.index[-1] - df.index[0]).days
            if span >= DAYS - 5:
                print(f"[cached] {tf}: {len(df):,} bars, {span}d — skip", flush=True)
                continue
        print(f"[fetch] {tf} ...", flush=True)
        fetch_tf(ex, tf)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
