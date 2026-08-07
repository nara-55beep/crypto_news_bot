"""
data_feed.py — CCXT OHLCV access (paginated), returning the package candle format.

A candle is {"t": epoch_sec, "o","h","l","c","v"}. fetch_ohlcv pages backward so a backtest can
pull months of 5m data. Live code uses `recent` (one page) and drops the still-forming last bar so
the strategy only ever sees CLOSED candles.
"""
from __future__ import annotations
import time
import ccxt


_TF_MS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
          "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
          "1d": 86400, "1w": 604800}


def make_exchange(cfg, signed=False):
    klass = getattr(ccxt, cfg.exchange)
    params = {"enableRateLimit": True, "timeout": 20000,
              "options": {"defaultType": "future"}}
    if signed and cfg.api_key:
        params["apiKey"] = cfg.api_key
        params["secret"] = cfg.api_secret
    ex = klass(params)
    if getattr(cfg, "testnet", False):
        try:
            ex.set_sandbox_mode(True)
        except Exception:
            pass
    return ex


def _norm(rows):
    return [{"t": int(r[0] // 1000), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows if r and r[0]]


def recent(ex, symbol, timeframe, limit=300, drop_unclosed=True):
    """One page of the most recent candles. The last bar is still forming live, so drop it."""
    rows = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    out = _norm(rows)
    if drop_unclosed and out:
        out = out[:-1]
    return out


def history(ex, symbol, timeframe, days):
    """Paginated history covering roughly `days` back to now (closed candles)."""
    step = _TF_MS.get(timeframe, 300)
    now_ms = ex.milliseconds()
    since = now_ms - int(days * 86400 * 1000)
    all_rows, cursor = [], since
    page = 1000
    guard = 0
    while cursor < now_ms and guard < 5000:
        guard += 1
        try:
            rows = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page)
        except Exception:
            time.sleep(1.0)
            continue
        if not rows:
            break
        all_rows.extend(rows)
        last = rows[-1][0]
        if last <= cursor:                 # no forward progress -> done
            break
        cursor = last + step * 1000
        time.sleep((ex.rateLimit or 200) / 1000.0)
    out = _norm(all_rows)
    # de-dupe by timestamp, keep closed bars
    seen, dedup = set(), []
    for c in out:
        if c["t"] in seen:
            continue
        seen.add(c["t"]); dedup.append(c)
    if dedup:
        dedup = dedup[:-1]          # drop the final (possibly still-forming) bar
    return dedup
