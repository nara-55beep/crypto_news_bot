"""
================================================================================
 journal.py  —  PERFORMANCE / TRADE JOURNAL  (the honest scoreboard)
================================================================================
Reads the CLOSED trades from every paper account you run —
  • the crypto news bot   (SQLite data/trades.db)
  • the manual account     (data/manual_state.json)
  • the news+whale bot      (data/news_whale_state.json)
  • the stock news bot       (data/stock_bot_state.json)
— normalizes them, and computes the numbers that actually tell you whether a
strategy works: win rate, average win vs average loss, profit factor, expectancy,
and an equity curve over time. No opinions, just your own results.
================================================================================
"""

from __future__ import annotations

import json
import os
import sqlite3

import config


def _load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _crypto_news_trades():
    out = []
    try:
        con = sqlite3.connect(config.DB_PATH)
        rows = con.execute(
            "SELECT symbol, side, entry_price, exit_price, pnl, margin, reason, "
            "closed_at FROM trades ORDER BY closed_at ASC").fetchall()
        con.close()
        for sym, side, ep, xp, pnl, margin, reason, ca in rows:
            out.append({"account": "news", "symbol": sym or "BTCUSDT", "side": side or "",
                        "entry": ep or 0, "exit": xp or 0, "pnl": pnl or 0,
                        "margin": margin or 0, "reason": reason or "", "closed_at": ca or 0})
    except Exception:
        pass
    return out


def _state_history(path, account, default_symbol="BTCUSDT"):
    d = _load_json(path)
    out = []
    for h in (d.get("history", []) or []):
        out.append({"account": account, "symbol": h.get("symbol", default_symbol),
                    "side": h.get("side", ""), "entry": h.get("entry", 0),
                    "exit": h.get("exit", 0), "pnl": h.get("pnl", 0),
                    "margin": h.get("margin", 0), "reason": h.get("reason", ""),
                    "closed_at": h.get("closed_at", 0)})
    return out


ACCOUNTS = {
    "news":   "Crypto news bot",
    "manual": "Manual",
    "whale":  "News + Whale bot",
    "stock":  "Stock news bot",
}


def all_trades():
    t = []
    t += _crypto_news_trades()
    t += _state_history(os.path.join(config.DATA_DIR, "manual_state.json"), "manual")
    t += _state_history(os.path.join(config.DATA_DIR, "news_whale_state.json"), "whale")
    t += _state_history(os.path.join(config.DATA_DIR, "stock_bot_state.json"), "stock")
    t.sort(key=lambda x: x.get("closed_at") or 0)
    return t


def stats(account="all"):
    trades = all_trades()
    if account != "all":
        trades = [x for x in trades if x["account"] == account]

    n = len(trades)
    wins = [x for x in trades if x["pnl"] > 0]
    losses = [x for x in trades if x["pnl"] < 0]
    gross_win = sum(x["pnl"] for x in wins)
    gross_loss = -sum(x["pnl"] for x in losses)          # positive number
    total = sum(x["pnl"] for x in trades)

    # equity curve: running cumulative P&L in trade order
    eq, run = [], 0.0
    for x in trades:
        run += x["pnl"]
        eq.append({"t": int(x.get("closed_at") or 0), "v": round(run, 2)})

    reasons = {}
    for x in trades:
        r = x["reason"] or "?"
        reasons[r] = reasons.get(r, 0) + 1

    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None   # None = no losing trades yet

    return {
        "account": account,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        "profit_factor": pf,
        "has_wins": gross_win > 0,
        "total_pnl": round(total, 2),
        "expectancy": round(total / n, 2) if n else 0,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "best": round(max((x["pnl"] for x in trades), default=0), 2),
        "worst": round(min((x["pnl"] for x in trades), default=0), 2),
        "equity": eq,
        "reasons": reasons,
        "recent": list(reversed(trades))[:50],
        "counts": {a: sum(1 for x in all_trades() if x["account"] == a) for a in ACCOUNTS},
    }
