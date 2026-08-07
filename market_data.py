"""
================================================================================
 market_data.py  —  MARKET CONTEXT LAYER  (Binance public REST, free, no key)
================================================================================
Keeps a live snapshot of BTC/ETH/SOL by POLLING Binance's public REST API a few
times per second:
  - mark price + funding rate  (from /fapi/v1/premiumIndex)
  - 1-minute % change          (computed from recent prices)
  - open interest              (from /fapi/v1/openInterest, less often)

We use simple REST polling instead of a websocket because it's more reliable
across networks and Python versions, and for PAPER trading 1-2s price freshness
is plenty. The thing that must be fast is news -> decision (the AI), not the
price refresh. If Binance is blocked on your internet, the error prints clearly.
================================================================================
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque

import aiohttp

import config

PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
OI = "https://fapi.binance.com/fapi/v1/openInterest"


class MarketData:
    def __init__(self, symbols=None):
        self.symbols = symbols or config.SYMBOLS
        self._price = {s: None for s in self.symbols}
        self._funding = {s: None for s in self.symbols}
        self._oi = {s: None for s in self.symbols}
        self._hist = {s: deque(maxlen=240) for s in self.symbols}
        self._missing = {}          # symbol -> consecutive misses (for bad-symbol drop)
        self._running = False
        self._logged_live = False
        self._errs = 0

    def price(self, symbol):
        return self._price.get(symbol)

    def snapshot(self):
        return {s: {"price": self._price[s], "funding_rate": self._funding[s],
                    "open_interest": self._oi[s], "change_1m_pct": self._change_1m(s)}
                for s in self.symbols}

    def _change_1m(self, symbol):
        buf = self._hist[symbol]
        if len(buf) < 2 or self._price[symbol] is None:
            return None
        now = time.time()
        ref = None
        for ts, px in buf:
            if now - ts <= 60:
                ref = px
                break
        if not ref:
            return None
        return round((self._price[symbol] - ref) / ref * 100, 3)

    async def run(self):
        self._running = True
        await asyncio.gather(self._price_loop(), self._oi_loop())

    async def _price_loop(self):
        # ONE request to premiumIndex (no symbol param) returns EVERY futures
        # symbol's mark price. So we make 1 request per cycle instead of one per
        # coin -- this keeps us far under Binance's rate limit (hitting it bans
        # the IP for a while, which is what HTTP 429/418 mean).
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.get(
                            PREMIUM, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            body = (await r.text())[:200]
                            self._note_error(f"HTTP {r.status}: {body}")
                            await self._backoff(r.status, body)
                            continue
                        data = await r.json()
                    self._apply_prices(data)
                    self._errs = 0                  # success -> reset error streak
                except Exception as e:
                    self._note_error(f"{type(e).__name__} - {e}")
                await asyncio.sleep(1.0)               # 1s price feed -> faster signals

    def _apply_prices(self, data):
        if not isinstance(data, list):
            return
        now = time.time()
        wanted = set(self.symbols)
        seen = set()
        for d in data:
            sym = d.get("symbol")
            if sym in wanted:
                try:
                    self._price[sym] = float(d["markPrice"])
                except (TypeError, ValueError, KeyError):
                    continue
                seen.add(sym)
                if d.get("lastFundingRate") not in (None, ""):
                    self._funding[sym] = float(d["lastFundingRate"])
                self._hist[sym].append((now, self._price[sym]))
        # a symbol Binance never lists is a bad entry in config.SYMBOLS -> drop it
        for s in list(self.symbols):
            if s in seen:
                self._missing[s] = 0
            elif data:                              # only count misses on real data
                self._missing[s] = self._missing.get(s, 0) + 1
                if self._missing[s] >= 4:
                    self.symbols.remove(s)
                    self._price.pop(s, None)
                    print(f"[market_data] '{s}' is not on Binance futures -> dropped "
                          f"it. (Remove or fix it in config.py SYMBOLS.)")
        if (not self._logged_live and self.symbols and
                all(self._price[s] is not None for s in self.symbols)):
            self._logged_live = True
            print("[market_data] LIVE prices:",
                  {s: self._price[s] for s in self.symbols})

    async def _backoff(self, status, body):
        """On 429/418 stop hammering; if banned, wait the ban out automatically."""
        if status not in (418, 429):
            await asyncio.sleep(2.0)
            return
        m = re.search(r"banned until (\d+)", body or "")
        if m:
            wait = int(m.group(1)) / 1000 - time.time() + 2
            wait = max(5, min(wait, 900))           # clamp 5s .. 15min
            print(f"[market_data] Binance temporarily limited this IP for too many "
                  f"requests (this is NOT a geo-block). Waiting {int(wait)}s for it to "
                  f"clear, then resuming automatically. Don't run two copies at once.")
            await asyncio.sleep(wait)
        else:
            print("[market_data] Binance rate-limited us (429). Backing off 60s.")
            await asyncio.sleep(60)

    async def _oi_loop(self):
        # Open interest has no "all symbols" endpoint, so it's one request each.
        # We run it only once a minute and space the calls out so it never bursts.
        async with aiohttp.ClientSession() as session:
            while self._running:
                for s in list(self.symbols):
                    try:
                        async with session.get(OI, params={"symbol": s},
                                               timeout=aiohttp.ClientTimeout(total=8)) as r:
                            if r.status == 200:
                                self._oi[s] = float((await r.json())["openInterest"])
                            elif r.status in (418, 429):
                                break               # rate-limited -> skip this round
                    except Exception:
                        pass
                    await asyncio.sleep(0.15)        # spread the requests out
                await asyncio.sleep(60)

    def _note_error(self, msg):
        self._errs += 1
        # print the first few, then occasionally, so we don't spam the screen
        if self._errs <= 3 or self._errs % 40 == 0:
            print(f"[market_data] price feed error #{self._errs}: {msg}")
            if self._errs == 1:
                print("[market_data] ^ if this says HTTP 451 or a connection error, "
                      "Binance is blocked on your internet -> tell me and I'll switch "
                      "to another exchange.")

    async def wait_until_ready(self, timeout=20):
        start = time.time()
        while time.time() - start < timeout:
            if all(self._price[s] is not None for s in self.symbols):
                return True
            await asyncio.sleep(0.3)
        return False
