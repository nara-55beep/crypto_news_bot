"""
engine.py — the LIVE trading engine.  ⚠️ REAL MONEY.

It reuses the entire PaperEngine (same strategy, same risk rules, same accounting)
and only adds one thing: it sends real market orders to the exchange when a position
opens or closes.

SAFETY — live trading cannot start unless ALL of these are true:
  1. BOT_LIVE_TRADING=true in the environment,
  2. both BOT_BINANCE_API_KEY and BOT_BINANCE_API_SECRET are set,
  3. the operator has "armed" the engine via the dashboard (an explicit POST /api/live/arm).

If any of these is missing, start() refuses and the engine stays a pure simulator.

This is an MVP. Before trading meaningful size you should add: real fill/position
reconciliation against the exchange, exchange-side stop/TP orders, and partial-fill
handling. Treat live mode as experimental.
"""

from __future__ import annotations

from backend.paper.engine import PaperEngine
from backend.utils.logger import get_logger

log = get_logger("live")


class LiveEngine(PaperEngine):
    mode = "live"

    def __init__(self, settings, db, exchange=None):
        super().__init__(settings, db, exchange)
        self.armed = False  # must be explicitly armed from the dashboard

    def arm(self) -> dict:
        """Operator confirmation that real orders may be placed."""
        why = self._why_cannot_arm()
        if why:
            return {"ok": False, "error": why}
        self.armed = True
        self._log("warn", "LIVE engine ARMED — real orders are now allowed")
        return {"ok": True, "armed": True}

    def disarm(self) -> dict:
        self.armed = False
        self._log("info", "LIVE engine disarmed")
        return {"ok": True, "armed": False}

    def _why_cannot_arm(self) -> str:
        if not self.s.live_trading:
            return "BOT_LIVE_TRADING is not true"
        if not (self.s.binance_api_key and self.s.binance_api_secret):
            return "API key/secret are not set in the environment"
        return ""

    def start(self) -> None:
        # Hard gate: refuse to run the live loop unless fully enabled + armed.
        why = self._why_cannot_arm()
        if why:
            raise RuntimeError(f"Cannot start LIVE engine: {why}")
        if not self.armed:
            raise RuntimeError("Cannot start LIVE engine: not armed. POST /api/live/arm first.")
        super().start()

    # ---- the only methods that touch real money ----
    def _on_open(self, symbol: str, side: str, qty: float, price: float) -> None:
        if not self.armed:
            return
        order_side = "buy" if side == "long" else "sell"
        try:
            self.exchange.create_market_order(symbol, order_side, qty)
        except Exception as e:
            log.exception("LIVE open order failed: %s", e)
            self._log("error", f"LIVE open order FAILED {symbol} {order_side} {qty}: {e}")

    def _on_close(self, symbol: str, side: str, qty: float, price: float) -> None:
        if not self.armed:
            return
        order_side = "sell" if side == "long" else "buy"   # reduce/close
        try:
            self.exchange.create_market_order(symbol, order_side, qty)
        except Exception as e:
            log.exception("LIVE close order failed: %s", e)
            self._log("error", f"LIVE close order FAILED {symbol} {order_side} {qty}: {e}")

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["armed"] = self.armed
        snap["can_arm"] = (self._why_cannot_arm() == "")
        snap["arm_blocked_reason"] = self._why_cannot_arm()
        return snap
