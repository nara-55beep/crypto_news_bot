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

import ast
import asyncio
import glob
import hashlib
import inspect
import json
import math
import os
import re
import textwrap
import time
import types
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

import config
import penny_quotes
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

# The scanner has two gears. A cheap all-symbol pass refreshes the market snapshot,
# while this deep loop builds the expensive Yahoo/SEC dossiers and refreshes the
# ranked board. Keep one explicit cadence across every market phase so the displayed
# deadline and the scheduler cannot silently disagree. The scan lock prevents overlap;
# if a deep pass itself takes longer than ten seconds, the next starts when it finishes.
DEEP_SCAN_SEC = 10
REGULAR_SCAN_SEC = DEEP_SCAN_SEC
HOT_SCAN_SEC = DEEP_SCAN_SEC
EXTENDED_SCAN_SEC = DEEP_SCAN_SEC
CLOSED_SCAN_SEC = DEEP_SCAN_SEC
FULL_SCAN_SEC = 15 * 60
MARK_EVERY_SEC = 20         # protective exits must not wait for a long market scan
LOOP_TICK_SEC = 5           # maximum housekeeping wait; scan deadlines wake it sooner
# The cheap all-symbol pass is independent of deep Yahoo/SEC/AI work. One pass takes
# about 53 Alpaca snapshot requests for the current ~13k listed universe; a 30-second
# target leaves ample headroom under the standard request limit while staying live.
UNIVERSE_TARGET_CYCLE_SEC = 30
# Free real-time data is IEX-only. Refresh delayed consolidated SIP periodically as the
# completeness baseline, and use IEX between those passes for timely mover discovery.
UNIVERSE_FULL_TAPE_SEC = 15 * 60
TOP_N = 20                 # leaderboard size
SCREEN_POOL = 60           # how many raw candidates to score before ranking
PULSE_SCREEN_POOL = 24
PULSE_SCORE_LIMIT = 32
DOSSIER_CONCURRENCY = 4    # bounded: faster than serial without a provider stampede
AI_DEEP_DIVE = 10          # AI calls allowed PER SCAN (reviews resets each scan),
                           # not per day - keeps one pass inside the free-tier budget
RETRY_EMPTY_SEC = 5 * 60
MAX_PROVIDER_BACKOFF_SEC = 30 * 60
AI_CACHE_SEC = 45 * 60
AI_ERROR_CACHE_SEC = 5 * 60
AI_MATERIAL_PRICE_PCT = 4.0
AI_MATERIAL_SCORE_POINTS = 7.0
AI_CACHE_MAX_NAMES = 100
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
# Same discipline as the session key. The bound read "cost_pct" while the writer stored
# "modeled_round_trip_cost_pct", so every imputed loss was -100% instead of -100% minus
# the round trip - and the tests agreed with the bug because their fixtures invented the
# reader's name. One constant, used by writer, reader and fixtures.
SIGNAL_COST_FIELD = "modeled_round_trip_cost_pct"
# IWM's own horizon return, stored per signal so a name that never resolves still has a
# benchmark leg. Without it the bounded excess collapses onto the bounded net.
SIGNAL_BENCHMARK_FIELD = "benchmark_horizon_pct"
# Retention is measured in sessions, not rows. A 400-row cap silently truncated the
# evidence below the 60-day gate it was supposed to feed, and cut sessions in half.
SIGNAL_RETENTION_SESSIONS = 180
# Days whose horizon has not elapsed are legitimately unmeasurable. Days that matured and
# still have no outcome are informative: a name that stops resolving has usually halted or
# delisted. The two must never be pooled.
HORIZON_GRACE_SESSIONS = 2
# What a permanently missing outcome is assumed to be worth when bounding the result. A
# halted microcap can go to zero, so the bound uses the true worst case rather than a
# comfortable one.
MISSING_OUTCOME_ASSUMPTION_PCT = -100.0
# Append-only evidence. The hot log is capped for memory; this is not, so retention can
# never quietly change a statistic.
SIGNAL_ARCHIVE_PATH = os.path.join(config.DATA_DIR, "pennystock_signal_archive.jsonl")
SIGNAL_ENGINE_VERSION = 7
# The live verdict must contain only AI-approved signals, but the question "does the AI
# add value?" requires the mechanically identical names it rejected.  Both cohorts are
# recorded under one durable measurement path and separated by an explicit role.  Rows
# written before this field existed are the original approved population and therefore
# remain primary for backward compatibility.
EVIDENCE_ROLE_FIELD = "evidence_role"
PRIMARY_EVIDENCE_ROLE = "live_ai_approved"
CONTROL_EVIDENCE_ROLE = "mechanical_ai_control"
AI_SELECTION_FIELD = "ai_selection"
AI_VALUE_MIN_COMPARISON_DAYS = 60
AI_VALUE_MIN_CLASSIFICATION_COVERAGE_PCT = 80.0
# The same floor applied per DAY. A global 80% is satisfied while individual days are
# entirely unclassified, and a day the model never saw is an outage - not a decision to
# hold cash - so scoring it as one credits the selector for an API failure.
AI_VALUE_MIN_DAY_CLASSIFICATION_PCT = 80.0
# Members of one signal day share IWM's horizon return, so in a same-day difference of
# two portfolios the benchmark cancels exactly. Divergence beyond this is not a second
# result; it means the day's rows disagree about the benchmark.
AI_VALUE_BENCHMARK_TOLERANCE_PCT = 0.10
# A model/prompt selector is part of the tested rule.  Keep this separate from both the
# mechanical engine and price-measurement schema so an AI-policy edit cannot pool its
# outcomes with an older policy and call the mixture evidence.
AI_DECISION_POLICY_VERSION = 1
# The STRATEGY and the MEASUREMENT are versioned separately, on purpose. Repairing a
# broken cost model must not restart the strategy's evidence clock, and bumping the
# strategy must not silently re-bless outcomes computed under a cost model since found
# wrong. Schema 0 is everything recorded before the entry basis was fixed: it measured
# gross return from the last TRADE and then subtracted the entry-time spread once as a
# supposed round trip, so a name quoted 1.00/1.20 that closed at 1.20 booked +1.82%
# when the executable ask->bid round trip was -0.83%. Those outcomes are not evidence.
# Schema 2 fixes the timing and validation of schema 1, which had ask-based arithmetic
# but accepted a PRIOR session's book as a close, let an opening quote settle a closing
# horizon, trusted a crossed book, resolved partial daily bars, and never checked the
# entry feed at all. Schema 1 outcomes stay non-evidentiary however they were stamped.
MEASUREMENT_SCHEMA_VERSION = 2
# A long is bought at the ASK. Nothing else is an entry price - not the last trade, not
# the mid. Rows without a usable ask cannot be measured executably at all.
ENTRY_BASIS_FIELD = "entry_ask"
# A daily bar is not final until the session ends and the provider publishes it.
# Resolving against a still-forming bar records a "close" that was never the close.
DAILY_BAR_PUBLICATION_DELAY_MIN = 20
# How old an entry book may be at confirmation and still describe what a purchase would
# have paid. A penny-stock book moves; two minutes is already generous.
ENTRY_QUOTE_MAX_AGE_SEC = 120
# Tolerance for ordinary clock skew between the venue and this machine. Beyond it, a
# timestamp AHEAD of our receipt is a feed or clock fault, not a fresher quote.
ENTRY_QUOTE_CLOCK_SKEW_SEC = 5
# Closing books are chased on their own clock. Tying capture to the hourly outcome timer
# meant a run at 15:54 and the next at 16:54 skipped the window completely.
CLOSING_CAPTURE_LEAD_MIN = 15
CLOSING_CAPTURE_RETRY_SEC = 45
# A later session's book measures a different holding period. It is recorded under its
# own key so it can never be pooled into the horizon it did not measure.
DELAYED_EXIT_SUFFIX = "_delayed_exit"
# How many sessions past the horizon a delayed exit will still be looked for.
MAX_DELAYED_EXIT_SESSIONS = 3


def _as_price(value) -> float | None:
    """A price, or None. Zero and negative are absences, not prices."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


_BEHAVIOUR_DIGESTS: dict = {}
# Scrub only the conventional trailing ``at 0x...>`` identity marker. A blanket
# ``0x...`` replacement also erased meaningful values such as bitmasks and hashes.
_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>$)")


def _semantic_value(value, _seen: set[int] | None = None):
    """Stable JSON-shaped representation of Python code constants and defaults.

    A bare sentinel has no state and is therefore type-only. Ordinary objects may use
    the same default repr while carrying behaviorally meaningful ``__dict__`` or slot
    values; those are serialized explicitly so different thresholds cannot collapse to
    one policy id. Cycles are represented by type rather than object identity.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return {"cycle": type_name}
    seen.add(identity)
    try:
        if isinstance(value, types.CodeType):
            return {
                "bytecode": value.co_code.hex(),
                "constants": [_semantic_value(item, seen) for item in value.co_consts],
                "names": list(value.co_names),
                "varnames": list(value.co_varnames),
                "freevars": list(value.co_freevars),
                "cellvars": list(value.co_cellvars),
                "argcount": value.co_argcount,
                "posonlyargcount": value.co_posonlyargcount,
                "kwonlyargcount": value.co_kwonlyargcount,
                "flags": value.co_flags,
                "exception_table": getattr(value, "co_exceptiontable", b"").hex(),
            }
        if isinstance(value, (tuple, list)):
            return [_semantic_value(item, seen) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [_semantic_value(item, seen) for item in value]
            return {"set": sorted(items, key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), default=str))}
        if isinstance(value, dict):
            return {str(key): _semantic_value(item, seen)
                    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}

        described = {"type": type_name}
        state = {}
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict) and attributes:
            state["attributes"] = _semantic_value(attributes, seen)
        slots = {}
        for cls in type(value).__mro__:
            names = cls.__dict__.get("__slots__", ())
            names = (names,) if isinstance(names, str) else names
            for name in names:
                if name in ("__dict__", "__weakref__") or name in slots:
                    continue
                try:
                    slots[name] = _semantic_value(getattr(value, name), seen)
                except (AttributeError, TypeError):
                    continue
        if slots:
            state["slots"] = {name: slots[name] for name in sorted(slots)}
        if state:
            described["state"] = state
        elif type(value).__repr__ is not object.__repr__:
            # Value types such as Decimal and Path have no Python-visible state, so
            # retain their custom repr. Only the default-style trailing address is
            # identity; other hexadecimal text can be meaningful behavior.
            described["repr"] = _ADDRESS.sub("0xX", repr(value))
        return described
    finally:
        seen.remove(identity)


def _function_runtime_semantics(target) -> dict:
    closure = []
    for cell in getattr(target, "__closure__", None) or ():
        try:
            closure.append(_semantic_value(cell.cell_contents))
        except ValueError:
            closure.append({"empty_cell": True})
    return {
        "defaults": _semantic_value(getattr(target, "__defaults__", None)),
        "kwdefaults": _semantic_value(getattr(target, "__kwdefaults__", None)),
        "closure": closure,
    }


def _behaviour_digest(target) -> str:
    """A digest of what a function DOES, ignoring comments and formatting.

    The source is normalised through the AST and stripped of docstrings, so rewording a
    comment does not discard an evidence population while a changed threshold, prompt
    line or branch does. If source is unavailable, semantic bytecode includes constants
    and defaults. A target with neither source nor a code object raises instead of
    silently hashing to a constant, because "we could not tell whether the selector
    changed" must never look like "it did not change".

    Memoised on the code object plus runtime defaults/closure: this runs on every
    recorded signal and every audited row, and re-reading the module from disk each time
    made the work quadratic. A replaced function - an edit, or a test patch - is a
    different code object and misses the cache, so the digest still moves whenever the
    behaviour does.
    """
    code = getattr(target, "__code__", None)
    runtime = _function_runtime_semantics(target)
    runtime_key = json.dumps(runtime, sort_keys=True, separators=(",", ":"),
                             default=str)
    cache_key = (code, runtime_key) if code is not None else None
    if cache_key is not None and cache_key in _BEHAVIOUR_DIGESTS:
        return _BEHAVIOUR_DIGESTS[cache_key]
    try:
        source = textwrap.dedent(inspect.getsource(target))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:]
        behaviour = {"source_ast": ast.dump(tree, annotate_fields=False),
                     "runtime": runtime}
    except (OSError, TypeError, SyntaxError, IndentationError):
        if code is None:
            raise TypeError(
                f"selector component is not introspectable: {target!r}")
        behaviour = {"semantic_bytecode": _semantic_value(code),
                     "runtime": runtime}
    normalised = json.dumps(behaviour, sort_keys=True, separators=(",", ":"),
                            default=str)
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]
    if cache_key is not None:
        _BEHAVIOUR_DIGESTS[cache_key] = digest
    return digest


def ai_decision_policy_components() -> dict:
    """Every input that decides which names the AI selector approves.

    Hashing only the system prompt and model chain left most of the selector outside the
    fingerprint. The USER prompt is the dossier the model actually reads; the normaliser
    decides which replies become valid verdicts at all; the acceptance rule turns a
    verdict into an approval; sampling decides how deterministic any of it is; and the
    cache decides how stale a reused decision may be. Editing any of those changes which
    names get approved, so pooling the before and after into one comparison population
    measures two selectors as if they were one - and, since the multiplicity correction
    counts distinct policy ids, an unfingerprinted edit is also a free untracked test.
    """
    return {
        "version": AI_DECISION_POLICY_VERSION,
        # what the model is asked
        "system_prompt": research.SYSTEM_PROMPT,
        "user_prompt_builder": _behaviour_digest(research._dossier_text),
        "user_prompt_dependencies": {
            "effective_spread": _behaviour_digest(research.effective_spread),
            "catalyst_alignment": _behaviour_digest(research.catalyst_alignment),
        },
        # which model answers, and how it is sampled
        "model_chain": list(research.AI_MODEL_CHAIN),
        "preferred_model": getattr(config, "PENNY_AI_MODEL", None),
        "sampling": dict(getattr(research, "AI_SAMPLING", {})),
        "model_extra_body": {k: dict(v) for k, v in
                             getattr(research, "AI_MODEL_EXTRA_BODY", {}).items()},
        "request_timeout_sec": float(getattr(research, "AI_TIMEOUT_SEC", 0.0)),
        "model_call": _behaviour_digest(research._call_ai_long),
        # how the reply becomes a decision
        "response_extractor": _behaviour_digest(research._extract_json),
        "output_normalizer": _behaviour_digest(research._normalize_ai),
        "analysis_pipeline": _behaviour_digest(research.analyse_dossier),
        "acceptance_rule": _behaviour_digest(research.signal_from),
        "require_ai_confirm": bool(research.REQUIRE_AI_CONFIRM),
        # which candidates are reviewed and how long a decision may be reused
        "review_limit_per_scan": AI_DEEP_DIVE,
        "cache_lookup": _behaviour_digest(PennyStockPaperBot._cached_ai),
        "cache_key_builder": _behaviour_digest(PennyStockPaperBot._catalyst_key),
        "cache_store": _behaviour_digest(PennyStockPaperBot._store_ai),
        "cache_sec": AI_CACHE_SEC,
        "error_cache_sec": AI_ERROR_CACHE_SEC,
        "cache_material_price_pct": AI_MATERIAL_PRICE_PCT,
        "cache_material_score_points": AI_MATERIAL_SCORE_POINTS,
        "cache_max_names": AI_CACHE_MAX_NAMES,
    }


def ai_decision_policy_id() -> str:
    """Stable identity of the actual LLM selector used by this process.

    A manual version is not enough: every part of the selector is easy to edit without
    remembering a counter. Hashing the whole surface makes a changed selector start a
    new comparison population automatically rather than pool two policies.
    """
    raw = json.dumps(ai_decision_policy_components(), sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _as_pct(value) -> float | None:
    """A percentage, or None. Unlike a price, zero is a legitimate value here."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def minute_of(when: datetime) -> int:
    """Minutes since midnight, market time."""
    return when.hour * 60 + when.minute


def _quote_is_two_sided(quote) -> bool:
    """A real book: both sides present, and neither crossed nor locked.

    bid 2.00 / ask 1.00 is not a wide quote, it is a broken one. Accepting it took the
    bid as an exit price and called the result evidence.
    """
    if not isinstance(quote, dict):
        return False
    bid, ask = _as_price(quote.get("bid")), _as_price(quote.get("ask"))
    return bid is not None and ask is not None and ask > bid


def _quote_mid(bid, ask) -> float | None:
    b, a = _as_price(bid), _as_price(ask)
    if b is None or a is None or a < b:
        return None
    return (b + a) / 2


def _half_spread_pct(bid, ask) -> float | None:
    """Half the quoted spread, as a percentage of the mid.

    ONE side of the round trip. A buy pays this above the mid and the eventual sell pays
    it again below the exit mid - and it is that second leg the old measurement never
    charged, because it subtracted the entry spread once and called the trip closed.
    """
    b, a = _as_price(bid), _as_price(ask)
    mid = _quote_mid(bid, ask)
    if mid is None or mid <= 0:
        return None
    return (a - b) / 2 / mid * 100


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

    def __init__(self, state_path: str | None = None,
                 archive_path: str | None = None):
        self.state_path = state_path or STATE_PATH
        self.archive_path = archive_path or SIGNAL_ARCHIVE_PATH
        self._quarantine_marker = self.archive_path + ".quarantine"
        self.enabled = False          # opt-in: needs AI credits to be useful
        self.balance = START_BALANCE
        self.pos: dict[str, Position] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.watchlist: list[dict] = []      # latest AI verdicts, incl. the rejects
        self.universe_coverage: dict = {}
        self._universe_rows: list[dict] = []
        self._universe_rows_by_feed: dict[str, list[dict]] = {}
        self._universe_task: asyncio.Task | None = None
        self._universe_ready = asyncio.Event()
        self._universe_pass_in_progress = False
        self._last_delayed_universe_scan = 0.0
        self._next_universe_scan_at = 0.0
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
        self.state_save_error = ""
        # Three distinct failures. A successful append proves the WRITE works; it
        # proves nothing about a corrupt record already on disk, and clearing one with
        # the other is how corruption stopped blocking verdicts after any new signal.
        self.archive_integrity_error = ""
        self.archive_write_error = ""
        self.outbox_error = ""
        # Separate from outbox_error: clearing a drained queue must not
        # also clear the record that evidence was quarantined.
        self.quarantine_error = ""
        # Not an integrity failure, so it does not join archive_error: a missed exit
        # book leaves the outcome non-evidentiary, which already blocks a verdict.
        self.quote_capture_error = ""
        self.calendar_error = ""
        self._last_closing_capture = 0.0
        # The record of when each session ended, stored apart from the quotes so a quote
        # can never be the authority on when the exchange closed.
        self._calendar_path = self.archive_path + ".calendar.json"
        self._session_calendar: dict = {}
        self._archive_outbox: list[dict] = []
        # Set when a quarantine move failed. The damaged outbox is then the only copy
        # of those bytes, so the path is off limits for writing until it is aside.
        self._outbox_write_blocked = False
        self._evidence: dict[str, dict] = {}
        self._was_market_open = False
        self._scan_lock = asyncio.Lock()
        self._load_session_calendar()
        self._load_outbox()
        self._load()

    # ---------------- persistence ----------------
    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled, "balance": self.balance,
                    "positions": {k: asdict(v) for k, v in self.pos.items()},
                    "history": self.history[:200], "log": self.log[:120],
                    "watchlist": self.watchlist[:30],
                    "universe_coverage": self.universe_coverage,
                    "day_key": self.day_key, "day_pnl": self.day_pnl,
                    "scan_count": self.scan_count,
                    "signal_log": self._retained_signals(),
                    "last_full_scan": self.last_full_scan,
                    "setup_states": self.setup_states,
                    "engine_version": SIGNAL_ENGINE_VERSION,
                    "engine_version_started_at": self.engine_version_started_at,
                }, f)
            os.replace(tmp, self.state_path)
            self.state_save_error = ""
            # Production never called _persist_outbox on its own, so a queue stranded by
            # a disk outage stayed memory-only until another archive event happened to
            # arrive. Retry it on the regular save path instead.
            if self._archive_outbox:
                # flush to the archive first; only queue-file persistence if that fails
                if not self._flush_archive_outbox():
                    self._persist_outbox()
        except Exception as e:
            # A silent failure here loses the forward evidence while the service still
            # reports "no errors". Surface it instead.
            self.state_save_error = f"state save failed: {type(e).__name__}: {e}"[:160]

    def _load(self):
        try:
            if not os.path.exists(self.state_path):
                # The archive outlives the state file, so evidence is restored even when
                # only the cache is missing.
                self._evidence = {str(r.get("id") or ""): r
                                  for r in self._replay_archive() if r.get("id")}
                self.signal_log = [x for x in self._evidence.values()
                                   if x.get("engine_version") == SIGNAL_ENGINE_VERSION]
                return
            with open(self.state_path, encoding="utf-8") as f:      # was leaking the handle
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
            self.universe_coverage = (d.get("universe_coverage")
                                      if isinstance(d.get("universe_coverage"), dict)
                                      else {})
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
            # The archive is the record. The persisted list is a cache, so it is used
            # only when the archive has nothing - otherwise retention could silently
            # change a past statistic across a restart.
            merged: dict[str, dict] = {}
            for row in (d.get("signal_log") or []):
                sid = str(row.get("id") or "")
                if sid:
                    merged[sid] = dict(row)
            for row in self._replay_archive():
                sid = str(row.get("id") or "")
                if not sid:
                    continue
                # union the outcomes: an event may have failed to append after the cache
                # was written, so neither side is reliably newer than the other
                base = merged.get(sid, {})
                outcomes = dict(base.get("outcomes") or {})
                outcomes.update(row.get("outcomes") or {})
                legs = dict(base.get(SIGNAL_BENCHMARK_FIELD) or {})
                legs.update(row.get(SIGNAL_BENCHMARK_FIELD) or {})
                merged[sid] = {**base, **row, "outcomes": outcomes,
                               SIGNAL_BENCHMARK_FIELD: legs,
                               "resolved": bool(base.get("resolved")
                                                or row.get("resolved"))}
            self._evidence = merged
            self.signal_log = sorted(
                (x for x in merged.values()
                 if x.get("engine_version") == SIGNAL_ENGINE_VERSION),
                key=lambda r: str(r.get(SIGNAL_DAY_FIELD) or ""), reverse=True)
        except Exception as e:
            self.state_save_error = f"state load failed: {type(e).__name__}: {e}"[:160]

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
        """Return the one advertised deep-scan cadence in every market phase."""
        return DEEP_SCAN_SEC

    def scan_due_at(self, is_open: bool, now: float | None = None) -> float:
        """Absolute deep-scan deadline shared by the scheduler and the dashboard."""
        now = time.time() if now is None else now
        anchor = self.last_scan_started or self.last_scan
        cadence_due = (anchor + self.scan_interval(is_open, now)
                       if anchor > 0 else now)
        return max(self.provider_backoff_until, cadence_due)

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
    def _interleave_candidates(*buckets, limit: int) -> list[str]:
        """Merge discovery sources without letting one ranking own every slot."""
        clean = [[str(symbol).strip().upper() for symbol in (bucket or [])
                  if str(symbol).strip()] for bucket in buckets]
        selected, seen = [], set()
        for index in range(max((len(bucket) for bucket in clean), default=0)):
            for bucket in clean:
                if index >= len(bucket):
                    continue
                symbol = bucket[index]
                if symbol in seen:
                    continue
                selected.append(symbol)
                seen.add(symbol)
                if len(selected) >= max(0, int(limit)):
                    return selected
        return selected

    def _pulse_candidates(self, yahoo_fresh: list[str], pool: int) -> list[str]:
        """Mix held setups, broad-market movers, and Yahoo movers for a fast pass."""
        market_fresh = penny_quotes.select_market_candidates(
            list(self._universe_rows), pool)
        priority = [
            str(w.get("ticker") or "") for w in self.watchlist
            if (w.get("signal") or {}).get("candidate_action")
            in ("BUY", "STRONG BUY")
        ]
        return self._interleave_candidates(
            priority, market_fresh, yahoo_fresh, limit=PULSE_SCORE_LIMIT)

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
        if len(self._ai_cache) > AI_CACHE_MAX_NAMES:
            oldest = sorted(self._ai_cache, key=lambda k: self._ai_cache[k].get("t", 0))
            for key in oldest[:-AI_CACHE_MAX_NAMES]:
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
            # Confirmation belongs to the pre-AI mechanical opportunity set.  Using
            # the post-AI action made every veto disappear before it could become a
            # measured control.  The approved path is unchanged; rejected names are
            # confirmed only for research and can never reach _open().
            mechanical_action = (sig.get("mechanical_action")
                                 or sig.get("candidate_action") or "")
            candidate = mechanical_action in ("BUY", "STRONG BUY")
            live_candidate = sig.get("candidate_action") in ("BUY", "STRONG BUY")
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
                "mechanical_action": mechanical_action,
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
            if candidate and not confirmed and live_candidate:
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

    @staticmethod
    def _benchmark_leg(item: dict, horizon: int,
                       bench_dates: list, bench_close: list) -> float | None:
        """IWM's return over the horizon, anchored the same way for every caller.

        The two paths disagreed: the resolved path measured from the signal-time
        benchmark_price while this one measured from the first FUTURE close, which on a
        rising benchmark reported 36.4% where the other reported 50.0%. Same event, two
        answers, and the bounded excess inherited whichever happened to run. The anchor
        is the signal-time snapshot, falling back to the last close on or before the
        signal day.
        """
        day = str(item.get(SIGNAL_DAY_FIELD) or "")
        future = [i for i, d in enumerate(bench_dates) if d > day]
        if len(future) < horizon:
            return None
        start = float(item.get("benchmark_price") or 0.0)
        if start <= 0:
            prior = [i for i, d in enumerate(bench_dates) if d <= day]
            if not prior:
                return None
            start = bench_close[prior[-1]]
        if start <= 0:
            return None
        return round((bench_close[future[horizon - 1]] / start - 1) * 100, 4)

    # ---------------- execution-realistic measurement ----------------
    @staticmethod
    def entry_basis(item: dict) -> float | None:
        """What a long actually pays: the ASK at signal time.

        Never the last trade and never the mid. Legacy rows predate entry_ask but did
        record the raw book, so the ask is reconstructed rather than the row discarded.
        A row with no ask at all cannot be measured executably and must not be guessed.
        """
        return _as_price(item.get(ENTRY_BASIS_FIELD)) or _as_price(item.get("ask"))

    @staticmethod
    def _entry_half_spread_pct(item: dict) -> float | None:
        seen = _as_pct(item.get("entry_half_spread_pct"))
        if seen is not None:
            return seen
        return _half_spread_pct(item.get("entry_bid") or item.get("bid"),
                                item.get("entry_ask") or item.get("ask"))

    @classmethod
    def modeled_exit_cost_pct(cls, item: dict) -> float | None:
        """A deliberately conservative stand-in for the unobserved exit half-spread.

        This is NOT calibrated. Nothing here has been checked against a real exit book,
        which is exactly why every outcome that uses it is stamped
        ``cost_evidentiary: false`` and cannot support a positive verdict. Two floors,
        whichever is worse: the entry half-spread (a penny name's book does not reliably
        tighten by the time you want out) and half the modeled round trip. With neither
        available the exit cannot be bounded at all, and None means do not measure.
        """
        floors = []
        entry_half = cls._entry_half_spread_pct(item)
        if entry_half is not None:
            floors.append(entry_half)
        round_trip = _as_pct(item.get(SIGNAL_COST_FIELD))
        if round_trip is not None:
            floors.append(round_trip / 2.0)
        return max(floors) if floors else None

    @staticmethod
    def _closing_observation(item: dict, day: str,
                            schedule: dict | None = None) -> dict | None:
        """The book at the CLOSE of one exact session, or None.

        Every clause here is load-bearing:
          * the EXACT session - a prior session's book is another day's price, and
            allowing a one-session tolerance let a Jan-8 quote settle a Jan-9 horizon;
          * the session its EXCHANGE stamp states, never the stored label, so a stale
            book cannot be relabelled into the day it is wanted for;
          * inside the closing window - an opening or midday quote is not a close, and
            comparing one against a daily bar measures two different moments;
          * a venue feed and a two-sided book, re-checked here rather than trusted from
            whatever happened to reach the archive.
        """
        # The close comes from the independently stored exchange calendar, NEVER from
        # the quote. A quote carrying its own session_close_minute could certify itself:
        # an 11:00 book declaring an 11:00 close became an evidentiary closing quote.
        close_minute = (schedule or {}).get("close_minute")
        if close_minute is None:
            return None
        close_minute = int(close_minute)
        best, best_gap = None, None
        for obs in (item.get("quote_observations") or []):
            if not _quote_is_two_sided(obs):
                continue
            if not penny_quotes.is_execution_feed(obs.get("feed")):
                continue
            if penny_quotes.session_date(obs.get("at")) != day:
                continue
            if not penny_quotes.in_closing_window(obs.get("at"), close_minute):
                continue
            minute = penny_quotes.session_minute(obs.get("at"))
            gap = abs(minute - close_minute)
            if best_gap is None or gap < best_gap:
                best, best_gap = obs, gap
        return best

    @classmethod
    def _delayed_closing_observation(cls, item: dict, horizon_day: str,
                                     sessions: list[str],
                                     schedules=None) -> tuple[dict, str, int] | None:
        """The earliest closing book AFTER the horizon, with how late it is.

        A later book is a real observation of a different holding period. It is returned
        separately so it can be recorded under its own horizon key instead of quietly
        standing in for the one that was actually missed.
        """
        if horizon_day not in sessions:
            return None
        start = sessions.index(horizon_day)
        for offset in range(1, MAX_DELAYED_EXIT_SESSIONS + 1):
            if start + offset >= len(sessions):
                break
            day = sessions[start + offset]
            schedule = schedules(day) if schedules else None
            seen = cls._closing_observation(item, day, schedule)
            if seen and (schedule or {}).get("evidentiary"):
                return seen, day, offset
        return None

    @classmethod
    def exit_leg(cls, item: dict, horizon_day: str, end_close: float,
                 sessions: list[str], schedule: dict | None = None) -> dict | None:
        """The sell side, measured as its own leg.

        The old model observed neither side: it ran the last TRADE to a future close and
        subtracted the entry-time spread once, as though the whole round trip had been
        paid on the way in. Selling costs again, against a book nobody had looked at.
        """
        seen = cls._closing_observation(item, horizon_day, schedule)
        # A guessed schedule may still drive capture, but it cannot certify that a quote
        # was taken at the close - so an outcome resting on one is not evidence.
        if seen and (schedule or {}).get("evidentiary"):
            bid = _as_price(seen.get("bid"))
            half = _half_spread_pct(seen.get("bid"), seen.get("ask"))
            # Fail closed independently of whatever validation the record passed to get
            # here: a price with no computable spread is not an observed round trip.
            if bid is not None and half is not None:
                return {"exit_price": bid,
                        "exit_basis": "observed_bid",
                        "exit_cost_pct": half,
                        "exit_quote_at": seen.get("at"),
                        "exit_session": horizon_day,
                        "exit_quote_source": seen.get("source") or "",
                        "exit_quote_feed": seen.get("feed") or "",
                        "cost_evidentiary": True}
        bound = cls.modeled_exit_cost_pct(item)
        if bound is None:
            return None
        # Round the bound UP. A cost that exists to be conservative must never be
        # softened by display rounding, however slightly.
        bound = math.ceil(bound * 10_000) / 10_000
        return {"exit_price": end_close * (1 - bound / 100.0),
                "exit_basis": "modeled_bound_on_close",
                "exit_cost_pct": bound,
                "exit_quote_at": None,
                "exit_session": horizon_day,
                "exit_quote_source": "",
                "exit_quote_feed": "",
                "cost_evidentiary": False}

    @staticmethod
    def entry_is_evidentiary(item: dict) -> bool:
        """Whether the ENTRY book is one a purchase could have been made against.

        Yahoo's quote is not: this codebase's own comments call it "not an execution
        feed", and its freshness is derived from the last TRADE timestamp rather than
        the bid/ask, so it cannot establish what a buy would have paid. Validating only
        the exit leg let a Yahoo entry and an Alpaca exit combine into "evidence".

        Re-derived from the stored row rather than trusting the flag written at capture,
        so a row that was mis-stamped - or written by an older, laxer version - cannot
        carry a stale book into a verdict.
        """
        return PennyStockPaperBot._entry_quote_usable(
            {"bid": item.get("entry_bid"), "ask": item.get("entry_ask"),
             "feed": item.get("entry_quote_feed"),
             "at": item.get("entry_quote_captured_at")},
            str(item.get(SIGNAL_DAY_FIELD) or ""),
            item.get("entry_quote_received_at"))

    @staticmethod
    def outcome_is_evidentiary(outcome: dict) -> bool:
        """Whether one horizon outcome may support a positive verdict.

        BOTH legs, under the current schema. A round trip with one observed side is not
        an observed round trip. Schema 1 stamped a single ``cost_evidentiary`` from the
        exit alone, while accepting a prior session's book as a close and a crossed book
        as a quote - so nothing it produced qualifies here, however it was stamped.
        """
        return (isinstance(outcome, dict)
                and int(outcome.get("measurement_schema") or 0) == MEASUREMENT_SCHEMA_VERSION
                and bool(outcome.get("entry_cost_evidentiary"))
                and bool(outcome.get("exit_cost_evidentiary")))

    @staticmethod
    def completed_sessions(dates: list[str], now_ny: datetime | None = None) -> int:
        """How many leading daily bars are final and safe to resolve against.

        Today's bar is still forming until the close, and the provider needs a few
        minutes after it to publish. Resolving a horizon against a partial bar writes a
        "close" that was never the close - permanently, because outcomes are immutable.
        """
        now = now_ny or datetime.now(NY)
        today = now.strftime("%Y-%m-%d")
        settled = (now.hour * 60 + now.minute
                   >= penny_quotes.MARKET_CLOSE_MINUTE + DAILY_BAR_PUBLICATION_DELAY_MIN)
        keep = 0
        for day in dates:
            if day < today or (day == today and settled):
                keep += 1
            else:
                break
        return keep

    @property
    def archive_error(self) -> str:
        return "; ".join(x for x in (self.archive_integrity_error,
                                     self.archive_write_error,
                                     self.quarantine_error,
                                     self.outbox_error) if x)

    @staticmethod
    def valid_event(event) -> bool:
        """Whether a decoded record is a usable event.

        Syntactically valid JSON is not a valid event: a bare `[]` decoded fine, slipped
        past corruption handling, and then crashed the reducer after malformed evidence
        had already been written. Every record must be a dict with a known type, a
        non-empty id, and the fields that type requires.
        """
        if not isinstance(event, dict):
            return False
        kind = event.get("event")
        if not str(event.get("id") or "").strip():
            return False
        if kind == "signal":
            # a production signal always carries its session and engine version; without
            # them it cannot be placed in a basket or filtered by rule version
            return (str(event.get(SIGNAL_DAY_FIELD) or "").strip() != ""
                    and event.get("engine_version") is not None)
        if kind == "benchmark":
            return isinstance(event.get("legs"), dict)
        if kind == "outcome":
            return (str(event.get("horizon") or "").strip() != ""
                    and isinstance(event.get("outcome"), dict))
        if kind == "quote":
            # A crossed or locked book is not a quote, however well formed the record
            # is: bid 2.00 / ask 1.00 passed here and was taken as an exit price.
            if not _quote_is_two_sided(event):
                return False
            # The session a quote belongs to is the one its EXCHANGE stamp says, and the
            # label has to agree. A stale book relabelled as today is fabricated
            # evidence, and it also convinces the capture that today is already done.
            stamped = penny_quotes.session_date(event.get("at"))
            if stamped is None or stamped != str(event.get("session_day") or "").strip():
                return False
            # Feed identity is required, not decorative. An IEX book is one venue and a
            # SIP book is the consolidated tape; pooling them measures neither, and an
            # unrecognised feed cannot be separated from either later.
            return penny_quotes.is_known_feed(event.get("feed"))
        return False

    @classmethod
    def _fold_evidence_event(cls, store: dict, event: dict) -> bool:
        """The ONE reducer. Replay and live appends must not drift apart again.

        Returns whether the event was applied. Assumes valid_event() already passed.
        """
        sid = str(event.get("id") or "")
        kind = event.get("event")
        if kind == "signal":
            store.setdefault(sid, {k: v for k, v in event.items() if k != "event"})
            return True
        if sid not in store:
            return False
        row = store[sid]
        if kind == "benchmark":
            row.setdefault(SIGNAL_BENCHMARK_FIELD, {}).update(event.get("legs") or {})
            return True
        if kind == "outcome":
            row.setdefault("outcomes", {})[str(event.get("horizon"))] = (
                event.get("outcome") or {})
            row["resolved"] = bool(event.get("resolved"))
            return True
        if kind == "quote":
            seen = row.setdefault("quote_observations", [])
            stamp = str(event.get("at") or "")
            # Keyed by the exchange's own timestamp so a replayed or retried event
            # cannot turn one observation of the book into two.
            if not any(str(o.get("at") or "") == stamp for o in seen):
                seen.append({k: v for k, v in event.items()
                             if k not in ("event", "id")})
                seen.sort(key=lambda o: str(o.get("at") or ""))
            return True
        return False

    def _apply_evidence_event(self, event: dict) -> None:
        """Fold one appended event into the authoritative store, so a flushed outbox is
        visible without a restart."""
        if self.valid_event(event):
            self._fold_evidence_event(self._evidence, event)

    def _repair_torn_tail(self) -> bool:
        """Make the archive safely appendable without destroying a valid last event.

        A missing trailing newline does NOT imply torn JSON - a complete event can simply
        have been written without one, and truncating it silently deleted real evidence.
        The fragment is parsed and validated first: valid means keep it and add the
        newline, invalid means quarantine it. The rewrite goes through a temporary file
        and os.replace, because opening the live archive "wb" truncates it before a
        failed rewrite can finish.
        """
        try:
            if not os.path.exists(self.archive_path):
                return True
            with open(self.archive_path, "rb") as f:
                raw = f.read()
            if not raw or raw.endswith(b"\n"):
                return True
            cut = raw.rfind(b"\n") + 1
            fragment = raw[cut:]
            try:
                decoded = json.loads(fragment.decode("utf-8", "strict"))
                keep = self.valid_event(decoded)
            except (ValueError, UnicodeDecodeError):
                keep = False

            body = raw if keep else raw[:cut]
            if not keep:
                with open(self.archive_path + ".torn", "ab") as f:
                    f.write(fragment + b"\n")
            tmp = self.archive_path + ".repair.tmp"
            with open(tmp, "wb") as f:
                f.write(body if body.endswith(b"\n") else body + b"\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.archive_path)
            return True
        except OSError:
            return False

    def _flush_archive_outbox(self) -> bool:
        """Try to write a stranded queue to the archive itself.

        Persisting the queue to its own file made it durable but not recorded: the events
        still needed some later archive event to come along and carry them in. Recovery
        must not depend on that.
        """
        if not self._archive_outbox:
            return True
        if not self._outbox_path_writable():
            # The preserved outbox still holds these very events, so a drain could not
            # be recorded: the next start would salvage and append them a second time.
            self.archive_write_error = (
                f"{len(self._archive_outbox)} event(s) held; the outbox file is "
                f"quarantined in place and the queue cannot be retired")
            return False
        if not self._repair_torn_tail():
            return False
        pending = list(self._archive_outbox)
        unusable = self._unusable_pending(pending)
        if unusable:
            if not self._quarantine_events(unusable,
                                           "no preceding signal, or not a valid event"):
                # Nothing about the quarantine reached disk, so retiring these events
                # would erase them and the block together. Hold the whole queue.
                self.archive_write_error = (
                    f"{len(unusable)} unusable event(s) could not be quarantined; "
                    f"{len(pending)} event(s) held and nothing was appended")
                return False
            drop = {id(x) for x in unusable}
            pending = [x for x in pending if id(x) not in drop]
            self._archive_outbox = list(pending)
            if not pending:
                self._persist_outbox()
                return False
        try:
            os.makedirs(os.path.dirname(self.archive_path) or ".", exist_ok=True)
            with open(self.archive_path, "a", encoding="utf-8") as f:
                for row in pending:
                    f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            self.archive_write_error = (
                f"signal archive write failed: {type(e).__name__}; "
                f"{len(pending)} event(s) awaiting retry")
            return False
        for row in pending:
            self._apply_evidence_event(row)
        self._archive_outbox = []
        self._persist_outbox()
        self.archive_write_error = ""
        return True

    # ---------------- quarantine bookkeeping ----------------
    def _quarantine_artifacts(self) -> list[str]:
        """Every quarantined-evidence file on disk, marker or no marker."""
        return sorted(glob.glob(
            glob.escape(self.archive_path + ".outbox") + ".corrupt.*"))

    def _record_quarantine(self, entry: dict) -> bool:
        """Add one line to the marker as a whole, fsynced file.

        Appending in place could leave a half-written line behind a crash, and an
        unreadable marker is indistinguishable from an absent one. Returns whether it
        reached disk: the artifact scan is the independent second record, so a failed
        marker write no longer erases the block.
        """
        try:
            lines: list[str] = []
            if os.path.exists(self._quarantine_marker):
                with open(self._quarantine_marker, encoding="utf-8") as f:
                    lines = [x for x in f.read().splitlines() if x.strip()]
            lines.append(json.dumps(entry, default=str))
            tmp = self._quarantine_marker + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._quarantine_marker)
            return True
        except OSError:
            return False

    def _note_existing_quarantine(self) -> None:
        """Block on EITHER record of a past quarantine, independently.

        Startup used to consult only the marker. A failed marker write therefore made
        the whole quarantine disappear on the next run while the damaged evidence sat
        on disk untouched, and collection resumed as if nothing had happened.
        """
        artifacts = self._quarantine_artifacts()
        marked = os.path.exists(self._quarantine_marker)
        if not artifacts and not marked:
            return
        entries: list[str] = []
        if marked:
            try:
                with open(self._quarantine_marker, encoding="utf-8") as f:
                    entries = [x for x in f.read().splitlines() if x.strip()]
            except OSError:
                entries = []
        count = max(len(entries), len(artifacts), 1)
        self.quarantine_error = (
            f"{count} unresolved outbox quarantine(s); evidence is incomplete until "
            f"{os.path.basename(self._quarantine_marker)} and every "
            f"{os.path.basename(self.archive_path)}.outbox.corrupt.* are cleared")

    def _quarantine_events(self, events: list[dict], why: str) -> bool:
        """Set unusable queued events aside on disk - never on the archive - and block.

        The artifact is named like an outbox quarantine on purpose, so the startup scan
        finds it and the block survives a restart even if the marker cannot be written.

        Returns whether either record reached disk. With neither, dropping these events
        from the queue would erase the evidence AND the block together - the exact
        failure this quarantine exists to prevent - so the caller must keep holding them.
        """
        stamp = str(int(time.time() * 1000))
        target = f"{self.archive_path}.outbox.corrupt.{stamp}"
        written = False
        try:
            with open(target, "w", encoding="utf-8") as f:
                for row in events:
                    f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            written = True
        except OSError:
            pass
        marked = self._record_quarantine(
            {"at": stamp, "bad_events": len(events), "why": why,
             "quarantined_to": os.path.basename(target) if written else None})
        self.quarantine_error = (
            f"{len(events)} queued event(s) quarantined ({why}); "
            + (f"held in {os.path.basename(target)}" if written
               else "they could not be written to disk and are still queued")
            + " - evidence incomplete until resolved")
        return written or marked

    def _outbox_path_writable(self) -> bool:
        """Whether the outbox file may be written, retrying a failed quarantine move.

        A failed move leaves the damaged file as the ONLY copy of the corrupt bytes -
        and of the salvage still inside it. Writing the path would destroy both, so it
        stays blocked until the move finally succeeds.
        """
        if not self._outbox_write_blocked:
            return True
        path = self.archive_path + ".outbox"
        if not os.path.exists(path):
            self._outbox_write_blocked = False
            return True
        stamp = str(int(time.time() * 1000))
        target = f"{path}.corrupt.{stamp}"
        try:
            os.replace(path, target)
        except OSError:
            return False
        self._record_quarantine({"at": stamp, "bad_events": None,
                                 "why": "deferred quarantine move succeeded",
                                 "quarantined_to": os.path.basename(target)})
        self._outbox_write_blocked = False
        return True

    def _unusable_pending(self, pending: list[dict]) -> list[dict]:
        """The events in a batch that must not be appended.

        Shape is not enough. A correctly formed outcome whose signal was never recorded
        cannot be folded, so appending it writes an orphan that only the NEXT restart
        discovers. The batch is folded through the real reducer against a probe store
        first - keys only, so there is neither a deep copy of the evidence nor a second
        copy of the ordering rule to drift from it.
        """
        probe: dict[str, dict] = {sid: {} for sid in self._evidence}
        bad = []
        for event in pending:
            if not self.valid_event(event) or not self._fold_evidence_event(probe, event):
                bad.append(event)
        return bad

    def _persist_outbox(self) -> bool:
        """Write the pending queue atomically so an outage cannot lose it on restart.

        Returns whether the queue actually reached disk. Total disk failure cannot be
        made durable - but it must not be reported as durable either, which is exactly
        what swallowing this exception did.
        """
        path = self.archive_path + ".outbox"
        if not self._outbox_path_writable():
            self.outbox_error = (
                f"{len(self._archive_outbox)} event(s) held in memory only - the outbox "
                f"file still holds unquarantined corrupt evidence and must not be "
                f"overwritten")
            return False
        try:
            if not self._archive_outbox:
                if os.path.exists(path):
                    os.remove(path)
                self.outbox_error = ""
                return True
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for row in self._archive_outbox:
                    f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self.outbox_error = ""
            return True
        except OSError as e:
            self.outbox_error = (
                f"{len(self._archive_outbox)} event(s) held in memory only - "
                f"outbox could not be written ({type(e).__name__})")
            return False

    def _load_outbox(self) -> None:
        """Restore any queue left behind by a failed write in a previous run."""
        # An unresolved quarantine keeps blocking regardless of the current outbox, and
        # the marker is only one of its two independent records.
        self._note_existing_quarantine()
        path = self.archive_path + ".outbox"
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                lines = [x for x in f.read().splitlines() if x.strip()]
        except OSError as e:
            self.outbox_error = f"outbox unreadable ({type(e).__name__}); evidence may be lost"
            return
        restored, bad = [], 0
        for line in lines:
            try:
                decoded = json.loads(line)
            except ValueError:
                bad += 1
                continue
            # Parsing is not validity. A bare [] was queued as an event and later written
            # into the archive, corrupting the evidence it was meant to protect.
            if self.valid_event(decoded):
                restored.append(decoded)
            else:
                bad += 1
        self._archive_outbox = restored
        if bad:
            # A fixed ".corrupt" name overwrote any earlier quarantine, and the block
            # vanished on the next restart because nothing re-read it. Unique names plus
            # a marker file keep both the evidence and the block until resolved.
            stamp = str(int(time.time() * 1000))
            target = f"{path}.corrupt.{stamp}"
            moved = False
            try:
                os.replace(path, target)
                moved = True
            except OSError:
                pass
            self._record_quarantine({"at": stamp, "bad_events": bad,
                                     "why": "unusable record(s) in the outbox",
                                     "quarantined_to": os.path.basename(target)
                                     if moved else None})
            if moved:
                # Salvaged events are written back only AFTER the damaged file has
                # moved, so they land in a fresh outbox rather than being erased with it.
                self._persist_outbox()
            else:
                # The damaged file is still the only copy of the corrupt bytes AND of
                # the salvage inside it. _persist_outbox would have overwritten it with
                # the salvage alone - or removed it outright when nothing was salvaged -
                # so the reported "left in place" was false. Leave the path untouched
                # and hold the salvage in memory until the move succeeds.
                self._outbox_write_blocked = True
            self.quarantine_error = (
                f"outbox had {bad} unusable event(s); "
                + (f"quarantined to {os.path.basename(target)}" if moved
                   else "quarantine move FAILED, damaged file preserved in place and "
                        "held out of the archive")
                + " - evidence incomplete until resolved")
        elif restored:
            self.outbox_error = (f"{len(restored)} archived event(s) awaiting retry "
                                 f"from a previous run")

    def _archive_event(self, kind: str, payload: dict) -> bool:
        """Append one immutable event. The archive is the record; the log is a cache.

        Appending only signal creations made this useless as evidence: outcomes arrive
        later, so the archive held rows that were permanently unresolved and could
        restore nothing. Creations and every horizon outcome are both events now, keyed
        by the signal's own id, so a restart rebuilds the same statistics.
        """
        event = {"event": kind, **payload}
        pending = list(self._archive_outbox) + [event]
        # Validate BEFORE any file write. This path trusted its own callers, so a thin
        # internally generated record was appended, reported as a success, and only
        # found on the next restart - by which point the archive was already corrupt.
        unusable = self._unusable_pending(pending)
        if any(x is event for x in unusable):
            # Our own output is a bug, not evidence: fail closed and queue nothing.
            self.archive_write_error = (
                f"refused to archive an invalid {kind} event "
                f"(id {str(payload.get('id') or '')!r}): it is malformed or has no "
                f"preceding signal; nothing was written")
            return False
        # A quarantined-in-place outbox blocks the archive too, and the check belongs
        # BEFORE the tail repair and the append: while the damaged file is still the
        # only copy of the queue, nothing may touch the archive and nothing may be
        # retired from that queue. Hold the new event with the rest and say so.
        if not self._outbox_path_writable():
            self._archive_outbox = pending
            # sets the memory-only error; writes nothing while the path is blocked
            self._persist_outbox()
            # archive_write_error is deliberately left alone: a prior append failure is
            # still true, and clearing it here would report a write that never happened.
            return False
        if unusable:
            if not self._quarantine_events(unusable,
                                           "no preceding signal, or not a valid event"):
                # See _flush_archive_outbox: an unrecorded quarantine may not retire
                # anything, or the queue and the block vanish together.
                self._archive_outbox = pending
                self._persist_outbox()
                self.archive_write_error = (
                    f"{len(unusable)} unusable event(s) could not be quarantined; "
                    f"{len(pending)} event(s) held and nothing was appended")
                return False
            drop = {id(x) for x in unusable}
            pending = [x for x in pending if id(x) not in drop]
            self._archive_outbox = [x for x in self._archive_outbox
                                    if id(x) not in drop]
        if not self._repair_torn_tail():
            # Appending onto an unterminated fragment concatenates the next event into
            # it, so BOTH are unreadable on replay - the tear silently eats the next
            # valid write. Hold the queue instead of corrupting more evidence.
            self._archive_outbox = pending
            self._persist_outbox()
            self.archive_write_error = (
                f"archive tail is torn and could not be repaired; "
                f"{len(self._archive_outbox)} event(s) held")
            return False
        try:
            os.makedirs(os.path.dirname(self.archive_path) or ".", exist_ok=True)
            with open(self.archive_path, "a", encoding="utf-8") as f:
                for row in pending:
                    f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            for row in pending:
                self._apply_evidence_event(row)
            self._archive_outbox = []
            self._persist_outbox()
            self.archive_write_error = ""
            return True
        except OSError as e:
            # Hold it rather than drop it. A failed final-horizon event would otherwise
            # leave the pending queue marked resolved and be recorded nowhere. The queue
            # is written to disk so a restart mid-outage does not lose it, and it is
            # never truncated: discarding evidence to bound memory is the same bug in a
            # smaller coat.
            self._archive_outbox = pending
            self._persist_outbox()
            self.archive_write_error = (
                f"signal archive write failed: {type(e).__name__}; "
                f"{len(self._archive_outbox)} event(s) awaiting retry")
            return False

    def _archive_signal(self, row: dict) -> None:
        self._archive_event("signal", row)

    def _replay_archive(self) -> list[dict]:
        """Rebuild every signal from the event archive, newest session first.

        A crash can only ever tear the LAST line, so that alone is a tolerable loss. A
        malformed line anywhere earlier means the file has been corrupted or edited, and
        silently skipping it would quietly drop evidence - it sets archive_error, which
        blocks evidentiary verdicts, rather than being swallowed.
        """
        rows: dict[str, dict] = {}
        try:
            if not os.path.exists(self.archive_path):
                return []
            with open(self.archive_path, encoding="utf-8") as f:
                raw = f.read()
            lines = raw.splitlines()
            # A crash tears the last write mid-line, leaving no trailing newline. A
            # malformed record that IS newline-terminated was written whole and is
            # corruption, not a tear.
            tolerate_last = bool(raw) and not raw.endswith("\n")
        except OSError as e:
            self.archive_integrity_error = f"signal archive read failed: {type(e).__name__}"
            return []

        bad_before_end = 0
        orphans = 0
        for number, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                if not (tolerate_last and number == len(lines) - 1):
                    bad_before_end += 1
                continue
            if not self.valid_event(event):
                # decodes as JSON but is not an event: same severity as corrupt bytes
                if not (tolerate_last and number == len(lines) - 1):
                    bad_before_end += 1
                continue
            if not self._fold_evidence_event(rows, event):
                # A benchmark/outcome whose signal never appears cannot be ordered after
                # its creation, so the archive is missing or misordered events.
                orphans += 1
        # A clean full replay is the ONLY thing that may clear an integrity error.
        self.archive_integrity_error = ""
        if bad_before_end or orphans:
            parts = []
            if bad_before_end:
                parts.append(f"{bad_before_end} unreadable line(s) before the end")
            if orphans:
                parts.append(f"{orphans} orphan event(s) with no preceding signal")
            self.archive_integrity_error = (
                "signal archive corrupt: " + "; ".join(parts) + "; evidence is incomplete")
        return sorted(rows.values(),
                      key=lambda r: str(r.get(SIGNAL_DAY_FIELD) or ""), reverse=True)

    def evidence_rows(self) -> list[dict]:
        """The population analytics run on: the whole archive, never the UI cache.

        Replay returned every session while the next _retained_signals() call trimmed to
        180, so a statistic changed as soon as one more signal arrived. Analytics now
        read the archive and the trimmed list is only what the dashboard renders.
        """
        # Re-replaying here discarded the reconciliation done at load: an outcome that
        # reached the state cache but never reached the archive vanished from every
        # statistic after a restart. There is one store, and this is it.
        rows = list(self._evidence.values()) if self._evidence else list(self.signal_log)
        return [x for x in rows
                if x.get("engine_version") == SIGNAL_ENGINE_VERSION
                and x.get(SIGNAL_DAY_FIELD)
                and x.get(EVIDENCE_ROLE_FIELD, PRIMARY_EVIDENCE_ROLE)
                == PRIMARY_EVIDENCE_ROLE]

    def mechanical_evidence_rows(self) -> list[dict]:
        """All pre-AI eligible rows, including the non-trading AI control group.

        This is deliberately separate from ``evidence_rows``: adding a control row must
        never change the live strategy's forward mean, evidence clock, or gate.
        """
        rows = list(self._evidence.values()) if self._evidence else list(self.signal_log)
        return [x for x in rows
                if x.get("engine_version") == SIGNAL_ENGINE_VERSION
                and x.get(SIGNAL_DAY_FIELD)
                and x.get(EVIDENCE_ROLE_FIELD, PRIMARY_EVIDENCE_ROLE)
                in (PRIMARY_EVIDENCE_ROLE, CONTROL_EVIDENCE_ROLE)]

    def _proxy_cost_share(self, admitted_days: set[str]) -> float:
        """Share of admitted rows whose cost was an ADV proxy rather than a real quote.

        Scoped to the sessions that actually entered the mean, so it describes the
        sample being reported rather than every row ever logged.
        """
        rows = [x for x in self.evidence_rows()
                if str(x.get(SIGNAL_DAY_FIELD) or "") in admitted_days]
        if not rows:
            return 0.0
        return round(100 * sum(bool(x.get("cost_is_proxy")) for x in rows) / len(rows), 1)

    def _recent_sessions(self) -> list:
        """Recent IWM trading dates, cached. Empty if unavailable - callers fall back."""
        now = time.time()
        cached_at, cached = getattr(self, "_session_cache", (0.0, []))
        if cached and now - cached_at < 6 * 3600:
            return cached
        dates: list = []
        try:
            if research.yf is not None:
                hist = research.yf.Ticker("IWM").history(period="6mo", interval="1d")
                dates = [d.date() for d in hist.index]
        except Exception:
            dates = []
        self._session_cache = (now, dates)
        return dates

    def daily_baskets(self, horizon: str = "5") -> dict:
        """Equal-weight basket per session, admitting only wholly resolved sessions.

        A session is complete only when EVERY ticker logged that day has an outcome at
        the horizon. Admitting partly resolved days silently drops the missing names from
        the equal-weight basket, and the names that stay unresolved are exactly the ones
        that halted, delisted or stopped returning data - overwhelmingly the losers. One
        winner plus one halted name was scoring as a complete +10% day.

        That is survivorship bias, reproduced inside the forward tracker built to avoid
        it. Excluding a partial basket is necessary but NOT sufficient - dropping a
        matured unresolved day is itself biased, which is why such days are classified
        stale and fed to missing_outcome_bound() rather than discarded.
        """
        current = self.evidence_rows()
        by_day: dict[str, list[dict]] = {}
        for item in current:
            by_day.setdefault(str(item[SIGNAL_DAY_FIELD]), []).append(item)

        # Maturity in exchange sessions, not calendar days: a calendar approximation
        # calls a day stale across a long holiday weekend when its horizon has not
        # actually elapsed, manufacturing informative-missingness out of a closed market.
        today = datetime.now(NY).date()
        need = int(horizon) + HORIZON_GRACE_SESSIONS
        sessions = self._recent_sessions()
        fallback_days = math.ceil(need * 7 / 5) + 2

        def is_mature(day: str) -> bool:
            try:
                d = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                return True                     # unparseable dates are treated as mature
            if sessions:
                return sum(1 for s in sessions if s > d) >= need
            # no calendar: err toward "pending" rather than toward "stale"
            return (today - d).days >= fallback_days

        mature_after = need

        complete: list[dict] = []
        pending_days, stale_days = 0, 0
        pending_rows, stale_rows = 0, 0
        modeled_days, modeled_rows = 0, 0
        stale_detail: list[dict] = []
        for day in sorted(by_day):
            rows = by_day[day]
            outcomes = [(r.get("outcomes") or {}).get(str(horizon)) for r in rows]
            resolved = [o for o in outcomes
                        if isinstance(o, dict) and o.get("net_return_pct") is not None]
            missing = len(rows) - len(resolved)
            if missing:
                if not is_mature(day):
                    pending_days += 1
                    pending_rows += missing
                else:
                    # matured and still missing: this is the informative kind
                    stale_days += 1
                    stale_rows += missing
                    nets = [float(o["net_return_pct"]) for o in resolved]
                    exc = [float(o["net_excess_return_pct"]) for o in resolved
                           if o.get("net_excess_return_pct") is not None]
                    costs = [float(r.get(SIGNAL_COST_FIELD) or 0.0) for r in rows]
                    # Independent of whether the NAME resolved: every row carries IWM's
                    # own horizon return, so a halted ticker still has a benchmark leg.
                    benches = [float((r.get(SIGNAL_BENCHMARK_FIELD) or {})[str(horizon)])
                               for r in rows
                               if str(horizon) in (r.get(SIGNAL_BENCHMARK_FIELD) or {})]
                    if not benches:
                        benches = [float(o["benchmark_return_pct"]) for o in resolved
                                   if o.get("benchmark_return_pct") is not None]
                    stale_detail.append({
                        "day": day, "members": len(rows), "missing": missing,
                        "resolved_net_pct": (sum(nets) / len(nets)) if nets else None,
                        "resolved_excess_pct": (sum(exc) / len(exc)) if exc else None,
                        "cost_pct": (sum(costs) / len(costs)) if costs else 0.0,
                        # the benchmark is observable even when the name is not
                        "benchmark_pct": (sum(benches) / len(benches)) if benches else 0.0,
                    })
                continue
            nets = [float(o["net_return_pct"]) for o in resolved]
            excess = [o.get("net_excess_return_pct") for o in resolved]
            # An outcome whose exit leg was never observed rests on an uncalibrated
            # bound. It can be reported, but it cannot support a positive verdict.
            modeled = [o for o in resolved if not self.outcome_is_evidentiary(o)]
            if modeled:
                modeled_days += 1
                modeled_rows += len(modeled)
            complete.append({
                "day": day,
                "members": len(rows),
                "net_pct": sum(nets) / len(nets),
                # benchmarked only when EVERY member has a benchmark, for the same reason
                "excess_pct": (sum(float(e) for e in excess) / len(excess)
                               if all(e is not None for e in excess) else None),
                "cost_modeled_members": len(modeled),
                "cost_evidentiary": not modeled,
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
            # full set for computation; the API truncates for display only
            "stale_all": stale_detail,
            "stale_detail": stale_detail[:20],
            # kept for callers that only want "not complete"
            "incomplete_days": pending_days + stale_days,
            "unresolved_rows": pending_rows + stale_rows,
            "mature_after_sessions": mature_after,
            # Baskets resting on a modeled exit cost rather than an observed exit book.
            # Reportable, never sufficient for a positive verdict.
            "cost_modeled_days": modeled_days,
            "cost_modeled_rows": modeled_rows,
        }

    def missing_outcome_bound(self, horizon: str = "5") -> dict:
        """Mean if every matured-but-missing outcome were the worst case.

        Excluding stale days is not conservative. It shrinks the sample, but the days
        that fail to resolve are the ones holding halted or delisted names, so removing
        them removes losses and lifts the estimate. The honest question is whether the
        result survives assuming those names went to zero.
        """
        book = self.daily_baskets(horizon)
        series: list[tuple[str, float, float | None]] = []
        for basket in book["baskets"]:
            series.append((basket["day"], basket["net_pct"], basket["excess_pct"]))
        # Every stale day, not a truncated preview: the display cap belongs in the API.
        for day in book["stale_all"]:
            members = day["members"]
            if not members:
                continue
            resolved_n = members - day["missing"]
            # A halted name is not merely -100%: the round trip is still charged, so the
            # after-cost outcome is worse than a total loss.
            worst = MISSING_OUTCOME_ASSUMPTION_PCT - float(day.get("cost_pct") or 0.0)
            net_sum = (float(day["resolved_net_pct"]) * resolved_n
                       if day.get("resolved_net_pct") is not None else 0.0)
            net = (net_sum + worst * day["missing"]) / members
            # The benchmark for a missing name is observable independently of the name,
            # so the bounded excess is the same worst case measured against it.
            if day.get("resolved_excess_pct") is not None and resolved_n:
                ex_sum = float(day["resolved_excess_pct"]) * resolved_n
            else:
                ex_sum = 0.0
            bench = float(day.get("benchmark_pct") or 0.0)
            excess = (ex_sum + (worst - bench) * day["missing"]) / members
            series.append((day["day"], net, excess))
        if not series:
            return {"applicable": False, "reason": "no admitted or stale days"}

        series.sort(key=lambda row: row[0])            # HAC needs chronological order
        nets = [row[1] for row in series]
        excesses = [row[2] for row in series if row[2] is not None]
        mean, lo, hi = self._hac_mean_ci(nets)
        ex_mean, ex_lo, ex_hi = self._hac_mean_ci(excesses) if excesses else (0.0, 0.0, 0.0)
        return {
            "applicable": True,
            "assumed_missing_outcome_pct": MISSING_OUTCOME_ASSUMPTION_PCT,
            "assumption": "missing outcome = -100% minus the modelled round trip",
            "bounded_mean_net_pct": round(mean, 3),
            "bounded_net_hac_95_pct": [round(lo, 3), round(hi, 3)],
            "bounded_mean_excess_pct": round(ex_mean, 3) if excesses else None,
            "bounded_excess_hac_95_pct": ([round(ex_lo, 3), round(ex_hi, 3)]
                                          if excesses else None),
            "signal_days_included": len(series),
            "imputed_days": len(book["stale_all"]),
            "imputed_rows": book["stale_incomplete_rows"],
            # the same dependence-robust standard the verdict itself uses, applied to the
            # bounded series - a positive mean with an interval spanning zero is not a
            # result that survives the missing outcomes
            "clears_zero": bool(lo > 0 and (not excesses or ex_lo > 0)),
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

    def _combined_universe_rows(self) -> list[dict]:
        """Consolidated baseline, updated by any fresher IEX observations."""
        combined: dict[str, dict] = {}
        for feed in ("delayed_sip", "iex"):
            for row in self._universe_rows_by_feed.get(feed, []):
                symbol = str(row.get("ticker") or "").upper()
                if symbol:
                    combined[symbol] = dict(row)
        return list(combined.values())

    async def _refresh_universe_once(self, feed: str) -> None:
        """Run one exhaustive cheap pass and publish it atomically to deep scans."""
        started = time.time()
        self._universe_pass_in_progress = True
        try:
            rows, coverage, error = await penny_quotes.market_wide_penny_scan(
                research.MIN_PRICE, research.MAX_PRICE, force=True, feed=feed)
            if rows:
                self._universe_rows_by_feed[feed] = [dict(row) for row in rows]
                self._universe_rows = self._combined_universe_rows()
            previous = dict(self.universe_coverage or {})
            passes = int(previous.get("continuous_passes") or 0) + 1
            current = dict(coverage or {})
            current.update({
                "continuous_passes": passes,
                "continuous_target_sec": UNIVERSE_TARGET_CYCLE_SEC,
                "last_pass_started_at": started,
                "last_pass_feed": feed,
                "combined_penny_price_matches": len(self._universe_rows),
                "strategy_id": research.LIVE_STRATEGY_ID,
                "engine_version": SIGNAL_ENGINE_VERSION,
            })
            # A universe refresh must not erase the separately completed deep-stage
            # telemetry. Both loops publish into this object, so explicitly retain the
            # latest deep result until the next deep scan replaces it.
            for key in (
                "deep_score_cap", "deep_score_target", "deep_scored",
                "leaderboard_count", "last_deep_scan_completed_at",
                "yahoo_in_play_candidates", "market_wide_candidates",
                "fresh_sec_catalyst_symbols",
            ):
                if key in previous:
                    current[key] = previous[key]
            if feed == "delayed_sip" and current.get("last_completed_at"):
                self._last_delayed_universe_scan = float(current["last_completed_at"])
                current.update({
                    "full_tape_last_completed_at": current["last_completed_at"],
                    "full_tape_snapshots_returned": current.get("snapshots_returned", 0),
                    "full_tape_symbols_requested": current.get("symbols_requested", 0),
                })
            else:
                for key in (
                    "full_tape_last_completed_at", "full_tape_snapshots_returned",
                    "full_tape_symbols_requested",
                ):
                    if key in previous:
                        current[key] = previous[key]
            if error and not current.get("error"):
                current["error"] = str(error)
            self.universe_coverage = current
        except Exception as e:
            self.universe_coverage.update({
                "status": "FAILED",
                "error": f"continuous universe {type(e).__name__}: {str(e)[:120]}",
                "last_attempt_at": started,
                "continuous_target_sec": UNIVERSE_TARGET_CYCLE_SEC,
            })
        finally:
            self._universe_pass_in_progress = False
            self._universe_ready.set()

    async def _continuous_universe_loop(self):
        """Start immediately and keep sweeping every supported listed symbol."""
        while True:
            started = time.time()
            feed = (
                "delayed_sip"
                if self._last_delayed_universe_scan <= 0
                or started - self._last_delayed_universe_scan >= UNIVERSE_FULL_TAPE_SEC
                else "iex"
            )
            await self._refresh_universe_once(feed)
            elapsed = time.time() - started
            wait_for = max(0.0, UNIVERSE_TARGET_CYCLE_SEC - elapsed)
            self._next_universe_scan_at = time.time() + wait_for
            await asyncio.sleep(wait_for)

    async def _scan_locked(self, mode: str = "full"):
        """Consume the nonstop listed-asset sweep -> rank candidates -> AI review.

        A separate task gives every active listed/tradable symbol the cheap first-stage
        snapshot every 30 seconds. Only interleaved SEC/mover/volume/spread candidates
        get expensive Yahoo fundamentals and mechanical scoring, and only mechanically eligible names
        get an AI call. The separate coverage telemetry keeps those stages explicit.
        """
        mode = "pulse" if mode == "pulse" else "full"
        started = time.time()
        self.last_scan_started = started
        self.last_scan_mode = mode
        self.status = f"{mode} screening the market..."
        try:
            pool = SCREEN_POOL if mode == "full" else PULSE_SCREEN_POOL
            if mode == "full":
                # The exhaustive network pass has its own 30-second loop. Deep scans
                # consume the newest completed pass instead of waiting 15 minutes and
                # then blocking on the same network work again.
                if not self._universe_rows:
                    self.status = "waiting for the first immediate all-symbol pass..."
                    try:
                        await asyncio.wait_for(self._universe_ready.wait(), timeout=45)
                    except asyncio.TimeoutError:
                        pass
                market_rows = list(self._universe_rows)
                coverage = dict(self.universe_coverage or {})
                market_error = str(coverage.get("error") or "")
                yahoo_result, catalyst_result = await asyncio.gather(
                    asyncio.to_thread(research.screen, pool),
                    asyncio.to_thread(
                        research.sec_edgar.current_8k_tickers,
                        research.FRESH_NEWS_HOURS),
                    return_exceptions=True,
                )
                yahoo_fresh = ([] if isinstance(yahoo_result, Exception)
                               else list(yahoo_result or []))
                catalyst_symbols = ([] if isinstance(catalyst_result, Exception)
                                    else list(catalyst_result or []))
                market_fresh = penny_quotes.select_market_candidates(
                    market_rows, pool, catalysts=catalyst_symbols)
                fresh = self._interleave_candidates(
                    yahoo_fresh, market_fresh, limit=pool)
                coverage = dict(coverage or {})
                coverage.update({
                    "deep_score_cap": pool,
                    "deep_score_target": len(fresh),
                    "yahoo_in_play_candidates": len(yahoo_fresh),
                    "market_wide_candidates": len(market_fresh),
                    "fresh_sec_catalyst_symbols": len(catalyst_symbols),
                    "strategy_id": research.LIVE_STRATEGY_ID,
                    "engine_version": SIGNAL_ENGINE_VERSION,
                })
                if isinstance(yahoo_result, Exception):
                    yahoo_error = f"Yahoo discovery {type(yahoo_result).__name__}: {str(yahoo_result)[:90]}"
                    coverage["error"] = "; ".join(
                        value for value in (coverage.get("error"), yahoo_error) if value)
                    if not market_rows:
                        raise RuntimeError(yahoo_error)
                if market_error and not yahoo_fresh:
                    raise RuntimeError(str(market_error))
                self.universe_coverage = coverage
            else:
                yahoo_fresh = await asyncio.to_thread(research.screen, pool)
                # Keep confirming prior candidates while also consuming the newest
                # exhaustive-market snapshot. Previously the frequent pulse ignored
                # that snapshot and only revisited Yahoo's mover lists.
                syms = self._pulse_candidates(yahoo_fresh, pool)
            if mode == "full":
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
        if mode == "full":
            self.universe_coverage.update({
                "deep_scored": len(scored),
                "leaderboard_count": len(top),
                "last_deep_scan_completed_at": time.time(),
            })

        # ---- 2) AI deep-dive on the best few ----
        board = []
        reviews = 0
        for rank, (comp, d, r) in enumerate(top, start=1):
            ai, ai_err, ai_model = None, "", ""
            rejected = self.hard_reject(d)
            eligible = research.mechanical_setup(d, r)
            # An empty AI column looked identical whether the model was broken, out of
            # quota, or simply never consulted. It is almost always the last of those -
            # the AI only reviews mechanically eligible setups - so say which.
            if rejected:
                ai_skip = f"not reviewed: {rejected}"
            elif not eligible:
                ai_skip = ("not reviewed: no mechanical setup yet (needs the dated "
                           "catalyst, confirmation and trusted quote first)")
            elif reviews >= AI_DEEP_DIVE:
                ai_skip = (f"not reviewed: this scan already used its {AI_DEEP_DIVE} "
                           f"AI reviews (per scan, not per day)")
            else:
                ai_skip = ""
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
                # Kept with the signal so the ADV proxy can later be scored against the
                # book that was actually observed, rather than re-derived from data that
                # has since moved.
                "dollar_volume": round(d.avg_volume * d.price, 2),
                "adv_proxy_pct": round(research.adv_spread_proxy(d), 4),
                # quote_type is the SECURITY type ("EQUITY"), not the data source. It
                # was recorded as the provenance of the quote, which made every entry
                # book claim to have come from a feed called EQUITY.
                "quote_source": "yfinance",
                "quote_feed": "yfinance",
                "quote_instrument_type": d.quote_type,
                "volume_surge": round(d.volume_surge, 2),
                "composite": r["composite"], "hype": r["hype"],
                "technical": r["technical"], "catalyst": r["catalyst"],
                "quality": r["quality"], "tradeability": r["tradeability"],
                "hype_why": r["hype_why"], "quality_why": r["quality_why"],
                "technical_why": r["technical_why"], "catalyst_why": r["catalyst_why"],
                "trade_why": r["trade_why"], "data_completeness": d.data_completeness,
                "signal": sig, "ai": ai, "ai_error": ai_err, "ai_model": ai_model,
                "ai_skip_reason": ai_skip,
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
        if mode == "full":
            self.status = (
                f"market-wide pass complete: "
                f"{int(self.universe_coverage.get('symbols_requested') or 0):,} listed requested, "
                f"{int(self.universe_coverage.get('penny_price_matches') or 0):,} penny-price matches, "
                f"{len(scored)} deep-scored"
            )
        else:
            self.status = f"pulse scan complete: {len(scored)} deep-scored"
        # The entry book has to be taken at confirmation; it cannot be fetched later.
        await self._capture_entry_quotes(board)
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

    @staticmethod
    def _entry_quote_usable(venue: dict, signal_day: str, received_at: str) -> bool:
        """Whether a venue book describes what a purchase would have paid, right then.

        A regular-session CLOCK TIME was the only test, so a book from a previous
        session at 10:15 passed and became an evidentiary entry. All four conditions
        matter: a real two-sided venue book, stamped for THIS signal's session, inside
        the regular session, and actually fresh at the moment we received it - neither
        stale nor stamped in the future.
        """
        if not _quote_is_two_sided(venue):
            return False
        if not penny_quotes.is_execution_feed(venue.get("feed")):
            return False
        stamp = venue.get("at")
        if penny_quotes.session_date(stamp) != str(signal_day or ""):
            return False
        if not penny_quotes.in_regular_session(stamp):
            return False
        age = penny_quotes.age_seconds(stamp, received_at)
        if age is None:
            return False
        return -ENTRY_QUOTE_CLOCK_SKEW_SEC <= age <= ENTRY_QUOTE_MAX_AGE_SEC

    @staticmethod
    def _entry_book(b: dict, now: float) -> dict:
        """The entry side of the measurement, preferring a venue book over Yahoo's.

        Yahoo's quote establishes the ADMISSION decision and that is unchanged. It
        cannot establish what a purchase would have PAID: the codebase's own comments
        call it "not an execution feed", and its freshness is derived from the last
        trade timestamp, not the bid/ask. When Alpaca has given us a real book at
        confirmation the row is measured against it; otherwise the row is still
        recorded, it is simply not evidence.
        """
        venue = b.get("entry_quote") or {}
        # Local receipt time, stored alongside the exchange stamp. Freshness is the gap
        # between the two; with only one of them there is nothing to measure it against.
        received_at = (venue.get("received_at")
                       or datetime.fromtimestamp(now, timezone.utc).isoformat())
        signal_day = datetime.fromtimestamp(now, NY).strftime("%Y-%m-%d")
        usable = PennyStockPaperBot._entry_quote_usable(venue, signal_day, received_at)
        bid = venue.get("bid") if usable else b.get("bid")
        ask = venue.get("ask") if usable else b.get("ask")
        return {
            "entry_bid": _as_price(bid),
            "entry_ask": _as_price(ask),
            "entry_quote_mid": _quote_mid(bid, ask),
            "entry_half_spread_pct": _half_spread_pct(bid, ask),
            "entry_quote_source": (venue.get("source") or "alpaca") if usable else "yfinance",
            "entry_quote_feed": (venue.get("feed") or "") if usable else "yfinance",
            # the exchange's own stamp, never our clock
            "entry_quote_captured_at": (
                venue.get("at") if usable
                else datetime.fromtimestamp(now, timezone.utc).isoformat()),
            "entry_quote_received_at": received_at,
            "entry_quote_age_sec": (penny_quotes.age_seconds(venue.get("at"), received_at)
                                    if usable else None),
            "entry_quote_is_consolidated": bool(venue.get("is_consolidated")) if usable else False,
            "entry_quote_age_min": b.get("quote_age_min"),
            "entry_cost_evidentiary": usable,
        }

    async def _capture_entry_quotes(self, board) -> None:
        """Attach a venue book to every name about to be recorded as a signal.

        Runs at confirmation, because the entry book cannot be fetched afterwards any
        more than the exit one can. Names with no venue book are recorded anyway - with
        entry_cost_evidentiary false, which keeps them out of a positive verdict rather
        than out of the archive.
        """
        wanted = [b for b in board
                  if b.get("quote_reliable")
                  and str(b.get("market_state") or "").upper() == "REGULAR"
                  and not b.get("spread_estimated")
                  and b.get("ticker")]
        if not wanted or not penny_quotes.configured():
            return
        quotes, error = await penny_quotes.latest_quotes([b["ticker"] for b in wanted])
        received_at = datetime.now(timezone.utc).isoformat()
        for b in wanted:
            seen = quotes.get(str(b["ticker"]).upper())
            if seen:
                # Stamp receipt here, next to the fetch. Freshness is the gap between
                # the exchange's time and ours, and it cannot be reconstructed later.
                b["entry_quote"] = {**seen, "received_at": received_at}
        if error:
            self.quote_capture_error = f"entry quote capture: {error}"

    def _record_signals(self, board):
        """Log the whole confirmed pre-AI opportunity set.

        Approved rows remain the only live-rule evidence.  AI WATCH/AVOID rows are a
        non-trading control measured with the exact same entry, exit and archive code.
        Otherwise the model selects the sample and its rejected counterfactual vanishes,
        making its incremental value impossible to test.
        """
        now = time.time()
        signal_day = datetime.fromtimestamp(now, NY).strftime("%Y-%m-%d")
        for b in board:
            act = b["signal"]["action"]
            candidate_action = b["signal"].get("candidate_action", act)
            mechanical_action = (b["signal"].get("mechanical_action")
                                 or candidate_action)
            confirmed = bool((b.get("confirmation") or {}).get("confirmed"))
            executable_snapshot = bool(
                b.get("quote_reliable")
                and str(b.get("market_state") or "").upper() == "REGULAR"
                and not b.get("spread_estimated")
            )
            if mechanical_action in ("BUY", "STRONG BUY") and confirmed and executable_snapshot:
                # One observation per ticker/session. Repeated adaptive scans are
                # correlated duplicates, not independent evidence of accuracy.
                if any(x.get("ticker") == b["ticker"] and x.get(SIGNAL_DAY_FIELD) == signal_day
                       for x in self.signal_log):
                    continue
                ai = b.get("ai") or {}
                verdict = str(ai.get("verdict") or "").upper()
                # signal_from() is the authoritative pre-policy selector. Repeating
                # the verdict rule here would create two AI gates that could drift.
                ai_approved = candidate_action in ("BUY", "STRONG BUY")
                ai_selection = (
                    "approved" if ai_approved
                    else "rejected" if verdict in ("WATCH", "AVOID")
                    else "unavailable"
                )
                evidence_role = (PRIMARY_EVIDENCE_ROLE if ai_approved
                                 else CONTROL_EVIDENCE_ROLE)
                self.signal_log = self._retained_signals()
                self.signal_log.insert(0, {
                    "id": uuid.uuid4().hex[:10], "t": now, SIGNAL_DAY_FIELD: signal_day,
                    "signal_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "engine_version": SIGNAL_ENGINE_VERSION,
                    "strategy_id": research.LIVE_STRATEGY_ID,
                    "ticker": b["ticker"], "action": act,
                    "candidate_action": candidate_action,
                    "mechanical_action": mechanical_action,
                    EVIDENCE_ROLE_FIELD: evidence_role,
                    AI_SELECTION_FIELD: ai_selection,
                    "ai_policy_version": AI_DECISION_POLICY_VERSION,
                    "ai_policy_id": ai_decision_policy_id(),
                    "confirmed": True,
                    "confirmation_observations": (b.get("confirmation") or {}).get("observations"),
                    "executed": act in ("BUY", "STRONG BUY"),
                    "rank": b["rank"], "price": b["price"],
                    # The measured entry basis, stored apart from every display field.
                    # "price" and "decision_mid" both used to hold Yahoo's last TRADE,
                    # and the outcome math then treated that as the purchase price - so
                    # half the real cost was never charged and "mid" was not a mid.
                    "measurement_schema": MEASUREMENT_SCHEMA_VERSION,
                    "entry_last_trade": _as_price(b.get("price")),
                    **self._entry_book(b, now),
                    "dollar_volume": b.get("dollar_volume"),
                    "adv_proxy_pct": b.get("adv_proxy_pct"),
                    "decision_mid": _quote_mid(b.get("bid"), b.get("ask")),
                    "bid": b.get("bid"), "ask": b.get("ask"),
                    "quote_age_min": b.get("quote_age_min"),
                    "market_state": b.get("market_state"),
                    "quote_reliable": True,
                    SIGNAL_COST_FIELD: b.get("spread_pct"),
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
                    "ai_verdict": ai.get("verdict"),
                    "ai_conviction": ai.get("conviction"),
                    # Preserve the ordinal signal for later calibration. It does not
                    # affect the live rule, but discarding it would force any future
                    # score audit to start collecting from zero.
                    "ai_score": ai.get("score"),
                    "outcomes": {}, "resolved": False,
                })
                self._evidence[str(self.signal_log[0].get("id"))] = self.signal_log[0]
                self._archive_signal(self.signal_log[0])
        # Session-based, matching what _save persists, so analytics cannot change across
        # a restart. The append-only archive keeps everything regardless.
        self.signal_log = self._retained_signals()

    def _record_delayed_exit(self, item: dict, outcomes: dict, horizon_key: str,
                             seen: dict, exit_day: str, offset: int,
                             entry: float, entry_mid: float,
                             entry_evidentiary: bool) -> None:
        """Store a later session's observed exit as its OWN horizon.

        The horizon whose closing book was missed stays on the modeled bound. This is a
        separate measurement of a longer hold, keyed so ``daily_baskets(horizon)`` - which
        looks up str(horizon) exactly - can never pick it up as the original.
        """
        key = f"{horizon_key}{DELAYED_EXIT_SUFFIX}"
        if key in outcomes:
            return
        bid = _as_price(seen.get("bid"))
        half = _half_spread_pct(seen.get("bid"), seen.get("ask"))
        if bid is None or half is None:
            return
        outcomes[key] = {
            "return_pct": round((bid / entry - 1) * 100, 2),
            "net_return_pct": round((bid / entry - 1) * 100, 2),
            "gross_return_pct": round((bid / entry_mid - 1) * 100, 2),
            "measurement_schema": MEASUREMENT_SCHEMA_VERSION,
            "horizon_basis": "delayed_exit",
            "measures_horizon_sessions": f"{horizon_key}+{offset}",
            "excluded_from_basket": f"the {horizon_key}-session basket; this is a "
                                    f"{offset}-session-longer hold",
            "entry_basis": "ask", "entry_price": round(entry, 6),
            "exit_basis": "observed_bid", "exit_price": round(bid, 6),
            "exit_cost_pct": round(half, 4),
            "exit_session": exit_day,
            "exit_delay_sessions": offset,
            "exit_quote_at": seen.get("at"),
            "exit_quote_feed": seen.get("feed") or "",
            "entry_quote_feed": item.get("entry_quote_feed") or "",
            "entry_cost_evidentiary": entry_evidentiary,
            "exit_cost_evidentiary": True,
            "cost_evidentiary": bool(entry_evidentiary),
        }

    @staticmethod
    def _within_tracking_window(row: dict, today: str) -> bool:
        """Whether a signal's horizons could still be open.

        Calendar-generous on purpose: one wasted quote request costs nothing, while a
        missed session loses that exit book permanently - it cannot be fetched later.
        """
        day = str(row.get(SIGNAL_DAY_FIELD) or "")
        try:
            started = datetime.strptime(day, "%Y-%m-%d").date()
            now = datetime.strptime(today, "%Y-%m-%d").date()
        except ValueError:
            return False
        return 0 <= (now - started).days <= max(SIGNAL_HORIZONS) * 3 + 7

    # ---------------- session calendar ----------------
    def _load_session_calendar(self) -> None:
        """Load the independently stored record of when each session ended.

        Kept apart from the quotes on purpose. A quote carried its own
        session_close_minute and was believed, so a book stamped 11:00 could declare
        that the exchange had closed at 11:00 and certify itself as the close.
        """
        try:
            if os.path.exists(self._calendar_path):
                with open(self._calendar_path, encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    self._session_calendar = stored
        except (OSError, ValueError) as e:
            self.calendar_error = (
                f"session calendar unreadable ({type(e).__name__}); every schedule "
                f"falls back and stays non-evidentiary")

    def _persist_session_calendar(self) -> bool:
        try:
            tmp = self._calendar_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._session_calendar, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._calendar_path)
            return True
        except OSError as e:
            self.calendar_error = f"session calendar could not be written ({type(e).__name__})"
            return False

    async def refresh_session_calendar(self, force: bool = False) -> None:
        """Fetch real session times once a day and freeze them by date.

        Frozen deliberately: a schedule already recorded for a date is never replaced,
        so a refresh after the close cannot overwrite today's 13:00 with tomorrow's
        16:00 - which is exactly what reading a live status payload did.
        """
        today = datetime.now(NY).date()
        if not force and self._session_calendar.get("fetched_on") == today.isoformat():
            return
        start = today - timedelta(days=45)
        end = today + timedelta(days=10)
        sessions, error = await penny_quotes.fetch_calendar(start, end)
        if error or not sessions:
            self.calendar_error = (
                f"session calendar unavailable ({error or 'empty response'}); "
                f"schedules fall back and stay non-evidentiary")
            return
        known = dict(self._session_calendar.get("sessions") or {})
        for day, times in sessions.items():
            known.setdefault(day, times)          # freeze: never overwrite a past date
        covered = self._session_calendar.get("covered") or {}
        self._session_calendar = {
            "sessions": known,
            "covered": {"from": min(covered.get("from", start.isoformat()),
                                    start.isoformat()),
                        "to": max(covered.get("to", end.isoformat()), end.isoformat())},
            "fetched_on": today.isoformat(),
            "source": "alpaca",
        }
        self.calendar_error = ""
        self._persist_session_calendar()

    def session_schedule(self, day: str) -> dict:
        """When that session ended, and whether we actually know.

        ``evidentiary`` is false whenever the answer came from a fallback rather than the
        exchange calendar. A guessed schedule may still drive capture - better to try -
        but it must never let an outcome into a positive verdict.
        """
        sessions = self._session_calendar.get("sessions") or {}
        covered = self._session_calendar.get("covered") or {}
        if day in sessions:
            return {"close_minute": int(sessions[day]["close_minute"]),
                    "source": "alpaca", "evidentiary": True, "is_trading_day": True}
        if covered.get("from") and covered["from"] <= day <= covered.get("to", ""):
            # inside a range the exchange calendar answered for, and absent from it
            return {"close_minute": None, "source": "alpaca-holiday",
                    "evidentiary": True, "is_trading_day": False}
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return {"close_minute": None, "source": "unknown",
                    "evidentiary": False, "is_trading_day": False}
        try:
            payload = research.us_market_status_payload()
        except Exception:
            payload = {}
        minute, source = penny_quotes.scheduled_close_minute(target, payload=payload)
        return {"close_minute": minute, "source": f"fallback:{source}",
                "evidentiary": False, "is_trading_day": True}

    @classmethod
    def _has_closing_quote(cls, row: dict, session_day: str, schedule: dict) -> bool:
        """Exactly the test the exit leg applies - not a looser one.

        A weaker check here meant capture believed a crossed, non-venue book was the
        session's close and stopped trying, while the exit leg correctly refused it. The
        result was no closing book at all.
        """
        return cls._closing_observation(row, session_day, schedule) is not None

    def closing_capture_due(self, now: float, now_ny: datetime | None = None,
                            close_minute: int | None = None,
                            schedule: dict | None = None) -> bool:
        """Whether a closing-book capture should run right now.

        Capture used to ride the hourly outcome timer, so a pass at 15:54 and the next
        at 16:54 skipped the 15:55-16:05 window completely - and a closing book missed
        is missed permanently. This runs on its own clock: from a quarter-hour before
        the real close, every CLOSING_CAPTURE_RETRY_SEC, until every tracked name has a
        valid closing quote. Outcome calculation stays on its own slower schedule.
        """
        when = now_ny or datetime.now(NY)
        session_day = when.strftime("%Y-%m-%d")
        if schedule is None:
            schedule = (self.session_schedule(session_day) if close_minute is None
                        else {"close_minute": close_minute, "evidentiary": True,
                              "is_trading_day": True, "source": "given"})
        # A date the exchange calendar does not list is a holiday. Capture must not run
        # against a dark market and then report that nothing had a two-sided book.
        if not schedule.get("is_trading_day") or schedule.get("close_minute") is None:
            return False
        close_minute = int(schedule["close_minute"])
        _low, high = penny_quotes.closing_window(close_minute)
        # The lead runs from the CLOSE, not from the start of the window - subtracting
        # both made a "15 minute" lead begin 20 minutes early.
        if not (close_minute - CLOSING_CAPTURE_LEAD_MIN <= minute_of(when) <= high):
            return False
        if now - self._last_closing_capture < CLOSING_CAPTURE_RETRY_SEC:
            return False
        return any(
            not row.get("resolved") and row.get("ticker")
            and self._within_tracking_window(row, session_day)
            and not self._has_closing_quote(row, session_day, schedule)
            for row in self._evidence.values())

    async def _capture_exit_quotes(self) -> None:
        """Record a timestamped book for every tracked signal, once per session.

        Every tracked name, not only those still on the top-20 board. A signal that fell
        off the board still has horizons to measure, and the names that stop ranking are
        exactly the ones carrying the losses - observing only the survivors is how a
        strategy measures itself into looking good.

        Without this the exit leg is unobservable and every outcome falls back to the
        uncalibrated bound, which blocks a positive verdict by design.
        """
        if not penny_quotes.configured():
            self.quote_capture_error = (
                "no exit quotes captured: Alpaca is not configured, so every exit leg "
                "falls back to the modeled bound and stays non-evidentiary")
            return
        now_ny = datetime.now(NY)
        session_day = now_ny.strftime("%Y-%m-%d")
        schedule = self.session_schedule(session_day)
        if not schedule.get("is_trading_day"):
            self.quote_capture_error = (
                f"{session_day} is not a trading session ({schedule['source']}); "
                f"no closing book exists to capture")
            return
        tracked: dict[str, list[dict]] = {}
        for row in self._evidence.values():
            if row.get("resolved") or not row.get("ticker"):
                continue
            if not self._within_tracking_window(row, session_day):
                continue
            # Keep capturing until the CLOSING book exists. Stopping at the first quote
            # of the day left an opening snapshot as the session's only observation.
            if self._has_closing_quote(row, session_day, schedule):
                continue
            tracked.setdefault(str(row["ticker"]).upper(), []).append(row)
        if not tracked:
            self.quote_capture_error = ""
            return
        quotes, error = await penny_quotes.latest_quotes(list(tracked))
        received_at = datetime.now(timezone.utc).isoformat()
        rejected: list[str] = []
        for symbol, observation in quotes.items():
            # The session is whatever the EXCHANGE stamp says. Stamping the wall clock
            # onto a stale book both fabricates the observation and convinces the next
            # pass that today has already been captured.
            stamped = penny_quotes.session_date(observation.get("at"))
            if stamped is None:
                rejected.append(f"{symbol}: unparseable quote timestamp")
                continue
            if stamped != session_day:
                rejected.append(f"{symbol}: book stamped {stamped}, not {session_day}")
                continue
            if not _quote_is_two_sided(observation):
                rejected.append(f"{symbol}: not a two-sided book")
                continue
            for row in tracked.get(symbol, []):
                if not row.get("id"):
                    continue
                self._archive_event("quote", {
                    "id": row["id"], "session_day": stamped,
                    # What we BELIEVED the close was at capture time. Provenance only -
                    # the exit leg resolves the real close from the stored exchange
                    # calendar, because a quote that certifies its own closing time can
                    # certify itself as the close.
                    "believed_close_minute": schedule.get("close_minute"),
                    "believed_close_source": schedule.get("source"),
                    "received_at": received_at,
                    **observation})
        missing = [s for s in tracked if s not in quotes]
        self.quote_capture_error = "; ".join(x for x in (
            error,
            (f"{len(missing)} tracked name(s) had no two-sided book this session"
             if missing else ""),
            ("; ".join(rejected[:5]) if rejected else "")) if x)

    def adv_proxy_audit(self, min_per_bucket: int = 20) -> dict:
        """Score the ADV spread proxy against the books observed after each signal.

        The proxy decides which names look tradeable, so an unaudited proxy is an
        unaudited admission rule. Reports median bias and the p90/p95 UNDERSTATEMENT -
        the direction that admits a name at a cost nobody could have traded - split by
        price, dollar volume, time of session and feed.
        """
        records = []
        for row in self._evidence.values():
            proxy = _as_pct(row.get("adv_proxy_pct"))
            if proxy is None:
                continue
            for obs in (row.get("quote_observations") or []):
                observed = _as_pct(obs.get("spread_pct"))
                if observed is None:
                    continue
                records.append({
                    "ticker": row.get("ticker"), "proxy_pct": proxy,
                    "observed_pct": observed,
                    "price": (_as_price(row.get("entry_last_trade"))
                              or _as_price(row.get("price"))),
                    "dollar_volume": row.get("dollar_volume"),
                    "at": obs.get("at"), "feed": obs.get("feed"),
                })
        return penny_quotes.adv_proxy_audit(records, min_per_bucket=min_per_bucket)

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
                # Today's bar is still forming; resolving against it is permanent.
                settled = self.completed_sessions(
                    [x.date().isoformat() for x in benchmark.index])
                benchmark = benchmark.iloc[:settled]
        except Exception:
            benchmark = None

        # IWM's horizon return does not depend on the stock, so record it before any
        # per-ticker fetch can fail. Previously a name with no history returned early and
        # left the benchmark leg at zero, which collapsed the bounded excess onto the
        # bounded net and made the two indistinguishable.
        if benchmark is not None and not benchmark.empty:
            bench_dates = [x.date().isoformat() for x in benchmark.index]
            bench_close = [float(x) for x in benchmark["Close"]]
            for item in pending:
                legs = item.setdefault(SIGNAL_BENCHMARK_FIELD, {})
                changed = False
                for horizon in SIGNAL_HORIZONS:
                    key = str(horizon)
                    if key in legs:
                        continue
                    value = self._benchmark_leg(item, horizon, bench_dates, bench_close)
                    if value is not None:
                        legs[key] = value
                        changed = True
                if changed:
                    # persist it: computed only in memory, it vanished on an
                    # archive-only restart and took the excess leg with it
                    self._archive_event("benchmark", {"id": item.get("id"),
                                                      "legs": dict(legs)})

        for ticker, items in by_ticker.items():
            try:
                hist = await asyncio.to_thread(fetch_history, ticker)
                if hist is None or hist.empty:
                    continue
                hist = hist.dropna(subset=["Close"])
                # Only completed sessions may resolve a horizon. The current bar moves
                # until the close, and an outcome written from it is never revisited.
                settled = self.completed_sessions(
                    [x.date().isoformat() for x in hist.index])
                hist = hist.iloc[:settled]
                if hist.empty:
                    continue
                dates = [x.date().isoformat() for x in hist.index]
                for item in items:
                    indices = [i for i, day in enumerate(dates)
                               if day > item[SIGNAL_DAY_FIELD]]
                    outcomes = item.setdefault("outcomes", {})
                    # A long is bought at the ask. Measuring from item["price"] - the
                    # last trade - charged none of the entry half-spread and then took
                    # the entry spread off the far end as if that were the round trip.
                    entry = self.entry_basis(item)
                    if entry is None:
                        item["measurement_blocked"] = (
                            "no entry ask recorded; the executable entry price is "
                            "unknown and must not be guessed from the last trade")
                        continue
                    item.pop("measurement_blocked", None)
                    entry_evidentiary = self.entry_is_evidentiary(item)
                    entry_mid = (_quote_mid(item.get("entry_bid") or item.get("bid"),
                                            item.get("entry_ask") or item.get("ask"))
                                 or _as_price(item.get("entry_last_trade"))
                                 or _as_price(item.get("price")) or entry)
                    for horizon in SIGNAL_HORIZONS:
                        key = str(horizon)
                        if key in outcomes or len(indices) < horizon:
                            continue
                        rows = hist.iloc[indices[:horizon]]
                        end_close = float(rows.iloc[-1]["Close"])
                        horizon_day = dates[indices[horizon - 1]]
                        leg = self.exit_leg(item, horizon_day, end_close, dates,
                                            self.session_schedule(horizon_day))
                        if leg is None or not _as_price(leg.get("exit_price")):
                            # Neither an observed exit book nor anything to bound one
                            # with. An unmeasurable horizon stays unmeasured; it does
                            # not get a number invented for it.
                            continue
                        # price move only, mid to close, charging nothing
                        gross = (end_close / entry_mid - 1) * 100
                        # what the round trip actually returns: ask in, bid out
                        net = (float(leg["exit_price"]) / entry - 1) * 100
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
                            # the headline is the executable number now, not the gross
                            "return_pct": round(net, 2),
                            "gross_return_pct": round(gross, 2),
                            "net_return_pct": round(net, 2),
                            "benchmark_return_pct": (round(bench_return, 2)
                                                     if bench_return is not None else None),
                            "net_excess_return_pct": (round(net - bench_return, 2)
                                                      if bench_return is not None else None),
                            "max_gain_pct": round((float(rows["High"].max()) / entry - 1) * 100, 2),
                            "max_drawdown_pct": round((float(rows["Low"].min()) / entry - 1) * 100, 2),
                            "target1_hit": bool(item.get("target1") and float(rows["High"].max()) >= float(item["target1"])),
                            "stop_hit": bool(item.get("stop") and float(rows["Low"].min()) <= float(item["stop"])),
                            # how this number was arrived at, so a later reader can tell
                            # an observed round trip from a bounded guess
                            "measurement_schema": MEASUREMENT_SCHEMA_VERSION,
                            "entry_basis": "ask",
                            "entry_price": round(entry, 6),
                            "entry_mid": round(entry_mid, 6),
                            "exit_basis": leg["exit_basis"],
                            "exit_price": round(float(leg["exit_price"]), 6),
                            "exit_cost_pct": (round(leg["exit_cost_pct"], 4)
                                              if leg.get("exit_cost_pct") is not None
                                              else None),
                            "exit_quote_at": leg.get("exit_quote_at"),
                            "exit_session": leg.get("exit_session"),
                            "exit_quote_source": leg.get("exit_quote_source") or "",
                            "exit_quote_feed": leg.get("exit_quote_feed") or "",
                            "entry_quote_feed": item.get("entry_quote_feed") or "",
                            # Both legs are stamped, and both are required. One observed
                            # side does not make an observed round trip.
                            "entry_cost_evidentiary": entry_evidentiary,
                            "exit_cost_evidentiary": bool(leg["cost_evidentiary"]),
                            "cost_evidentiary": bool(leg["cost_evidentiary"]
                                                     and entry_evidentiary),
                        }
                        # A book from a LATER session measures a different holding
                        # period. Record it under its own key so it is available as
                        # evidence for what it did measure, and can never be pooled
                        # into the horizon it did not.
                        if not leg["cost_evidentiary"]:
                            delayed = self._delayed_closing_observation(
                                item, horizon_day, dates, self.session_schedule)
                            if delayed:
                                seen, exit_day, offset = delayed
                                self._record_delayed_exit(
                                    item, outcomes, key, seen, exit_day, offset,
                                    entry, entry_mid, entry_evidentiary)
                    # Delayed-exit keys measure a different hold and must not make a
                    # signal look resolved at the horizon they did not reach.
                    item["resolved"] = all(str(h) in outcomes for h in SIGNAL_HORIZONS)
                    for h in SIGNAL_HORIZONS:
                        for key in (str(h), f"{h}{DELAYED_EXIT_SUFFIX}"):
                            if key in outcomes:
                                self._archive_event("outcome", {
                                    "id": item.get("id"), "horizon": key,
                                    "outcome": outcomes[key],
                                    "resolved": item["resolved"]})
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
            # The signal log also holds AI-rejected controls. This display describes
            # the live approved rule, so controls must not leak into it any more than
            # they leak into daily_baskets/forward_validation.
            values = [x.get("outcomes", {}).get(str(horizon))
                      for x in self.signal_log
                      if x.get(EVIDENCE_ROLE_FIELD, PRIMARY_EVIDENCE_ROLE)
                      == PRIMARY_EVIDENCE_ROLE]
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

    @staticmethod
    def _multiplicity_adjusted_low(mean: float, low: float, tests: int) -> float:
        """The lower bound widened for the number of policies that have been tried.

        Searching over selectors and reporting the nominal 95% interval of whichever one
        currently looks best is how a tuned prompt becomes an "edge". The standard error
        is recovered from the nominal bound and re-applied at a Bonferroni alpha, so one
        tested policy leaves the interval untouched and each further one widens it.
        """
        if tests <= 1:
            return low
        se = (mean - low) / 1.96
        if se <= 0:
            return low
        z = NormalDist().inv_cdf(1.0 - (0.05 / tests) / 2.0)
        return mean - z * se

    @staticmethod
    def _minimum_selector_return(approved: list[float], unavailable: list[float]) -> float:
        """Worst return compatible with the decisions that were not recorded.

        Classified approvals are fixed.  Each unavailable name could have been
        approved or rejected; for any chosen count the worst subset is the names with
        the lowest returns.  Trying every such count therefore gives the exact lower
        bound for the equal-weight selector portfolio.  With no fixed approvals, cash
        at 0% is also a possible decision.
        """
        fixed = [float(value) for value in approved]
        unknown = sorted(float(value) for value in unavailable)
        candidates = [sum(fixed) / len(fixed) if fixed else 0.0]
        total = sum(fixed)
        count = len(fixed)
        for value in unknown:
            total += value
            count += 1
            candidates.append(total / count)
        return min(candidates)

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
        if self.archive_error:
            # A damaged evidence file, or events stuck in the outbox, means the sample
            # being measured is not the sample that was recorded. That outranks progress.
            status = "DATA_INCOMPLETE"
            reason = f"evidence integrity problem: {self.archive_error}"
        elif progress["completed_signal_days"] < required_days or not enough_excess:
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
        elif book.get("cost_modeled_days"):
            # A positive result computed on an exit cost nobody observed is a statement
            # about the bound, not about the market. The bound is deliberately harsh, so
            # this is not the usual "too optimistic" failure - it is simply not evidence
            # either way, and it may not be promoted until real exit books back it.
            status = "DATA_INCOMPLETE"
            reason = (
                f"{book['cost_modeled_days']} of {len(book['baskets'])} completed "
                f"basket(s) hold {book['cost_modeled_rows']} outcome(s) whose exit leg "
                f"was never observed; their round trip rests on an uncalibrated exit-cost "
                f"bound (cost_evidentiary: false). A positive verdict needs horizon bid "
                f"quotes, not a modeled one"
            )
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
                x.get("ticker") for x in self.evidence_rows()
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

    def ai_value_audit(self, horizon: str = "5") -> dict:
        """Economic value of the AI selector against the full mechanical portfolio.

        ``mean(approved) - mean(rejected)`` is not the capital policy: it gives the two
        labels equal weight regardless of group size and drops all-approved/all-skipped
        days. Instead, every sufficiently classified day compares the equal-weight
        AI-selected portfolio (0% cash when it selects nothing) with the equal-weight
        portfolio of every pre-AI mechanical name. Missing classifications receive an
        adverse selection bound even when the day is excluded from the point estimate.
        Every member must resolve with observed costs.

        This can reject or flag a promising selector; it can never authorize trading
        or prove that the selected portfolio itself is profitable.
        """
        horizon = str(horizon)
        policy_id = ai_decision_policy_id()
        eligible_rows = [r for r in self.mechanical_evidence_rows()
                         if int(r.get("ai_policy_version") or 0)
                         == AI_DECISION_POLICY_VERSION
                         and r.get("ai_policy_id") == policy_id]
        rows = [r for r in eligible_rows
                if r.get(AI_SELECTION_FIELD) in ("approved", "rejected")]
        # Anything outside the two classified decisions is unavailable. Treating a
        # missing or future value as neither classified nor unavailable would lower
        # coverage while also omitting it from the adverse decision bound.
        unavailable_rows = [r for r in eligible_rows
                            if r.get(AI_SELECTION_FIELD) not in ("approved", "rejected")]
        classification_coverage = (
            100.0 * len(rows) / len(eligible_rows) if eligible_rows else 0.0
        )
        model_counts: dict[str, int] = {}
        for row in rows:
            model = str(row.get("ai_model") or "unknown")
            model_counts[model] = model_counts.get(model, 0) + 1
        by_day: dict[str, list[dict]] = {}
        # An unavailable model fails closed in production, but its name still existed
        # in the mechanical opportunity set. Keep it in the economic baseline so API
        # outages cannot silently select the comparison sample.
        for row in eligible_rows:
            by_day.setdefault(str(row[SIGNAL_DAY_FIELD]), []).append(row)

        today = datetime.now(NY).date()
        need = int(horizon) + HORIZON_GRACE_SESSIONS
        sessions = self._recent_sessions()
        fallback_days = math.ceil(need * 7 / 5) + 2

        def is_mature(day: str) -> bool:
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                return True
            if sessions:
                return sum(1 for session in sessions if session > parsed) >= need
            return (today - parsed).days >= fallback_days

        comparisons: list[dict] = []
        pending_days = stale_days = modeled_days = 0
        mixed_days = all_approved_days = all_skipped_days = 0
        unclassified_days = unclassified_rows = benchmark_inconsistent_days = 0
        unclassified_bound_days = missing_decision_bound_days = 0
        bounded_daily_lifts: list[float] = []
        for day in sorted(by_day):
            members = by_day[day]
            approved = [r for r in members
                        if r.get(AI_SELECTION_FIELD) == "approved"]
            rejected = [r for r in members
                        if r.get(AI_SELECTION_FIELD) == "rejected"]
            unavailable = [r for r in members
                           if r.get(AI_SELECTION_FIELD) not in ("approved", "rejected")]
            # A day the model barely saw is an OUTAGE, not a decision. Scoring a fully
            # unavailable day as "the selector chose cash" credits it with the whole
            # negative of the mechanical basket - so a model failure during a losing
            # stretch manufactures positive lift out of an API error. The global
            # coverage gate cannot catch this: 90% overall coverage still allows whole
            # days to be dark. The floor is applied to each day as well.
            classified_members = len(approved) + len(rejected)
            day_coverage = (100.0 * classified_members / len(members)
                            if members else 0.0)
            if day_coverage < AI_VALUE_MIN_DAY_CLASSIFICATION_PCT:
                unclassified_days += 1
                unclassified_rows += len(unavailable)
            # Outcome integrity is checked BEFORE a low-coverage day can be excluded
            # from the point estimate.  Otherwise a fully unclassified halted or
            # delisted name disappears before stale/modeled counters see it, allowing
            # the audit to call the surviving days promising.
            outcomes = [(r.get("outcomes") or {}).get(horizon) for r in members]
            if not all(isinstance(out, dict)
                       and out.get("net_return_pct") is not None for out in outcomes):
                if is_mature(day):
                    stale_days += 1
                else:
                    pending_days += 1
                continue
            if not all(self.outcome_is_evidentiary(out) for out in outcomes):
                modeled_days += 1
                continue

            def group_mean(group: list[dict], field: str) -> float | None:
                values = [(r.get("outcomes") or {}).get(horizon, {}).get(field)
                          for r in group]
                if not values or any(value is None for value in values):
                    return None
                return sum(float(value) for value in values) / len(values)

            mechanical_net = group_mean(members, "net_return_pct")
            mechanical_excess = group_mean(members, "net_excess_return_pct")
            approved_net = group_mean(approved, "net_return_pct")
            rejected_net = group_mean(rejected, "net_return_pct")
            approved_excess = group_mean(approved, "net_excess_return_pct")
            if mechanical_net is None:
                continue

            # Every unavailable decision is unknown, including the permitted minority
            # on a day that meets the coverage floor. Treating that name as a rejection
            # can manufacture lift when it later loses. The point estimate retains the
            # deployed cash result on otherwise usable days, but the positive gate uses
            # the worst approve/reject assignment for every unavailable name.
            if unavailable:
                approved_values = [
                    float((r.get("outcomes") or {})[horizon]["net_return_pct"])
                    for r in approved
                ]
                unavailable_values = [
                    float((r.get("outcomes") or {})[horizon]["net_return_pct"])
                    for r in unavailable
                ]
                worst_ai_net = self._minimum_selector_return(
                    approved_values, unavailable_values)
                bounded_daily_lifts.append(worst_ai_net - mechanical_net)
                missing_decision_bound_days += 1

            if day_coverage < AI_VALUE_MIN_DAY_CLASSIFICATION_PCT:
                # The point estimate cannot pretend the model made decisions it never
                # made. But dropping the day from every calculation would select the
                # sample, so its adverse assignment remains in bounded_daily_lifts.
                unclassified_bound_days += 1
                continue

            # When the selector approves nothing the deployed policy holds cash. Net
            # cash return is conservatively 0%. For the excess leg, infer the day's
            # benchmark from each observed net-minus-excess pair and measure cash
            # against that benchmark.
            if approved:
                ai_net = approved_net
                ai_excess = approved_excess
            else:
                ai_net = 0.0
                implied_benchmarks = []
                for outcome in outcomes:
                    net = outcome.get("net_return_pct")
                    excess = outcome.get("net_excess_return_pct")
                    if net is None or excess is None:
                        implied_benchmarks = []
                        break
                    implied_benchmarks.append(float(net) - float(excess))
                ai_excess = (-sum(implied_benchmarks) / len(implied_benchmarks)
                             if implied_benchmarks else None)

            if len(approved) == len(members):
                all_approved_days += 1
            elif not approved:
                all_skipped_days += 1
            else:
                mixed_days += 1

            net_lift = ai_net - mechanical_net
            excess_lift = (ai_excess - mechanical_excess
                           if ai_excess is not None and mechanical_excess is not None
                           else None)
            # Not a second result. Every member of a signal day shares IWM's horizon
            # return, so in a difference of two same-day portfolios the benchmark
            # cancels and this equals net_lift by construction. A divergence therefore
            # says the day's rows disagree about the benchmark, which is a data problem.
            if (excess_lift is not None
                    and abs(excess_lift - net_lift) > AI_VALUE_BENCHMARK_TOLERANCE_PCT):
                benchmark_inconsistent_days += 1

            if not unavailable:
                bounded_daily_lifts.append(net_lift)

            comparisons.append({
                "day": day,
                "mechanical_members": len(members),
                "approved_members": len(approved),
                "rejected_members": len(rejected),
                "unavailable_members": len(unavailable),
                "ai_portfolio_net_pct": ai_net,
                "mechanical_net_pct": mechanical_net,
                "rejected_net_pct": rejected_net,
                "net_lift_pct": net_lift,
                "excess_lift_pct": excess_lift,
                "classification_coverage_pct": round(day_coverage, 1),
            })

        net_lifts = [item["net_lift_pct"] for item in comparisons]
        excess_lifts = [item["excess_lift_pct"] for item in comparisons
                        if item["excess_lift_pct"] is not None]
        # Consecutive signal days share sessions at a multi-session horizon, so the lift
        # series is overlapping and carries dependence out to horizon-1 lags. A fixed
        # 5-lag bandwidth truncates that at horizon 10 and reports an interval narrower
        # than the data supports.
        hac_lag = max(5, int(horizon) - 1)
        net_mean, net_lo, net_hi = self._hac_mean_ci(net_lifts, max_lag=hac_lag)
        ex_mean, ex_lo, ex_hi = self._hac_mean_ci(excess_lifts, max_lag=hac_lag)
        # Every AI policy that has ever accumulated evidence is a separate test of the
        # same question. An uncorrected 95% interval on the k-th one tried is not a 95%
        # interval; the promising branch therefore has to clear a Bonferroni-corrected
        # bound rather than the nominal one.
        policies_tested = len({
            (int(r.get("ai_policy_version") or 0), str(r.get("ai_policy_id") or ""))
            for r in self.mechanical_evidence_rows() if r.get("ai_policy_id")})
        net_lo_adj = self._multiplicity_adjusted_low(net_mean, net_lo, policies_tested)
        ex_lo_adj = self._multiplicity_adjusted_low(ex_mean, ex_lo, policies_tested)
        bounded_mean, bounded_lo, bounded_hi = self._hac_mean_ci(
            bounded_daily_lifts, max_lag=hac_lag)
        bounded_lo_adj = self._multiplicity_adjusted_low(
            bounded_mean, bounded_lo, policies_tested)
        missing_decision_bound_clears = bool(
            bounded_daily_lifts and bounded_lo_adj > 0)
        ai_mean = (sum(item["ai_portfolio_net_pct"] for item in comparisons)
                   / len(comparisons) if comparisons else 0.0)
        mechanical_mean = (sum(item["mechanical_net_pct"] for item in comparisons)
                           / len(comparisons) if comparisons else 0.0)
        rejected_values = [item["rejected_net_pct"] for item in comparisons
                           if item["rejected_net_pct"] is not None]
        rejected_mean = (sum(rejected_values) / len(rejected_values)
                         if rejected_values else 0.0)

        if self.archive_error:
            status = "DATA_INCOMPLETE"
            reason = f"evidence integrity problem: {self.archive_error}"
        elif (eligible_rows and classification_coverage
              < AI_VALUE_MIN_CLASSIFICATION_COVERAGE_PCT):
            status = "DATA_INCOMPLETE"
            reason = (
                f"AI classified only {classification_coverage:.1f}% of mechanically "
                f"eligible rows; need at least "
                f"{AI_VALUE_MIN_CLASSIFICATION_COVERAGE_PCT:.0f}% so API/model failures "
                f"cannot select the measured sample"
            )
        elif stale_days or modeled_days:
            status = "DATA_INCOMPLETE"
            reason = (
                f"{stale_days} matured comparison day(s) have missing outcomes and "
                f"{modeled_days} comparison day(s) lack observed execution costs"
            )
        elif (len(comparisons) < AI_VALUE_MIN_COMPARISON_DAYS
              or len(excess_lifts) < AI_VALUE_MIN_COMPARISON_DAYS):
            status = "COLLECTING"
            reason = (
                f"need {AI_VALUE_MIN_COMPARISON_DAYS} completed AI-versus-mechanical "
                f"days; have {len(comparisons)} net / "
                f"{len(excess_lifts)} IWM-relative"
            )
        elif benchmark_inconsistent_days:
            status = "DATA_INCOMPLETE"
            reason = (
                f"{benchmark_inconsistent_days} comparison day(s) have members that "
                f"disagree about IWM's horizon return by more than "
                f"{AI_VALUE_BENCHMARK_TOLERANCE_PCT}pp; in a same-day portfolio "
                f"difference the benchmark must cancel, so the rows are inconsistent"
            )
        elif missing_decision_bound_days and not missing_decision_bound_clears:
            status = "DATA_INCOMPLETE"
            reason = (
                f"{missing_decision_bound_days} completed day(s) contain unavailable "
                "AI decisions; assigning every missing decision in the most adverse "
                "way leaves the multiplicity-"
                f"adjusted lower bound at {bounded_lo_adj:.3f}%, so missing decisions "
                "can still explain the apparent lift"
            )
        elif (net_lo_adj > 0 and ex_lo_adj > 0
              and missing_decision_bound_clears):
            status = "AI_LIFT_PROMISING_NOT_VALIDATED"
            reason = (
                "the actual AI-selected portfolio beat the full same-day mechanical "
                "portfolio after observed costs"
                + (f", and survives correcting for the {policies_tested} AI policies "
                   f"tested" if policies_tested > 1 else "")
                + "; an independent frozen-rule audit is still required"
            )
        else:
            status = "NO_MEASURED_AI_EDGE"
            reason = (
                "the capital-aware after-cost AI lift is non-positive or its "
                "dependence-robust interval crosses zero"
                + (f" once corrected for the {policies_tested} AI policies tested"
                   if policies_tested > 1 else "")
            )

        return {
            "status": status,
            "auto_trade_allowed": False,
            "reason": reason,
            "horizon_sessions": int(horizon),
            "comparison_days": len(comparisons),
            "minimum_comparison_days": AI_VALUE_MIN_COMPARISON_DAYS,
            "comparison_days_remaining": max(
                0, AI_VALUE_MIN_COMPARISON_DAYS - len(comparisons)),
            "pending_comparison_days": pending_days,
            "stale_comparison_days": stale_days,
            "modeled_cost_comparison_days": modeled_days,
            "mixed_selection_days": mixed_days,
            "all_approved_days": all_approved_days,
            "all_skipped_days": all_skipped_days,
            # Days the model barely saw. Excluded rather than scored as a decision to
            # hold cash, which would credit the selector for an API outage.
            "unclassified_days": unclassified_days,
            "unclassified_rows": unclassified_rows,
            "unclassified_bound_days": unclassified_bound_days,
            "missing_decision_bound_days": missing_decision_bound_days,
            "minimum_day_classification_pct": AI_VALUE_MIN_DAY_CLASSIFICATION_PCT,
            "classified_rows": len(rows),
            "unavailable_rows": len(unavailable_rows),
            "classification_coverage_pct": round(classification_coverage, 1),
            "minimum_classification_coverage_pct": (
                AI_VALUE_MIN_CLASSIFICATION_COVERAGE_PCT),
            "ai_policy_version": AI_DECISION_POLICY_VERSION,
            "ai_policy_id": policy_id,
            # The names of everything the fingerprint covers, so the guarantee that a
            # changed selector starts a new population is auditable rather than claimed.
            # Names only: the system prompt itself does not belong in an API payload.
            "ai_policy_fingerprint_covers": sorted(ai_decision_policy_components()),
            "model_counts": model_counts,
            "ai_portfolio_mean_net_pct": round(ai_mean, 3) if comparisons else None,
            "mechanical_mean_net_pct": (round(mechanical_mean, 3)
                                         if comparisons else None),
            "rejected_diagnostic_mean_net_pct": (
                round(rejected_mean, 3) if rejected_values else None),
            "mean_ai_lift_pct": round(net_mean, 3) if comparisons else None,
            "ai_lift_hac_95_pct": ([round(net_lo, 3), round(net_hi, 3)]
                                    if comparisons else None),
            "mean_ai_excess_lift_pct": (round(ex_mean, 3) if excess_lifts else None),
            "ai_excess_lift_hac_95_pct": (
                [round(ex_lo, 3), round(ex_hi, 3)] if excess_lifts else None),
            "hac_max_lag": hac_lag,
            # How many selectors have been tried, and the bound the promising branch
            # actually has to clear once that search is priced in.
            "ai_policies_tested": policies_tested,
            "ai_lift_multiplicity_adjusted_low_pct": (
                round(net_lo_adj, 3) if comparisons else None),
            "ai_excess_lift_multiplicity_adjusted_low_pct": (
                round(ex_lo_adj, 3) if excess_lifts else None),
            "classification_missing_bound": {
                "days_bounded": missing_decision_bound_days,
                "low_coverage_days_bounded": unclassified_bound_days,
                "bounded_series_days": len(bounded_daily_lifts),
                "bounded_mean_ai_lift_pct": (
                    round(bounded_mean, 3) if bounded_daily_lifts else None),
                "bounded_hac_95_pct": (
                    [round(bounded_lo, 3), round(bounded_hi, 3)]
                    if bounded_daily_lifts else None),
                "multiplicity_adjusted_low_pct": (
                    round(bounded_lo_adj, 3) if bounded_daily_lifts else None),
                "clears_zero": missing_decision_bound_clears,
                "assumption": (
                    "each unavailable AI decision is assigned to the approve/reject "
                    "combination producing the lowest equal-weight portfolio return"),
            },
            "benchmark_inconsistent_days": benchmark_inconsistent_days,
            "benchmark_leg_is_independent": False,
            "benchmark_leg_note": (
                "Members of one signal day share IWM's horizon return, so it cancels in "
                "a same-day difference of two portfolios: the IWM-relative lift equals "
                "the net lift by construction. It is a consistency check, not a second "
                "confirmation."),
            # The AI portfolio is not the mechanical basket's risk. A one-name selection
            # measured against a twenty-name basket is a concentration change as much as
            # a skill claim, so the sizes are reported next to the lift.
            "ai_portfolio_concentration": {
                "mean_selected_names": (
                    round(sum(item["approved_members"] for item in comparisons)
                          / len(comparisons), 2) if comparisons else None),
                "mean_mechanical_names": (
                    round(sum(item["mechanical_members"] for item in comparisons)
                          / len(comparisons), 2) if comparisons else None),
                "single_name_days": sum(1 for item in comparisons
                                        if item["approved_members"] == 1),
                "cash_days": sum(1 for item in comparisons
                                 if item["approved_members"] == 0),
            },
            "grouping": ("equal-weight AI-selected portfolio versus the full "
                         "equal-weight mechanical basket per signal day; 0% cash when "
                         "AI selects nothing"),
            "note": ("This tests the deployed AI filter's incremental economic value, "
                     "including all-approved, all-skipped, and API-failure days. It does "
                     "not prove the selected portfolio is profitable and never unlocks "
                     "trading."),
        }

    async def manage_loop(self):
        # Both loops begin immediately. The first deep scan waits for the initial
        # all-symbol result inside _scan_locked, so a fixed startup sleep only made the
        # page look idle without protecting any dependency.
        if self._universe_task is None or self._universe_task.done():
            self._universe_task = asyncio.create_task(self._continuous_universe_loop())
        while True:
            next_wake = float(LOOP_TICK_SEC)
            try:
                now = time.time()
                today = datetime.now(NY).strftime("%Y-%m-%d")
                if today != self.day_key:
                    self.day_key = today
                    self.day_pnl = 0.0
                is_open = self.market_open()
                # Two separate clocks. The closing book exists for ten minutes a day and
                # cannot be fetched afterwards; the outcome arithmetic can run whenever.
                await self.refresh_session_calendar()
                if self.closing_capture_due(now):
                    self._last_closing_capture = now
                    await self._capture_exit_quotes()
                if now - self.last_outcome_update >= OUTCOME_UPDATE_SEC:
                    await self._capture_exit_quotes()
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
                # Housekeeping can stay on its five-second rhythm without making a
                # ten-second scan late. Wake on the absolute scan deadline; if a scan
                # is still running, the wait below wakes as soon as that task ends.
                if not self._scan_lock.locked():
                    clock_now = time.time()
                    next_wake = min(
                        next_wake,
                        max(0.05, self.scan_due_at(is_open, clock_now) - clock_now),
                    )
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.status = "error"
            if self._scan_task is not None and not self._scan_task.done():
                await asyncio.wait({self._scan_task}, timeout=LOOP_TICK_SEC)
            else:
                await asyncio.sleep(next_wake)

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
        due_at = self.scan_due_at(market_is_open, now)
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
        coverage = dict(self.universe_coverage or {})
        coverage.update({
            "scan_in_progress": self._universe_pass_in_progress,
            "next_pass_in_sec": max(0, int(self._next_universe_scan_at - now)),
            "combined_penny_price_matches": len(self._universe_rows),
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
            "universe_coverage": coverage,
            "sec_feed": research.sec_edgar.current_feed_status(),
            "evidence_clock": self._evidence_clock(),
            "persistence_error": "; ".join(x for x in (self.state_save_error, self.archive_error) if x),
            "state_save_error": self.state_save_error,
            "archive_error": self.archive_error,
            # Measurement provenance, so the page can never show a return without
            # showing what it was measured against.
            "measurement_schema": MEASUREMENT_SCHEMA_VERSION,
            "quote_capture_error": self.quote_capture_error,
            "quote_feed": penny_quotes.feed_name(),
            "quote_feed_description": penny_quotes.feed_description(),
            "quote_feed_is_consolidated": penny_quotes.is_consolidated(),
            "last_scan": self.last_scan,
            "last_scan_started": self.last_scan_started,
            "last_full_scan": self.last_full_scan,
            "last_scan_mode": self.last_scan_mode,
            "last_scan_duration_sec": self.last_scan_duration,
            "scan_interval_sec": interval,
            "server_time": now,
            "next_scan_at": due_at,
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
            "ai_value_audit": self.ai_value_audit(),
            "top_n": TOP_N,
            "regime": research.market_regime(),
            "edge_policy": research.edge_policy(),
            "live_rule_evidence": research.live_rule_evidence(),
            "max_per_sector": MAX_PER_SECTOR,
            "history": self.history[:40],
            "log": self.log[:25],
            "last_error": self.last_error,
            "rules": (f"every supported listed symbol swept every {UNIVERSE_TARGET_CYCLE_SEC}s "
                      f"from startup (real-time IEX between {UNIVERSE_FULL_TAPE_SEC // 60}m "
                      f"delayed-consolidated baselines), plus "
                      f"deep dossier scans every {DEEP_SCAN_SEC}s in every market phase "
                      f"(non-overlapping; a slow pass resumes immediately); "
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
                     "promote a weak setup. Confirmed AI rejections are retained as a non-trading "
                     "control group, so the model's incremental value can be measured instead of "
                     "assumed. Confirmed unvalidated candidates are never filled, but "
                     "cost-adjusted and IWM-relative results are measured at 1/5/10 later sessions."),
        }
