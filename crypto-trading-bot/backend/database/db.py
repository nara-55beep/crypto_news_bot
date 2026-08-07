"""
db.py — tiny SQLite layer (no ORM, beginner-friendly).

Tables:
  trades   — every closed trade (paper or live)
  signals  — every signal the strategy produced (taken or not)
  equity   — periodic equity snapshots (for the equity curve)
  logs     — notable events/errors

The connection uses check_same_thread=False because the engine runs in a
background thread; all writes go through a lock to stay safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import List, Optional


class Database:
    def __init__(self, path: str = "trading_bot.db"):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT, symbol TEXT, side TEXT,
                    entry_time TEXT, entry_price REAL, qty REAL,
                    stop REAL, take REAL,
                    exit_time TEXT, exit_price REAL,
                    pnl REAL, pnl_pct REAL, fees REAL, reason TEXT,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT, symbol TEXT, side TEXT, price REAL,
                    reason TEXT, meta TEXT, taken INTEGER, created_at REAL
                );
                CREATE TABLE IF NOT EXISTS equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT, mode TEXT, equity REAL, created_at REAL
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT, level TEXT, message TEXT, created_at REAL
                );
                """
            )
            self.conn.commit()

    # ------------------------------- writers ----------------------------------
    def insert_trade(self, t: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO trades (mode,symbol,side,entry_time,entry_price,qty,stop,take,
                   exit_time,exit_price,pnl,pnl_pct,fees,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.get("mode"), t.get("symbol"), t.get("side"), t.get("entry_time"),
                    t.get("entry_price"), t.get("qty"), t.get("stop"), t.get("take"),
                    t.get("exit_time"), t.get("exit_price"), t.get("pnl"), t.get("pnl_pct"),
                    t.get("fees"), t.get("reason"), time.time(),
                ),
            )
            self.conn.commit()

    def insert_signal(self, sig: dict, taken: bool) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO signals (time,symbol,side,price,reason,meta,taken,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    sig.get("time"), sig.get("symbol"), sig.get("side"), sig.get("price"),
                    sig.get("reason"), json.dumps(sig.get("meta", {})), 1 if taken else 0, time.time(),
                ),
            )
            self.conn.commit()

    def insert_equity(self, mode: str, equity: float, when: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO equity (time,mode,equity,created_at) VALUES (?,?,?,?)",
                (when, mode, equity, time.time()),
            )
            self.conn.commit()

    def insert_log(self, level: str, message: str, when: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO logs (time,level,message,created_at) VALUES (?,?,?,?)",
                (when, level, message, time.time()),
            )
            self.conn.commit()

    # ------------------------------- readers ----------------------------------
    def recent_trades(self, limit: int = 100, mode: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM trades"
        args: tuple = ()
        if mode:
            q += " WHERE mode = ?"
            args = (mode,)
        q += " ORDER BY id DESC LIMIT ?"
        args = args + (limit,)
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 50) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self, mode: str, limit: int = 1000) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT time, equity FROM equity WHERE mode = ? ORDER BY id DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_logs(self, limit: int = 100) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
