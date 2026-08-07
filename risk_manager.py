"""
================================================================================
 risk_manager.py  —  RISK MANAGEMENT + POSITION SIZING
================================================================================
The job of this module is to say NO. A news bot's failure mode is overtrading
and revenge-trading after losses; these gates exist to stop that.

Gates (all must pass to allow a new entry):
  - daily loss circuit breaker     (stop after -X% on the day)
  - cooldown after N consecutive losses
  - max concurrent positions
  - max trades per rolling hour
  - one position per symbol (don't stack the same trade)

Sizing is RISK-BASED: we choose margin so that hitting the stop-loss loses
exactly RISK_PCT_PER_TRADE of current equity, then scale that down by the AI's
confidence. This is how the $100 survives a losing streak.
================================================================================
"""

from __future__ import annotations

import time
from collections import deque

import config


class RiskManager:
    def __init__(self, broker):
        self.broker = broker
        self.day = time.gmtime().tm_yday
        self.day_start_equity = broker.equity()
        self.trade_times = deque()              # timestamps of recent entries
        self.cooldown_until = 0.0

    def _roll_day(self):
        today = time.gmtime().tm_yday
        if today != self.day:
            self.day = today
            self.day_start_equity = self.broker.equity()
            self.cooldown_until = 0.0

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        self._roll_day()
        now = time.time()

        # daily loss breaker
        eq = self.broker.equity()
        dd = (eq - self.day_start_equity) / self.day_start_equity
        if dd <= -config.MAX_DAILY_LOSS_PCT:
            return False, f"daily loss limit hit ({dd*100:.1f}%)"

        # cooldown after a losing streak
        if self.broker.consecutive_losses >= config.COOLDOWN_AFTER_LOSSES:
            self.cooldown_until = max(
                self.cooldown_until, now + config.COOLDOWN_MINUTES * 60)
            self.broker.consecutive_losses = 0   # arm cooldown once
        if now < self.cooldown_until:
            left = int((self.cooldown_until - now) / 60)
            return False, f"cooldown active (~{left} min left)"

        # concurrent cap
        if len(self.broker.positions) >= config.MAX_CONCURRENT:
            return False, "max concurrent positions"

        # one per symbol
        if symbol in self.broker.open_symbols():
            return False, f"already in a {symbol} position"

        # overtrading throttle (rolling hour)
        while self.trade_times and now - self.trade_times[0] > 3600:
            self.trade_times.popleft()
        if len(self.trade_times) >= config.MAX_TRADES_PER_HOUR:
            return False, "max trades/hour throttle"

        return True, "ok"

    def register_trade(self):
        self.trade_times.append(time.time())

    def position_margin(self, size_fraction: float) -> float:
        """Risk-based sizing. At the stop-loss, loss = margin*leverage*SL_PCT.
        We want that to equal risk_amount, so:
            margin = risk_amount / (leverage * SL_PCT)
        size_fraction (0-1) comes from the AI's conviction (low/med/high), so a
        low-conviction trade risks less than a high-conviction one."""
        eq = self.broker.equity()
        frac = max(0.0, min(1.0, size_fraction))
        risk_amount = eq * config.RISK_PCT_PER_TRADE * frac
        denom = config.MAX_LEVERAGE * config.STOP_LOSS_PCT
        margin = risk_amount / denom if denom else 0.0
        return min(margin, eq)   # never exceed what we have
