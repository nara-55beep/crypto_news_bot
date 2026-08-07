"""
cot_fade_paper.py - live PAPER bot for the COT crowded-positioning fade idea.

The repo has no CFTC/COT data feed yet, so this bot runs the Jason Shapiro-style
playbook as a live proxy: find crowded one-way positioning from higher-timeframe
stretch, then take the other side only after lower-timeframe reversal confirmation.
It paper-trades BTC perps with the same $100 account style as the other /paper bots.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass

import lighter_markets

START_BALANCE = 100.0
TIMEFRAME = "15m"
LEVERAGE = 10
EXTREME_SCORE = 0.45
STOPLOSS = 0.008
TAKE_PROFIT = 0.012
TRAIL_ARM = 0.008
TRAIL_GAP = 0.006
MAX_HOLD_SEC = 6 * 60 * 60

STRATEGIES = {"cot_fade": "COT Crowded-Positioning Fade (extreme positioning + reversal confirmation)"}


def ema(vals, length):
    if not vals:
        return None
    k = 2.0 / (length + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, length=14):
    if len(closes) < length + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - length, len(closes)):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / length) / (losses / length)
    return 100.0 - 100.0 / (1.0 + rs)


@dataclass
class COTPos:
    id: str
    side: str
    entry: float
    qty: float
    stake: float
    opened_at: float
    peak: float
    note: str = ""
    trail_on: bool = False

    def unrealized(self, mark):
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" else self.qty * (self.entry - mark)


class COTFadePaperBot:
    def __init__(self, market):
        self.market = market
        self.enabled = True
        self.strategy = "cot_fade"
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos: "COTPos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []

        self._cs15 = []
        self._cs1d = []
        self._last_bar_t = 0
        self._t15 = 0.0
        self._t1d = 0.0
        self._rl_until = 0.0
        self._err = ""
        self._ctx = {"crowd": None, "score": None, "rsi_d": None, "ema_fast": None, "ema_slow": None}
        self._load()

    def _path(self):
        return os.path.join("data", "cot_fade_state.json")

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
            self.enabled = bool(d.get("enabled", True))
            self.balance = float(d.get("balance", START_BALANCE))
            self.start_balance = float(d.get("start_balance", START_BALANCE))
            self.history = d.get("history", [])
            self.log = d.get("log", [])
            if not self.history:
                self.balance = START_BALANCE
                self.start_balance = START_BALANCE
            else:
                self.balance = round(self.start_balance + sum(h.get("pnl", 0.0) for h in self.history), 2)
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
            eq += self.pos.stake + self.pos.unrealized(self._btc() or self.pos.entry)
        return eq

    async def _refresh(self, now):
        if now < self._rl_until:
            return
        try:
            if now - self._t15 >= 60:
                self._cs15 = await asyncio.to_thread(lighter_markets.candles, "BTC", TIMEFRAME, 220)
                self._t15 = now
            if now - self._t1d >= 600:
                self._cs1d = await asyncio.to_thread(lighter_markets.candles, "BTC", "1d", 90)
                self._t1d = now
            self._err = ""
        except Exception as e:
            msg = str(e)
            if "RateLimit" in type(e).__name__ or "23000" in msg or "Too Many" in msg:
                self._rl_until = now + 60.0
                self._err = "rate-limited by Lighter - backing off 60s"
            else:
                self._err = f"{type(e).__name__}: {str(e)[:120]}"

    def _context(self):
        dcloses = [c["c"] for c in self._cs1d]
        closes = [c["c"] for c in self._cs15]
        if len(dcloses) < 22 or len(closes) < 30:
            return self._ctx
        r = rsi(dcloses, 14)
        roc20 = (dcloses[-1] / dcloses[-21] - 1.0) if dcloses[-21] else 0.0
        score = ((r or 50.0) - 50.0) / 35.0 + (roc20 / 0.20)
        score = max(-2.0, min(2.0, score))
        crowd = "long_risk_short_usd" if score >= EXTREME_SCORE else (
            "short_risk_long_usd" if score <= -EXTREME_SCORE else "balanced")
        self._ctx = {
            "crowd": crowd,
            "score": round(score, 2),
            "rsi_d": round(r, 1) if r is not None else None,
            "ema_fast": round(ema(closes[-60:], 9), 1),
            "ema_slow": round(ema(closes[-80:], 21), 1),
        }
        return self._ctx

    def _open(self, side, price, note):
        stake = round(self.balance, 2)
        if stake < 1 or not price:
            return
        qty = stake * LEVERAGE / price
        self.balance -= stake
        self.pos = COTPos(id=uuid.uuid4().hex[:6], side=side, entry=price, qty=qty,
                          stake=stake, opened_at=time.time(), peak=price, note=note)
        self._note(f"ENTRY {side.upper()} @ {price:,.1f} - {note} - margin ${stake:.2f} {LEVERAGE}x", "open")
        self._save()

    def _close(self, price, reason):
        p = self.pos
        pnl = p.unrealized(price)
        self.balance += p.stake + pnl
        rec = {"id": p.id, "side": p.side, "entry": round(p.entry, 1), "exit": round(price, 1),
               "qty": round(p.qty, 6), "pnl": round(pnl, 2), "reason": reason,
               "pnl_pct": round(pnl / p.stake * 100, 2) if p.stake else 0.0,
               "opened_at": p.opened_at, "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self._note(f"EXIT {p.side.upper()} @ {price:,.1f} - {reason} - P&L ${pnl:+.2f} ({rec['pnl_pct']:+.2f}%)",
                   "win" if pnl >= 0 else "loss")
        self.pos = None
        self._save()

    def _manage(self, px):
        p = self.pos
        if p is None or not px:
            return
        profit = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
        if p.unrealized(px) <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if profit <= -STOPLOSS:
            self._close(px, "tight_invalidation_stop"); return
        if profit >= TAKE_PROFIT:
            self._close(px, "target_reached"); return
        if time.time() - p.opened_at >= MAX_HOLD_SEC:
            self._close(px, "time_stop"); return
        if not p.trail_on and profit >= TRAIL_ARM:
            p.trail_on = True
        if p.trail_on:
            if p.side == "long":
                p.peak = max(p.peak, px)
                if px <= p.peak * (1 - TRAIL_GAP):
                    self._close(px, "trailing_stop")
            else:
                p.peak = min(p.peak, px)
                if px >= p.peak * (1 + TRAIL_GAP):
                    self._close(px, "trailing_stop")

    def _check_entry(self):
        if len(self._cs15) < 30:
            return
        ctx = self._context()
        closes = [c["c"] for c in self._cs15]
        lows = [c["l"] for c in self._cs15]
        highs = [c["h"] for c in self._cs15]
        ef = ema(closes[-40:], 9)
        prev_ef = ema(closes[-41:-1], 9)
        price = self._btc() or closes[-1]
        break_down = closes[-1] < min(lows[-7:-1]) or (prev_ef and closes[-2] >= prev_ef and closes[-1] < ef)
        break_up = closes[-1] > max(highs[-7:-1]) or (prev_ef and closes[-2] <= prev_ef and closes[-1] > ef)

        if ctx.get("score") is None:
            return
        if ctx["score"] >= EXTREME_SCORE and break_down:
            self._open("short", price, f"COT fade: crowd long risk / short USD, reversal confirmed (score {ctx['score']:+.2f})")
        elif ctx["score"] <= -EXTREME_SCORE and break_up:
            self._open("long", price, f"COT fade: crowd short risk / long USD, reversal confirmed (score {ctx['score']:+.2f})")

    async def manage_loop(self):
        await asyncio.sleep(7.0)
        while True:
            await asyncio.sleep(15.0)
            try:
                now = time.time()
                await self._refresh(now)
                px = self._btc()
                if self.pos is not None and px:
                    self._manage(px)
                if not self._cs15:
                    continue
                newest = self._cs15[-1]["t"]
                self._context()
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                if self.pos is None and self.enabled:
                    self._check_entry()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - paper-trading the COT crowded-positioning fade"
                   if self.enabled else "bot PAUSED - no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos = None
        self.history = []
        self.log = []
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
                "id": p.id, "side": p.side, "entry": round(p.entry, 1), "qty": round(p.qty, 6),
                "leverage": LEVERAGE,
                "liq": round(p.entry * (1 - 1.0 / LEVERAGE), 1) if p.side == "long"
                       else round(p.entry * (1 + 1.0 / LEVERAGE), 1),
                "pnl": round(up, 2), "pnl_pct": round(up / p.stake * 100, 2) if p.stake else 0.0,
                "news": p.note + (" - trailing" if p.trail_on else ""),
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "name": "COT Crowded-Positioning Fade",
            "enabled": self.enabled,
            "running": True,
            "strategy": self.strategy,
            "strategy_label": STRATEGIES[self.strategy],
            "strategies": STRATEGIES,
            "timeframe": TIMEFRAME,
            "leverage": LEVERAGE,
            "market": "perp",
            "crowd": c.get("crowd"),
            "score": c.get("score"),
            "rsi_d": c.get("rsi_d"),
            "ema_fast": c.get("ema_fast"),
            "ema_slow": c.get("ema_slow"),
            "price": round(px, 2) if px else None,
            "balance": round(self.balance, 2),
            "equity": round(self.equity(), 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(self.equity() - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "data_error": self._err,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
        }
