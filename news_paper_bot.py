"""
================================================================================
 news_paper_bot.py — "News Paper Bot"  (PAPER MONEY ONLY)
================================================================================
Watches the website's existing news feed, runs each headline through the
deterministic engine (news_paper_engine.py), and opens SIMULATED trades with a
fake balance. There is NO exchange connection and NO real order anywhere in this
file — it only reads live prices (read-only) to mark paper P&L.

Stores 6 "collections" inside ONE json file (config.DATA_DIR/news_paper_state.json):
  paper_account, paper_positions, paper_trades, bot_decisions,
  processed_news_hashes, bot_settings
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
import news_paper_engine as engine

STATE_PATH = os.path.join(config.DATA_DIR, "news_paper_state.json")

SUPPORTED = engine.SUPPORTED          # ("BTCUSDT","ETHUSDT","SOLUSDT")
POLL_SEC = 1.0                        # mark positions + sample prices every ~1s

# Editable settings + their defaults (exactly the spec list). Percent values are
# stored as percentages (0.35 means 0.35%); we divide by 100 when applying them.
DEFAULT_SETTINGS = {
    "starting_balance": 100.0,
    "leverage": 20.0,                 # ALL-IN: notional = balance * leverage on every trade
    "max_open_trades": 1,
    "max_daily_loss_pct": 3.0,
    "min_impact_score": 8.0,
    "min_confidence": 0.70,
    "stop_loss_pct": 0.35,
    "take_profit_pct": 0.70,
    "dup_cooldown_min": 20,
    "min_confirmation_move_pct": 0.05,
    "max_already_moved_pct": 0.60,
    "max_hold_min": 15,
}

# type coercion + sane clamps for settings coming from the UI
_SETTING_BOUNDS = {
    "starting_balance": (10.0, 1e9), "leverage": (1.0, 125.0),
    "max_open_trades": (1, 20), "max_daily_loss_pct": (0.1, 100.0),
    "min_impact_score": (0.0, 30.0), "min_confidence": (0.0, 1.0),
    "stop_loss_pct": (0.01, 50.0), "take_profit_pct": (0.01, 100.0),
    "dup_cooldown_min": (0, 1440), "min_confirmation_move_pct": (0.0, 5.0),
    "max_already_moved_pct": (0.0, 20.0), "max_hold_min": (1, 1440),
}
_INT_SETTINGS = {"max_open_trades", "dup_cooldown_min", "max_hold_min"}


class NewsPaperBot:
    def __init__(self, market=None):
        self.market = market
        self.enabled = False                       # OFF until the user presses Start
        self.settings = dict(DEFAULT_SETTINGS)
        self.balance = float(self.settings["starting_balance"])
        self.realized_pnl = 0.0
        self.peak_equity = self.balance
        self.max_drawdown = 0.0
        self.day_key = time.strftime("%Y-%m-%d")
        self.day_pnl = 0.0
        self.positions: list[dict] = []
        self.trades: list[dict] = []               # closed trades (newest first)
        self.decisions: list[dict] = []            # decision log (newest first)
        self.recent_news: list[dict] = []          # raw news seen (newest first)
        self.processed: dict[str, float] = {}      # headline hash -> last-seen ts (dedup)
        self._hist: dict[str, deque] = {s: deque(maxlen=180) for s in SUPPORTED}  # (ts,px)
        self.status = "OFF"
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence (one file, the 6 collections) -----------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "paper_account": {"enabled": self.enabled, "balance": self.balance,
                                      "realized_pnl": self.realized_pnl, "peak_equity": self.peak_equity,
                                      "max_drawdown": self.max_drawdown, "day_key": self.day_key,
                                      "day_pnl": self.day_pnl},
                    "paper_positions": self.positions,
                    "paper_trades": self.trades,
                    "bot_decisions": self.decisions[:200],
                    "processed_news_hashes": self.processed,
                    "bot_settings": self.settings,
                    "recent_news": self.recent_news[:50],
                }, f)
        except Exception as e:
            print(f"[newspaper] save error: {e}")

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.settings = {**DEFAULT_SETTINGS, **(d.get("bot_settings") or {})}
            acc = d.get("paper_account") or {}
            self.enabled = bool(acc.get("enabled", False))
            self.balance = float(acc.get("balance", self.settings["starting_balance"]))
            self.realized_pnl = float(acc.get("realized_pnl", 0.0))
            self.peak_equity = float(acc.get("peak_equity", self.balance))
            self.max_drawdown = float(acc.get("max_drawdown", 0.0))
            self.day_key = acc.get("day_key", time.strftime("%Y-%m-%d"))
            self.day_pnl = float(acc.get("day_pnl", 0.0))
            self.positions = d.get("paper_positions") or []
            self.trades = d.get("paper_trades") or []
            self.decisions = d.get("bot_decisions") or []
            self.processed = d.get("processed_news_hashes") or {}
            self.recent_news = d.get("recent_news") or []
            print(f"[newspaper] restored: bal ${self.balance:.2f}, {len(self.trades)} trades, "
                  f"{len(self.positions)} open, {'ON' if self.enabled else 'OFF'}")
        except Exception as e:
            print(f"[newspaper] load error: {e}")

    # ---- live price (READ ONLY — no orders ever) -------------------------
    def _px(self, symbol):
        try:
            p = self.market.price(symbol) if self.market else None
            return float(p) if p else None
        except Exception:
            return None

    def _price_ago(self, buf, secs, now):
        """Price sample closest to `secs` ago, or None if the buffer doesn't reach back."""
        if not buf or (now - buf[0][0]) < secs * 0.6:
            return None
        target = now - secs
        return min(buf, key=lambda tp: abs(tp[0] - target))[1]

    # ---- market confirmation: is price already moving our way? -----------
    def _confirmation(self, symbol, direction):
        """Return (confirmation_score, already_moved_pct, limited_data)."""
        now = time.time()
        now_px = self._px(symbol)
        buf = self._hist.get(symbol)
        if not now_px or not buf or len(buf) < 2:
            return 0.0, 0.0, True                  # neutral, but flag limited data
        exp = 1 if direction == "LONG" else -1
        expected_moves = []
        for look in (10, 30, 60):
            past = self._price_ago(buf, look, now)
            if past:
                expected_moves.append(((now_px - past) / past) * exp)   # + = moving our way
        if not expected_moves:
            return 0.0, 0.0, True
        best = max(expected_moves)                                       # most in our favour
        already = max(0.0, best) * 100.0                                 # how far it already ran our way
        minmove = self.settings["min_confirmation_move_pct"] / 100.0
        if best >= 2 * minmove:
            score = 2.0                            # strong move in expected direction
        elif best >= minmove:
            score = 1.0                            # slight move in expected direction
        elif best <= -minmove:
            score = -2.0                           # moving the OPPOSITE way
        else:
            score = -1.0                           # has data but flat = no confirmation
        return score, already, False

    # ---- dedup -----------------------------------------------------------
    def _is_duplicate(self, h):
        last = self.processed.get(h)
        if not last:
            return False
        return (time.time() - last) <= self.settings["dup_cooldown_min"] * 60

    def _prune_processed(self):
        cutoff = time.time() - max(self.settings["dup_cooldown_min"] * 60 * 3, 3600)
        self.processed = {k: v for k, v in self.processed.items() if v >= cutoff}

    def _daily_loss_hit(self):
        self._roll_day()
        limit = self.settings["starting_balance"] * self.settings["max_daily_loss_pct"] / 100.0
        return self.day_pnl <= -limit

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day_key:
            self.day_key, self.day_pnl = today, 0.0

    # ====================================================================
    #  NEWS ENTRY POINT — called for every news item on the site
    # ====================================================================
    def on_news(self, source: str = "", text: str = ""):
        """Wiring shim for main.py's on_news(source, text)."""
        return self.process_news({"source": source, "headline": text, "timestamp": time.time()})

    def process_news(self, news: dict, force: bool = False):
        """Classify -> confirm -> score -> gate -> (maybe) open a paper trade. Always
        logs a decision (TRADE or SKIP with reason). `force` lets the Test button run
        even while the bot is OFF."""
        try:
            headline = (news.get("headline") or news.get("text") or "").strip()
            if not headline:
                return None
            ts = float(news.get("timestamp") or time.time())
            source = news.get("source") or ""
            body = news.get("body") or ""
            h = engine.headline_hash(headline)
            nid = news.get("id") or h

            cls = engine.classify({"headline": headline, "body": body, "source": source})
            is_dup = self._is_duplicate(h)

            confirm_score, already, limited = 0.0, 0.0, True
            if cls["symbol"]:
                confirm_score, already, limited = self._confirmation(cls["symbol"], cls["direction"])

            ctx = {"settings": self.settings, "is_duplicate": is_dup, "similar_recent": is_dup,
                   "confirmation_score": confirm_score, "already_moved_pct": already,
                   "confirmation_limited": limited}
            dec = engine.score({"headline": headline, "body": body, "source": source}, cls, ctx)

            # ---- operational gates (these need bot/account state) ----
            trade, reason = dec["trade"], dec["reason"]
            if trade:
                if not self.enabled and not force:
                    trade, reason = False, "bot is OFF"
                elif dec["symbol"] not in SUPPORTED:
                    trade, reason = False, "symbol not supported"
                elif self._px(dec["symbol"]) is None:
                    trade, reason = False, "symbol price unavailable"
                elif len(self.positions) >= int(self.settings["max_open_trades"]):
                    trade, reason = False, f"max open trades ({int(self.settings['max_open_trades'])}) reached"
                elif self._daily_loss_hit():
                    trade, reason = False, "daily loss limit hit"

            entry = sl = tp = None
            pos = None
            if trade:
                pos = self._open(dec, {"id": nid, "headline": headline, "source": source})
                if pos:
                    entry, sl, tp = pos["entry"], pos["sl"], pos["tp"]
                else:
                    trade, reason = False, "could not size position"

            d = {"id": uuid.uuid4().hex[:8], "ts": ts, "headline": headline[:240], "source": source,
                 "category": dec["category"], "symbol": dec["symbol"], "direction": dec["direction"],
                 "impact": dec["impactScore"], "confidence": dec["confidence"],
                 "decision": "TRADE" if trade else "SKIP", "reason": reason,
                 "entry": entry, "sl": sl, "tp": tp, "result": None,
                 "pos_id": pos["id"] if pos else None, "breakdown": dec.get("scoreBreakdown", "")}
            self.decisions.insert(0, d)
            self.decisions = self.decisions[:200]
            self.recent_news.insert(0, {"ts": ts, "source": source, "headline": headline[:200]})
            self.recent_news = self.recent_news[:50]
            self.processed[h] = ts
            self._prune_processed()
            self._save()
            return d
        except Exception as e:
            print(f"[newspaper] process_news error: {type(e).__name__}: {e}")
            return None

    # ---- paper order open (NO real order) --------------------------------
    def _open(self, dec, news):
        symbol, direction = dec["symbol"], dec["direction"]
        price = self._px(symbol)
        if not price or self.balance <= 0:
            return None
        sl_frac = dec["stopLossPct"] / 100.0
        tp_frac = dec["takeProfitPct"] / 100.0
        if sl_frac <= 0:
            return None
        lev = float(self.settings["leverage"])
        margin = self.balance                          # ALL-IN: the whole balance backs the trade
        notional = margin * lev                        # 20x notional
        qty = notional / price
        if direction == "LONG":
            sl, tp = price * (1 - sl_frac), price * (1 + tp_frac)
            liq = price * (1 - 1.0 / lev)              # ~5% adverse @ 20x wipes the margin
        else:
            sl, tp = price * (1 + sl_frac), price * (1 - tp_frac)
            liq = price * (1 + 1.0 / lev)
        pos = {"id": uuid.uuid4().hex[:8], "symbol": symbol, "direction": direction,
               "entry": round(price, 4), "qty": qty, "notional": round(notional, 2),
               "margin": round(margin, 2), "leverage": lev,
               "sl": round(sl, 4), "tp": round(tp, 4), "liq": round(liq, 4),
               "open_ts": time.time(), "news_id": news.get("id"), "headline": news.get("headline", "")[:200]}
        self.positions.append(pos)
        print(f"[newspaper] OPEN {direction} {symbol} @ {price:.2f} "
              f"(${notional:,.0f} = {lev:g}x on ${margin:.2f}, SL {sl:.2f}, TP {tp:.2f}) · {news.get('headline','')[:50]}")
        return pos

    def _close(self, pos, exit_px, reason):
        long = pos["direction"] == "LONG"
        exp = 1 if long else -1
        pnl = pos["qty"] * (exit_px - pos["entry"]) * exp
        pnl = max(pnl, -pos.get("margin", self.balance))   # isolated: can't lose more than the margin
        pnl_pct = (exit_px - pos["entry"]) / pos["entry"] * 100.0 * exp
        self.balance = max(0.0, self.balance + pnl)
        self.realized_pnl += pnl
        self._roll_day()
        self.day_pnl += pnl
        tr = {"id": pos["id"], "symbol": pos["symbol"], "direction": pos["direction"],
              "entry": pos["entry"], "exit": round(exit_px, 4), "pnl": round(pnl, 2),
              "pnl_pct": round(pnl_pct, 3), "reason": reason, "headline": pos.get("headline", ""),
              "open_ts": pos["open_ts"], "close_ts": time.time(), "notional": pos["notional"]}
        self.trades.insert(0, tr)
        self.trades = self.trades
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        for dd in self.decisions:                  # write the result back onto the opening decision
            if dd.get("pos_id") == pos["id"]:
                dd["result"] = f"{reason} {pnl:+.2f} ({pnl_pct:+.2f}%)"
                break
        print(f"[newspaper] CLOSE {pos['direction']} {pos['symbol']} @ {exit_px:.2f} · {reason} · ${pnl:+.2f}")

    # ---- the loop: sample prices, mark positions, hit SL/TP/time ---------
    async def manage_loop(self):
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                now = time.time()
                for s in SUPPORTED:                # always sample (needed for confirmation later)
                    px = self._px(s)
                    if px:
                        self._hist[s].append((now, px))
                self._roll_day()
                for pos in list(self.positions):   # check exits
                    self._check_exit(pos, now)
                self._update_equity()
                self.status = "ON" if self.enabled else "OFF"
            except Exception as e:
                print(f"[newspaper] loop error: {type(e).__name__}: {e}")

    def _check_exit(self, pos, now):
        px = self._px(pos["symbol"])
        if not px:
            return
        long = pos["direction"] == "LONG"
        liq = pos.get("liq")
        reason, exit_px = None, px
        if liq and long and px <= liq:
            reason, exit_px = "liquidation", liq
        elif liq and (not long) and px >= liq:
            reason, exit_px = "liquidation", liq
        elif long and px <= pos["sl"]:
            reason, exit_px = "stop_loss", pos["sl"]
        elif long and px >= pos["tp"]:
            reason, exit_px = "take_profit", pos["tp"]
        elif (not long) and px >= pos["sl"]:
            reason, exit_px = "stop_loss", pos["sl"]
        elif (not long) and px <= pos["tp"]:
            reason, exit_px = "take_profit", pos["tp"]
        elif (now - pos["open_ts"]) >= self.settings["max_hold_min"] * 60:
            reason, exit_px = "time_exit", px
        if reason:
            self._close(pos, exit_px, reason)
            self._save()

    def _unrealized(self):
        u = 0.0
        for p in self.positions:
            px = self._px(p["symbol"])
            if px:
                exp = 1 if p["direction"] == "LONG" else -1
                u += p["qty"] * (px - p["entry"]) * exp
        return u

    def _equity(self):
        return self.balance + self._unrealized()

    def _update_equity(self):
        eq = self._equity()
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            dd = (self.peak_equity - eq) / self.peak_equity * 100.0
            self.max_drawdown = max(self.max_drawdown, dd)

    # ---- controls --------------------------------------------------------
    def start(self):
        self.enabled = True
        self.status = "ON"
        self._save()
        return {"ok": True, "enabled": True}

    def stop(self):
        self.enabled = False
        self.status = "OFF"
        self._save()
        return {"ok": True, "enabled": False}

    def reset(self):
        sb = float(self.settings["starting_balance"])
        self.balance = sb
        self.realized_pnl = 0.0
        self.peak_equity = sb
        self.max_drawdown = 0.0
        self.day_pnl = 0.0
        self.day_key = time.strftime("%Y-%m-%d")
        self.positions = []
        self.trades = []
        self.decisions = []
        self.recent_news = []
        self.processed = {}
        self._save()
        return {"ok": True}

    def update_settings(self, new: dict):
        for k, v in (new or {}).items():
            if k not in DEFAULT_SETTINGS:
                continue
            try:
                val = int(v) if k in _INT_SETTINGS else float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = _SETTING_BOUNDS.get(k, (None, None))
            if lo is not None:
                val = max(lo, min(hi, val))
            self.settings[k] = val
        self._save()
        return {"ok": True, "settings": self.settings}

    def test_news(self, headline: str, source: str = "Test (Reuters)"):
        """Manual Test-News: process even when OFF so the user can see TRADE/SKIP."""
        return self.process_news({"headline": headline, "source": source or "Test (Reuters)",
                                  "timestamp": time.time()}, force=True)

    # ---- snapshot for the UI ---------------------------------------------
    def state(self):
        eq = self._equity()
        sb = float(self.settings["starting_balance"])
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        n = len(self.trades)
        open_pos = []
        for p in self.positions:
            px = self._px(p["symbol"])
            exp = 1 if p["direction"] == "LONG" else -1
            up = p["qty"] * ((px - p["entry"]) * exp) if px else 0.0
            open_pos.append({**{k: p[k] for k in ("id", "symbol", "direction", "entry", "qty",
                                                  "notional", "sl", "tp", "open_ts", "headline")},
                             "current": round(px, 4) if px else None, "upnl": round(up, 2)})
        return {
            "enabled": self.enabled, "status": "ON" if self.enabled else "OFF",
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "starting_balance": sb, "day_pnl": round(self.day_pnl, 2),
            "total_pnl": round(eq - sb, 2), "total_pnl_pct": round((eq / sb - 1) * 100, 2) if sb else 0,
            "realized_pnl": round(self.realized_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "num_trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
            "open_count": len(self.positions), "max_open": int(self.settings["max_open_trades"]),
            "positions": open_pos, "trades": self.trades, "decisions": self.decisions[:60],
            "recent_news": self.recent_news[:25], "settings": self.settings,
            "supported": list(SUPPORTED),
        }
