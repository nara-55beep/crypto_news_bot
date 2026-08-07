"""
controller.py — owns the single running engine and ties everything together.

The dashboard/API talk ONLY to this controller. It can swap between the paper and
live engines, start/stop them, run backtests, and apply live setting edits.
"""

from __future__ import annotations

from typing import Optional

from backend.config import EDITABLE_KEYS, settings
from backend.database.db import Database
from backend.backtest.engine import run_backtest
from backend.live.engine import LiveEngine
from backend.paper.engine import PaperEngine
from backend.utils.logger import get_logger

log = get_logger("controller")


class Controller:
    def __init__(self):
        self.s = settings
        self.db = Database(settings.db_path)
        # Always boot in a SAFE mode. Even if the env says live, we start in paper
        # and require the operator to switch + arm deliberately.
        self.mode = "paper"
        self.engine = self._build(self.mode)

    def _build(self, mode: str):
        if mode == "live":
            return LiveEngine(self.s, self.db)
        return PaperEngine(self.s, self.db)

    # ------------------------------- mode / control ---------------------------
    def set_mode(self, mode: str) -> dict:
        if mode not in ("paper", "live"):
            return {"ok": False, "error": "mode must be 'paper' or 'live'"}
        if mode == self.mode:
            return {"ok": True, "mode": self.mode}
        # stop the old engine, build the new one (starts stopped)
        try:
            self.engine.stop()
        except Exception:
            pass
        self.mode = mode
        self.engine = self._build(mode)
        log.info("mode switched to %s", mode)
        return {"ok": True, "mode": self.mode}

    def start(self) -> dict:
        try:
            self.engine.start()
            return {"ok": True, "running": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        self.engine.stop()
        return {"ok": True, "running": False}

    def emergency_stop(self) -> dict:
        self.engine.emergency_stop()
        return {"ok": True, "running": False}

    # ------------------------------- live arming ------------------------------
    def arm(self) -> dict:
        if not isinstance(self.engine, LiveEngine):
            return {"ok": False, "error": "switch to live mode first"}
        return self.engine.arm()

    def disarm(self) -> dict:
        if isinstance(self.engine, LiveEngine):
            return self.engine.disarm()
        return {"ok": True, "armed": False}

    # ------------------------------- settings ---------------------------------
    def update_settings(self, patch: dict) -> dict:
        applied, rejected = {}, {}
        for k, v in patch.items():
            if k not in EDITABLE_KEYS:
                rejected[k] = "not editable at runtime"
                continue
            try:
                current = getattr(self.s, k)
                # cast the incoming value to the existing type
                if isinstance(current, bool):
                    newv = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    newv = int(v)
                elif isinstance(current, float):
                    newv = float(v)
                else:
                    newv = v
                setattr(self.s, k, newv)
                applied[k] = newv
            except Exception as e:
                rejected[k] = str(e)
        log.info("settings updated: %s (rejected: %s)", applied, rejected)
        return {"ok": True, "applied": applied, "rejected": rejected}

    # ------------------------------- backtest ---------------------------------
    def backtest(self, symbols: Optional[list] = None, days: Optional[int] = None) -> dict:
        symbols = symbols or self.s.symbol_list
        if days:
            self.s.backtest_days = int(days)
        out = {}
        for sym in symbols:
            try:
                out[sym] = run_backtest(self.s, sym, exchange=self.engine.exchange)
            except Exception as e:
                log.exception("backtest failed for %s", sym)
                out[sym] = {"error": str(e)}
        return {"ok": True, "results": out}

    # ------------------------------- status -----------------------------------
    def status(self) -> dict:
        snap = self.engine.snapshot()
        snap["mode"] = self.mode
        snap["settings"] = self.s.public_dict()
        snap["live_enabled_in_env"] = self.s.live_trading
        return snap
