"""
pennystock_bot.py - penny-stock research bot: screen -> dossier -> AI verdict.

WHAT THIS IS: a research assistant. For each listed-U.S. candidate it assembles
price/liquidity, causal technical confirmation, float, cash vs burn, dilution,
recent SEC form types and time-stamped news, then asks the AI for an independent
structured risk verdict.

WHAT THIS IS NOT: a predictor. Penny stocks are the hardest category in markets -
pump-and-dumps, dilution, delisting, and spreads of 5-10%+. That last one is decisive:
a strategy needs an edge bigger than the round-trip cost before it makes a cent, and
penny-stock spreads are ~50x a crypto taker fee. The bot therefore reports what is
TRUE and what the RISKS are, and refuses to dress a coin flip up as a signal.

Hard red flags it checks (each one kills most penny-stock trades on its own):
  * spread / illiquidity  - can you even get in and out?
  * dilution              - share count exploding = your slice shrinks every quarter
  * cash runway           - burning cash with < 2 quarters left = dilution is coming
  * reverse splits        - near-universal marker of long-term value destruction
  * pump signature        - tiny float + volume spike + price spike, no filings

Paper/advisory only. It never places an order.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import config

try:
    import yfinance as yf
except Exception:                                  # pragma: no cover
    yf = None

import ai_client
from research import edgar_catalysts as sec_edgar

STATE_PATH = os.path.join(config.DATA_DIR, "pennystock_state.json")
EDGE_POLICY_PATH = os.path.join(config.DATA_DIR, "pennystock_edge_policy.json")
EDGE_REPORT_PATH = os.path.join(config.DATA_DIR, "pennystock_edge_report.json")
LIVE_AUDIT_PATH = os.path.join(config.DATA_DIR, "pennystock_live_rule_audit.json")
CATALYST_AUDIT_PATH = os.path.join(config.DATA_DIR, "pennystock_catalyst_confirmation.json")
ITEM_AUDIT_PATH = os.path.join(config.DATA_DIR, "pennystock_item_code_audit.json")
_EDGE_POLICY_CACHE: tuple[float, dict] = (-1.0, {})
_LIVE_AUDIT_CACHE: tuple[tuple[float, float, float], dict] = ((-1.0, -1.0, -1.0), {})
_PENNY_UNIVERSE_CACHE: tuple[float, set[str]] = (0.0, set())
PENNY_UNIVERSE_TTL_SEC = 15 * 60

# Research authorization belongs to an exact implementation, not to the broad idea of
# "buying penny stocks".  Version 4 requires a time-aligned headline AND an official,
# non-adverse SEC 8-K.  The previous implementation was named as though SEC confirmation
# were mandatory but still let an arbitrary fresh Yahoo headline qualify by itself.
# Changing the identifier is intentional: evidence for an older implementation must
# never unlock this one.
LIVE_STRATEGY_ID = "live_sec_news_align_v4"

# A provider's regularMarketTime timestamps the last trade, not the bid/ask update.  It
# is still the best freshness evidence available on the free feed, so keep the window
# tight and require an internally plausible, unlocked book before calling it usable for
# confirmation or a paper fill.
MAX_EXECUTION_QUOTE_AGE_MIN = 5.0
CATALYST_ALIGNMENT_HOURS = 24.0

# The shared config.AI_TIMEOUT_SEC is deliberately tight (6s) because the news bots
# must DROP a stale signal rather than trade it. Company analysis has no such decay -
# a 70B model needs ~6s and truncating it would just lose the verdict. So this bot
# uses its own generous timeout via a dedicated client.
AI_TIMEOUT_SEC = 45.0


# Groq free tier meters BOTH tokens-per-minute and tokens-per-DAY, per model
# (the 70B is only 100k tokens/day ~= 94 analyses). Relying on one model means the
# bot goes blind once that budget is gone, so try progressively cheaper models.
# Order matters: qwen emits a long <think> block that eats the token budget before
# it ever reaches the JSON, so it goes LAST. The llama models answer directly.
AI_MODEL_CHAIN = [
    "llama-3.3-70b-versatile",   # best reasoning, answers directly
    "llama-3.1-8b-instant",      # fast, biggest daily budget, answers directly
    "qwen/qwen3.6-27b",          # last resort (needs a big max_tokens to get past <think>)
]
LAST_MODEL_USED = ""


async def _call_ai_long(system_prompt: str, user_prompt: str) -> str:
    """Ask the best model with budget left. Falls through the chain on rate limits
    and backs off on transient per-minute limits."""
    global LAST_MODEL_USED
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=config.AI_API_KEY, base_url=config.AI_BASE_URL,
                         timeout=AI_TIMEOUT_SEC, max_retries=0)
    preferred = getattr(config, "PENNY_AI_MODEL", None)
    chain = ([preferred] if preferred else []) + [m for m in AI_MODEL_CHAIN if m != preferred]
    last = None
    for model in chain:
        for attempt in range(2):
            try:
                request = dict(
                    model=model,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                    temperature=0.2,
                    max_tokens=1600,         # room for the full object even after a <think> preamble
                    response_format={"type": "json_object"},
                )
                if model.startswith("qwen/"):
                    # Groq Qwen reasoning otherwise arrives in <think> inside content
                    # and can consume the whole output before the JSON answer.
                    request["extra_body"] = {"reasoning_effort": "none"}
                r = await client.chat.completions.create(**request)
                LAST_MODEL_USED = model
                return r.choices[0].message.content or ""
            except Exception as e:
                last = e
                msg = str(e)
                if "429" not in msg and "rate" not in msg.lower():
                    raise
                if "per day" in msg or "TPD" in msg:
                    break                      # daily budget gone -> next model
                await asyncio.sleep(5)         # per-minute limit -> brief wait, retry
    raise last


# --- screening universe ----------------------------------------------------
MAX_PRICE = 5.00          # classic penny-stock ceiling
MIN_PRICE = 0.10          # below this, spreads and delisting risk explode
MIN_AVG_VOLUME = 300_000  # you must be able to exit
MIN_MARKET_CAP = 10_000_000
SCAN_LIMIT = 40           # candidates pulled per scan
LISTED_EXCHANGES = {"NMS", "NCM", "NGM", "NYQ", "ASE", "NAE"}
FRESH_NEWS_HOURS = 72.0
OFFERING_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B", "EFFECT")

_MARKET_CACHE: tuple[float, bool, str] = (0.0, False, "unknown")


def live_rule_evidence() -> dict:
    """Backtest evidence for the scanner's price/volume component, for display only.

    ``edge_policy`` already blocks fills, so this never gates anything - it exists so the
    desk shows the negative base-rate evidence instead of hiding it.  Historical bars
    cannot reconstruct point-in-time headlines or filings, so this is explicitly not a
    backtest of the full catalyst-confirmation strategy.
    """
    global _LIVE_AUDIT_CACHE
    try:
        mtimes = tuple(
            os.path.getmtime(path) if os.path.exists(path) else -1.0
            for path in (LIVE_AUDIT_PATH, CATALYST_AUDIT_PATH, ITEM_AUDIT_PATH)
        )
        if _LIVE_AUDIT_CACHE[0] == mtimes:
            return dict(_LIVE_AUDIT_CACHE[1])
        with open(LIVE_AUDIT_PATH, encoding="utf-8") as f:
            audit = json.load(f)
        dec = (audit.get("cost_decomposition") or {}).get("all") or {}
        test = ((audit.get("splits") or {}).get("test")) or {}
        rank = audit.get("rank_information") or {}
        value = {
            "measured": True,
            "strategy_id": audit.get("strategy_id"),
            "current_strategy_id": LIVE_STRATEGY_ID,
            "scope": "price_volume_core_only",
            "trades": audit.get("trades"),
            "test_mean_net_pct": test.get("mean_net_pct"),
            "mean_gross_pct": dec.get("mean_gross_pct"),
            "mean_cost_pct": dec.get("mean_cost_pct"),
            "gross_is_zero": dec.get("gross_indistinguishable_from_zero"),
            "diagnosis": dec.get("diagnosis"),
            "rank_verdicts": {
                k: (rank.get(k) or {}).get("verdict")
                for k in ("composite", "hype", "technical") if rank.get(k)
            },
        }
        # The catalyst gate is the desk's current premise, so its own out-of-sample
        # result belongs next to the rule it replaced rather than sitting at
        # "COLLECTING" while a ten-year point-in-time answer already exists.
        try:
            with open(CATALYST_AUDIT_PATH, encoding="utf-8") as f:
                cat = json.load(f)
            if cat.get("applicable"):
                value["catalyst_gate"] = {
                    "tested": True,
                    "setups": cat.get("test_setups"),
                    "gross_pct": cat.get("gross_pct"),
                    "gross_ci_pct": cat.get("gross_ci_pct"),
                    "profitable_at_any_modelled_cost": any(
                        v.get("confidently_profitable")
                        for v in (cat.get("net_at_cost") or {}).values()
                    ),
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        # Item codes classify disclosure type and adverse events; they do not earn
        # bullish points by themselves.  Show the closest causal marginal audit with
        # its limitations instead of mislabelling it an exact live-rule backtest.
        try:
            with open(ITEM_AUDIT_PATH, encoding="utf-8") as f:
                item = json.load(f)
            proxy = (((item.get("results") or {}).get("reaction_confirmed_proxy") or {})
                     .get("post_2024_reused") or {})
            material = (proxy.get("material_direction_unknown") or {})
            gross = material.get("gross") or {}
            net = material.get("net_after_0_5pct_cost") or {}
            if gross.get("applicable"):
                value["item_gate"] = {
                    "tested": True,
                    "scope": "reaction-confirmed marginal proxy; not exact live rule",
                    "window": "post_2024_reused_not_holdout",
                    "events": gross.get("events"),
                    "material_gross_basket_pct": gross.get(
                        "mean_signal_day_basket_net_pct"
                    ),
                    "material_gross_ci_pct": gross.get("bootstrap_95_pct"),
                    "material_net_basket_pct": net.get(
                        "mean_signal_day_basket_net_pct"
                    ),
                    "exact_live_rule_backtest": bool(
                        item.get("exact_live_rule_backtest")
                    ),
                    "status": item.get("status"),
                    "verdict": item.get("verdict"),
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        _LIVE_AUDIT_CACHE = (mtimes, value)
        return dict(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"measured": False,
                "diagnosis": "the deployed rule has not been backtested"}


def edge_policy(force_refresh: bool = False) -> dict:
    """Load the research gate. Missing/corrupt evidence always fails closed."""
    global _EDGE_POLICY_CACHE
    default = {
        "status": "MISSING",
        "auto_trade_allowed": False,
        "selected_strategy": "none",
        "strategy_id": "none",
        "reason": "No reproducible edge report is available.",
        "failed_checks": ["edge policy missing"],
    }
    try:
        mtime = max(os.path.getmtime(EDGE_POLICY_PATH), os.path.getmtime(EDGE_REPORT_PATH))
        if not force_refresh and _EDGE_POLICY_CACHE[0] == mtime:
            return dict(_EDGE_POLICY_CACHE[1])
        with open(EDGE_POLICY_PATH, encoding="utf-8") as f:
            value = json.load(f)
        with open(EDGE_REPORT_PATH, encoding="utf-8") as f:
            report = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("edge policy is not an object")
        if not isinstance(report, dict):
            raise ValueError("edge report is not an object")
        if (
            value.get("policy_hash") != report.get("policy_hash")
            or value.get("status") != report.get("status")
            or bool(value.get("auto_trade_allowed"))
            != bool(report.get("auto_trade_allowed"))
            or value.get("selected_strategy") != report.get("selected_strategy")
        ):
            raise ValueError("edge policy/report mismatch")
        value["auto_trade_allowed"] = bool(value.get("auto_trade_allowed", False))
        value["status"] = str(value.get("status") or "INVALID").upper()
        generated = datetime.fromisoformat(
            str(value.get("generated_at") or "").replace("Z", "+00:00")
        )
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - generated).total_seconds() / 86400.0
        if age_days < -1 or age_days > 7:
            value["status"] = "STALE"
            value["reason"] = "Edge audit is older than seven days and must be rerun."
        if value["status"] != "VALIDATED":
            value["auto_trade_allowed"] = False
        elif value["auto_trade_allowed"]:
            authorized_id = str(
                value.get("strategy_id") or value.get("selected_strategy") or ""
            )
            if authorized_id != LIVE_STRATEGY_ID:
                value["status"] = "STRATEGY_MISMATCH"
                value["auto_trade_allowed"] = False
                value["reason"] = (
                    f"Audit authorizes {authorized_id or 'an unspecified strategy'}, but "
                    f"this scanner runs {LIVE_STRATEGY_ID}; authorization cannot transfer."
                )
        _EDGE_POLICY_CACHE = (mtime, value)
        return dict(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _EDGE_POLICY_CACHE = (-1.0, default)
        return dict(default)


@dataclass
class Dossier:
    ticker: str
    price: float = 0.0
    change_pct: float = 0.0
    avg_volume: float = 0.0
    volume: float = 0.0
    volume_surge: float = 0.0      # today's volume / average
    market_cap: float = 0.0
    float_shares: float = 0.0
    shares_outstanding: float = 0.0
    shares_change_pct: float = 0.0  # YoY dilution
    cash: float = 0.0
    debt: float = 0.0
    op_cashflow: float = 0.0        # negative = burning
    runway_quarters: float = 0.0
    revenue: float = 0.0
    cash_known: bool = False
    debt_known: bool = False
    revenue_known: bool = False
    op_cashflow_known: bool = False
    runway_known: bool = False
    shares_change_known: bool = False
    data_completeness: float = 0.0
    spread_pct: float = 0.0
    spread_reliable: bool = False
    bid: float = 0.0
    ask: float = 0.0
    quote_age_min: float = -1.0
    market_state: str = "UNKNOWN"
    exchange: str = ""
    quote_type: str = ""
    off_52w_high_pct: float = 0.0
    sector: str = ""
    name: str = ""
    # --- catalysts / positioning ---
    earnings_date: str = ""
    days_to_earnings: float = -1.0
    short_pct_float: float = 0.0
    inst_pct: float = 0.0
    insider_pct: float = 0.0
    analyst_target: float = 0.0
    analyst_upside_pct: float = 0.0
    recommendation: str = ""
    fresh_news_count: int = 0
    latest_news_age_hours: float = -1.0
    recent_filings: list = field(default_factory=list)
    sec_8k_verified: bool = False
    latest_sec_8k_age_hours: float = -1.0
    recent_8k_items: list = field(default_factory=list)
    adverse_8k_items: list = field(default_factory=list)
    recent_offering: bool = False
    latest_offering_age_days: float = -1.0
    recent_reverse_split: bool = False
    # price/volume confirmation. These are deliberately mechanical and causal.
    technical_known: bool = False
    raw_volume_ratio: float = 0.0
    gap_pct: float = 0.0
    from_open_pct: float = 0.0
    close_location: float = 0.5
    return_5d_pct: float = 0.0
    return_20d_pct: float = 0.0
    sma20_distance_pct: float = 0.0
    high20_distance_pct: float = 0.0
    atr_pct: float = 0.0
    catalysts: list = field(default_factory=list)
    news: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    error: str = ""


def _safe(d: dict, *keys, default=0.0):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
    return default


def _known(d: dict, key: str) -> bool:
    """True when a provider supplied a finite numeric value, including zero."""
    value = d.get(key)
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _age_hours(value) -> float:
    """Parse Yahoo ISO/epoch dates. Unknown or future values return -1."""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            txt = str(value or "").strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
        return max(0.0, age) if age > -1.0 else -1.0
    except (TypeError, ValueError, OSError):
        return -1.0


def _expected_volume_fraction(now: datetime | None = None) -> float:
    """Approximate the regular-session cumulative volume curve.

    A straight-line projection badly exaggerates RVOL just after the open. The
    piecewise curve is intentionally conservative and is only used while Yahoo
    reports a regular session; completed sessions always use 1.0.
    """
    from zoneinfo import ZoneInfo
    n = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
    minute = n.hour * 60 + n.minute + n.second / 60.0
    progress = max(0.0, min(1.0, (minute - 570.0) / 390.0))
    curve = ((0.0, 0.0), (30/390, 0.20), (90/390, 0.38),
             (195/390, 0.58), (300/390, 0.78), (1.0, 1.0))
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if progress <= x1:
            if x1 == x0:
                return y1
            return max(0.08, y0 + (y1 - y0) * (progress - x0) / (x1 - x0))
    return 1.0


def us_market_open(force_refresh: bool = False) -> bool:
    """Holiday/early-close aware U.S. market status, cached to avoid polling Yahoo."""
    global _MARKET_CACHE
    now = time.time()
    if not force_refresh and now - _MARKET_CACHE[0] < 45:
        return _MARKET_CACHE[1]
    try:
        status = (yf.Market("US", timeout=8).status or {}) if yf is not None else {}
        label = str(status.get("status") or "unknown").lower()
        is_open = label == "open"
        _MARKET_CACHE = (now, is_open, label)
    except Exception:
        # A provider outage must fail closed. It is safer to postpone a paper fill
        # than to pretend a holiday or early close is an active market.
        _MARKET_CACHE = (now, False, "unavailable")
    return _MARKET_CACHE[1]


def build_dossier(ticker: str) -> Dossier:
    """Collect the facts. No opinions here - just what is verifiably true."""
    d = Dossier(ticker=ticker.upper())
    if yf is None:
        d.error = "yfinance not installed"
        return d
    try:
        t = yf.Ticker(d.ticker)
        info = t.info or {}
        d.name = str(info.get("shortName") or info.get("longName") or "")
        d.sector = str(info.get("sector") or "")
        d.exchange = str(info.get("exchange") or "").upper()
        d.quote_type = str(info.get("quoteType") or "").upper()
        d.market_state = str(info.get("marketState") or "UNKNOWN").upper()
        d.price = _safe(info, "currentPrice", "regularMarketPrice", "previousClose")
        prev = _safe(info, "regularMarketPreviousClose", "previousClose") or d.price
        d.change_pct = ((d.price / prev - 1) * 100) if prev else 0.0
        d.avg_volume = _safe(info, "averageVolume", "averageVolume10days")
        d.volume = _safe(info, "volume", "regularMarketVolume")
        d.raw_volume_ratio = (d.volume / d.avg_volume) if d.avg_volume else 0.0
        volume_fraction = _expected_volume_fraction() if d.market_state == "REGULAR" else 1.0
        d.volume_surge = d.raw_volume_ratio / max(volume_fraction, 0.08)
        d.market_cap = _safe(info, "marketCap")
        d.float_shares = _safe(info, "floatShares")
        d.shares_outstanding = _safe(info, "sharesOutstanding")
        d.cash_known = _known(info, "totalCash")
        d.debt_known = _known(info, "totalDebt")
        d.revenue_known = _known(info, "totalRevenue")
        d.op_cashflow_known = _known(info, "operatingCashflow")
        d.cash = _safe(info, "totalCash")
        d.debt = _safe(info, "totalDebt")
        d.revenue = _safe(info, "totalRevenue")
        d.op_cashflow = _safe(info, "operatingCashflow")

        d.bid, d.ask = _safe(info, "bid"), _safe(info, "ask")
        quote_age = _age_hours(info.get("regularMarketTime"))
        d.quote_age_min = quote_age * 60.0 if quote_age >= 0 else -1.0
        if d.bid > 0 and d.ask > 0 and d.ask >= d.bid:
            d.spread_pct = (d.ask - d.bid) / ((d.ask + d.bid) / 2) * 100
        d.spread_reliable = bool(
            d.market_state == "REGULAR"
            and 0 <= d.quote_age_min <= MAX_EXECUTION_QUOTE_AGE_MIN
            and d.bid > 0 and d.ask > d.bid
        )
        hi52 = _safe(info, "fiftyTwoWeekHigh")
        if hi52 > 0 and d.price > 0:
            d.off_52w_high_pct = (d.price / hi52 - 1) * 100

        # cash runway: quarters of burn left before they must raise (= dilute you)
        if d.op_cashflow_known and d.cash_known and d.op_cashflow < 0 and d.cash > 0:
            d.runway_quarters = d.cash / (abs(d.op_cashflow) / 4.0)
            d.runway_known = True
        elif d.op_cashflow_known and d.op_cashflow >= 0:
            d.runway_quarters = 99.0
            d.runway_known = True

        # dilution: compare share count now vs a year ago
        try:
            bs = t.quarterly_balance_sheet
            if bs is not None and not bs.empty:
                for key in ("Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"):
                    if key in bs.index:
                        row = bs.loc[key].dropna()
                        if len(row) >= 2:
                            newest, oldest = float(row.iloc[0]), float(row.iloc[-1])
                            if oldest > 0:
                                d.shares_change_pct = (newest / oldest - 1) * 100
                                d.shares_change_known = True
                        break
        except Exception:
            pass

        d.data_completeness = round(100.0 * sum((
            d.cash_known, d.debt_known, d.revenue_known,
            d.op_cashflow_known, d.shares_change_known,
        )) / 5.0, 1)

        # Causal price/volume confirmation from repaired historical bars. Unlike
        # analyst targets, these fields were all observable at decision time.
        try:
            hist = t.history(period="3mo", interval="1d", prepost=False,
                             auto_adjust=False, actions=True, repair=True,
                             timeout=10, raise_errors=True)
            if hist is not None and not hist.empty and "Close" in hist:
                hist = hist.dropna(subset=["Close"])
                closes = hist["Adj Close"] if "Adj Close" in hist else hist["Close"]
                closes = closes.dropna()
                if len(closes) >= 21 and d.price > 0:
                    d.technical_known = True
                    d.return_5d_pct = (d.price / float(closes.iloc[-6]) - 1) * 100
                    d.return_20d_pct = (d.price / float(closes.iloc[-21]) - 1) * 100
                    sma20 = float(closes.iloc[-20:].mean())
                    d.sma20_distance_pct = (d.price / sma20 - 1) * 100 if sma20 else 0.0
                    high20 = float(hist["High"].dropna().iloc[-20:].max())
                    d.high20_distance_pct = (d.price / high20 - 1) * 100 if high20 else 0.0
                    highs = hist["High"].astype(float)
                    lows = hist["Low"].astype(float)
                    prior = hist["Close"].astype(float).shift(1)
                    tr = (highs - lows).to_frame("hl")
                    tr["hp"] = (highs - prior).abs()
                    tr["lp"] = (lows - prior).abs()
                    atr14 = float(tr.max(axis=1).dropna().iloc[-14:].mean())
                    d.atr_pct = atr14 / d.price * 100 if d.price else 0.0
                hist_vol = hist["Volume"].dropna() if "Volume" in hist else None
                if hist_vol is not None and len(hist_vol) >= 6:
                    prior_vol = hist_vol.iloc[-21:-1] if len(hist_vol) >= 21 else hist_vol.iloc[:-1]
                    typical = float(prior_vol.median()) if len(prior_vol) else 0.0
                    if typical > 0:
                        d.raw_volume_ratio = d.volume / typical
                        d.volume_surge = d.raw_volume_ratio / max(volume_fraction, 0.08)
        except Exception:
            pass

        open_px = _safe(info, "regularMarketOpen")
        day_hi = _safe(info, "regularMarketDayHigh", "dayHigh")
        day_lo = _safe(info, "regularMarketDayLow", "dayLow")
        if open_px > 0 and prev > 0:
            d.gap_pct = (open_px / prev - 1) * 100
            d.from_open_pct = (d.price / open_px - 1) * 100
        if day_hi > day_lo > 0:
            d.close_location = max(0.0, min(1.0, (d.price - day_lo) / (day_hi - day_lo)))

        # ---- catalysts: the dated events that actually move these names ----
        short_raw = _safe(info, "shortPercentOfFloat") * 100
        inst_raw = _safe(info, "heldPercentInstitutions") * 100
        insider_raw = _safe(info, "heldPercentInsiders") * 100
        d.short_pct_float = short_raw if 0 <= short_raw <= 100 else 0.0
        d.inst_pct = inst_raw if 0 <= inst_raw <= 100 else 0.0
        d.insider_pct = insider_raw if 0 <= insider_raw <= 100 else 0.0
        d.analyst_target = _safe(info, "targetMeanPrice")
        d.recommendation = str(info.get("recommendationKey") or "")
        if d.analyst_target > 0 and d.price > 0:
            d.analyst_upside_pct = (d.analyst_target / d.price - 1) * 100
        try:
            cal = t.calendar
            ed = None
            if isinstance(cal, dict):
                ed = (cal.get("Earnings Date") or [None])
                ed = ed[0] if isinstance(ed, (list, tuple)) and ed else ed
            elif cal is not None and hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.index:
                ed = cal.loc["Earnings Date"][0]
            if ed is not None:
                import datetime as _dt
                if hasattr(ed, "strftime"):
                    d.earnings_date = ed.strftime("%Y-%m-%d")
                    d.days_to_earnings = (ed - _dt.date.today()).days if isinstance(ed, _dt.date) and not isinstance(ed, _dt.datetime) \
                        else (ed.date() - _dt.date.today()).days
                else:
                    d.earnings_date = str(ed)[:10]
        except Exception:
            pass

        if 0 <= d.days_to_earnings <= 30:
            d.catalysts.append(f"EARNINGS in {d.days_to_earnings:.0f} days ({d.earnings_date}) - dated, binary event")
        if d.short_pct_float >= 20:
            d.catalysts.append(f"HIGH SHORT INTEREST {d.short_pct_float:.1f}% of float - squeeze potential (cuts both ways)")
        elif d.short_pct_float >= 10:
            d.catalysts.append(f"short interest {d.short_pct_float:.1f}% of float")
        if d.analyst_upside_pct > 50 and d.analyst_target > 0:
            d.catalysts.append(f"analyst target ${d.analyst_target:.2f} = {d.analyst_upside_pct:+.0f}% (targets on micro-caps are unreliable)")
        if d.inst_pct >= 30:
            d.catalysts.append(f"institutional ownership {d.inst_pct:.0f}% - unusual for a penny stock, mildly validating")
        if d.insider_pct >= 20:
            d.catalysts.append(f"insiders hold {d.insider_pct:.0f}% - aligned, but also illiquid")
        if d.volume_surge >= 3:
            d.catalysts.append(f"volume {d.volume_surge:.1f}x normal today - something is happening; find out WHAT before acting")

        try:
            news_items = t.get_news(count=10, tab="all") or []
            ages = []
            for n in news_items[:10]:
                c = n.get("content", n)
                title = c.get("title") or n.get("title") or ""
                pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else n.get("publisher", "")
                when = c.get("pubDate") or n.get("providerPublishTime") or ""
                age = _age_hours(when)
                if title:
                    d.news.append({"title": str(title)[:180], "publisher": str(pub)[:40],
                                   "when": str(when)[:25], "age_hours": round(age, 1)})
                    if age >= 0:
                        ages.append(age)
            d.fresh_news_count = sum(1 for age in ages if age <= FRESH_NEWS_HOURS)
            d.latest_news_age_hours = min(ages) if ages else -1.0
        except Exception:
            pass

        if d.fresh_news_count:
            d.catalysts.append(f"{d.fresh_news_count} headline(s) in the last {FRESH_NEWS_HOURS:.0f}h")

        # Filing forms are more reliable than promotional headlines. A recent
        # registration/prospectus is not proof of an immediate sale, but it is a
        # real dilution overhang and must be visible to both scoring and the AI.
        try:
            for filing in (t.get_sec_filings() or [])[:20]:
                age_days = _age_hours(filing.get("date") or filing.get("epochDate")) / 24.0
                if age_days < 0 or age_days > 180:
                    continue
                form = str(filing.get("type") or "").upper()
                item = {"date": str(filing.get("date") or "")[:10], "type": form,
                        "title": str(filing.get("title") or "")[:100],
                        "url": str(filing.get("edgarUrl") or ""),
                        "age_days": round(age_days, 1)}
                d.recent_filings.append(item)
                if form.startswith(OFFERING_FORMS) and age_days <= 90:
                    d.recent_offering = True
                    if d.latest_offering_age_days < 0 or age_days < d.latest_offering_age_days:
                        d.latest_offering_age_days = age_days
        except Exception:
            pass

        # Yahoo's filing list is useful as a fallback, but it drops the SEC's exact
        # acceptance timestamp and 8-K item codes.  The official current-filings feed
        # supplies both.  Item codes matter: an earnings release (2.02), a delisting
        # notice (3.01), and bankruptcy (1.03) must never receive the same catalyst
        # score merely because all three arrived on Form 8-K.
        try:
            official_events = sec_edgar.current_8k_for_symbol(
                d.ticker, max_age_hours=FRESH_NEWS_HOURS
            )
            seen_accessions = {
                str(x.get("accessionNumber") or "") for x in d.recent_filings
                if x.get("accessionNumber")
            }
            item_codes: set[str] = set()
            adverse_codes: set[str] = set()
            for event in official_events:
                age_hours = float(event.get("age_hours") or 0.0)
                items = [str(x) for x in (event.get("items") or [])]
                item_codes.update(items)
                adverse_codes.update(str(x) for x in (event.get("negative_items") or []))
                accession = str(event.get("accessionNumber") or "")
                if accession and accession in seen_accessions:
                    continue
                d.recent_filings.append({
                    "date": str(event.get("accepted_at") or "")[:10],
                    "accepted_at": str(event.get("accepted_at") or ""),
                    "type": "8-K",
                    "title": str(event.get("company") or "")[:100],
                    "url": str(event.get("url") or ""),
                    "age_days": round(age_hours / 24.0, 2),
                    "age_hours": round(age_hours, 2),
                    "items": items,
                    "accessionNumber": accession,
                    "official_sec": True,
                })
            if official_events:
                d.sec_8k_verified = True
                d.latest_sec_8k_age_hours = min(
                    float(x.get("age_hours") or 0.0) for x in official_events
                )
                d.recent_8k_items = sorted(item_codes)
                d.adverse_8k_items = sorted(adverse_codes)
                material = sorted(item_codes & (
                    set(sec_edgar.EARNINGS_8K_ITEMS)
                    | set(sec_edgar.AGREEMENT_8K_ITEMS)
                    | {"1.05", "7.01", "8.01"}
                ))
                if material:
                    d.catalysts.append(
                        "SEC 8-K verified " + ", ".join(material)
                        + f" ({d.latest_sec_8k_age_hours:.1f}h ago)"
                    )
            d.recent_filings.sort(
                key=lambda x: float(x.get("age_days", 1e9))
            )
        except Exception:
            # The SEC feed improves classification and discovery, but a transient
            # outage must not erase the rest of the dossier.  It also cannot silently
            # authorize a trade: ``sec_8k_verified`` remains false.
            pass

        split_age_days = _age_hours(info.get("lastSplitDate")) / 24.0
        split_factor = str(info.get("lastSplitFactor") or "")
        try:
            left, right = (float(x) for x in split_factor.split(":", 1))
            d.recent_reverse_split = 0 <= split_age_days <= 730 and left < right
        except (TypeError, ValueError):
            pass
    except Exception as e:
        d.error = f"{type(e).__name__}: {str(e)[:120]}"
        return d

    d.flags = risk_flags(d)
    return d


def risk_flags(d: Dossier) -> list[str]:
    """The things that actually blow up penny-stock trades."""
    f = []
    spread, estimated = effective_spread(d)
    if spread >= 3:
        label = "estimated spread" if estimated else "WIDE SPREAD"
        f.append(f"{label} {spread:.1f}% - round-trip cost is a major hurdle")
    elif spread >= 1:
        f.append(f"{'estimated ' if estimated else ''}spread {spread:.1f}% - meaningful cost drag")
    if d.quote_type and d.quote_type != "EQUITY":
        f.append(f"NOT COMMON EQUITY ({d.quote_type})")
    if d.exchange and d.exchange not in LISTED_EXCHANGES:
        f.append(f"UNSUPPORTED/OTC EXCHANGE {d.exchange} - weaker disclosure and execution quality")
    if d.avg_volume and d.avg_volume < MIN_AVG_VOLUME:
        f.append(f"THIN LIQUIDITY avg vol {d.avg_volume:,.0f} - hard to exit at your price")
    if not d.shares_change_known:
        f.append("DILUTION DATA UNAVAILABLE - do not interpret zero as no dilution")
    elif d.shares_change_pct > 25:
        f.append(f"HEAVY DILUTION share count +{d.shares_change_pct:.0f}% - your stake is being shrunk")
    elif d.shares_change_pct > 10:
        f.append(f"dilution +{d.shares_change_pct:.0f}% share count")
    if not d.runway_known:
        f.append("CASH RUNWAY UNKNOWN - balance-sheet confidence is limited")
    elif 0 < d.runway_quarters < 2:
        f.append(f"CASH CRISIS ~{d.runway_quarters:.1f} quarters of runway - a dilutive raise is likely imminent")
    elif 2 <= d.runway_quarters < 4:
        f.append(f"short runway ~{d.runway_quarters:.1f} quarters")
    if d.revenue_known and d.revenue <= 0:
        f.append("NO REVENUE - story stock; value rests entirely on promises")
    if d.recent_offering:
        f.append(f"RECENT OFFERING FILING {d.latest_offering_age_days:.0f}d ago - dilution overhang")
    if d.adverse_8k_items:
        f.append(
            "ADVERSE SEC 8-K ITEM(S) " + ", ".join(d.adverse_8k_items)
            + " - bankruptcy/delisting/dilution/restatement risk"
        )
    if d.recent_reverse_split:
        f.append("REVERSE SPLIT within 2 years - recurring listing/value-destruction risk")
    if d.float_shares and d.float_shares < 20_000_000 and d.volume_surge > 5:
        f.append(f"PUMP SIGNATURE - tiny float ({d.float_shares/1e6:.1f}M) + {d.volume_surge:.1f}x volume spike")
    if d.debt > 0 and d.cash > 0 and d.debt > 3 * d.cash:
        f.append(f"debt {d.debt/1e6:.0f}M vs cash {d.cash/1e6:.0f}M")
    if d.price < 0.5:
        f.append("sub-$0.50 - delisting/reverse-split risk")
    if d.off_52w_high_pct < -80:
        f.append(f"down {abs(d.off_52w_high_pct):.0f}% from 52w high - falling knife unless something changed")
    return f


# ============================ RANKING ENGINE ============================
# Three independent scores. Ranking on hype alone would systematically buy tops,
# because a pump and real momentum look identical at the entry. Quality decides
# what survives; tradeability decides whether you can transact at all.

# ---------------------------------------------------------------------------
# MARKET REGIME
# ---------------------------------------------------------------------------
# Penny stocks are the highest-beta corner of the market. The same breakout that
# works in a risk-on tape fails repeatedly when small caps are being sold, because
# the marginal buyer disappears. Nothing else in this bot looked at the tape, so a
# perfect-looking setup scored identically on a day the whole complex was bleeding.
# IWM (Russell 2000) is the reference: it IS the small-cap bid.
# When True a setup must ALSO be confirmed by the analyst model before it can
# become a BUY. Safer, but these free models are conservative and inconsistent
# across fallback tiers, so this currently yields very few signals. Set False to
# let a strong mechanical setup stand on its own (the model can still VETO either
# way). Which is better is an empirical question - the forward tracker will answer
# it; do not flip this on a hunch.
REQUIRE_AI_CONFIRM = os.getenv("PENNY_REQUIRE_AI_CONFIRM", "1") == "1"

_REGIME_CACHE: dict = {"t": 0.0, "data": None}
_REGIME_TTL = 900          # 15 min - regime does not change minute to minute


def market_regime(force: bool = False) -> dict:
    """Risk appetite for small caps. Returns score 0-100, label and reasons."""
    now = time.time()
    if not force and _REGIME_CACHE["data"] and now - _REGIME_CACHE["t"] < _REGIME_TTL:
        return _REGIME_CACHE["data"]
    out = {"score": 50.0, "label": "unknown", "why": ["regime data unavailable"],
           "iwm_5d": 0.0, "iwm_vs_sma20": 0.0, "iwm_price": 0.0,
           "known": False}
    if yf is None:
        return out
    try:
        h = yf.Ticker("IWM").history(period="3mo", interval="1d", auto_adjust=True, repair=True)
        if h is None or len(h) < 25:
            _REGIME_CACHE.update({"t": now, "data": out}); return out
        c = h["Close"].astype(float)
        last = float(c.iloc[-1])
        sma20 = float(c.tail(20).mean())
        r5 = (last / float(c.iloc[-6]) - 1) * 100 if len(c) > 6 else 0.0
        r20 = (last / float(c.iloc[-21]) - 1) * 100 if len(c) > 21 else 0.0
        vs_sma = (last / sma20 - 1) * 100 if sma20 else 0.0

        pts, why = 50.0, []
        if vs_sma >= 2:    pts += 20; why.append(f"IWM {vs_sma:+.1f}% vs 20d avg (risk-on)")
        elif vs_sma >= 0:  pts += 8;  why.append(f"IWM {vs_sma:+.1f}% vs 20d avg")
        elif vs_sma >= -3: pts -= 12; why.append(f"IWM {vs_sma:.1f}% below 20d avg")
        else:              pts -= 28; why.append(f"IWM {vs_sma:.1f}% below 20d avg (risk-off)")
        if r5 >= 2:    pts += 14; why.append(f"small caps +{r5:.1f}% in 5d")
        elif r5 <= -4: pts -= 22; why.append(f"small caps {r5:.1f}% in 5d (selling)")
        elif r5 < 0:   pts -= 8
        if r20 <= -8:  pts -= 12; why.append(f"small caps {r20:.1f}% in 20d (downtrend)")
        elif r20 >= 5: pts += 8

        score = max(0.0, min(100.0, pts))
        label = ("risk-on" if score >= 65 else "neutral" if score >= 42 else "risk-off")
        out = {"score": round(score, 1), "label": label, "why": why,
               "iwm_5d": round(r5, 2), "iwm_vs_sma20": round(vs_sma, 2),
               "iwm_price": round(last, 4), "known": True}
    except Exception as e:
        out["why"] = [f"regime lookup failed: {type(e).__name__}"]
    _REGIME_CACHE.update({"t": now, "data": out})
    return out


def hype_score(d: "Dossier") -> tuple[float, list]:
    """0-100: is this stock IN PLAY right now? Drivers day-traders actually use."""
    pts, why = 0.0, []
    # relative volume - the single best "in play" marker
    rv = d.volume_surge
    if rv >= 10:   pts += 30; why.append(f"RVOL {rv:.1f}x (extreme)")
    elif rv >= 5:  pts += 24; why.append(f"RVOL {rv:.1f}x (very high)")
    elif rv >= 3:  pts += 18; why.append(f"RVOL {rv:.1f}x (high)")
    elif rv >= 1.5: pts += 10; why.append(f"RVOL {rv:.1f}x")
    # float rotation - how many times the tradeable float changed hands
    if d.float_shares > 0:
        rot = d.volume / d.float_shares
        if rot >= 1.0:   pts += 22; why.append(f"float rotated {rot:.1f}x today")
        elif rot >= 0.5: pts += 16; why.append(f"{rot:.1f}x float turnover")
        elif rot >= 0.2: pts += 9;  why.append(f"{rot:.2f}x float turnover")
    # today's move
    ch = d.change_pct
    if ch >= 30:   pts += 20; why.append(f"+{ch:.0f}% today")
    elif ch >= 15: pts += 16; why.append(f"+{ch:.0f}% today")
    elif ch >= 5:  pts += 11; why.append(f"+{ch:.0f}% today")
    elif ch > 0:   pts += 5
    elif ch < -10: pts -= 8;  why.append(f"{ch:.0f}% today (falling)")
    # squeeze fuel
    if d.short_pct_float >= 20:   pts += 14; why.append(f"short {d.short_pct_float:.0f}% of float")
    elif d.short_pct_float >= 10: pts += 8;  why.append(f"short {d.short_pct_float:.0f}%")
    # small float = violent moves
    if 0 < d.float_shares < 20_000_000:  pts += 9; why.append(f"micro float {d.float_shares/1e6:.1f}M")
    elif 0 < d.float_shares < 50_000_000: pts += 5
    # fresh news flow
    if d.fresh_news_count >= 4: pts += 7; why.append(f"{d.fresh_news_count} fresh headlines")
    elif d.fresh_news_count >= 1: pts += 3; why.append(f"{d.fresh_news_count} fresh headline(s)")
    return max(0.0, min(100.0, pts)), why


def quality_score(d: "Dossier") -> tuple[float, list]:
    """0-100: will the company still exist, and will your slice keep its value?"""
    pts, why = 45.0, []
    if not d.runway_known:
        pts -= 10; why.append("cash runway unknown")
    elif d.runway_quarters >= 8:   pts += 18; why.append("well funded (2y+ runway)")
    elif d.runway_quarters >= 4: pts += 10; why.append(f"{d.runway_quarters:.1f}q runway")
    elif 0 < d.runway_quarters < 2: pts -= 28; why.append(f"only {d.runway_quarters:.1f}q cash - raise imminent")
    elif 2 <= d.runway_quarters < 4: pts -= 10; why.append(f"{d.runway_quarters:.1f}q runway is short")
    if not d.shares_change_known:
        pts -= 10; why.append("dilution history unknown")
    elif d.shares_change_pct > 50:   pts -= 30; why.append(f"share count +{d.shares_change_pct:.0f}% (heavy dilution)")
    elif d.shares_change_pct > 20: pts -= 16; why.append(f"dilution +{d.shares_change_pct:.0f}%")
    elif abs(d.shares_change_pct) <= 2: pts += 10; why.append("no meaningful dilution found")
    if not d.revenue_known:
        pts -= 5; why.append("revenue data unknown")
    elif d.revenue > 50_000_000:   pts += 16; why.append(f"revenue ${d.revenue/1e6:.0f}M")
    elif d.revenue > 5_000_000:  pts += 9;  why.append(f"revenue ${d.revenue/1e6:.1f}M")
    elif d.revenue <= 0:         pts -= 18; why.append("no revenue (story stock)")
    if d.inst_pct >= 30: pts += 10; why.append(f"institutions {d.inst_pct:.0f}%")
    elif d.inst_pct >= 10: pts += 5
    if d.cash_known and d.debt_known and d.cash > 0 and d.debt > 3 * d.cash:
        pts -= 12; why.append("debt >3x cash")
    if d.op_cashflow_known and d.op_cashflow > 0: pts += 12; why.append("cash-flow positive")
    if d.recent_offering: pts -= 18; why.append("recent offering/prospectus filing")
    if d.recent_reverse_split: pts -= 18; why.append("recent reverse split")
    return max(0.0, min(100.0, pts)), why


def effective_spread(d: "Dossier") -> tuple[float, bool]:
    """Return (cost proxy, estimated).

    Only a fresh regular-session bid/ask is called a quote. Outside that window we
    use an ADV-derived *ranking proxy*, never pretend the stale book is executable.
    """
    quoted = d.spread_pct
    adv = d.avg_volume * d.price
    if adv >= 50_000_000:   est = 0.25
    elif adv >= 10_000_000: est = 0.55
    elif adv >= 2_000_000:  est = 1.20
    elif adv >= 500_000:    est = 2.50
    else:                  est = 6.00

    # A quote that contradicts the name's own liquidity is a data artifact, not a price.
    # yfinance regularly hands back a one-sided or cross-venue book (bid from one venue,
    # ask stale from another) that passes every freshness check yet implies a 40%+ round
    # trip on a name doing $10M a day. Trusting it silently buries the best setups:
    # on 2026-08-07 EMBC scored hype 51 / tech 100 / qual 99 at RVOL 9.7x and ranked 17th
    # of 20 solely because of a bogus 46.6% "reliable" spread. Two other names on the same
    # board carried 46.3% and 41.8% - three hits clustered in one range is a systematic
    # artifact, not three coincidentally untradeable stocks.
    # The mirror case is just as wrong: an exact 0.00% spread means bid == ask, a locked
    # market that cannot persist, and it hands the name a free 100 tradeability score.
    implausible = quoted <= 0 or quoted > max(4.0 * est, est + 3.0)
    if d.spread_reliable and not implausible:
        return quoted, False
    return est, True


def trusted_execution_quote(d: "Dossier") -> bool:
    """Whether the free feed is good enough for confirmation or a paper fill.

    ``spread_reliable`` alone is insufficient: an apparently fresh but locked or
    liquidity-contradicting Yahoo book is deliberately replaced by an ADV proxy in
    ``effective_spread``.  A proxy is useful for ranking, never for execution.
    """
    _spread, estimated = effective_spread(d)
    return bool(
        d.market_state == "REGULAR"
        and d.spread_reliable
        and not estimated
        and 0 <= d.quote_age_min <= MAX_EXECUTION_QUOTE_AGE_MIN
        and d.bid > 0
        and d.ask > d.bid
    )


def tradeability(d: "Dossier") -> tuple[float, list]:
    """0-100: can you actually get in and out without the costs eating the trade?"""
    pts, why = 100.0, []
    sp, estimated = effective_spread(d)
    if estimated:
        why.append(f"cost proxy ~{sp:.2f}% (estimated from ${d.avg_volume*d.price/1e6:.1f}M ADV; "
                   f"not an executable quote)")
    if sp > 8:   pts -= 70; why.append(f"spread {sp:.1f}% - round trip is brutal")
    elif sp > 4: pts -= 40; why.append(f"spread {sp:.1f}%")
    elif sp > 2: pts -= 18; why.append(f"spread {sp:.1f}%")
    elif sp > 0 and not estimated: why.append(f"tight spread {sp:.2f}%")
    dollar_vol = d.avg_volume * d.price
    if dollar_vol < 500_000:    pts -= 40; why.append(f"only ${dollar_vol/1e6:.2f}M ADV - thin")
    elif dollar_vol < 2_000_000: pts -= 15; why.append(f"${dollar_vol/1e6:.1f}M ADV")
    else: why.append(f"${dollar_vol/1e6:.1f}M ADV - liquid")
    if d.price < 0.3: pts -= 15; why.append("sub-$0.30 (delisting risk)")
    if d.exchange and d.exchange not in LISTED_EXCHANGES:
        pts -= 50; why.append(f"{d.exchange} is not a supported primary U.S. listing")
    return max(0.0, min(100.0, pts)), why


def technical_score(d: "Dossier") -> tuple[float, list]:
    """0-100: confirmation that a move is holding rather than immediately fading."""
    if not d.technical_known:
        return 25.0, ["insufficient repaired price history"]
    pts, why = 50.0, []
    if d.from_open_pct >= 3: pts += 12; why.append(f"{d.from_open_pct:+.1f}% from open")
    elif d.from_open_pct <= -3: pts -= 18; why.append(f"fading {d.from_open_pct:.1f}% from open")
    if d.close_location >= 0.75: pts += 15; why.append("holding near session high")
    elif d.close_location <= 0.35: pts -= 18; why.append("in lower third of session range")
    if d.sma20_distance_pct >= 0: pts += 10; why.append("above 20-day average")
    else: pts -= 8; why.append("below 20-day average")
    if -2 <= d.high20_distance_pct <= 1: pts += 10; why.append("at 20-day breakout area")
    if 0 <= d.return_5d_pct <= 25: pts += 8
    elif d.return_5d_pct > 50: pts -= 15; why.append(f"+{d.return_5d_pct:.0f}% in 5d (overextended)")
    elif d.return_5d_pct < -10: pts -= 10; why.append(f"{d.return_5d_pct:.0f}% in 5d")
    if d.gap_pct >= 20 and d.from_open_pct < 0:
        pts -= 25; why.append("large gap is fading")
    if d.atr_pct > 25:
        pts -= 12; why.append(f"ATR {d.atr_pct:.0f}% (extreme gap/slippage risk)")
    return max(0.0, min(100.0, pts)), why


def catalyst_alignment(d: "Dossier") -> dict:
    """Match a news headline to an official material 8-K in event time.

    A press release can be promotional and an item code does not reveal direction.
    Requiring both makes each source corroborate the other's missing piece: EDGAR proves
    a material disclosure happened, while the headline gives the AI text it can assess.
    The match is deliberately temporal rather than semantic; the LLM remains a veto and
    is never allowed to promote an unmatched event.
    """
    material_items = (
        set(sec_edgar.EARNINGS_8K_ITEMS)
        | set(sec_edgar.AGREEMENT_8K_ITEMS)
        | {"1.05", "7.01", "8.01"}
    )
    events = []
    for filing in d.recent_filings or []:
        if not filing.get("official_sec") or str(filing.get("type") or "").upper() != "8-K":
            continue
        items = sorted(set(str(x) for x in (filing.get("items") or [])) & material_items)
        if not items:
            continue
        try:
            age = float(filing.get("age_hours"))
        except (TypeError, ValueError):
            try:
                age = float(filing.get("age_days")) * 24.0
            except (TypeError, ValueError):
                continue
        if 0 <= age <= FRESH_NEWS_HOURS:
            events.append((age, filing, items))

    headlines = []
    for news in d.news or []:
        try:
            age = float(news.get("age_hours"))
        except (TypeError, ValueError):
            continue
        if 0 <= age <= FRESH_NEWS_HOURS and str(news.get("title") or "").strip():
            headlines.append((age, news))

    pairs = []
    if not d.adverse_8k_items:
        for event_age, filing, items in events:
            for news_age, news in headlines:
                gap = abs(event_age - news_age)
                if gap <= CATALYST_ALIGNMENT_HOURS:
                    pairs.append((gap, max(event_age, news_age), event_age, news_age,
                                  filing, items, news))
    if not pairs:
        return {
            "aligned": False,
            "reason": ("adverse SEC item present" if d.adverse_8k_items else
                       "no headline matched a non-adverse material 8-K within "
                       f"{CATALYST_ALIGNMENT_HOURS:.0f}h"),
            "material_event_count": len(events),
            "fresh_headline_count": len(headlines),
        }

    _gap, _freshness, event_age, news_age, filing, items, news = min(
        pairs, key=lambda value: (value[0], value[1])
    )
    return {
        "aligned": True,
        "gap_hours": round(abs(event_age - news_age), 2),
        "event_age_hours": round(event_age, 2),
        "news_age_hours": round(news_age, 2),
        "accession": str(filing.get("accessionNumber") or ""),
        "accepted_at": str(filing.get("accepted_at") or ""),
        "items": items,
        "headline": str(news.get("title") or "")[:180],
        "publisher": str(news.get("publisher") or "")[:60],
        "material_event_count": len(events),
        "fresh_headline_count": len(headlines),
    }


def catalyst_score(d: "Dossier") -> tuple[float, list]:
    """0-100: corroborated dated information, with offering risk deducted."""
    pts, why = 0.0, []
    aligned = catalyst_alignment(d)
    if aligned.get("aligned"):
        freshness = max(float(aligned["event_age_hours"]), float(aligned["news_age_hours"]))
        if freshness <= 24:
            pts += 45
        else:
            pts += 28
        why.append(
            f"headline + official 8-K aligned within {aligned['gap_hours']:.1f}h "
            f"(items {', '.join(aligned['items'])})"
        )
    elif d.fresh_news_count:
        why.append("fresh headline is not corroborated by a time-aligned material SEC 8-K")
    if 0 <= d.days_to_earnings <= 7:
        why.append(f"earnings in {d.days_to_earnings:.0f}d (binary risk, not published news)")
    items = set(d.recent_8k_items or [])
    if d.sec_8k_verified and 0 <= d.latest_sec_8k_age_hours <= FRESH_NEWS_HOURS:
        if items & set(sec_edgar.EARNINGS_8K_ITEMS):
            why.append("SEC item 2.02 verified; direction not proven by item code")
        if items & set(sec_edgar.AGREEMENT_8K_ITEMS):
            why.append("SEC agreement/transaction verified; terms still need review")
        if items & {"1.05", "7.01", "8.01"}:
            why.append("SEC material/Regulation FD disclosure; direction unverified")
    if d.adverse_8k_items:
        pts -= 60; why.append("adverse SEC item " + ", ".join(d.adverse_8k_items))
    if d.recent_offering:
        pts -= 35; why.append("recent offering/prospectus")
    return max(0.0, min(100.0, pts)), why


def has_dated_catalyst(d: "Dossier") -> bool:
    """True only for an observable, time-stamped event near the decision.

    A breakout is confirmation, not a cause.  The v1 audit showed that treating price
    and volume as a substitute for a catalyst had no gross expectancy and lost money
    after costs.  This gate keeps unexplained promotion/volume spikes in WATCH.
    """
    # A scheduled earnings date is future binary risk, an arbitrary headline may be a
    # promotion, and an item code does not say whether terms are good.  V4 therefore
    # needs a non-adverse material filing and a time-aligned headline; AI still has to
    # confirm direction afterwards.
    return bool(catalyst_alignment(d).get("aligned"))


def hard_risk_reason(d: "Dossier") -> str:
    """Fail-closed gates that neither a high score nor an LLM may override."""
    if d.error:
        return f"data error: {d.error}"
    if d.price <= 0:
        return "no valid price"
    if not MIN_PRICE < d.price < MAX_PRICE:
        return f"outside penny-stock price range (${d.price:.2f})"
    if d.quote_type and d.quote_type != "EQUITY":
        return f"unsupported security type {d.quote_type}"
    if d.exchange and d.exchange not in LISTED_EXCHANGES:
        return f"unsupported/OTC exchange {d.exchange}"
    if not d.avg_volume or d.avg_volume < MIN_AVG_VOLUME:
        return f"illiquid: avg volume {d.avg_volume:,.0f}"
    if d.data_completeness < 40:
        return f"insufficient fundamental data ({d.data_completeness:.0f}% complete)"
    if d.runway_known and 0 < d.runway_quarters < 2:
        return f"cash runway {d.runway_quarters:.1f}q"
    if d.shares_change_known and d.shares_change_pct > 40:
        return f"share dilution +{d.shares_change_pct:.0f}%"
    if d.adverse_8k_items:
        return "adverse SEC 8-K item(s): " + ", ".join(d.adverse_8k_items)
    spread, estimated = effective_spread(d)
    if not estimated and spread > 4:
        return f"live spread {spread:.1f}%"
    if d.gap_pct >= 20 and d.from_open_pct <= -3:
        return "large opening gap is already fading"
    if d.change_pct >= 40 and d.close_location < 0.60:
        return "extreme move is not holding near its high"
    return ""


def rank_score(d: "Dossier") -> dict:
    """Composite for the leaderboard (regime is applied at signal time, not here,
    so the ranking still reflects the stock's own merit)."""
    """Composite used for the leaderboard. Hype finds what is moving; quality and
    tradeability stop us buying a pump we cannot exit."""
    h, hw = hype_score(d)
    q, qw = quality_score(d)
    t, tw = tradeability(d)
    tech, techw = technical_score(d)
    cat, catw = catalyst_score(d)
    # tradeability is a MULTIPLIER, not an addend: if you cannot trade it, nothing
    # else matters, no matter how exciting the chart looks.
    composite = (0.35 * h + 0.30 * tech + 0.20 * q + 0.15 * cat) * (t / 100.0)
    return {"composite": round(composite, 1), "hype": round(h, 1), "quality": round(q, 1),
            "technical": round(tech, 1), "catalyst": round(cat, 1),
            "tradeability": round(t, 1), "hype_why": hw, "quality_why": qw,
            "technical_why": techw, "catalyst_why": catw, "trade_why": tw}


def mechanical_setup(d: "Dossier", r: dict) -> str:
    """Return the candidate tier before AI, regime, persistence, or edge policy."""
    comp, hype = float(r.get("composite", 0)), float(r.get("hype", 0))
    qual, tech = float(r.get("quality", 0)), float(r.get("technical", 0))
    cat = float(r.get("catalyst", 0))
    breakout = d.high20_distance_pct >= -2 and d.close_location >= 0.70
    dated_catalyst = has_dated_catalyst(d)
    if (comp >= 66 and hype >= 50 and tech >= 65 and qual >= 42
            and cat >= 35 and dated_catalyst and breakout):
        return "STRONG BUY"
    if (comp >= 56 and hype >= 35 and tech >= 58 and qual >= 35
            and cat >= 25 and dated_catalyst):
        return "BUY"
    return ""


def signal_from(d: "Dossier", r: dict, ai: dict | None) -> dict:
    """Turn evidence into a conservative signal. The LLM can veto, never promote."""
    ai = ai or {}
    comp, hype, qual, trade = r["composite"], r["hype"], r["quality"], r["tradeability"]
    tech, cat = r.get("technical", 0), r.get("catalyst", 0)
    verdict = (ai.get("verdict") or "").upper()
    conviction = str(ai.get("conviction") or "").lower()
    hard = hard_risk_reason(d)

    if hard:
        action, why = "NO TRADE", hard
    elif trade < 55:
        action, why = "NO TRADE", "cannot be traded at an acceptable cost"
    elif not d.technical_known:
        action, why = "WATCH", "price history is incomplete; no technical confirmation"
    else:
        dated_catalyst = has_dated_catalyst(d)
        # Momentum confirms a real event; it is no longer allowed to impersonate one.
        # This is deliberately stricter than v1 because the v1 price-only audit showed
        # -0.87% gross expectancy in the untouched test before costs.
        setup = mechanical_setup(d, r)
        strong_setup, buy_setup = setup == "STRONG BUY", setup == "BUY"
        if not (strong_setup or buy_setup):
            if comp >= 38 and not dated_catalyst:
                action, why = "WATCH", "price/volume move has no dated catalyst; do not chase it"
            else:
                action, why = (("WATCH", "setup needs stronger confirmation")
                               if comp >= 38 else ("AVOID", "risks dominate"))
        elif not ai:
            action, why = "WATCH", "mechanically eligible, awaiting independent AI review"
        elif verdict == "AVOID":
            action, why = "AVOID", "independent analyst found a decisive risk"
        elif REQUIRE_AI_CONFIRM and (verdict != "SPECULATIVE_BUY"
                                     or conviction not in ("medium", "high")):
            action, why = "WATCH", "analyst did not confirm a speculative buy"
        elif strong_setup:
            action, why = "STRONG BUY", "catalyst, momentum, quality, and price confirmation align"
        else:
            action, why = "BUY", "confirmed momentum with acceptable structure and execution"

    # ---- market-regime gate -------------------------------------------------
    # Applied last, so it can only ever REDUCE risk. Penny stocks are the highest
    # beta thing you can own: the identical breakout fails far more often when the
    # small-cap bid has gone. IWM is the reference for that bid.
    reg = market_regime()
    if reg.get("known") and action in ("BUY", "STRONG BUY"):
        if reg["score"] < 30:
            action, why = "WATCH", f"tape is risk-off ({reg['label']}) - standing aside"
        elif reg["score"] < 42:
            if action == "STRONG BUY":
                action, why = "BUY", f"downgraded: strong setup but tape is {reg['label']}"
            else:
                action, why = "WATCH", f"weak tape ({reg['label']}) - needs a stronger setup"

    # A score is not an edge.  The independent research audit is the final gate
    # between an interesting setup and anything labelled/traded as a buy.
    candidate_action = action
    policy = edge_policy()
    policy_strategy = str(
        policy.get("strategy_id") or policy.get("selected_strategy") or ""
    )
    execution_authorized = bool(
        policy.get("auto_trade_allowed") and policy_strategy == LIVE_STRATEGY_ID
    )
    if candidate_action in ("BUY", "STRONG BUY") and not execution_authorized:
        action = "RESEARCH"
        why = (
            f"unvalidated {candidate_action.lower()} candidate; edge audit is "
            f"{policy['status'].lower()}, so this is tracked but not traded"
        )

    px = d.price
    risk_pct = max(7.0, min(15.0, (d.atr_pct * 1.15) if d.atr_pct > 0 else 10.0))
    stop = round(px * (1 - risk_pct / 100.0), 4)
    target1_pct = risk_pct * 1.5
    target2_pct = risk_pct * 2.5
    return {"action": action, "candidate_action": candidate_action, "why": why,
            "entry": round(px, 4), "stop": stop,
            "target1": round(px * (1 + target1_pct / 100.0), 4),
            "target2": round(px * (1 + target2_pct / 100.0), 4),
            "risk_pct": round(risk_pct, 1), "reward_pct": round(target2_pct, 1),
            "needs_open_recheck": not trusted_execution_quote(d),
            "strategy_id": LIVE_STRATEGY_ID,
            "regime": reg.get("label", "unknown"), "regime_score": reg.get("score", 50.0),
            "benchmark_price": reg.get("iwm_price", 0.0),
            "edge_status": policy["status"],
            "auto_trade_allowed": execution_authorized}


SYSTEM_PROMPT = """You are a sceptical micro-cap equity analyst. You know most penny
stocks lose money, that promotion is common, and that dilution and spreads destroy
retail returns. But you are an ANALYST, not a refusal machine: when a setup is
genuinely attractive you must say so and explain why.

Judge ONLY the supplied dossier. Never invent numbers, filings or catalysts.
Missing data is UNKNOWN, never evidence that the company has no debt/dilution risk.
Treat offering/prospectus filings as dilution risk, not as a bullish catalyst.
An SEC 8-K item code proves the disclosure category, not whether its terms are bullish;
do not infer direction unless the supplied headline or filing description supports it.
Price targets and short interest may be stale; neither can justify a buy by itself.

Weigh the catalyst honestly. A big short interest, an imminent earnings date or a
funded balance sheet ARE real reasons a stock can rise - say so in the bull case,
then decide whether they outweigh the risks. Explain WHY a catalyst does or does not
change the verdict, rather than dismissing it.

Reply with STRICT JSON only. No markdown, no commentary, no reasoning outside the JSON.
Keep every string under 220 characters.
{
 "verdict": "AVOID" | "WATCH" | "SPECULATIVE_BUY",
 "conviction": "low" | "medium" | "high",
 "score": <0-100 attractiveness, 0=uninvestable, 50=balanced, 100=exceptional>,
 "bull_case": "<the strongest honest argument FOR, using the dossier>",
 "bear_case": "<the most likely way this loses money>",
 "catalyst_assessment": "<is the catalyst real, dated, and NOT already priced in?>",
 "why_this_verdict": "<what specifically tipped the balance>",
 "cost_hurdle": "<how far it must move to clear the spread, and is that plausible>",
 "key_risks": ["<risk>", "<risk>"],
 "what_to_watch": "<the one thing that would change your mind>",
 "confidence_note": "<what you could NOT verify from the dossier>"
}

Verdict guidance:
- SPECULATIVE_BUY: a concrete catalyst, tradeable liquidity, and no imminent dilution.
- WATCH: real potential but something is missing (timing, liquidity, confirmation).
- AVOID: risks dominate. Explain which risk is decisive.
Never state a probability that the price will rise - you cannot know that.
Your verdict is an independent risk review; it cannot override mechanical hard gates."""


def _dossier_text(d: Dossier) -> str:
    effective, estimated = effective_spread(d)
    aligned = catalyst_alignment(d)
    runway = f"{d.runway_quarters:.1f} quarters" if d.runway_known else "UNKNOWN"
    dilution = f"{d.shares_change_pct:+.1f}%" if d.shares_change_known else "UNKNOWN"
    lines = [
        f"TICKER {d.ticker}  ({d.name}) sector={d.sector or 'n/a'} exchange={d.exchange or 'n/a'}",
        f"price ${d.price:.4f} ({d.change_pct:+.1f}% today), 52w-high distance {d.off_52w_high_pct:+.0f}%",
        f"volume {d.volume:,.0f} vs avg {d.avg_volume:,.0f}  (pace-adjusted RVOL {d.volume_surge:.1f}x)",
        f"market cap ${d.market_cap/1e6:,.1f}M, float {d.float_shares/1e6:,.1f}M shares",
        f"execution cost {'proxy' if estimated else 'live spread'} {effective:.2f}% "
        f"(marketState={d.market_state}, raw quote={d.spread_pct:.2f}%, age={d.quote_age_min:.0f}m)",
        ("verified event match: YES - official 8-K items "
         + ", ".join(aligned.get("items") or [])
         + f" and headline aligned by {float(aligned.get('gap_hours') or 0):.1f}h"
         if aligned.get("aligned") else
         "verified event match: NO - " + str(aligned.get("reason") or "unavailable")),
        f"revenue {'$'+format(d.revenue/1e6,',.1f')+'M' if d.revenue_known else 'UNKNOWN'}, "
        f"cash {'$'+format(d.cash/1e6,',.1f')+'M' if d.cash_known else 'UNKNOWN'}, "
        f"debt {'$'+format(d.debt/1e6,',.1f')+'M' if d.debt_known else 'UNKNOWN'}",
        f"operating cash flow {'$'+format(d.op_cashflow/1e6,',.1f')+'M' if d.op_cashflow_known else 'UNKNOWN'} "
        f"-> runway {runway}",
        f"share count change (recent reported quarters): {dilution}; data completeness {d.data_completeness:.0f}%",
        f"technical: gap {d.gap_pct:+.1f}%, from open {d.from_open_pct:+.1f}%, "
        f"range location {d.close_location:.0%}, 5d {d.return_5d_pct:+.1f}%, "
        f"vs SMA20 {d.sma20_distance_pct:+.1f}%, ATR {d.atr_pct:.1f}%",
        "",
        "MECHANICAL RISK FLAGS:",
    ]
    lines += [f"  - {x}" for x in (d.flags or ["  (none triggered)"])]
    lines.append("")
    lines.append("CATALYSTS / POSITIONING:")
    lines += [f"  - {x}" for x in (d.catalysts or ["  (no dated catalyst identified)"])]
    lines.append(f"  analyst: {d.recommendation or 'n/a'}, target ${d.analyst_target:.2f} ({d.analyst_upside_pct:+.0f}%)")
    lines.append(f"  short {d.short_pct_float:.1f}% of float | institutions {d.inst_pct:.0f}% | insiders {d.insider_pct:.0f}%")
    lines.append("")
    lines.append("RECENT NEWS HEADLINES:")
    if d.news:
        lines += [f"  - [{n['when']}; age {n.get('age_hours',-1)}h] {n['title']} ({n['publisher']})" for n in d.news]
    else:
        lines.append("  - none retrieved")
    lines.append("")
    lines.append("RECENT SEC FILINGS (official rows retain acceptance time/items; item codes do not prove direction):")
    if d.recent_filings:
        lines += [
            f"  - {x.get('accepted_at') or x.get('date','')} {x.get('type','')} "
            f"items={','.join(str(v) for v in (x.get('items') or [])) or 'n/a'} "
            f"accession={x.get('accessionNumber') or 'n/a'} {x.get('title','')}"
            for x in d.recent_filings[:8]
        ]
    else:
        lines.append("  - none retrieved")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict:
    """Pull the JSON object out of a model reply. Different models wrap it
    differently - fenced blocks, prose, or <think> reasoning - so find the first
    balanced {...} rather than trusting the whole string to be clean JSON."""
    txt = str(raw or "").strip()
    # drop reasoning blocks some models emit before the answer
    for tag in ("</think>", "</reasoning>", "</thought>"):
        if tag in txt:
            txt = txt.split(tag)[-1]
    if "```" in txt:                       # fenced code block
        parts = txt.split("```")
        for part in parts:
            cand = part[4:] if part.lower().startswith("json") else part
            if cand.strip().startswith("{"):
                txt = cand
                break
    decoder = json.JSONDecoder()
    for start, ch in enumerate(txt):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(txt[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no valid JSON object in reply: {txt[:120]}")


def _normalize_ai(value: dict) -> dict:
    """Validate model output before it can influence a signal."""
    if not isinstance(value, dict):
        raise ValueError("AI reply is not an object")
    verdict = str(value.get("verdict") or "").upper()
    if verdict not in {"AVOID", "WATCH", "SPECULATIVE_BUY"}:
        raise ValueError(f"invalid AI verdict {verdict!r}")
    conviction = str(value.get("conviction") or "low").lower()
    if conviction not in {"low", "medium", "high"}:
        conviction = "low"
    try:
        score = max(0.0, min(100.0, float(value.get("score"))))
    except (TypeError, ValueError):
        raise ValueError("AI score is missing or invalid")
    out = {"verdict": verdict, "conviction": conviction, "score": round(score, 1)}
    for key in ("bull_case", "bear_case", "catalyst_assessment", "why_this_verdict",
                "cost_hurdle", "what_to_watch", "confidence_note"):
        out[key] = str(value.get(key) or "")[:300]
    risks = value.get("key_risks") or []
    if not isinstance(risks, list):
        risks = [risks]
    out["key_risks"] = [str(x)[:180] for x in risks[:5] if str(x).strip()]
    return out


async def analyse_dossier(d: Dossier) -> dict:
    """Review the exact immutable dossier used by mechanical ranking."""
    out = {"ticker": d.ticker, "dossier": asdict(d), "ai": None, "ai_error": ""}
    if d.error:
        out["ai_error"] = d.error
        return out
    try:
        raw = await _call_ai_long(SYSTEM_PROMPT, _dossier_text(d))
        out["ai"] = _normalize_ai(_extract_json(raw))
        out["model"] = LAST_MODEL_USED
    except Exception as e:
        out["ai_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


async def analyse(ticker: str) -> dict:
    """Build one dossier, then review that same snapshot."""
    return await analyse_dossier(build_dossier(ticker))


def _current_penny_universe(query, force: bool = False) -> set[str]:
    """Currently eligible listed names, cached for catalyst-first intersection.

    The SEC feed is market-wide.  Intersecting it with the same price/liquidity query
    used by the screener prevents large caps, funds and warrants from consuming the
    limited analysis slots while still finding a filing before a stock becomes a mover.
    """
    global _PENNY_UNIVERSE_CACHE
    created, symbols = _PENNY_UNIVERSE_CACHE
    if not force and symbols and time.time() - created < PENNY_UNIVERSE_TTL_SEC:
        return set(symbols)
    found: set[str] = set()
    for offset in range(0, 2_000, 250):
        response = yf.screen(
            query, offset=offset, size=250, sortField="ticker", sortAsc=True
        )
        quotes = (response or {}).get("quotes") or []
        found.update(
            str(quote.get("symbol") or "").strip().upper()
            for quote in quotes if quote.get("symbol")
        )
        if len(quotes) < 250:
            break
    if found:
        _PENNY_UNIVERSE_CACHE = (time.time(), set(found))
    return found


def screen(limit: int = SCAN_LIMIT) -> list[str]:
    """Find penny-stock candidates that are IN PLAY.

    Runs several passes because one sort misses most of the action: the biggest
    gainers, the highest-volume names, and heavily shorted listed small caps are
    different lists. Interleave the passes so one 100-name result cannot crowd the
    other sources out before the final slice.
    """
    if yf is None:
        return []
    base = [
        yf.EquityQuery("gt", ["intradayprice", MIN_PRICE]),
        yf.EquityQuery("lt", ["intradayprice", MAX_PRICE]),
        yf.EquityQuery("gt", ["avgdailyvol3m", MIN_AVG_VOLUME]),
        yf.EquityQuery("gt", ["intradaymarketcap", MIN_MARKET_CAP]),
        yf.EquityQuery("eq", ["region", "us"]),
        yf.EquityQuery("is-in", ["exchange", *sorted(LISTED_EXCHANGES)]),
    ]
    passes = [
        ("percentchange", False),   # today's biggest movers  <- the hype
        ("dayvolume", False),       # heaviest volume
        ("short_percentage_of_float.value", False),  # squeeze candidates, not a buy signal
    ]
    query = yf.EquityQuery("and", base)
    buckets, errors = [], []
    try:
        # Catalyst-first discovery catches an SEC event before it becomes a top mover.
        # Filter the market-wide filing feed before it consumes an analysis slot.
        eligible = _current_penny_universe(query)
        buckets.append([
            symbol for symbol in sec_edgar.current_8k_tickers(FRESH_NEWS_HOURS)
            if symbol in eligible
        ])
    except Exception as e:
        errors.append(f"SEC current 8-K feed: {type(e).__name__}")
    for field, asc in passes:
        try:
            res = yf.screen(query, size=min(100, max(30, limit)),
                            sortField=field, sortAsc=asc)
            bucket = [str(q.get("symbol") or "").upper()
                      for q in (res or {}).get("quotes", []) if q.get("symbol")]
            buckets.append(bucket)
        except Exception as e:
            errors.append(f"{field}: {type(e).__name__}")
    seen, out = set(), []
    max_rows = max((len(b) for b in buckets), default=0)
    for row in range(max_rows):
        for bucket in buckets:
            if row >= len(bucket):
                continue
            sym = bucket[row]
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
                if len(out) >= limit:
                    return out
    if not out and errors:
        raise RuntimeError("screener failed: " + "; ".join(errors[:3]))
    return out


async def scan_and_rank(tickers: list[str] | None = None, limit: int = 10) -> dict:
    """Screen (or take a supplied list), analyse each, return sorted results."""
    syms = tickers or screen(limit=limit * 3)
    syms = syms[:limit]
    results = []
    for s in syms:
        try:
            results.append(await analyse(s))
        except Exception as e:
            results.append({"ticker": s, "ai_error": str(e)[:120]})
        await asyncio.sleep(0.4)          # be polite to the data source
    rank = {"SPECULATIVE_BUY": 0, "WATCH": 1, "AVOID": 2, None: 3}
    results.sort(key=lambda r: (rank.get((r.get("ai") or {}).get("verdict"), 3),
                                len((r.get("dossier") or {}).get("flags") or [])))
    payload = {"scanned_at": time.time(), "count": len(results), "results": results}
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass
    return payload


def _print(r: dict):
    d = r.get("dossier") or {}
    ai = r.get("ai") or {}
    print(f"\n{'='*74}\n{r['ticker']}  {d.get('name','')}")
    print(f"{'='*74}")
    if r.get("ai_error"):
        print(f"  data/AI error: {r['ai_error']}")
    print(f"  ${d.get('price',0):.4f} ({d.get('change_pct',0):+.1f}%)  "
          f"vol {d.get('volume',0):,.0f} ({d.get('volume_surge',0):.1f}x avg)  "
          f"spread {d.get('spread_pct',0):.2f}%")
    print(f"  cap ${d.get('market_cap',0)/1e6:,.0f}M  runway {d.get('runway_quarters',0):.1f}q  "
          f"dilution {d.get('shares_change_pct',0):+.0f}%")
    if d.get("flags"):
        print("  RISK FLAGS:")
        for x in d["flags"]:
            print(f"    ! {x}")
    if ai:
        print(f"\n  VERDICT: {ai.get('verdict')}  (conviction {ai.get('conviction')})")
        print(f"  thesis    : {ai.get('thesis','')}")
        print(f"  bear case : {ai.get('bear_case','')}")
        print(f"  catalyst  : {ai.get('catalyst','')}")
        print(f"  cost hurdle: {ai.get('cost_hurdle','')}")
        for k in ai.get("key_risks") or []:
            print(f"    - {k}")
        print(f"  unverified: {ai.get('confidence_note','')}")


async def _main():
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        out = await scan_and_rank(tickers=[a.upper() for a in args], limit=len(args))
    else:
        print("screening US penny stocks (price $0.10-$5, avg vol >300k, cap >$10M)...")
        out = await scan_and_rank(limit=8)
    if not out["results"]:
        print("no candidates found (screener returned nothing).")
    for r in out["results"]:
        _print(r)
    print(f"\n{out['count']} analysed. Saved to {STATE_PATH}")
    print("\nREMINDER: this is research, not a prediction. Penny-stock spreads alone")
    print("can exceed any edge; assume you are the least-informed participant.")


if __name__ == "__main__":
    asyncio.run(_main())
