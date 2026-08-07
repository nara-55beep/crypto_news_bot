"""
base.py — the exchange interface every adapter must implement.

Keeping the rest of the bot behind this small interface means we can add OKX,
Bybit, etc. later WITHOUT touching the strategy, backtest, or engine code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional


class BaseExchange(ABC):
    """Abstract exchange. Data methods are required; trading methods are only
    used by the live engine (and are optional/guarded)."""

    #: human-friendly name, e.g. "binanceusdm"
    name: str = "base"

    # ------------------------------- market data -------------------------------
    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> List[list]:
        """Return a list of [timestamp_ms, open, high, low, close, volume] rows,
        OLDEST first. Raise on failure (callers handle/log)."""

    @abstractmethod
    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Latest funding rate as a fraction (e.g. 0.0001 = 0.01%), or None if
        the exchange/market does not provide it."""

    @abstractmethod
    def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        """Latest traded price for a symbol, or None on failure."""

    # ------------------------------- trading (live only) -----------------------
    def fetch_balance_usdt(self) -> Optional[float]:  # pragma: no cover - live only
        """Free USDT balance (live mode). Default None = not implemented."""
        return None

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:  # pragma: no cover
        """Place a real market order (live mode only). Adapters that support live
        trading override this. Default raises so paper/backtest can never trade."""
        raise NotImplementedError("Live trading is not implemented for this exchange.")
