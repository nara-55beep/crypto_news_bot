"""
================================================================================
 orderbook.py  —  MULTI-EXCHANGE ORDER BOOK + TRADE TAPE  (via CCXT)
================================================================================
Now powered by CCXT (https://github.com/ccxt/ccxt) — one unified API across 100+
crypto exchanges, instead of a hand-written fetcher per venue. The set of
exchanges is config.EXCHANGES; add/remove ids there.

What this gives the rest of the app (unchanged shapes, so nothing else breaks):
  • WhaleTracker  — a rolling tape of EVERY trade (>= config.WHALE_MIN_USD) across
                    all the exchanges, with buy/sell flow.
  • fetch_all_orderbooks() — the consolidated order book (one row per exchange
                    per price bin) used by the order-book page and the whale bot.

Honest limits (CCXT can't change these): a public order book is still anonymous
and grouped by price (no per-order identity, no long/short); trades are polled
over REST (~1.5s), so it's near-real-time, not tick-perfect. If an exchange errors
(bad symbol, geo-block, rate limit) it simply drops out — the others keep going.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque, defaultdict

import config

# CCXT is required for the order book + trade tape. If it's missing, the rest of
# the app (prices, charts, bots, journal) still runs; this part just stays empty.
try:
    import ccxt.async_support as ccxt          # async build: fits our asyncio loop
    CCXT_OK = True
except Exception:
    ccxt = None
    CCXT_OK = False

# Price-bin size (USD) for the consolidated book. Bigger = fewer, chunkier rows.
PRICE_BIN = 25.0
# Minimum single-trade size (USD) to KEEP in the tape (and thus counted in flow %).
# 0 = keep every trade. From config.
MIN_TRADE_USD = config.WHALE_MIN_USD
# Trades below this are kept/counted in flow but NOT streamed to the browser or shown
# in the trades panel (keeps the whale UI readable when MIN_TRADE_USD is 0).
DISPLAY_MIN_USD = float(getattr(config, "WHALE_DISPLAY_USD", 10000))

EXCHANGES = list(config.EXCHANGES)
# Preference order when picking which BTC market to track on each exchange.
SYMBOL_CANDIDATES = ["BTC/USDT", "BTC/USD", "BTC/USDC",
                     "BTC/USDT:USDT", "BTC/USD:USD", "BTC/USDC:USDC"]

# How many order-book levels to request per exchange. Default 200. KuCoin's
# public book only serves fixed depths (20/100); anything else hits its FULL
# book, which needs an API key — so cap KuCoin at its deepest public level, 100.
DEFAULT_DEPTH = 200
DEPTH_LIMIT = {"kucoin": 100}

_clients = {}      # exchange id -> ccxt async client (created once, reused)
_symbols = {}      # exchange id -> resolved BTC symbol (or None if unavailable)
_warned = [False]
_noted = {}        # (slot, ex_id) -> last status string, so we print each change once
_book_seen = {}    # ex_id -> last time its order book answered (for sticky status)
_ob_cache = {"ts": 0.0, "data": None}   # short cache so rapid polls don't hammer exchanges
OB_CACHE_TTL = 2.5      # seconds; the book barely changes faster than this
OB_STICKY_SEC = 20      # show an exchange "ok" if it answered within this many seconds


def _note(slot, ex_id, msg):
    """Print a per-exchange status line ONCE (until it changes), so the console
    shows WHY an exchange has no data instead of failing silently."""
    k = (slot, ex_id)
    if _noted.get(k) != msg:
        _noted[k] = msg
        print(f"[orderbook] {ex_id} {slot}: {msg}")


def trade_feed_status():
    """The latest trade-tape status message per exchange (e.g. 'live (BTC/USDT)' or
    'ConnectorError: ...'), so the UI can show WHY an exchange has no trades."""
    return {ex_id: msg for (slot, ex_id), msg in _noted.items() if slot == "trades"}


# Per-exchange ccxt overrides (host/options). Empty by default; OKX is handled specially in
# _poll_exchange (its default host www.okx.com is reachable, but ccxt's load_markets crashes on
# OKX's instrument list — so we bypass it and hit OKX's raw public trades endpoint instead).
_EXCHANGE_OPTS = {}


def _client(ex_id):
    if not CCXT_OK:
        return None
    if ex_id not in _clients:
        try:
            opts = {"enableRateLimit": True, "timeout": 12000}
            opts.update(_EXCHANGE_OPTS.get(ex_id, {}))
            _clients[ex_id] = getattr(ccxt, ex_id)(opts)
        except Exception:
            _clients[ex_id] = None
    return _clients[ex_id]


async def _symbol(ex_id):
    """Load an exchange's markets once and pick its best BTC market."""
    if ex_id in _symbols:
        return _symbols[ex_id]
    cl = _client(ex_id)
    if cl is None:
        _symbols[ex_id] = None
        return None
    try:
        await cl.load_markets()
        sym = next((s for s in SYMBOL_CANDIDATES if s in cl.markets), None)
        if sym is None:                          # fallback: any active BTC/USD-ish market
            for m in cl.markets.values():
                if (m.get("base") == "BTC" and m.get("quote") in ("USDT", "USD", "USDC")
                        and m.get("active", True)):
                    sym = m["symbol"]
                    break
        _symbols[ex_id] = sym
    except Exception:
        _symbols[ex_id] = None
    return _symbols[ex_id]


def _no_ccxt_notice():
    if not _warned[0]:
        _warned[0] = True
        print("=" * 64)
        print("[orderbook] CCXT is NOT importable -> order book + trade tape are OFF.")
        print("            (Prices, charts, bots and the journal still work.)")
        print("            It must be installed INTO THE VENV the bot runs in.")
        print("            In this folder, run these two lines:")
        print("                venv\\Scripts\\activate.bat")
        print("                pip install ccxt")
        print("            then close this window and start run.bat again.")
        print("=" * 64)


# ===========================================================================
# CONSOLIDATED ORDER BOOK
# ===========================================================================
async def fetch_all_orderbooks(depth_levels=44):
    """One row per exchange per price bin (not merged across exchanges). Rows
    nearest the mid price first. Anonymous + grouped — all a public book holds."""
    sources, errors = {}, {}
    raw_bids, raw_asks = [], []                  # (price, btc, exchange)
    if not CCXT_OK:
        _no_ccxt_notice()
        return {"mid": 0, "bids": [], "asks": [], "sources": {}, "errors": {}, "bin": PRICE_BIN}

    # serve a very recent snapshot if we have one (cuts request load on the
    # exchanges, which is what makes the stricter ones like Kraken miss polls)
    if _ob_cache["data"] is not None and (time.time() - _ob_cache["ts"]) < OB_CACHE_TTL:
        return _ob_cache["data"]

    async def one(ex_id):
        cl = _client(ex_id)
        if cl is None:
            sources[ex_id] = False
            return
        try:
            sym = await _symbol(ex_id)
            if not sym:
                sources[ex_id] = False
                errors[ex_id] = "no BTC market"
                _note("book", ex_id, "no BTC/USDT-like market found")
                return
            ob = await cl.fetch_order_book(sym, limit=DEPTH_LIMIT.get(ex_id, DEFAULT_DEPTH))
            sources[ex_id] = True
            _book_seen[ex_id] = time.time()
            _note("book", ex_id, f"OK ({sym})")
            for p, q in ob.get("bids", []):
                raw_bids.append((float(p), float(q), ex_id))
            for p, q in ob.get("asks", []):
                raw_asks.append((float(p), float(q), ex_id))
        except Exception as e:
            sources[ex_id] = False
            errors[ex_id] = type(e).__name__
            _note("book", ex_id, f"{type(e).__name__}: {str(e)[:110]}")

    await asyncio.gather(*[one(e) for e in EXCHANGES])

    best_bid = max((p for p, _, _ in raw_bids), default=None)
    best_ask = min((p for p, _, _ in raw_asks), default=None)
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else (best_bid or best_ask or 0)

    def build(raw, reverse):
        agg = defaultdict(float)                 # (price_bin, exchange) -> btc
        for p, q, ex in raw:
            b = round(p / PRICE_BIN) * PRICE_BIN
            agg[(b, ex)] += q
        rows = [{"price": b, "ex": ex, "btc": round(v, 3), "usd": round(b * v)}
                for (b, ex), v in agg.items()]
        rows.sort(key=lambda r: r["price"], reverse=reverse)
        return rows[:depth_levels]

    # sticky status: an exchange that answered within OB_STICKY_SEC counts as
    # "ok" even if it missed THIS snapshot, so a single slow poll (common on
    # rate-limited venues) doesn't flip its dot red. A genuinely-down exchange
    # still goes red once it stops answering for that whole window.
    now = time.time()
    for e in EXCHANGES:
        sources[e] = bool(sources.get(e)) or (now - _book_seen.get(e, 0) < OB_STICKY_SEC)

    result = {"mid": mid, "bids": build(raw_bids, True), "asks": build(raw_asks, False),
              "sources": sources, "errors": errors, "bin": PRICE_BIN}
    _ob_cache["ts"], _ob_cache["data"] = now, result
    return result


# ===========================================================================
# TRADE TAPE  (every trade >= MIN_TRADE_USD across all exchanges)
# ===========================================================================
class WhaleTracker:
    """Live trade tape across config.EXCHANGES (via CCXT). Each trade is a real
    executed trade with a taker side (buy = hit the offers, sell = hit the bids)."""

    def __init__(self, min_usd=MIN_TRADE_USD, display_min_usd=DISPLAY_MIN_USD,
                 keep=200000, poll_interval=0.8):
        self.min_usd = min_usd
        self.display_min_usd = display_min_usd     # below this: counted in flow, not shown/streamed
        # Bigger tape: with min_usd=0 the tape fills with ALL trades, so we keep more of
        # them to still cover the longer windows (baseline / volume) in time terms.
        self.tape = deque(maxlen=keep)
        self._seen = defaultdict(set)
        self.running = False
        self.sources = {}
        # Per-exchange REST poll cadence (seconds). Each exchange runs its OWN loop,
        # so this is how often a single venue re-fetches its tape — not a shared round.
        # CCXT's enableRateLimit still throttles each client to its safe rate, so a
        # tight interval just means "poll as fast as this exchange allows".
        self.poll_interval = poll_interval
        self._seq = 0                            # monotonic id per trade (tape cursor)
        self.on_add = None                       # optional callback(trade_dict) fired the
                                                 # instant a trade is added (used to push
                                                 # it straight to the browser over SSE)

    def _is_new(self, ex, tid):
        s = self._seen[ex]
        if tid in s:
            return False
        s.add(tid)
        if len(s) > 40000:                       # old ids won't reappear in 'recent' feeds
            s.clear()
        return True

    def _add(self, ex, tid, price, qty, side, ts):
        if not self._is_new(ex, tid):
            return
        usd = price * qty
        if usd >= self.min_usd:                  # min_usd=0 -> keep EVERY trade (counted in flow)
            self._seq += 1
            t = {"ex": ex, "price": price, "btc": round(qty, 4),
                 "usd": round(usd), "side": side, "ts": ts, "seq": self._seq}
            self.tape.append(t)
            # Only STREAM whale-sized trades to the browser, so the live trades panel
            # stays readable and the SSE isn't flooded with tiny prints.
            if self.on_add is not None and usd >= self.display_min_usd:
                try:
                    self.on_add(t)
                except Exception:
                    pass

    async def run(self):
        self.running = True
        if not CCXT_OK:
            _no_ccxt_notice()
            return
        # Hyperliquid has NO public REST trade tape (its fetch_trades returns a
        # single wallet's fills). Its public trades come over a websocket, so we
        # feed those separately and poll the REST tape for everyone else.
        # Hyperliquid and Binance go over public websockets (instant). Hyperliquid's
        # REST tape is per-wallet, and Binance is the deepest BTC book where poll lag
        # is most noticeable — so both stream live. Everyone else is REST-polled.
        ws_exch = [e for e in ("hyperliquid", "binance") if e in EXCHANGES]
        rest_tape = [e for e in EXCHANGES if e not in ("hyperliquid", "binance")]
        if "hyperliquid" in EXCHANGES:
            asyncio.create_task(self._hyperliquid_ws())
        if "binance" in EXCHANGES:
            asyncio.create_task(self._binance_ws())
        print(f"[orderbook] CCXT ready — REST trade tape on {len(rest_tape)} exchanges: "
              f"{', '.join(rest_tape)}  (each polled independently every "
              f"~{self.poll_interval:g}s)" +
              (f"  + {', '.join(ws_exch)} via websocket" if ws_exch else ""))
        # ONE independent loop per exchange. Previously every venue shared a single
        # gather() round + 1.5s sleep, so the whole tape only refreshed as fast as the
        # SLOWEST exchange (a sluggish Kraken/Coinbase stalled Gate/OKX/Bybit too).
        # Now each venue re-fetches on its own cadence and a slow one can't hold the
        # others back, so fast exchanges deliver trades to the flow far sooner.
        await asyncio.gather(*[self._poll_exchange(e) for e in rest_tape])

    async def _poll_exchange(self, ex_id):
        """Continuously poll ONE exchange's trade tape on its own cadence. Independent
        of every other exchange, so its latency only affects itself."""
        first = True                             # prime on first pass; don't dump backlog
        logged = False
        while self.running:
            cl = _client(ex_id)
            if cl is None:
                self.sources[ex_id] = False
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                if ex_id == "okx":
                    # ccxt's load_markets() CRASHES on OKX's instrument list (one instrument has a
                    # None symbol and ccxt's sort raises TypeError), so fetch_trades never runs.
                    # Skip markets entirely and hit OKX's RAW public trades endpoint — implicit API
                    # methods don't need markets loaded. This is why OKX was red.
                    sym = "BTC/USDT"
                    raw = await cl.publicGetMarketTrades({"instId": "BTC-USDT", "limit": "100"})
                    trades = [{"id": d.get("tradeId"), "price": d.get("px"), "amount": d.get("sz"),
                               "side": d.get("side"), "timestamp": int(d.get("ts") or 0)}
                              for d in (raw.get("data") or [])]
                else:
                    sym = await _symbol(ex_id)
                    if not sym:
                        self.sources[ex_id] = False
                        _note("trades", ex_id, "no BTC/USDT-like market found")
                        await asyncio.sleep(self.poll_interval)
                        continue
                    trades = await cl.fetch_trades(sym, limit=200)
                self.sources[ex_id] = True
                _note("trades", ex_id, f"live ({sym})")
                for t in trades:
                    tid = str(t.get("id") or t.get("timestamp") or t.get("datetime"))
                    price = float(t.get("price") or 0)
                    qty = float(t.get("amount") or 0)
                    if price <= 0 or qty <= 0:
                        continue
                    side = (t.get("side") or "buy").lower()
                    ts = int(t.get("timestamp") or time.time() * 1000)
                    if first:
                        self._is_new(ex_id, tid)
                    else:
                        self._add(ex_id, tid, price, qty, side, ts)
                if first and not logged:
                    logged = True
                    print(f"[orderbook] CCXT trade tape live on: {ex_id} ({sym})")
                first = False
            except Exception as e:
                self.sources[ex_id] = False
                _note("trades", ex_id, f"{type(e).__name__}: {str(e)[:110]}")
            await asyncio.sleep(self.poll_interval)

    async def _hyperliquid_ws(self):
        """Hyperliquid's public trades are only on its websocket (its REST tape
        is per-wallet). Subscribe to the BTC trades channel and feed the tape.
        Reconnects on drop; if the websockets lib is missing, it just stays off."""
        try:
            import websockets
        except Exception:
            _note("trades", "hyperliquid", "websockets library not installed")
            return
        url = "wss://api.hyperliquid.xyz/ws"
        sub = json.dumps({"method": "subscribe",
                          "subscription": {"type": "trades", "coin": "BTC"}})
        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(sub)
                    self.sources["hyperliquid"] = True
                    _note("trades", "hyperliquid", "live (websocket BTC)")
                    async for msg in ws:
                        try:
                            d = json.loads(msg)
                        except Exception:
                            continue
                        if d.get("channel") != "trades":
                            continue
                        for t in d.get("data", []) or []:
                            try:
                                px = float(t.get("px") or 0)
                                sz = float(t.get("sz") or 0)
                                if px <= 0 or sz <= 0:
                                    continue
                                # Hyperliquid side: "B" = buy aggressor, "A" = sell
                                side = "buy" if str(t.get("side", "")).upper().startswith("B") else "sell"
                                ts = int(t.get("time") or time.time() * 1000)
                                tid = str(t.get("tid") or t.get("hash") or f"{ts}-{px}-{sz}")
                                self._add("hyperliquid", tid, px, sz, side, ts)
                            except Exception:
                                continue
            except Exception as e:
                self.sources["hyperliquid"] = False
                _note("trades", "hyperliquid", f"ws {type(e).__name__}: {str(e)[:90]}")
                await asyncio.sleep(3)           # back off, then reconnect

    async def _binance_ws(self):
        """Binance has the deepest BTC liquidity, so polling lag is most visible there.
        Stream its trades over the public futures websocket (aggTrade) instead — each
        trade arrives the instant it executes. Feeds the same tape; reconnects on drop;
        if the websockets lib is missing it just stays off (REST others still work)."""
        try:
            import websockets
        except Exception:
            _note("trades", "binance", "websockets library not installed")
            return
        url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"   # USDT-M futures, BTCUSDT
        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              ping_timeout=20, close_timeout=5) as ws:
                    self.sources["binance"] = True
                    _note("trades", "binance", "live (websocket aggTrade)")
                    async for msg in ws:
                        try:
                            d = json.loads(msg)
                        except Exception:
                            continue
                        if d.get("e") != "aggTrade":
                            continue
                        try:
                            px = float(d.get("p") or 0)
                            sz = float(d.get("q") or 0)
                            if px <= 0 or sz <= 0:
                                continue
                            # m = True -> buyer is the maker -> the taker (aggressor) SOLD
                            side = "sell" if d.get("m") else "buy"
                            ts = int(d.get("T") or time.time() * 1000)
                            tid = "agg" + str(d.get("a") or f"{ts}-{px}-{sz}")
                            self._add("binance", tid, px, sz, side, ts)
                        except Exception:
                            continue
            except Exception as e:
                self.sources["binance"] = False
                _note("trades", "binance", f"ws {type(e).__name__}: {str(e)[:90]}")
                await asyncio.sleep(3)           # back off, then reconnect

    def get(self, limit=250, window_sec=None):
        rows = self.tape
        if window_sec:
            cutoff = (time.time() - window_sec) * 1000
            rows = [t for t in rows if t["ts"] >= cutoff]
        return sorted(rows, key=lambda x: x["ts"], reverse=True)[:limit]

    def flow(self, window_sec=60):
        """USD-weighted taker buy vs sell over the last window (for the %).
        Single backward pass: newest-first, stop once we're safely past the
        window (15s margin, since trades can arrive a little out of order)."""
        cutoff = (time.time() - window_sec) * 1000
        stop = cutoff - 15000
        buy = sell = 0.0
        for t in reversed(self.tape):
            ts = t["ts"]
            if ts < stop:
                break
            if ts >= cutoff:
                if t["side"] == "buy":
                    buy += t["usd"]
                else:
                    sell += t["usd"]
        return buy, sell

    def since(self, since_seq=0, window_sec=60, min_usd=None):
        """Trades newer than since_seq still inside the window (newest-first), plus
        the current max seq (next cursor), the window cutoff, and the count in the
        window. Single backward pass (newest-first) that stops once we're past the
        window (+15s out-of-order margin), so a fast poll never rescans the whole
        tape. Only trades >= min_usd (default: the display threshold) are returned
        and counted, so the trades panel stays whale-only even though the tape now
        holds every trade for the flow %."""
        floor = self.display_min_usd if min_usd is None else float(min_usd)
        cutoff = (time.time() - window_sec) * 1000
        stop = cutoff - 15000
        new = []
        in_window = 0
        for t in reversed(self.tape):
            ts = t["ts"]
            if ts < stop:
                break
            if ts >= cutoff and t["usd"] >= floor:
                in_window += 1
                if t["seq"] > since_seq:
                    new.append(t)        # reversed(tape) is already newest-first
        return {"new": new, "max_seq": self._seq, "cutoff_ts": cutoff, "count": in_window}
