"""
================================================================================
 manual_trader.py  —  MANUAL PAPER-TRADING ENGINE  (you click the trades)
================================================================================
A second, SEPARATE paper account that YOU control by hand from the chart page —
the bot's own paper account (paper_broker.py) is untouched and keeps trading the
news on its own.

It mirrors how a Binance futures account behaves, but with fake money:
  - start with a balance you choose
  - open LONG or SHORT at the live BTC price (market order), with leverage
  - optional stop-loss / take-profit prices (auto-close when hit)
  - automatic liquidation if the loss eats your margin
  - live unrealized P&L, realized P&L, equity
  - a history of closed trades

It is priced/marked against the SAME live BTC price the bot uses. No real order
ever leaves your machine.
================================================================================
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict

import config

STATE_PATH = os.path.join(config.DATA_DIR, "manual_state.json")


@dataclass
class MPosition:
    id: str
    side: str            # "long" | "short"
    entry: float
    qty: float           # BTC
    margin: float        # USDT locked
    leverage: float
    sl: float            # 0 = none
    tp: float            # 0 = none
    opened_at: float

    def unrealized(self, mark: float) -> float:
        if self.side == "long":
            return self.qty * (mark - self.entry)
        return self.qty * (self.entry - mark)

    def liq_price(self) -> float:
        # approx liquidation (ignores fees/funding): margin fully lost
        if self.side == "long":
            return self.entry * (1 - 1 / self.leverage)
        return self.entry * (1 + 1 / self.leverage)


class ManualTrader:
    def __init__(self):
        self.active = False
        self.balance = 0.0
        self.start_balance = 0.0
        self.positions: dict[str, MPosition] = {}
        self.history: list[dict] = []
        self._load()

    # ---- persistence: survive restarts ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "active": self.active,
                    "balance": self.balance,
                    "start_balance": self.start_balance,
                    "history": self.history[:100],
                    "positions": [asdict(p) for p in self.positions.values()],
                }, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.active = bool(d.get("active", False))
            self.balance = float(d.get("balance", 0.0))
            self.start_balance = float(d.get("start_balance", 0.0))
            self.history = d.get("history", []) or []
            for pd in d.get("positions", []) or []:
                try:
                    p = MPosition(**pd)
                    self.positions[p.id] = p
                except Exception:
                    pass
            if self.active:
                print(f"[manual] restored: balance ${self.balance:.2f}, "
                      f"{len(self.history)} past trades, {len(self.positions)} open")
        except Exception:
            pass

    # ---- lifecycle ----
    def start(self, balance: float):
        b = float(balance)
        if b <= 0:
            return {"error": "balance must be greater than 0"}
        self.active = True
        self.balance = b
        self.start_balance = b
        self.positions = {}
        self.history = []
        self._save()
        return {"ok": True}

    def reset(self):
        self.active = False
        self.balance = 0.0
        self.start_balance = 0.0
        self.positions = {}
        self.history = []
        self._save()
        return {"ok": True}

    # ---- trading ----
    def open(self, side, margin, leverage, price, sl=0.0, tp=0.0):
        if not self.active:
            return {"error": "start the account first"}
        if not price or price <= 0:
            return {"error": "no live BTC price yet"}
        side = "long" if str(side).lower() in ("long", "buy", "bull") else "short"
        try:
            margin = float(margin)
            leverage = max(1.0, min(float(leverage), 125.0))
            sl = float(sl or 0)
            tp = float(tp or 0)
        except (TypeError, ValueError):
            return {"error": "bad numbers"}
        if margin <= 0:
            return {"error": "size (margin) must be greater than 0"}
        if margin > self.balance:
            # the 100% button can round a hair above the true balance -> clamp it
            if margin <= self.balance * 1.02 or margin <= self.balance + 1.0:
                margin = self.balance
            else:
                return {"error": "not enough balance"}

        qty = (margin * leverage) / price
        pos = MPosition(id=uuid.uuid4().hex[:6], side=side, entry=price, qty=qty,
                        margin=margin, leverage=leverage, sl=sl, tp=tp,
                        opened_at=time.time())
        self.balance -= margin
        self.positions[pos.id] = pos
        self._save()
        return {"ok": True, "id": pos.id}

    def set_sl_tp(self, pid, sl=None, tp=None):
        """Update an open position's stop-loss / take-profit (used by chart drag)."""
        pos = self.positions.get(pid)
        if not pos:
            return {"error": "no such position"}
        try:
            if sl is not None:
                pos.sl = float(sl) if sl else 0.0
            if tp is not None:
                pos.tp = float(tp) if tp else 0.0
        except (TypeError, ValueError):
            return {"error": "bad price"}
        self._save()
        return {"ok": True, "sl": pos.sl, "tp": pos.tp}

    def close(self, pid, price, reason="manual"):
        pos = self.positions.pop(pid, None)
        if not pos:
            return {"error": "no such position"}
        pnl = pos.unrealized(price) if price else 0.0
        self.balance += pos.margin + pnl
        self.history.insert(0, {
            "id": pos.id, "side": pos.side, "entry": round(pos.entry, 2),
            "exit": round(price, 2), "qty": round(pos.qty, 5),
            "margin": round(pos.margin, 2), "leverage": pos.leverage,
            "pnl": round(pnl, 2), "reason": reason, "closed_at": time.time()})
        self.history = self.history[:100]
        self._save()
        return {"ok": True, "pnl": round(pnl, 2)}

    # ---- called every tick with the live price ----
    def mark(self, price):
        if not price or price <= 0:
            return
        for pos in list(self.positions.values()):
            up = pos.unrealized(price)
            if up <= -pos.margin * 0.98:
                self.close(pos.id, price, "liquidation"); continue
            if pos.side == "long":
                if pos.sl and price <= pos.sl: self.close(pos.id, price, "stop_loss"); continue
                if pos.tp and price >= pos.tp: self.close(pos.id, price, "take_profit"); continue
            else:
                if pos.sl and price >= pos.sl: self.close(pos.id, price, "stop_loss"); continue
                if pos.tp and price <= pos.tp: self.close(pos.id, price, "take_profit"); continue

    # ---- snapshot for the UI ----
    def state(self, price):
        positions = []
        equity = self.balance
        for p in self.positions.values():
            up = p.unrealized(price) if price else 0.0
            equity += p.margin + up
            positions.append({
                "id": p.id, "side": p.side, "entry": round(p.entry, 2),
                "qty": round(p.qty, 5), "margin": round(p.margin, 2),
                "leverage": p.leverage, "sl": round(p.sl, 2) if p.sl else 0,
                "tp": round(p.tp, 2) if p.tp else 0, "liq": round(p.liq_price(), 2),
                "pnl": round(up, 2),
                "pnl_pct": round(up / p.margin * 100, 2) if p.margin else 0})
        return {
            "active": self.active,
            "price": round(price, 2) if price else None,
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(equity - self.start_balance, 2) if self.active else 0,
            "positions": positions,
            "history": self.history[:30],
        }
