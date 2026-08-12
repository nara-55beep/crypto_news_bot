"""All-market Cryptal opportunity scanner and best-candidate paper collector.

The Georgian side is always Cryptal. Binance is used only as the external fair-value
and delta-hedge reference. The scanner evaluates every active Cryptal pair whose base
asset has a live Binance USDT perpetual; the collector follows one best candidate at
a time so one $100 paper bankroll is not falsely presented as sixty funded accounts.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any

import aiohttp

import config
import cryptal_maker_paper as maker


NAME = "Cryptal Georgian All-Market Maker + Binance Hedge"
SCAN_INTERVAL_SEC = 5 * 60
MAX_TRADE_AGE_SEC = 15 * 60
MAX_DISPLAY_MARKETS = 60
SWITCH_IMPROVEMENT_BPS = 25.0
SUPERVISOR_STATE_PATH = os.path.join(
    config.DATA_DIR, "cryptal_georgian_best_market_supervisor.json"
)


def _n(value: Any) -> float:
    return maker._number(value)


def _trade_time(value: Any) -> float:
    stamp = _n(value)
    return stamp / 1000.0 if stamp > 100_000_000_000 else stamp


def _levels(rows: Any, reverse: bool = False) -> list[tuple[float, float]]:
    return maker._book_levels(rows, reverse=reverse)


def _filter(symbol: dict, name: str) -> dict:
    for row in symbol.get("filters") or []:
        if row.get("filterType") == name:
            return row
    return {}


class CryptalGeorgianMarketScanner:
    def __init__(
        self,
        data_hub: maker.CryptalPublicDataHub,
        *,
        clock=None,
        sleeper=None,
    ):
        self.data_hub = data_hub
        self._clock = clock or time.time
        self._sleep = sleeper or asyncio.sleep
        self.running = False
        self.scan_in_progress = False
        self.status = "waiting for first all-market scan"
        self.data_error = ""
        self.last_scan_at = 0.0
        self.next_scan_at = 0.0
        self.catalog_count = 0
        self.eligible_count = 0
        self.scanned_count = 0
        self.opportunities: list[dict] = []
        self.markets: list[dict] = []
        self.excluded: list[dict] = []

    @staticmethod
    async def _json(session: aiohttp.ClientSession, url: str,
                    params: dict | None = None) -> Any:
        for attempt in range(3):
            try:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        await response.text()
                        raise maker.PublicFeedUnavailable(url)
                    return await response.json()
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                if attempt == 2:
                    raise maker.PublicFeedUnavailable(url)
                await asyncio.sleep(float(2 ** attempt))
        raise maker.PublicFeedUnavailable(url)

    @staticmethod
    def _conversion_pair(quote: str) -> str | None:
        return {
            "USD": "USDT-USD",
            "GEL": "USDT-GEL",
            "EUR": "USDT-EUR",
        }.get(quote)

    @staticmethod
    def _quote_per_usdt(quote: str, conversions: dict[str, float],
                        hedge_mids: dict[str, float]) -> float:
        if quote == "BTC":
            btc = hedge_mids.get("BTCUSDT", 0.0)
            return 1.0 / btc if btc > 0 else 0.0
        return conversions.get(quote, 0.0)

    async def _conversion_books(self, session: aiohttp.ClientSession) -> dict[str, float]:
        result: dict[str, float] = {}
        for quote in ("USD", "GEL", "EUR"):
            pair = self._conversion_pair(quote)
            url = f"{maker.CRYPTAL_BASE}/api/v1/public/orderbook/{pair}"
            book = await self.data_hub.get(
                session, url, {"limit": 25}, cache_ttl_sec=1.0
            )
            bids = _levels(book.get("bids"), reverse=True)
            asks = _levels(book.get("asks"))
            if not bids or not asks or bids[0][0] >= asks[0][0]:
                raise ValueError(f"{pair} conversion book is not executable")
            result[quote] = (bids[0][0] + asks[0][0]) / 2.0
        return result

    async def scan_once(self, session: aiohttp.ClientSession) -> None:
        # Manual refresh and the scheduled loop can meet on the same event-loop
        # turn. Coalesce them rather than doubling a universe scan.
        if self.scan_in_progress:
            return
        self.scan_in_progress = True
        self.data_error = ""
        self.status = "scanning every hedgeable Cryptal market"
        try:
            pairs_url = f"{maker.CRYPTAL_BASE}/api/v1/public/pairs"
            pairs = await self.data_hub.get(
                session, pairs_url, cache_ttl_sec=60 * 60
            )
            tickers_url = f"{maker.CRYPTAL_BASE}/api/v1/public/ticker"
            ticker_rows = await self.data_hub.get(
                session, tickers_url, cache_ttl_sec=30.0
            )
            tickers = {
                str(row.get("pair") or ""): row
                for row in ticker_rows if isinstance(row, dict)
            } if isinstance(ticker_rows, list) else {}
            exchange_info, hedge_books = await asyncio.gather(
                self._json(session, "https://fapi.binance.com/fapi/v1/exchangeInfo"),
                self._json(session, "https://fapi.binance.com/fapi/v1/ticker/bookTicker"),
            )
            self.catalog_count = len(pairs) if isinstance(pairs, list) else 0
            hedge_symbols: dict[str, dict] = {}
            for symbol in exchange_info.get("symbols") or []:
                if (symbol.get("contractType") == "PERPETUAL"
                        and symbol.get("quoteAsset") == "USDT"
                        and symbol.get("status") == "TRADING"):
                    hedge_symbols[str(symbol.get("baseAsset") or "").upper()] = symbol
            hedge_mids: dict[str, float] = {}
            for row in hedge_books or []:
                bid, ask = _n(row.get("bidPrice")), _n(row.get("askPrice"))
                if bid > 0 and ask > bid:
                    hedge_mids[str(row.get("symbol") or "")] = (bid + ask) / 2.0
            conversions = await self._conversion_books(session)
            settlement_mid = conversions["USD"]

            eligible: list[tuple[dict, dict]] = []
            excluded: list[dict] = []
            for pair in pairs if isinstance(pairs, list) else []:
                base = str(pair.get("baseCurrency") or "").upper()
                quote = str(pair.get("quoteCurrency") or "").upper()
                hedge = hedge_symbols.get(base)
                reason = ""
                if not pair.get("tradeEnabled"):
                    reason = "Cryptal trading disabled"
                elif quote not in {"USD", "GEL", "EUR", "BTC"}:
                    reason = "quote currency has no executable conversion mapping"
                elif not hedge or hedge.get("symbol") not in hedge_mids:
                    reason = "no live Binance USDT perpetual hedge"
                if reason:
                    excluded.append({"pair": pair.get("pair"), "reason": reason})
                else:
                    eligible.append((pair, hedge))
            self.eligible_count = len(eligible)

            rows: list[dict] = []
            errors: list[dict] = []
            for pair_meta, hedge_meta in eligible:
                pair = str(pair_meta["pair"])
                try:
                    ticker = tickers.get(pair) or {}
                    bid = _n(ticker.get("bidPrice"))
                    ask = _n(ticker.get("askPrice"))
                    if bid <= 0 or ask <= bid:
                        raise ValueError("empty, one-sided, locked or crossed book")

                    base = str(pair_meta["baseCurrency"]).upper()
                    quote = str(pair_meta["quoteCurrency"]).upper()
                    hedge_symbol = str(hedge_meta["symbol"])
                    hedge_mid = hedge_mids[hedge_symbol]
                    quote_per_usdt = self._quote_per_usdt(
                        quote, conversions, hedge_mids
                    )
                    if quote_per_usdt <= 0:
                        raise ValueError("missing quote conversion")
                    fair = hedge_mid * quote_per_usdt
                    mid = (bid + ask) / 2.0
                    spread_bps = (ask - bid) / mid * 1e4
                    bid_discount = (fair - bid) / fair * 1e4
                    ask_premium = (ask - fair) / fair * 1e4
                    maker_fee_rate = _n(pair_meta.get("makerFee"))
                    round_trip_cost_bps = (
                        2 * maker_fee_rate * 1e4
                        + 2 * maker.BINANCE_TAKER_FEE_RATE * 1e4
                        + 2 * maker.HEDGE_SLIPPAGE_BPS
                        + maker.FUNDING_AND_BASIS_RESERVE_BPS
                    )
                    projected_net = spread_bps - round_trip_cost_bps
                    conservative_net = (
                        projected_net - 2 * maker.ADVERSE_SELECTION_RESERVE_BPS
                    )

                    cryptal_step = 10 ** -int(pair_meta.get("baseScale") or 0)
                    price_tick = 10 ** -int(pair_meta.get("quoteScale") or 0)
                    lot_filter = _filter(hedge_meta, "LOT_SIZE")
                    market_filter = _filter(hedge_meta, "MARKET_LOT_SIZE")
                    hedge_step = max(
                        _n(lot_filter.get("stepSize")),
                        _n(market_filter.get("stepSize")),
                    )
                    qty_step = max(cryptal_step, hedge_step)
                    notional_filter = _filter(hedge_meta, "MIN_NOTIONAL")
                    hedge_min_notional = _n(notional_filter.get("notional"))
                    min_cost_quote = _n(pair_meta.get("minCost"))
                    min_cost_usd = (
                        min_cost_quote / quote_per_usdt * settlement_mid
                    )

                    # The bulk ticker screens every catalog market with one public
                    # call. Per-pair tape is fetched only for a market that could
                    # pass all non-time conditions; it supplies the timestamped,
                    # uniquely identified prints the ticker deliberately lacks.
                    static_candidate = (
                        int(_n(ticker.get("tradeCount"))) >= 2
                        and bid_discount >= maker.CryptalMakerPaperBot._required_half_spread_bps(
                            maker_fee_rate
                        )
                        and ask_premium > 0
                        and conservative_net >= maker.MIN_PROJECTED_NET_BPS
                        and min_cost_usd <= maker.ORDER_NOTIONAL_USD
                        and hedge_min_notional <= maker.ORDER_NOTIONAL_USD
                    )
                    trades: list[dict] = []
                    if static_candidate:
                        trades_url = (
                            f"{maker.CRYPTAL_BASE}/api/v1/public/trades/{pair}"
                        )
                        payload = await self.data_hub.get(
                            session, trades_url, {"limit": 25}, cache_ttl_sec=2.0
                        )
                        trades = payload if isinstance(payload, list) else []
                    stamps = [
                        _trade_time(row.get("timestamp"))
                        for row in trades
                        if isinstance(row, dict) and _trade_time(row.get("timestamp")) > 0
                    ]
                    newest = max(stamps) if stamps else 0.0
                    age = max(0.0, time.time() - newest) if newest else math.inf
                    unique_trades = len({
                        str(row.get("id")) for row in trades
                        if isinstance(row, dict) and row.get("id") is not None
                    })
                    tape_span = max(stamps) - min(stamps) if len(stamps) > 1 else 0.0

                    qualified = (
                        static_candidate
                        and age <= MAX_TRADE_AGE_SEC
                        and unique_trades >= 2
                    )
                    score = conservative_net - min(age / 60.0, 60.0)
                    rows.append({
                        "pair": pair,
                        "display_pair": pair_meta.get("pairDisplayName") or pair,
                        "base_asset": base,
                        "quote_currency": quote,
                        "hedge_symbol": hedge_symbol,
                        "stable_pair": self._conversion_pair(quote),
                        "maker_fee_bps": maker_fee_rate * 1e4,
                        "maker_fee_rate": maker_fee_rate,
                        "minimum_cost_quote": min_cost_quote,
                        "minimum_cost_usd": round(min_cost_usd, 4),
                        "price_tick": price_tick,
                        "quantity_step": qty_step,
                        "cryptal_bid": bid,
                        "cryptal_ask": ask,
                        "fair_quote": fair,
                        "spread_bps": round(spread_bps, 1),
                        "bid_discount_bps": round(bid_discount, 1),
                        "ask_premium_bps": round(ask_premium, 1),
                        "projected_net_bps": round(projected_net, 1),
                        "conservative_net_bps": round(conservative_net, 1),
                        "last_trade_at": newest,
                        "last_trade_age_sec": round(age, 1) if math.isfinite(age) else None,
                        "unique_trades": unique_trades,
                        "tape_span_hours": round(tape_span / 3600.0, 2),
                        "qualified": qualified,
                        "screen_score": round(score, 1),
                        "paper_only": True,
                        "screen_book_source": "Cryptal bulk public ticker",
                        "tape_checked": static_candidate,
                    })
                except maker.CryptalRateLimitError:
                    raise
                except Exception as exc:
                    errors.append({"pair": pair, "reason": str(exc)[:120]})

            rows.sort(key=lambda row: row["screen_score"], reverse=True)
            self.markets = rows[:MAX_DISPLAY_MARKETS]
            self.opportunities = [row for row in rows if row["qualified"]]
            self.scanned_count = len(rows)
            self.excluded = (excluded + errors)[:80]
            self.last_scan_at = self._clock()
            # A full rate-safe scan can itself take several minutes while the
            # active collectors share the gateway. Start the interval after
            # completion; measuring from start made slow scans run back-to-back
            # forever and consumed the entire public-data budget.
            self.next_scan_at = self.last_scan_at + SCAN_INTERVAL_SEC
            self.data_error = ""
            self.status = (
                f"scanned {self.scanned_count}/{self.eligible_count} hedgeable "
                f"Cryptal markets; {len(self.opportunities)} paper candidates"
            )
        except maker.CryptalRateLimitError as exc:
            self.data_error = ""
            self.status = (
                "Cryptal public gateway cooling down; keeping the last complete "
                f"ranking and retrying in {max(1, math.ceil(exc.retry_after))}s"
            )
            raise
        except maker.PublicFeedUnavailable:
            self.data_error = ""
            self.status = (
                "public market feeds reconnecting after bounded retries; keeping "
                "the last complete ranking"
            )
            raise
        except Exception as exc:
            self.data_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            self.status = "all-market scan paused until public data recovers"
            raise
        finally:
            self.scan_in_progress = False

    async def _wait_until_due(self) -> None:
        while True:
            # Check the authoritative due time *before* every scheduled scan.
            # A manual scan may finish while this loop sleeps and move the due
            # time forward; re-checking prevents an immediate duplicate scan.
            delay = self.next_scan_at - self._clock()
            if delay > 0:
                await self._sleep(delay)
                continue
            if self.scan_in_progress:
                await self._sleep(1.0)
                continue
            return

    async def manage_loop(self) -> None:
        self.running = True
        timeout = aiohttp.ClientTimeout(total=maker.REQUEST_TIMEOUT_SEC)
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            while True:
                await self._wait_until_due()
                try:
                    await self.scan_once(session)
                except Exception:
                    self.next_scan_at = self._clock() + max(
                        15.0, self.data_hub.retry_delay()
                    )

    def best_executable(self, *, exclude: set[str] | None = None) -> dict | None:
        excluded = exclude or set()
        for row in self.opportunities:
            if row["pair"] not in excluded and row.get("stable_pair"):
                return dict(row)
        return None

    def state(self) -> dict:
        return {
            "running": self.running,
            "scan_in_progress": self.scan_in_progress,
            "status": self.status,
            "data_error": self.data_error,
            "last_scan_at": self.last_scan_at,
            "next_scan_at": self.next_scan_at,
            "scan_interval_sec": SCAN_INTERVAL_SEC,
            "catalog_count": self.catalog_count,
            "eligible_count": self.eligible_count,
            "scanned_count": self.scanned_count,
            "opportunity_count": len(self.opportunities),
            "opportunities": self.opportunities[:25],
            "markets": self.markets,
            "excluded": self.excluded,
            "public_data_health": self.data_hub.state(),
            "method": (
                "The bulk ticker screens every active Cryptal market with a live "
                "Binance USDT perpetual; only cost-qualified survivors request a "
                "timestamped trade tape for freshness, then pay both fee legs, "
                "hedge slippage, funding/basis and adverse-selection reserves."
            ),
            "evidentiary": False,
        }


class CryptalBestGeorgianMarketPaperBot:
    """Continuously paper-collect the scanner's best non-BTC candidate."""

    def __init__(self, scanner: CryptalGeorgianMarketScanner,
                 data_hub: maker.CryptalPublicDataHub):
        self.scanner = scanner
        self.data_hub = data_hub
        self.enabled = True
        self.active_pair = ""
        self.active_meta: dict = {}
        self.bot: maker.CryptalMakerPaperBot | None = None
        self.status = "waiting for the first all-market ranking"
        self.data_error = ""
        self.switches: list[dict] = []
        self._load_supervisor()

    @staticmethod
    def _pair_state_path(pair: str) -> str:
        safe = pair.lower().replace("/", "-")
        return os.path.join(config.DATA_DIR, f"cryptal_georgian_{safe}_state.json")

    def _load_supervisor(self) -> None:
        try:
            with open(SUPERVISOR_STATE_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.enabled = bool(data.get("enabled", True))
                self.active_pair = str(data.get("active_pair") or "")
                self.switches = [
                    row for row in data.get("switches", []) if isinstance(row, dict)
                ][:50]
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.data_error = f"supervisor state load failed: {type(exc).__name__}"

    def _save_supervisor(self) -> None:
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = SUPERVISOR_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({
                    "enabled": self.enabled,
                    "active_pair": self.active_pair,
                    "switches": self.switches[:50],
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, SUPERVISOR_STATE_PATH)
        except Exception as exc:
            self.data_error = f"supervisor state save failed: {type(exc).__name__}"

    def _activate(self, meta: dict) -> None:
        pair = str(meta["pair"])
        self.active_pair = pair
        self.active_meta = dict(meta)
        self.bot = maker.CryptalMakerPaperBot(
            state_path=self._pair_state_path(pair),
            pair=pair,
            stable_pair=str(meta["stable_pair"]),
            display_pair=str(meta["display_pair"]),
            base_asset=str(meta["base_asset"]),
            quote_currency=str(meta["quote_currency"]),
            hedge_symbol=str(meta["hedge_symbol"]),
            price_tick=_n(meta["price_tick"]),
            quantity_step=_n(meta["quantity_step"]),
            maker_fee_rate=_n(meta["maker_fee_rate"]),
            minimum_cost_quote=_n(meta["minimum_cost_quote"]),
            data_hub=self.data_hub,
        )
        self.bot.enabled = self.enabled
        self.switches.insert(0, {
            "at": time.time(),
            "pair": pair,
            "screen_score": meta.get("screen_score"),
            "conservative_net_bps": meta.get("conservative_net_bps"),
        })
        self.switches = self.switches[:50]
        self._save_supervisor()

    @staticmethod
    def _execution_signature(meta: dict) -> tuple:
        keys = (
            "stable_pair", "hedge_symbol", "price_tick", "quantity_step",
            "maker_fee_rate", "minimum_cost_quote",
        )
        return tuple(meta.get(key) for key in keys)

    def _choose(self) -> None:
        excluded = {maker.CRYPTAL_PAIR, maker.CRYPTAL_GEL_PAIR}
        candidate = self.scanner.best_executable(exclude=excluded)
        if not candidate:
            return
        if self.bot is None:
            # Resume only if the persisted pair still clears the current screen.
            # Merely appearing in the catalog is not permission to place a quote.
            if self.active_pair:
                persisted = next(
                    (row for row in self.scanner.opportunities
                     if row.get("pair") == self.active_pair),
                    None,
                )
                if persisted and persisted.get("stable_pair"):
                    self._activate(persisted)
                    return
            self._activate(candidate)
            return
        if self.bot.spot_qty > 0 or self.bot.short_qty > 0:
            return
        current = next(
            (row for row in self.scanner.opportunities
             if row.get("pair") == self.active_pair),
            None,
        )
        if current and self._execution_signature(current) != self._execution_signature(
            self.active_meta
        ):
            # Fee or lot/tick metadata changed. Reload the same flat ledger with
            # the new executable rules before it is allowed to quote again.
            self._activate(current)
            return
        if current:
            self.active_meta = dict(current)
        current_score = _n((current or self.active_meta).get("screen_score"))
        candidate_score = _n(candidate.get("screen_score"))
        if (current is None
                or candidate_score >= current_score + SWITCH_IMPROVEMENT_BPS):
            if candidate["pair"] != self.active_pair:
                self._activate(candidate)

    async def manage_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=maker.REQUEST_TIMEOUT_SEC)
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            while True:
                started = time.time()
                try:
                    self._choose()
                    if self.bot is None:
                        self.status = "waiting for a qualified non-BTC Georgian market"
                    else:
                        snapshot = await self.bot._fetch_snapshot(session)
                        self.bot._tick(snapshot)
                        self.status = (
                            f"collecting {self.bot.display_pair}: {self.bot.status}"
                        )
                        self.data_error = ""
                except maker.CryptalRateLimitError as exc:
                    self.data_error = ""
                    self.status = (
                        "Cryptal public gateway cooling down; selected-market "
                        f"quote cancelled; retrying in "
                        f"{max(1, math.ceil(exc.retry_after))}s"
                    )
                    if self.bot:
                        self.bot.quote = None
                except maker.PublicFeedUnavailable:
                    self.data_error = ""
                    self.status = (
                        "public market feeds reconnecting after bounded retries; "
                        "selected-market quote cancelled"
                    )
                    if self.bot:
                        self.bot.quote = None
                except Exception as exc:
                    self.data_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    self.status = "best-market collector waiting for public data"
                    if self.bot:
                        self.bot.quote = None
                delay = max(0.1, maker.POLL_SEC - (time.time() - started))
                delay = max(delay, self.data_hub.retry_delay())
                await asyncio.sleep(delay)

    def set_enabled(self, enabled: bool) -> dict:
        self.enabled = bool(enabled)
        if self.bot:
            self.bot.set_enabled(self.enabled)
        self._save_supervisor()
        return {"ok": True, "enabled": self.enabled}

    def reset(self) -> dict:
        if not self.bot:
            return {"ok": False, "error": "no active market yet"}
        result = self.bot.reset()
        self._save_supervisor()
        return result

    def state(self) -> dict:
        scanner_state = self.scanner.state()
        if self.bot:
            state = self.bot.state()
        else:
            state = {
                "running": True,
                "name": NAME,
                "enabled": self.enabled,
                "mode": "PAPER_ONLY",
                "live_trading_enabled": False,
                "status": self.status,
                "data_error": self.data_error or self.scanner.data_error,
                "public_data_health": self.data_hub.state(),
                "persistence_error": "",
                "pair": "",
                "display_pair": "WAITING",
                "base_asset": "",
                "quote_currency": "",
                "hedge": "Binance USDT perpetual selected per market",
                "poll_sec": maker.POLL_SEC,
                "paper_bankroll_usd": maker.PAPER_BANKROLL_USD,
                "balance": maker.PAPER_BANKROLL_USD,
                "equity": maker.PAPER_BANKROLL_USD,
                "start_balance": maker.PAPER_BANKROLL_USD,
                "total_pnl": 0.0,
                "trades": 0,
                "wins": 0,
                "market": {},
                "quote": None,
                "inventory": {},
                "history": [],
                "log": [],
                "validation": {
                    "status": "COLLECTING", "completed_cycles": 0,
                    "minimum_cycles": maker.MIN_PAPER_CYCLES,
                    "evidentiary": False,
                },
            }
        state.update({
            "name": NAME,
            "enabled": self.enabled,
            "status": self.status,
            "data_error": self.data_error or state.get("data_error", ""),
            "active_market": self.active_meta,
            "market_universe": scanner_state,
            "market_switches": self.switches[:20],
            "capital_model": (
                "One $100 paper collector follows one market at a time; candidate "
                "markets are not summed as separately funded accounts."
            ),
            "note": (
                "All-market Georgian discovery with public Cryptal books/trades and "
                "Binance hedge references. Paper fills remain non-evidentiary."
            ),
        })
        return state
