"""
================================================================================
 stock_data.py  —  LIVE STOCK PRICES via ALPACA  (free real-time IEX feed)
================================================================================
The stock equivalent of market_data.py, but pointed at Alpaca instead of Binance.

Unlike crypto, stock data needs a (free) key — set ALPACA_API_KEY /
ALPACA_SECRET_KEY in config.py. We only READ market data; no real orders are sent.

It fetches the latest trade price for EVERY watched symbol in ONE request (so we
stay under rate limits), and serves candles for the chart. If keys are missing it
prints a clear message and stays quiet instead of crashing. Outside US market
hours (≈9:30am–4pm ET, weekdays) prices simply stop updating — that's normal.
================================================================================
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import aiohttp

import config

# chart interval (our UI) -> Alpaca timeframe string
TF = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour",
      "2h": "2Hour", "4h": "4Hour", "1d": "1Day", "1w": "1Week", "1M": "1Month"}


class StockData:
    def __init__(self, symbols=None):
        self.symbols = list(symbols or config.STOCK_SYMBOLS)
        self._price = {s: None for s in self.symbols}
        self._hist = {s: deque(maxlen=240) for s in self.symbols}
        self._running = False
        self._logged_live = False
        self._errs = 0
        self.last_error = ""
        self.candle_error = ""
        self.ok = self._keys_ok()

    def _keys_ok(self):
        k, s = config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        return bool(k) and bool(s) and "PASTE" not in k and "PASTE" not in s

    def _headers(self):
        return {"APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY}

    # ---- live prices ----
    async def run(self):
        if not self.ok:
            print("[stock_data] No Alpaca keys set -> stock prices are OFF. Add "
                  "ALPACA_API_KEY / ALPACA_SECRET_KEY in config.py (free at "
                  "alpaca.markets) and restart. The crypto side is unaffected.")
            return
        self._running = True
        url = config.ALPACA_DATA_URL.rstrip("/") + "/v2/stocks/trades/latest"
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            while self._running:
                try:
                    params = {"symbols": ",".join(self.symbols), "feed": config.ALPACA_FEED}
                    async with session.get(url, params=params,
                                           timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            body = (await r.text())[:200]
                            self.last_error = f"HTTP {r.status}: {body}"
                            if r.status in (401, 403):
                                print(f"[stock_data] Alpaca rejected the key ({r.status}). "
                                      f"Check ALPACA_API_KEY / ALPACA_SECRET_KEY. Pausing 60s.")
                                await asyncio.sleep(60)
                            else:
                                await asyncio.sleep(5)
                            continue
                        data = await r.json()
                    self._apply(data.get("trades", {}))
                    self._errs = 0
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    self._errs += 1
                await asyncio.sleep(3.0)

    def _apply(self, trades):
        now = time.time()
        for sym, t in (trades or {}).items():
            if sym in self._price and t and t.get("p") is not None:
                self._price[sym] = float(t["p"])
                self._hist[sym].append((now, self._price[sym]))
        if not self._logged_live and any(v is not None for v in self._price.values()):
            self._logged_live = True
            live = {s: self._price[s] for s in self.symbols if self._price[s] is not None}
            print(f"[stock_data] LIVE stock prices ({len(live)} symbols), e.g. "
                  + ", ".join(f"{k}={v}" for k, v in list(live.items())[:5]))

    def price(self, sym):
        return self._price.get(sym)

    def change_pct(self, sym, window_sec=60):
        h = self._hist.get(sym)
        if not h or len(h) < 2:
            return None
        now_p = h[-1][1]
        cutoff = time.time() - window_sec
        old = None
        for t, p in h:
            if t <= cutoff:
                old = p
        if old is None:
            old = h[0][1]
        return round((now_p - old) / old * 100, 3) if old else None

    def snapshot(self):
        """For the AI: price + recent change for each symbol that has a price."""
        out = {}
        for s in self.symbols:
            p = self._price[s]
            if p is not None:
                out[s] = {"price": p, "change_1m_pct": self.change_pct(s, 60)}
        return out

    async def candles(self, symbol, interval="1m", limit=300):
        """Return [{time, open, high, low, close}] (oldest→newest) for the chart.
        Fetches the most recent bars (sort=desc) then reverses, so it works even
        when the market is closed (you still see the last session). Records the
        real error in self.candle_error so the page can show why it's empty."""
        self.candle_error = ""
        if not self.ok:
            self.candle_error = "no Alpaca key"
            return []
        tf = TF.get(interval, "1Min")
        url = config.ALPACA_DATA_URL.rstrip("/") + "/v2/stocks/bars"
        params = {"symbols": symbol, "timeframe": tf, "limit": str(limit),
                  "feed": config.ALPACA_FEED, "adjustment": "raw", "sort": "desc"}
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=12)) as r:
                    body = await r.text()
                    if r.status != 200:
                        self.candle_error = f"Alpaca bars HTTP {r.status}: {body[:180]}"
                        return []
                    import json as _json
                    data = _json.loads(body)
            bars = (data.get("bars") or {}).get(symbol, []) or []
            if not bars:
                self.candle_error = "Alpaca returned 0 bars (try a longer interval; " \
                                    "free IEX data can be thin)"
                return []
            out = []
            from datetime import datetime
            for b in bars:
                ts = b.get("t")
                try:
                    epoch = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
                out.append({"time": epoch, "open": b["o"], "high": b["h"],
                            "low": b["l"], "close": b["c"]})
            out.sort(key=lambda c: c["time"])          # oldest → newest for the chart
            return out
        except Exception as e:
            self.candle_error = f"{type(e).__name__}: {e}"
            return []
