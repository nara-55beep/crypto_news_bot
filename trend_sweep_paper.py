"""
trend_sweep_paper.py — in-process PAPER adapter for the trend_sweep_bot strategy, for the /paper page.

It reuses the EXACT same strategy state machine, indicators and risk module as the standalone
package (trend_sweep_bot/), but runs them on the dashboard's live Lighter candle feed and simulates
fills against the live price (with slippage + fee for realism). No real orders, no keys. $100 paper.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass

import lighter_markets
from trend_sweep_bot.config import Config
from trend_sweep_bot import indicators
from trend_sweep_bot.strategy import TrendSweepStrategy
from trend_sweep_bot.risk import RiskManager, position_size

START_BALANCE = 100.0
SLIP = 2.0 / 1e4      # 0.02% per side
FEE = 0.0             # ZERO — Lighter (the real venue) has no trading fees

STRATEGIES = {"trend_sweep": "Trend-Sweep VWAP (4H trend · PDL/PDH sweep · 5m breakout)"}


@dataclass
class TSPos:
    id: str
    side: str
    entry: float
    qty: float
    qty0: float
    stop: float
    stop0: float
    tp1: float
    tp2: float
    risk_usd: float
    opened_at: float
    tp1_done: bool = False
    realized: float = 0.0
    note: str = ""

    def unrealized(self, mark):
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" else self.qty * (self.entry - mark)


class TrendSweepPaperBot:
    def __init__(self, market):
        self.market = market
        self.cfg = Config()                 # strategy defaults (1% risk, etc.)
        self.cfg.start_balance = START_BALANCE
        self.enabled = False
        self.strategy = "trend_sweep"
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos: "TSPos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []

        self.strat = TrendSweepStrategy(self.cfg)
        self.risk = RiskManager(self.cfg)
        self._ctx = {"trend": None, "pdh": None, "pdl": None, "vwap": None, "atr": None}

        self._cs5 = []
        self._cs4h = []
        self._cs1d = []
        self._last_bar_t = 0
        self._t5 = self._t4 = self._t1d = 0.0
        self._rl_until = 0.0
        self._err = ""
        self._load()

    # ---- persistence ----
    def _path(self):
        return os.path.join("data", "trend_sweep_state.json")

    def _save(self):
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._path(), "w") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance,
                           "start_balance": self.start_balance,
                           "history": self.history, "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            with open(self._path()) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.start_balance = float(d.get("start_balance", START_BALANCE))
            self.history = d.get("history", [])
            self.log = d.get("log", [])
            if not self.history:
                self.balance = START_BALANCE
                self.start_balance = START_BALANCE
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "msg": msg, "kind": kind})
        self.log = self.log[:60]

    def _btc(self):
        try:
            return self.market.price("BTCUSDT")
        except Exception:
            return None

    def equity(self):
        eq = self.balance
        if self.pos is not None:
            eq += self.pos.unrealized(self._btc() or self.pos.entry)
        return eq

    # ---- candles (gentle, rate-limit safe) ----
    async def _refresh(self, now):
        if now < self._rl_until:
            return
        try:
            if now - self._t5 >= 60:
                self._cs5 = await asyncio.to_thread(lighter_markets.candles, "BTC", "5m", 300)
                self._t5 = now
            elif now - self._t4 >= 300:
                self._cs4h = await asyncio.to_thread(lighter_markets.candles, "BTC", "4h", 200)
                self._t4 = now
            elif now - self._t1d >= 600:
                self._cs1d = await asyncio.to_thread(lighter_markets.candles, "BTC", "1d", 60)
                self._t1d = now
            self._err = ""
        except Exception as e:
            msg = str(e)
            if "RateLimit" in type(e).__name__ or "23000" in msg or "Too Many" in msg:
                self._rl_until = now + 60.0
                self._err = "rate-limited by Lighter — backing off 60s"
            else:
                self._err = f"{type(e).__name__}: {str(e)[:120]}"

    def _build_ctx(self, newest_t):
        c5, c4h, c1d = self._cs5, self._cs4h, self._cs1d
        trend = indicators.classify_trend(c4h, self.cfg.swing_width, self.cfg.trend_ema_len,
                                          self.cfg.trend_lookback) if len(c4h) > 8 else None
        pdh, pdl = indicators.prev_day_high_low(c1d, newest_t) if c1d else (None, None)
        vwap = indicators.daily_vwap(c5, newest_t) if c5 else None
        atr_val = indicators.atr(c5[-(self.cfg.atr_len + 2):], self.cfg.atr_len) if len(c5) > self.cfg.atr_len else None
        self._ctx = {"trend": trend, "pdh": pdh, "pdl": pdl, "vwap": vwap, "atr": atr_val}
        return self._ctx

    # ---- trade lifecycle (paper) ----
    def _open(self, sig):
        ok, why = self.risk.can_trade(time.time())
        if not ok:
            self._note(f"signal but blocked: {why}", "skip")
            return
        side = sig["side"]
        entry = sig["entry"] * (1 + SLIP) if side == "long" else sig["entry"] * (1 - SLIP)
        stop = sig["stop"]
        qty = position_size(self.equity(), self.cfg.risk_pct, entry, stop)
        if qty <= 0:
            return
        risk_usd = qty * abs(entry - stop)
        fee = FEE * entry * qty
        self.pos = TSPos(id=uuid.uuid4().hex[:6], side=side, entry=entry, qty=qty, qty0=qty,
                         stop=stop, stop0=stop, tp1=sig["tp1"], tp2=sig["tp2"], risk_usd=risk_usd,
                         opened_at=time.time(), note=sig["reason"])
        self.pos.realized = -fee   # entry fee accrues into realized; balance changes only at close
        self.risk.on_open(time.time())
        self._note(f"ENTRY {side.upper()} @ {entry:,.1f} · stop {stop:,.1f} · TP1 {sig['tp1']:,.1f} "
                   f"· TP2 {sig['tp2']:,.1f} · qty {qty:.5f}", "open")
        self._save()

    def _close_leg(self, qty, price, reason, final):
        p = self.pos
        exit_px = price * (1 - SLIP) if p.side == "long" else price * (1 + SLIP)
        leg = (exit_px - p.entry) * qty if p.side == "long" else (p.entry - exit_px) * qty
        fee = FEE * exit_px * qty
        p.realized += leg - fee
        p.qty -= qty
        if final or p.qty <= 1e-12:
            self.balance += p.realized       # realized includes entry fee + all leg PnL - exit fees
            total = p.realized
            r = total / p.risk_usd if p.risk_usd else 0.0
            rec = {"side": p.side, "entry": round(p.entry, 1), "exit": round(exit_px, 1),
                   "qty": round(p.qty0, 6), "pnl": round(total, 2), "rr": round(r, 2),
                   "reason": reason, "opened_at": p.opened_at, "closed_at": time.time()}
            self.history.insert(0, rec)
            self.history = self.history
            self.risk.on_close(total)
            self._note(f"EXIT {p.side.upper()} @ {exit_px:,.1f} · {reason} · P&L ${total:+.2f} "
                       f"({r:+.2f}R)", "win" if total >= 0 else "loss")
            self.pos = None
            self._save()

    def _manage(self, px):
        p = self.pos
        if p is None or not px:
            return
        if p.side == "long":
            if px <= p.stop:
                self._close_leg(p.qty, p.stop, "stop_loss" if not p.tp1_done else "breakeven", True); return
            if not p.tp1_done and px >= p.tp1:
                self._close_leg(p.qty0 * self.cfg.tp1_size, p.tp1, "tp1_vwap", False)
                p.tp1_done = True
                if self.cfg.move_stop_be_after_tp1 and self.pos:
                    p.stop = p.entry
            if self.pos and px >= p.tp2:
                self._close_leg(p.qty, p.tp2, "tp2_2R", True); return
        else:
            if px >= p.stop:
                self._close_leg(p.qty, p.stop, "stop_loss" if not p.tp1_done else "breakeven", True); return
            if not p.tp1_done and px <= p.tp1:
                self._close_leg(p.qty0 * self.cfg.tp1_size, p.tp1, "tp1_vwap", False)
                p.tp1_done = True
                if self.cfg.move_stop_be_after_tp1 and self.pos:
                    p.stop = p.entry
            if self.pos and px <= p.tp2:
                self._close_leg(p.qty, p.tp2, "tp2_2R", True); return
        # hard time stop
        if self.pos and (time.time() - p.opened_at) >= self.cfg.max_hold_bars * 300:
            self._close_leg(p.qty, px, "time_stop", True)

    # ---- main loop ----
    async def manage_loop(self):
        await asyncio.sleep(5.0)
        while True:
            await asyncio.sleep(15.0)
            try:
                now = time.time()
                await self._refresh(now)
                px = self._btc()
                if self.pos is not None and px:
                    self._manage(px)
                if not self._cs5:
                    continue
                newest = self._cs5[-1]["t"]
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                ctx = self._build_ctx(newest)
                if self.pos is None and self.enabled:
                    sig = self.strat.on_bar(self._cs5, len(self._cs5) - 1, ctx)
                    if sig:
                        self._open(sig)
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    # ---- controls / state ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED — paper-trading the trend-sweep strategy" if self.enabled
                   else "bot PAUSED — no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_strategy(self, name):
        if name and name not in STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[self.strategy]}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos = None
        self.history = []
        self.log = []
        self.strat.reset()
        self._note("bot reset (paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return {"ok": True}

    def state(self, **_):
        px = self._btc()
        c = self._ctx
        positions = []
        if self.pos is not None:
            p = self.pos
            up = p.unrealized(px) if px else 0.0
            positions.append({
                "id": p.id, "side": p.side, "entry": round(p.entry, 1),
                "qty": round(p.qty, 6), "stop": round(p.stop, 1),
                "tp1": round(p.tp1, 1), "tp2": round(p.tp2, 1),
                "pnl": round(up, 2), "pnl_R": round(up / p.risk_usd, 2) if p.risk_usd else 0.0,
                "news": p.note + (" · TP1 done" if p.tp1_done else ""),
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "name": "Trend-Sweep VWAP",
            "enabled": self.enabled,
            "running": True,
            "strategy": self.strategy,
            "strategy_label": STRATEGIES[self.strategy],
            "strategies": STRATEGIES,
            "trend": c.get("trend"),
            "pdh": round(c["pdh"], 1) if c.get("pdh") else None,
            "pdl": round(c["pdl"], 1) if c.get("pdl") else None,
            "vwap": round(c["vwap"], 1) if c.get("vwap") else None,
            "price": round(px, 2) if px else None,
            "balance": round(self.balance, 2),
            "equity": round(self.equity(), 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(self.equity() - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "trades_today": self.risk.trades_today,
            "data_error": self._err,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
        }
