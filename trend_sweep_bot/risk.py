"""
risk.py — position sizing and the trading circuit breakers.

  - size by risk: qty = (risk_pct * equity) / stop_distance, so every trade risks the same %.
  - one position per symbol (enforced by the engine/live executor holding a single position).
  - max N trades per UTC day.
  - halt after K consecutive losses (resets at the UTC day boundary).
"""
from __future__ import annotations
from datetime import datetime, timezone


def position_size(equity, risk_pct, entry, stop):
    """Base quantity so that a stop-out loses exactly risk_pct of equity. 0 if degenerate."""
    dist = abs(entry - stop)
    if dist <= 0 or equity <= 0:
        return 0.0
    risk_amount = equity * risk_pct
    return risk_amount / dist


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._day = None
        self.trades_today = 0
        self.consecutive_losses = 0

    @staticmethod
    def _utc_day(ts):
        d = datetime.fromtimestamp(ts, timezone.utc)
        return d.strftime("%Y-%m-%d")

    def roll_day(self, ts):
        day = self._utc_day(ts)
        if day != self._day:
            self._day = day
            self.trades_today = 0
            self.consecutive_losses = 0

    def can_trade(self, ts):
        self.roll_day(ts)
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max trades/day reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, f"{self.consecutive_losses} consecutive losses — halted for the day"
        return True, ""

    def on_open(self, ts):
        self.roll_day(ts)
        self.trades_today += 1

    def on_close(self, pnl):
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
