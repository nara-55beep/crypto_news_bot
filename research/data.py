"""
research/data.py — download + cache daily OHLCV for a crypto universe (and perp
funding) from Binance via ccxt. Everything is cached to research/cache/*.csv so the
whole study is reproducible offline after the first run.

Run directly to (re)download and print a coverage report.
"""
from __future__ import annotations
import os, time, sys
import ccxt
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)

# Universe by cap bucket (Binance spot USDT pairs). Some only list partway through
# history — that's handled (each series starts when it lists).
UNIVERSE = {
    "major":  ["BTC", "ETH"],
    "large":  ["BNB", "XRP", "ADA", "SOL", "DOGE", "AVAX", "DOT", "LINK", "LTC",
               "BCH", "TRX", "ATOM"],
    "mid":    ["UNI", "AAVE", "FIL", "ETC", "XLM", "ALGO", "NEAR", "APT", "ARB",
               "OP", "INJ", "SUI", "ICP", "SAND", "MANA"],
    "low":    ["SHIB", "PEPE", "FLOKI", "WIF", "BONK", "GALA", "CHZ", "ENJ"],
}
ALL_COINS = [c for v in UNIVERSE.values() for c in v]
BUCKET = {c: b for b, v in UNIVERSE.items() for c in v}

TIMEFRAME = "1d"
SINCE = "2017-01-01T00:00:00Z"


def _ex():
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def fetch_ohlcv(ex, symbol, timeframe=TIMEFRAME, since_iso=SINCE):
    """Page through Binance OHLCV from `since` to now. Returns a DataFrame indexed by
    UTC date with columns open/high/low/close/volume (volume in base units)."""
    since = ex.parse8601(since_iso)
    out = []
    while True:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except ccxt.BadSymbol:
            return None
        except Exception as e:
            print(f"   retry {symbol}: {type(e).__name__}"); time.sleep(2); continue
        if not batch:
            break
        out += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(ex.rateLimit / 1000.0)
    if not out:
        return None
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts")
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    return df.set_index("date")[["open", "high", "low", "close", "volume"]]


def fetch_funding(ex_perp, symbol):
    """Perp funding-rate history (8h). Returns Series indexed by datetime (rate per 8h)."""
    since = ex_perp.parse8601(SINCE)
    out = []
    while True:
        try:
            batch = ex_perp.fetch_funding_rate_history(symbol, since=since, limit=1000)
        except Exception:
            break
        if not batch:
            break
        out += batch
        since = batch[-1]["timestamp"] + 1
        if len(batch) < 1000:
            break
        time.sleep(ex_perp.rateLimit / 1000.0)
    if not out:
        return None
    s = pd.Series({pd.to_datetime(b["timestamp"], unit="ms"): b["fundingRate"] for b in out})
    return s.sort_index()


def load_prices(refresh=False):
    """Return dict coin -> OHLCV DataFrame, loading from cache (downloading if missing)."""
    ex = _ex()
    prices = {}
    for c in ALL_COINS:
        path = os.path.join(CACHE, f"{c}_1d.csv")
        if os.path.exists(path) and not refresh:
            prices[c] = pd.read_csv(path, index_col=0, parse_dates=True)
            continue
        sym = f"{c}/USDT"
        print(f"downloading {sym} ...")
        df = fetch_ohlcv(ex, sym)
        if df is None or len(df) < 200:
            print(f"   SKIP {c} (insufficient/no data)"); continue
        df.to_csv(path)
        prices[c] = df
    return prices


def load_funding(refresh=False):
    """Return dict coin -> 8h funding Series for coins that have a USDT-perp."""
    exp = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    fund = {}
    for c in ALL_COINS:
        path = os.path.join(CACHE, f"{c}_funding.csv")
        if os.path.exists(path) and not refresh:
            s = pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
            fund[c] = s; continue
        sym = f"{c}/USDT:USDT"
        print(f"funding {sym} ...")
        s = fetch_funding(exp, sym)
        if s is None or len(s) < 100:
            print(f"   SKIP funding {c}"); continue
        s.to_frame("funding").to_csv(path)
        fund[c] = s
    return fund


def coverage_report(prices):
    rows = []
    for c, df in prices.items():
        rows.append({"coin": c, "bucket": BUCKET[c], "n": len(df),
                     "start": df.index[0].date(), "end": df.index[-1].date()})
    rep = pd.DataFrame(rows).sort_values(["bucket", "start"])
    print(rep.to_string(index=False))
    return rep


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    prices = load_prices(refresh=refresh)
    print(f"\nLoaded {len(prices)} price series.\n")
    coverage_report(prices)
    if "--funding" in sys.argv:
        fund = load_funding(refresh=refresh)
        print(f"\nLoaded funding for {len(fund)} coins.")
