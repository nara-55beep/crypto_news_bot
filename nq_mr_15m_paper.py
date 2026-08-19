"""BTC 1-minute port of the ``NQ 15m Mean Reversion`` paper bundle.

The display name is intentionally retained because it is how the strategy is
identified on the Paper Trading page. The instrument and execution model are
not retained: this bot trades BTC/USDT only, from closed Binance one-minute
candles, with a fresh $100 isolated paper account and at most 20x notional.

The three causal setup families from the former NQ implementation remain:

* an intraday VWAP two-standard-deviation fade;
* a Turtle Soup false break of a prior 20-day extreme; and
* an 80-20 reversal based on the preceding UTC day.

There is never more than one real position. Signals are detected from a closed
bar and filled at that bar's close. No broker or private exchange endpoint
exists in this module.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import pandas as pd
import requests

import config


NAME = "NQ 15m Mean Reversion (paper)"
SYMBOL = "BTCUSDT"
SYMBOL_LABEL = "BTC/USDT"
INTERVAL = "1m"
START_BALANCE = 100.0
LEVERAGE = 20.0
FEE_RATE = 0.0  # the dashboard's intended paper venue (Lighter) is zero-fee
POLL_SEC = 15
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

VWAP_K = 2.0
VWAP_MIN_BARS = 20
TURTLE_LOOKBACK = 20
TURTLE_BUFFER_PCT = 0.00025
EIGHTY_BUFFER_PCT = 0.00025


@dataclass
class BTCMRPosition:
    id: str
    strat: str
    side: str
    qty: float
    qty0: float
    entry: float
    stop: float
    stop0: float
    liquidation: float
    tp1: float
    target: float
    r_points: float
    margin: float
    opened_at: float
    opened_bar: int
    best: float
    partial_done: bool = False
    realized: float = 0.0
    note: str = ""


def _request_klines(**params) -> list:
    response = requests.get(BINANCE_KLINES, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected Binance response: {payload!r}")
    return payload


def _rows(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    frame["dt_utc"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["close_time"] = pd.to_numeric(frame["close_time"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[frame["close_time"] < int(time.time() * 1000)]
    return (
        frame[["dt_utc", "open", "high", "low", "close", "volume"]]
        .dropna()
        .drop_duplicates("dt_utc")
        .sort_values("dt_utc")
        .reset_index(drop=True)
    )


def _fetch_binance() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch the current UTC day's closed one-minute bars and prior daily bars."""
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    cursor = int(midnight.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    minute_rows: list = []
    while cursor < end_ms:
        batch = _request_klines(
            symbol=SYMBOL, interval=INTERVAL, startTime=cursor, endTime=end_ms, limit=1000,
        )
        if not batch:
            break
        minute_rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break

    minute = _rows(minute_rows)
    daily = _rows(_request_klines(symbol=SYMBOL, interval="1d", limit=40))
    if not daily.empty:
        daily["day"] = daily["dt_utc"].dt.date
        daily = daily[daily["day"] < now.date()].reset_index(drop=True)
    return minute, daily


class NQMR15PaperBot:
    """Compatibility class name used by ``main.py`` and ``dashboard.py``."""

    NAME = NAME

    def __init__(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.position: BTCMRPosition | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.price = 0.0
        self.status = "starting..."
        self.data_error = ""
        self.fired_keys: set[str] = set()
        self._last_bar_ts = 0
        self._load()

    def _path(self) -> str:
        # Never restore the old $50k MNQ ledger into this $100 BTC account.
        return os.path.join(config.DATA_DIR, "nq_mr_btc_1m_state_v1.json")

    def _save(self) -> None:
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            temporary = self._path() + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": 1,
                    "enabled": self.enabled,
                    "balance": self.balance,
                    "position": asdict(self.position) if self.position else None,
                    "fired_keys": sorted(self.fired_keys)[-80:],
                    "history": self.history[:160],
                    "log": self.log[:100],
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path())
        except Exception as exc:
            self.data_error = f"state save failed: {type(exc).__name__}: {exc}"

    def _load(self) -> None:
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as handle:
                payload = json.load(handle)
            if int(payload.get("schema", 0)) != 1:
                return
            self.enabled = bool(payload.get("enabled", True))
            self.balance = float(payload.get("balance", START_BALANCE))
            raw_position = payload.get("position")
            self.position = BTCMRPosition(**raw_position) if raw_position else None
            self.fired_keys = set(payload.get("fired_keys") or [])
            self.history = payload.get("history") or []
            self.log = payload.get("log") or []
        except Exception as exc:
            self.data_error = f"state restore ignored: {type(exc).__name__}: {exc}"

    def _note(self, message: str, kind: str = "info") -> None:
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": message})
        self.log = self.log[:100]
        print(f"[nq_mr_btc_1m] {message}")

    async def manage_loop(self) -> None:
        await asyncio.sleep(7.0)
        while True:
            try:
                minute, daily = await asyncio.get_running_loop().run_in_executor(None, _fetch_binance)
                self.data_error = ""
                self._tick(minute, daily)
                self._save()
            except Exception as exc:
                self.data_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.status = "data error"
                self._save()
            await asyncio.sleep(POLL_SEC)

    def _tick(self, minute: pd.DataFrame, daily: pd.DataFrame) -> None:
        if minute.empty:
            self.status = "waiting for closed BTC 1m candles"
            return
        current = minute.iloc[-1]
        self.price = float(current["close"])
        bar_ts = int(pd.Timestamp(current["dt_utc"]).timestamp())
        if bar_ts == self._last_bar_ts:
            self._set_status()
            return
        self._last_bar_ts = bar_ts

        if self.position is not None:
            self._manage(current)

        day_key = str(pd.Timestamp(current["dt_utc"]).date())
        self.fired_keys = {key for key in self.fired_keys if key.startswith(day_key + ":")}
        if self.enabled and self.position is None:
            for signal in self._signals(minute, daily):
                fired_key = f"{day_key}:{signal['strat']}"
                if fired_key in self.fired_keys:
                    continue
                self.fired_keys.add(fired_key)
                self._open(signal, current)
                break  # cap total account exposure at one 20x position
        self._set_status()

    def _signals(self, minute: pd.DataFrame, daily: pd.DataFrame) -> list[dict]:
        out: list[dict] = []
        out.extend(self._vwap_signal(minute))
        out.extend(self._turtle_signal(minute.iloc[-1], daily))
        out.extend(self._eighty_twenty_signal(minute, daily))
        return out

    def _vwap_signal(self, minute: pd.DataFrame) -> list[dict]:
        if len(minute) < VWAP_MIN_BARS:
            return []
        typical = (minute["high"] + minute["low"] + minute["close"]) / 3.0
        volume = minute["volume"].where(minute["volume"] > 0, 1.0)
        total = float(volume.sum())
        vwap = float((typical * volume).sum() / total)
        variance = max(float((typical.pow(2) * volume).sum() / total) - vwap * vwap, 0.0)
        sigma = math.sqrt(variance)
        if sigma <= 0:
            return []
        current = minute.iloc[-1]
        upper, lower = vwap + VWAP_K * sigma, vwap - VWAP_K * sigma
        if float(current["high"]) >= upper:
            return [self._signal("VWAP2s", -1, vwap + 3.0 * sigma, vwap,
                                 f"fade +2 sigma stretch to UTC-session VWAP {vwap:.2f}")]
        if float(current["low"]) <= lower:
            return [self._signal("VWAP2s", 1, vwap - 3.0 * sigma, vwap,
                                 f"fade -2 sigma stretch to UTC-session VWAP {vwap:.2f}")]
        return []

    def _turtle_signal(self, current: pd.Series, daily: pd.DataFrame) -> list[dict]:
        if len(daily) < TURTLE_LOOKBACK:
            return []
        lookback = daily.tail(TURTLE_LOOKBACK)
        prior_low = float(lookback["low"].min())
        prior_high = float(lookback["high"].max())
        close = float(current["close"])
        if float(current["low"]) < prior_low and close > prior_low:
            stop = float(current["low"]) * (1.0 - TURTLE_BUFFER_PCT)
            return [self._signal("TurtleSoup", 1, stop, close + 2.0 * (close - stop),
                                 "false break of prior 20-day BTC low")]
        if float(current["high"]) > prior_high and close < prior_high:
            stop = float(current["high"]) * (1.0 + TURTLE_BUFFER_PCT)
            return [self._signal("TurtleSoup", -1, stop, close - 2.0 * (stop - close),
                                 "false break of prior 20-day BTC high")]
        return []

    def _eighty_twenty_signal(self, minute: pd.DataFrame, daily: pd.DataFrame) -> list[dict]:
        if daily.empty:
            return []
        yesterday = daily.iloc[-1]
        span = float(yesterday["high"] - yesterday["low"])
        if span <= 0:
            return []
        opened_top = float(yesterday["open"]) >= float(yesterday["high"]) - 0.2 * span
        closed_bottom = float(yesterday["close"]) <= float(yesterday["low"]) + 0.2 * span
        opened_bottom = float(yesterday["open"]) <= float(yesterday["low"]) + 0.2 * span
        closed_top = float(yesterday["close"]) >= float(yesterday["high"]) - 0.2 * span
        current = minute.iloc[-1]
        close = float(current["close"])
        if opened_top and closed_bottom:
            level = float(yesterday["low"])
            pushed = float(minute["low"].min()) <= level * (1.0 - EIGHTY_BUFFER_PCT)
            if pushed and close >= level:
                stop = float(minute["low"].min()) * (1.0 - EIGHTY_BUFFER_PCT)
                return [self._signal("80-20", 1, stop, close + 2.0 * (close - stop),
                                     "prior UTC-day bullish 80-20 reversal")]
        if opened_bottom and closed_top:
            level = float(yesterday["high"])
            pushed = float(minute["high"].max()) >= level * (1.0 + EIGHTY_BUFFER_PCT)
            if pushed and close <= level:
                stop = float(minute["high"].max()) * (1.0 + EIGHTY_BUFFER_PCT)
                return [self._signal("80-20", -1, stop, close - 2.0 * (stop - close),
                                     "prior UTC-day bearish 80-20 reversal")]
        return []

    @staticmethod
    def _signal(strat: str, direction: int, stop: float, target: float, note: str) -> dict:
        return {
            "strat": strat, "side": "long" if direction > 0 else "short",
            "stop": float(stop), "target": float(target), "note": note,
        }

    def _open(self, signal: dict, current: pd.Series) -> None:
        equity = self.equity()
        entry = float(current["close"])
        if equity <= 0 or entry <= 0:
            return
        direction = 1 if signal["side"] == "long" else -1
        notional = equity * LEVERAGE
        qty = notional / entry
        liquidation = entry * (1.0 - 1.0 / LEVERAGE) if direction > 0 else entry * (1.0 + 1.0 / LEVERAGE)
        stop = float(signal["stop"])
        stop = max(stop, liquidation) if direction > 0 else min(stop, liquidation)
        r_points = abs(entry - stop)
        target = float(signal["target"])
        if (direction > 0 and target <= entry) or (direction < 0 and target >= entry):
            target = entry + direction * max(2.0 * r_points, entry * 0.001)
        tp1 = entry + direction * r_points
        self.balance -= notional * FEE_RATE
        self.position = BTCMRPosition(
            id=uuid.uuid4().hex[:6], strat=signal["strat"], side=signal["side"],
            qty=qty, qty0=qty, entry=entry, stop=stop, stop0=stop,
            liquidation=liquidation, tp1=tp1, target=target, r_points=r_points,
            margin=equity, opened_at=time.time(),
            opened_bar=int(pd.Timestamp(current["dt_utc"]).timestamp()), best=entry,
            note=signal["note"],
        )
        self._note(
            f"OPEN {signal['strat']} {signal['side'].upper()} BTC @ ${entry:,.2f} · "
            f"{qty:.6f} BTC · ${notional:,.2f} notional (20x)", "open",
        )

    def _manage(self, bar: pd.Series) -> None:
        position = self.position
        if position is None:
            return
        direction = 1 if position.side == "long" else -1
        high, low = float(bar["high"]), float(bar["low"])
        position.best = max(position.best, high) if direction > 0 else min(position.best, low)
        liquidated = low <= position.liquidation if direction > 0 else high >= position.liquidation
        stopped = low <= position.stop if direction > 0 else high >= position.stop
        target_hit = high >= position.target if direction > 0 else low <= position.target
        tp1_hit = high >= position.tp1 if direction > 0 else low <= position.tp1
        if liquidated:
            self._close(position.liquidation, "liquidation")
        elif stopped:
            self._close(position.stop, "stop")
        elif target_hit:
            self._close(position.target, "target")
        elif tp1_hit and not position.partial_done:
            self._partial(position.qty0 * 0.5, position.tp1)
            if self.position:
                self.position.partial_done = True
                self.position.stop = self.position.entry
                self._note("+1R · half closed, remaining stop moved to breakeven")

    @staticmethod
    def _pnl(position: BTCMRPosition, exit_price: float, qty: float) -> float:
        direction = 1 if position.side == "long" else -1
        return direction * (exit_price - position.entry) * qty

    def _partial(self, qty: float, exit_price: float) -> None:
        position = self.position
        if position is None:
            return
        qty = min(qty, position.qty)
        pnl = self._pnl(position, exit_price, qty) - exit_price * qty * FEE_RATE
        self.balance += pnl
        position.qty -= qty
        position.realized += pnl
        self.history.insert(0, {
            "mkt": f"BTC.{position.strat}", "side": position.side,
            "entry": round(position.entry, 2), "exit": round(exit_price, 2),
            "qty": round(qty, 8), "pnl": round(pnl, 2), "reason": "tp1",
            "closed_at": time.time(),
        })

    def _close(self, exit_price: float, reason: str) -> None:
        position = self.position
        if position is None:
            return
        pnl = self._pnl(position, exit_price, position.qty) - exit_price * position.qty * FEE_RATE
        total = position.realized + pnl
        self.balance = max(0.0, self.balance + pnl)
        self.history.insert(0, {
            "mkt": f"BTC.{position.strat}", "side": position.side,
            "entry": round(position.entry, 2), "exit": round(exit_price, 2),
            "qty": round(position.qty, 8), "pnl": round(pnl, 2), "reason": reason,
            "closed_at": time.time(),
        })
        self._note(
            f"CLOSE {position.strat} BTC @ ${exit_price:,.2f} · {reason} · cycle ${total:+.2f}",
            "win" if total >= 0 else "loss",
        )
        self.position = None

    def _set_status(self) -> None:
        if not self.enabled:
            self.status = "paused"
        elif self.position:
            self.status = f"in trade: {self.position.strat} {self.position.side}"
        elif self.balance <= 0:
            self.status = "account liquidated · reset required"
        else:
            self.status = "live · scanning BTC 1m mean-reversion signals"

    def equity(self) -> float:
        equity = self.balance
        if self.position and self.price:
            equity += self._pnl(self.position, self.price, self.position.qty)
        return max(0.0, equity)

    def set_enabled(self, on) -> dict:
        self.enabled = bool(on)
        self._note("bot ENABLED · BTC 1m · $100 · 20x" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self) -> dict:
        self.enabled = True
        self.balance = START_BALANCE
        self.position = None
        self.history = []
        self.log = []
        self.price = 0.0
        self.fired_keys = set()
        self._last_bar_ts = 0
        self._note("bot reset and enabled · BTC paper account back to $100")
        self._save()
        return {"ok": True, "enabled": True}

    def state(self) -> dict:
        equity = self.equity()
        wins = sum(1 for item in self.history if item.get("pnl", 0) > 0)
        positions: list[dict] = []
        if self.position:
            position = self.position
            unrealized = self._pnl(position, self.price or position.entry, position.qty)
            risk_usd = position.r_points * position.qty0
            positions.append({
                "mkt": f"BTC.{position.strat}", "side": position.side,
                "entry": round(position.entry, 2), "qty": round(position.qty, 8),
                "stop": round(position.stop, 2), "tp1": round(position.tp1, 2),
                "tp2": round(position.target, 2),
                "pnl": round(unrealized + position.realized, 2),
                "pnl_R": round((unrealized + position.realized) / max(risk_usd, 1e-9), 2),
                "news": position.note + (" · runner at breakeven" if position.partial_done else ""),
            })
        return {
            "running": True, "enabled": self.enabled, "name": self.NAME,
            "status": self.status, "symbols": SYMBOL_LABEL, "timeframe": INTERVAL,
            "balance": round(self.balance, 2), "equity": round(equity, 2),
            "start_balance": START_BALANCE, "leverage": LEVERAGE,
            "total_pnl": round(equity - START_BALANCE, 2),
            "total_pnl_pct": round((equity / START_BALANCE - 1.0) * 100.0, 2),
            "phase": "BTC 1m · isolated 20x", "risk_per_trade": START_BALANCE,
            "trades": len(self.history), "wins": wins, "positions": positions,
            "setups": [], "history": self.history[:80], "log": self.log[:30],
            "data_error": self.data_error,
            "price": round(self.price, 2) if self.price else None,
            "backtest_note": "BTC-only 1m paper port; the displayed NQ name is retained for continuity.",
        }
