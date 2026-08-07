"""
================================================================================
 ai_analyzer.py  —  THE AI INTERPRETATION LAYER
================================================================================
THIS is the module the AI runs in. You do NOT edit your key here — the key,
base_url, and model all live in config.py (section 2). This file just *uses*
them. Read SYSTEM_PROMPT below: that is the brain. Tune it freely.

What it does: takes a raw news string + a live market snapshot, asks the model
to reason in causal chains, and returns a STRUCTURED signal (dataclass) the rest
of the bot can act on. The model is forced to answer in JSON; we parse it
defensively.

----- WHERE THE AI ACTUALLY GETS CALLED -----
The AI *transport* (the provider-specific HTTP call) lives in ai_client.py, which
works with any provider via the AI_PROVIDER setting in config.py. This file only
builds the prompt and parses the reply:

    text = await ai_client.call_ai(SYSTEM_PROMPT, user_prompt)   # provider-agnostic
    signal = parse(text)

So to change AI vendor you edit config.py (+ optionally ai_client.py); you never
touch this file. The SYSTEM_PROMPT below is the "brain" — tune it freely.
================================================================================
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import config
import ai_client


# ---------------------------------------------------------------------------
# The structured signal the AI must produce. This is the contract between the
# "AI brain" and the "trading body". Everything downstream reads these fields.
# ---------------------------------------------------------------------------
# The AI's verdict. The AI itself decides TRADE vs SKIP — there are no numeric
# thresholds in the bot anymore. The AI weighs importance / priced-in / conviction
# internally (like a trader) and just returns its decision + reasoning.
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    decision: str = "SKIP"              # "TRADE" or "SKIP" — the AI decides
    direction: str = "neutral"          # bullish | bearish (only matters if TRADE)
    affected_assets: list = field(default_factory=list)   # tickers, e.g. ["BTC"]
    conviction: str = "low"             # low | medium | high (AI's own confidence)
    horizon: str = "hours"              # minutes | hours | days
    market_read: str = ""               # how it read crypto/stocks/oil/gold/macro
    causal_chain: str = ""              # the path to the crypto effect
    reasoning: str = ""                 # why TRADE or SKIP
    raw: str = ""                       # the original headline
    latency_ms: float = 0.0             # how long the AI call took
    error: str = ""                     # set if the AI call/parse failed
    raw_response: str = ""              # the raw text the AI returned (for debugging)
    researched: bool = False            # did we run web search before deciding?
    search_queries: list = field(default_factory=list)   # what it searched
    research_context: str = ""          # the search snippets used (truncated)

    @property
    def wants_trade(self) -> bool:
        return (self.decision == "TRADE"
                and self.direction in ("bullish", "bearish")
                and bool(self.affected_assets))


# The "brain". This persona makes the AI decide everything itself.
SYSTEM_PROMPT = """You are a seasoned professional macro trader with 20 years on \
a top proprietary trading desk. You have traded crypto, equities, oil, gold, \
rates, and FX through multiple cycles. You have deep instincts for what actually \
moves markets versus what is noise, and for what is already priced in. A live \
news headline crosses your desk. You decide, on your own judgment, whether it is \
worth taking a CRYPTO position right now.

You can only TRADE crypto perpetual futures. But you THINK across every market, \
because that is how a real macro trader reads a headline:
- Read what it means for EQUITIES / risk sentiment, OIL, GOLD, the US DOLLAR \
(DXY), BONDS / interest-rate expectations, and then how that flows into CRYPTO.
- Crypto trades like a high-beta risk asset: risk-on (soft inflation, rate cuts, \
equity rallies, weak dollar) tends bullish; risk-off (war, oil shocks, hawkish \
Fed, equity selloffs, strong dollar) tends bearish. Coin-specific news (an \
upgrade, a hack, an ETF, an exchange issue) moves that coin directly.

Your decision is YOURS. There are no fixed scoring rules and no thresholds — use \
your trader's judgment the way you would with real capital on the line. Weigh, in \
your own head: How big is this, really? Is it concrete or vague? Is it a genuine \
surprise or already priced in? One source or confirmed? Tradable in the next \
minutes-to-days, or just background? Then commit to TRADE or SKIP.

Decide TRADE only when you would actually put your own money on it: a clear, \
market-moving, not-already-priced-in catalyst with a direction you believe in. \
Otherwise SKIP. A real pro SKIPS the vast majority of headlines — vague \
statements, routine commentary, old news, anything you cannot act on with \
conviction. Truncated/cut-off headlines: SKIP (you cannot trade what you cannot \
fully read). Do not invent urgency from a quiet market; a flat tape after a \
headline usually means the news does not matter.

TRADABLE CRYPTO ASSETS (use the TICKER): BTC ETH SOL BNB XRP ADA DOGE AVAX LINK \
DOT MATIC LTC BCH ATOM UNI TRX NEAR APT ARB OP FIL INJ SUI SEI TIA RUNE AAVE ETC \
FTM ALGO PEPE WIF ENA. Broad macro -> usually BTC/ETH (market leaders). \
Coin-specific -> that coin. If none are meaningfully affected, SKIP.

RESEARCH: You may request a quick web search BEFORE committing, but only when it \
would actually change your decision. Set needs_research=true and list 1-3 precise \
search_queries ONLY when ALL of these hold: (a) the headline is concrete and \
specific (a real event/decision/number, not a vague "could/might/thinks"), AND \
(b) you are genuinely UNSURE of the market impact without current context, AND \
(c) fresh facts could flip you between TRADE and SKIP. For vague, speculative, \
routine, or clearly-junk headlines, set needs_research=false and just SKIP — do \
NOT waste a search. Good queries are specific: "Iran nuclear deal oil sanctions \
impact", "Coinbase SEC lawsuit outcome", not "Iran news".

Answer with ONLY this JSON object and nothing else. Keep every string SHORT \
(one clause). Always close the JSON:
{
  "decision": "TRADE" or "SKIP",
  "direction": "bullish" or "bearish" or "neutral",
  "affected_assets": ["TICKERS you would trade, empty if SKIP"],
  "conviction": "low" or "medium" or "high",
  "horizon": "minutes" or "hours" or "days",
  "market_read": "one short clause on stocks/oil/gold/dollar/rates impact",
  "causal_chain": "short path to the crypto effect",
  "reasoning": "short: why you would trade this or skip it",
  "needs_research": true or false,
  "search_queries": ["1-3 precise queries if needs_research, else empty"]
}"""

# Appended in PASS 2 (after we've fetched search results) so the model re-decides
# WITH context. It must now commit — no more research.
PASS2_SUFFIX = """

You previously asked to research this. Below are live web search results. Using \
them plus your judgment, make your FINAL decision now. Do NOT request more \
research (set needs_research=false). Same JSON format as before."""


def _build_user_message(news: str, market_snapshot: dict) -> str:
    """Hand the model the news AND current market state so it can judge
    'priced in' and pick affected assets sensibly."""
    lines = [f"NEWS: {news.strip()}", "", "LIVE MARKET SNAPSHOT:"]
    for sym, snap in market_snapshot.items():
        lines.append(
            f"  {sym}: price={snap.get('price')}, "
            f"1m_change={snap.get('change_1m_pct')}%, "
            f"funding_rate={snap.get('funding_rate')}, "
            f"open_interest={snap.get('open_interest')}"
        )
    return "\n".join(lines)


def _apply_fields(sig: "Signal", data: dict):
    """Copy parsed JSON fields onto the Signal."""
    sig.decision = str(data.get("decision", "SKIP")).upper()
    sig.direction = str(data.get("direction", "neutral")).lower()
    sig.affected_assets = [str(a).upper() for a in data.get("affected_assets", [])]
    sig.conviction = str(data.get("conviction", "low")).lower()
    sig.horizon = str(data.get("horizon", "hours")).lower()
    sig.market_read = str(data.get("market_read", ""))
    sig.causal_chain = str(data.get("causal_chain", ""))
    sig.reasoning = str(data.get("reasoning", ""))


def _wants_research(data: dict) -> bool:
    v = data.get("needs_research", False)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


async def analyze(news: str, market_snapshot: dict) -> Signal:
    """Two-pass AI decision:
      PASS 1: read headline -> decide, OR ask to research (with queries)
      RESEARCH: if asked, run free web search (web_research)
      PASS 2: re-decide WITH the search results
    Never raises — on any failure it returns a SKIP Signal so the bot won't trade."""
    t0 = time.perf_counter()
    sig = Signal(raw=news)
    base_user = _build_user_message(news, market_snapshot)
    try:
        # ---- PASS 1 ----
        text = await ai_client.call_ai(SYSTEM_PROMPT, base_user)
        sig.raw_response = (text or "")[:600]
        data = _safe_json(text or "{}")
        if not data:
            sig.error = "AI reply could not be parsed as JSON"
        _apply_fields(sig, data)

        # ---- RESEARCH (only if the AI asked and gave queries) ----
        if _wants_research(data):
            queries = [str(q) for q in data.get("search_queries", []) if str(q).strip()]
            sig.search_queries = queries[:3]
            if sig.search_queries:
                import web_research
                context = await web_research.research(sig.search_queries)
                if context:
                    sig.researched = True
                    sig.research_context = context[:1500]
                    # ---- PASS 2: decide again WITH the results ----
                    pass2_user = (base_user + "\n\nWEB SEARCH RESULTS:\n" + context)
                    text2 = await ai_client.call_ai(
                        SYSTEM_PROMPT + PASS2_SUFFIX, pass2_user)
                    sig.raw_response = (text2 or "")[:600]
                    data2 = _safe_json(text2 or "{}")
                    if data2:
                        _apply_fields(sig, data2)
                        sig.error = ""
                    else:
                        sig.error = "pass-2 reply could not be parsed"
                # if context == "": search failed -> keep pass-1 decision (graceful)
    except Exception as e:
        sig.decision = "SKIP"
        sig.error = f"{type(e).__name__}: {e}"
    finally:
        sig.latency_ms = (time.perf_counter() - t0) * 1000.0
    return sig


def _safe_json(text: str) -> dict:
    """Tolerant JSON parse. Handles ```json fences, surrounding prose, AND replies
    that got CUT OFF by the token limit (we salvage the key:value pairs we got)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # 1) try as-is
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) try the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    # 3) REPAIR a truncated reply: pull out each "key": value pair we can find.
    #    This rescues cut-off replies like the Alphabet one (missing closing }).
    import re
    out = {}
    if start != -1:
        body = text[start + 1:]
        # string fields:  "key": "value"
        for k, v in re.findall(r'"(\w+)"\s*:\s*"([^"]*)"', body):
            out[k] = v
        # array field:    "affected_assets": ["BTC","ETH"
        m = re.search(r'"affected_assets"\s*:\s*\[([^\]]*)', body)
        if m:
            out["affected_assets"] = [s.strip().strip('"')
                                      for s in m.group(1).split(",") if s.strip()]
        # bare-word fields (decision etc. without quotes, just in case)
        for k, v in re.findall(r'"(\w+)"\s*:\s*([A-Za-z]+)', body):
            out.setdefault(k, v)
    return out
