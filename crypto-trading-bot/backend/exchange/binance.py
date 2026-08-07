"""
binance.py — Binance adapter built on ccxt.

Uses Binance USD-M futures by default (so funding rates and shorting are natural).
Read-only methods (OHLCV, price, funding) never need API keys. Trading methods are
only wired up when live trading is explicitly enabled with keys present.
"""

from __future__ import annotations

from typing import List, Optional

import ccxt

from backend.exchange.base import BaseExchange
from backend.utils.logger import get_logger

log = get_logger("exchange.binance")


class BinanceExchange(BaseExchange):
    def __init__(self, settings):
        self.settings = settings
        self.name = settings.exchange

        # Pick the ccxt class by id. binanceusdm = USD-M futures (recommended).
        ccxt_class = getattr(ccxt, settings.exchange, None) or ccxt.binanceusdm

        params = {"enableRateLimit": True, "options": {"defaultType": "future"}}
        # Only attach keys if BOTH are present AND live trading is on. Otherwise we
        # stay fully read-only — there is no way to place an order without keys.
        if settings.live_trading and settings.binance_api_key and settings.binance_api_secret:
            params["apiKey"] = settings.binance_api_key
            params["secret"] = settings.binance_api_secret
            log.info("Binance adapter created WITH api keys (live trading is enabled).")
        else:
            log.info("Binance adapter created in READ-ONLY mode (no keys / live off).")

        self.client = ccxt_class(params)
        try:
            self.client.load_markets()
        except Exception as e:  # network/geo errors are common — log, don't crash
            log.warning("load_markets failed (continuing, will retry on demand): %s", e)

    # ------------------------------- market data -------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> List[list]:
        """[ts_ms, o, h, l, c, v] rows, oldest first."""
        return self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            fr = self.client.fetch_funding_rate(symbol)
            rate = fr.get("fundingRate")
            return float(rate) if rate is not None else None
        except Exception as e:
            log.debug("funding rate unavailable for %s: %s", symbol, e)
            return None

    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.client.fetch_ticker(symbol)
            return float(t["last"]) if t.get("last") is not None else None
        except Exception as e:
            log.debug("ticker price failed for %s: %s", symbol, e)
            return None

    # ------------------------------- trading (live only) -----------------------
    def fetch_balance_usdt(self) -> Optional[float]:
        try:
            bal = self.client.fetch_balance()
            return float(bal["free"].get("USDT", 0.0))
        except Exception as e:
            log.error("fetch_balance failed: %s", e)
            return None

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        # side is "buy" or "sell". Caller (live engine) is responsible for all the
        # safeguards BEFORE getting here. This is intentionally the only place that
        # can spend real money.
        if not (self.settings.live_trading and self.client.apiKey):
            raise RuntimeError("Refusing to place a real order: live trading is not fully enabled.")
        log.warning("LIVE ORDER -> %s %s %.6f", side, symbol, amount)
        return self.client.create_order(symbol, "market", side, amount)
