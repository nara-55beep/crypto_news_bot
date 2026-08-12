"""Fill-driven Cryptal maker / Binance hedge strategy (paper only).

The strategy mirrors the inventory cycle visible in Cryptal's thin BTC-USD book:

1. Rest a passive BTC bid on Cryptal below a cross-venue fair value.
2. If a new public Cryptal sell trade reaches that bid, simulate the maker fill and
   immediately short the same BTC quantity on Binance USD-M futures.
3. Rest a passive Cryptal ask for the acquired BTC. If a buyer reaches it, simulate
   the spot sale and buy back the Binance short.

This module deliberately contains no private endpoint and cannot place a real order.
Public trades cannot reveal exact queue position, so the paper fill model consumes the
visible size ahead of the quote before granting a fill and labels its results as
non-evidentiary. It is a collector for deciding whether live pilot work is justified,
not proof of profit.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
from decimal import Decimal
from typing import Any

import aiohttp

import config


NAME = "Cryptal Maker + Binance Hedge"
STATE_PATH = os.path.join(config.DATA_DIR, "cryptal_maker_state.json")
GEL_STATE_PATH = os.path.join(config.DATA_DIR, "cryptal_maker_btc_gel_state.json")

CRYPTAL_BASE = "https://exchange.cryptal.com/exchange"
CRYPTAL_PAIR = "BTC-USD"       # displayed as BTC-TOUSD in Cryptal's web UI
STABLE_PAIR = "USDT-USD"       # TOUSD value of one USDT
CRYPTAL_GEL_PAIR = "BTC-GEL"   # displayed as BTC-TOGEL in Cryptal's web UI
GEL_STABLE_PAIR = "USDT-GEL"   # TOGEL value of one USDT
SETTLEMENT_PAIR = "USDT-USD"   # marks both ledgers in a common USD equivalent
BINANCE_BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

POLL_SEC = 2.0
REQUEST_TIMEOUT_SEC = 8
MAX_DATA_AGE_SEC = 12
MAX_HEDGED_HOLD_SEC = 24 * 60 * 60
PRICE_TICK = 0.01

PAPER_ACCOUNT_VERSION = 2
PAPER_BANKROLL_USD = 100.0
# The real version would need both venues prefunded. Split the user's planned
# $100 bankroll evenly and leave a 20% buffer on each side by quoting $40.
START_CRYPTAL_USD = 50.0
START_BINANCE_USDT = 50.0
ORDER_NOTIONAL_USD = 40.0

# Current public pair metadata and Cryptal's published fee schedule both state 0.25%
# for maker and taker. Binance's VIP-0 USD-M futures taker fee is modelled at a
# conservative 5 bps per hedge execution; a user's actual tier can only improve it.
CRYPTAL_MAKER_FEE_RATE = 0.0025
BINANCE_TAKER_FEE_RATE = 0.0005
HEDGE_SLIPPAGE_BPS = 1.0
FUNDING_AND_BASIS_RESERVE_BPS = 10.0
MIN_PROJECTED_NET_BPS = 50.0
ADVERSE_SELECTION_RESERVE_BPS = 40.0
REPRICE_BPS = 8.0
MIN_PAPER_CYCLES = 30


class CryptalRateLimitError(RuntimeError):
    """Public Cryptal gateway rejected the request and supplied no JSON body."""

    def __init__(self, status: int, endpoint: str, retry_after: float):
        self.status = int(status)
        self.endpoint = endpoint
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(
            f"Cryptal HTTP {self.status} for {self.endpoint}; "
            f"shared collector backing off {self.retry_after:.0f}s"
        )


async def _public_json_request(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> tuple[int, Any, str]:
    """Perform the module's sole public, read-only HTTP operation."""
    async with session.get(url, params=params) as response:
        status = int(response.status)
        if status == 200:
            return status, await response.json(), ""
        return status, None, (await response.text())[:120]


class CryptalPublicDataHub:
    """Rate-safe shared cache for every Cryptal public-data consumer.

    Cryptal advertises a 20-token burst and a 20-token/second replenish rate.
    The application deliberately stays below that ceiling, shares duplicate
    conversion books, and applies one process-wide backoff after a 403/429.
    """

    def __init__(self, *, min_interval_sec: float = 0.0, cache_ttl_sec: float = 0.0):
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.cache_ttl_sec = max(0.0, float(cache_ttl_sec))
        self._lock: asyncio.Lock | None = None
        self._last_request_at = 0.0
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self.blocked_until = 0.0
        self.consecutive_blocks = 0
        self.request_count = 0
        self.cache_hits = 0
        self.last_status = 0
        self.last_error = ""

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _key(url: str, params: dict | None) -> tuple:
        return url, tuple(sorted((params or {}).items()))

    @staticmethod
    def _endpoint(url: str) -> str:
        marker = "/api/v1/public/"
        return url.split(marker, 1)[-1] if marker in url else url

    def retry_delay(self) -> float:
        return max(0.0, self.blocked_until - time.monotonic())

    def state(self) -> dict:
        return {
            "request_count": self.request_count,
            "cache_hits": self.cache_hits,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "backoff_remaining_sec": round(self.retry_delay(), 1),
            "minimum_request_spacing_sec": self.min_interval_sec,
            "shared_cache_ttl_sec": self.cache_ttl_sec,
        }

    async def get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict | None = None,
        *,
        cache_ttl_sec: float | None = None,
    ) -> Any:
        ttl = self.cache_ttl_sec if cache_ttl_sec is None else max(0.0, cache_ttl_sec)
        key = self._key(url, params)
        now = time.monotonic()
        cached = self._cache.get(key)
        if ttl > 0 and cached and now - cached[0] <= ttl:
            self.cache_hits += 1
            return copy.deepcopy(cached[1])

        async with self._get_lock():
            now = time.monotonic()
            cached = self._cache.get(key)
            if ttl > 0 and cached and now - cached[0] <= ttl:
                self.cache_hits += 1
                return copy.deepcopy(cached[1])

            blocked = self.retry_delay()
            if blocked > 0:
                raise CryptalRateLimitError(
                    self.last_status or 429, self._endpoint(url), blocked
                )
            spacing = self.min_interval_sec - (now - self._last_request_at)
            if spacing > 0:
                await asyncio.sleep(spacing)

            status, payload, body = await _public_json_request(
                session, url, params
            )
            self._last_request_at = time.monotonic()
            self.request_count += 1
            self.last_status = status
            if status in (403, 429):
                self.consecutive_blocks += 1
                delay = min(120.0, 5.0 * (2 ** (self.consecutive_blocks - 1)))
                self.blocked_until = time.monotonic() + delay
                self.last_error = f"HTTP {status} at {self._endpoint(url)}"
                raise CryptalRateLimitError(status, self._endpoint(url), delay)
            if status != 200:
                self.last_error = f"HTTP {status} at {self._endpoint(url)}"
                raise RuntimeError(self.last_error)

            self.consecutive_blocks = 0
            self.blocked_until = 0.0
            self.last_error = ""
            self._cache[key] = (time.monotonic(), copy.deepcopy(payload))
            return payload

GEORGIAN_MARKET_AUDIT = {
    "as_of": "2026-08-12",
    "nbg_register_as_of": "2026-08-06",
    "registered_vasps_in_scope": 42,
    "qualification": (
        "Georgian-venue public CLOB + timestamped trade tape + fees + "
        "executable currency conversion + live delta hedge"
    ),
    "included": [
        "Cryptal public market catalog (81 observed; 60 hedgeable when checked)"
    ],
    "excluded": {
        "WhiteBIT Georgia": (
            "global WhiteBIT books; no Georgia-isolated order book or GEL spot pair"
        ),
        "Bybit Georgia": (
            "global Bybit books; no Georgia-isolated order book or GEL spot pair"
        ),
        "Mycoins": "no public central limit order book plus timestamped trade tape found",
        "Coinmania": "wallet/P2P surface; no public central limit order book plus trade tape found",
        "other_registered_vasps": "no verifiable public local CLOB and trade tape found",
    },
    "report": "docs/georgian_btc_gel_market_audit.md",
}


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _round_down(value: float, tick: float = PRICE_TICK) -> float:
    return math.floor((value + 1e-10) / tick) * tick


def _round_up(value: float, tick: float = PRICE_TICK) -> float:
    return math.ceil((value - 1e-10) / tick) * tick


def _book_levels(rows: Any, reverse: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price, volume = _number(row.get("price")), _number(row.get("volume"))
        if price > 0 and volume > 0:
            levels.append((price, volume))
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


class CryptalMakerPaperBot:
    def __init__(
        self,
        state_path: str | None = None,
        *,
        pair: str | None = None,
        stable_pair: str | None = None,
        display_pair: str | None = None,
        base_asset: str = "BTC",
        quote_currency: str = "USD",
        hedge_symbol: str = "BTCUSDT",
        price_tick: float = PRICE_TICK,
        quantity_step: float = 0.000001,
        maker_fee_rate: float = CRYPTAL_MAKER_FEE_RATE,
        minimum_cost_quote: float = 5.0,
        data_hub: CryptalPublicDataHub | None = None,
    ):
        self.pair = pair or CRYPTAL_PAIR
        self.stable_pair = stable_pair or STABLE_PAIR
        self.display_pair = display_pair or "BTC-TOUSD"
        self.base_asset = str(base_asset or "BTC").upper()
        self.quote_currency = str(quote_currency or "USD").upper()
        self.hedge_symbol = str(hedge_symbol or f"{self.base_asset}USDT").upper()
        self.price_tick = max(1e-12, _number(price_tick) or PRICE_TICK)
        self.price_decimals = max(
            0, min(12, -Decimal(str(self.price_tick)).as_tuple().exponent)
        )
        self.quantity_step = max(1e-12, _number(quantity_step) or 0.000001)
        self.maker_fee_rate = max(0.0, _number(maker_fee_rate))
        self.minimum_cost_quote = max(0.0, _number(minimum_cost_quote))
        # Tests and standalone bots get an isolated no-cache hub. The dashboard
        # explicitly injects one shared throttled hub into every live consumer.
        self.data_hub = data_hub or CryptalPublicDataHub()
        self.state_path = state_path or STATE_PATH
        self.instance_name = (
            NAME if self.pair == CRYPTAL_PAIR
            else f"Cryptal {self.display_pair} Maker + Binance Hedge"
        )
        self.enabled = True
        self.live_trading_enabled = False
        # This legacy attribute is native quote cash. It remains named ``_usd`` so
        # existing BTC-TOUSD state and integrations stay backward compatible.
        self.cryptal_cash_usd = (
            START_CRYPTAL_USD if self.quote_currency == "USD" else 0.0
        )
        self.starting_quote_cash = self.cryptal_cash_usd
        self._quote_cash_initialized = self.quote_currency == "USD"
        self.binance_balance_usdt = START_BINANCE_USDT
        self.spot_qty = 0.0
        self.spot_entry = 0.0
        self.spot_cost_usd = 0.0
        self.short_qty = 0.0
        self.short_entry_usdt = 0.0
        self.quote: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.market: dict = {}
        self.status = "starting public market-data collector"
        self.data_error = ""
        self.persistence_error = ""
        self.last_poll_at = 0.0
        self.last_good_at = 0.0
        self.poll_count = 0
        self.quote_count = 0
        self.fill_count = 0
        self.opportunity_count = 0
        self._seen_trade_ids: list[str] = []
        self._seen_trade_set: set[str] = set()
        self._absorbed_backlog = False
        self.initial_equity_usd = 0.0
        self._cycle_start_equity = self.start_equity
        self._cycle_opened_at = 0.0
        self._cycle_buy_price = 0.0
        self._load()

    @property
    def start_equity(self) -> float:
        return (self.initial_equity_usd
                or START_CRYPTAL_USD + START_BINANCE_USDT)

    def _note(self, message: str, kind: str = "info") -> None:
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": message})
        self.log = self.log[:80]
        print(f"[cryptal-maker {self.display_pair}] {message}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({
                    "paper_account_version": PAPER_ACCOUNT_VERSION,
                    "paper_bankroll_usd": PAPER_BANKROLL_USD,
                    "market_pair": self.pair,
                    "stable_pair": self.stable_pair,
                    "display_pair": self.display_pair,
                    "base_asset": self.base_asset,
                    "quote_currency": self.quote_currency,
                    "hedge_symbol": self.hedge_symbol,
                    "price_tick": self.price_tick,
                    "quantity_step": self.quantity_step,
                    "maker_fee_rate": self.maker_fee_rate,
                    "enabled": self.enabled,
                    "cryptal_cash_usd": self.cryptal_cash_usd,
                    "cryptal_cash_quote": self.cryptal_cash_usd,
                    "starting_quote_cash": self.starting_quote_cash,
                    "quote_cash_initialized": self._quote_cash_initialized,
                    "binance_balance_usdt": self.binance_balance_usdt,
                    "spot_qty": self.spot_qty,
                    "spot_entry": self.spot_entry,
                    "spot_cost_usd": self.spot_cost_usd,
                    "short_qty": self.short_qty,
                    "short_entry_usdt": self.short_entry_usdt,
                    "quote": self.quote,
                    "history": self.history[:300],
                    "log": self.log[:80],
                    "poll_count": self.poll_count,
                    "quote_count": self.quote_count,
                    "fill_count": self.fill_count,
                    "opportunity_count": self.opportunity_count,
                    "seen_trade_ids": self._seen_trade_ids[-1000:],
                    "absorbed_backlog": self._absorbed_backlog,
                    "initial_equity_usd": self.initial_equity_usd,
                    "cycle_start_equity": self._cycle_start_equity,
                    "cycle_opened_at": self._cycle_opened_at,
                    "cycle_buy_price": self._cycle_buy_price,
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
            self.persistence_error = ""
        except Exception as exc:
            self.persistence_error = (
                f"state save failed: {type(exc).__name__}: {str(exc)[:120]}")

    def _load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
            if int(data.get("paper_account_version", 0)) != PAPER_ACCOUNT_VERSION:
                self.persistence_error = (
                    "legacy $200 paper account ignored; starting the requested "
                    "$100 paper bankroll")
                return
            stored_pair = str(data.get("market_pair") or CRYPTAL_PAIR)
            if stored_pair != self.pair:
                self.persistence_error = (
                    f"state for {stored_pair} ignored by the {self.pair} paper ledger")
                return
            self.enabled = bool(data.get("enabled", self.enabled))
            self.cryptal_cash_usd = _number(
                data.get(
                    "cryptal_cash_quote",
                    data.get("cryptal_cash_usd", self.cryptal_cash_usd),
                )
            )
            self._quote_cash_initialized = bool(
                data.get("quote_cash_initialized", self.quote_currency == "USD")
            )
            self.starting_quote_cash = _number(data.get(
                "starting_quote_cash",
                START_CRYPTAL_USD if self.quote_currency == "USD" else 0.0,
            ))
            self.binance_balance_usdt = _number(
                data.get("binance_balance_usdt", self.binance_balance_usdt))
            self.spot_qty = _number(data.get("spot_qty"))
            self.spot_entry = _number(data.get("spot_entry"))
            self.spot_cost_usd = _number(data.get("spot_cost_usd"))
            self.short_qty = _number(data.get("short_qty"))
            self.short_entry_usdt = _number(data.get("short_entry_usdt"))
            self.quote = data.get("quote") if isinstance(data.get("quote"), dict) else None
            self.history = [row for row in data.get("history", []) if isinstance(row, dict)]
            self.log = [row for row in data.get("log", []) if isinstance(row, dict)]
            self.poll_count = int(data.get("poll_count", 0))
            self.quote_count = int(data.get("quote_count", 0))
            self.fill_count = int(data.get("fill_count", 0))
            self.opportunity_count = int(data.get("opportunity_count", 0))
            self._seen_trade_ids = [str(value) for value in data.get("seen_trade_ids", [])][-1000:]
            self._seen_trade_set = set(self._seen_trade_ids)
            self._absorbed_backlog = bool(data.get("absorbed_backlog", False))
            self.initial_equity_usd = _number(data.get("initial_equity_usd"))
            self._cycle_start_equity = _number(
                data.get("cycle_start_equity", self.start_equity))
            self._cycle_opened_at = _number(data.get("cycle_opened_at"))
            self._cycle_buy_price = _number(data.get("cycle_buy_price"))
        except Exception as exc:
            self.persistence_error = (
                f"state load failed: {type(exc).__name__}: {str(exc)[:120]}")
            self.enabled = False
            self.quote = None

    @staticmethod
    async def _json(session: aiohttp.ClientSession, url: str,
                    params: dict | None = None) -> Any:
        status, payload, body = await _public_json_request(session, url, params)
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {body}")
        return payload

    async def _fetch_snapshot(self, session: aiohttp.ClientSession) -> dict:
        btc_url = f"{CRYPTAL_BASE}/api/v1/public/orderbook/{self.pair}"
        stable_url = f"{CRYPTAL_BASE}/api/v1/public/orderbook/{self.stable_pair}"
        trades_url = f"{CRYPTAL_BASE}/api/v1/public/trades/{self.pair}"
        requests = [
            self.data_hub.get(session, btc_url, {"limit": 25}),
            self.data_hub.get(session, stable_url, {"limit": 25}),
            self.data_hub.get(session, trades_url, {"limit": 100}),
            self._json(session, BINANCE_BOOK_URL, {"symbol": self.hedge_symbol}),
        ]
        needs_settlement = self.stable_pair != SETTLEMENT_PAIR
        if needs_settlement:
            settlement_url = (
                f"{CRYPTAL_BASE}/api/v1/public/orderbook/{SETTLEMENT_PAIR}"
            )
            requests.append(self.data_hub.get(
                session, settlement_url, {"limit": 25}
            ))
        results = await asyncio.gather(*requests)
        btc_book, stable_book, trades, binance = results[:4]
        settlement_book = results[4] if needs_settlement else stable_book
        bids = _book_levels(btc_book.get("bids"), reverse=True)
        asks = _book_levels(btc_book.get("asks"), reverse=False)
        stable_bids = _book_levels(stable_book.get("bids"), reverse=True)
        stable_asks = _book_levels(stable_book.get("asks"), reverse=False)
        settlement_bids = _book_levels(settlement_book.get("bids"), reverse=True)
        settlement_asks = _book_levels(settlement_book.get("asks"), reverse=False)
        if (not bids or not asks or not stable_bids or not stable_asks
                or not settlement_bids or not settlement_asks):
            raise ValueError("Cryptal returned an empty or one-sided order book")
        if (bids[0][0] >= asks[0][0]
                or stable_bids[0][0] >= stable_asks[0][0]
                or settlement_bids[0][0] >= settlement_asks[0][0]):
            raise ValueError("Cryptal returned a locked or crossed order book")
        binance_bid = _number(binance.get("bidPrice"))
        binance_ask = _number(binance.get("askPrice"))
        if binance_bid <= 0 or binance_ask <= binance_bid:
            raise ValueError("Binance returned a missing or crossed hedge book")
        return {
            "received_at": time.time(),
            "cryptal_at": _number(btc_book.get("timestamp")) / 1000.0,
            "stable_at": _number(stable_book.get("timestamp")) / 1000.0,
            "settlement_at": _number(settlement_book.get("timestamp")) / 1000.0,
            "binance_at": _number(binance.get("time")) / 1000.0,
            "pair_label": self.pair,
            "stable_pair_label": self.stable_pair,
            "settlement_pair_label": SETTLEMENT_PAIR,
            "quote_currency": self.quote_currency,
            "bids": bids,
            "asks": asks,
            "stable_bids": stable_bids,
            "stable_asks": stable_asks,
            "settlement_bids": settlement_bids,
            "settlement_asks": settlement_asks,
            "binance_bid": binance_bid,
            "binance_ask": binance_ask,
            "trades": [row for row in trades or [] if isinstance(row, dict)],
        }

    @staticmethod
    def _snapshot_market(snapshot: dict) -> dict:
        bid, ask = snapshot["bids"][0][0], snapshot["asks"][0][0]
        stable_bid = snapshot["stable_bids"][0][0]
        stable_ask = snapshot["stable_asks"][0][0]
        stable_mid = (stable_bid + stable_ask) / 2.0
        settlement_bids = snapshot.get("settlement_bids") or snapshot["stable_bids"]
        settlement_asks = snapshot.get("settlement_asks") or snapshot["stable_asks"]
        settlement_bid = settlement_bids[0][0]
        settlement_ask = settlement_asks[0][0]
        settlement_mid = (settlement_bid + settlement_ask) / 2.0
        binance_bid, binance_ask = snapshot["binance_bid"], snapshot["binance_ask"]
        binance_mid = (binance_bid + binance_ask) / 2.0
        fair = binance_mid * stable_mid
        return {
            "cryptal_bid": bid,
            "cryptal_ask": ask,
            "cryptal_spread_bps": (ask - bid) / ((ask + bid) / 2.0) * 1e4,
            "stable_bid": stable_bid,
            "stable_ask": stable_ask,
            "stable_mid": stable_mid,
            "settlement_bid": settlement_bid,
            "settlement_ask": settlement_ask,
            "settlement_mid": settlement_mid,
            "binance_bid": binance_bid,
            "binance_ask": binance_ask,
            "binance_mid": binance_mid,
            "fair_quote": fair,
            # Backward-compatible name for the original BTC-TOUSD dashboard.
            "fair_usd": fair,
            "fair_usd_equivalent": binance_mid * settlement_mid,
            "bid_discount_bps": (fair - bid) / fair * 1e4,
            "ask_premium_bps": (ask - fair) / fair * 1e4,
            "received_at": snapshot["received_at"],
        }

    @staticmethod
    def _data_error(snapshot: dict) -> str:
        now = _number(snapshot.get("received_at")) or time.time()
        checks = [
            ("cryptal_at", f"{snapshot.get('pair_label') or 'BTC-USD'} book"),
            ("stable_at", f"{snapshot.get('stable_pair_label') or 'USDT-USD'} book"),
            ("binance_at", "Binance hedge book"),
        ]
        if snapshot.get("settlement_at") is not None and (
            snapshot.get("settlement_pair_label")
            != snapshot.get("stable_pair_label")
        ):
            checks.append((
                "settlement_at",
                f"{snapshot.get('settlement_pair_label') or SETTLEMENT_PAIR} book",
            ))
        for key, label in checks:
            stamp = _number(snapshot.get(key))
            if stamp <= 0:
                return f"{label} has no exchange timestamp"
            age = now - stamp
            if age < -2 or age > MAX_DATA_AGE_SEC:
                return f"{label} is stale ({age:.1f}s)"
        return ""

    @staticmethod
    def _required_half_spread_bps(
        maker_fee_rate: float = CRYPTAL_MAKER_FEE_RATE,
    ) -> float:
        round_trip_cost = (
            2 * maker_fee_rate * 1e4
            + 2 * BINANCE_TAKER_FEE_RATE * 1e4
            + 2 * HEDGE_SLIPPAGE_BPS
            + FUNDING_AND_BASIS_RESERVE_BPS
        )
        return ((round_trip_cost + MIN_PROJECTED_NET_BPS) / 2.0
                + ADVERSE_SELECTION_RESERVE_BPS)

    def _desired_bid(self, snapshot: dict) -> tuple[float, float, float] | None:
        market = self._snapshot_market(snapshot)
        fair = market["fair_quote"]
        max_safe = fair * (
            1.0 - self._required_half_spread_bps(self.maker_fee_rate) / 1e4
        )
        price = _round_down(
            min(market["cryptal_bid"] + self.price_tick, max_safe),
            self.price_tick,
        )
        if price <= 0 or price >= market["cryptal_ask"]:
            return None
        affordable = self.cryptal_cash_usd / (price * (1.0 + self.maker_fee_rate))
        hedge_capacity = self.binance_balance_usdt / max(
            market["binance_mid"], 1e-12
        )
        quote_limit = ORDER_NOTIONAL_USD
        if self.quote_currency != "USD":
            # Convert the $40 cap into native quote currency at executable sides:
            # TOGEL -> USDT at the local ask, then USDT -> TOUSD at the settlement bid.
            quote_limit *= market["stable_ask"] / max(market["settlement_bid"], 1e-9)
        qty = min(quote_limit / price, affordable, hedge_capacity)
        qty = math.floor((qty + 1e-15) / self.quantity_step) * self.quantity_step
        if qty <= 0 or qty * price < self.minimum_cost_quote:
            return None
        expected_gross_bps = (fair - price) / fair * 1e4
        return price, qty, expected_gross_bps

    def _projected_cycle_pnl(self, ask_price: float, hedge_close_usdt: float,
                             stable_bid: float, stable_ask: float) -> float:
        qty = min(self.spot_qty, self.short_qty)
        if qty <= 0:
            return 0.0
        spot = qty * (
            ask_price * (1.0 - self.maker_fee_rate)
            - self.spot_entry * (1.0 + self.maker_fee_rate)
        )
        hedge_usdt = qty * (
            self.short_entry_usdt - hedge_close_usdt
            - BINANCE_TAKER_FEE_RATE * (self.short_entry_usdt + hedge_close_usdt)
        )
        hedge_usd = hedge_usdt * (stable_bid if hedge_usdt >= 0 else stable_ask)
        return spot + hedge_usd

    def _required_ask(self, snapshot: dict) -> tuple[float, float, float] | None:
        if self.spot_qty <= 0 or self.short_qty <= 0:
            return None
        market = self._snapshot_market(snapshot)
        close_exec = market["binance_ask"] * (1.0 + HEDGE_SLIPPAGE_BPS / 1e4)
        qty = min(self.spot_qty, self.short_qty)
        # Preserve the funding/stable-basis reserve on the actual exit calculation as
        # well as on the initial quote. Otherwise a moved market could reprice the ask
        # to exactly the headline target and silently spend the reserve.
        target_bps = MIN_PROJECTED_NET_BPS + FUNDING_AND_BASIS_RESERVE_BPS
        target = qty * self.spot_entry * target_bps / 1e4
        hedge_usdt = qty * (
            self.short_entry_usdt - close_exec
            - BINANCE_TAKER_FEE_RATE * (self.short_entry_usdt + close_exec)
        )
        hedge_usd = hedge_usdt * (
            market["stable_bid"] if hedge_usdt >= 0 else market["stable_ask"])
        required = (
            target - hedge_usd
            + qty * self.spot_entry * (1.0 + self.maker_fee_rate)
        ) / (qty * (1.0 - self.maker_fee_rate))
        price = _round_up(
            max(market["cryptal_ask"] - self.price_tick, required),
            self.price_tick,
        )
        if price <= market["cryptal_bid"]:
            return None
        projected = self._projected_cycle_pnl(
            price, close_exec, market["stable_bid"], market["stable_ask"])
        projected_bps = projected / max(qty * self.spot_entry, 1e-9) * 1e4
        return price, qty, projected_bps

    @staticmethod
    def _exchange_watermark(snapshot: dict) -> float:
        """Cryptal-clock instant at or after which a new quote may be considered live.

        Fill eligibility must be decided entirely in exchange time. Comparing a
        Cryptal trade timestamp against a local clock lets any skew widen the
        acceptance window: with Cryptal reading d seconds ahead, a print that truly
        happened up to (tolerance + d) seconds before placement still passes and
        books a fill for an order that did not exist yet. The book timestamp and the
        newest visible print are both stamped by Cryptal, so the later of the two is
        a conservative activation point on a single clock.
        """
        watermark = _number(snapshot.get("cryptal_at"))
        for row in snapshot.get("trades") or []:
            if isinstance(row, dict):
                watermark = max(watermark, _number(row.get("timestamp")) / 1000.0)
        return watermark

    @staticmethod
    def _queue_ahead(snapshot: dict, side: str, price: float) -> float:
        levels = snapshot["bids"] if side == "BID" else snapshot["asks"]
        if side == "BID":
            return sum(volume for level, volume in levels if level >= price - 1e-9)
        return sum(volume for level, volume in levels if level <= price + 1e-9)

    def _place_or_reprice(self, side: str, price: float, qty: float,
                          expected_bps: float, snapshot: dict) -> bool:
        metric = "discount_to_fair" if side == "BID" else "projected_cycle_net"
        watermark = self._exchange_watermark(snapshot)
        current = self.quote
        if current and current.get("side") == side and _number(current.get("price")) > 0:
            change_bps = abs(price / _number(current["price"]) - 1.0) * 1e4
            if change_bps < REPRICE_BPS and abs(qty - _number(current.get("qty"))) < 1e-9:
                current["edge_metric"] = metric
                current["edge_metric_bps"] = round(expected_bps, 2)
                # A quote restored from state predates this field. Activate it from
                # the current exchange watermark rather than letting it stay unfillable.
                if _number(current.get("activation_exchange_at")) <= 0:
                    current["activation_exchange_at"] = watermark
                return False
        queue = self._queue_ahead(snapshot, side, price)
        self.quote = {
            "side": side,
            "price": round(price, self.price_decimals),
            "qty": qty,
            "remaining_qty": qty,
            "queue_ahead_btc": queue,
            "queue_ahead_base": queue,
            "placed_at": time.time(),
            "activation_exchange_at": watermark,
            "edge_metric": metric,
            "edge_metric_bps": round(expected_bps, 2),
        }
        self.quote_count += 1
        self._note(
            f"PAPER {side} {qty:.8f} {self.base_asset} @ {price:,.8f}; "
            f"visible queue ahead {queue:.8f} {self.base_asset}", "quote")
        return True

    def _remember_trade(self, trade_id: str) -> None:
        if trade_id in self._seen_trade_set:
            return
        self._seen_trade_ids.append(trade_id)
        self._seen_trade_set.add(trade_id)
        while len(self._seen_trade_ids) > 1000:
            old = self._seen_trade_ids.pop(0)
            self._seen_trade_set.discard(old)

    @staticmethod
    def _trade_sort_key(trade: dict) -> tuple[float, str]:
        return _number(trade.get("timestamp")), str(trade.get("id") or "")

    def _new_trades(self, rows: list[dict]) -> list[dict]:
        ordered = sorted(rows, key=self._trade_sort_key)
        if not self._absorbed_backlog:
            for trade in ordered:
                trade_id = str(trade.get("id") or "")
                if trade_id:
                    self._remember_trade(trade_id)
            self._absorbed_backlog = True
            return []
        fresh = []
        for trade in ordered:
            trade_id = str(trade.get("id") or "")
            if not trade_id or trade_id in self._seen_trade_set:
                continue
            self._remember_trade(trade_id)
            fresh.append(trade)
        return fresh

    def _trade_reaches_quote(self, trade: dict) -> bool:
        if not self.quote:
            return False
        price = _number(trade.get("price"))
        quote_price = _number(self.quote.get("price"))
        # A print that happened before this virtual order existed cannot fill it even
        # if pagination makes its id appear for the first time on a later poll. Both
        # sides of this comparison are Cryptal timestamps, so venue/host clock skew
        # cannot widen the window; an unverifiable print fails closed.
        # The watermark is read from the snapshot that *precedes* the virtual quote,
        # so a print stamped at that exact millisecond is not proven to post-date the
        # order. Cryptal stamps to the millisecond, making that boundary reachable in
        # production, so equality is rejected rather than assumed favourable.
        activation_at = _number(self.quote.get("activation_exchange_at"))
        trade_at = _number(trade.get("timestamp")) / 1000.0
        if activation_at <= 0 or trade_at <= 0 or trade_at <= activation_at:
            return False
        if self.quote.get("side") == "BID":
            return price <= quote_price + 1e-9
        return price >= quote_price - 1e-9

    def _fill_bid(self, qty: float, price: float, snapshot: dict) -> None:
        market = self._snapshot_market(snapshot)
        hedge_price = market["binance_bid"] * (1.0 - HEDGE_SLIPPAGE_BPS / 1e4)
        spot_cost = qty * price * (1.0 + self.maker_fee_rate)
        hedge_fee = qty * hedge_price * BINANCE_TAKER_FEE_RATE
        if spot_cost > self.cryptal_cash_usd + 1e-9 or hedge_fee > self.binance_balance_usdt:
            self.enabled = False
            self.quote = None
            self._note("paper fill rejected: insufficient prefunded inventory", "error")
            return
        self._cycle_start_equity = self._equity(market)
        self._cycle_opened_at = time.time()
        self._cycle_buy_price = price
        self.cryptal_cash_usd -= spot_cost
        self.binance_balance_usdt -= hedge_fee
        old_qty = self.spot_qty
        self.spot_qty += qty
        self.spot_entry = (
            (self.spot_entry * old_qty + price * qty) / self.spot_qty)
        self.spot_cost_usd += spot_cost
        old_short = self.short_qty
        self.short_qty += qty
        self.short_entry_usdt = (
            (self.short_entry_usdt * old_short + hedge_price * qty) / self.short_qty)
        self.fill_count += 1
        self._note(
            f"PAPER FILL bought {qty:.8f} {self.base_asset} on Cryptal @ {price:,.8f}; "
            f"short hedge @ {hedge_price:,.2f}", "open")

    def _fill_ask(self, qty: float, price: float, snapshot: dict,
                  reason: str = "maker_exit") -> None:
        market = self._snapshot_market(snapshot)
        qty = min(qty, self.spot_qty, self.short_qty)
        if qty <= 0:
            return
        close_price = market["binance_ask"] * (1.0 + HEDGE_SLIPPAGE_BPS / 1e4)
        spot_proceeds = qty * price * (1.0 - self.maker_fee_rate)
        hedge_pnl = qty * (self.short_entry_usdt - close_price)
        close_fee = qty * close_price * BINANCE_TAKER_FEE_RATE
        self.cryptal_cash_usd += spot_proceeds
        self.binance_balance_usdt += hedge_pnl - close_fee
        self.spot_qty = max(0.0, self.spot_qty - qty)
        self.short_qty = max(0.0, self.short_qty - qty)
        self.fill_count += 1
        if self.spot_qty <= 1e-10 and self.short_qty <= 1e-10:
            self.spot_qty = self.short_qty = 0.0
            self.spot_entry = self.short_entry_usdt = 0.0
            self.spot_cost_usd = 0.0
            equity = self._equity(market)
            pnl = equity - self._cycle_start_equity
            record = {
                "opened_at": self._cycle_opened_at or time.time(),
                "closed_at": time.time(),
                "buy": round(self._cycle_buy_price, self.price_decimals),
                "sell": round(price, self.price_decimals),
                "hedge_close": round(close_price, 8),
                "pnl": round(pnl, 6),
                "return_bps": round(pnl / max(ORDER_NOTIONAL_USD, 1.0) * 1e4, 2),
                "reason": reason,
                "fill_model": "public-trade crossing after visible queue depletion",
            }
            self.history.insert(0, record)
            self.history = self.history[:300]
            self._cycle_opened_at = 0.0
            self._cycle_buy_price = 0.0
            self._note(
                f"PAPER CYCLE {reason} @ {price:,.8f}; "
                f"net P&L {pnl:+.4f} USD-equivalent",
                "win" if pnl > 0 else "loss")

    def _force_expired_inventory(self, snapshot: dict) -> None:
        if (self.spot_qty <= 0 or self.short_qty <= 0
                or self._cycle_opened_at <= 0
                or time.time() - self._cycle_opened_at < MAX_HEDGED_HOLD_SEC):
            return
        # The honest emergency exit is the executable Cryptal bid, not the hoped-for
        # ask. Cryptal charges the same 25 bps as taker, so _fill_ask's fee arithmetic
        # remains correct while the recorded reason distinguishes the forced unwind.
        bid = snapshot["bids"][0][0]
        self.quote = None
        self._fill_ask(
            min(self.spot_qty, self.short_qty), bid, snapshot,
            reason="24h_time_stop_crossed_spread")

    def _process_fills(self, trades: list[dict], snapshot: dict) -> None:
        for trade in trades:
            if not self.quote or not self._trade_reaches_quote(trade):
                continue
            traded = _number(trade.get("volume"))
            if traded <= 0:
                continue
            queue = _number(self.quote.get("queue_ahead_btc"))
            consumed = min(queue, traded)
            queue -= consumed
            traded -= consumed
            self.quote["queue_ahead_btc"] = queue
            self.quote["queue_ahead_base"] = queue
            if traded <= 0:
                continue
            remaining = _number(self.quote.get("remaining_qty"))
            fill_qty = min(remaining, traded)
            side, price = self.quote.get("side"), _number(self.quote.get("price"))
            if side == "BID":
                self.quote = None
                self._fill_bid(fill_qty, price, snapshot)
            else:
                self.quote["remaining_qty"] = max(0.0, remaining - fill_qty)
                self._fill_ask(fill_qty, price, snapshot)
                if not self.quote["remaining_qty"] or self.spot_qty <= 0:
                    self.quote = None

    def _refresh_quote(self, snapshot: dict) -> None:
        # Pausing means no NEW inventory. A filled spot/hedge pair must continue
        # working its risk-reducing ask even while the entry toggle is off.
        if self.spot_qty > 0 or self.short_qty > 0:
            desired = self._required_ask(snapshot)
            if desired is None:
                self.quote = None
                self.status = "hedged inventory; no profitable maker ask currently available"
                return
            price, qty, projected = desired
            self._place_or_reprice("ASK", price, qty, projected, snapshot)
            self.status = (
                f"spot {self.base_asset} held and delta-hedged; "
                "passive Cryptal ask working"
            )
            return
        if not self.enabled:
            self.quote = None
            self.status = "paper entry quoting paused"
            return
        desired = self._desired_bid(snapshot)
        if desired is None:
            self.quote = None
            self.status = "flat; no safely funded maker bid"
            return
        price, qty, gross = desired
        if self._place_or_reprice("BID", price, qty, gross, snapshot):
            self.opportunity_count += 1
        self.status = "flat cash; passive Cryptal bid working"

    def _equity(self, market: dict | None = None) -> float:
        market = market or self.market
        fair = _number(market.get("fair_quote", market.get("fair_usd")))
        stable_bid = _number(market.get("stable_bid")) or 1.0
        stable_ask = _number(market.get("stable_ask")) or stable_bid
        spot_value = self.spot_qty * fair
        short_mark = _number(market.get("binance_ask"))
        short_pnl_usdt = self.short_qty * (self.short_entry_usdt - short_mark)
        if self.quote_currency != "USD":
            settlement_bid = _number(market.get("settlement_bid"))
            settlement_ask = _number(market.get("settlement_ask")) or settlement_bid
            if stable_ask <= 0 or settlement_bid <= 0:
                return self.start_equity
            local_assets_usdt = (self.cryptal_cash_usd + spot_value) / stable_ask
            total_usdt = local_assets_usdt + self.binance_balance_usdt + short_pnl_usdt
            settlement = settlement_bid if total_usdt >= 0 else settlement_ask
            return total_usdt * settlement
        short_pnl_usd = short_pnl_usdt * (
            stable_bid if short_pnl_usdt >= 0 else stable_ask)
        return (self.cryptal_cash_usd + spot_value
                + self.binance_balance_usdt * stable_bid + short_pnl_usd)

    def _initialize_quote_cash(self, market: dict) -> None:
        if self._quote_cash_initialized:
            return
        stable_ask = _number(market.get("stable_ask"))
        settlement_bid = _number(market.get("settlement_bid"))
        if stable_ask <= 0 or settlement_bid <= 0:
            return
        # Pre-fund $50-equivalent native quote cash. Executable conversion sides are
        # deliberately used so the paper account cannot manufacture FX value.
        self.cryptal_cash_usd = START_CRYPTAL_USD * stable_ask / settlement_bid
        self.starting_quote_cash = self.cryptal_cash_usd
        self._quote_cash_initialized = True

    def _tick(self, snapshot: dict) -> None:
        self.poll_count += 1
        self.last_poll_at = time.time()
        error = self._data_error(snapshot)
        if error:
            self.data_error = error
            self.status = "data unsafe; quote cancelled"
            self.quote = None
            return
        self.data_error = ""
        self.last_good_at = time.time()
        self.market = self._snapshot_market(snapshot)
        self._initialize_quote_cash(self.market)
        if self.initial_equity_usd <= 0:
            self.initial_equity_usd = self._equity(self.market)
            self._cycle_start_equity = self.initial_equity_usd
        new_trades = self._new_trades(snapshot.get("trades") or [])
        self._process_fills(new_trades, snapshot)
        self._force_expired_inventory(snapshot)
        self._refresh_quote(snapshot)
        self._save()

    async def manage_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        # aiodns intermittently returns "Could not contact DNS servers" on this
        # Windows host while the OS resolver works. Use aiohttp's threaded system
        # resolver explicitly so the always-on collector follows the same path as
        # the browser and PowerShell connectivity checks.
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            while True:
                started = time.time()
                try:
                    snapshot = await self._fetch_snapshot(session)
                    self._tick(snapshot)
                except Exception as exc:
                    self.data_error = f"{type(exc).__name__}: {str(exc)[:140]}"
                    self.status = "public market-data error; quote cancelled"
                    self.quote = None
                delay = max(0.1, POLL_SEC - (time.time() - started))
                delay = max(delay, self.data_hub.retry_delay())
                await asyncio.sleep(delay)

    def set_enabled(self, enabled: bool) -> dict:
        self.enabled = bool(enabled)
        if not self.enabled and self.spot_qty <= 0 and self.short_qty <= 0:
            self.quote = None
        self._note(
            "paper quoting enabled" if self.enabled else
            "new paper bids paused; any existing hedged inventory will still exit")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self) -> dict:
        self.enabled = True
        self.cryptal_cash_usd = (
            START_CRYPTAL_USD if self.quote_currency == "USD" else 0.0
        )
        self.starting_quote_cash = self.cryptal_cash_usd
        self._quote_cash_initialized = self.quote_currency == "USD"
        self.binance_balance_usdt = START_BINANCE_USDT
        self.spot_qty = self.spot_entry = self.spot_cost_usd = 0.0
        self.short_qty = self.short_entry_usdt = 0.0
        self.quote = None
        self.history = []
        self.log = []
        self.poll_count = self.quote_count = self.fill_count = 0
        self.opportunity_count = 0
        self._seen_trade_ids = []
        self._seen_trade_set = set()
        self._absorbed_backlog = False
        self.initial_equity_usd = 0.0
        self._initialize_quote_cash(self.market)
        if _number(self.market.get("stable_bid")) > 0:
            self.initial_equity_usd = self._equity(self.market)
        self._cycle_start_equity = self.start_equity
        self._cycle_opened_at = 0.0
        self._cycle_buy_price = 0.0
        self._note("paper account reset")
        self._save()
        return {"ok": True}

    def _validation(self) -> dict:
        count = len(self.history)
        values = [_number(row.get("pnl")) for row in self.history]
        mean = sum(values) / count if count else None
        return {
            "status": "COLLECTING" if count < MIN_PAPER_CYCLES else "PAPER_RESULT_ONLY",
            "completed_cycles": count,
            "minimum_cycles": MIN_PAPER_CYCLES,
            "mean_pnl": round(mean, 6) if mean is not None else None,
            "evidentiary": False,
            "reason": (
                "Public prints cannot prove exact queue position. Paper results screen "
                "the idea but cannot authorize live trading."),
        }

    def state(self) -> dict:
        marked_equity = self._equity()
        # Stablecoin conversion can make $50 TOUSD + $50 USDT mark slightly above
        # or below $100 at the first tick. Normalize that opening mark to exactly
        # $100, then report every subsequent real conversion/strategy change as P&L.
        equity = PAPER_BANKROLL_USD + (marked_equity - self.start_equity)
        wins = sum(1 for row in self.history if _number(row.get("pnl")) > 0)
        quote = dict(self.quote) if self.quote else None
        return {
            "running": True,
            "name": self.instance_name,
            "enabled": self.enabled,
            "mode": "PAPER_ONLY",
            "live_trading_enabled": self.live_trading_enabled,
            "status": self.status,
            "data_error": self.data_error,
            "public_data_health": self.data_hub.state(),
            "persistence_error": self.persistence_error,
            "pair": self.pair,
            "display_pair": self.display_pair,
            "base_asset": self.base_asset,
            "quote_currency": self.quote_currency,
            "hedge_symbol": self.hedge_symbol,
            "hedge": f"Binance {self.hedge_symbol} perpetual",
            "poll_sec": POLL_SEC,
            "max_hedged_hold_sec": MAX_HEDGED_HOLD_SEC,
            "last_good_at": self.last_good_at,
            "poll_count": self.poll_count,
            "quote_count": self.quote_count,
            "fill_count": self.fill_count,
            "opportunity_count": self.opportunity_count,
            "market": self.market,
            "quote": quote,
            "inventory": {
                "spot_qty": round(self.spot_qty, 8),
                "spot_entry": round(self.spot_entry, self.price_decimals),
                "short_qty": round(self.short_qty, 8),
                "short_entry": round(self.short_entry_usdt, 8),
                "delta_btc": round(self.spot_qty - self.short_qty, 8),
                "base_asset": self.base_asset,
                "delta_base": round(self.spot_qty - self.short_qty, 8),
            },
            "cryptal_cash_usd": round(self.cryptal_cash_usd, 4),
            "cryptal_cash_quote": round(self.cryptal_cash_usd, 4),
            "binance_balance_usdt": round(self.binance_balance_usdt, 4),
            "paper_bankroll_usd": PAPER_BANKROLL_USD,
            "starting_allocation": {
                "cryptal_tousd": START_CRYPTAL_USD,
                "cryptal_quote_currency": self.quote_currency,
                "cryptal_quote_amount": round(self.starting_quote_cash, 4),
                "cryptal_usd_equivalent": START_CRYPTAL_USD,
                "binance_usdt": START_BINANCE_USDT,
                "maximum_quote_notional": ORDER_NOTIONAL_USD,
                "maximum_quote_notional_usd": ORDER_NOTIONAL_USD,
            },
            "marked_equity_tousd": round(marked_equity, 4),
            "marked_equity_usd": round(marked_equity, 4),
            "balance": round(equity, 4),
            "equity": round(equity, 4),
            "start_balance": PAPER_BANKROLL_USD,
            "total_pnl": round(equity - PAPER_BANKROLL_USD, 4),
            "trades": len(self.history),
            "wins": wins,
            "fees": {
                "cryptal_maker_bps_each_fill": self.maker_fee_rate * 1e4,
                "binance_taker_bps_each_hedge": BINANCE_TAKER_FEE_RATE * 1e4,
                "hedge_slippage_bps_each": HEDGE_SLIPPAGE_BPS,
                "funding_basis_reserve_bps": FUNDING_AND_BASIS_RESERVE_BPS,
                "minimum_projected_net_bps": MIN_PROJECTED_NET_BPS,
            },
            "validation": self._validation(),
            "georgian_market_audit": GEORGIAN_MARKET_AUDIT,
            "history": self.history[:40],
            "log": self.log[:30],
            "note": (
                "Automated public-data paper simulation. It never calls Cryptal or "
                "Binance private trading endpoints and cannot move real money."),
        }


class CryptalGelMakerPaperBot(CryptalMakerPaperBot):
    """Independent $100 paper ledger for Cryptal's live BTC-TOGEL order book."""

    def __init__(
        self,
        state_path: str | None = None,
        *,
        data_hub: CryptalPublicDataHub | None = None,
    ):
        super().__init__(
            state_path=state_path or GEL_STATE_PATH,
            pair=CRYPTAL_GEL_PAIR,
            stable_pair=GEL_STABLE_PAIR,
            display_pair="BTC-TOGEL",
            quote_currency="GEL",
            minimum_cost_quote=10.0,
            data_hub=data_hub,
        )
