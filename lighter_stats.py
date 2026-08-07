"""
================================================================================
 lighter_stats.py  —  MANUAL real-money trade tracker (PnL / win-rate / stats)
================================================================================
Tracks every MANUAL round-trip from the Lighter "REAL MONEY" panel and computes
realized P&L straight from LIGHTER's OWN account equity — not from estimated
prices. When you open we snapshot your perps EQUITY; when the position goes flat
again the equity DELTA is the exact realized P&L (Lighter is zero-fee), the same
number Lighter's website shows. That's why the figure now matches the exchange.

Persists to data/lighter_manual_stats.json (+ CSV) so it survives a restart.
================================================================================
"""
from __future__ import annotations
import csv
import json
import os
import time

import config

STATE_PATH = os.path.join(config.DATA_DIR, "lighter_manual_stats.json")
CSV_PATH = os.path.join(config.DATA_DIR, "lighter_manual_trades.csv")


class ManualTracker:
    def __init__(self):
        self.open: dict | None = None      # the live manual position (None if flat)
        self.history: list[dict] = []      # closed round-trips, newest first
        self._load()

    # ---- events ------------------------------------------------------------
    def on_open(self, market: str, side: str, entry: float, size: float, equity: float | None):
        """A manual order opened a position. `equity` = perps equity at open (the baseline)."""
        side = "long" if str(side).lower() in ("long", "buy") else "short"
        try:
            entry = float(entry); size = abs(float(size))
        except Exception:
            entry = 0.0; size = 0.0
        self.open = {"market": market or "BTC", "side": side, "entry": entry, "size": size,
                     "equity_open": (float(equity) if equity not in (None, "") else None),
                     "ts": time.time()}
        self._save()

    def on_state(self, has_position: bool, equity: float | None, mark: float | None = None):
        """Called on every account poll. If we were tracking a manual position and Lighter now
        shows FLAT, the trade closed -> realized P&L = equity now - equity at open (EXACT)."""
        if not self.open or has_position:
            return
        o = self.open
        # grace: ignore the first ~2s after open (settlement lag can momentarily read flat)
        if time.time() - o["ts"] < 2.5:
            return
        pnl = None
        if o.get("equity_open") is not None and equity not in (None, ""):
            pnl = float(equity) - o["equity_open"]           # TRUE realized P&L from Lighter
        elif mark and o.get("entry"):                        # fallback if we never got equity
            d = 1.0 if o["side"] == "long" else -1.0
            pnl = (float(mark) - o["entry"]) * o["size"] * d
        if pnl is None:
            self.open = None; self._save(); return
        rec = {"market": o["market"], "side": o["side"], "entry": round(o.get("entry") or 0, 2),
               "exit": round(float(mark), 2) if mark else None, "size": round(o.get("size") or 0, 6),
               "pnl": round(pnl, 4), "opened_at": o["ts"], "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history[:1000]
        self.open = None
        self._save()
        self._append_csv(rec)
        return rec

    # ---- numbers the page shows -------------------------------------------
    def stats(self) -> dict:
        pnls = [h["pnl"] for h in self.history]
        if not pnls:
            return {"n": 0, "pnl": 0.0, "winrate": None, "wins": 0, "losses": 0,
                    "avg_win": 0.0, "avg_loss": 0.0, "best": 0.0, "worst": 0.0,
                    "profit_factor": None, "history": [], "open": self.open}
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gw, gl = sum(wins), abs(sum(losses))
        return {
            "n": len(pnls), "pnl": round(sum(pnls), 2),
            "winrate": round(100 * len(wins) / len(pnls), 1),
            "wins": len(wins), "losses": len(losses),
            "avg_win": round(gw / len(wins), 3) if wins else 0.0,
            "avg_loss": round(-gl / len(losses), 3) if losses else 0.0,
            "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
            "profit_factor": round(gw / gl, 2) if gl > 0 else None,
            "history": self.history[:25], "open": self.open,
        }

    # ---- persistence -------------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"open": self.open, "history": self.history[:1000]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(STATE_PATH):
                d = json.load(open(STATE_PATH))
                self.open = d.get("open")
                self.history = d.get("history", []) or []
        except Exception:
            self.open = None; self.history = []

    def _append_csv(self, rec):
        try:
            new = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "market", "side", "entry", "exit", "size", "pnl"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["market"], rec["side"], rec["entry"], rec["exit"], rec["size"], rec["pnl"]])
        except Exception:
            pass
