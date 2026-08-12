"""Execution-audited paper ledger for the original Lucid five-strategy basket.

The signal definitions and Dukascopy input path remain in ``lucid_pass_paper``.
This class changes only how the standard pass-stop bot turns a completed signal
bar into a paper fill and how that fill is charged and risk-limited.  The
continuous bot deliberately continues to instantiate the original class.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict

import pandas as pd

import config
import lucid_pass_paper as base


NAME = "Lucid 50K Monthly Pass Basket - Execution Audited (paper)"
AUDIT_VERSION = "lucid_5basket_execution_audit_v19"
MAX_AUDITED_MICROS = 40
COMMISSION_PER_SIDE = 0.50
COMMISSION_ROUND_TURN = 2.0 * COMMISSION_PER_SIDE
ENTRY_SLIP_TICKS = 1.0
EXIT_SLIP_TICKS = 1.0
INITIAL_TRAIL_BALANCE = 52_100.0
LOCKED_MLL_BALANCE = 50_100.0


def _audit_fingerprint() -> str:
    payload = {
        "audit_version": AUDIT_VERSION,
        "base_strategy_fingerprint": base.STRATEGY_FINGERPRINT,
        "components": base.COMPONENTS,
        "max_micros": MAX_AUDITED_MICROS,
        "commission_per_side": COMMISSION_PER_SIDE,
        "entry_slip_ticks": ENTRY_SLIP_TICKS,
        "exit_slip_ticks": EXIT_SLIP_TICKS,
        "entry_basis": "completed_signal_bar_close",
        "same_bar_exit": False,
        "stop_gap_policy": "worse_of_stop_or_next_bar_open",
        "mll": {
            "amount": base.MAX_DRAWDOWN,
            "initial_trail_balance": INITIAL_TRAIL_BALANCE,
            "locked_balance": LOCKED_MLL_BALANCE,
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


AUDIT_FINGERPRINT = _audit_fingerprint()


class AuditedLucidPassPaperBot(base.LucidPassPaperBot):
    """Same signals as v18, with a separate conservative execution ledger."""

    NAME = NAME

    def _path(self) -> str:
        # Replaces only the standard pass bot. The continuous bot uses its own
        # lucid_continuous_state.json path and original execution class.
        return os.path.join(config.DATA_DIR, "lucid_pass_state.json")

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            path = self._path()
            tmp = path + ".tmp"
            data = {
                "enabled": self.enabled,
                "strategy_version": AUDIT_VERSION,
                "strategy_fingerprint": AUDIT_FINGERPRINT,
                "balance": self.balance,
                "peak": self.peak,
                "locked": self.locked,
                "floor": self.floor,
                "day_key": self.day_key,
                "day_pnl": self.day_pnl,
                "passed": self.passed,
                "failed": self.failed,
                "daily_stopped_day": self.daily_stopped_day,
                "telegram_enabled": self.telegram_enabled,
                "positions": {key: asdict(pos) for key, pos in self.pos.items()},
                "fired_keys": sorted(self.fired_keys)[-120:],
                "warning_keys": sorted(self.warning_keys)[-160:],
                "history": self.history[:180],
                "log": self.log[:120],
                "setups": self.setups,
            }
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception as exc:
            self.data_error = f"audited state save failed: {type(exc).__name__}: {str(exc)[:100]}"

    def _load(self):
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("strategy_version") != AUDIT_VERSION:
                self.telegram_enabled = bool(data.get("telegram_enabled", True))
                return
            if str(data.get("strategy_fingerprint") or "") != AUDIT_FINGERPRINT:
                self.telegram_enabled = bool(data.get("telegram_enabled", True))
                return
            self.enabled = bool(data.get("enabled", True))
            self.balance = float(data.get("balance", base.START_BALANCE))
            self.peak = float(data.get("peak", self.balance))
            self.locked = bool(data.get("locked", False))
            self.floor = float(data.get("floor", base.START_BALANCE - base.MAX_DRAWDOWN))
            self.day_key = str(data.get("day_key", ""))
            self.day_pnl = float(data.get("day_pnl", 0.0))
            self.passed = bool(data.get("passed", False))
            self.failed = bool(data.get("failed", False))
            self.daily_stopped_day = str(data.get("daily_stopped_day", ""))
            self.telegram_enabled = bool(data.get("telegram_enabled", True))
            self.pos = {
                key: base.LucidPos(**value)
                for key, value in (data.get("positions") or {}).items()
            }
            self.fired_keys = set(data.get("fired_keys") or [])
            self.warning_keys = set(data.get("warning_keys") or [])
            self.history = data.get("history", []) or []
            self.log = data.get("log", []) or []
            self.setups = data.get("setups", {}) or {}
        except Exception as exc:
            self.data_error = f"audited state load failed: {type(exc).__name__}: {str(exc)[:100]}"

    def _size_for_levels(self, symbol: str, entry: float, stop: float) -> tuple[int, float]:
        pv, tick, _ = base.MARKETS[symbol]
        per_micro = self._planned_loss_per_micro(entry, stop, pv, tick)
        if per_micro <= 0:
            return 0, 0.0
        qty = min(MAX_AUDITED_MICROS, int(math.floor(base.RISK_USD / per_micro)))
        return qty, per_micro * qty

    def _planned_loss_per_micro(self, entry: float, stop: float, pv: float, tick: float) -> float:
        distance = abs(float(entry) - float(stop)) + tick * EXIT_SLIP_TICKS
        return distance * pv + COMMISSION_ROUND_TURN

    def _reserved_stop_risk(self, pos: base.LucidPos) -> float:
        side = 1 if pos.side == "long" else -1
        stop_fill = float(pos.stop) - side * pos.tick * EXIT_SLIP_TICKS
        gross = max(0.0, side * (pos.entry - stop_fill) * pos.micro_pv * pos.qty)
        return gross + COMMISSION_ROUND_TURN * pos.qty

    def _entry_qty(self, entry: float, stop: float, pv: float, tick: float) -> tuple[int, float]:
        per_micro = self._planned_loss_per_micro(entry, stop, pv, tick)
        if per_micro <= 0:
            return 0, 0.0
        requested = int(math.floor(base.RISK_USD / per_micro))
        open_micros = sum(int(pos.qty) for pos in self.pos.values())
        cap_room = max(0, MAX_AUDITED_MICROS - open_micros)
        reserved = sum(self._reserved_stop_risk(pos) for pos in self.pos.values())
        floor_room = max(0.0, self.equity() - self.floor - reserved - 0.01)
        daily_room = max(0.0, base.DAILY_LOSS_LIMIT + self.day_pnl - reserved - 0.01)
        risk_room_qty = int(math.floor(min(floor_room, daily_room) / per_micro))
        qty = max(0, min(requested, cap_room, risk_room_qty))
        return qty, per_micro * qty

    def _open(self, sig: dict, cur: pd.Series):
        if not self._live_open_guard_ok(sig, cur):
            self._note(
                f"REJECT stale/out-of-session audited open for {sig.get('strat', sig.get('key', ''))}",
                "loss",
            )
            return
        side = 1 if sig["side"] == "long" else -1
        pv, tick, _ = base.MARKETS[sig["symbol"]]
        observed_close = float(cur["close"])
        entry = observed_close + side * tick * ENTRY_SLIP_TICKS
        stop = float(sig["stop"])
        target = float(sig["target"])
        valid = (
            stop < entry < target
            if side > 0
            else target < entry < stop
        )
        if not valid:
            self.fired_keys.add(f"{self.day_key}:{sig['key']}")
            self._note(
                f"REJECT {sig['strat']} - completed close {observed_close:.2f} no longer lies "
                "between its original stop and target",
                "loss",
            )
            return
        qty, risk_usd = self._entry_qty(entry, stop, pv, tick)
        if qty < 1:
            self.fired_keys.add(f"{self.day_key}:{sig['key']}")
            self._note(
                f"REJECT {sig['strat']} - no whole micro fits $200 risk, the 40-micro "
                "portfolio cap, daily room and drawdown room",
                "loss",
            )
            return
        r_points = abs(entry - stop)
        tp1 = entry + side * r_points
        pos = base.LucidPos(
            id=base.uuid.uuid4().hex[:6],
            key=sig["key"],
            symbol=sig["symbol"],
            label=sig["label"],
            strat=sig["strat"],
            side=sig["side"],
            qty=qty,
            qty0=qty,
            entry=entry,
            stop=stop,
            stop0=stop,
            tp1=tp1,
            target=target,
            r_points=r_points,
            micro_pv=pv,
            tick=tick,
            risk_usd=risk_usd,
            cost_usd=qty * COMMISSION_ROUND_TURN,
            opened_at=base.time.time(),
            opened_bar=base._row_ts(cur),
            best=entry,
            # The completed signal bar supplied the decision. It cannot also
            # stop or profit the trade; management begins with a later bar.
            last_managed_bar=base._row_ts(cur),
            last_close=entry,
            last_day=str(cur.get("day", "")),
            realized=0.0,
            note=(
                sig["note"]
                + f"; audited entry at completed close {observed_close:.2f} plus one tick"
            ),
        )
        self.pos[pos.key] = pos
        self.fired_keys.add(f"{self.day_key}:{pos.key}")
        self._note(
            f"AUDITED OPEN {pos.strat} {pos.side.upper()} {pos.label} @ {entry:.2f} "
            f"stop {stop:.2f} tp1 {tp1:.2f} target {target:.2f} ({qty} whole micros)",
            "open",
        )
        self._alert(self._open_alert_text(pos))

    def _manage(self, key: str, bar: pd.Series):
        pos = self.pos.get(key)
        if pos is None:
            return
        pos.last_managed_bar = base._row_ts(bar)
        side = 1 if pos.side == "long" else -1
        hi = float(bar["high"])
        lo = float(bar["low"])
        close = float(bar["close"])
        bar_open = float(bar["open"])
        bar_day = str(bar.get("day", ""))
        if pos.last_day and bar_day and bar_day != pos.last_day:
            self._close(key, pos.last_close or pos.entry, "eod")
            return
        if bar_day:
            pos.last_day = bar_day
        pos.last_close = close
        utc_minute = base._utc_minute(bar["dt_utc"])
        if side > 0:
            pos.best = max(pos.best, hi)
            stopped = lo <= pos.stop
            tp1_hit = hi >= pos.tp1
            target_hit = hi >= pos.target
            stop_fill = min(pos.stop, bar_open)
        else:
            pos.best = min(pos.best, lo)
            stopped = hi >= pos.stop
            tp1_hit = lo <= pos.tp1
            target_hit = lo <= pos.target
            stop_fill = max(pos.stop, bar_open)

        # If a completed bar contains both a stop and a profit level, the
        # adverse outcome wins because tick ordering is unavailable.
        if stopped:
            reason = "be" if pos.partial_done and abs(pos.stop - pos.entry) < 1e-9 else "stop"
            self._close(key, stop_fill, reason)
            return
        if target_hit:
            self._close(key, pos.target, "target")
            return
        if not pos.partial_done and tp1_hit:
            partial_qty = min(int(pos.qty0) // 2, int(pos.qty))
            if partial_qty > 0:
                self._partial(key, partial_qty, pos.tp1)
            if key in self.pos:
                pos.partial_done = True
                pos.stop = pos.entry
                self._note(f"+1R {pos.label} {pos.strat} - runner stop to breakeven", "info")
                if utc_minute >= base._component_flat_utc_min(key):
                    self._close(key, close, "eod")
            return
        if utc_minute >= base._component_flat_utc_min(key):
            self._close(key, close, "eod")

    def _exit_price(self, pos: base.LucidPos, raw_exit: float) -> float:
        side = 1 if pos.side == "long" else -1
        return float(raw_exit) - side * pos.tick * EXIT_SLIP_TICKS

    def _trade_cost_usd(self, qty: int) -> float:
        return max(0, int(qty)) * COMMISSION_ROUND_TURN

    def _roll_day(self, day_key: str):
        if self.day_key and self.day_key != day_key:
            self.peak = max(self.peak, self.balance)
            if self.peak >= INITIAL_TRAIL_BALANCE:
                self.locked = True
                self.floor = max(self.floor, LOCKED_MLL_BALANCE)
            else:
                self.floor = max(
                    base.START_BALANCE - base.MAX_DRAWDOWN,
                    self.peak - base.MAX_DRAWDOWN,
                )
        super()._roll_day(day_key)

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("audited pass bot ENABLED" if self.enabled else "audited pass bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        result = super().reset()
        self._note("execution-audited ledger starts from $50,000; old v18 P&L is not mixed in")
        self._save()
        return result

    def state(self):
        state = super().state()
        state.update({
            "name": self.NAME,
            "strategy_version": AUDIT_VERSION,
            "strategy_fingerprint": AUDIT_FINGERPRINT,
            "max_micros": MAX_AUDITED_MICROS,
            "trail_locked": self.locked,
            "execution_audited": True,
            "entry_basis": "completed signal-bar close + 1 adverse tick",
            "exit_basis": "later completed bars + 1 adverse tick; gap-aware stops",
            "commission_per_side": COMMISSION_PER_SIDE,
            "legacy_pnl_carried": False,
            "backtest_note": (
                "Same five v18 signal rules and same Dukascopy chart feed. This replacement "
                "ledger counts only completed-bar decisions, never exits on the signal bar, "
                "uses whole micros, one adverse tick per side, $0.50 commission per side, "
                "a 40-micro portfolio cap, the $1,200 daily guard and Lucid's EOD drawdown. "
                "Dukascopy remains a price proxy rather than a broker fill receipt, so this is "
                "a conservative paper audit - not proof of live profitability. Old v18 P&L is "
                "preserved separately and is not mixed into this ledger."
            ),
        })
        return state
