"""Forward paper runtime for the selected Lucid Strategy Lab portfolio.

This module has no order-routing code.  It watches public TradingView continuous-
futures data, forms signals only from completed one-minute bars, and records fills
in a persistent LucidPro 25K paper ledger.  Historical proxy evidence remains
separate from this forward population.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import json
import os
from pathlib import Path
import socket
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd

import config
import lucid_pass_paper as tv
import penny_quotes

from .rules import INSTRUMENTS, get_account_rules


NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SCHEMA_VERSION = 1
STRATEGY_VERSION = "lucid_lab_selected_3sleeve_forward_v1"
STARTING_BALANCE = Decimal("25000.00")
TARGET_BALANCE = Decimal("26250.00")
INITIAL_FLOOR = Decimal("24000.00")
SAFETY_RESERVE = Decimal("100.00")
DAILY_PROFIT_LOCK = Decimal("600.00")
MAX_TRADES_PER_DAY = 3
MAX_LOSSES_PER_DAY = 2
ENTRY_MAX_AGE_SEC = 20.0
QUOTE_MAX_AGE_SEC = 15.0
CALENDAR_REFRESH_SEC = 6 * 60 * 60


MARKETS = {
    "es": {
        "instrument": "MES", "tv_symbol": "CME_MINI:ES1!", "legacy_key": "ES_VWAP3",
        "tick": Decimal("0.25"), "point_value": Decimal("5"),
    },
    "nq": {
        "instrument": "MNQ", "tv_symbol": "CME_MINI:NQ1!", "legacy_key": "NQ_VWAP3",
        "tick": Decimal("0.25"), "point_value": Decimal("2"),
    },
}

SLEEVES = {
    "es_gap_fill": {
        "name": "MES 09:45 gap fill", "market": "es", "risk": Decimal("400"), "rr": Decimal("1.5"),
    },
    "nq_opening_drive": {
        "name": "MNQ 09:45 opening drive", "market": "nq", "risk": Decimal("400"), "rr": Decimal("2"),
    },
    "nq_prior_breakout": {
        "name": "MNQ prior-range breakout", "market": "nq", "risk": Decimal("100"), "rr": Decimal("2"),
    },
}


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _iso(timestamp: Any) -> str:
    return pd.Timestamp(timestamp).to_pydatetime().astimezone(UTC).isoformat()


def _epoch(timestamp: Any) -> float:
    stamp = pd.Timestamp(timestamp)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return float(stamp.timestamp())


def _quote_epoch(value: Any) -> float:
    """Normalise exchange timestamps that may be seconds, milliseconds or microseconds."""
    stamp = float(value)
    while stamp > 100_000_000_000:
        stamp /= 1000.0
    return stamp


@dataclass(frozen=True)
class PaperSignal:
    sleeve: str
    market: str
    side: str
    signal_at: str
    stop: str
    reward_risk: str
    note: str


@dataclass
class PaperPosition:
    id: str
    sleeve: str
    market: str
    instrument: str
    side: str
    quantity: int
    entry: str
    stop: str
    target: str
    opened_at: str
    activation_exchange_at: float
    last_quote_at: float
    risk_reserved: str
    entry_feed: str
    entry_evidentiary: bool
    event_filter_verified: bool
    last_mark: str = "0"

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self.side == "long" else Decimal("-1")


class LucidLabPaperBot:
    """One shared paper account with three independently displayed sleeves."""

    def __init__(self, state_path: str | Path | None = None):
        self.rules = get_account_rules("lucidpro", "evaluation", 25_000)
        self.state_path = Path(state_path) if state_path else Path(config.DATA_DIR) / "lucid_lab_paper_state.json"
        self.enabled = bool(getattr(config, "LUCID_LAB_PAPER_ENABLED", True))
        self.balance = STARTING_BALANCE
        self.floor = INITIAL_FLOOR
        self.highest_close = STARTING_BALANCE
        self.trail_locked = False
        self.passed = False
        self.breached = False
        self.current_session = ""
        self.day_start_balance = STARTING_BALANCE
        self.day_pnl = Decimal("0")
        self.day_trades = 0
        self.day_losses = 0
        self.trading_days = 0
        self.positions: dict[str, PaperPosition] = {}
        self.history: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.fired: set[str] = set()
        self.sleeve_status = {key: "waiting for futures data" for key in SLEEVES}
        self.feed_status = "starting TradingView futures feed"
        self.feed_realtime = False
        self.feed_error = ""
        self.calendar: dict[str, dict[str, int]] = {}
        self.calendar_status = "calendar not loaded"
        self.calendar_refreshed_at = 0.0
        self.persistence_error = ""
        self.event_filter_status = (
            "No point-in-time high-impact USD calendar is connected; paper trades are exploratory and non-evidentiary."
        )
        self._frames: dict[str, pd.DataFrame] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        self._last_processed_minute = ""
        self._primed = False
        self._load()

    def _note(self, message: str, kind: str = "info") -> None:
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": str(message)})
        self.log = self.log[:160]
        print(f"[lucid-lab-paper] {message}")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "strategy_version": STRATEGY_VERSION,
            "enabled": self.enabled,
            "balance": str(self.balance),
            "floor": str(self.floor),
            "highest_close": str(self.highest_close),
            "trail_locked": self.trail_locked,
            "passed": self.passed,
            "breached": self.breached,
            "current_session": self.current_session,
            "day_start_balance": str(self.day_start_balance),
            "day_pnl": str(self.day_pnl),
            "day_trades": self.day_trades,
            "day_losses": self.day_losses,
            "trading_days": self.trading_days,
            "positions": {key: asdict(value) for key, value in self.positions.items()},
            "history": self.history[:400],
            "log": self.log[:160],
            "snapshots": self.snapshots[:180],
            "fired": sorted(self.fired)[-120:],
        }

    def _save(self) -> bool:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._payload(), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
            self.persistence_error = ""
            return True
        except Exception as exc:
            self.persistence_error = f"{type(exc).__name__}: {exc}"
            return False

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported paper-state schema")
            if data.get("strategy_version") != STRATEGY_VERSION:
                raise ValueError("paper state belongs to a different strategy version")
            self.enabled = bool(data.get("enabled", self.enabled))
            self.balance = money(data["balance"])
            self.floor = money(data["floor"])
            self.highest_close = money(data["highest_close"])
            self.trail_locked = bool(data.get("trail_locked"))
            self.passed = bool(data.get("passed"))
            self.breached = bool(data.get("breached"))
            self.current_session = str(data.get("current_session") or "")
            self.day_start_balance = money(data.get("day_start_balance", self.balance))
            self.day_pnl = money(data.get("day_pnl", "0"))
            self.day_trades = int(data.get("day_trades", 0))
            self.day_losses = int(data.get("day_losses", 0))
            self.trading_days = int(data.get("trading_days", 0))
            self.positions = {
                key: PaperPosition(**value) for key, value in (data.get("positions") or {}).items()
            }
            self.history = list(data.get("history") or [])[:400]
            self.log = list(data.get("log") or [])[:160]
            self.snapshots = list(data.get("snapshots") or [])[:180]
            self.fired = set(data.get("fired") or [])
        except Exception as exc:
            self.enabled = False
            self.persistence_error = f"state load failed closed: {type(exc).__name__}: {exc}"

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if enabled and (self.passed or self.breached):
            raise ValueError("reset the completed paper account before enabling entries")
        self.enabled = bool(enabled)
        self._note("paper entries enabled" if self.enabled else "paper entries paused")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self) -> dict[str, Any]:
        if self.positions:
            raise RuntimeError("close the paper positions before resetting the account")
        self.balance = STARTING_BALANCE
        self.floor = INITIAL_FLOOR
        self.highest_close = STARTING_BALANCE
        self.trail_locked = self.passed = self.breached = False
        self.current_session = ""
        self.day_start_balance = STARTING_BALANCE
        self.day_pnl = Decimal("0")
        self.day_trades = self.day_losses = self.trading_days = 0
        self.history = []
        self.log = []
        self.snapshots = []
        self.fired = set()
        self.enabled = True
        self._note("paper account reset to $25,000")
        self._save()
        return {"ok": True, "enabled": True}

    def set_calendar(self, sessions: dict[str, dict[str, int]], error: str = "") -> None:
        if error:
            detail = str(error).strip()
            if detail.rstrip(":") == "TimeoutError":
                detail = "Alpaca US-session calendar request timed out; retrying in five minutes"
            self.calendar_status = detail
            # Fail closed, but retry a transient provider/credential error in five minutes.
            self.calendar_refreshed_at = time.time() - CALENDAR_REFRESH_SEC + 300
            return
        self.calendar = dict(sessions)
        self.calendar_refreshed_at = time.time()
        self.calendar_status = f"Alpaca market calendar verified ({len(sessions)} sessions cached)"

    def _session_row(self, day: date) -> dict[str, int] | None:
        return self.calendar.get(day.isoformat())

    @staticmethod
    def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume", "dt_ny", "day", "minute"])
        out = frame[["dt_utc", "open", "high", "low", "close", "volume"]].copy()
        out["dt_utc"] = pd.to_datetime(out["dt_utc"], utc=True)
        out = out.dropna(subset=["dt_utc", "open", "high", "low", "close"])
        out = out.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc")
        out["dt_ny"] = out["dt_utc"].dt.tz_convert(NY)
        out["day"] = out["dt_ny"].dt.date
        out["minute"] = out["dt_ny"].dt.hour * 60 + out["dt_ny"].dt.minute
        return out.reset_index(drop=True)

    @staticmethod
    def _rth(frame: pd.DataFrame, day: date, close_minute: int = 16 * 60) -> pd.DataFrame:
        return frame[(frame["day"] == day) & (frame["minute"] >= 9 * 60 + 30) & (frame["minute"] < close_minute)].copy()

    @staticmethod
    def _complete_prior(frame: pd.DataFrame, before: date) -> pd.DataFrame | None:
        prior = frame[frame["day"] < before]
        for _, group in reversed(list(prior.groupby("day", sort=True))):
            rows = group[(group["minute"] >= 570) & (group["minute"] < 960)].copy()
            if len(rows) == 390 and rows["minute"].tolist() == list(range(570, 960)):
                return rows.reset_index(drop=True)
        return None

    def _quote_usable(self, market: str, now_epoch: float) -> bool:
        row = self._quotes.get(market) or {}
        try:
            bid, ask, at = D(row["bid"]), D(row["ask"]), _quote_epoch(row["at"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False
        age = now_epoch - at
        return bid > 0 and ask > bid and -2 <= age <= QUOTE_MAX_AGE_SEC

    def _feed_allows_entry(self) -> str:
        if not self.enabled:
            return "paper entries paused"
        if self.passed:
            return "evaluation target passed"
        if self.breached:
            return "maximum-loss floor breached"
        if self.persistence_error:
            return "state persistence is unhealthy"
        if not self.feed_realtime:
            return "TradingView feed is not confirmed realtime"
        return ""

    def _signals_for_minute(self, current_at: pd.Timestamp, close_minute: int) -> list[PaperSignal]:
        day = current_at.tz_convert(NY).date()
        local_minute = current_at.tz_convert(NY).hour * 60 + current_at.tz_convert(NY).minute
        signals: list[PaperSignal] = []
        es = self._rth(self._frames["es"], day, close_minute)
        nq = self._rth(self._frames["nq"], day, close_minute)
        es_complete = es[es["dt_utc"] < current_at]
        nq_complete = nq[nq["dt_utc"] < current_at]
        es_prior = self._complete_prior(self._frames["es"], day)
        nq_prior = self._complete_prior(self._frames["nq"], day)

        if local_minute == 585:
            signal = self._morning_signal("es_gap_fill", es_complete, es_prior)
            if signal:
                signals.append(signal)
            signal = self._morning_signal("nq_opening_drive", nq_complete, nq_prior)
            if signal:
                signals.append(signal)

        if local_minute >= 585 and (local_minute - 570) % 15 == 0:
            signal = self._prior_breakout_signal(nq_complete, nq_prior, local_minute)
            if signal:
                signals.append(signal)
        return signals

    def _morning_signal(self, sleeve: str, complete: pd.DataFrame, prior: pd.DataFrame | None) -> PaperSignal | None:
        cfg = SLEEVES[sleeve]
        if prior is None:
            self.sleeve_status[sleeve] = "blocked: no complete prior RTH session"
            return None
        opening = complete[(complete["minute"] >= 570) & (complete["minute"] <= 584)]
        if len(opening) != 15 or opening["minute"].tolist() != list(range(570, 585)):
            self.sleeve_status[sleeve] = "blocked: incomplete 09:30-09:44 window"
            return None
        prior_range = D(prior["high"].max()) - D(prior["low"].min())
        high, low = D(opening["high"].max()), D(opening["low"].min())
        opening_range = high - low
        session_open, close = D(opening.iloc[0]["open"]), D(opening.iloc[-1]["close"])
        if prior_range <= 0 or opening_range <= 0:
            return None
        if sleeve == "nq_opening_drive":
            basis = close - session_open
            side = "long" if basis > 0 else "short"
            threshold = Decimal("0.25")
            stop = session_open
            condition = abs(basis) >= threshold * prior_range
        else:
            gap = session_open - D(prior.iloc[-1]["close"])
            side = "short" if gap > 0 else "long"
            threshold = Decimal("0.10")
            drive = close - session_open
            condition = gap != 0 and abs(gap) >= threshold * prior_range and drive * (1 if side == "long" else -1) > 0
            tick = MARKETS[cfg["market"]]["tick"]
            stop = low - tick if side == "long" else high + tick
        location = (close - low) / opening_range
        condition = condition and (location >= Decimal("0.80") if side == "long" else location <= Decimal("0.20"))
        if not condition:
            self.sleeve_status[sleeve] = "no qualifying 09:45 setup today"
            return None
        self.sleeve_status[sleeve] = "signal confirmed; evaluating paper fill"
        return PaperSignal(
            sleeve, cfg["market"], side, _iso(opening.iloc[-1]["dt_utc"]), str(stop), str(cfg["rr"]),
            "completed 09:30-09:44 opening classification",
        )

    def _prior_breakout_signal(self, complete: pd.DataFrame, prior: pd.DataFrame | None, entry_minute: int) -> PaperSignal | None:
        sleeve = "nq_prior_breakout"
        if prior is None:
            self.sleeve_status[sleeve] = "blocked: no complete prior RTH session"
            return None
        end_minute = entry_minute - 1
        start_minute = entry_minute - 15
        block = complete[(complete["minute"] >= start_minute) & (complete["minute"] <= end_minute)]
        if len(block) != 15 or block["minute"].tolist() != list(range(start_minute, entry_minute)):
            self.sleeve_status[sleeve] = "blocked: incomplete clock-aligned 15-minute bar"
            return None
        today = complete[(complete["minute"] >= 570) & (complete["minute"] <= end_minute)]
        if today.empty:
            return None
        session_open = D(today.iloc[0]["open"])
        prior_range = D(prior["high"].max()) - D(prior["low"].min())
        if prior_range <= 0:
            return None
        long_level = session_open + Decimal("0.25") * prior_range
        short_level = session_open - Decimal("0.25") * prior_range
        signal_close = D(block.iloc[-1]["close"])
        previous_close = session_open
        if start_minute > 570:
            prior_rows = complete[complete["minute"] < start_minute]
            if prior_rows.empty:
                return None
            previous_close = D(prior_rows.iloc[-1]["close"])
        side = ""
        if previous_close <= long_level and signal_close > long_level:
            side = "long"
        elif previous_close >= short_level and signal_close < short_level:
            side = "short"
        if not side:
            self.sleeve_status[sleeve] = "scanning completed 15-minute closes"
            return None
        tick = MARKETS["nq"]["tick"]
        stop = D(block["low"].min()) - tick if side == "long" else D(block["high"].max()) + tick
        self.sleeve_status[sleeve] = "breakout confirmed; evaluating paper fill"
        return PaperSignal(
            sleeve, "nq", side, _iso(block.iloc[-1]["dt_utc"]), str(stop), "2",
            "completed 15-minute close crossed the prior-range threshold",
        )

    def _committed_stop_risk(self) -> Decimal:
        return money(sum((D(position.risk_reserved) for position in self.positions.values()), Decimal("0")))

    def _open_signal(self, signal: PaperSignal, current_at: pd.Timestamp, now_epoch: float) -> bool:
        fired_key = f"{current_at.tz_convert(NY).date()}:{signal.sleeve}"
        if fired_key in self.fired:
            return False
        reason = self._feed_allows_entry()
        if reason:
            self.sleeve_status[signal.sleeve] = "blocked: " + reason
            return False
        if self.day_trades >= MAX_TRADES_PER_DAY:
            self.sleeve_status[signal.sleeve] = "blocked: three-trade daily cap reached"
            return False
        if self.day_losses >= MAX_LOSSES_PER_DAY:
            self.sleeve_status[signal.sleeve] = "blocked: two-loss daily stop reached"
            return False
        if self.day_pnl >= DAILY_PROFIT_LOCK:
            self.sleeve_status[signal.sleeve] = "blocked: $600 daily profit lock reached"
            return False
        if not self._quote_usable(signal.market, now_epoch):
            self.sleeve_status[signal.sleeve] = "blocked: fresh two-sided futures quote unavailable"
            return False
        if any(p.market == signal.market and p.side != signal.side for p in self.positions.values()):
            self.sleeve_status[signal.sleeve] = "blocked: opposite position in the same contract"
            self.fired.add(fired_key)
            return False

        cfg = SLEEVES[signal.sleeve]
        market = MARKETS[signal.market]
        quote = self._quotes[signal.market]
        tick, pv = market["tick"], market["point_value"]
        side_sign = Decimal("1") if signal.side == "long" else Decimal("-1")
        entry = (D(quote["ask"]) + tick) if signal.side == "long" else (D(quote["bid"]) - tick)
        stop = D(signal.stop)
        if (signal.side == "long" and stop >= entry) or (signal.side == "short" and stop <= entry):
            self.sleeve_status[signal.sleeve] = "rejected: stop is not beyond the executable entry"
            self.fired.add(fired_key)
            return False
        distance = abs(entry - stop)
        commission_rt = INSTRUMENTS[market["instrument"]].commission_per_side * Decimal("2")
        risk_per_contract = money((distance + tick) * pv + commission_rt)
        committed = self._committed_stop_risk()
        floor_room = max(Decimal("0"), self.balance - self.floor - SAFETY_RESERVE - committed - Decimal("0.01"))
        by_risk = int((cfg["risk"] / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))
        by_floor = int((floor_room / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))
        used_micros = sum(position.quantity for position in self.positions.values())
        quantity = max(0, min(by_risk, by_floor, self.rules.max_micros - used_micros))
        self.fired.add(fired_key)
        if quantity <= 0:
            self.sleeve_status[signal.sleeve] = "risk rejected: no whole micro fits remaining MLL room"
            self._note(f"REJECT {cfg['name']}: no whole micro fits the shared floor room", "warn")
            self._save()
            return False
        target = entry + side_sign * D(signal.reward_risk) * distance
        position = PaperPosition(
            id=uuid.uuid4().hex[:10], sleeve=signal.sleeve, market=signal.market,
            instrument=market["instrument"], side=signal.side, quantity=quantity,
            entry=str(entry), stop=str(stop), target=str(target),
            opened_at=datetime.fromtimestamp(now_epoch, UTC).isoformat(),
            activation_exchange_at=_quote_epoch(quote["at"]), last_quote_at=_quote_epoch(quote["at"]),
            risk_reserved=str(money(risk_per_contract * quantity)), entry_feed="TradingView continuous futures",
            entry_evidentiary=True, event_filter_verified=False, last_mark=str(entry),
        )
        self.positions[position.id] = position
        self.day_trades += 1
        self.sleeve_status[signal.sleeve] = f"OPEN {signal.side.upper()} {quantity} {position.instrument}"
        self._note(
            f"PAPER OPEN {cfg['name']} {signal.side.upper()} {quantity} {position.instrument} @ {entry} "
            f"stop {stop} target {target}", "open",
        )
        self._save()
        return True

    def _exit_price(self, position: PaperPosition, reason: str) -> Decimal | None:
        quote = self._quotes.get(position.market) or {}
        try:
            bid, ask = D(quote["bid"]), D(quote["ask"])
        except (KeyError, ArithmeticError):
            return None
        if bid <= 0 or ask <= bid:
            return None
        tick = MARKETS[position.market]["tick"]
        stop, target = D(position.stop), D(position.target)
        if position.side == "long":
            if reason == "stop":
                return min(stop, bid) - tick
            if reason == "target":
                return target - tick
            return bid - tick
        if reason == "stop":
            return max(stop, ask) + tick
        if reason == "target":
            return target + tick
        return ask + tick

    def _close_position(self, position_id: str, reason: str, *, evidentiary: bool = True) -> bool:
        position = self.positions.get(position_id)
        if position is None:
            return False
        exit_price = self._exit_price(position, reason)
        if exit_price is None:
            return False
        pv = MARKETS[position.market]["point_value"]
        gross = money(position.sign * (exit_price - D(position.entry)) * pv * position.quantity)
        commission = money(INSTRUMENTS[position.instrument].commission_per_side * Decimal("2") * position.quantity)
        net = money(gross - commission)
        self.balance = money(self.balance + net)
        self.day_pnl = money(self.day_pnl + net)
        if net < 0:
            self.day_losses += 1
        self.history.insert(0, {
            "id": position.id, "sleeve": position.sleeve, "name": SLEEVES[position.sleeve]["name"],
            "instrument": position.instrument, "side": position.side, "quantity": position.quantity,
            "entry": str(position.entry), "exit": str(exit_price), "stop": str(position.stop),
            "target": str(position.target), "gross_pnl": str(gross), "commission": str(commission),
            "net_pnl": str(net), "reason": reason, "opened_at": position.opened_at,
            "closed_at": datetime.now(UTC).isoformat(),
            "evidentiary": bool(evidentiary and position.entry_evidentiary and position.event_filter_verified),
            "evidence_note": "event filter was not point-in-time verified" if not position.event_filter_verified else "",
        })
        self.history = self.history[:400]
        self.positions.pop(position_id, None)
        self.sleeve_status[position.sleeve] = f"closed {reason}: {net:+.2f}"
        self._note(f"PAPER CLOSE {SLEEVES[position.sleeve]['name']} {reason} P&L ${net:+.2f}", "win" if net >= 0 else "loss")
        if self.balance <= self.floor:
            self.breached = True
        self._save()
        return True

    def _position_mark(self, position: PaperPosition) -> Decimal:
        quote = self._quotes.get(position.market) or {}
        try:
            bid, ask = D(quote["bid"]), D(quote["ask"])
            if bid <= 0 or ask <= bid:
                raise ArithmeticError("invalid two-sided quote")
            mark = bid if position.side == "long" else ask
        except (KeyError, ArithmeticError):
            mark = D(position.last_mark or position.entry)
        pv = MARKETS[position.market]["point_value"]
        commission = INSTRUMENTS[position.instrument].commission_per_side * Decimal("2") * position.quantity
        return money(position.sign * (mark - D(position.entry)) * pv * position.quantity - commission)

    def equity(self) -> Decimal:
        return money(self.balance + sum((self._position_mark(position) for position in self.positions.values()), Decimal("0")))

    def _manage_quote(self, market: str, quote: dict[str, Any]) -> None:
        try:
            quote_at = _quote_epoch(quote["at"])
            bid, ask = D(quote["bid"]), D(quote["ask"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return
        if bid <= 0 or ask <= bid:
            return
        changed = False
        for position in list(self.positions.values()):
            if position.market != market or quote_at <= max(position.activation_exchange_at, position.last_quote_at):
                continue
            position.last_quote_at = quote_at
            mark = bid if position.side == "long" else ask
            position.last_mark = str(mark)
            stop_hit = mark <= D(position.stop) if position.side == "long" else mark >= D(position.stop)
            target_hit = mark >= D(position.target) if position.side == "long" else mark <= D(position.target)
            # A single timestamp cannot prove target-before-stop; stop wins ambiguity.
            if stop_hit:
                changed = self._close_position(position.id, "stop") or changed
            elif target_hit:
                changed = self._close_position(position.id, "target") or changed
        if self.positions and self.equity() <= self.floor:
            for position in list(self.positions.values()):
                self._close_position(position.id, "intraday_mll", evidentiary=True)
            self.breached = True
            self.enabled = False
            self._note("maximum-loss floor touched by open equity; paper account stopped", "loss")
            changed = True
        if changed:
            self._save()

    def _force_close_all(self, reason: str, evidentiary: bool = True) -> bool:
        ok = True
        for position in list(self.positions.values()):
            ok = self._close_position(position.id, reason, evidentiary=evidentiary) and ok
        return ok

    def _end_session(
        self,
        day: date,
        *,
        reason: str = "session_close",
        evidentiary: bool = True,
    ) -> None:
        if self.current_session != day.isoformat():
            return
        if self.positions and not self._force_close_all(reason, evidentiary=evidentiary):
            self.feed_error = "session close is due but no executable quote is available"
            return
        if self.day_trades > 0:
            self.trading_days += 1
        self.highest_close = max(self.highest_close, self.balance)
        if self.highest_close > self.rules.trail_trigger:
            self.floor = money(self.rules.locked_floor)
            self.trail_locked = True
        else:
            self.floor = max(self.floor, money(self.highest_close - self.rules.max_loss))
        if self.balance <= self.floor:
            self.breached = True
            self.enabled = False
        if self.balance >= TARGET_BALANCE and not self.breached and self.trading_days >= self.rules.minimum_trading_days:
            self.passed = True
            self.enabled = False
        self.snapshots.insert(0, {
            "session": self.current_session, "balance": str(self.balance), "day_pnl": str(self.day_pnl),
            "floor": str(self.floor), "trades": self.day_trades, "losses": self.day_losses,
            "status": "BREACHED" if self.breached else ("PASSED" if self.passed else "ACTIVE"),
        })
        self.snapshots = self.snapshots[:180]
        self.current_session = ""
        self.day_pnl = Decimal("0")
        self.day_trades = self.day_losses = 0
        self._save()

    def _start_session(self, day: date) -> None:
        key = day.isoformat()
        if self.current_session == key:
            return
        if self.current_session:
            old = date.fromisoformat(self.current_session)
            self._end_session(old)
            if self.current_session:
                return
        self.current_session = key
        self.day_start_balance = self.balance
        self.day_pnl = Decimal("0")
        self.day_trades = self.day_losses = 0

    def ingest_update(
        self,
        market: str,
        frame: pd.DataFrame | None,
        quote: dict[str, Any] | None,
        feed_status: str,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        now_epoch = current.timestamp()
        self.feed_status = feed_status
        block = tv._lucid_feed_block_reason(feed_status)
        self.feed_realtime = not bool(block)
        self.feed_error = block
        if frame is not None and not frame.empty:
            self._frames[market] = self._normalise_frame(frame)
        if quote:
            self._quotes[market] = dict(quote)
            self._manage_quote(market, quote)
        if set(self._frames) != set(MARKETS):
            return

        day = current.astimezone(NY).date()
        if self.current_session and self.current_session != day.isoformat():
            # A restart/outage may span the intended flat time. Liquidate conservatively
            # on the first usable later quote, but never count that delayed exit as evidence.
            old = date.fromisoformat(self.current_session)
            self._end_session(old, reason="missed_session_close", evidentiary=False)
            if self.current_session:
                for sleeve in SLEEVES:
                    self.sleeve_status[sleeve] = "blocked: prior session could not be flattened"
                return
        session = self._session_row(day)
        if session is None:
            for sleeve in SLEEVES:
                self.sleeve_status[sleeve] = "blocked: verified calendar says closed or is unavailable"
            return
        open_minute = int(session.get("open_minute") or 570)
        close_minute = int(session.get("close_minute") or 960)
        minute_now = current.astimezone(NY).hour * 60 + current.astimezone(NY).minute
        if minute_now >= close_minute:
            self._end_session(day)
            for sleeve in SLEEVES:
                self.sleeve_status[sleeve] = "session finished"
            return
        if minute_now < open_minute:
            for sleeve in SLEEVES:
                self.sleeve_status[sleeve] = "waiting for 09:30 New York"
            return
        self._start_session(day)

        latest = []
        for key in MARKETS:
            rows = self._frames[key]
            if rows.empty:
                return
            latest.append(pd.Timestamp(rows.iloc[-1]["dt_utc"]))
        current_at = min(latest)
        minute_key = _iso(current_at)
        age = now_epoch - _epoch(current_at)
        if not self._primed:
            self._last_processed_minute = minute_key
            self._primed = True
            for sleeve in SLEEVES:
                self.sleeve_status[sleeve] = "primed; waiting for the next completed bar"
            return
        if minute_key == self._last_processed_minute:
            return
        self._last_processed_minute = minute_key
        if age < -2 or age > ENTRY_MAX_AGE_SEC:
            for sleeve in SLEEVES:
                if self.sleeve_status[sleeve].startswith("signal confirmed"):
                    self.sleeve_status[sleeve] = "blocked: next-minute entry bar arrived stale"
            return
        signals = self._signals_for_minute(current_at, close_minute)
        for signal in signals:  # generator order is the frozen ES, NQ-drive, NQ-prior priority.
            self._open_signal(signal, current_at, now_epoch)
        self._save()

    def state(self) -> dict[str, Any]:
        equity = self.equity()
        blockers = []
        if self.persistence_error:
            blockers.append("paper-state persistence is unhealthy")
        if not self.feed_realtime:
            blockers.append(self.feed_error or "realtime futures feed is not confirmed")
        today = datetime.now(UTC).astimezone(NY).date().isoformat()
        if today not in self.calendar:
            blockers.append(self.calendar_status or "verified US session calendar is unavailable")
        positions = []
        for position in self.positions.values():
            row = asdict(position)
            row["name"] = SLEEVES[position.sleeve]["name"]
            row["unrealized_pnl"] = str(self._position_mark(position))
            positions.append(row)
        sleeve_rows = []
        for key, cfg in SLEEVES.items():
            sleeve_history = [row for row in self.history if row.get("sleeve") == key]
            sleeve_rows.append({
                "id": key, "name": cfg["name"], "instrument": MARKETS[cfg["market"]]["instrument"],
                "risk_budget": str(cfg["risk"]), "reward_risk": str(cfg["rr"]),
                "status": self.sleeve_status.get(key, "scanning"),
                "open_positions": sum(1 for row in self.positions.values() if row.sleeve == key),
                "closed_trades": len(sleeve_history),
                "net_pnl": str(money(sum((D(row["net_pnl"]) for row in sleeve_history), Decimal("0")))),
            })
        evidentiary = sum(1 for row in self.history if row.get("evidentiary"))
        return {
            "ok": True, "running": True, "paper_only": True, "live_order_routing": False,
            "enabled": self.enabled, "strategy_version": STRATEGY_VERSION,
            "status": (
                "BREACHED" if self.breached else
                ("PASSED" if self.passed else
                 ("PAUSED" if not self.enabled else ("BLOCKED" if blockers else "SCANNING")))
            ),
            "blocking_reasons": blockers,
            "balance": str(self.balance), "equity": str(equity), "net_pnl": str(money(equity - STARTING_BALANCE)),
            "starting_balance": str(STARTING_BALANCE), "target_balance": str(TARGET_BALANCE),
            "target_remaining": str(max(Decimal("0"), money(TARGET_BALANCE - equity))),
            "floor": str(self.floor), "drawdown_room": str(money(equity - self.floor)),
            "trail_locked": self.trail_locked, "current_session": self.current_session,
            "day_pnl": str(self.day_pnl), "day_trades": self.day_trades, "day_losses": self.day_losses,
            "max_micros": self.rules.max_micros,
            "open_micros": sum(position.quantity for position in self.positions.values()),
            "committed_stop_risk": str(self._committed_stop_risk()), "trading_days": self.trading_days,
            "feed_status": self.feed_status, "feed_realtime": self.feed_realtime, "feed_error": self.feed_error,
            "calendar_status": self.calendar_status, "event_filter_status": self.event_filter_status,
            "persistence_error": self.persistence_error, "sleeves": sleeve_rows, "positions": positions,
            "history": self.history[:100], "log": self.log[:60], "snapshots": self.snapshots[:60],
            "forward_evidence": {
                "closed_trades": len(self.history), "evidentiary_trades": evidentiary,
                "status": "COLLECTING" if evidentiary < 60 else "READY_FOR_INDEPENDENT_AUDIT",
                "note": "Paper P&L is not proof of profitability; trades without a verified event filter are excluded.",
            },
        }


async def _stream_market(market: str, queue: asyncio.Queue) -> None:
    """Stream 1-minute chart bars plus executable quote fields for one market."""
    spec = MARKETS[market]
    headers = {"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"}
    auth = getattr(config, "LUCID_TRADINGVIEW_AUTH_TOKEN", "unauthorized_user_token")
    while True:
        chart_session = tv._tv_session("cs")
        quote_session = tv._tv_session("qs")
        rows: dict[int, list] = {}
        raw_df = pd.DataFrame()
        quote: dict[str, Any] = {}
        stream_mode = "connecting"
        history_ready = False
        try:
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(), family=socket.AF_INET, ttl_dns_cache=300,
            )
            async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
                async with session.ws_connect(
                    tv.TV_WS_URL, heartbeat=20, timeout=aiohttp.ClientTimeout(total=30),
                ) as ws:
                    async def send(method: str, params: list) -> None:
                        await ws.send_str(tv._tv_frame({"m": method, "p": params}))

                    await send("set_auth_token", [auth])
                    await send("chart_create_session", [chart_session, ""])
                    await send("quote_create_session", [quote_session])
                    await send("quote_set_fields", [quote_session, "lp", "lp_time", "bid", "ask", "rtc"])
                    await send("resolve_symbol", [chart_session, "symbol_1", tv._tv_resolve_expr(spec["tv_symbol"])])
                    await send("create_series", [chart_session, "s1", "s1", "symbol_1", "1", 10000])
                    await send("quote_add_symbols", [quote_session, spec["tv_symbol"]])
                    while True:
                        message = await asyncio.wait_for(ws.receive(), timeout=tv.TV_STALE_SECONDS)
                        if message.type != aiohttp.WSMsgType.TEXT:
                            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                raise RuntimeError("TradingView websocket closed")
                            continue
                        raw = message.data
                        if raw.startswith("~m~4~m~~h~") or "~h~" in raw:
                            await ws.send_str(raw)
                        frame_changed = quote_changed = False
                        for packet in tv._tv_messages(raw):
                            method, params = packet.get("m"), packet.get("p") or []
                            if method == "timescale_update" and len(params) >= 2:
                                data = (params[1] or {}).get("s1") or {}
                                for item in data.get("s") or []:
                                    try:
                                        rows[int(item["i"])] = item["v"]
                                    except Exception:
                                        continue
                                if rows:
                                    raw_df = tv._tv_rows_to_frame(rows)
                                    history_ready = len(rows) >= 400
                                    frame_changed = True
                            elif method == "series_completed" and len(params) >= 3:
                                stream_mode = str(params[2])
                            elif method == "qsd" and len(params) >= 2:
                                item = params[1] or {}
                                if item.get("n") != spec["tv_symbol"]:
                                    continue
                                values = item.get("v") or {}
                                if values.get("bid") is not None:
                                    quote["bid"] = float(values["bid"])
                                if values.get("ask") is not None:
                                    quote["ask"] = float(values["ask"])
                                if values.get("lp") is not None:
                                    quote["last"] = float(values["lp"])
                                if values.get("lp_time") is not None:
                                    quote["at"] = _quote_epoch(values["lp_time"])
                                quote_changed = bool(quote.get("at") and quote.get("bid") and quote.get("ask"))
                        if history_ready and (frame_changed or quote_changed):
                            status = f"TradingView websocket ({stream_mode})"
                            await queue.put((market, raw_df if frame_changed else None, dict(quote) if quote_changed else None, status))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put((market, None, None, f"TradingView websocket reconnecting: {type(exc).__name__}"))
            await asyncio.sleep(2)


async def manage_loop(bot: LucidLabPaperBot) -> None:
    """Run the public-data paper feed forever; real order APIs are never called."""
    queue: asyncio.Queue = asyncio.Queue()
    tasks = [asyncio.create_task(_stream_market(market, queue)) for market in MARKETS]
    statuses: dict[str, str] = {}
    try:
        while True:
            now = datetime.now(UTC)
            if time.time() - bot.calendar_refreshed_at >= CALENDAR_REFRESH_SEC:
                sessions, error = await penny_quotes.fetch_calendar(
                    now.astimezone(NY).date() - timedelta(days=10),
                    now.astimezone(NY).date() + timedelta(days=10),
                )
                bot.set_calendar(sessions, error)
            try:
                market, frame, quote, status = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                if statuses:
                    combined = tv._combine_tradingview_statuses(statuses)
                    # Drive session boundaries even when a quiet feed emits no update.
                    bot.ingest_update("es", None, None, combined, now=now)
                continue
            statuses[market] = status
            combined = tv._combine_tradingview_statuses(statuses)
            bot.ingest_update(market, frame, quote, combined)
    finally:
        for task in tasks:
            task.cancel()
