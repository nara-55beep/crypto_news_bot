"""
================================================================================
 lighter_markets.py  —  ALL Lighter perp markets (price, funding, volume, OI)
================================================================================
Pulls EVERY market listed on Lighter via the public (unsigned) ccxt.lighter feed
— the same source funding_bot uses — and merges funding rates with tickers so the
website can show a Lighter-style markets table: search by name, sort by funding,
price, 24h change, volume, open interest, etc.

Cached for a few seconds so the table can poll cheaply. Blocking ccxt calls, so
the dashboard runs this in a thread.
================================================================================
"""
from __future__ import annotations
import time

_ex = None
_cache = {"ts": 0.0, "rows": []}


def _client():
    global _ex
    if _ex is None:
        import ccxt
        _ex = ccxt.lighter({"enableRateLimit": True, "timeout": 20000})
        _ex.load_markets()
    return _ex


def _name(sym: str) -> str:
    """'BTC/USDC:USDC' -> 'BTC' (clean display ticker)."""
    for suf in ("/USDC:USDC", "/USDC", "/USDT:USDT", "/USDT", ":USDC", ":USDT"):
        sym = sym.replace(suf, "")
    return sym


def _f(*vals):
    for v in vals:
        if v in (None, "", "null"):
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def fetch_markets() -> list[dict]:
    """One bulk pull of every Lighter market: price, 24h change, 24h volume, open
    interest, funding rate, max leverage. Merged by symbol."""
    ex = _client()
    funding = {}
    try:
        fr = ex.fetch_funding_rates()
        for sym, d in fr.items():
            r = d.get("fundingRate")
            if r is not None:
                funding[sym] = float(r)
    except Exception:
        pass
    rows = []
    tickers = ex.fetch_tickers()
    for sym, d in tickers.items():
        info = d.get("info") or {}
        price = _f(d.get("last"), d.get("close"), info.get("mark_price"),
                   info.get("last_trade_price"))
        if not price:
            continue
        change = _f(d.get("percentage"), info.get("daily_price_change"))
        volume = _f(d.get("quoteVolume"), info.get("daily_quote_token_volume"))
        oi_base = _f(info.get("open_interest"))
        oi = oi_base * price if (oi_base and price) else None   # base tokens -> USD (Lighter shows USD)
        # max leverage = 10000 / min-initial-margin-fraction (Lighter quotes the fraction in
        # parts-per-10000, e.g. 200 -> 2% margin -> 50x). Lowest margin = highest leverage.
        maxlev = None
        imf = _f(info.get("min_initial_margin_fraction"),
                 info.get("default_initial_margin_fraction"))
        if imf and imf > 0:
            maxlev = round(10000.0 / imf)
        rows.append({
            "symbol": _name(sym), "market": sym, "price": price,
            "change": change, "volume": volume, "oi": oi,
            "funding": funding.get(sym), "max_leverage": maxlev,
        })
    rows.sort(key=lambda r: (r["volume"] or 0), reverse=True)
    return rows


def _full_symbol(market: str) -> str:
    """'BTC' or 'BTC/USDC:USDC' -> the ccxt market symbol Lighter uses."""
    if "/" in market:
        return market
    return market.upper() + "/USDC:USDC"


# chart intervals Lighter doesn't serve -> nearest supported one (so the chart never breaks)
_TF_MAP = {"2h": "1h", "1w": "1d", "1M": "1d"}
_CANDLE_CACHE = {}   # (market, tf) -> (ts, rows) — bounds how often the 2s chart poll hits Lighter


def candles(market: str, tf: str = "1m", limit: int = 300) -> list[dict]:
    """Lighter's OWN candles for any market (for the chart). [{t,o,h,l,c,v}], t = epoch secs.
    Cached ~3s per market+timeframe so the live chart poll never rate-limits Lighter."""
    key = (market, tf)
    now = time.time()
    hit = _CANDLE_CACHE.get(key)
    if hit and (now - hit[0]) < 3.0:
        return hit[1]
    ex = _client()
    sym = _full_symbol(market)
    o = ex.fetch_ohlcv(sym, _TF_MAP.get(tf, tf), limit=int(limit))
    rows = [{"t": int(c[0] / 1000), "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]}
            for c in o if c and c[0]]
    _CANDLE_CACHE[key] = (now, rows)
    return rows


def markets(ttl: float = 20.0, force: bool = False) -> list[dict]:
    """Cached market list. Returns the last good snapshot on a transient error."""
    now = time.time()
    if not force and _cache["rows"] and (now - _cache["ts"]) < ttl:
        return _cache["rows"]
    try:
        rows = fetch_markets()
        if rows:
            _cache["rows"] = rows
            _cache["ts"] = now
    except Exception:
        pass
    return _cache["rows"]
