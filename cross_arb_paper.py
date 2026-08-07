"""
================================================================================
 cross_arb_paper.py  —  "CROSS-EXCHANGE ARBITRAGE"  (paper, all exchanges)
================================================================================
Watches BTC across EVERY exchange on the live tape (Binance, Bybit, OKX,
Coinbase, Kraken, Gate, KuCoin, Bitget, Hyperliquid — whichever are streaming)
and trades the price GAPS between them, fully hedged:

  - each tick it ranks every venue's live BTC price, finds the CHEAPEST and the
    DEAREST, and if their gap is wide enough it LONGS the cheap venue and SHORTS
    the dear one at equal size (delta-neutral).
  - it can run SEVERAL such pairs at once across different venues — every exchange
    holds at most one leg (it has one $100 margin slot).
  - each pair closes when ITS two venues' prices converge (banking the spread),
    or a leg liquidates, or it times out.

$100 of paper margin sits on EACH exchange separately (they don't share), so a big
one-sided move can liquidate a leg — risk grows with the leverage you pick. Profit
per pair = (entry gap - exit gap) x size. All paper, real live prices.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import config

STATE_PATH = os.path.join(config.DATA_DIR, "cross_arb_state.json")

START_PER_EX  = 100.0        # paper margin available on EACH exchange (USDT)
ENTRY_BPS     = 4.0          # open a pair when its gap >= this many basis points (1 bp = 0.01%)
CONVERGE_BPS  = 1.0          # close a pair when its gap shrinks back to this
MAX_HOLD_SEC  = 3600         # safety: close a stuck pair after this long
STALE_MS      = 30000        # ignore a venue whose last trade is older than this
POLL_SEC      = 2.0

LEVS = {"1": "1x (safe, tiny P&L)", "5": "5x", "10": "10x", "20": "20x (risky)"}
DEFAULT_LEV = 10


class CrossArbBot:
    def __init__(self, market=None, whales=None):
        self.market = market           # MarketData (Binance mark-price fallback)
        self.whales = whales           # orderbook.WhaleTracker (live per-exchange tape)
        self.enabled = True
        self.lev = DEFAULT_LEV
        self.bal: dict[str, float] = {}    # exchange -> paper balance (lazily $100 each)
        self.positions: list[dict] = []    # open hedged pairs
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.prices: dict[str, float] = {} # last seen live prices (for the panel)
        self.status = "starting…"
        self._load()

    def attach(self, market, whales):
        self.market = market
        self.whales = whales

    # ---- persistence ------------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"enabled": self.enabled, "lev": self.lev, "bal": self.bal,
                           "positions": self.positions, "history": self.history,
                           "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.lev = int(d.get("lev", self.lev))
            self.bal = {k: float(v) for k, v in (d.get("bal") or {}).items()}
            self.positions = d.get("positions", []) or []
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            print(f"[arb] restored: {len(self.bal)} venues, {len(self.history)} trades, "
                  f"{len(self.positions)} open pair(s)")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[arb] {msg}")

    # ---- live prices: latest fresh BTC trade per venue across the whole tape
    def _live_prices(self):
        out = {}
        targets = set(getattr(config, "EXCHANGES", []))
        if self.whales is not None:
            now_ms = time.time() * 1000
            scanned = 0
            for t in reversed(self.whales.tape):
                ex = t.get("ex")
                if ex and ex not in out:
                    if now_ms - t["ts"] <= STALE_MS:
                        out[ex] = t["price"]
                    else:
                        out[ex] = None        # seen but stale -> mark so we don't keep scanning it
                if targets and all(e in out for e in targets):
                    break
                scanned += 1
                if scanned > 30000:
                    break
            out = {ex: p for ex, p in out.items() if p is not None}
        if "binance" not in out and self.market is not None:   # Binance mark-price fallback
            try:
                pb = self.market.price("BTCUSDT")
                if pb:
                    out["binance"] = pb
            except Exception:
                pass
        return out

    @staticmethod
    def _leg_pnl(leg, px):
        if leg["side"] == "long":
            return leg["qty"] * (px - leg["entry"])
        return leg["qty"] * (leg["entry"] - px)

    def _busy(self):
        b = set()
        for pr in self.positions:
            for leg in pr["legs"]:
                b.add(leg["ex"])
        return b

    # ---- the loop ---------------------------------------------------------
    async def manage_loop(self):
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                self._tick()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:80]}", "error")

    def _tick(self):
        prices = self._live_prices()
        self.prices = prices
        for ex in prices:                      # $100 on each venue, allocated when first seen
            self.bal.setdefault(ex, START_PER_EX)
        self._manage_all(prices)               # close converged / liquidated / timed-out pairs
        if self.enabled:
            self._open_pairs(prices)           # open new pairs on the widest free gaps
        self._set_status(prices)

    def _set_status(self, prices):
        if len(prices) < 2:
            self.status = f"need >=2 live exchanges (have {len(prices)}: {', '.join(prices) or 'none'})"
            return
        lo = min(prices.values()); hi = max(prices.values())
        gap = (hi - lo) / ((hi + lo) / 2) * 1e4
        self.status = (f"{len(prices)} venues live · widest gap {gap:.1f} bps · "
                       f"{len(self.positions)} pair(s) open")

    def _open_pairs(self, prices):
        max_pairs = len(prices) // 2
        while len(self.positions) < max_pairs:
            busy = self._busy()
            free = {ex: p for ex, p in prices.items()
                    if ex not in busy and self.bal.get(ex, 0) > 1}
            if len(free) < 2:
                break
            cheap = min(free, key=free.get)
            dear = max(free, key=free.get)
            pc, pd = free[cheap], free[dear]
            mid = (pc + pd) / 2.0
            gap_bps = (pd - pc) / mid * 1e4
            if cheap == dear or gap_bps < ENTRY_BPS:
                break
            if not self._open(cheap, pc, dear, pd, gap_bps):
                break

    def _open(self, cheap, pc, dear, pd, gap_bps):
        qty = (START_PER_EX * self.lev) / max(pc, pd)    # size off dearer leg so its margin ~ $100
        m_long = qty * pc / self.lev
        m_short = qty * pd / self.lev
        if self.bal.get(cheap, 0) < m_long * 0.999 or self.bal.get(dear, 0) < m_short * 0.999:
            return False
        self.bal[cheap] -= m_long
        self.bal[dear] -= m_short
        self.positions.append({
            "id": uuid.uuid4().hex[:6], "a": cheap, "b": dear,
            "legs": [
                {"ex": cheap, "side": "long", "entry": pc, "qty": qty, "margin": m_long},
                {"ex": dear, "side": "short", "entry": pd, "qty": qty, "margin": m_short},
            ],
            "opened_at": time.time(), "entry_gap_bps": gap_bps,
        })
        self._note(f"OPEN  LONG {cheap} @ {pc:,.1f}  /  SHORT {dear} @ {pd:,.1f}  ·  "
                   f"gap {gap_bps:.1f} bps · {self.lev}x · {qty:.4f} BTC", "open")
        self._save()
        return True

    def _manage_all(self, prices):
        for pr in list(self.positions):
            pa = prices.get(pr["a"])
            pb = prices.get(pr["b"])
            if pa is None or pb is None:
                continue                          # a venue went quiet — hold and wait
            # per-leg liquidation (isolated margin per venue)
            liq = None
            for leg in pr["legs"]:
                px = prices.get(leg["ex"])
                if px is not None and self._leg_pnl(leg, px) <= -leg["margin"] * 0.97:
                    liq = leg["ex"]
                    break
            mid = (pa + pb) / 2.0
            gap_bps = abs(pa - pb) / mid * 1e4
            if liq:
                self._close(pr, prices, f"liquidation ({liq} leg)")
            elif gap_bps <= CONVERGE_BPS:
                self._close(pr, prices, "converged")
            elif time.time() - pr["opened_at"] >= MAX_HOLD_SEC:
                self._close(pr, prices, "time_stop")

    def _close(self, pr, prices, reason):
        total = 0.0
        for leg in pr["legs"]:
            px = prices.get(leg["ex"]) or leg["entry"]
            pnl = max(self._leg_pnl(leg, px), -leg["margin"])   # isolated: can't lose > margin
            self.bal[leg["ex"]] = self.bal.get(leg["ex"], 0) + leg["margin"] + pnl
            total += pnl
        pa = prices.get(pr["a"]); pb = prices.get(pr["b"])
        exit_gap = (abs(pa - pb) / ((pa + pb) / 2) * 1e4) if (pa and pb) else 0.0
        rec = {"pnl": round(total, 4), "reason": reason, "a": pr["a"], "b": pr["b"],
               "entry_gap_bps": round(pr["entry_gap_bps"], 2), "exit_gap_bps": round(exit_gap, 2),
               "lev": self.lev, "opened_at": pr["opened_at"], "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self.positions = [p for p in self.positions if p["id"] != pr["id"]]
        self._note(f"CLOSE {pr['a']}/{pr['b']} · {reason} · P&L ${total:+.4f} "
                   f"(gap {rec['entry_gap_bps']:.1f}->{rec['exit_gap_bps']:.1f} bps)",
                   "win" if total >= 0 else "loss")
        self._save()

    # ---- controls + snapshot ---------------------------------------------
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_lev(self, lev):
        s = str(lev)
        if s not in LEVS:
            return {"ok": False, "error": f"unknown leverage '{lev}'"}
        if self.positions:
            return {"ok": False, "error": "close open pairs before changing leverage"}
        self.lev = int(s)
        self._note(f"leverage set to {self.lev}x")
        self._save()
        return {"ok": True, "lev": self.lev}

    def reset(self):
        self.enabled = True
        self.bal = {}
        self.positions = []
        self.history = []
        self.log = []
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        prices = self.prices
        equity = sum(self.bal.values())
        pairs = []
        for pr in self.positions:
            legs, combined = [], 0.0
            for leg in pr["legs"]:
                mark = prices.get(leg["ex"])
                pnl = self._leg_pnl(leg, mark) if mark else 0.0
                combined += pnl
                equity += leg["margin"] + pnl
                legs.append({"ex": leg["ex"], "side": leg["side"], "entry": round(leg["entry"], 1),
                             "qty": round(leg["qty"], 5), "margin": round(leg["margin"], 2),
                             "mark": round(mark, 1) if mark else None, "pnl": round(pnl, 4),
                             "pnl_pct": round(pnl / leg["margin"] * 100, 2) if leg["margin"] else 0})
            pa = prices.get(pr["a"]); pb = prices.get(pr["b"])
            gnow = (abs(pa - pb) / ((pa + pb) / 2) * 1e4) if (pa and pb) else None
            pairs.append({"id": pr["id"], "a": pr["a"], "b": pr["b"],
                          "entry_gap_bps": round(pr["entry_gap_bps"], 1),
                          "gap_bps_now": round(gnow, 1) if gnow is not None else None,
                          "combined_pnl": round(combined, 4), "legs": legs})
        rows = sorted(({"ex": ex, "px": round(p, 1)} for ex, p in prices.items()),
                      key=lambda r: r["px"])
        best_gap = 0.0
        if len(prices) >= 2:
            best_gap = (max(prices.values()) - min(prices.values())) / \
                       (sum(prices.values()) / len(prices)) * 1e4
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        start = START_PER_EX * max(1, len(self.bal))
        return {
            "enabled": self.enabled, "lev": self.lev, "levs": LEVS, "status": self.status,
            "prices": rows, "best_gap_bps": round(best_gap, 1),
            "entry_bps": ENTRY_BPS, "converge_bps": CONVERGE_BPS,
            "balance": round(equity, 2), "equity": round(equity, 2),
            "start_balance": round(start, 2), "total_pnl": round(equity - start, 2),
            "venues": len(self.bal), "trades": len(self.history), "wins": wins,
            "pairs": pairs, "history": self.history, "log": self.log[:25],
        }
