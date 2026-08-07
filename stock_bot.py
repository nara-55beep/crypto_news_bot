"""
================================================================================
 stock_bot.py  —  THE STOCK AI NEWS BOT  (same idea as the crypto news bot)
================================================================================
For every news headline it asks Gemini (via stock_analyzer) "does this move one of
my stocks, and which way?" If the AI says TRADE, it opens a paper position in that
stock; otherwise it skips. It marks positions against live Alpaca prices and exits
on take-profit / stop-loss / time. Its own separate paper account; logs every
decision, trade, profit and loss (console + the UI panel + a CSV).

No order book, no whale flow — purely news + the AI's judgment, exactly like the
first crypto news bot, but for stocks.
================================================================================
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict

import config
import stock_analyzer

STATE_PATH = os.path.join(config.DATA_DIR, "stock_bot_state.json")


@dataclass
class SPosition:
    id: str
    symbol: str
    side: str            # "long" | "short"
    entry: float
    qty: float           # shares
    margin: float
    leverage: float
    opened_at: float
    conviction: str
    news: str

    def unrealized(self, mark):
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" \
            else self.qty * (self.entry - mark)

    def liq_price(self):
        if self.leverage <= 1:
            return 0.0 if self.side == "long" else self.entry * 2
        return self.entry * (1 - 1 / self.leverage) if self.side == "long" \
            else self.entry * (1 + 1 / self.leverage)


class StockNewsBot:
    def __init__(self, market):
        self.market = market
        self.enabled = config.STOCK_ENABLED
        self.balance = float(config.STOCK_START_BALANCE)
        self.start_balance = float(config.STOCK_START_BALANCE)
        self.positions: dict[str, SPosition] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.news_events: list[dict] = []
        self._busy = False
        self._load()

    # ---- persistence: survive restarts ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "balance": self.balance,
                    "start_balance": self.start_balance,
                    "enabled": self.enabled,
                    "history": self.history[:300],
                    "log": self.log[:80],
                    "news_events": self.news_events[:400],
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
            self.balance = float(d.get("balance", self.balance))
            self.start_balance = float(d.get("start_balance", self.start_balance))
            self.enabled = bool(d.get("enabled", self.enabled))
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.news_events = d.get("news_events", []) or []
            for pd in d.get("positions", []) or []:
                try:
                    p = SPosition(**pd)
                    self.positions[p.id] = p
                except Exception:
                    pass
            print(f"[stock-bot] restored: balance ${self.balance:.2f}, "
                  f"{len(self.history)} past trades, {len(self.positions)} open")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[stock-bot] {msg}")

    def _price(self, sym):
        try:
            return self.market.price(sym)
        except Exception:
            return None

    # ---- triggered by every news message ----
    async def on_news(self, source, text):
        if not self.enabled or not config.STOCK_ANALYZE_ALL:
            return
        asyncio.create_task(self._process(source, text))

    async def _process(self, source, text):
        snap = {}
        try:
            snap = self.market.snapshot()
        except Exception:
            pass
        sig = await stock_analyzer.analyze(text, snap)
        ev = {"ts": time.time(), "text": (text or "").strip().replace("\n", " "),
              "source": source, "decision": sig.decision, "direction": sig.direction,
              "symbols": sig.affected_assets, "conviction": sig.conviction,
              "reasoning": sig.reasoning, "traded": False}
        head = ev["text"][:70]
        if sig.error:
            self._note(f"AI error ({sig.error[:50]}) on: {head}", "error")
        if sig.wants_trade and self.enabled:
            sym = next((a for a in sig.affected_assets
                        if a in config.STOCK_SYMBOLS and self._price(a)), None)
            if sym and sym not in {p.symbol for p in self.positions.values()} \
                    and len(self.positions) < config.STOCK_MAX_CONCURRENT:
                side = "long" if sig.direction == "bullish" else "short"
                if self._open(sym, side, sig.conviction, head):
                    ev["traded"] = True
            elif not sym:
                self._note(f"TRADE {sig.direction} but no live price -> skip: {head}", "skip")
            else:
                self._note(f"TRADE signal but already full/holding -> skip: {head}", "skip")
        else:
            self._note(f"SKIP: {head}", "skip")
        self.news_events.insert(0, ev)
        self.news_events = self.news_events[:400]
        self._save()

    def _open(self, symbol, side, conviction, news):
        price = self._price(symbol)
        if not price:
            return False
        margin = round(self.balance * config.STOCK_RISK_FRAC, 2)
        if margin < 1 or margin > self.balance:
            self._note("balance too low to open -> skip", "skip"); return False
        lev = config.STOCK_LEVERAGE
        qty = margin * lev / price
        pos = SPosition(id=uuid.uuid4().hex[:6], symbol=symbol, side=side, entry=price,
                        qty=qty, margin=margin, leverage=lev, opened_at=time.time(),
                        conviction=conviction, news=news)
        self.balance -= margin
        self.positions[pos.id] = pos
        self._note(f"OPEN {side.upper()} {symbol} @ ${price:,.2f} · {conviction} conv · "
                   f"margin ${margin:.2f}{('  '+str(lev)+'x') if lev>1 else ''}", "open")
        return True

    def _close(self, pos, price, reason):
        self.positions.pop(pos.id, None)
        pnl = pos.unrealized(price)
        self.balance += pos.margin + pnl
        rec = {"id": pos.id, "symbol": pos.symbol, "side": pos.side,
               "entry": round(pos.entry, 2), "exit": round(price, 2),
               "qty": round(pos.qty, 4), "margin": round(pos.margin, 2),
               "pnl": round(pnl, 2), "reason": reason, "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history[:300]
        self._note(f"CLOSE {pos.side.upper()} {pos.symbol} @ ${price:,.2f} · {reason} · "
                   f"P&L ${pnl:+.2f}", "win" if pnl >= 0 else "loss")
        self._csv(rec)
        self._save()

    def _csv(self, rec):
        try:
            new = not os.path.exists(config.STOCK_TRADES_CSV)
            with open(config.STOCK_TRADES_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "symbol", "side", "entry", "exit",
                                "qty", "margin", "pnl", "reason"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["symbol"], rec["side"], rec["entry"], rec["exit"],
                            rec["qty"], rec["margin"], rec["pnl"], rec["reason"]])
        except Exception:
            pass

    async def manage_loop(self):
        while True:
            await asyncio.sleep(2.0)
            for pos in list(self.positions.values()):
                px = self._price(pos.symbol)
                if not px:
                    continue
                held = time.time() - pos.opened_at
                if pos.side == "long":
                    if px <= pos.entry * (1 - config.STOCK_STOP_LOSS_PCT):
                        self._close(pos, px, "stop_loss"); continue
                    if px >= pos.entry * (1 + config.STOCK_TAKE_PROFIT_PCT):
                        self._close(pos, px, "take_profit"); continue
                else:
                    if px >= pos.entry * (1 + config.STOCK_STOP_LOSS_PCT):
                        self._close(pos, px, "stop_loss"); continue
                    if px <= pos.entry * (1 - config.STOCK_TAKE_PROFIT_PCT):
                        self._close(pos, px, "take_profit"); continue
                if pos.leverage > 1 and pos.unrealized(px) <= -pos.margin * 0.98:
                    self._close(pos, px, "liquidation"); continue
                if held >= config.STOCK_MAX_HOLD_MIN * 60:
                    self._close(pos, px, "time_stop"); continue

    # ---- controls + snapshot ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = config.STOCK_ENABLED
        self.balance = float(config.STOCK_START_BALANCE)
        self.start_balance = float(config.STOCK_START_BALANCE)
        self.positions = {}
        self.history = []
        self.log = []
        self.news_events = []
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        positions, equity = [], self.balance
        for p in self.positions.values():
            px = self._price(p.symbol)
            up = p.unrealized(px)
            equity += p.margin + up
            positions.append({
                "id": p.id, "symbol": p.symbol, "side": p.side,
                "entry": round(p.entry, 2), "price": round(px, 2) if px else None,
                "qty": round(p.qty, 4), "margin": round(p.margin, 2),
                "leverage": p.leverage, "conviction": p.conviction,
                "pnl": round(up, 2),
                "pnl_pct": round(up / p.margin * 100, 2) if p.margin else 0})
        wins = sum(1 for h in self.history if h["pnl"] >= 0)
        market_ok = getattr(self.market, "ok", False)
        return {
            "enabled": self.enabled,
            "market_ok": market_ok,
            "market_error": getattr(self.market, "last_error", "")[:120],
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(equity - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "news_seen": len(self.news_events),
            "positions": positions,
            "history": self.history[:30],
            "log": self.log[:25],
        }
