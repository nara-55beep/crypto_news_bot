"""Public, paper-only arbitrage screen for registered Georgian crypto venues.

Cryptal exposes a local order book and timestamped prints, so its passive-maker
collector lives in :mod:`cryptal_georgian_scanner`.  The other machine-readable
Georgian venues found in the NBG register expose dealer conversion quotes instead.
This module keeps those different execution models separate: it compares fixed
quotes with executable Binance spot prices, reserves the cost of a temporary
perpetual hedge and transfer, and never turns a displayed quote into a paper fill.

There are deliberately no credentials, private endpoints or live-order methods.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from typing import Any

import aiohttp

import cryptal_maker_paper as maker


NBG_REGISTER_URL = (
    "https://nbg.gov.ge/en/page/virtual-asset-service-providers-vasps"
)
NBG_REGISTER_AS_OF = "2026-08-06"

# Every active head office in the English NBG workbook dated 2026-08-06.  The
# list is scope/audit metadata, not an endorsement and not a claim that each
# registrant operates an exchange or public API.
REGISTERED_VASPS: tuple[tuple[str, str], ...] = (
    ("0001-9404", "Mycoins"),
    ("0002-9404", "Cryptal"),
    ("0003-9404", "Bitanica"),
    ("0004-9404", "Bitexchange"),
    ("0005-9404", "Cryptomat"),
    ("0006-9404", "Coinet"),
    ("0007-9404", "WhiteBIT"),
    ("0008-9404", "Bithold"),
    ("0010-9404", "Alltrust.me"),
    ("0011-9404", "AURUM"),
    ("0012-9404", "CoinSwap"),
    ("0013-9404", "Coinmania"),
    ("0014-9404", "Bitnet"),
    ("0015-9404", "Digital Currency"),
    ("0016-9404", "CryptoExchange"),
    ("0017-9404", "PLEX"),
    ("0018-9404", "GECRYPTO"),
    ("0019-9404", "Bybit Georgia Limited"),
    ("0020-9404", "Crypto Exchange"),
    ("0021-9404", "Werty"),
    ("0022-9404", "Matio"),
    ("0023-9404", "Stellex"),
    ("0024-9404", "Cryptox"),
    ("0025-9404", "SPEX"),
    ("0026-9404", "City Pay"),
    ("0027-9404", "Crypto Change Batumi"),
    ("0028-9404", "Bitrust"),
    ("0029-9404", "Gaus Crypto"),
    ("0030-9404", "Coinero"),
    ("0031-9404", "BITARI"),
    ("0032-9404", "Bitcasa"),
    ("0033-9404", "PayBit"),
    ("0034-9404", "FinSec"),
    ("0035-9404", "Coinsflow"),
    ("0036-9404", "SOLUNEX"),
    ("0037-9404", "CoinnetX"),
    ("0038-9404", "Bipayhi"),
    ("0039-9404", "Covex"),
    ("0040-9404", "BitFi"),
    ("0041-9404", "Digital Assets Sakartvelo"),
    ("0042-9404", "GLOBAL CRYPTO"),
    ("0043-9404", "Fintrust"),
)

CAPABILITIES = {
    "Cryptal": (
        "public_orderbook_and_trades",
        "Integrated by the existing passive-maker paper collector.",
    ),
    "Coinet": (
        "public_fixed_quote_rest",
        "Integrated here; public limits are checked but quote acceptance is not assumed.",
    ),
    "Mycoins": (
        "public_fixed_quote_signalr",
        "Integrated here; public size limits were not exposed, so candidates fail closed.",
    ),
    "PLEX": (
        "public_fixed_quote_rest_manual",
        "Integrated as PlatformaEX; manual routes are visible but never called automated.",
    ),
    "WhiteBIT": (
        "global_public_orderbook_no_verified_local_gel_book",
        "Public global API exists; no distinct Georgian GEL order book was verified.",
    ),
    "Bybit Georgia Limited": (
        "global_public_orderbook_no_verified_local_gel_book",
        "Public global API exists; no distinct Georgian GEL order book was verified.",
    ),
    "Coinmania": (
        "machine_feed_blocked_by_challenge",
        "Public-looking prices exist, but the API returned a browser challenge.",
    ),
}

COINET_URL = "https://www.coinet.ge/api/Operational/Exchange/Pairs"
PLATFORMA_URL = (
    "https://www.platformaex.com/service/api/v1/public/exchanger/route/get"
)
MYCOINS_NEGOTIATE_URL = (
    "https://ws.mycoins.ge/ws/currencyHub/negotiate?negotiateVersion=1"
)
MYCOINS_WS_URL = "wss://ws.mycoins.ge/ws/currencyHub"
BINANCE_SPOT_BOOK_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BINANCE_FUTURES_BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

SCAN_INTERVAL_SEC = 15.0
PLATFORMA_REFRESH_SEC = 5 * 60.0
QUOTE_NOTIONAL_USD = 100.0
MAX_FIXED_QUOTE_AGE_SEC = 30.0
MIN_SCREENED_EDGE_BPS = 100.0
BINANCE_SPOT_TAKER_BPS = 10.0
TRANSFER_RESERVE_USD = 2.50
OPERATIONS_RESERVE_BPS = 25.0
MAX_HEDGE_BASIS_BPS = 150.0
MAX_DISPLAY_ROWS = 80


def _n(value: Any) -> float:
    return maker._number(value)


def _iso_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _capability_rows() -> list[dict]:
    rows = []
    for registration_id, name in REGISTERED_VASPS:
        capability, note = CAPABILITIES.get(
            name,
            (
                "no_verified_public_machine_market_feed",
                "Registered VASP, but no public order book or fixed-quote feed was verified.",
            ),
        )
        rows.append({
            "registration_id": registration_id,
            "name": name,
            "capability": capability,
            "note": note,
        })
    return rows


class MycoinsRateFeed:
    """Maintain the official Mycoins SignalR public rate snapshot."""

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; GeorgianVenuePaperScanner/1.0)",
        "Origin": "https://mycoins.ge",
        "Referer": "https://mycoins.ge/",
    }

    def __init__(self, *, clock=None, sleeper=None):
        self._clock = clock or time.time
        self._sleep = sleeper or asyncio.sleep
        self.rates: list[dict] = []
        self.connected = False
        self.last_update_at = 0.0
        self.last_error = "waiting for first Mycoins public rate update"

    @staticmethod
    def parse_signalr_message(raw: str) -> list[dict] | None:
        for part in str(raw).split("\x1e"):
            if not part.strip():
                continue
            try:
                event = json.loads(part)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict) or event.get("type") != 1:
                continue
            if event.get("target") not in {"UpdateCurrencyRate", "RateUpdate"}:
                continue
            args = event.get("arguments") or []
            for value in args:
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
                if isinstance(value, dict):
                    for key in ("data", "rates", "currencies"):
                        nested = value.get(key)
                        if isinstance(nested, list):
                            return [row for row in nested if isinstance(row, dict)]
        return None

    async def _connect_once(self, session: aiohttp.ClientSession) -> None:
        async with session.post(
            MYCOINS_NEGOTIATE_URL, headers=self._HEADERS
        ) as response:
            if response.status != 200:
                await response.text()
                raise maker.PublicFeedUnavailable(MYCOINS_NEGOTIATE_URL)
            negotiated = await response.json()
        token = str(negotiated.get("connectionToken") or "")
        if not token:
            raise ValueError("Mycoins SignalR negotiation returned no connection token")
        async with session.ws_connect(
            MYCOINS_WS_URL,
            params={"id": token},
            headers={
                "User-Agent": self._HEADERS["User-Agent"],
                "Referer": self._HEADERS["Referer"],
            },
            origin=self._HEADERS["Origin"],
            heartbeat=20,
        ) as websocket:
            await websocket.send_str('{"protocol":"json","version":1}\x1e')
            self.connected = True
            self.last_error = ""
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    rates = self.parse_signalr_message(message.data)
                    if rates is not None:
                        self.rates = rates
                        self.last_update_at = self._clock()
                elif message.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break

    async def manage_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=45)
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            while True:
                try:
                    await self._connect_once(session)
                    self.last_error = "Mycoins public rate stream disconnected"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {str(exc)[:140]}"
                finally:
                    self.connected = False
                await self._sleep(5.0)

    def state(self) -> dict:
        age = max(0.0, self._clock() - self.last_update_at) if self.last_update_at else None
        return {
            "connected": self.connected,
            "last_update_at": self.last_update_at,
            "age_sec": round(age, 1) if age is not None else None,
            "rate_count": len(self.rates),
            "error": self.last_error,
        }


class GeorgianVenueOpportunityScanner:
    """Screen public Georgian dealer quotes without simulating unproved fills."""

    def __init__(
        self,
        cryptal_data: maker.CryptalPublicDataHub,
        *,
        clock=None,
        sleeper=None,
        mycoins_feed: MycoinsRateFeed | None = None,
    ):
        self.cryptal_data = cryptal_data
        self._clock = clock or time.time
        self._sleep = sleeper or asyncio.sleep
        self.mycoins = mycoins_feed or MycoinsRateFeed(clock=self._clock)
        self.running = False
        self.scan_in_progress = False
        self.status = "waiting for first multi-venue fixed-quote scan"
        self.last_error = ""
        self.last_scan_at = 0.0
        self.next_scan_at = 0.0
        self.scanned_quote_count = 0
        self.rows: list[dict] = []
        self.opportunities: list[dict] = []
        self.source_health: dict[str, dict] = {}
        self._platforma_routes: list[dict] = []
        self._platforma_fetched_at = 0.0

    @staticmethod
    async def _json(
        session: aiohttp.ClientSession,
        url: str,
        *,
        method: str = "GET",
    ) -> Any:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (compatible; GeorgianVenuePaperScanner/1.0)",
        }
        for attempt in range(3):
            try:
                async with session.request(method, url, headers=headers) as response:
                    if response.status != 200:
                        await response.text()
                        raise maker.PublicFeedUnavailable(url)
                    return await response.json(content_type=None)
            except maker.PublicFeedUnavailable:
                if attempt == 2:
                    raise
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError):
                if attempt == 2:
                    raise maker.PublicFeedUnavailable(url)
            await asyncio.sleep(float(2 ** attempt))
        raise maker.PublicFeedUnavailable(url)

    async def _settlement_books(self, session: aiohttp.ClientSession) -> dict[str, dict]:
        result = {"USDT": {"bid": 1.0, "ask": 1.0, "pair": "USDT"}}
        for quote in ("USD", "GEL"):
            pair = f"USDT-{quote}"
            payload = await self.cryptal_data.get(
                session,
                f"{maker.CRYPTAL_BASE}/api/v1/public/orderbook/{pair}",
                {"limit": 25},
                cache_ttl_sec=1.0,
            )
            bids = maker._book_levels(payload.get("bids"), reverse=True)
            asks = maker._book_levels(payload.get("asks"), reverse=False)
            if not bids or not asks or bids[0][0] >= asks[0][0]:
                raise ValueError(f"{pair} has no executable conversion book")
            result[quote] = {"bid": bids[0][0], "ask": asks[0][0], "pair": pair}
        return result

    @staticmethod
    def parse_coinet(payload: Any, captured_at: float) -> list[dict]:
        source = payload.get("data") if isinstance(payload, dict) else payload
        rows = []
        for item in source if isinstance(source, list) else []:
            base = str(item.get("currency1") or "").upper()
            quote = str(item.get("currency2") or "").upper()
            bid, ask = _n(item.get("sellRate")), _n(item.get("buyRate"))
            if not base or quote not in {"USDT", "USD", "GEL"} or bid <= 0 or ask <= bid:
                continue
            rows.append({
                "venue": "Coinet",
                "registration_id": "0006-9404",
                "base": base,
                "quote": quote,
                "bid": bid,
                "ask": ask,
                "min_quote": _n(item.get("limitMin")),
                "max_quote": _n(item.get("limitMax")),
                "size_verified": True,
                "quote_kind": "account fixed quote",
                "workflow": "remote conversion after account/KYC",
                "captured_at": captured_at,
                "source": COINET_URL,
            })
        return rows

    @staticmethod
    def parse_mycoins(source: Any, captured_at: float) -> list[dict]:
        rows = []
        for item in source if isinstance(source, list) else []:
            base = str(
                item.get("currency1_abbr") or item.get("currency1") or ""
            ).upper()
            quote = str(
                item.get("currency2_abbr") or item.get("currency2") or ""
            ).upper()
            bid, ask = _n(item.get("sell_rate")), _n(item.get("buy_rate"))
            if not base or quote not in {"USDT", "USD", "GEL"} or bid <= 0 or ask <= bid:
                continue
            expires = _iso_epoch(item.get("expiration_utc"))
            rows.append({
                "venue": "Mycoins",
                "registration_id": "0001-9404",
                "base": base,
                "quote": quote,
                "bid": bid,
                "ask": ask,
                "min_quote": 0.0,
                "max_quote": 0.0,
                "size_verified": False,
                "quote_kind": "account fixed quote",
                "workflow": "remote conversion after account/KYC",
                "captured_at": captured_at,
                "expires_at": expires,
                "source": MYCOINS_WS_URL,
            })
        return rows

    @staticmethod
    def parse_platforma(payload: Any, captured_at: float) -> list[dict]:
        source = payload.get("routes") if isinstance(payload, dict) else payload
        rows = []
        for item in source if isinstance(source, list) else []:
            if not isinstance(item, dict) or not item.get("isShowWeb", True):
                continue
            source_currency = item.get("from") or {}
            target_currency = item.get("to") or {}
            source_symbol = str(source_currency.get("symbol") or "").upper()
            target_symbol = str(target_currency.get("symbol") or "").upper()
            rate = item.get("rate") or {}
            rate_in, rate_out = _n(rate.get("in")), _n(rate.get("out"))
            if rate_in <= 0 or rate_out <= 0:
                continue
            if source_symbol in {"USD", "GEL"} and target_symbol == "USDT":
                side, base, quote = "local_buy", "USDT", source_symbol
            elif source_symbol == "USDT" and target_symbol in {"USD", "GEL"}:
                side, base, quote = "local_sell", "USDT", target_symbol
            elif source_symbol in {"USDT", "USD", "GEL"} and target_symbol not in {
                "USDT", "USD", "GEL"
            }:
                side, base, quote = "local_buy", target_symbol, source_symbol
            elif target_symbol in {"USDT", "USD", "GEL"} and source_symbol not in {
                "USDT", "USD", "GEL"
            }:
                side, base, quote = "local_sell", source_symbol, target_symbol
            else:
                continue
            rows.append({
                "venue": "PlatformaEX / PLEX",
                "registration_id": "0017-9404",
                "base": base,
                "quote": quote,
                "side": side,
                "rate_in": rate_in,
                "rate_out": rate_out,
                "output_fee": _n(rate.get("outFeeAmount")),
                "min_input": _n(source_currency.get("min")),
                "max_input": _n(source_currency.get("max")),
                "size_verified": True,
                "quote_kind": "directed fixed route",
                "workflow": str(item.get("payType") or "manual") + " route",
                "manual": str(item.get("payType") or "").lower() == "manual",
                "route_id": str(item.get("routeId") or ""),
                "route": (
                    f"{source_currency.get('name') or source_symbol} -> "
                    f"{target_currency.get('name') or target_symbol}"
                ),
                "captured_at": captured_at,
                "max_quote_age_sec": PLATFORMA_REFRESH_SEC,
                "source": PLATFORMA_URL,
            })
        return rows

    @staticmethod
    def _book_maps(rows: Any) -> dict[str, dict]:
        result = {}
        for item in rows if isinstance(rows, list) else []:
            symbol = str(item.get("symbol") or "")
            bid, ask = _n(item.get("bidPrice")), _n(item.get("askPrice"))
            if symbol.endswith("USDT") and bid > 0 and ask > bid:
                result[symbol] = {"bid": bid, "ask": ask}
        return result

    @staticmethod
    def _cost_bps(hedge_basis_bps: float) -> float:
        return (
            BINANCE_SPOT_TAKER_BPS
            + 2 * maker.BINANCE_TAKER_FEE_RATE * 1e4
            + 2 * maker.HEDGE_SLIPPAGE_BPS
            + max(maker.FUNDING_AND_BASIS_RESERVE_BPS, hedge_basis_bps)
            + OPERATIONS_RESERVE_BPS
            + TRANSFER_RESERVE_USD / QUOTE_NOTIONAL_USD * 1e4
        )

    @staticmethod
    def _stable_cost_bps() -> float:
        return (
            maker.CRYPTAL_MAKER_FEE_RATE * 1e4
            + OPERATIONS_RESERVE_BPS
            + TRANSFER_RESERVE_USD / QUOTE_NOTIONAL_USD * 1e4
        )

    def _common_result(
        self,
        row: dict,
        *,
        direction: str,
        local_price: float,
        external_price: float,
        gross_edge_bps: float,
        hedge_basis_bps: float,
        size_ok: bool,
        detail: str = "",
        modeled_cost_bps: float | None = None,
    ) -> dict:
        modeled_cost = (
            self._cost_bps(hedge_basis_bps)
            if modeled_cost_bps is None
            else max(0.0, float(modeled_cost_bps))
        )
        net_edge = gross_edge_bps - modeled_cost
        quote_age = max(0.0, self._clock() - _n(row.get("captured_at")))
        expires_at = _n(row.get("expires_at"))
        max_quote_age = _n(row.get("max_quote_age_sec")) or MAX_FIXED_QUOTE_AGE_SEC
        fresh = (
            quote_age <= max_quote_age
            and (expires_at <= 0 or expires_at > self._clock())
        )
        manual = bool(row.get("manual"))
        candidate = (
            fresh
            and size_ok
            and not manual
            and hedge_basis_bps <= MAX_HEDGE_BASIS_BPS
            and net_edge >= MIN_SCREENED_EDGE_BPS
        )
        blockers = []
        if not fresh:
            blockers.append("stale public quote")
        if not size_ok:
            blockers.append("$100 size not verified inside published limits")
        if manual:
            blockers.append("manual/cash workflow")
        if hedge_basis_bps > MAX_HEDGE_BASIS_BPS:
            blockers.append("spot/perpetual basis too wide")
        if net_edge < MIN_SCREENED_EDGE_BPS:
            blockers.append("edge does not clear all modeled costs")
        if candidate:
            blockers.append("screen only: acceptance, withdrawal and network fee need confirmation")
        return {
            **row,
            "pair": f"{row['base']}/{row['quote']}",
            "direction": direction,
            "local_price": local_price,
            "external_spot_price": external_price,
            "gross_edge_bps": round(gross_edge_bps, 1),
            "modeled_cost_bps": round(modeled_cost, 1),
            "net_edge_bps": round(net_edge, 1),
            "hedge_basis_bps": round(hedge_basis_bps, 1),
            "quote_age_sec": round(quote_age, 1),
            "screen_candidate": candidate,
            "qualified": candidate,
            "executable": False,
            "paper_fill_allowed": False,
            "live_trading_enabled": False,
            "reason": "; ".join(blockers),
            "detail": detail,
        }

    def _evaluate_book(
        self,
        row: dict,
        settlement: dict[str, dict],
        spot: dict[str, dict],
        futures: dict[str, dict],
    ) -> list[dict]:
        conversion = settlement.get(row["quote"])
        if not conversion:
            return []
        if row["base"] == "USDT":
            quote_notional = QUOTE_NOTIONAL_USD * conversion["ask"]
            minimum, maximum = _n(row.get("min_quote")), _n(row.get("max_quote"))
            size_ok = bool(row.get("size_verified"))
            size_ok = size_ok and (minimum <= 0 or quote_notional >= minimum)
            size_ok = size_ok and (maximum <= 0 or quote_notional <= maximum)
            stable_cost = self._stable_cost_bps()
            buy_gross = (conversion["bid"] - _n(row["ask"])) / _n(row["ask"]) * 1e4
            sell_gross = (_n(row["bid"]) - conversion["ask"]) / conversion["ask"] * 1e4
            results = []
            for direction, local_price, external_price, gross in (
                ("buy USDT locally / sell on Cryptal", _n(row["ask"]), conversion["bid"], buy_gross),
                ("buy USDT on Cryptal / sell locally", _n(row["bid"]), conversion["ask"], sell_gross),
            ):
                result = self._common_result(
                    row,
                    direction=direction,
                    local_price=local_price,
                    external_price=external_price,
                    gross_edge_bps=gross,
                    hedge_basis_bps=0.0,
                    size_ok=size_ok,
                    detail="direct stablecoin transfer; no price hedge required",
                    modeled_cost_bps=stable_cost,
                )
                results.append(result)
            return results
        symbol = f"{row['base']}USDT"
        if symbol not in spot or symbol not in futures:
            return []
        spot_book, hedge_book = spot[symbol], futures[symbol]
        spot_mid = (spot_book["bid"] + spot_book["ask"]) / 2.0
        hedge_mid = (hedge_book["bid"] + hedge_book["ask"]) / 2.0
        basis = abs(hedge_mid - spot_mid) / spot_mid * 1e4
        quote_notional = QUOTE_NOTIONAL_USD * conversion["ask"]
        min_quote, max_quote = _n(row.get("min_quote")), _n(row.get("max_quote"))
        size_ok = bool(row.get("size_verified"))
        size_ok = size_ok and (min_quote <= 0 or quote_notional >= min_quote)
        size_ok = size_ok and (max_quote <= 0 or quote_notional <= max_quote)

        external_sell = spot_book["bid"] * conversion["bid"]
        local_ask = _n(row["ask"])
        buy_edge = (external_sell - local_ask) / local_ask * 1e4
        external_buy = spot_book["ask"] * conversion["ask"]
        local_bid = _n(row["bid"])
        sell_edge = (local_bid - external_buy) / external_buy * 1e4
        return [
            self._common_result(
                row,
                direction="buy locally / sell Binance spot / temporary short hedge",
                local_price=local_ask,
                external_price=external_sell,
                gross_edge_bps=buy_edge,
                hedge_basis_bps=basis,
                size_ok=size_ok,
            ),
            self._common_result(
                row,
                direction="buy Binance spot / sell locally / temporary short hedge",
                local_price=local_bid,
                external_price=external_buy,
                gross_edge_bps=sell_edge,
                hedge_basis_bps=basis,
                size_ok=size_ok,
            ),
        ]

    def _evaluate_route(
        self,
        row: dict,
        settlement: dict[str, dict],
        spot: dict[str, dict],
        futures: dict[str, dict],
    ) -> list[dict]:
        conversion = settlement.get(row["quote"])
        if not conversion:
            return []
        if row["base"] == "USDT":
            rate_in, rate_out = _n(row["rate_in"]), _n(row["rate_out"])
            fee = _n(row.get("output_fee"))
            if row["side"] == "local_buy":
                input_amount = QUOTE_NOTIONAL_USD * conversion["ask"]
                output = input_amount / rate_in * rate_out - fee
                if output <= 0:
                    return []
                local_price = input_amount / output
                external_price = conversion["bid"]
                gross = (external_price - local_price) / local_price * 1e4
                direction = "buy USDT on PlatformaEX / sell on Cryptal"
            else:
                input_amount = QUOTE_NOTIONAL_USD
                output = input_amount / rate_in * rate_out - fee
                if output <= 0:
                    return []
                local_price = output / input_amount
                external_price = conversion["ask"]
                gross = (local_price - external_price) / external_price * 1e4
                direction = "buy USDT on Cryptal / sell on PlatformaEX"
            minimum, maximum = _n(row.get("min_input")), _n(row.get("max_input"))
            size_ok = (minimum <= 0 or input_amount >= minimum)
            size_ok = size_ok and (maximum <= 0 or input_amount <= maximum)
            result = self._common_result(
                row,
                direction=direction,
                local_price=local_price,
                external_price=external_price,
                gross_edge_bps=gross,
                hedge_basis_bps=0.0,
                size_ok=size_ok,
                detail=f"published output fee {fee:g} {row['base'] if row['side'] == 'local_buy' else row['quote']}",
                modeled_cost_bps=self._stable_cost_bps(),
            )
            return [result]
        symbol = f"{row['base']}USDT"
        if symbol not in spot or symbol not in futures:
            return []
        spot_book, hedge_book = spot[symbol], futures[symbol]
        spot_mid = (spot_book["bid"] + spot_book["ask"]) / 2.0
        hedge_mid = (hedge_book["bid"] + hedge_book["ask"]) / 2.0
        basis = abs(hedge_mid - spot_mid) / spot_mid * 1e4
        rate_in, rate_out = _n(row["rate_in"]), _n(row["rate_out"])
        output_fee = _n(row.get("output_fee"))
        side = row["side"]
        if side == "local_buy":
            input_amount = QUOTE_NOTIONAL_USD * conversion["ask"]
            output_base = input_amount / rate_in * rate_out - output_fee
            if output_base <= 0:
                return []
            local_price = input_amount / output_base
            external_price = spot_book["bid"] * conversion["bid"]
            gross = (external_price - local_price) / local_price * 1e4
            input_for_limit = input_amount
            direction = "buy on PlatformaEX / sell Binance spot / temporary short hedge"
        else:
            input_amount = QUOTE_NOTIONAL_USD / spot_book["ask"]
            output_quote = input_amount / rate_in * rate_out - output_fee
            if output_quote <= 0:
                return []
            local_price = output_quote / input_amount
            external_price = spot_book["ask"] * conversion["ask"]
            gross = (local_price - external_price) / external_price * 1e4
            input_for_limit = input_amount
            direction = "buy Binance spot / sell on PlatformaEX / temporary short hedge"
        minimum, maximum = _n(row.get("min_input")), _n(row.get("max_input"))
        size_ok = (minimum <= 0 or input_for_limit >= minimum)
        size_ok = size_ok and (maximum <= 0 or input_for_limit <= maximum)
        return [self._common_result(
            row,
            direction=direction,
            local_price=local_price,
            external_price=external_price,
            gross_edge_bps=gross,
            hedge_basis_bps=basis,
            size_ok=size_ok,
            detail=f"published output fee {output_fee:g} {row['base'] if side == 'local_buy' else row['quote']}",
        )]

    async def scan_once(self, session: aiohttp.ClientSession) -> None:
        if self.scan_in_progress:
            return
        self.scan_in_progress = True
        self.status = "scanning public Georgian fixed quotes against Binance"
        self.last_error = ""
        try:
            captured = self._clock()
            coinet_task = self._json(session, COINET_URL)
            spot_task = self._json(session, BINANCE_SPOT_BOOK_URL)
            futures_task = self._json(session, BINANCE_FUTURES_BOOK_URL)
            settlement_task = self._settlement_books(session)
            coinet_payload, spot_payload, futures_payload, settlement = await asyncio.gather(
                coinet_task, spot_task, futures_task, settlement_task
            )
            self.source_health["Coinet"] = {"ok": True, "at": captured, "error": ""}

            if (
                not self._platforma_routes
                or captured - self._platforma_fetched_at >= PLATFORMA_REFRESH_SEC
            ):
                try:
                    platforma_payload = await self._json(session, PLATFORMA_URL)
                    self._platforma_routes = self.parse_platforma(
                        platforma_payload, captured
                    )
                    self._platforma_fetched_at = captured
                    self.source_health["PlatformaEX / PLEX"] = {
                        "ok": True, "at": captured, "error": ""
                    }
                except Exception as exc:
                    self.source_health["PlatformaEX / PLEX"] = {
                        "ok": bool(self._platforma_routes),
                        "at": self._platforma_fetched_at,
                        "error": f"{type(exc).__name__}: {str(exc)[:100]}",
                    }

            coinet_rows = self.parse_coinet(coinet_payload, captured)
            mycoins_rows = self.parse_mycoins(
                list(self.mycoins.rates), self.mycoins.last_update_at
            )
            mycoins_state = self.mycoins.state()
            self.source_health["Mycoins"] = {
                "ok": bool(mycoins_rows) and (
                    mycoins_state.get("age_sec") is not None
                    and mycoins_state["age_sec"] <= MAX_FIXED_QUOTE_AGE_SEC
                ),
                "at": self.mycoins.last_update_at,
                "error": mycoins_state.get("error", ""),
            }

            spot = self._book_maps(spot_payload)
            futures = self._book_maps(futures_payload)
            evaluated: list[dict] = []
            for row in coinet_rows + mycoins_rows:
                evaluated.extend(self._evaluate_book(row, settlement, spot, futures))
            for row in self._platforma_routes:
                evaluated.extend(self._evaluate_route(row, settlement, spot, futures))
            evaluated.sort(key=lambda item: item["net_edge_bps"], reverse=True)
            self.rows = evaluated[:MAX_DISPLAY_ROWS]
            self.opportunities = [
                row for row in evaluated if row.get("screen_candidate")
            ][:25]
            self.scanned_quote_count = len(evaluated)
            self.last_scan_at = self._clock()
            self.next_scan_at = self.last_scan_at + SCAN_INTERVAL_SEC
            self.status = (
                f"screened {self.scanned_quote_count} fixed-quote directions across "
                f"Coinet, Mycoins and PlatformaEX; {len(self.opportunities)} clear "
                "the cost screen but still require execution verification"
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            self.status = "multi-venue quote scan retaining its last complete snapshot"
            self.next_scan_at = self._clock() + 15.0
            raise
        finally:
            self.scan_in_progress = False

    async def manage_loop(self) -> None:
        self.running = True
        mycoins_task = asyncio.create_task(self.mycoins.manage_loop())
        timeout = aiohttp.ClientTimeout(total=maker.REQUEST_TIMEOUT_SEC)
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                while True:
                    delay = self.next_scan_at - self._clock()
                    if delay > 0:
                        await self._sleep(delay)
                        continue
                    try:
                        await self.scan_once(session)
                    except Exception:
                        await self._sleep(15.0)
        finally:
            mycoins_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mycoins_task

    def state(self) -> dict:
        audit = _capability_rows()
        return {
            "running": self.running,
            "scan_in_progress": self.scan_in_progress,
            "status": self.status,
            "last_error": self.last_error,
            "last_scan_at": self.last_scan_at,
            "next_scan_at": self.next_scan_at,
            "scan_interval_sec": SCAN_INTERVAL_SEC,
            "paper_notional_usd": QUOTE_NOTIONAL_USD,
            "scanned_quote_count": self.scanned_quote_count,
            "opportunity_count": len(self.opportunities),
            "opportunities": self.opportunities,
            "rows": self.rows,
            "source_health": self.source_health,
            "mycoins_stream": self.mycoins.state(),
            "registered_vasp_count": len(REGISTERED_VASPS),
            "machine_readable_local_count": 4,
            "same_as_cryptal_count": 1,
            "registry_as_of": NBG_REGISTER_AS_OF,
            "registry_url": NBG_REGISTER_URL,
            "venue_audit": audit,
            "method": (
                "Coinet and Mycoins dealer quotes and PlatformaEX directed routes are "
                "compared with executable Binance spot books and a Binance perpetual "
                "hedge. The screen charges spot/perpetual fees, hedge slippage, current "
                "basis, a $2.50 transfer reserve and an operations reserve. Platforma "
                "output fees are applied at the $100 test size. No quote creates a fill."
            ),
            "limitations": (
                "Only Cryptal exposed a verified local public order book and trade tape. "
                "Fixed quotes may be rejected, expire, require KYC or manual cash service, "
                "and actual withdrawal/network fees must be confirmed before funding."
            ),
            "evidentiary": False,
            "paper_only": True,
            "live_trading_enabled": False,
        }
