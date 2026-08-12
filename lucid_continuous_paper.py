"""
lucid_continuous_paper.py - Lucid 50K basket paper bot without stopping at pass target.

This is the same causal three-strategy ES/NQ basket as lucid_pass_paper.py,
but it keeps opening new trades after the $53,000 pass target is crossed. It
uses its own state file so it does not interfere with the pass-stop bot.
"""
from __future__ import annotations

import os

import config
import lucid_pass_paper as base


NAME = "Lucid 50K Monthly Pass Basket - Continuous (paper)"


class LucidContinuousPaperBot(base.LucidPassPaperBot):
    NAME = NAME

    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "lucid_continuous_state.json")

    def _stops_after_target(self) -> bool:
        return False

    def _uses_daily_loss_guard(self) -> bool:
        return True

    def _can_open(self, cur) -> bool:
        return self._can_open_key("", cur)

    def _can_open_key(self, key, cur) -> bool:
        if not self.enabled or self.failed:
            return False
        eq = self.equity()
        if eq <= self.floor:
            if not self.failed:
                self.failed = True
                self._note("drawdown floor breached - bot stopped", "loss")
            return False
        if self.day_pnl <= -base.DAILY_LOSS_LIMIT:
            if self.daily_stopped_day != self.day_key:
                self.daily_stopped_day = self.day_key
                self._note("daily loss guard hit - no more entries today", "loss")
            return False
        if not self._exact_realtime_entry_ok():
            if key:
                self.setups[key] = {
                    "mkt": base.COMPONENTS[key]["label"],
                    "status": "blocked - exact realtime bridge not ready",
                }
            return False
        if not self._entry_guard_ok(key, cur):
            return False
        return True

    def _book(self, pnl: float):
        self.balance += pnl
        self.day_pnl += pnl
        if self.balance >= base.TARGET_BALANCE and not self.passed:
            self.passed = True
            self._note("pass target crossed - continuous mode keeps trading", "win")
        if self.balance <= self.floor and not self.failed:
            self.failed = True
            self._note("drawdown floor breached - bot stopped", "loss")

    def _set_status(self):
        if not self.enabled:
            self.status = "paused"
        elif self.failed:
            self.status = "stopped - drawdown floor breached"
        elif self.pos:
            self.status = "in trade: " + ", ".join(
                p.label + " " + p.strat for p in self.pos.values()
            )
        elif self.passed or self.equity() >= base.TARGET_BALANCE:
            self.passed = True
            self.status = "target crossed - continuous mode still scanning causal ES/NQ basket"
        elif not self._real_entry_window_ok():
            self.status = "outside real entry window - waiting for next NY session"
        else:
            self.status = "live - scanning continuous causal ES/NQ 15m basket"

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - Lucid continuous causal basket" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = super().reset()
        self._note("continuous mode active - will not stop at $53,000 target")
        self._save()
        return out

    def state(self):
        s = super().state()
        eq = self.equity()
        s["name"] = self.NAME
        s["status"] = self.status
        s["daily_loss_limit"] = base.DAILY_LOSS_LIMIT
        s["phase"] = (
            "continuous after target"
            if self.passed or eq >= base.TARGET_BALANCE
            else ("floor breached" if self.failed else "continuous mode")
        )
        s["backtest_note"] = (
            "Continuous causal research basket: NQ 15m opening drive, ES 15m gap fill "
            "and NQ 15m prior-range momentum. The old +493.6% / 78% win claim was from "
            "invalid same-bar proxy fills and is discarded. New results start from zero "
            "with integer micros, one adverse tick each side, $1 round-turn commission, "
            "40-micro aggregate cap, drawdown reservation and the $1,200 daily guard. "
            "No profitability claim is made until forward paper evidence accumulates."
        )
        return s
