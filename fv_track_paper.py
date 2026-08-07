"""
================================================================================
 fv_track_paper.py  —  "FAIR-VALUE TRACKING"  (paper, Lighter follower vs leaders)
================================================================================
The idea (mine, from first principles):

  Short-horizon crypto signals are REAL but tiny — a few bps of edge per trade.
  On every fee-charging venue that edge dies on the cost cliff (a signal that's
  clearly profitable at 0 bps round-trip is gone by 2-5 bps). Lighter charges
  ZERO trading fees, so an edge that is negative-EV for everyone else survives
  here — and because only zero-cost players can touch it, the gap persists.

  Mechanism — fair-value tracking of a follower venue:
    - Binance/Bybit/OKX/Coinbase are the price-DISCOVERY venues (deep, fast).
      Their consensus is "fair value" (FV) — here the median of the fresh
      leader prints on the live tape.
    - Lighter is a FOLLOWER: it tracks FV with a small lag and occasional
      liquidity-gap dislocations.
    - When Lighter deviates from FV by >= ENTER_BPS, bet on CONVERGENCE:
      Lighter cheap vs FV  -> LONG Lighter;  Lighter rich vs FV -> SHORT.
      Whether FV led (info lag) or Lighter's thin book got swept (noise), the
      trade is identical: trade toward FV.
    - Close when the deviation collapses back to ~0 (converged, banked), or it
      keeps widening against us (we misread info as noise -> cut), or time-stop.

  Fees are ZERO here. The panel also shows what the same trades would have NET'd
  after a typical 4.5 bps round-trip taker fee — that number is usually negative,
  which is the whole point: the edge only exists because Lighter is free.

$100 of paper margin, one position at a time, marked on Lighter's live price.
All paper, real live prices.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import config

STATE_PATH = os.path.join(config.DATA_DIR, "fv_track_state.json")

START_BAL       = 100.0     # paper account (USDT)
ENTER_BPS       = 2.5       # open when |Lighter - FV| >= this many bps (1 bp = 0.01%)
EXIT_BPS        = 0.6       # close (converged / banked) when |deviation| shrinks to this
STOP_WIDEN_BPS  = 9.0       # cut when |deviation| grows this far BEYOND the entry deviation
MAX_HOLD_SEC    = 180       # safety: close a stuck position after this long
STALE_MS        = 8000      # ignore any price older than this (FV must be FRESH or we don't trade)
MIN_LEADERS     = 2         # need at least this many fresh leader venues to trust FV
POLL_SEC        = 1.5
FEE_BPS_REF     = 4.5       # reference round-trip taker fee on a NORMAL venue (for the what-if column)

LEADERS = ("binance", "bybit", "okx", "coinbase", "kraken")  # price-discovery venues for FV
LEVS = {"1": "1x (safe, tiny P&L)", "3": "3x", "5": "5x", "10": "10x (risky)"}
DEFAULT_LEV = 5


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


class FVTrackBot:
    def __init__(self, market=None, whales=None):
        self.market = market           # MarketData (Binance mark-price fallback for FV)
        self.whales = whales           # orderbook.WhaleTracker (live per-exchange tape)
        self.enabled = True
        self.lev = DEFAULT_LEV
        self.bal = START_BAL
        self.pos: dict | None = None   # the one open position (or None)
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.fee_would_pay = 0.0       # cumulative fee a normal venue would have charged
        self.fv = None                 # last computed fair value (leaders)
        self.lt = None                 # last Lighter price
        self.dev_bps = None            # last deviation (FV vs Lighter), signed bps
        self.n_leaders = 0
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
                           "pos": self.pos, "history": self.history,
                           "log": self.log[:60], "fee_would_pay": self.fee_would_pay}, f)
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
            self.bal = float(d.get("bal", self.bal))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.fee_would_pay = float(d.get("fee_would_pay", 0.0))
            print(f"[fv] restored: bal ${self.bal:.2f}, {len(self.history)} trades, "
                  f"{'1 open' if self.pos else 'flat'}")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[fv] {msg}")

    # ---- live prices: freshest print per venue off the whole tape ---------
    def _live_prices(self):
        out = {}
        if self.whales is not None:
            now_ms = time.time() * 1000
            scanned = 0
            wanted = set(LEADERS) | {"lighter"}
            for t in reversed(self.whales.tape):
                ex = t.get("ex")
                if ex in wanted and ex not in out:
                    if now_ms - t["ts"] <= STALE_MS:
                        out[ex] = t["price"]
                    else:
                        out[ex] = None        # seen but stale -> stop re-scanning this venue
                if all(e in out for e in wanted):
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

    def _fair_value(self, prices):
        """FV = median of fresh LEADER venue prints (robust to one venue glitching)."""
        leader_px = [prices[ex] for ex in LEADERS if ex in prices]
        return _median(leader_px), len(leader_px)

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
        fv, n_leaders = self._fair_value(prices)
        lt = prices.get("lighter")
        self.fv, self.lt, self.n_leaders = fv, lt, n_leaders

        # We must have a FRESH fair value AND a fresh Lighter price, or we sit out.
        if fv is None or lt is None or lt <= 0 or n_leaders < MIN_LEADERS:
            self.dev_bps = None
            if self.pos:                       # holding but feed went dark -> hold, mark stale
                self.status = "feed stale — holding open position, not marking"
            else:
                miss = "Lighter" if lt is None else f"{n_leaders} leader(s)"
                self.status = f"waiting for fresh feed ({miss})"
            return

        dev = (fv - lt) / lt * 1e4             # +ve => Lighter cheap vs FV
        self.dev_bps = dev

        if self.pos:
            self._manage(lt, fv, dev)
        elif self.enabled and abs(dev) >= ENTER_BPS:
            self._open(lt, fv, dev)

        self._set_status(dev)

    def _set_status(self, dev):
        if self.pos:
            self.status = (f"holding {self.pos['side'].upper()} · deviation "
                           f"{dev:+.1f} bps (entry {self.pos['entry_dev']:+.1f})")
        else:
            arm = "ARMED" if self.enabled else "paused"
            self.status = f"{arm} · flat · deviation {dev:+.1f} bps (enter at ±{ENTER_BPS})"

    def _open(self, lt, fv, dev):
        side = "long" if dev > 0 else "short"
        margin = self.bal
        qty = (margin * self.lev) / lt
        self.pos = {
            "id": uuid.uuid4().hex[:6], "side": side, "entry": lt, "entry_fv": fv,
            "entry_dev": dev, "qty": qty, "margin": margin, "opened_at": time.time(),
        }
        self.bal = 0.0
        self._note(f"OPEN  {side.upper()} Lighter @ {lt:,.1f}  ·  FV {fv:,.1f}  ·  "
                   f"deviation {dev:+.1f} bps · {self.lev}x · {qty:.4f} BTC", "open")
        self._save()

    def _pnl(self, px):
        p = self.pos
        if p["side"] == "long":
            return p["qty"] * (px - p["entry"])
        return p["qty"] * (p["entry"] - px)

    def _manage(self, lt, fv, dev):
        p = self.pos
        pnl = self._pnl(lt)
        held = time.time() - p["opened_at"]
        reason = None
        if pnl <= -p["margin"] * 0.97:                       # isolated liquidation
            reason = "liquidation"
        elif abs(dev) <= EXIT_BPS:                           # converged to FV -> bank it
            reason = "converged"
        elif abs(dev) >= abs(p["entry_dev"]) + STOP_WIDEN_BPS:  # diverged against us -> cut
            reason = "diverged_stop"
        elif held >= MAX_HOLD_SEC:
            reason = "time_stop"
        if reason:
            self._close(lt, fv, dev, reason)

    def _close(self, lt, fv, dev, reason):
        p = self.pos
        pnl = max(self._pnl(lt), -p["margin"])               # isolated: can't lose > margin
        notional = p["qty"] * p["entry"]
        fee_ref = notional * FEE_BPS_REF / 1e4               # what a NORMAL venue would've charged
        self.fee_would_pay += fee_ref
        self.bal = p["margin"] + pnl                         # fees are ZERO on Lighter
        rec = {"pnl": round(pnl, 4), "reason": reason, "side": p["side"],
               "entry": round(p["entry"], 1), "exit": round(lt, 1),
               "entry_dev": round(p["entry_dev"], 2), "exit_dev": round(dev, 2),
               "fee_ref": round(fee_ref, 4), "lev": self.lev,
               "opened_at": p["opened_at"], "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self.pos = None
        self._note(f"CLOSE {p['side'].upper()} · {reason} · P&L ${pnl:+.4f} "
                   f"(dev {rec['entry_dev']:+.1f}->{rec['exit_dev']:+.1f} bps · "
                   f"saved ${fee_ref:.4f} in fees)",
                   "win" if pnl >= 0 else "loss")
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
        if self.pos:
            return {"ok": False, "error": "close the open position before changing leverage"}
        self.lev = int(s)
        self._note(f"leverage set to {self.lev}x")
        self._save()
        return {"ok": True, "lev": self.lev}

    def reset(self):
        self.enabled = True
        self.bal = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self.fee_would_pay = 0.0
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        equity = self.bal
        pos = None
        if self.pos:
            p = self.pos
            mark = self.lt
            pnl = self._pnl(mark) if mark else 0.0
            equity += p["margin"] + pnl
            pos = {"id": p["id"], "side": p["side"], "entry": round(p["entry"], 1),
                   "entry_fv": round(p["entry_fv"], 1), "entry_dev": round(p["entry_dev"], 1),
                   "qty": round(p["qty"], 5), "margin": round(p["margin"], 2),
                   "mark": round(mark, 1) if mark else None, "pnl": round(pnl, 4),
                   "pnl_pct": round(pnl / p["margin"] * 100, 2) if p["margin"] else 0,
                   "dev_now": round(self.dev_bps, 1) if self.dev_bps is not None else None}
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        total_pnl = equity - START_BAL
        return {
            "enabled": self.enabled, "lev": self.lev, "levs": LEVS, "status": self.status,
            "fv": round(self.fv, 1) if self.fv else None,
            "lighter": round(self.lt, 1) if self.lt else None,
            "dev_bps": round(self.dev_bps, 1) if self.dev_bps is not None else None,
            "n_leaders": self.n_leaders, "enter_bps": ENTER_BPS, "exit_bps": EXIT_BPS,
            "balance": round(self.bal, 2), "equity": round(equity, 2),
            "start_balance": round(START_BAL, 2), "total_pnl": round(total_pnl, 2),
            "trades": len(self.history), "wins": wins,
            "fee_would_pay": round(self.fee_would_pay, 2),
            "net_with_fees": round(total_pnl - self.fee_would_pay, 2),
            "pos": pos, "history": self.history, "log": self.log[:25],
        }
