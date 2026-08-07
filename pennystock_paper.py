"""
pennystock_paper.py - AI penny-stock PAPER trading bot for the /paper page.

It screens US penny stocks, builds a factual dossier (liquidity, float, cash runway,
dilution, short interest, earnings date, news), asks the AI for a structured verdict,
and then TRADES its own decisions: entry, stop-loss, take-profit, trailing stop and a
time exit. Paper only - it never sends a real order.

HONEST EXECUTION MODEL (this is the part most penny-stock bots fake):
  * You BUY at the ASK and SELL at the BID. The spread is charged on every trade.
    Penny-stock spreads run 3-50%+, which is why most of these trades are unwinnable
    before the thesis even matters. The bot therefore REFUSES any name whose spread
    exceeds MAX_SPREAD_PCT, and reports spread cost separately in every trade record.
  * Position size is risk-based: RISK_PCT of equity divided by the stop distance,
    then capped so one name can never exceed MAX_POSITION_PCT of the account.
  * Hard gates run BEFORE the AI is consulted, so a persuasive story can never talk
    the bot into an illiquid or about-to-dilute name.

Nothing here predicts price. The AI supplies a thesis and risks; the risk engine
decides whether the trade is even takeable.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import pennystock_bot as research

NY = ZoneInfo("America/New_York")
NAME = "AI Penny Stock (paper)"
STATE_PATH = os.path.join(config.DATA_DIR, "pennystock_paper_state.json")

START_BALANCE = 100.0
RISK_PCT = 1.0             # % of equity risked per trade
MAX_PORTFOLIO_RISK_PCT = 5.0
MAX_POSITION_PCT = 12.5    # notional cap; risk cap below is the binding control
MAX_OPEN = 6               # leaderboard has 20; only independently confirmed setups trade
MAX_SPREAD_PCT = 4.0       # above this the round-trip cost is unwinnable -> skip
MIN_RUNWAY_Q = 2.0         # quarters of cash; below this a dilutive raise is imminent
MAX_DILUTION_PCT = 40.0    # share-count growth above this = your slice is shrinking
MAX_PER_SECTOR = 2         # concentration cap: 6 biotechs is one FDA headline, not a portfolio
DAILY_LOSS_LIMIT_PCT = 3.0

STOP_PCT = 12.0            # initial stop below entry
TP_PCT = 25.0              # take profit
TRAIL_ARM_PCT = 12.0       # once up this much, trail
TRAIL_PCT = 8.0            # trail distance from the high-water mark
MAX_HOLD_DAYS = 10

SCAN_EVERY_SEC = 30 * 60   # rescan for new candidates
MARK_EVERY_SEC = 60        # re-price open positions
TOP_N = 20                 # leaderboard size
SCREEN_POOL = 60           # how many raw candidates to score before ranking
AI_DEEP_DIVE = 10          # only the top setups get an AI call (free-tier token budget)
RETRY_EMPTY_SEC = 5 * 60    # if a scan finds nothing, try again in 5 min not 30
OUTCOME_UPDATE_SEC = 60 * 60
SIGNAL_HORIZONS = (1, 5, 10)
SIGNAL_ENGINE_VERSION = 3


@dataclass
class Position:
    id: str
    ticker: str
    name: str
    qty: int
    entry: float           # ask price actually paid
    mid_at_entry: float
    stop: float
    take_profit: float
    high_water: float
    opened_at: float
    opened_day: str
    thesis: str = ""
    catalyst: str = ""
    spread_cost: float = 0.0
    trailing: bool = False
    last_price: float = 0.0
    rank: int = 0
    action: str = ""
    sector: str = ""
    composite: float = 0.0
    spread_estimated: bool = False


class PennyStockPaperBot:
    NAME = NAME

    def __init__(self):
        self.enabled = False          # opt-in: needs AI credits to be useful
        self.balance = START_BALANCE
        self.pos: dict[str, Position] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.watchlist: list[dict] = []      # latest AI verdicts, incl. the rejects
        self.status = "idle"
        self.last_scan = 0.0
        self.last_error = ""
        self.day_key = ""
        self.day_pnl = 0.0
        self.scan_count = 0
        self.signal_log: list[dict] = []
        self.last_outcome_update = 0.0
        self._was_market_open = False
        self._scan_lock = asyncio.Lock()
        self._load()

    # ---------------- persistence ----------------
    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled, "balance": self.balance,
                    "positions": {k: asdict(v) for k, v in self.pos.items()},
                    "history": self.history[:200], "log": self.log[:120],
                    "watchlist": self.watchlist[:30],
                    "day_key": self.day_key, "day_pnl": self.day_pnl,
                    "scan_count": self.scan_count,
                    "signal_log": self.signal_log[:400],
                }, f)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH, encoding="utf-8") as f:      # was leaking the handle
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.pos = {k: Position(**v) for k, v in (d.get("positions") or {}).items()}
            self.history = d.get("history") or []
            self.log = d.get("log") or []
            self.watchlist = d.get("watchlist") or []
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.scan_count = int(d.get("scan_count", 0))
            # Version 1 observations include signals produced by the old rule that
            # could promote an AI WATCH to BUY. They are invalid training/evaluation
            # data and must not contaminate forward accuracy statistics.
            self.signal_log = [x for x in (d.get("signal_log") or [])
                               if x.get("engine_version") == SIGNAL_ENGINE_VERSION]
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:120]
        print(f"[penny] {msg}")

    # ---------------- market clock ----------------
    @staticmethod
    def market_open() -> bool:
        return research.us_market_open()

    # ---------------- risk gates (run BEFORE the AI) ----------------
    def hard_reject(self, d) -> str:
        """Returns a reason string if the trade is untakeable regardless of story."""
        return research.hard_risk_reason(d)

    def open_risk(self) -> float:
        return sum(p.qty * max(0.0, p.entry - p.stop) for p in self.pos.values())

    def size_position(self, price: float, stop: float) -> int:
        eq = self.equity()
        per_trade = eq * (RISK_PCT / 100.0)
        portfolio_cap = eq * (MAX_PORTFOLIO_RISK_PCT / 100.0)
        risk_dollars = min(per_trade, max(0.0, portfolio_cap - self.open_risk()))
        per_share_risk = max(price - stop, 0.01)
        qty = int(risk_dollars / per_share_risk)
        max_by_weight = int((eq * (MAX_POSITION_PCT / 100.0)) / max(price, 0.01))
        qty = min(qty, max_by_weight)
        affordable = int(self.balance / max(price, 0.01))
        return max(0, min(qty, affordable))

    # ---------------- trading ----------------
    def sector_full(self, sector: str) -> bool:
        """Six positions all in biotech is one FDA headline, not diversification."""
        if not sector:
            return False
        n = sum(1 for p in self.pos.values() if (p.sector or "") == sector)
        return n >= MAX_PER_SECTOR

    def _open(self, d, ai: dict, sig: dict | None = None, rank: int = 0):
        policy = research.edge_policy()
        policy_strategy = str(
            policy.get("strategy_id") or policy.get("selected_strategy") or ""
        )
        sig = sig or {}
        signal_strategy = str(sig.get("strategy_id") or "")
        if (
            not policy.get("auto_trade_allowed")
            or policy_strategy != research.LIVE_STRATEGY_ID
            or signal_strategy != research.LIVE_STRATEGY_ID
        ):
            self._note(
                f"skip {d.ticker}: edge audit is {policy.get('status', 'missing')} - "
                "this exact signal implementation is not authorized",
                "info",
            )
            return
        if self.sector_full(d.sector or ""):
            self._note(f"skip {d.ticker}: already hold {MAX_PER_SECTOR} in {d.sector}", "info")
            return
        reject = self.hard_reject(d)
        if reject:
            self._note(f"skip {d.ticker}: {reject}", "info")
            return
        eff, estimated = research.effective_spread(d)
        if eff > MAX_SPREAD_PCT:
            self._note(f"skip {d.ticker}: effective spread {eff:.1f}% exceeds the "
                       f"{MAX_SPREAD_PCT}% cap", "info")
            return
        if not d.spread_reliable:
            self._note(f"skip {d.ticker}: no fresh regular-session bid/ask; "
                       "the displayed cost is only an ADV proxy", "info")
            return
        signal_entry = float(sig.get("entry") or d.price)
        if signal_entry > 0 and abs(d.price / signal_entry - 1) > 0.05:
            self._note(f"skip {d.ticker}: price moved {abs(d.price/signal_entry-1)*100:.1f}% "
                       "since analysis; signal is stale", "info")
            return
        fresh_rank = research.rank_score(d)
        fresh_sig = research.signal_from(d, fresh_rank, ai)
        if fresh_sig["action"] not in ("BUY", "STRONG BUY"):
            self._note(f"skip {d.ticker}: fresh recheck is {fresh_sig['action']} "
                       f"({fresh_sig['why']})", "info")
            return
        ask = d.ask
        risk_pct = float(fresh_sig.get("risk_pct") or STOP_PCT)
        stop = ask * (1 - risk_pct / 100.0)
        stop = min(stop, ask * (1 - 0.02))                    # never above ~breakeven
        qty = self.size_position(ask, stop)
        if qty < 1:
            self._note(f"skip {d.ticker}: account/risk cap cannot fund one share", "info")
            return
        cost = qty * ask
        spread_cost = qty * (ask - d.price)
        self.balance -= cost
        p = Position(
            id=uuid.uuid4().hex[:6], ticker=d.ticker, name=d.name, qty=qty,
            entry=ask, mid_at_entry=d.price, stop=stop,
            take_profit=ask * (1 + float(fresh_sig.get("reward_pct") or TP_PCT) / 100.0), high_water=ask,
            opened_at=time.time(), opened_day=datetime.now(NY).strftime("%Y-%m-%d"),
            thesis=str(ai.get("why_this_verdict") or ai.get("bull_case") or "")[:200],
            catalyst=str(ai.get("catalyst_assessment") or (sig or {}).get("why") or "")[:120],
            rank=rank, action=str(fresh_sig.get("action") or ""),
            sector=d.sector or "",
            composite=float(fresh_rank.get("composite") or 0.0), spread_estimated=estimated,
            spread_cost=spread_cost, last_price=d.price,
        )
        self.pos[d.ticker] = p
        self._note(
            f"BUY {qty} {d.ticker} @ ${ask:.4f} (mid ${d.price:.4f}, spread cost ${spread_cost:.2f}) "
            f"stop ${stop:.4f} tp ${p.take_profit:.4f} | #{rank} {p.action}", "open")

    def _close(self, ticker: str, bid: float, mid: float, reason: str):
        p = self.pos.get(ticker)
        if not p:
            return
        proceeds = p.qty * bid
        self.balance += proceeds
        pnl = proceeds - (p.qty * p.entry)
        exit_spread = p.qty * (mid - bid)
        self.day_pnl += pnl
        self.history.insert(0, {
            "ticker": p.ticker, "name": p.name, "qty": p.qty,
            "entry": round(p.entry, 4), "exit": round(bid, 4),
            "pnl": round(pnl, 2), "pnl_pct": round((bid / p.entry - 1) * 100, 2),
            "reason": reason, "held_days": round((time.time() - p.opened_at) / 86400, 1),
            "spread_cost": round(p.spread_cost + exit_spread, 2),
            "thesis": p.thesis, "closed_at": time.time(),
        })
        self._note(f"SELL {p.qty} {p.ticker} @ ${bid:.4f} - {reason} - P&L ${pnl:+.2f} "
                   f"(spread cost ${p.spread_cost + exit_spread:.2f})",
                   "win" if pnl >= 0 else "loss")
        self.pos.pop(ticker, None)

    def _manage(self, p: Position, d):
        p.last_price = d.price
        bid, mid = d.bid, d.price
        if bid > p.high_water:
            p.high_water = bid
        if not p.trailing and bid >= p.entry * (1 + TRAIL_ARM_PCT / 100.0):
            p.trailing = True
            p.stop = max(p.stop, p.entry)                       # at worst, breakeven
            self._note(f"{p.ticker} +{TRAIL_ARM_PCT:.0f}% - trailing armed, stop to breakeven", "info")
        if p.trailing:
            p.stop = max(p.stop, p.high_water * (1 - TRAIL_PCT / 100.0))

        if bid <= p.stop:
            self._close(p.ticker, bid, mid, "trailing stop" if p.trailing else "stop loss")
            return
        if bid >= p.take_profit:
            self._close(p.ticker, bid, mid, "take profit")
            return
        if (time.time() - p.opened_at) / 86400 >= MAX_HOLD_DAYS:
            self._close(p.ticker, bid, mid, f"time exit ({MAX_HOLD_DAYS}d)")

    # ---------------- loops ----------------
    async def _mark_positions(self):
        for ticker in list(self.pos.keys()):
            try:
                d = await asyncio.to_thread(research.build_dossier, ticker)
                if d.error or d.price <= 0:
                    continue
                p = self.pos.get(ticker)
                if p:
                    p.last_price = d.price
                    # Never trigger stops or fabricate exits from stale/estimated
                    # off-hours quotes. Exit simulation requires a fresh bid.
                    if self.market_open() and d.spread_reliable:
                        self._manage(p, d)
            except Exception as e:
                self.last_error = f"mark {ticker}: {type(e).__name__}"
            await asyncio.sleep(0.3)

    async def _scan(self):
        """Serialize manual, startup, and scheduled scans within this process."""
        if self._scan_lock.locked():
            self._note("scan request ignored: a scan is already in progress", "info")
            return
        async with self._scan_lock:
            await self._scan_locked()

    async def _scan_locked(self):
        """Screen wide -> score everything -> rank -> AI-review the top -> signal -> trade.

        Only the top few get an AI call: the free tier has a daily token budget, and
        spending it on the 40th-ranked name is waste. Everything still gets a
        mechanical score, so the leaderboard is complete.
        """
        self.status = "screening the market..."
        try:
            syms = await asyncio.to_thread(research.screen, SCREEN_POOL)
        except Exception as e:
            self.last_error = f"screen: {type(e).__name__}: {str(e)[:90]}"
            self._note(self.last_error, "loss")
            self.last_scan = time.time() - SCAN_EVERY_SEC + RETRY_EMPTY_SEC
            return
        if not syms:
            self.last_error = "screener returned no candidates"
            self._note(self.last_error, "info")
            self.last_scan = time.time() - SCAN_EVERY_SEC + RETRY_EMPTY_SEC
            self.scan_count += 1
            return

        # ---- 1) mechanical scoring pass over the whole pool ----
        scored = []
        for i, sym in enumerate(syms):
            self.status = f"scoring {i+1}/{len(syms)}: {sym}"
            try:
                d = await asyncio.to_thread(research.build_dossier, sym)
                if d.error or d.price <= 0:
                    continue
                r = research.rank_score(d)
                scored.append((r["composite"], d, r))
            except Exception as e:
                self.last_error = f"score {sym}: {type(e).__name__}"
            await asyncio.sleep(0.15)
        if not scored:
            self.last_error = "no candidates could be scored"
            self.last_scan = time.time() - SCAN_EVERY_SEC + RETRY_EMPTY_SEC
            self.scan_count += 1
            return

        scored.sort(key=lambda x: -x[0])
        top = scored[:TOP_N]

        # ---- 2) AI deep-dive on the best few ----
        board = []
        reviews = 0
        for rank, (comp, d, r) in enumerate(top, start=1):
            ai, ai_err = None, ""
            rejected = self.hard_reject(d)
            if reviews < AI_DEEP_DIVE and not rejected and comp >= 38:
                self.status = f"AI reviewing #{rank} {d.ticker}..."
                try:
                    # Review the same snapshot that was ranked. Re-fetching here
                    # used to let the AI and mechanical engine see different facts.
                    res = await research.analyse_dossier(d)
                    ai, ai_err = res.get("ai"), res.get("ai_error", "")
                    if ai_err:
                        self.last_error = ai_err[:140]
                except Exception as e:
                    ai_err = f"{type(e).__name__}: {str(e)[:80]}"
                reviews += 1
                await asyncio.sleep(2.5)
            sig = research.signal_from(d, r, ai)
            spread, spread_estimated = research.effective_spread(d)
            board.append({
                "rank": rank, "ticker": d.ticker, "name": d.name,
                "price": round(d.price, 4), "change_pct": round(d.change_pct, 2),
                "spread_pct": round(spread, 2),
                "spread_quoted": round(d.spread_pct, 2),
                "spread_estimated": spread_estimated,
                "spread_unreliable": spread_estimated,
                "market_state": d.market_state,
                "volume_surge": round(d.volume_surge, 2),
                "composite": r["composite"], "hype": r["hype"],
                "technical": r["technical"], "catalyst": r["catalyst"],
                "quality": r["quality"], "tradeability": r["tradeability"],
                "hype_why": r["hype_why"], "quality_why": r["quality_why"],
                "technical_why": r["technical_why"], "catalyst_why": r["catalyst_why"],
                "trade_why": r["trade_why"], "data_completeness": d.data_completeness,
                "signal": sig, "ai": ai, "ai_error": ai_err,
                "rejected": rejected, "catalysts": d.catalysts[:5], "flags": d.flags[:6],
                "held": d.ticker in self.pos, "t": time.time(),
            })

        self.watchlist = board
        self.scan_count += 1
        self.last_scan = time.time()
        self._record_signals(board)

        # ---- 3) trade every actionable name we can ----
        if not self.market_open():
            n = sum(1 for b in board if b["signal"]["action"] in ("BUY", "STRONG BUY"))
            research_n = sum(1 for b in board if b["signal"]["action"] == "RESEARCH")
            if n:
                self._note(f"{n} setup(s) found after hours - full price/spread recheck required at the open", "info")
            elif research_n:
                self._note(
                    f"{research_n} unvalidated candidate(s) logged for forward measurement; no fills",
                    "info",
                )
            return
        if self.day_pnl <= -self.equity() * DAILY_LOSS_LIMIT_PCT / 100.0:
            self._note("daily loss limit hit - no new entries today", "loss")
            return
        for b in board:
            if len(self.pos) >= MAX_OPEN:
                break
            if b["ticker"] in self.pos:
                continue
            if b["signal"]["action"] not in ("BUY", "STRONG BUY"):
                continue
            try:
                d = await asyncio.to_thread(research.build_dossier, b["ticker"])
                if not d.error and d.price > 0:
                    self._open(d, b.get("ai") or {}, b["signal"], b["rank"])
            except Exception as e:
                self.last_error = f"open {b['ticker']}: {type(e).__name__}"
            await asyncio.sleep(0.3)

    def _record_signals(self, board):
        """Log every actionable signal with the price at the time, so we can later
        MEASURE whether these calls beat random instead of assuming they do."""
        now = time.time()
        signal_day = datetime.fromtimestamp(now, NY).strftime("%Y-%m-%d")
        for b in board:
            act = b["signal"]["action"]
            candidate_action = b["signal"].get("candidate_action", act)
            if candidate_action in ("BUY", "STRONG BUY"):
                # One observation per ticker/session. Repeated 30-minute scans are
                # correlated duplicates, not independent evidence of accuracy.
                if any(x.get("ticker") == b["ticker"] and x.get("signal_day") == signal_day
                       for x in self.signal_log):
                    continue
                self.signal_log.insert(0, {
                    "id": uuid.uuid4().hex[:10], "t": now, "signal_day": signal_day,
                    "engine_version": SIGNAL_ENGINE_VERSION,
                    "ticker": b["ticker"], "action": act,
                    "candidate_action": candidate_action,
                    "executed": act in ("BUY", "STRONG BUY"),
                    "rank": b["rank"], "price": b["price"],
                    "stop": b["signal"].get("stop"), "target1": b["signal"].get("target1"),
                    "target2": b["signal"].get("target2"),
                    "composite": b["composite"], "hype": b["hype"],
                    "technical": b.get("technical"), "catalyst": b.get("catalyst"),
                    "quality": b["quality"], "tradeability": b.get("tradeability"),
                    "outcomes": {}, "resolved": False,
                })
        self.signal_log = self.signal_log[:400]

    async def _update_signal_outcomes(self):
        """Attach causal 1/5/10-session returns to prior signals."""
        pending = [x for x in self.signal_log if not x.get("resolved") and x.get("signal_day")]
        if not pending or research.yf is None:
            self.last_outcome_update = time.time()
            return

        def fetch_history(ticker):
            return research.yf.Ticker(ticker).history(
                period="3mo", interval="1d", prepost=False, auto_adjust=False,
                actions=True, repair=True, timeout=10, raise_errors=True)

        by_ticker = {}
        for item in pending:
            by_ticker.setdefault(item["ticker"], []).append(item)
        for ticker, items in by_ticker.items():
            try:
                hist = await asyncio.to_thread(fetch_history, ticker)
                if hist is None or hist.empty:
                    continue
                hist = hist.dropna(subset=["Close"])
                dates = [x.date().isoformat() for x in hist.index]
                for item in items:
                    indices = [i for i, day in enumerate(dates) if day > item["signal_day"]]
                    outcomes = item.setdefault("outcomes", {})
                    entry = float(item.get("price") or 0)
                    if entry <= 0:
                        continue
                    for horizon in SIGNAL_HORIZONS:
                        key = str(horizon)
                        if key in outcomes or len(indices) < horizon:
                            continue
                        rows = hist.iloc[indices[:horizon]]
                        end_close = float(rows.iloc[-1]["Close"])
                        outcomes[key] = {
                            "return_pct": round((end_close / entry - 1) * 100, 2),
                            "max_gain_pct": round((float(rows["High"].max()) / entry - 1) * 100, 2),
                            "max_drawdown_pct": round((float(rows["Low"].min()) / entry - 1) * 100, 2),
                            "target1_hit": bool(item.get("target1") and float(rows["High"].max()) >= float(item["target1"])),
                            "stop_hit": bool(item.get("stop") and float(rows["Low"].min()) <= float(item["stop"])),
                        }
                    item["resolved"] = all(str(h) in outcomes for h in SIGNAL_HORIZONS)
            except Exception as e:
                self.last_error = f"outcome {ticker}: {type(e).__name__}"
            await asyncio.sleep(0.2)
        self.last_outcome_update = time.time()

    def signal_stats(self) -> dict:
        stats = {}
        for horizon in SIGNAL_HORIZONS:
            values = [x.get("outcomes", {}).get(str(horizon)) for x in self.signal_log]
            values = [x for x in values if isinstance(x, dict)]
            returns = [float(x.get("return_pct", 0)) for x in values]
            stats[str(horizon)] = {
                "count": len(values),
                "hit_rate": round(100 * sum(r > 0 for r in returns) / len(returns), 1) if returns else 0.0,
                "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else 0.0,
                "target1_rate": round(100 * sum(bool(x.get("target1_hit")) for x in values) / len(values), 1) if values else 0.0,
            }
        return stats

    async def manage_loop(self):
        await asyncio.sleep(12)
        while True:
            try:
                today = datetime.now(NY).strftime("%Y-%m-%d")
                if today != self.day_key:
                    self.day_key = today
                    self.day_pnl = 0.0
                is_open = self.market_open()
                if time.time() - self.last_outcome_update >= OUTCOME_UPDATE_SEC:
                    await self._update_signal_outcomes()
                if not self.enabled:
                    self.status = "paused"
                else:
                    # RESEARCH RUNS 24/7 - only order FILLS need the market open,
                    # because a fill at a stale closing price would be fiction.
                    if self.pos and is_open:
                        await self._mark_positions()
                    # An after-hours setup is never blindly carried into the open.
                    # Re-screen immediately on the closed->open transition.
                    just_opened = is_open and not self._was_market_open
                    if just_opened:
                        self._note("market opened - revalidating every setup with fresh quotes", "info")
                    if (just_opened or time.time() - self.last_scan > SCAN_EVERY_SEC) and len(self.pos) < MAX_OPEN:
                        await self._scan()
                    if self.pos:
                        self.status = f"holding {len(self.pos)}: " + ", ".join(self.pos)
                    elif is_open:
                        self.status = "scanning for candidates"
                    else:
                        n = sum(1 for w in self.watchlist
                                if (w.get("signal") or {}).get("action") in ("BUY", "STRONG BUY"))
                        self.status = (f"market closed - {n} setup(s) await an opening recheck" if n else
                                       "market closed - researching (no buy candidates yet)")
                self._was_market_open = is_open
                self._save()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "error"
            await asyncio.sleep(MARK_EVERY_SEC)

    # ---------------- api ----------------
    def equity(self) -> float:
        eq = self.balance
        for p in self.pos.values():
            eq += p.qty * (p.last_price or p.entry)
        return eq

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self._note("bot ENABLED - AI penny stock scanner live" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = START_BALANCE
        self.pos = {}
        self.history = []
        self.log = []
        self.watchlist = []
        self.day_pnl = 0.0
        self.scan_count = 0
        self.last_scan = 0.0
        self.signal_log = []
        self.last_outcome_update = 0.0
        self.last_error = ""          # stale provider errors must not linger
        self._note("bot reset to $100")
        self._save()
        return {"ok": True}

    async def scan_now(self):
        await self._scan()
        self._save()
        return {"ok": True, "found": len(self.watchlist)}

    def state(self) -> dict:
        eq = self.equity()
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        n = len(self.history)
        spread_paid = sum(h.get("spread_cost", 0) for h in self.history)
        positions = []
        for p in self.pos.values():
            mid = p.last_price or p.entry
            up = p.qty * (mid - p.entry)
            positions.append({
                "ticker": p.ticker, "name": p.name[:28], "qty": p.qty,
                "rank": p.rank, "action": p.action,
                "entry": round(p.entry, 4), "price": round(mid, 4),
                "stop": round(p.stop, 4), "tp": round(p.take_profit, 4),
                "pnl": round(up, 2), "pnl_pct": round((mid / p.entry - 1) * 100, 2),
                "trailing": p.trailing, "catalyst": p.catalyst,
                "held_days": round((time.time() - p.opened_at) / 86400, 1),
            })
        return {
            "running": True, "enabled": self.enabled, "name": self.NAME,
            "ai_model": (research.LAST_MODEL_USED or getattr(config, "PENNY_AI_MODEL", "?")),
            "status": self.status, "market_open": self.market_open(),
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BALANCE,
            "total_pnl": round(eq - START_BALANCE, 2),
            "total_pnl_pct": round((eq / START_BALANCE - 1) * 100, 2),
            "day_pnl": round(self.day_pnl, 2),
            "trades": n, "wins": wins,
            "win_rate": round(100 * wins / n, 1) if n else 0.0,
            "spread_paid": round(spread_paid, 2),
            "open_risk": round(self.open_risk(), 2),
            "max_portfolio_risk_pct": MAX_PORTFOLIO_RISK_PCT,
            "open_count": len(self.pos), "max_open": MAX_OPEN,
            "scan_count": self.scan_count,
            "last_scan": self.last_scan,
            "positions": positions,
            "watchlist": self.watchlist[:TOP_N],
            "signal_log": self.signal_log[:40],
            "signal_stats": self.signal_stats(),
            "top_n": TOP_N,
            "regime": research.market_regime(),
            "edge_policy": research.edge_policy(),
            "live_rule_evidence": research.live_rule_evidence(),
            "max_per_sector": MAX_PER_SECTOR,
            "history": self.history[:40],
            "log": self.log[:25],
            "last_error": self.last_error,
            "rules": (f"evidence gate must be VALIDATED before any auto-trade; "
                      f"risk {RISK_PCT}%/trade and {MAX_PORTFOLIO_RISK_PCT}% total, "
                      f"max {MAX_POSITION_PCT}% per name / {MAX_OPEN} open, spread cap {MAX_SPREAD_PCT}%, "
                      f"ATR-scaled stop with 2.5R target / trail {TRAIL_PCT}% after +{TRAIL_ARM_PCT}%, "
                      f"time exit {MAX_HOLD_DAYS}d"),
            "note": ("Paper fills require a fresh regular-session bid/ask: buy at ask, sell at bid. "
                     "After-hours cost values are ADV proxies for ranking only. Hard risk gates and "
                     "technical confirmation run before the AI; the AI may veto but never promote a "
                     "weak setup. Unvalidated candidates are never filled, but their accuracy is "
                     "measured at 1/5/10 later sessions."),
        }
