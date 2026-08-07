"""Fetch the maximum practical 1m BTC perp history (3 years) for the ICT backtest.
Resampling to 5m/1h is done in the backtest harness so all TFs stay aligned."""
import os, time
import ccxt, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cache", "BTC_1m_max.csv")
SYMBOL = "BTC/USDT:USDT"
START_ISO = "2023-06-01T00:00:00Z"     # ~3 years of 1m


def main():
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since = ex.parse8601(START_ISO)
    now = ex.milliseconds()
    out, n, last = [], 0, -1
    while since < now:
        try:
            b = ex.fetch_ohlcv(SYMBOL, "1m", since=since, limit=1000)
        except Exception as e:
            print("retry", type(e).__name__, e, flush=True); time.sleep(1.5); continue
        if not b or b[-1][0] == last:
            break
        out += b; last = b[-1][0]; since = b[-1][0] + 60_000; n += 1
        if n % 50 == 0:
            print(f"{n} reqs, {len(out):,} bars, at {pd.to_datetime(b[-1][0], unit='ms')}", flush=True)
        time.sleep(ex.rateLimit / 1000.0)
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts").sort_values("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    df.set_index("dt")[["open", "high", "low", "close", "volume"]].to_csv(OUT)
    print(f"DONE {len(df):,} bars {df['dt'].iloc[0]} -> {df['dt'].iloc[-1]} -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
