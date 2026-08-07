"""
================================================================================
 news_momo_paper.py  —  "NEWS-MOMENTUM (follow the 5s-late crowd)"  (paper)
================================================================================
Exactly the strategy the user asked for:

  ENTRY  — the news reaches us ~5s AFTER the fast crowd already started buying/
           selling on it. So when news lands we look at the price move over the
           last 5 SECONDS (our delay). If the crowd already pushed price >= 0.07%,
           we FOLLOW them immediately — up -> LONG, down -> SHORT — then ride it.
           Flat/tiny move (< 0.07%) -> no one's reacting -> no trade.

  EXIT   — the "Freqtrade-style (improved TP/SL 5%)" trailing logic, and NOTHING
           else from that bot: a margin-based stop at -5% of margin, moved to
           break-even once +3% margin, then trailing the best profit by a 5%
           margin gap. (No RSI/ROI/entry logic is borrowed — only the TP/SL.)

  ACCOUNT — $100 paper, ALL-IN 20x, BTC. Zero fees (Lighter has none). One
           position at a time; news only opens trades, the 5% trail closes them.

NOTE on resolution: the shared price feed is 1-second REST polling, so a 5-second
look-back holds only ~5 price points — the 0.07% move is judged at ~1s
granularity, not millisecond ticks. Real BTC price, all paper.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque

import config

STATE_PATH = os.path.join(config.DATA_DIR, "news_momo_state.json")

PX_SYMBOL   = "BTCUSDT"        # key into the shared MarketData price feed
START_BAL   = 100.0           # paper account (USDT)
LEVERAGE    = 20.0            # ALL-IN: notional = whole equity * 20x
FEE         = 0.0             # ZERO — Lighter (the real venue) has no trading fees

# --- entry: we're ~5s behind the fast crowd, so FOLLOW the move they already made ---
LOOKBACK_SEC = 5.0           # our news delay — compare price NOW vs this many seconds ago
MOVE_THRESH  = 0.0007         # 0.07% — if the crowd already pushed it this far, follow them
SAMPLE_SEC   = 0.25          # how often we sample the price into the rolling buffer
BUF_SEC      = 12.0          # keep this many seconds of (ts, price) samples

# --- exit: "Freqtrade improved TP/SL 5%" trailing (margin-based, leverage-agnostic) ---
STOP_MARGIN   = -0.05         # initial hard stop at -5% of MARGIN
BE_TRIGGER    = 0.03          # once +3% margin in profit, pull stop to break-even
TRAIL_TRIGGER = 0.05          # once +5% margin, start trailing
TRAIL_GAP     = 0.05          # trail the best profit by a 5%-margin gap
LIQ_MARGIN    = -0.98         # isolated-margin liquidation backstop


class NewsMomentumBot:
    def __init__(self, market=None):
        self.market = market
        self.enabled = True                       # added + running, paper-trading
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.price = 0.0
        self._buf: deque = deque(maxlen=int(BUF_SEC / SAMPLE_SEC) + 8)   # (ts, price) rolling buffer
        self.last_signal = ""                     # human-readable last news decision
        self.last_news_at = 0.0
        self.news_seen = 0
        self.status = "starting…"
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence (buffer is in-memory only) --------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance, "pos": self.pos,
                           "history": self.history, "log": self.log[:60],
                           "news_seen": self.news_seen, "last_signal": self.last_signal,
                           "last_news_at": self.last_news_at}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.news_seen = int(d.get("news_seen", 0))
            self.last_signal = d.get("last_signal", "") or ""
            self.last_news_at = float(d.get("last_news_at", 0.0) or 0.0)
            print(f"[newsmomo] restored: bal ${self.balance:.2f}, {len(self.history)} trades, "
                  f"{'1 open' if self.pos else 'flat'}")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[newsmomo] {msg}")

    # ---- price feed -------------------------------------------------------
    def _price(self) -> float:
        try:
            p = self.market.price(PX_SYMBOL) if self.market else None
            if p:
                self.price = float(p)
        except Exception:
            pass
        return self.price

    # ---- the loop: check price, manage the position, catch post-news breakouts ----
    async def manage_loop(self):
        while True:
            try:
                px = self._price()
                if px:
                    self._buf.append((time.time(), px))     # roll the price buffer
                    if self.pos is not None:
                        self._manage(px)                    # 5% trailing TP/SL (the only exit)
                self._set_status()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:80]}", "error")
            await asyncio.sleep(SAMPLE_SEC)

    # ---- the news trigger (called from main.py on EVERY news item) --------
    def on_news(self, source: str = "", text: str = ""):
        """We get news ~5s AFTER the fast crowd already reacted. So when news lands we look
        at the move over the last LOOKBACK_SEC (our delay): if the crowd already pushed
        price >= 0.07%, we FOLLOW them — up -> LONG, down -> SHORT — immediately."""
        try:
            self.news_seen += 1
            self.last_news_at = time.time()
            if not self.enabled:
                self.last_signal = "paused — ignored news"
                return
            px = self._price()
            if not px:
                self.last_signal = "no price yet — skipped"
                return
            if self.pos is not None:                        # one position at a time
                self.last_signal = "news while in a trade — ignored for entry"
                return
            move = self._recent_move(self.last_news_at)
            src = (source or "?")[:18]
            if move is None:
                self.last_signal = "price history still filling (just restarted) — skipped"
                self._note(f"news from {src} · {self.last_signal}", "skip")
                return
            pct = move * 100.0
            if move >= MOVE_THRESH:
                self.last_signal = f"last {LOOKBACK_SEC:.0f}s {pct:+.3f}% ≥0.07% (crowd buying) → LONG"
                self._open("long", px, move)
            elif move <= -MOVE_THRESH:
                self.last_signal = f"last {LOOKBACK_SEC:.0f}s {pct:+.3f}% ≤-0.07% (crowd selling) → SHORT"
                self._open("short", px, move)
            else:
                self.last_signal = f"last {LOOKBACK_SEC:.0f}s {pct:+.3f}% < 0.07% → no clear move, skipped"
                self._note(f"news from {src} · {self.last_signal}", "skip")
        except Exception as e:
            self._note(f"on_news error: {type(e).__name__}: {str(e)[:80]}", "error")
        finally:
            self._save()        # persist news_seen + last_signal on EVERY news event (survives dev.bat reloads)

    def _recent_move(self, now: float):
        """Fractional price move over the last LOOKBACK_SEC (price now vs ~5s ago).
        None if we don't have ~5s of price history buffered yet."""
        if len(self._buf) < 2 or (now - self._buf[0][0]) < LOOKBACK_SEC * 0.6:
            return None
        target = now - LOOKBACK_SEC
        then_px = min(self._buf, key=lambda tp: abs(tp[0] - target))[1]   # sample closest to 5s ago
        now_px = self._buf[-1][1]
        if not then_px or not now_px:
            return None
        return (now_px - then_px) / then_px

    # ---- open (all-in 20x) ------------------------------------------------
    def _open(self, side, price, move):
        eq = self._equity()
        if eq <= 0:
            return
        margin = eq                                     # ALL-IN: whole account is the margin
        notional = margin * LEVERAGE
        qty = notional / price
        self.balance -= notional * FEE                  # 0 (Lighter has no fees)
        liq = price * (1 - 1 / LEVERAGE) if side == "long" else price * (1 + 1 / LEVERAGE)
        self.pos = {"id": uuid.uuid4().hex[:6], "side": side, "entry": price, "qty": qty,
                    "notional": round(notional, 2), "margin": round(margin, 2),
                    "best_pnl": 0.0, "stop_pnl": round(margin * STOP_MARGIN, 4),
                    "trail_on": False, "liq": liq, "entry_move_pct": round(move * 100, 3),
                    "opened_at": time.time()}
        self._note(f"OPEN {side.upper()} BTC @ ${price:,.1f} · followed {LOOKBACK_SEC:.0f}s move "
                   f"{move*100:+.3f}% · ${notional:,.0f} (20x on ${margin:,.2f}) · "
                   f"stop -5% margin", "open")
        self._save()

    # ---- manage: the "improved TP/SL 5%" trailing exit (only this) --------
    def _manage(self, px):
        p = self.pos
        long = p["side"] == "long"
        pnl = p["qty"] * ((px - p["entry"]) if long else (p["entry"] - px))
        p["best_pnl"] = max(float(p.get("best_pnl", 0.0)), pnl)
        margin = p["margin"]
        best_margin = (p["best_pnl"] / margin) if margin else 0.0

        if best_margin >= BE_TRIGGER:                   # +3% margin -> lock break-even
            p["stop_pnl"] = max(p["stop_pnl"], 0.0)
        if best_margin >= TRAIL_TRIGGER:                # +5% margin -> trail by 5% margin gap
            p["stop_pnl"] = max(p["stop_pnl"], p["best_pnl"] - margin * TRAIL_GAP)
            p["trail_on"] = True

        if pnl <= margin * LIQ_MARGIN:                  # isolated liquidation backstop
            self._close(px, "liquidation"); return
        if pnl <= p["stop_pnl"]:
            reason = "trailing_take_profit_5%" if p["stop_pnl"] >= 0 else "stoploss_5%"
            self._close(px, f"{reason} ({p['stop_pnl'] / margin * 100:+.1f}% margin)")

    def _close(self, price, reason):
        p = self.pos
        long = p["side"] == "long"
        pnl = p["qty"] * ((price - p["entry"]) if long else (p["entry"] - price)) - p["notional"] * FEE
        pnl = max(pnl, -p["margin"])                    # isolated 20x: can't lose more than margin
        self.balance = max(0.0, self.balance + pnl)
        self.history.insert(0, {"side": p["side"], "entry": round(p["entry"], 1), "exit": round(price, 1),
                                "pnl": round(pnl, 2), "reason": reason,
                                "entry_move_pct": p.get("entry_move_pct"),
                                "opened_at": p["opened_at"], "closed_at": time.time()})
        self.history = self.history
        self.pos = None
        self._note(f"CLOSE {p['side'].upper()} @ ${price:,.1f} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")
        self._save()

    # ---- helpers + controls ----------------------------------------------
    def _equity(self):
        eq = self.balance
        if self.pos and self.price:
            long = self.pos["side"] == "long"
            eq += self.pos["qty"] * ((self.price - self.pos["entry"]) if long
                                     else (self.pos["entry"] - self.price))
        return eq

    def _set_status(self):
        eq = self._equity()
        self.status = (f"watching chart · {self.news_seen} news seen · "
                       f"{'in trade' if self.pos else 'flat'} · equity ${eq:,.2f} · "
                       f"{'armed' if self.enabled else 'paused'}")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self.news_seen = 0
        self.last_signal = ""
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        pos = None
        if self.pos:
            p = self.pos
            long = p["side"] == "long"
            upnl = p["qty"] * ((self.price - p["entry"]) if long else (p["entry"] - self.price)) if self.price else 0.0
            stop_price = (p["entry"] + p["stop_pnl"] / p["qty"]) if long else (p["entry"] - p["stop_pnl"] / p["qty"])
            pos = {"side": p["side"], "entry": round(p["entry"], 1), "notional": p["notional"],
                   "mark": round(self.price, 1), "upnl": round(upnl, 2),
                   "stop": round(stop_price, 1), "liq": round(p["liq"], 1),
                   "best_pnl": round(p.get("best_pnl", 0.0), 2), "trail_on": bool(p.get("trail_on")),
                   "stop_pct_margin": round(p["stop_pnl"] / p["margin"] * 100, 1) if p["margin"] else 0.0,
                   "entry_move_pct": p.get("entry_move_pct")}
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled, "status": self.status, "price": round(self.price, 1) if self.price else None,
            "lookback_sec": LOOKBACK_SEC, "move_thresh_pct": MOVE_THRESH * 100,
            "last_signal": self.last_signal, "news_seen": self.news_seen,
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BAL, "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100, 2),
            "position": pos, "trades": len(self.history), "wins": wins,
            "history": self.history, "log": self.log[:25],
        }
