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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import pennystock_bot as research
from research import penny_feasibility as feasibility

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
# Exit research can propose a change, but only an exact, point-in-time live-rule audit may
# change the default. The broad 8-K result did not replicate on the stricter reaction
# proxy, so the fixed target remains on. Researchers can still run the paper-only
# experiment explicitly with PENNY_FIXED_TARGET=0.
USE_FIXED_TARGET = os.getenv("PENNY_FIXED_TARGET", "1") == "1"
USE_TRAILING_STOP = os.getenv("PENNY_TRAILING_STOP", "1") == "1"
TRAIL_ARM_PCT = 12.0       # once up this much, trail
TRAIL_PCT = 8.0            # trail distance from the high-water mark
MAX_HOLD_DAYS = 10

# The scanner has two gears.  A cheap pulse watches movers repeatedly while a full
# breadth pass refreshes the whole board.  Research runs even when new paper entries
# are paused; otherwise the forward sample is selection-biased and opportunities are
# missed whenever the portfolio is full.
REGULAR_SCAN_SEC = 2 * 60
HOT_SCAN_SEC = 60
EXTENDED_SCAN_SEC = 5 * 60
CLOSED_SCAN_SEC = 30 * 60
FULL_SCAN_SEC = 15 * 60
MARK_EVERY_SEC = 20         # protective exits must not wait for a long market scan
LOOP_TICK_SEC = 5
TOP_N = 20                 # leaderboard size
SCREEN_POOL = 60           # how many raw candidates to score before ranking
PULSE_SCREEN_POOL = 24
PULSE_SCORE_LIMIT = 32
DOSSIER_CONCURRENCY = 4    # bounded: faster than serial without a provider stampede
AI_DEEP_DIVE = 10          # only the top setups get an AI call (free-tier token budget)
RETRY_EMPTY_SEC = 5 * 60
MAX_PROVIDER_BACKOFF_SEC = 30 * 60
AI_CACHE_SEC = 45 * 60
AI_ERROR_CACHE_SEC = 5 * 60
AI_MATERIAL_PRICE_PCT = 4.0
AI_MATERIAL_SCORE_POINTS = 7.0
CONFIRM_SCANS = 2
CONFIRM_MIN_SEC = 45
CONFIRM_MAX_GAP_SEC = 20 * 60
CONFIRM_MAX_CHASE_PCT = 5.0
SETUP_TTL_SEC = 2 * 60 * 60
OUTCOME_UPDATE_SEC = 60 * 60
SIGNAL_HORIZONS = (1, 5, 10)
# One name for the session key, used by both the writer and every reader. The first
# evidence-clock attempt read "day" while signals were written as "signal_day", so the
# count silently stayed at zero - and the test passed because its fixture invented the
# same wrong key. A shared constant makes that class of drift impossible rather than
# something a future test has to remember to catch.
SIGNAL_DAY_FIELD = "signal_day"
# Retention is measured in sessions, not rows. A 400-row cap silently truncated the
# evidence below the 60-day gate it was supposed to feed, and cut sessions in half.
SIGNAL_RETENTION_SESSIONS = 180
# Days whose horizon has not elapsed are legitimately unmeasurable. Days that matured and
# still have no outcome are informative: a name that stops resolving has usually halted or
# delisted. The two must never be pooled.
HORIZON_GRACE_DAYS = 3
# What a permanently missing outcome is assumed to be worth when bounding the result. A
# halted microcap can go to zero, so the bound uses the true worst case rather than a
# comfortable one.
MISSING_OUTCOME_ASSUMPTION_PCT = -100.0
# Append-only evidence. The hot log is capped for memory; this is not, so retention can
# never quietly change a statistic.
SIGNAL_ARCHIVE_PATH = os.path.join(config.DATA_DIR, "pennystock_signal_archive.jsonl")
SIGNAL_ENGINE_VERSION = 6


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
        self.last_scan_started = 0.0
        self.last_error = ""
        self.day_key = ""
        self.day_pnl = 0.0
        self.scan_count = 0
        self.signal_log: list[dict] = []
        self._forward_feasibility_cache: tuple[object, dict] = (None, {})
        self.last_outcome_update = 0.0
        self.last_full_scan = 0.0
        self.last_scan_duration = 0.0
        self.last_scan_mode = "none"
        self.scan_failures = 0
        self.provider_backoff_until = 0.0
        self.setup_states: dict[str, dict] = {}
        self._ai_cache: dict[str, dict] = {}
        self._scan_task: asyncio.Task | None = None
        self._last_mark = 0.0
        self.engine_version_started_at = time.time()
        self.persistence_error = ""
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
                    "signal_log": self._retained_signals(),
                    "last_full_scan": self.last_full_scan,
                    "setup_states": self.setup_states,
                    "engine_version": SIGNAL_ENGINE_VERSION,
                    "engine_version_started_at": self.engine_version_started_at,
                }, f)
            os.replace(tmp, STATE_PATH)
            self.persistence_error = ""
        except Exception as e:
            # A silent failure here loses the forward evidence while the service still
            # reports "no errors". Surface it instead.
            self.persistence_error = f"state save failed: {type(e).__name__}: {e}"[:160]

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH, encoding="utf-8") as f:      # was leaking the handle
                d = json.load(f)
            # Discarding prior-version outcomes below is correct - old-rule signals would
            # contaminate forward accuracy. But it has an unpriced cost: every strategy
            # bump restarts the evidence clock at zero. Record when the current version
            # began collecting so that cost is visible instead of silent.
            if int(d.get("engine_version", -1)) == SIGNAL_ENGINE_VERSION:
                self.engine_version_started_at = float(
                    d.get("engine_version_started_at") or time.time())
            else:
                self.engine_version_started_at = time.time()
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.pos = {k: Position(**v) for k, v in (d.get("positions") or {}).items()}
            self.history = d.get("history") or []
            self.log = d.get("log") or []
            # A board row is a decision artifact, not generic cache data.  After a
            # strategy implementation change, showing old rows under the new header
            # misrepresents what the running rule decided.  Keep only exact-ID rows;
            # the next scheduled scan repopulates the board.
            self.watchlist = [
                row for row in (d.get("watchlist") or [])
                if isinstance(row, dict)
                and isinstance(row.get("signal"), dict)
                and row["signal"].get("strategy_id") == research.LIVE_STRATEGY_ID
            ]
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.scan_count = int(d.get("scan_count", 0))
            self.last_full_scan = float(d.get("last_full_scan", 0.0))
            self.setup_states = {
                str(k): v for k, v in (d.get("setup_states") or {}).items()
                if isinstance(v, dict)
                and int(v.get("engine_version", 0)) == SIGNAL_ENGINE_VERSION
            }
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

    @staticmethod
    def session_phase(is_open: bool, now: datetime | None = None) -> str:
        """Regular/extended/closed clock used only for research cadence.

        The provider's holiday-aware market status remains authoritative for fills.
        Clock-derived pre/post labels may cause an extra research scan on a holiday,
        but can never authorize an order.
        """
        if is_open:
            return "regular"
        n = (now or datetime.now(NY)).astimezone(NY)
        minute = n.hour * 60 + n.minute
        if n.weekday() < 5 and 4 * 60 <= minute < 9 * 60 + 30:
            return "premarket"
        if n.weekday() < 5 and 16 * 60 <= minute < 20 * 60:
            return "afterhours"
        return "closed"

    def hot_setup_count(self, now: float | None = None) -> int:
        """Unconfirmed candidates get the fastest follow-up scan."""
        now = time.time() if now is None else now
        return sum(
            1 for st in self.setup_states.values()
            if st.get("candidate") and not st.get("confirmed")
            and now - float(st.get("last_seen", 0)) <= CONFIRM_MAX_GAP_SEC
        )

    def scan_interval(self, is_open: bool, now: float | None = None) -> int:
        phase = self.session_phase(is_open)
        if phase == "regular":
            return HOT_SCAN_SEC if self.hot_setup_count(now) else REGULAR_SCAN_SEC
        if phase in ("premarket", "afterhours"):
            return EXTENDED_SCAN_SEC
        return CLOSED_SCAN_SEC

    def scan_plan(self, is_open: bool, now: float | None = None,
                  force: bool = False) -> str | None:
        """Return ``full``/``pulse`` when due, independent of entry capacity."""
        now = time.time() if now is None else now
        if self._scan_lock.locked() or now < self.provider_backoff_until:
            return None
        anchor = self.last_scan_started or self.last_scan
        if force or anchor <= 0:
            return "full"
        if now - anchor < self.scan_interval(is_open, now):
            return None
        if self.last_full_scan <= 0 or now - self.last_full_scan >= FULL_SCAN_SEC:
            return "full"
        return "pulse"

    @staticmethod
    def _catalyst_key(d) -> str:
        news = "|".join(
            f"{x.get('when','')}:{x.get('publisher','')}:{x.get('title','')}"
            for x in (d.news or [])[:5]
        )
        filings = "|".join(
            f"{x.get('accepted_at') or x.get('date','')}:{x.get('type','')}:"
            f"{x.get('accessionNumber','')}:{','.join(str(v) for v in (x.get('items') or []))}"
            for x in (d.recent_filings or [])[:5]
        )
        adverse = ",".join(str(x) for x in (d.adverse_8k_items or []))
        return (f"{news}::{filings}::{d.earnings_date}::"
                f"{int(d.recent_offering)}::{adverse}")

    def _cached_ai(self, d, score: float) -> tuple[dict | None, str, bool, str]:
        """Reuse an analyst verdict only while its facts and price remain immaterially changed."""
        item = self._ai_cache.get(d.ticker)
        if not item:
            return None, "", False, ""
        ttl = AI_ERROR_CACHE_SEC if item.get("error") else AI_CACHE_SEC
        age = time.time() - float(item.get("t", 0))
        old_price = float(item.get("price", 0))
        price_move = abs(d.price / old_price - 1) * 100 if old_price > 0 else 999.0
        score_move = abs(float(score) - float(item.get("score", 0)))
        if (age > ttl or price_move >= AI_MATERIAL_PRICE_PCT
                or score_move >= AI_MATERIAL_SCORE_POINTS
                or item.get("catalyst_key") != self._catalyst_key(d)):
            return None, "", False, ""
        return (item.get("ai"), str(item.get("error") or ""), True,
                str(item.get("model") or ""))

    def _store_ai(self, d, score: float, ai: dict | None, error: str, model: str = ""):
        self._ai_cache[d.ticker] = {
            "t": time.time(), "price": d.price, "score": float(score),
            "catalyst_key": self._catalyst_key(d), "ai": ai,
            "error": str(error or "")[:160],
            "model": str(model or ""),
        }
        # The cache is operational, not evidence. Bound it independently of the board.
        if len(self._ai_cache) > 100:
            oldest = sorted(self._ai_cache, key=lambda k: self._ai_cache[k].get("t", 0))
            for key in oldest[:-100]:
                self._ai_cache.pop(key, None)

    def _update_setup_states(self, board: list[dict], now: float | None = None):
        """Require persistence across separated observations before entry/logging.

        One noisy quote can create a pretty score.  Two observations at least 45s
        apart, with the same catalyst and without chasing >5%, are a stronger event.
        This is a debounce/risk control, not a claim of statistical edge.
        """
        now = time.time() if now is None else now
        for b in board:
            ticker = b["ticker"]
            sig = b.get("signal") or {}
            candidate = sig.get("candidate_action") in ("BUY", "STRONG BUY")
            executable_observation = bool(
                b.get("quote_reliable")
                and str(b.get("market_state") or "").upper() == "REGULAR"
                and not b.get("spread_estimated")
            )
            prior = self.setup_states.get(ticker) or {}
            price = float(b.get("price") or 0)
            catalyst_key = str(b.get("catalyst_key") or "")
            hits = 0
            first_seen = now
            first_price = price
            reset_reason = ""

            if candidate and executable_observation:
                gap = now - float(prior.get("last_seen", 0))
                same_thesis = catalyst_key == str(prior.get("catalyst_key") or "")
                old_first = float(prior.get("first_price", 0))
                chase = ((price / old_first - 1) * 100
                         if old_first > 0 and price > 0 else 0.0)
                can_continue = (
                    prior.get("candidate") and same_thesis
                    and 0 <= gap <= CONFIRM_MAX_GAP_SEC
                    and chase <= CONFIRM_MAX_CHASE_PCT
                )
                if can_continue:
                    hits = int(prior.get("hits", 1))
                    first_seen = float(prior.get("first_seen", now))
                    first_price = old_first or price
                    if gap >= CONFIRM_MIN_SEC:
                        hits += 1
                else:
                    hits = 1
                    if prior.get("candidate") and not same_thesis:
                        reset_reason = "catalyst changed"
                    elif prior.get("candidate") and chase > CONFIRM_MAX_CHASE_PCT:
                        reset_reason = f"price chased {chase:.1f}% since detection"

            elif candidate:
                reset_reason = "waiting for fresh regular-session executable quote"

            confirmed = bool(candidate and executable_observation and hits >= CONFIRM_SCANS)
            state = {
                "engine_version": SIGNAL_ENGINE_VERSION,
                "candidate": candidate, "confirmed": confirmed, "hits": hits,
                "first_seen": first_seen, "last_seen": now,
                "first_price": first_price, "last_price": price,
                "catalyst_key": catalyst_key,
                "candidate_action": sig.get("candidate_action"),
                "executable_observation": executable_observation,
                "reset_reason": reset_reason,
            }
            self.setup_states[ticker] = state
            confirmation = {
                "state": ("confirmed" if confirmed else "detected" if candidate
                          else "not_eligible"),
                "confirmed": confirmed, "observations": hits,
                "required": CONFIRM_SCANS, "first_seen": first_seen,
                "executable_observation": executable_observation,
                "reset_reason": reset_reason,
            }
            b["confirmation"] = confirmation
            sig["confirmed"] = confirmed
            sig["confirmation_observations"] = hits
            if candidate and not confirmed:
                if not executable_observation:
                    sig["why"] = (
                        "research candidate only; waiting for a fresh, plausible "
                        "regular-session bid/ask before confirmation"
                    )
                else:
                    sig["why"] = (
                        f"setup detected {hits}/{CONFIRM_SCANS}; waiting for a separated "
                        "confirmation scan before any entry"
                    )
                # A future validated policy may otherwise expose a BUY before the
                # persistence gate. RESEARCH already means track-only and stays so.
                if sig.get("action") in ("BUY", "STRONG BUY"):
                    sig["action"] = "WATCH"

        cutoff = now - SETUP_TTL_SEC
        for ticker in list(self.setup_states):
            if float(self.setup_states[ticker].get("last_seen", 0)) < cutoff:
                self.setup_states.pop(ticker, None)

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
        if estimated or not research.trusted_execution_quote(d):
            self._note(f"skip {d.ticker}: no trusted regular-session bid/ask; "
                       "the displayed cost is only an ADV proxy or the book is suspect", "info")
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

    def _retained_signals(self) -> list[dict]:
        """Keep whole sessions, not a row count.

        A flat 400-row cap made the 60-day gate unreachable: at roughly seven signals a
        session, 61 sessions is 427 rows, so truncation left about 57 distinct days and
        the threshold could never be crossed. It also truncated mid-session, which is the
        partial-basket bias again by another route. Retention is now counted in sessions,
        and a session is kept whole or not at all.
        """
        by_day: dict[str, list[dict]] = {}
        undated: list[dict] = []
        for item in self.signal_log:
            day = str(item.get(SIGNAL_DAY_FIELD) or "")
            (by_day.setdefault(day, []) if day else undated).append(item)
        keep_days = sorted(by_day, reverse=True)[:SIGNAL_RETENTION_SESSIONS]
        kept = [row for day in keep_days for row in by_day[day]]
        return kept + undated[:20]

    def _archive_signal(self, row: dict) -> None:
        """Append-only evidence, so retention can never change a past statistic.

        The hot log is trimmed for memory, which meant analytics saw 181 sessions before
        a restart and 180 after - the same question answered differently depending on
        uptime. Nothing is discarded here; the trimmed log is only a cache over this.
        """
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(SIGNAL_ARCHIVE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError as e:
            self.persistence_error = f"signal archive write failed: {type(e).__name__}"

    def _proxy_cost_share(self, admitted_days: set[str]) -> float:
        """Share of admitted rows whose cost was an ADV proxy rather than a real quote.

        Scoped to the sessions that actually entered the mean, so it describes the
        sample being reported rather than every row ever logged.
        """
        rows = [x for x in self.signal_log
                if str(x.get(SIGNAL_DAY_FIELD) or "") in admitted_days]
        if not rows:
            return 0.0
        return round(100 * sum(bool(x.get("cost_is_proxy")) for x in rows) / len(rows), 1)

    def daily_baskets(self, horizon: str = "5") -> dict:
        """Equal-weight basket per session, admitting only wholly resolved sessions.

        A session is complete only when EVERY ticker logged that day has an outcome at
        the horizon. Admitting partly resolved days silently drops the missing names from
        the equal-weight basket, and the names that stay unresolved are exactly the ones
        that halted, delisted or stopped returning data - overwhelmingly the losers. One
        winner plus one halted name was scoring as a complete +10% day.

        That is survivorship bias, reproduced inside the forward tracker built to avoid
        it. Excluding the whole day is the conservative choice: a partial basket is not a
        smaller sample, it is a biased one.
        """
        current = [x for x in self.signal_log
                   if x.get("engine_version") == SIGNAL_ENGINE_VERSION
                   and x.get(SIGNAL_DAY_FIELD)]
        by_day: dict[str, list[dict]] = {}
        for item in current:
            by_day.setdefault(str(item[SIGNAL_DAY_FIELD]), []).append(item)

        # Calendar allowance for a session-counted horizon, plus a grace period.
        mature_after = math.ceil(int(horizon) * 7 / 5) + HORIZON_GRACE_DAYS
        today = datetime.now(NY).date()

        complete: list[dict] = []
        pending_days, stale_days = 0, 0
        pending_rows, stale_rows = 0, 0
        stale_detail: list[dict] = []
        for day in sorted(by_day):
            rows = by_day[day]
            outcomes = [(r.get("outcomes") or {}).get(str(horizon)) for r in rows]
            resolved = [o for o in outcomes
                        if isinstance(o, dict) and o.get("net_return_pct") is not None]
            missing = len(rows) - len(resolved)
            if missing:
                try:
                    age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
                except ValueError:
                    age = mature_after + 1        # unparseable dates are treated as mature
                if age < mature_after:
                    pending_days += 1
                    pending_rows += missing
                else:
                    # matured and still missing: this is the informative kind
                    stale_days += 1
                    stale_rows += missing
                    nets = [float(o["net_return_pct"]) for o in resolved]
                    stale_detail.append({"day": day, "members": len(rows),
                                         "missing": missing,
                                         "resolved_net_pct": (sum(nets) / len(nets)
                                                              if nets else None)})
                continue
            nets = [float(o["net_return_pct"]) for o in resolved]
            excess = [o.get("net_excess_return_pct") for o in resolved]
            complete.append({
                "day": day,
                "members": len(rows),
                "net_pct": sum(nets) / len(nets),
                # benchmarked only when EVERY member has a benchmark, for the same reason
                "excess_pct": (sum(float(e) for e in excess) / len(excess)
                               if all(e is not None for e in excess) else None),
            })
        return {
            "horizon": str(horizon),
            "baskets": complete,
            "logged_days": len(by_day),
            "logged_rows": len(current),
            "pending_days": pending_days,
            "pending_rows": pending_rows,
            "stale_incomplete_days": stale_days,
            "stale_incomplete_rows": stale_rows,
            "stale_detail": stale_detail[:20],
            # kept for callers that only want "not complete"
            "incomplete_days": pending_days + stale_days,
            "unresolved_rows": pending_rows + stale_rows,
            "mature_after_days": mature_after,
        }

    def missing_outcome_bound(self, horizon: str = "5") -> dict:
        """Mean if every matured-but-missing outcome were the worst case.

        Excluding stale days is not conservative. It shrinks the sample, but the days
        that fail to resolve are the ones holding halted or delisted names, so removing
        them removes losses and lifts the estimate. The honest question is whether the
        result survives assuming those names went to zero.
        """
        book = self.daily_baskets(horizon)
        rows: list[float] = []
        for basket in book["baskets"]:
            rows.extend([basket["net_pct"]] * basket["members"])
        for day in book["stale_detail"]:
            resolved = day.get("resolved_net_pct")
            resolved_members = day["members"] - day["missing"]
            if resolved is not None and resolved_members > 0:
                rows.extend([float(resolved)] * resolved_members)
            rows.extend([MISSING_OUTCOME_ASSUMPTION_PCT] * day["missing"])
        if not rows:
            return {"applicable": False, "reason": "no admitted or stale rows"}
        bounded = sum(rows) / len(rows)
        return {
            "applicable": True,
            "assumed_missing_outcome_pct": MISSING_OUTCOME_ASSUMPTION_PCT,
            "bounded_mean_net_pct": round(bounded, 3),
            "rows_included": len(rows),
            "imputed_rows": book["stale_incomplete_rows"],
            "clears_zero": bool(bounded > 0),
        }

    def signal_day_progress(self, horizon: str = "5") -> dict:
        """The single count of forward progress, shared by the clock and the gate.

        Logged, completed and benchmarked are three different quantities: a logged day
        only records that the rule fired. Counting logged days once let the clock read
        60/60 while the gate still reported COLLECTING with nothing completed.
        """
        book = self.daily_baskets(horizon)
        baskets = book["baskets"]
        benchmarked = [b for b in baskets if b["excess_pct"] is not None]
        required = 60
        benchmark_required = math.ceil(0.8 * required)
        coverage = (len(baskets) / book["logged_days"] * 100.0
                    if book["logged_days"] else 0.0)
        return {
            "horizon": str(horizon),
            "signal_days_logged": book["logged_days"],
            "completed_signal_days": len(baskets),
            "benchmarked_signal_days": len(benchmarked),
            "completed_signal_days_required": required,
            "benchmarked_signal_days_required": benchmark_required,
            "completed_days_remaining": max(0, required - len(baskets)),
            "benchmark_days_remaining": max(0, benchmark_required - len(benchmarked)),
            # days holding at least one unresolved ticker - NOT "days with nothing done"
            "incomplete_signal_days": book["incomplete_days"],
            "unresolved_rows": book["unresolved_rows"],
            "outcome_coverage_pct": round(coverage, 1),
        }

    def _evidence_clock(self, horizon: str = "5") -> dict:
        """How much forward evidence exists, and what a version bump would discard.

        Filtering the signal log to the current engine version is right: prior-rule
        outcomes would contaminate forward accuracy. The unpriced side is that each bump
        restarts collection at zero. Making that cost visible is the point.

        No ETA is offered. This rule's own signal rate has never been measured, and the
        historical proxy's rate cannot be promoted into an estimate for it.
        """
        progress = self.signal_day_progress(horizon)
        days_live = max(0.0, (time.time() - self.engine_version_started_at) / 86400.0)
        return {
            "strategy_id": research.LIVE_STRATEGY_ID,
            "engine_version": SIGNAL_ENGINE_VERSION,
            "days_since_version_change": round(days_live, 2),
            **progress,
            "discarded_by_next_version_bump": progress["signal_days_logged"],
            "note": ("a strategy change discards every row above and restarts this "
                     "clock. Progress toward a verdict is measured in COMPLETED signal "
                     "days, not logged ones: a logged day only records that the rule "
                     "fired. 60 completed days is the minimum before a first verdict, "
                     "which may itself be REJECTED - it is not proof of profitability."),
        }

    def _manage(self, p: Position, d):
        p.last_price = d.price
        bid, mid = d.bid, d.price
        if bid > p.high_water:
            p.high_water = bid
        if USE_TRAILING_STOP and not p.trailing and bid >= p.entry * (1 + TRAIL_ARM_PCT / 100.0):
            p.trailing = True
            p.stop = max(p.stop, p.entry)                       # at worst, breakeven
            self._note(f"{p.ticker} +{TRAIL_ARM_PCT:.0f}% - trailing armed, stop to breakeven", "info")
        if p.trailing:
            p.stop = max(p.stop, p.high_water * (1 - TRAIL_PCT / 100.0))

        if bid <= p.stop:
            self._close(p.ticker, bid, mid, "trailing stop" if p.trailing else "stop loss")
            return
        if USE_FIXED_TARGET and bid >= p.take_profit:
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

    async def _scan(self, mode: str = "full"):
        """Serialize manual, startup, and scheduled scans within this process."""
        if self._scan_lock.locked():
            self._note("scan request ignored: a scan is already in progress", "info")
            return
        async with self._scan_lock:
            await self._scan_locked(mode)

    async def _scan_locked(self, mode: str = "full"):
        """Screen wide -> score everything -> rank -> AI-review the top -> signal -> trade.

        Only the top few get an AI call: the free tier has a daily token budget, and
        spending it on the 40th-ranked name is waste. Everything still gets a
        mechanical score, so the leaderboard is complete.
        """
        mode = "pulse" if mode == "pulse" else "full"
        started = time.time()
        self.last_scan_started = started
        self.last_scan_mode = mode
        self.status = f"{mode} screening the market..."
        try:
            pool = SCREEN_POOL if mode == "full" else PULSE_SCREEN_POOL
            fresh = await asyncio.to_thread(research.screen, pool)
            if mode == "pulse":
                # Keep confirming prior candidates while reserving most of the pulse
                # for fresh movers returned by the live screener.
                priority = [
                    str(w.get("ticker") or "") for w in self.watchlist
                    if (w.get("signal") or {}).get("candidate_action")
                    in ("BUY", "STRONG BUY")
                ]
                syms = list(dict.fromkeys(priority + fresh))[:PULSE_SCORE_LIMIT]
            else:
                syms = fresh
        except Exception as e:
            self.last_error = f"screen: {type(e).__name__}: {str(e)[:90]}"
            self._note(self.last_error, "loss")
            self._scan_failed(started)
            return
        if not syms:
            self.last_error = "screener returned no candidates"
            self._note(self.last_error, "info")
            self._scan_failed(started, empty=True)
            return

        # ---- 1) mechanical scoring pass over the whole pool ----
        async def score_one(sym):
            try:
                d = await asyncio.to_thread(research.build_dossier, sym)
                if d.error or d.price <= 0:
                    return None
                r = research.rank_score(d)
                return r["composite"], d, r
            except Exception as e:
                self.last_error = f"score {sym}: {type(e).__name__}"
                return None

        scored = []
        for start in range(0, len(syms), DOSSIER_CONCURRENCY):
            batch = syms[start:start + DOSSIER_CONCURRENCY]
            self.status = f"scoring {start + 1}-{start + len(batch)}/{len(syms)}"
            results = await asyncio.gather(*(score_one(sym) for sym in batch))
            scored.extend(x for x in results if x is not None)
            await asyncio.sleep(0.15)
        if not scored:
            self.last_error = "no candidates could be scored"
            self._scan_failed(started, empty=True)
            return

        scored.sort(key=lambda x: -x[0])
        top = scored[:TOP_N]

        # ---- 2) AI deep-dive on the best few ----
        board = []
        reviews = 0
        for rank, (comp, d, r) in enumerate(top, start=1):
            ai, ai_err, ai_model = None, "", ""
            rejected = self.hard_reject(d)
            eligible = research.mechanical_setup(d, r)
            if reviews < AI_DEEP_DIVE and not rejected and eligible:
                self.status = f"AI reviewing #{rank} {d.ticker}..."
                ai, ai_err, cached, ai_model = self._cached_ai(d, comp)
                if not cached:
                    try:
                        # Review the same snapshot that was ranked. Re-fetching here
                        # used to let the AI and mechanical engine see different facts.
                        res = await research.analyse_dossier(d)
                        ai, ai_err = res.get("ai"), res.get("ai_error", "")
                        ai_model = str(res.get("model") or "")
                        if ai_err:
                            self.last_error = ai_err[:140]
                    except Exception as e:
                        ai_err = f"{type(e).__name__}: {str(e)[:80]}"
                    self._store_ai(d, comp, ai, ai_err, ai_model)
                    reviews += 1
                    await asyncio.sleep(2.5)
            sig = research.signal_from(d, r, ai)
            spread, spread_estimated = research.effective_spread(d)
            alignment = research.catalyst_alignment(d)
            board.append({
                "rank": rank, "ticker": d.ticker, "name": d.name,
                "price": round(d.price, 4), "change_pct": round(d.change_pct, 2),
                "spread_pct": round(spread, 2),
                "spread_quoted": round(d.spread_pct, 2),
                "spread_estimated": spread_estimated,
                "spread_unreliable": spread_estimated,
                "market_state": d.market_state,
                "bid": round(d.bid, 4), "ask": round(d.ask, 4),
                "quote_age_min": round(d.quote_age_min, 2),
                "volume_surge": round(d.volume_surge, 2),
                "composite": r["composite"], "hype": r["hype"],
                "technical": r["technical"], "catalyst": r["catalyst"],
                "quality": r["quality"], "tradeability": r["tradeability"],
                "hype_why": r["hype_why"], "quality_why": r["quality_why"],
                "technical_why": r["technical_why"], "catalyst_why": r["catalyst_why"],
                "trade_why": r["trade_why"], "data_completeness": d.data_completeness,
                "signal": sig, "ai": ai, "ai_error": ai_err, "ai_model": ai_model,
                "rejected": rejected, "catalysts": d.catalysts[:5], "flags": d.flags[:6],
                "catalyst_key": self._catalyst_key(d),
                "catalyst_alignment": alignment,
                "sec_accessions": [
                    str(x.get("accessionNumber") or "")
                    for x in (d.recent_filings or []) if x.get("official_sec")
                ][:5],
                "sec_items": list(d.recent_8k_items or []),
                "adverse_sec_items": list(d.adverse_8k_items or []),
                "news_snapshot": [
                    {"title": str(x.get("title") or "")[:180],
                     "publisher": str(x.get("publisher") or "")[:60],
                     "when": str(x.get("when") or "")[:30],
                     "age_hours": x.get("age_hours")}
                    for x in (d.news or [])[:5]
                ],
                "quote_reliable": research.trusted_execution_quote(d),
                "held": d.ticker in self.pos, "t": time.time(),
            })

        self._update_setup_states(board)
        self.watchlist = board
        self.scan_count += 1
        self.last_scan = time.time()
        if mode == "full":
            self.last_full_scan = self.last_scan
        self.last_scan_duration = round(self.last_scan - started, 2)
        self.scan_failures = 0
        self.provider_backoff_until = 0.0
        self.last_error = ""
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
        if not self.enabled:
            self._note("research updated; new paper entries are paused", "info")
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
            if not (b.get("confirmation") or {}).get("confirmed"):
                continue
            try:
                d = await asyncio.to_thread(research.build_dossier, b["ticker"])
                if not d.error and d.price > 0:
                    self._open(d, b.get("ai") or {}, b["signal"], b["rank"])
            except Exception as e:
                self.last_error = f"open {b['ticker']}: {type(e).__name__}"
            await asyncio.sleep(0.3)

    def _scan_failed(self, started: float, empty: bool = False):
        """Record bounded exponential provider backoff without stopping the loop."""
        now = time.time()
        self.last_scan = now
        self.last_scan_duration = round(now - started, 2)
        self.scan_count += 1
        self.scan_failures += 1
        base = RETRY_EMPTY_SEC if empty else 60
        delay = min(MAX_PROVIDER_BACKOFF_SEC, base * (2 ** (self.scan_failures - 1)))
        self.provider_backoff_until = now + delay
        self.status = f"provider backoff {int(delay // 60)}m"

    def _record_signals(self, board):
        """Log every actionable signal with the price at the time, so we can later
        MEASURE whether these calls beat random instead of assuming they do."""
        now = time.time()
        signal_day = datetime.fromtimestamp(now, NY).strftime("%Y-%m-%d")
        for b in board:
            act = b["signal"]["action"]
            candidate_action = b["signal"].get("candidate_action", act)
            confirmed = bool((b.get("confirmation") or {}).get("confirmed"))
            executable_snapshot = bool(
                b.get("quote_reliable")
                and str(b.get("market_state") or "").upper() == "REGULAR"
                and not b.get("spread_estimated")
            )
            if candidate_action in ("BUY", "STRONG BUY") and confirmed and executable_snapshot:
                # One observation per ticker/session. Repeated adaptive scans are
                # correlated duplicates, not independent evidence of accuracy.
                if any(x.get("ticker") == b["ticker"] and x.get(SIGNAL_DAY_FIELD) == signal_day
                       for x in self.signal_log):
                    continue
                self.signal_log = self._retained_signals()
                self.signal_log.insert(0, {
                    "id": uuid.uuid4().hex[:10], "t": now, SIGNAL_DAY_FIELD: signal_day,
                    "signal_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "engine_version": SIGNAL_ENGINE_VERSION,
                    "strategy_id": research.LIVE_STRATEGY_ID,
                    "ticker": b["ticker"], "action": act,
                    "candidate_action": candidate_action,
                    "confirmed": True,
                    "confirmation_observations": (b.get("confirmation") or {}).get("observations"),
                    "executed": act in ("BUY", "STRONG BUY"),
                    "rank": b["rank"], "price": b["price"],
                    "decision_mid": b["price"],
                    "bid": b.get("bid"), "ask": b.get("ask"),
                    "quote_age_min": b.get("quote_age_min"),
                    "market_state": b.get("market_state"),
                    "quote_reliable": True,
                    "modeled_round_trip_cost_pct": b.get("spread_pct"),
                    "cost_is_proxy": bool(b.get("spread_estimated")),
                    "benchmark_ticker": "IWM",
                    "benchmark_price": b["signal"].get("benchmark_price"),
                    "stop": b["signal"].get("stop"), "target1": b["signal"].get("target1"),
                    "target2": b["signal"].get("target2"),
                    "composite": b["composite"], "hype": b["hype"],
                    "technical": b.get("technical"), "catalyst": b.get("catalyst"),
                    "quality": b["quality"], "tradeability": b.get("tradeability"),
                    "catalyst_alignment": b.get("catalyst_alignment") or {},
                    "sec_accessions": b.get("sec_accessions") or [],
                    "sec_items": b.get("sec_items") or [],
                    "adverse_sec_items": b.get("adverse_sec_items") or [],
                    "news_snapshot": b.get("news_snapshot") or [],
                    "ai_model": b.get("ai_model") or "",
                    "ai_verdict": (b.get("ai") or {}).get("verdict"),
                    "ai_conviction": (b.get("ai") or {}).get("conviction"),
                    "outcomes": {}, "resolved": False,
                })
                self._archive_signal(self.signal_log[0])
        # Session-based, matching what _save persists, so analytics cannot change across
        # a restart. The append-only archive keeps everything regardless.
        self.signal_log = self._retained_signals()

    async def _update_signal_outcomes(self):
        """Attach causal, cost-adjusted 1/5/10-session returns to prior signals."""
        pending = [x for x in self.signal_log
                   if not x.get("resolved") and x.get(SIGNAL_DAY_FIELD)]
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
        benchmark = None
        try:
            benchmark = await asyncio.to_thread(fetch_history, "IWM")
            if benchmark is not None and not benchmark.empty:
                benchmark = benchmark.dropna(subset=["Close"])
        except Exception:
            benchmark = None
        for ticker, items in by_ticker.items():
            try:
                hist = await asyncio.to_thread(fetch_history, ticker)
                if hist is None or hist.empty:
                    continue
                hist = hist.dropna(subset=["Close"])
                dates = [x.date().isoformat() for x in hist.index]
                for item in items:
                    indices = [i for i, day in enumerate(dates)
                               if day > item[SIGNAL_DAY_FIELD]]
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
                        gross = (end_close / entry - 1) * 100
                        cost = max(0.0, float(item.get("modeled_round_trip_cost_pct") or 0))
                        bench_return = None
                        if benchmark is not None and not benchmark.empty:
                            bench_dates = [x.date().isoformat() for x in benchmark.index]
                            bench_idx = [i for i, day in enumerate(bench_dates)
                                         if day > item[SIGNAL_DAY_FIELD]]
                            if len(bench_idx) >= horizon:
                                # Use the same signal snapshot convention as the stock:
                                # signal-day close to the matching future close.
                                prior_rows = [i for i, day in enumerate(bench_dates)
                                              if day <= item[SIGNAL_DAY_FIELD]]
                                if prior_rows:
                                    bench_entry = float(item.get("benchmark_price") or 0)
                                    if bench_entry <= 0:
                                        bench_entry = float(benchmark.iloc[prior_rows[-1]]["Close"])
                                    bench_end = float(benchmark.iloc[bench_idx[horizon - 1]]["Close"])
                                    if bench_entry > 0:
                                        bench_return = (bench_end / bench_entry - 1) * 100
                        outcomes[key] = {
                            "return_pct": round(gross, 2),
                            "gross_return_pct": round(gross, 2),
                            "net_return_pct": round(gross - cost, 2),
                            "benchmark_return_pct": (round(bench_return, 2)
                                                     if bench_return is not None else None),
                            "net_excess_return_pct": (round(gross - cost - bench_return, 2)
                                                      if bench_return is not None else None),
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
        """Display-only per-horizon summary. NOT evidence.

        These averages are taken over whichever rows happen to have resolved, so they
        carry the survivor bias the basket builder exists to remove: one +10% winner
        beside an unresolved halted name reports count 1 and +10%. Every entry is
        therefore stamped non-evidentiary and paired with the admitted-basket counts, so
        a reader can see how much of the log the number actually covers. Verdicts come
        from forward_validation(), which admits only wholly resolved sessions.
        """
        stats = {}
        for horizon in SIGNAL_HORIZONS:
            book = self.daily_baskets(str(horizon))
            values = [x.get("outcomes", {}).get(str(horizon)) for x in self.signal_log]
            values = [x for x in values if isinstance(x, dict)]
            returns = [float(x.get("net_return_pct", x.get("return_pct", 0))) for x in values]
            excess = [float(x["net_excess_return_pct"]) for x in values
                      if x.get("net_excess_return_pct") is not None]
            stats[str(horizon)] = {
                "count": len(values),
                "net_hit_rate": round(100 * sum(r > 0 for r in returns) / len(returns), 1) if returns else 0.0,
                "hit_rate": round(100 * sum(r > 0 for r in returns) / len(returns), 1) if returns else 0.0,
                "avg_net_return_pct": round(sum(returns) / len(returns), 2) if returns else 0.0,
                "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else 0.0,
                "avg_net_excess_pct": round(sum(excess) / len(excess), 2) if excess else None,
                "target1_rate": round(100 * sum(bool(x.get("target1_hit")) for x in values) / len(values), 1) if values else 0.0,
                "stop_rate": round(100 * sum(bool(x.get("stop_hit")) for x in values) / len(values), 1) if values else 0.0,
                "evidentiary": False,
                "basis": "resolved rows only - survivor-biased; see forward_validation",
                "admitted_basket_days": len(book["baskets"]),
                "stale_incomplete_days": book["stale_incomplete_days"],
            }
        return stats

    @staticmethod
    def _hac_mean_ci(values: list[float], max_lag: int = 5) -> tuple[float, float, float]:
        """Mean and a Newey-West 95% interval for serially dependent signal days."""
        n = len(values)
        if not n:
            return 0.0, 0.0, 0.0
        mean = sum(values) / n
        if n == 1:
            return mean, mean, mean
        dev = [x - mean for x in values]
        gamma0 = sum(x * x for x in dev) / n
        long_run_var = gamma0
        lag_cap = min(max_lag, n - 1)
        for lag in range(1, lag_cap + 1):
            covariance = sum(dev[i] * dev[i - lag] for i in range(lag, n)) / n
            weight = 1.0 - lag / (lag_cap + 1.0)
            long_run_var += 2.0 * weight * covariance
        se = math.sqrt(max(0.0, long_run_var) / n)
        return mean, mean - 1.96 * se, mean + 1.96 * se

    def forward_validation(self, horizon: str = "5") -> dict:
        """Prospective evidence for v2, grouped by signal day to avoid fake sample size.

        This can report PROMISING or REJECTED, never authorize trading.  A live paper
        sample still lacks a licensed point-in-time universe and real fills, so the
        reproducible edge-policy audit remains the only execution gate.
        """
        # Exactly the baskets the clock counts. Building a second, looser set here is
        # what let a partly resolved day into the mean while the clock reported it as
        # complete; the missing names are the halted and delisted ones.
        book = self.daily_baskets(horizon)
        daily_net = [b["net_pct"] for b in book["baskets"]]
        daily_dates = [b["day"] for b in book["baskets"]]
        daily_excess = [b["excess_pct"] for b in book["baskets"]
                        if b["excess_pct"] is not None]

        mean, lo, hi = self._hac_mean_ci(daily_net)
        ex_mean, ex_lo, ex_hi = self._hac_mean_ci(daily_excess)
        feasibility_key = (
            str(horizon), tuple(daily_dates), tuple(round(value, 8) for value in daily_net)
        )
        if self._forward_feasibility_cache[0] != feasibility_key:
            planning = feasibility.feasibility(
                [value / 100.0 for value in daily_net],
                daily_dates,
                target_effect_pct=2.0,
                patience_years=5.0,
                n_boot=800,
                block=5,
                min_history_years=1.0,
            )
            planning["summary"] = feasibility.verdict_line(planning)
            self._forward_feasibility_cache = (feasibility_key, planning)
        else:
            planning = dict(self._forward_feasibility_cache[1])
        # Same source as the evidence clock, so the two can never report different
        # progress against the same log.
        progress = self.signal_day_progress(horizon)
        bound = self.missing_outcome_bound(horizon)
        required_days = progress["completed_signal_days_required"]
        enough_excess = (progress["benchmarked_signal_days"]
                         >= progress["benchmarked_signal_days_required"])
        if progress["completed_signal_days"] < required_days or not enough_excess:
            status = "COLLECTING"
            reason = (
                f"need {required_days} completed signal days with benchmark coverage; "
                f"have {progress['completed_signal_days']} net / "
                f"{progress['benchmarked_signal_days']} benchmarked "
                f"({progress['incomplete_signal_days']} day(s) hold "
                f"{progress['unresolved_rows']} unresolved ticker(s) and are excluded)"
            )
        elif mean <= 0 or ex_mean <= 0 or lo <= 0 or ex_lo <= 0:
            status = "REJECTED"
            reason = "forward net edge is non-positive or its dependence-robust 95% interval crosses zero"
        elif book["stale_incomplete_days"] and not bound.get("clears_zero"):
            # Matured days that never resolved are not missing at random: they hold the
            # names that halted or delisted. Excluding them removes losses, so a positive
            # verdict may only stand if it survives assuming those names went to zero.
            status = "DATA_INCOMPLETE"
            reason = (
                f"{book['stale_incomplete_days']} matured day(s) still hold "
                f"{book['stale_incomplete_rows']} unresolved ticker(s); assuming those "
                f"went to {MISSING_OUTCOME_ASSUMPTION_PCT:.0f}% the mean is "
                f"{bound.get('bounded_mean_net_pct')}%, so the positive result is not "
                f"robust to the missing outcomes. Resolving them needs delisting-aware "
                f"data this desk does not have."
            )
        else:
            status = "PROMISING_NOT_VALIDATED"
            reason = "positive after-cost and IWM-relative forward result; formal audit is still required"

        return {
            "status": status, "auto_trade_allowed": False, "reason": reason,
            # counted from the admitted baskets only, so a partly resolved day cannot
            # inflate the sample it was just excluded from
            "horizon_sessions": int(horizon),
            "completed_signals": sum(b["members"] for b in book["baskets"]),
            "signal_days": len(daily_net), "benchmarked_days": len(daily_excess),
            "unique_tickers": len({
                x.get("ticker") for x in self.signal_log
                if str(x.get(SIGNAL_DAY_FIELD) or "") in set(daily_dates)}),
            "incomplete_signal_days": book["incomplete_days"],
            "unresolved_rows": book["unresolved_rows"],
            "pending_days": book["pending_days"],
            "stale_incomplete_days": book["stale_incomplete_days"],
            "stale_incomplete_rows": book["stale_incomplete_rows"],
            "missing_outcome_bound": bound,
            "mean_net_pct": round(mean, 3),
            "net_hac_95_pct": [round(lo, 3), round(hi, 3)],
            "mean_net_excess_pct": round(ex_mean, 3) if daily_excess else None,
            "excess_hac_95_pct": ([round(ex_lo, 3), round(ex_hi, 3)]
                                  if daily_excess else None),
            "cost_proxy_share_pct": self._proxy_cost_share(set(daily_dates)),
            "minimum_signal_days": required_days,
            "feasibility": planning,
            "grouping": "equal-weight basket per signal day",
            "uncertainty": "Newey-West HAC, 5-lag, 95% interval",
        }

    async def manage_loop(self):
        await asyncio.sleep(12)
        while True:
            try:
                now = time.time()
                today = datetime.now(NY).strftime("%Y-%m-%d")
                if today != self.day_key:
                    self.day_key = today
                    self.day_pnl = 0.0
                is_open = self.market_open()
                if now - self.last_outcome_update >= OUTCOME_UPDATE_SEC:
                    await self._update_signal_outcomes()

                # Pausing controls NEW entries only. Existing risk is always marked and
                # managed, and research always runs so the forward sample is continuous.
                if self.pos and is_open and now - self._last_mark >= MARK_EVERY_SEC:
                    await self._mark_positions()
                    self._last_mark = time.time()

                if self._scan_task is not None and self._scan_task.done():
                    try:
                        self._scan_task.result()
                    except Exception as e:
                        self.last_error = f"scan task: {type(e).__name__}: {str(e)[:100]}"
                        self._note(self.last_error, "loss")
                        self._scan_failed(time.time())
                    self._scan_task = None

                # An after-hours setup is never blindly carried into the open.
                # Force a full re-screen on the closed->open transition.
                just_opened = is_open and not self._was_market_open
                if just_opened:
                    self._note("market opened - revalidating every setup with fresh quotes", "info")
                    self.provider_backoff_until = 0.0
                plan = self.scan_plan(is_open, force=just_opened)
                if plan and self._scan_task is None:
                    self._scan_task = asyncio.create_task(self._scan(plan))

                if not self._scan_lock.locked():
                    # "entries on" read as though the desk would trade, while the edge
                    # gate was refusing every fill. Execution did fail closed, but the
                    # status implied otherwise. Name the binding constraint instead.
                    # Mirror _open()'s authorization exactly: auto_trade_allowed alone
                    # would let a validated but UNRELATED strategy make the dashboard
                    # claim entries are live while every fill still fails closed.
                    policy = research.edge_policy() or {}
                    authorized = bool(
                        policy.get("auto_trade_allowed")
                        and (policy.get("strategy_id")
                             or policy.get("selected_strategy")) == research.LIVE_STRATEGY_ID
                    )
                    if not self.enabled:
                        entry_state = "entries paused"
                    elif not authorized:
                        entry_state = "paper-entry toggle on; execution blocked by edge gate"
                    else:
                        entry_state = "entries on"
                    if self.pos:
                        self.status = (f"watching {len(self.pos)} position(s); continuous research, "
                                       f"{entry_state}")
                    elif is_open:
                        self.status = f"continuous market surveillance; {entry_state}"
                    else:
                        self.status = f"continuous {self.session_phase(is_open)} research; {entry_state}"
                self._was_market_open = is_open
                self._save()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "error"
            await asyncio.sleep(LOOP_TICK_SEC)

    # ---------------- api ----------------
    def equity(self) -> float:
        eq = self.balance
        for p in self.pos.values():
            eq += p.qty * (p.last_price or p.entry)
        return eq

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self._note(
            "new paper entries ENABLED; continuous scanner remains live"
            if self.enabled else
            "new paper entries PAUSED; research and protective exits remain live"
        )
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
        self.last_scan_started = 0.0
        self.signal_log = []
        self.last_outcome_update = 0.0
        self.last_full_scan = 0.0
        self.last_scan_duration = 0.0
        self.last_scan_mode = "none"
        self.scan_failures = 0
        self.provider_backoff_until = 0.0
        self.setup_states = {}
        self._ai_cache = {}
        self.last_error = ""          # stale provider errors must not linger
        self._note("bot reset to $100")
        self._save()
        return {"ok": True}

    async def scan_now(self):
        await self._scan("full")
        self._save()
        return {"ok": True, "found": len(self.watchlist)}

    def state(self) -> dict:
        eq = self.equity()
        now = time.time()
        market_is_open = self.market_open()
        interval = self.scan_interval(market_is_open, now)
        anchor = self.last_scan_started or self.last_scan
        due_at = max(self.provider_backoff_until,
                     (anchor + interval) if anchor > 0 else now)
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
            "scanner_always_on": True,
            "entries_enabled": self.enabled,
            "ai_model": (research.LAST_MODEL_USED or getattr(config, "PENNY_AI_MODEL", "?")),
            "status": self.status, "market_open": market_is_open,
            "session_phase": self.session_phase(market_is_open),
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
            "evidence_clock": self._evidence_clock(),
            "persistence_error": self.persistence_error,
            "last_scan": self.last_scan,
            "last_scan_started": self.last_scan_started,
            "last_full_scan": self.last_full_scan,
            "last_scan_mode": self.last_scan_mode,
            "last_scan_duration_sec": self.last_scan_duration,
            "scan_interval_sec": interval,
            "next_scan_in_sec": max(0, int(due_at - now)),
            "scan_in_progress": self._scan_lock.locked(),
            "scan_failures": self.scan_failures,
            "provider_backoff_sec": max(0, int(self.provider_backoff_until - now)),
            "hot_setups": self.hot_setup_count(now),
            "confirmation_required": CONFIRM_SCANS,
            "positions": positions,
            "watchlist": self.watchlist[:TOP_N],
            "signal_log": self.signal_log[:40],
            "signal_stats": self.signal_stats(),
            "forward_validation": self.forward_validation(),
            "top_n": TOP_N,
            "regime": research.market_regime(),
            "edge_policy": research.edge_policy(),
            "live_rule_evidence": research.live_rule_evidence(),
            "max_per_sector": MAX_PER_SECTOR,
            "history": self.history[:40],
            "log": self.log[:25],
            "last_error": self.last_error,
            "rules": (f"always-on adaptive scans ({HOT_SCAN_SEC}s hot / {REGULAR_SCAN_SEC}s regular / "
                      f"{EXTENDED_SCAN_SEC}s extended / {CLOSED_SCAN_SEC}s closed); "
                      f"official non-adverse 8-K + headline aligned within "
                      f"{research.CATALYST_ALIGNMENT_HOURS:.0f}h; "
                      f"{CONFIRM_SCANS} separated trusted regular-session quotes required; "
                      f"evidence gate must be VALIDATED before any auto-trade; "
                      f"risk {RISK_PCT}%/trade and {MAX_PORTFOLIO_RISK_PCT}% total, "
                      f"max {MAX_POSITION_PCT}% per name / {MAX_OPEN} open, spread cap {MAX_SPREAD_PCT}%, "
                      f"ATR-scaled stop, "
                      + ("2.5R target, " if USE_FIXED_TARGET
                         else "no fixed target (paper-only experiment), ")
                      + (f"trail {TRAIL_PCT}% after +{TRAIL_ARM_PCT}%, " if USE_TRAILING_STOP
                         else "no trail, ")
                      + f"time exit {MAX_HOLD_DAYS}d"),
            "note": ("Research runs continuously even when entries are paused or the book is full. "
                     "Pausing never disables protective exits. Paper fills require a fresh regular-session "
                     "bid/ask: buy at ask, sell at bid. "
                     "After-hours, stale, locked, or internally suspect books are ADV proxies for "
                     "ranking only and never count as confirmations. Hard risk gates and "
                     "technical/catalyst confirmation run before the AI; the AI may veto but never "
                     "promote a weak setup. Confirmed unvalidated candidates are never filled, but "
                     "cost-adjusted and IWM-relative results are measured at 1/5/10 later sessions."),
        }
