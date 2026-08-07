"""
engine.py — the PAPER trading engine (STRATEGY V2).

Runs in a background thread. Every poll it:
  1. pulls fresh 15m + 4H candles per symbol,
  2. manages an open position the V2 way (partial at +1R -> breakeven -> ATR trail ->
     time stop),
  3. when flat and the risk guardrails allow, takes a V2 signal on the last CLOSED bar,
  4. records signals/trades/equity to the database.

No real orders here (that's the live engine). Fills are simulated at the current price
with slippage + fees, so paper results stay comparable to the backtest.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from backend.data.fetcher import fetch_ohlcv_df
from backend.exchange import make_exchange
from backend.risk.manager import RiskManager
from backend.strategy import indicators as ind
from backend.strategy.signal import Strategy, StrategyV3, make_strategy
from backend.utils.logger import get_logger

log = get_logger("paper")

_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
           "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}


@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    qty: float          # remaining quantity
    qty0: float         # original quantity
    r: float            # 1R distance (price)
    stop: float
    opened_at: str
    opened_epoch: float
    best: float
    take: float = 0.0   # fixed take-profit at rr*R
    partial_done: bool = False
    reason: str = ""

    def unrealized(self, price: float) -> float:
        return self.qty * (price - self.entry) if self.side == "long" else self.qty * (self.entry - price)


class PaperEngine:
    mode = "paper"

    def __init__(self, settings, db, exchange=None):
        self.s = settings
        self.db = db
        self.exchange = exchange or make_exchange(settings)
        self.strategy = make_strategy(settings)   # v2 or v3 per settings.strategy_version
        self.risk = RiskManager(settings)

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.running = False
        self.balance = settings.start_balance
        self.positions: Dict[str, Position] = {}
        self.prices: Dict[str, float] = {}
        self.latest_signal: Optional[dict] = None
        self.status = "stopped"
        self.paused_reason = ""

        self._day_key = datetime.now(timezone.utc).date()
        self._day_start_equity = self.balance
        self._last_loss_ts: Optional[float] = None

    # ------------------------------- control ----------------------------------
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self.status = "running"
        self._thread = threading.Thread(target=self._loop, name=f"{self.mode}-engine", daemon=True)
        self._thread.start()
        self._log("info", f"{self.mode} engine started (Strategy {self.s.strategy_version.upper()})")

    def stop(self) -> None:
        self.running = False
        self.status = "stopped"
        self._stop.set()
        self._log("info", f"{self.mode} engine stopped")

    def emergency_stop(self) -> None:
        with self._lock:
            for sym, pos in list(self.positions.items()):
                self._close_position(sym, self.prices.get(sym, pos.entry), "emergency_stop")
        self.stop()
        self._log("warn", "EMERGENCY STOP — all positions flattened")

    # ------------------------------- main loop --------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:
                log.exception("step failed: %s", e)
                self._log("error", f"step failed: {type(e).__name__}: {e}")
            self._stop.wait(self.s.poll_seconds)

    def step(self) -> None:
        self._rollover_day()
        ver = self.s.strategy_version
        v3 = ver == "v3"
        v4 = ver == "v4"
        strat = make_strategy(self.s)                      # pick up runtime strategy switches
        if v3:
            tf = htf_tf = self.s.v3_timeframe
            need = self.s.v3_regime_ma + self.s.v3_donchian + 60
        elif v4:
            tf = htf_tf = self.s.v4_timeframe              # V4 is single-timeframe
            need = max(self.s.v4_ema_slow, self.s.v4_sweep_lookback, self.s.v4_atr_period, self.s.v4_vol_ma) + 80
        else:
            tf, htf_tf = self.s.entry_timeframe, self.s.htf_timeframe
            need = max(self.s.ema_slow, self.s.atr_floor_win) + 80
        for symbol in self.s.symbol_list:
            try:
                entry_df = fetch_ohlcv_df(self.exchange, symbol, tf, limit=need)
                htf_df = entry_df if (v3 or v4) else fetch_ohlcv_df(self.exchange, symbol, htf_tf, limit=need)
            except Exception as e:
                self._log("error", f"data fetch failed for {symbol}: {e}")
                continue

            price = float(entry_df["close"].iloc[-1])
            hi = float(entry_df["high"].iloc[-1])
            lo = float(entry_df["low"].iloc[-1])
            atr_now = float(ind.atr(entry_df, self.s.atr_period).iloc[-1])
            ema_now = float(ind.ema(entry_df["close"], self.s.v3_regime_ma).iloc[-1]) if v3 else None
            with self._lock:
                self.prices[symbol] = price

            if symbol in self.positions:
                self._manage_position(symbol, price, hi, lo, atr_now, ema_now)

            if symbol not in self.positions:
                funding = self.exchange.fetch_funding_rate(symbol) if self.s.use_funding_filter else None
                sig = strat.latest_signal(htf_df, entry_df, symbol=symbol, funding=funding)
                if sig is not None:
                    self._handle_signal(sig, price)

        self._record_equity()
        self._update_status()

    # ------------------------------- entries ----------------------------------
    def _handle_signal(self, sig, price: float) -> None:
        sig_dict = {"time": sig.time, "symbol": sig.symbol, "side": sig.side,
                    "price": sig.price, "reason": sig.reason, "meta": sig.meta}
        with self._lock:
            self.latest_signal = sig_dict
            equity = self._equity_locked()
            day_pnl_pct = (equity - self._day_start_equity) / self._day_start_equity
            ok, why = self.risk.check_can_open(time.time(), len(self.positions), day_pnl_pct, self._last_loss_ts)
            if not ok:
                self.paused_reason = why
                self.db.insert_signal(sig_dict, taken=False)
                return
            ver = self.s.strategy_version
            v3 = ver == "v3"
            v4 = ver == "v4"
            entry_px = price * (1 + self.s.slippage) if sig.side == "long" else price * (1 - self.s.slippage)
            if v4:                                        # V4 = ATR stop + fixed R target; skip too-wide stops
                r = sig.atr * self.s.v4_atr_stop
                if entry_px <= 0 or (r / entry_px) > self.s.v4_max_stop_pct:
                    self.db.insert_signal(sig_dict, taken=False)
                    return
                if sig.side == "long":
                    stop, take = entry_px - r, entry_px + self.s.v4_rr * r
                else:
                    stop, take = entry_px + r, entry_px - self.s.v4_rr * r
            elif v3:                                      # V3 = long-only, chandelier trail, no fixed TP
                r = sig.atr * self.s.v3_atr_stop
                stop, take = entry_px - r, 0.0
            elif sig.side == "long":                      # V2 = fixed bracket at rr*R
                r = sig.atr * self.s.atr_stop_mult
                stop, take = entry_px - r, entry_px + self.s.rr * r
            else:
                r = sig.atr * self.s.atr_stop_mult
                stop, take = entry_px + r, entry_px - self.s.rr * r
            qty = self.risk.position_size(equity, entry_px, stop)
            if qty <= 0 or r <= 0:
                self.db.insert_signal(sig_dict, taken=False)
                return
            self.balance -= entry_px * qty * self.s.fee_rate
            self.positions[sig.symbol] = Position(
                symbol=sig.symbol, side=sig.side, entry=entry_px, qty=qty, qty0=qty, r=r,
                stop=stop, take=take, opened_at=_now_iso(), opened_epoch=time.time(),
                best=entry_px, reason=sig.reason,
            )
        self.db.insert_signal(sig_dict, taken=True)
        self._on_open(sig.symbol, sig.side, qty, entry_px)
        self._log("trade", f"OPEN {sig.side.upper()} {sig.symbol} @ {entry_px:.2f} · stop {stop:.2f} · "
                           f"TP {take:.2f} ({self.s.rr:g}R) · {sig.reason}")

    # ------------------------------- management -------------------------------
    def _manage_position(self, symbol: str, price: float, hi: float, lo: float,
                         atr_now: float, ema_now: float | None = None) -> None:
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return
            long = pos.side == "long"
            if self.s.strategy_version == "v3":
                # V3 = chandelier ATR trailing stop, ride until the trend bends; also exit
                # if the daily close drops below the regime MA. Long-only.
                pos.best = max(pos.best, hi)
                pos.stop = max(pos.stop, pos.best - self.s.v3_trail_atr * atr_now)
                if lo <= pos.stop:
                    self._close_position(symbol, pos.stop, "trail_stop")
                elif ema_now is not None and price < ema_now:
                    self._close_position(symbol, price, "regime_exit")
                return
            # V2 = fixed bracket (stop at 1R, take-profit at rr*R), stop-first.
            if (long and lo <= pos.stop) or (not long and hi >= pos.stop):
                self._close_position(symbol, pos.stop, "stop")
            elif (long and hi >= pos.take) or (not long and lo <= pos.take):
                self._close_position(symbol, pos.take, "take_profit")

    def _partial_close(self, symbol: str, exit_price: float, frac: float) -> None:
        """Caller holds the lock. Closes `frac` of the position, banks it, keeps the rest."""
        pos = self.positions.get(symbol)
        if pos is None:
            return
        q = pos.qty * frac
        fill = exit_price * (1 - self.s.slippage) if pos.side == "long" else exit_price * (1 + self.s.slippage)
        pnl = q * ((fill - pos.entry) if pos.side == "long" else (pos.entry - fill)) - fill * q * self.s.fee_rate
        self.balance += pnl
        pos.qty -= q
        notional = pos.entry * q
        self.db.insert_trade({
            "mode": self.mode, "symbol": symbol, "side": pos.side, "entry_time": pos.opened_at,
            "entry_price": round(pos.entry, 4), "qty": round(q, 6), "stop": round(pos.stop, 4), "take": None,
            "exit_time": _now_iso(), "exit_price": round(fill, 4), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / notional * 100, 3) if notional else 0.0,
            "fees": round(fill * q * self.s.fee_rate, 4), "reason": "partial_1R",
        })
        self._on_close(symbol, pos.side, q, fill)
        self._log("trade", f"PARTIAL {pos.side.upper()} {symbol} @ {fill:.2f} (+1R) · banked {pnl:+.2f} · stop->BE")

    def _close_position(self, symbol: str, exit_price: float, reason: str) -> None:
        """Caller holds the lock. Closes the REMAINING quantity."""
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        fill = exit_price * (1 - self.s.slippage) if pos.side == "long" else exit_price * (1 + self.s.slippage)
        pnl = pos.qty * ((fill - pos.entry) if pos.side == "long" else (pos.entry - fill)) - fill * pos.qty * self.s.fee_rate
        self.balance += pnl
        notional = pos.entry * pos.qty
        self.db.insert_trade({
            "mode": self.mode, "symbol": symbol, "side": pos.side, "entry_time": pos.opened_at,
            "entry_price": round(pos.entry, 4), "qty": round(pos.qty, 6), "stop": round(pos.stop, 4), "take": None,
            "exit_time": _now_iso(), "exit_price": round(fill, 4), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / notional * 100, 3) if notional else 0.0,
            "fees": round(fill * pos.qty * self.s.fee_rate, 4), "reason": reason,
        })
        if pnl < 0:
            self._last_loss_ts = time.time()
        self._on_close(symbol, pos.side, pos.qty, fill)
        self._log("trade", f"CLOSE {pos.side.upper()} {symbol} @ {fill:.2f} · {reason} · P&L {pnl:+.2f}")

    # Hooks: paper does nothing; the live engine overrides these to send real orders.
    def _on_open(self, symbol: str, side: str, qty: float, price: float) -> None:
        pass

    def _on_close(self, symbol: str, side: str, qty: float, price: float) -> None:
        pass

    # ------------------------------- helpers ----------------------------------
    def _equity_locked(self) -> float:
        eq = self.balance
        for sym, pos in self.positions.items():
            eq += pos.unrealized(self.prices.get(sym, pos.entry))
        return eq

    def equity(self) -> float:
        with self._lock:
            return self._equity_locked()

    def _rollover_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day_key:
            with self._lock:
                self._day_key = today
                self._day_start_equity = self._equity_locked()
                self.paused_reason = ""
            self._log("info", "new UTC day — daily loss limit reset")

    def _record_equity(self) -> None:
        self.db.insert_equity(self.mode, round(self.equity(), 2), _now_iso())

    def _update_status(self) -> None:
        eq = self.equity()
        day_pnl_pct = (eq - self._day_start_equity) / self._day_start_equity * 100
        if not self.running:
            self.status = "stopped"
        elif self.paused_reason:
            self.status = f"running · blocked: {self.paused_reason}"
        else:
            self.status = f"running ({self.s.strategy_version.upper()}) · {len(self.positions)} open · day {day_pnl_pct:+.2f}%"

    def _log(self, level: str, msg: str) -> None:
        log.info("[%s] %s", level, msg)
        try:
            self.db.insert_log(level, msg, _now_iso())
        except Exception:
            pass

    # ------------------------------- snapshot ---------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            eq = self._equity_locked()
            positions = [
                {"symbol": p.symbol, "side": p.side, "entry": round(p.entry, 2),
                 "qty": round(p.qty, 6), "stop": round(p.stop, 2),
                 "take": round(p.take, 2), "partial_done": p.partial_done,
                 "price": round(self.prices.get(p.symbol, p.entry), 2),
                 "unrealized": round(p.unrealized(self.prices.get(p.symbol, p.entry)), 2),
                 "opened_at": p.opened_at}
                for p in self.positions.values()
            ]
            day_pnl = eq - self._day_start_equity
            return {
                "mode": self.mode, "strategy": self.s.strategy_version.upper(), "running": self.running, "status": self.status,
                "paused_reason": self.paused_reason, "balance": round(self.balance, 2),
                "equity": round(eq, 2), "start_balance": round(self.s.start_balance, 2),
                "total_pnl": round(eq - self.s.start_balance, 2),
                "total_pnl_pct": round((eq / self.s.start_balance - 1) * 100, 2),
                "day_pnl": round(day_pnl, 2), "day_pnl_pct": round(day_pnl / self._day_start_equity * 100, 2),
                "open_positions": positions, "latest_signal": self.latest_signal,
                "symbols": self.s.symbol_list, "prices": {k: round(v, 2) for k, v in self.prices.items()},
            }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
