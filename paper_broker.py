"""
================================================================================
 paper_broker.py  —  SIMULATED EXECUTION ENGINE  ($100 virtual, real prices)
================================================================================
A self-contained paper-trading futures account. It uses REAL live prices from
market_data, but fills are simulated and no real order ever leaves your machine.
This is the safe sandbox: you can lose the whole virtual $100 and learn exactly
nothing bad happens.

Models, simply but correctly:
  - balance (starts at config.STARTING_BALANCE_USDT)
  - positions opened with a chosen margin + leverage -> notional = margin*lev
  - mark-to-market PnL every tick
  - exits on stop-loss / take-profit / time-stop / liquidation
  - every trade and every news+signal is logged to SQLite (data/trades.db)

When you eventually go LIVE, you replace THIS class with one that sends real
Binance orders, keeping the same method names. Nothing else changes.
================================================================================
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict

import config

STATE_PATH = os.path.join(config.DATA_DIR, "paper_broker_state.json")


@dataclass
class Position:
    id: str
    symbol: str
    side: str               # "long" | "short"
    entry_price: float
    quantity: float         # in base asset (e.g. BTC)
    margin: float           # USDT committed
    leverage: int
    sl_price: float
    tp_price: float
    opened_at: float
    causal_chain: str = ""

    def unrealized(self, mark: float) -> float:
        if self.side == "long":
            return self.quantity * (mark - self.entry_price)
        return self.quantity * (self.entry_price - mark)


class PaperBroker:
    def __init__(self, market):
        self.market = market
        self.balance = config.STARTING_BALANCE_USDT
        self.positions: dict[str, Position] = {}
        self.consecutive_losses = 0
        self._init_db()
        self._load_state()

    # ---- persistence: balance + open positions survive restarts -----------
    # (closed trades already live in the SQLite trades table)
    def _save_state(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "balance": self.balance,
                    "consecutive_losses": self.consecutive_losses,
                    "positions": [asdict(p) for p in self.positions.values()],
                }, f)
        except Exception:
            pass

    def _load_state(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.balance = float(d.get("balance", self.balance))
            self.consecutive_losses = int(d.get("consecutive_losses", 0))
            for pd in d.get("positions", []) or []:
                try:
                    p = Position(**pd)
                    self.positions[p.id] = p
                except Exception:
                    pass
            print(f"[broker] restored: balance ${self.balance:.2f}, "
                  f"{len(self.positions)} open position(s)")
        except Exception:
            pass

    # ---- DB ---------------------------------------------------------------
    def _init_db(self):
        self.db = sqlite3.connect(config.DB_PATH)
        # If an OLD news table exists (from before the AI-decides change), its
        # columns differ. Rename it aside so the new schema can be created.
        try:
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(news)")]
            if cols and "decision" not in cols:
                ts = int(time.time())
                self.db.execute(f"ALTER TABLE news RENAME TO news_old_{ts}")
                print(f"[db] migrated old news table -> news_old_{ts} "
                      f"(old rows kept, new format starts fresh)")
                self.db.commit()
        except Exception:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS trades(
            id TEXT PRIMARY KEY, symbol TEXT, side TEXT,
            entry_price REAL, exit_price REAL, quantity REAL,
            margin REAL, leverage INTEGER, pnl REAL, reason TEXT,
            opened_at REAL, closed_at REAL, causal_chain TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS news(
            ts REAL, source TEXT, text TEXT, decision TEXT,
            direction TEXT, conviction TEXT, coins TEXT,
            traded INTEGER, note TEXT)""")
        self.db.commit()
        self._trim_news()          # clear any old backlog so the UI starts light

    def _trim_news(self, keep=100):
        """Keep only the most recent `keep` news rows so the table, the feed and
        the chart markers stay small — old news is deleted as new news arrives."""
        try:
            self.db.execute(
                "DELETE FROM news WHERE rowid NOT IN "
                "(SELECT rowid FROM news ORDER BY ts DESC LIMIT ?)", (keep,))
            self.db.commit()
        except Exception:
            pass

    def log_news(self, source, text, sig, traded: bool, note: str = "", ts=None):
        self.db.execute(
            "INSERT INTO news VALUES(?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), source, text[:500], sig.decision, sig.direction,
             sig.conviction, ",".join(sig.affected_assets), int(traded), note))
        self.db.commit()
        self._trim_news()

    def log_news_fast(self, source, text, ts=None):
        """Insert a news row the INSTANT it arrives (decision still pending), so the
        chart + feed can show it with zero wait for the AI. Returns the row id so the
        AI's verdict can update this very row later (update_news_decision) — no dup."""
        cur = self.db.execute(
            "INSERT INTO news VALUES(?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), source, text[:500], "…", "", "", "", 0, "analyzing"))
        self.db.commit()
        self._trim_news()
        return cur.lastrowid

    def update_news_decision(self, rowid, sig, traded: bool, note: str = ""):
        """Fill in the AI's verdict on the row already logged by log_news_fast."""
        try:
            self.db.execute(
                "UPDATE news SET decision=?, direction=?, conviction=?, coins=?, "
                "traded=?, note=? WHERE rowid=?",
                (sig.decision, sig.direction, sig.conviction,
                 ",".join(sig.affected_assets), int(traded), note, rowid))
            self.db.commit()
        except Exception:
            pass

    # ---- account ----------------------------------------------------------
    def equity(self) -> float:
        # balance excludes margin while a position is open (we subtract it on
        # open and return it on close), so equity must add the locked margin
        # back plus current unrealized PnL. Margin is reserved, not spent.
        eq = self.balance
        for p in self.positions.values():
            mark = self.market.price(p.symbol)
            eq += p.margin + (p.unrealized(mark) if mark else 0.0)
        return eq

    def open_position(self, symbol, side, margin, leverage, causal_chain=""):
        """Open a simulated position. margin in USDT, side long/short."""
        mark = self.market.price(symbol)
        if mark is None or margin <= 0:
            return None
        margin = min(margin, self.balance)            # can't risk what you lack
        leverage = max(1, min(leverage, config.MAX_LEVERAGE))
        notional = margin * leverage
        qty = notional / mark
        if side == "long":
            sl = mark * (1 - config.STOP_LOSS_PCT)
            tp = mark * (1 + config.TAKE_PROFIT_PCT)
        else:
            sl = mark * (1 + config.STOP_LOSS_PCT)
            tp = mark * (1 - config.TAKE_PROFIT_PCT)

        pos = Position(
            id=uuid.uuid4().hex[:8], symbol=symbol, side=side,
            entry_price=mark, quantity=qty, margin=margin, leverage=leverage,
            sl_price=sl, tp_price=tp, opened_at=time.time(),
            causal_chain=causal_chain)
        self.balance -= margin                        # margin is locked
        self.positions[pos.id] = pos
        self._save_state()
        print(f"[OPEN ] {side.upper():5} {symbol} @ {mark:.2f}  "
              f"margin=${margin:.2f} x{leverage}  SL={sl:.2f} TP={tp:.2f}  "
              f"({causal_chain})")
        return pos

    def _close(self, pos: Position, mark: float, reason: str):
        pnl = pos.unrealized(mark)
        self.balance += pos.margin + pnl              # return margin +/- pnl
        self.positions.pop(pos.id, None)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self.db.execute(
            "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos.id, pos.symbol, pos.side, pos.entry_price, mark, pos.quantity,
             pos.margin, pos.leverage, pnl, reason, pos.opened_at, time.time(),
             pos.causal_chain))
        self.db.commit()
        self._save_state()
        print(f"[CLOSE] {pos.side.upper():5} {pos.symbol} @ {mark:.2f}  "
              f"PnL=${pnl:+.2f}  reason={reason}  equity=${self.equity():.2f}")

    def mark_to_market(self):
        """Call every loop tick. Closes positions that hit SL/TP/time/liq."""
        for pos in list(self.positions.values()):
            mark = self.market.price(pos.symbol)
            if mark is None:
                continue
            up = pos.unrealized(mark)
            # liquidation: loss eats (almost) all margin
            if up <= -pos.margin * 0.97:
                self._close(pos, mark, "liquidation"); continue
            if pos.side == "long":
                if mark <= pos.sl_price:  self._close(pos, mark, "stop_loss"); continue
                if mark >= pos.tp_price:  self._close(pos, mark, "take_profit"); continue
            else:
                if mark >= pos.sl_price:  self._close(pos, mark, "stop_loss"); continue
                if mark <= pos.tp_price:  self._close(pos, mark, "take_profit"); continue
            if (time.time() - pos.opened_at) / 60 >= config.MAX_HOLD_MINUTES:
                self._close(pos, mark, "time_stop")

    def open_symbols(self):
        return {p.symbol for p in self.positions.values()}

    # ---- reset ------------------------------------------------------------
    def reset(self):
        """Wipe this account back to a fresh start: close/forget all open
        positions, reset balance to the configured starting amount, and DELETE
        the closed-trade history from the DB. The `news` table is left intact so
        the chart's news dots and the news feed are not affected."""
        self.positions.clear()
        self.balance = config.STARTING_BALANCE_USDT
        self.consecutive_losses = 0
        try:
            self.db.execute("DELETE FROM trades")
            self.db.commit()
        except Exception:
            pass
        self._save_state()
        print(f"[broker] RESET -> balance ${self.balance:.2f}, trade history cleared")
        return True
