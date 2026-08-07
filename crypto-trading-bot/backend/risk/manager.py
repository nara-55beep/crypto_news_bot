"""
manager.py — risk management. This is the most important module in the project.

It decides:
  * how big a position may be (fixed % of account risked per trade),
  * where the stop-loss and take-profit go (ATR-based stop, R/R target),
  * whether we are ALLOWED to open a trade at all (the guardrails),
  * how a trailing stop moves.

Hard rules baked in: no martingale, no averaging down (engines open ONE position
per symbol and never add to a loser), cooldown after a loss, and a daily-drawdown
pause. None of these can be bypassed by the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class Bracket:
    stop: float          # stop-loss price
    take: float          # take-profit price
    stop_distance: float # price distance from entry to stop (the "1R" risk unit)


class RiskManager:
    def __init__(self, settings):
        self.s = settings

    # ------------------------- stop / take-profit -----------------------------
    def build_bracket(self, entry: float, side: str, atr: float) -> Bracket:
        """ATR-based stop, take-profit at rr * risk."""
        stop_distance = max(atr * self.s.atr_stop_mult, entry * 1e-4)  # never zero
        if side == "long":
            stop = entry - stop_distance
            take = entry + stop_distance * self.s.rr
        else:
            stop = entry + stop_distance
            take = entry - stop_distance * self.s.rr
        return Bracket(stop=stop, take=take, stop_distance=stop_distance)

    # ------------------------- position sizing --------------------------------
    def position_size(self, balance: float, entry: float, stop: float) -> float:
        """Quantity (in base units, e.g. BTC) so that hitting the stop loses exactly
        `risk_per_trade` of the account. Capped by max_leverage."""
        risk_amount = balance * self.s.risk_per_trade
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return 0.0
        qty = risk_amount / stop_distance
        # Cap notional so we never exceed max leverage.
        max_qty = (balance * self.s.max_leverage) / entry
        return max(0.0, min(qty, max_qty))

    # ------------------------- the guardrails ---------------------------------
    def check_can_open(
        self,
        now_ts: float,
        open_positions: int,
        day_pnl_pct: float,
        last_loss_ts: Optional[float],
    ) -> Tuple[bool, str]:
        """Return (allowed, reason). Reason explains a block (shown in the UI)."""
        if open_positions >= self.s.max_open_positions:
            return False, f"max open positions reached ({self.s.max_open_positions})"
        if day_pnl_pct <= -abs(self.s.max_daily_loss):
            return False, (
                f"daily loss limit hit ({day_pnl_pct*100:.2f}% <= -{self.s.max_daily_loss*100:.2f}%) "
                f"— trading paused until the next UTC day"
            )
        if last_loss_ts is not None:
            cooldown = self.s.cooldown_minutes * 60
            remaining = cooldown - (now_ts - last_loss_ts)
            if remaining > 0:
                return False, f"cooldown after a loss ({remaining/60:.0f} min left)"
        return True, "ok"

    # ------------------------- trailing stop ----------------------------------
    def update_trailing_stop(self, side: str, current_stop: float, best_price: float, atr: float) -> float:
        """Move the stop in the favourable direction only (never loosens)."""
        if not self.s.use_trailing_stop:
            return current_stop
        dist = atr * self.s.trail_atr_mult
        if side == "long":
            return max(current_stop, best_price - dist)
        return min(current_stop, best_price + dist)
