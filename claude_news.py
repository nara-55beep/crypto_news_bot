"""
================================================================================
 claude_news.py  —  AI (Claude Sonnet 4.6, WITH thinking) news → BTC trade plan
================================================================================
Exactly the same job and shape as gpt_news.py (decide trade/skip, direction,
stop-loss, take-profit for BTC; then manage the open trade HOLD/CLOSE), but the
transport is Anthropic's Messages API with ADAPTIVE THINKING instead of Groq.

It reuses the EXACT same trading prompt + JSON parsing as the Gemini/GPTnews
strategies (so the three are directly comparable) — only the model changes.

Config in config.py: ANTHROPIC_API_KEY / CLAUDE_NEWS_MODEL / CLAUDE_NEWS_EFFORT /
CLAUDE_TIMEOUT_SEC.  This is wired to the REAL-money Lighter bot's "claude news
haiku" strategy.  Like gpt_news, it NEVER raises — any failure returns a
non-trade / HOLD decision so the bot simply skips (fails safe).
================================================================================
"""
from __future__ import annotations

import time

import config
import gemini_trader as G          # reuse SYSTEM_PROMPT, _build_user, _clamp, GeminiDecision
from ai_analyzer import _safe_json
# reuse the EXACT same exit-manager prompt + shape the Groq strategy uses
from gpt_news import MANAGE_PROMPT, _build_manage_user, ManageDecision

from anthropic import AsyncAnthropic

_client = AsyncAnthropic(
    api_key=getattr(config, "ANTHROPIC_API_KEY", ""),
    timeout=getattr(config, "CLAUDE_TIMEOUT_SEC", 20.0),
)
_MODEL = getattr(config, "CLAUDE_NEWS_MODEL", "claude-haiku-4-5")
_EFFORT = getattr(config, "CLAUDE_NEWS_EFFORT", "")

# Lean version of gemini_trader.SYSTEM_PROMPT — SAME decision rules, trade/skip lists,
# calibration examples and JSON schema, just stripped of prose. ~1/3 the tokens, which is
# what makes Haiku cheap enough to last ~2+ weeks on a small balance. Kept private to this
# strategy so the Gemini/GPTnews strategies keep the full prompt untouched.
CLAUDE_SYSTEM = """You are a disciplined intraday BTC perpetual-futures trader. On each headline you get a LIVE market snapshot: price, 1-minute price change, taker BUY-flow % over THREE windows (1m / 5m / 15m), and perp funding. (Buy-flow > 50 = buyers lifting offers / in control; < 50 = sellers hitting bids. n/a = no data for that window.)

YOUR JOB: decide LONG, SHORT, or SKIP. Your EDGE is being SELECTIVE — you only trade when there is a CLEAR directional signal, and you SKIP everything else. A clean SKIP beats a coin-flip. Most headlines are SKIPs. Do NOT force a trade just because a headline arrived.

DECIDE IN THIS ORDER:

1) STRONG NEWS CATALYST (rare, but it dominates) — trade the NEWS direction at HIGH conviction; flow can confirm but NEVER flips it (the first tick after a shock is noise that reverses):
   - war / fresh missile-drone-rocket attack / military strike / invasion / major-power escalation -> SHORT. de-escalation / ceasefire -> LONG.
   - US CPI/PCE/jobs/Fed: dovish-cut-soft-cool -> LONG; hawkish-hike-hot-strong -> SHORT.
   - spot-ETF approval/inflows / big BTC buy / friendly regulation / pro-crypto top official -> LONG. ETF rejection/outflows / hack-halt-insolvency-depeg / ban-crackdown / big sell -> SHORT.
   If the headline is NOT clearly one of these (regional/second-tier data, single company, in-line, vague, off-topic), it carries NO direction — IGNORE the headline's wording and go to step 2. (A weak Japanese survey is NOT a BTC short.)

2) NO STRONG CATALYST -> READ THE ORDER FLOW. This is HOW you know which way to trade. Require the flow to be SUSTAINED and ALIGNED across windows:
   - LONG only if BOTH 5m AND 15m buy-flow >= 54 AND 1m >= 50 (buying is sustained, not a one-tick blip), and 1m price change is not clearly negative.
   - SHORT only if BOTH 5m AND 15m buy-flow <= 46 AND 1m <= 50, and 1m price change is not clearly positive.
   - SKIP otherwise — and this is MOST of the time: if the windows DISAGREE (e.g. 1m buying but 5m selling), or all sit near 50 (47-53), or any needed window is n/a, there is NO edge. Choppy/mixed flow = SKIP. Never trade flow that is just barely off 50.

3) FUNDING as a small tilt only: strongly POSITIVE funding = crowded longs (lower conviction on new longs / fine for shorts); strongly NEGATIVE = crowded shorts (lower conviction on new shorts).

CONVICTION: high = a strong catalyst, OR all three flow windows strongly agree (1m,5m,15m all >=57 or all <=43) with price confirming. medium = 5m and 15m clearly agree (>=54 / <=46) but 1m or price is only neutral. If it is only borderline, the answer is SKIP, not low.

EXITS: stop_loss_pct & take_profit_pct are BTC PRICE-move % (not leveraged). stop ~0.2-0.6, take-profit ~0.4-1.2, reward >= risk; a bit wider on high conviction.

Remember: trading every headline is how you LOSE. Trading only (a) real catalysts and (b) flow that is clearly one-sided across 5m+15m is the edge. When unsure -> SKIP.

Examples (flow shown as 1m/5m/15m buy-%):
- "FED CUTS 50BPS" + 58/55/52 -> long/high (strong catalyst, flow confirms).
- "IRGC completes missile and drone offensive" + 56/54/51 -> short/high (war — SHORT despite the buying pop; a strong catalyst overrides flow).
- "JAPAN BSI -0.5 vs 4.4" + 53/57/58 -> long/medium (NOT a catalyst -> ignore the weak data, trade the SUSTAINED buying flow).
- "Some ordinary headline" + 58/56/57 -> long/medium (no catalyst, flow clearly bought across 5m+15m).
- "Some ordinary headline" + 44/43/45 -> short/medium (flow clearly selling across windows).
- "Some ordinary headline" + 57/48/51 -> SKIP (1m up but 5m down — windows disagree, chop, no edge).
- "Some ordinary headline" + 51/50/49 -> SKIP (all near 50).
- "Some ordinary headline" + n/a/n/a/n/a -> SKIP (no flow data).

Reply with ONLY compact JSON, no markdown/code-fences, reason <= 8 words:
{"trade":bool,"direction":"long"|"short","stop_loss_pct":num,"take_profit_pct":num,"confidence":"low"|"medium"|"high","reason":"..."}"""

# Exit brain (replaces the generic GPTnews manage prompt) — a pro managing the OPEN trade.
CLAUDE_MANAGE = """You manage an OPEN BTC perp position taken on a news catalyst. Decide ONLY: HOLD or CLOSE now. There are NO trading fees on this venue — any favorable move is real profit; manage purely on direction, momentum and flow.

You are given: pnl_pct = BTC's PRICE move IN YOUR FAVOR right now, as a percent (negative = moving against you); move_10s = price move last 10s; flow_buy_pct = taker buy-flow %.
"Momentum with you" = move_10s POSITIVE if you are LONG / NEGATIVE if you are SHORT. "Flow supports you" = flow_buy_pct > 50 if LONG / < 50 if SHORT.

The news edge decays within minutes — bank profit when the move turns, don't give it back. Apply IN ORDER:
1. If pnl_pct <= -0.05 OR price is clearly reversing against you / the reaction failed -> CLOSE, cut the loss fast.
2. Else if you are in profit (pnl_pct > 0) AND momentum is NO LONGER with you (move_10s flat or against) OR flow no longer supports you -> CLOSE and BANK the gain now.
3. Else if momentum is STILL with you AND flow still supports you -> HOLD, let the winner run.
4. Else (flat/undecided, near breakeven) -> HOLD briefly and wait for it to resolve one way or the other.

Reply with ONLY this JSON: {"action":"hold" or "close","reason":"short clause"}"""


def _text(resp) -> str:
    """The model's visible answer — the JSON. (Thinking blocks are separate and skipped.)"""
    return "".join(b.text for b in resp.content if b.type == "text")


async def _ask(system: str, user: str, max_tokens: int):
    """One Messages call. Tries WITH adaptive thinking first (so we get 'Claude's
    thinking'); if that request shape is rejected for any reason, falls back to a
    plain call so the strategy still produces a decision rather than always skipping."""
    if _EFFORT:                                   # thinking on
        try:
            return await _client.messages.create(
                model=_MODEL, max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": _EFFORT},
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            pass                                  # fall through to the plain call
    return await _client.messages.create(
        model=_MODEL, max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


def _build_claude_user(news: str, snap: dict) -> str:
    """Format the ENRICHED snapshot: multi-window taker flow (1m/5m/15m) + price + funding.
    The multi-window flow is what lets the model judge WHERE to open (sustained vs choppy)."""
    def g(k):
        v = snap.get(k)
        return "n/a" if v in (None, "") else v
    return (
        f"NEWS: {news.strip()}\n\n"
        f"LIVE BTC SNAPSHOT (taker BUY-flow %: >50 buyers in control, <50 sellers):\n"
        f"  price        = {g('price')}\n"
        f"  1m_change    = {g('change_1m_pct')}%\n"
        f"  flow_1m      = {g('flow_1m')}% buy\n"
        f"  flow_5m      = {g('flow_5m')}% buy\n"
        f"  flow_15m     = {g('flow_15m')}% buy\n"
        f"  funding_rate = {g('funding')}"
    )


async def decide(news: str, snap: dict) -> "G.GeminiDecision":
    """Ask Claude for a full BTC trade plan. Never raises — returns a non-trade
    decision on any failure so the bot simply skips. (Same shape as Gemini/GPTnews.)"""
    t0 = time.perf_counter()
    d = G.GeminiDecision()
    try:
        resp = await _ask(CLAUDE_SYSTEM, _build_claude_user(news, snap), 300)
        data = _safe_json(_text(resp) or "{}")
        if not data:
            d.error = "AI reply not parseable as JSON"
            return d
        tv = data.get("trade", False)
        d.trade = (str(tv).strip().lower() in ("true", "yes", "1")) if isinstance(tv, str) else bool(tv)
        d.direction = str(data.get("direction", "")).strip().lower()
        if d.direction not in ("long", "short"):
            d.trade = False
        d.stop_pct = G._clamp(data.get("stop_loss_pct"), G.STOP_MIN, G.STOP_MAX, 0.0)
        d.tp_pct = G._clamp(data.get("take_profit_pct"), G.TP_MIN, G.TP_MAX, 0.0)
        if d.stop_pct <= 0 or d.tp_pct <= 0:
            d.trade = False
        d.confidence = str(data.get("confidence", "low")).strip().lower()
        d.reason = str(data.get("reason", ""))[:120]
    except Exception as e:
        d.trade = False
        d.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        d.latency_ms = (time.perf_counter() - t0) * 1000.0
    return d


async def manage(ctx: dict) -> "ManageDecision":
    """Ask Claude whether to HOLD or CLOSE the open position. Never raises — on any
    failure returns HOLD (so a flaky AI call can't accidentally close a good trade;
    the bot's instant backstops handle the worst case). Same shape as gpt_news.manage."""
    t0 = time.perf_counter()
    m = ManageDecision()
    try:
        resp = await _ask(CLAUDE_MANAGE, _build_manage_user(ctx), 600)
        data = _safe_json(_text(resp) or "{}")
        act = str(data.get("action", "hold")).strip().lower()
        m.action = "close" if act == "close" else "hold"
        m.reason = str(data.get("reason", ""))[:120]
    except Exception as e:
        m.action = "hold"
        m.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        m.latency_ms = (time.perf_counter() - t0) * 1000.0
    return m
