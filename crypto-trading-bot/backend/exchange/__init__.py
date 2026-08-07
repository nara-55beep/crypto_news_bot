"""Exchange adapters. Binance first; add OKX/Bybit by subclassing BaseExchange."""

from backend.exchange.base import BaseExchange
from backend.exchange.binance import BinanceExchange


def make_exchange(settings) -> BaseExchange:
    """Factory: return the exchange adapter for the configured exchange id.

    To add OKX/Bybit later: create the adapter class and add it to this map.
    """
    exchange_id = (settings.exchange or "binanceusdm").lower()
    if exchange_id in ("binance", "binanceusdm", "binanceus"):
        return BinanceExchange(settings)
    # Future: elif exchange_id == "okx": return OKXExchange(settings)
    raise ValueError(
        f"Exchange '{settings.exchange}' is not supported yet. "
        f"Add an adapter in backend/exchange/ and register it in make_exchange()."
    )
