"""
================================================================================
 funding_bot.py  —  FUNDING-SETTLEMENT TIMING BOT  (PAPER, real Lighter data)
================================================================================
A brand-new strategy, separate from every other bot. It plays the price move
around Lighter's HOURLY funding settlement (top of each UTC hour):

  1. Scans all Lighter perps for markets with |funding| >= MIN_ABS_FUNDING (0.5%).
  2. Exactly ENTRY_LEAD_SEC (40s) before the settlement, it opens a position in
     EVERY qualifying market, in the DIRECTION OF THE FUNDING:
        funding POSITIVE -> LONG     funding NEGATIVE -> SHORT
  3. At settlement it BOOKS THE FUNDING IT PAYS (we sit on the paying side, so
     funding is always a cost — this is the "funding rate loss").
  4. AFTER settlement it holds each position and closes it the INSTANT its OVERALL
     P&L turns positive — i.e. once the price move has crossed the funding loss so
     the trade is net-profitable. Each market exits independently on its own profit.
  5. Every trade is scored: price P&L, funding paid, NET P&L, and % return — and
     written to data/funding_trades.csv + the live page.

PAPER MODE: it uses REAL Lighter funding rates and REAL mark prices, but the
fills are simulated — no real orders are placed. This validates the strategy and
the 40s/20s timing before any real money is risked. Settlement = top of each UTC
hour, computed from the clock (Lighter's API does not expose a settlement clock).
================================================================================
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import time

import config

STATE_PATH = os.path.join(config.DATA_DIR, "funding_state.json")
CSV_PATH = os.path.join(config.DATA_DIR, "funding_trades.csv")

# ---- tuning knobs ----------------------------------------------------------
START_BALANCE   = 1000.0     # paper starting capital
BET_USD         = 100.0      # notional per bet (per your spec)
ENTRY_LEAD_SEC  = 40         # open this many seconds BEFORE settlement
EXIT_CHECK_SEC  = 3          # after settlement, re-check prices this often for the profit exit
SETTLE_INTERVAL = 3600       # Lighter settles HOURLY -> top of each UTC hour
MIN_ABS_FUNDING = 0.005      # only trade markets with |funding| >= 0.5%  (<= -0.5% or >= +0.5%)
MAX_BETS_PER_CYCLE = 250     # safety cap
FUNDING_SCALE   = 1.0        # multiply the API funding rate to match what Lighter actually
                             #   charges per settlement (set <1 if the shown rate is multi-hour)
FEE_BPS         = 0.0        # round-trip taker fee in bps (Lighter ~0); applied to notional
MAX_TRADES_KEEP = 500
MAX_LOG_KEEP    = 60


def _next_settle(now: float) -> float:
    """Next top-of-hour in UTC (epoch seconds align to UTC, so multiples of 3600)."""
    return (math.floor(now / SETTLE_INTERVAL) + 1) * SETTLE_INTERVAL


def _mname(market: str) -> str:
    """Short display name, e.g. 'H100/USDC:USDC' -> 'H100'."""
    return market.replace("/USDC:USDC", "").replace("/USDC", "")


class FundingBot:
    def __init__(self):
        self.enabled = False
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.positions: list[dict] = []  # ALL open paper positions this cycle (one per market)
        self.trades: list[dict] = []    # closed trades, newest first
        self.log: list[dict] = []       # event feed, newest first
        self.leaderboard: list[dict] = []   # current top markets by |funding| (display)
        self.px_map: dict[str, float] = {}  # market -> latest mark price (to value open positions)
        self.wins = 0
        self.losses = 0
        self.last_refresh = 0.0
        self._last_exit_check = 0.0     # throttle the post-settlement profit-exit price polling
        self._cycle = None              # settlement epoch we've already acted on
        self._ex = None                 # ccxt.lighter (lazy, public/unsigned)
        self.running = False
        self._load()

    # ---- logging -----------------------------------------------------------
    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:MAX_LOG_KEEP]
        try:                                    # console may be cp1252; never let a print crash the loop
            print(f"[funding] {msg}")
        except Exception:
            pass

    # ---- Lighter data (sync ccxt; called via asyncio.to_thread) ------------
    def _client(self):
        if self._ex is None:
            import ccxt
            self._ex = ccxt.lighter({"enableRateLimit": True, "timeout": 20000})
            self._ex.load_markets()
        return self._ex

    def _fetch_funding(self) -> list[dict]:
        ex = self._client()
        fr = ex.fetch_funding_rates()
        out = []
        for sym, d in fr.items():
            r = d.get("fundingRate")
            if r is None:
                continue
            mk = d.get("markPrice") or d.get("indexPrice") or d.get("last")
            out.append({"market": sym, "rate": float(r),
                        "px": float(mk) if mk else None})
        out.sort(key=lambda x: abs(x["rate"]), reverse=True)
        return out

    def _mark_price(self, market: str) -> float | None:
        ex = self._client()
        try:
            t = ex.fetch_ticker(market)
            px = t.get("last") or t.get("close") or t.get("markPrice")
            if not px:
                info = t.get("info") or {}
                px = info.get("mark_price") or info.get("last_trade_price")
            return float(px) if px else None
        except Exception:
            return None

    def _fetch_prices(self) -> dict:
        """One bulk call for every market's price (the funding feed has no price)."""
        ex = self._client()
        out = {}
        try:
            ts = ex.fetch_tickers()
            for sym, d in ts.items():
                px = d.get("last") or d.get("close") or d.get("markPrice")
                if not px:
                    info = d.get("info") or {}
                    px = info.get("mark_price") or info.get("last_trade_price")
                if px:
                    out[sym] = float(px)
        except Exception:
            pass
        return out

    # ---- the timing loop ---------------------------------------------------
    async def run(self):
        self.running = True
        self._note("funding bot started (paper) — scanning Lighter funding rates")
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:120]}", "error")
            await asyncio.sleep(0.5)

    async def _tick(self):
        now = time.time()
        # refresh the funding leaderboard + price map for the page every ~25s
        if now - self.last_refresh > 25:
            self.last_refresh = now
            lb = await asyncio.to_thread(self._fetch_funding)
            if lb:
                self.leaderboard = lb[:12]   # (funding feed has no price; px_map is set at entry)

        if not self.enabled:
            return

        settle = _next_settle(now)
        t_to_settle = settle - now

        # ENTRY — 40s before settlement, once per cycle, only when flat
        if not self.positions and self._cycle != settle and 0 < t_to_settle <= ENTRY_LEAD_SEC:
            await self._enter(settle)

        if self.positions:
            settle_at = self.positions[0]["settle_at"]
            if now >= settle_at:                       # at settlement: pay funding, then...
                self._book_funding_all()
                # ...close each position the MOMENT it is net-profitable (price beats funding loss)
                if now - self._last_exit_check >= EXIT_CHECK_SEC:
                    self._last_exit_check = now
                    await self._check_profit_exits()

    async def _enter(self, settle: float):
        self._cycle = settle            # mark cycle handled even if we skip (no retry spam)
        lb = await asyncio.to_thread(self._fetch_funding)
        if lb:
            self.leaderboard = lb[:12]
            self.px_map = {r["market"]: r["px"] for r in lb if r["px"]}
        # bet EVERY market whose |funding| clears the threshold (capped for safety)
        candidates = [r for r in lb if abs(r["rate"]) >= MIN_ABS_FUNDING]
        if not candidates:
            self._note("no funding markets returned by Lighter → skip this settlement", "skip")
            return
        if len(candidates) > MAX_BETS_PER_CYCLE:
            self._note(f"{len(candidates)} markets qualified — capping at {MAX_BETS_PER_CYCLE}", "info")
            candidates = candidates[:MAX_BETS_PER_CYCLE]
        prices = await asyncio.to_thread(self._fetch_prices)   # bulk price lookup (funding feed has none)
        if prices:
            self.px_map.update(prices)
        opened = 0
        for p in candidates:
            px = p.get("px") or prices.get(p["market"])
            if not px or px <= 0:                              # per-market fallback
                px = await asyncio.to_thread(self._mark_price, p["market"])
            if not px or px <= 0:
                self._note(f"no price for {_mname(p['market'])} → skipped this one", "skip")
                continue
            side = "long" if p["rate"] > 0 else "short"
            self.positions.append({
                "market": p["market"], "side": side, "rate": p["rate"],
                "entry_px": px, "qty": BET_USD / px, "notional": BET_USD,
                "opened_at": time.time(), "settle_at": settle,
                "funding_paid": 0.0, "funding_booked": False})
            self.px_map[p["market"]] = px
            opened += 1
            self._note(f"OPEN {side.upper()} {_mname(p['market'])} @ {px:.6g} | "
                       f"funding {p['rate']*100:+.4f}% | ${BET_USD:.0f}", "open")
        if opened == 0:
            self._note("candidates found but no prices available → nothing entered", "skip")
        else:
            self._note(f"entered {opened} market(s) this settlement — total ${opened*BET_USD:.0f} notional", "info")
        self._save()

    def _book_funding_all(self):
        booked = 0
        for pos in self.positions:
            if pos.get("funding_booked"):
                continue
            loss = abs(pos["rate"]) * pos["notional"] * FUNDING_SCALE   # we're on the paying side
            pos["funding_paid"] = loss
            pos["funding_booked"] = True
            self.balance -= loss
            booked += 1
            self._note(f"SETTLEMENT {_mname(pos['market'])}: funding {pos['rate']*100:+.4f}% → paid -${loss:.4f}",
                       "funding")
        if booked:
            self._save()

    def _net_pnl(self, pos, px):
        """Overall P&L including the funding paid: price move minus funding loss minus fees."""
        if pos["side"] == "long":
            price_pnl = pos["qty"] * (px - pos["entry_px"])
        else:
            price_pnl = pos["qty"] * (pos["entry_px"] - px)
        funding_loss = pos.get("funding_paid", 0.0)
        fee = pos["notional"] * (FEE_BPS / 10000.0) * 2.0
        return price_pnl, funding_loss, fee, (price_pnl - funding_loss - fee)

    async def _check_profit_exits(self):
        """Post-settlement: close each position the instant its OVERALL P&L turns positive
        (i.e. the price move has crossed the funding loss). Positions that aren't yet
        profitable are held and re-checked. Each market closes independently."""
        prices = await asyncio.to_thread(self._fetch_prices)
        if prices:
            self.px_map.update(prices)
        closed = 0
        for pos in list(self.positions):
            if not pos.get("funding_booked"):
                continue                                  # only after funding is actually paid
            px = prices.get(pos["market"]) or self.px_map.get(pos["market"]) or pos["entry_px"]
            price_pnl, funding_loss, fee, net = self._net_pnl(pos, px)
            if net > 0:                                   # profitable overall -> close NOW
                self._close_position(pos, px, price_pnl, funding_loss, fee, net)
                closed += 1
        if closed:
            self._save()

    def _close_position(self, pos, px, price_pnl, funding_loss, fee, net):
        self.balance += price_pnl - fee                  # funding was already subtracted at settlement
        pct = net / pos["notional"] * 100.0
        rec = {"market": pos["market"], "side": pos["side"], "rate": pos["rate"],
               "funding_loss": round(funding_loss, 5), "entry_px": pos["entry_px"],
               "exit_px": px, "price_pnl": round(price_pnl, 5), "fee": round(fee, 5),
               "net_pnl": round(net, 5), "pct": round(pct, 4),
               "opened_at": pos["opened_at"], "settle_at": pos["settle_at"],
               "closed_at": time.time()}
        self.trades.insert(0, rec)
        self.trades = self.trades[:MAX_TRADES_KEEP]
        if net >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self._append_csv(rec)
        sgn = "+" if net >= 0 else ""
        held = int(time.time() - pos["settle_at"])
        self._note(f"CLOSE {_mname(pos['market'])} @ {px:.6g} (profit, +{held}s post-settle) | "
                   f"price ${price_pnl:+.4f} − funding ${funding_loss:.4f} = NET {sgn}${net:.4f} ({sgn}{pct:.2f}%)",
                   "win" if net >= 0 else "loss")
        self.positions.remove(pos)

    # ---- persistence -------------------------------------------------------
    def _append_csv(self, rec):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            new = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "market", "side", "funding_rate_pct", "funding_loss_usd",
                                "entry_px", "exit_px", "price_pnl_usd", "net_pnl_usd", "pct"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["market"], rec["side"], round(rec["rate"] * 100, 5),
                            rec["funding_loss"], rec["entry_px"], rec["exit_px"],
                            rec["price_pnl"], rec["net_pnl"], rec["pct"]])
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance,
                           "start_balance": self.start_balance, "wins": self.wins,
                           "losses": self.losses, "trades": self.trades[:MAX_TRADES_KEEP],
                           "log": self.log[:MAX_LOG_KEEP], "positions": self.positions}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.start_balance = float(d.get("start_balance", START_BALANCE))
            self.wins = int(d.get("wins", 0))
            self.losses = int(d.get("losses", 0))
            self.trades = d.get("trades", []) or []
            self.log = d.get("log", []) or []
            self.positions = d.get("positions", []) or []   # resume in-flight positions if any
        except Exception:
            pass

    # ---- controls + snapshot ----------------------------------------------
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = self.start_balance = START_BALANCE
        self.positions = []
        self.trades = []
        self.wins = self.losses = 0
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        now = time.time()
        n = self.wins + self.losses
        total_funding = sum(t.get("funding_loss", 0.0) for t in self.trades)
        opens = []
        for pos in self.positions:
            px = self.px_map.get(pos["market"]) or pos["entry_px"]
            price_pnl, funding_loss, fee, net = self._net_pnl(pos, px)
            opens.append({**pos, "mark_px": px, "upnl": price_pnl, "net": net})
        open_net = sum(o["net"] for o in opens)
        return {
            "enabled": self.enabled, "balance": self.balance,
            "net_pnl": self.balance - self.start_balance,
            "net_pct": (self.balance - self.start_balance) / self.start_balance * 100.0,
            "wins": self.wins, "losses": self.losses, "trades_n": n,
            "win_rate": (self.wins / n * 100.0) if n else None,
            "total_funding_paid": total_funding,
            "now": now, "next_settle": _next_settle(now),
            "entry_lead": ENTRY_LEAD_SEC, "exit_mode": "profit", "bet_usd": BET_USD,
            "min_funding_pct": MIN_ABS_FUNDING * 100.0,
            "opens": opens, "open_net": open_net, "leaderboard": self.leaderboard[:10],
            "trades": self.trades[:80], "log": self.log[:40],
        }
