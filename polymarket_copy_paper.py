"""
polymarket_copy_paper.py — paper copy-trade bot for a Polymarket trader.

Mirrors every trade of @huskyvs (proxy 0xaf17116ae2b1476032785a67bd5b7c8c05905c20) into a
$100 paper account. Their ~$900k book is scaled down by (100 / their_portfolio_value) so the
paper account tracks their PERCENTAGE performance. On first run it snapshots their current
positions (a mini-replica), then polls the public Polymarket data-api for new trades and
mirrors BUY/SELL. Marks positions to market each poll; settles when a market resolves.

Public, read-only, no keys. All paper. Runs in a background daemon thread.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

import config

ADDR = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"   # @huskyvs proxy wallet
USERNAME = "huskyvs"
DATA_API = "https://data-api.polymarket.com"
START_BALANCE = 100.0
POLL_SEC = 30
STATE_PATH = os.path.join(config.DATA_DIR, "polymarket_copy_state.json")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class PolymarketCopyBot:
    def __init__(self):
        self.addr = ADDR
        self.username = USERNAME
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.scale = None
        self.positions = {}      # asset -> {shares, avg, cur, title, outcome, slug, last_seen}
        self.history = []        # mirrored closed/realized trades (newest first)
        self.opens = []          # log of mirrored opens (newest first)
        self.seen = set()        # transaction hashes already mirrored
        self.last_poll = 0
        self.running = False
        self.status = "starting"
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._load()

    # ---------------- persistence ----------------
    def _save(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"balance": self.balance, "scale": self.scale,
                           "positions": self.positions, "history": self.history[:500],
                           "opens": self.opens[:200], "seen": list(self.seen)[-3000:]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(STATE_PATH):
                d = json.load(open(STATE_PATH, encoding="utf-8"))
                self.balance = float(d.get("balance", START_BALANCE))
                self.scale = d.get("scale")
                self.positions = d.get("positions", {}) or {}
                self.history = d.get("history", []) or []
                self.opens = d.get("opens", []) or []
                self.seen = set(d.get("seen", []) or [])
        except Exception:
            pass

    # ---------------- control ----------------
    def start(self):
        if self.running:
            return {"ok": True, "running": True}
        self._stop.clear(); self.running = True
        self._thread = threading.Thread(target=self._loop, name="polymarket-copy", daemon=True)
        self._thread.start()
        return {"ok": True, "running": True}

    def stop(self):
        self.running = False; self._stop.set()
        return {"ok": True, "running": False}

    def reset(self):
        with self._lock:
            self.balance = START_BALANCE; self.scale = None
            self.positions = {}; self.history = []; self.opens = []; self.seen = set(); self.last_poll = 0
            self._save()
        return {"ok": True}

    # ---------------- their portfolio value (for scaling) ----------------
    def _their_value(self):
        try:
            v = _get(f"{DATA_API}/value?user={self.addr}")
            if isinstance(v, list) and v:
                return float(v[0].get("value") or v[0].get("amount") or 0) or None
            if isinstance(v, dict):
                return float(v.get("value") or v.get("amount") or 0) or None
        except Exception:
            pass
        return None

    def _their_positions(self):
        try:
            return _get(f"{DATA_API}/positions?user={self.addr}&sizeThreshold=0.1&limit=500")
        except Exception:
            return None

    # ---------------- first-run priming ----------------
    def _prime(self):
        """Start with $100 cash and copy trades FROM NOW. Sets the position-size scale
        (so each copied trade is the same % of my $100 as it is of their active book), and
        marks all current activity as already-seen so we don't retroactively copy history."""
        poss = self._their_positions()
        if poss is None:
            return False
        val = self._their_value()
        if not val:                                  # scale to their active book value
            val = sum(float(p.get("currentValue") or 0) for p in poss) or 1000.0
        self.scale = START_BALANCE / val
        try:
            acts = _get(f"{DATA_API}/activity?user={self.addr}&limit=100")
            for a in acts:
                if a.get("transactionHash"):
                    self.seen.add(a["transactionHash"])
        except Exception:
            return False
        self.opens.insert(0, {"t": time.time(),
                              "msg": f"started: $100 cash, now copying every NEW trade by @{self.username} "
                                     f"(scale {self.scale:.2e})"})
        self._save()
        return True

    # ---------------- mirror a single trade ----------------
    def _mirror(self, a):
        side = a.get("side"); asset = a.get("asset")
        price = float(a.get("price") or 0); their_size = float(a.get("size") or 0)
        if not asset or price <= 0 or their_size <= 0 or self.scale is None:
            return
        size = their_size * self.scale
        title = a.get("title", ""); outcome = a.get("outcome", "")
        if side == "BUY":
            cost = size * price
            if cost > self.balance:                  # cap to available cash
                size = self.balance / price; cost = size * price
            if size <= 0:
                return
            self.balance -= cost
            p = self.positions.get(asset)
            if p:
                tot = p["shares"] + size
                p["avg"] = (p["avg"] * p["shares"] + price * size) / tot if tot else price
                p["shares"] = tot; p["cur"] = price; p["last_seen"] = time.time()
            else:
                self.positions[asset] = {"shares": size, "avg": price, "cur": price,
                                         "title": title, "outcome": outcome,
                                         "slug": a.get("slug", ""), "last_seen": time.time()}
            self.opens.insert(0, {"t": a.get("timestamp", time.time()),
                                  "msg": f"BUY {outcome} @ {price:.3f} · ${cost:.2f} · {title[:60]}"})
        elif side == "SELL":
            p = self.positions.get(asset)
            if not p:
                return
            sell = min(p["shares"], size)
            proceeds = sell * price; self.balance += proceeds
            pnl = sell * (price - p["avg"])
            p["shares"] -= sell; p["cur"] = price
            self.history.insert(0, {"t": a.get("timestamp", time.time()), "outcome": outcome,
                                    "entry": round(p["avg"], 4), "exit": round(price, 4),
                                    "pnl": round(pnl, 4), "reason": "sell (copied)", "title": title[:70]})
            if p["shares"] <= 1e-9:
                self.positions.pop(asset, None)

    # ---------------- poll + mark ----------------
    def _tick(self):
        if self.scale is None:
            if not self._prime():
                self.status = "waiting for Polymarket data…"
            else:
                self.status = f"watching @{self.username} for new trades…"
            return
        # 1) mirror new trades (chronological)
        try:
            acts = _get(f"{DATA_API}/activity?user={self.addr}&limit=50")
        except Exception as e:
            self.status = f"poll error: {type(e).__name__}"; return
        new = [a for a in acts if a.get("type") == "TRADE" and a.get("transactionHash") not in self.seen]
        new.sort(key=lambda a: a.get("timestamp", 0))
        with self._lock:
            for a in new:
                self.seen.add(a["transactionHash"]); self._mirror(a)
            # 2) mark to market + settle resolved
            theirs = self._their_positions()
            curmap = {p["asset"]: float(p.get("curPrice") or 0) for p in (theirs or [])}
            for asset in list(self.positions.keys()):
                pos = self.positions[asset]
                if asset in curmap:
                    pos["cur"] = curmap[asset]; pos["last_seen"] = time.time()
                elif time.time() - pos.get("last_seen", 0) > 180:   # gone from their book -> settle at last mark
                    payout = pos["shares"] * pos["cur"]; self.balance += payout
                    self.history.insert(0, {"t": time.time(), "outcome": pos["outcome"],
                                            "entry": round(pos["avg"], 4), "exit": round(pos["cur"], 4),
                                            "pnl": round(pos["shares"] * (pos["cur"] - pos["avg"]), 4),
                                            "reason": "resolved/redeemed", "title": pos["title"][:70]})
                    self.positions.pop(asset, None)
            self.last_poll = time.time()
            eq = self.equity()
            self.status = f"copying @{self.username} · {len(self.positions)} positions · {len(new)} new"
            self._save()

    def _loop(self):
        time.sleep(2.0)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self.status = f"loop error: {type(e).__name__}: {str(e)[:60]}"
            self._stop.wait(POLL_SEC)

    # ---------------- snapshot ----------------
    def equity(self):
        return self.balance + sum(p["shares"] * p["cur"] for p in self.positions.values())

    def state(self):
        with self._lock:
            eq = self.equity()
            wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
            nt = len(self.history)
            poss = [{"title": p["title"][:70], "outcome": p["outcome"], "shares": round(p["shares"], 4),
                     "entry": round(p["avg"], 4), "cur": round(p["cur"], 4),
                     "value": round(p["shares"] * p["cur"], 2),
                     "upnl": round(p["shares"] * (p["cur"] - p["avg"]), 2)}
                    for p in sorted(self.positions.values(), key=lambda x: -x["shares"] * x["cur"])]
            return {
                "running": self.running, "username": self.username, "addr": self.addr,
                "status": self.status, "start_balance": self.start_balance,
                "balance": round(self.balance, 2), "equity": round(eq, 2),
                "pnl": round(eq - self.start_balance, 2),
                "pnl_pct": round((eq / self.start_balance - 1) * 100, 2),
                "trades": nt, "wins": wins,
                "win_rate": round(100 * wins / nt, 1) if nt else 0.0,
                "scale": self.scale, "positions": poss,
                "history": self.history[:200], "opens": self.opens[:60],
            }
