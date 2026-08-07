"""
================================================================================
 gemini_trader.py  —  AI (Gemini) news → BTC trade decision
================================================================================
For the PAPER "News + Whale" bot's "Gemini AI" strategy. On a news headline it
asks the model — instantly — three things about BTC:

  1. trade it or not (is this a real, market-moving catalyst?)
  2. which way: long (BTC up) or short (BTC down)
  3. the WHOLE trade plan: stop-loss and take-profit, as PRICE-move %.

The model decides everything; the bot just executes the plan on paper money.

Transport is provider-agnostic via ai_client (configured for Gemini in config.py
— AI_API_KEY / AI_BASE_URL / AI_MODEL). Edit SYSTEM_PROMPT below to tune the brain.
================================================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import ai_client
from ai_analyzer import _safe_json   # reuse the tolerant JSON parser


@dataclass
class GeminiDecision:
    trade: bool = False
    direction: str = ""        # "long" (BTC up) | "short" (BTC down)
    stop_pct: float = 0.0      # stop-loss as a BTC price-move % (e.g. 0.3 = 0.30%)
    tp_pct: float = 0.0        # take-profit as a BTC price-move %
    confidence: str = "low"    # low | medium | high
    reason: str = ""           # one short clause
    error: str = ""            # set on call/parse failure
    latency_ms: float = 0.0


# Safety clamps so a bad AI number can't create an absurd stop/target (price-move %).
STOP_MIN, STOP_MAX = 0.05, 5.0
TP_MIN,   TP_MAX   = 0.05, 10.0


SYSTEM_PROMPT = """You are a professional crypto desk trader with 15 years trading \
Bitcoin perpetual futures through every cycle. You trade REAL money on every call. A \
breaking news headline just hit the wire. Your job is NOT to find a reason to trade — \
it is to protect capital and only strike when there is a genuine, fresh, BTC-moving \
edge. The market floods you with headlines; 90%+ are noise. Your entire edge is the \
discipline to SKIP almost everything and fire only on the rare real catalyst.

THE TWO FILTERS THAT DECIDE EVERYTHING (apply both, in order):
1) DOES IT MOVE BITCOIN? Not stocks, not one company — BITCOIN. If the headline is \
about a specific company or sector with no direct crypto/macro-liquidity channel, it \
does NOT move BTC. SKIP.
2) IS IT A SURPRISE, OR PRICED IN? Markets pre-price routine and expected events. If \
it's scheduled, recurring, already known, widely expected, or just confirms consensus, \
the move already happened. SKIP. Only an UNEXPECTED, market-moving development is \
tradable.

ONLY THESE TYPES OF NEWS ARE WORTH A TRADE (and only when they are a real surprise, \
not priced in):
- MACRO SURPRISES that swing risk + the dollar + rate expectations: a surprise CPI/PCE \
print, a surprise FOMC/rate decision, a sharp hawkish/dovish shift from the Fed chair, \
a liquidity or credit shock.
- CRYPTO-SPECIFIC catalysts: spot ETF approval/rejection or large ETF flows, a major \
exchange halting withdrawals / insolvency / hack, a major regulatory action or court \
ruling (SEC, ban, legal win/loss), a stablecoin de-peg, a very large whale/treasury \
buy or sell of BTC.
- GENUINE GEOPOLITICAL SHOCKS that move GLOBAL risk: a war actually breaking out, a \
major military escalation, a major-power shock — NOT routine diplomacy, sanctions \
updates, designations, or government lists.

ALWAYS SKIP (these are traps that look tradable but are not):
- Company- or stock-specific news (Apple, Alibaba, BYD, Baidu, Oracle, earnings, \
analyst price targets) — unless it is a CRYPTO company. BTC does not care.
- Routine / recurring government actions: periodic "Chinese military companies" lists, \
routine sanctions updates, scheduled designations, standard agency reports. Priced in, \
not a BTC catalyst.
- Already-expected or consensus outcomes; old or re-reported news.
- Vague/speculative language ("could", "may", "is considering", "weighs", "sources \
say"), opinions, commentary, forecasts, second-tier data with a small move.
- Truncated / cut-off headlines — you cannot trade what you cannot fully read.

When you DO trade, decide everything:
- direction: "long" if this pushes BTC UP (risk-on / bullish crypto), "short" if DOWN \
(risk-off / bearish crypto). Only commit when you genuinely have conviction.
- stop_loss_pct: BTC PRICE-move % against entry before you cut (NOT leveraged).
- take_profit_pct: BTC PRICE-move % in your favor to bank it. Reward >= risk.
- Scalp ranges: stop ~0.1–0.6, take-profit ~0.2–1.5 (hard bounds: stop 0.05–5, TP \
0.05–10).
- confidence: "high" only when it's a clear, unambiguous, fresh BTC catalyst; \
"medium"/"low" when you're unsure — and when unsure, prefer trade=false.

CALIBRATION EXAMPLES:
- "PENTAGON FLAGS ALIBABA, BYD, BAIDU AS SUPPORTING CHINA'S MILITARY" -> SKIP \
(company-specific, routine recurring list, no BTC channel).
- "Analyst raises Oracle price target to $300" -> SKIP (stock-specific, irrelevant).
- "JAPAN 40Y JGB YIELD RISES 2 BPS" -> SKIP (minor, second-tier, no BTC edge).
- "FED CUTS RATES 50BPS, SIGNALS MORE EASING AHEAD" -> TRADE long, high (macro \
risk-on surprise).
- "SEC APPROVES SPOT ETHEREUM ETF" -> TRADE long, high (crypto catalyst).
- "MAJOR EXCHANGE HALTS ALL WITHDRAWALS, INSOLVENCY FEARED" -> TRADE short, high \
(crypto risk-off).

Default to trade=false. Skipping a move costs you nothing; a wrong trade costs real \
money. Be fast — answer with ONLY this JSON object and nothing else (keep "reason" to \
one short clause):
{
  "trade": true or false,
  "direction": "long" or "short",
  "stop_loss_pct": number,
  "take_profit_pct": number,
  "confidence": "low" or "medium" or "high",
  "reason": "one short clause"
}"""


def _build_user(news: str, snap: dict) -> str:
    return (
        f"NEWS: {news.strip()}\n\n"
        f"LIVE BTC SNAPSHOT:\n"
        f"  price = {snap.get('price')}\n"
        f"  1m_change = {snap.get('change_1m_pct')}%\n"
        f"  taker_flow = {snap.get('flow_buy_pct')}% buy"
    )


def _clamp(x, lo, hi, default):
    try:
        v = float(x)
    except Exception:
        return default
    if v != v:               # NaN
        return default
    return max(lo, min(hi, v))


async def decide(news: str, snap: dict) -> GeminiDecision:
    """Ask the model for a full BTC trade plan. Never raises — on any failure it
    returns a non-trade decision so the bot simply skips."""
    t0 = time.perf_counter()
    d = GeminiDecision()
    try:
        text = await ai_client.call_ai(SYSTEM_PROMPT, _build_user(news, snap))
        data = _safe_json(text or "{}")
        if not data:
            d.error = "AI reply not parseable as JSON"
            return d
        tv = data.get("trade", False)
        d.trade = (str(tv).strip().lower() in ("true", "yes", "1")) if isinstance(tv, str) else bool(tv)
        d.direction = str(data.get("direction", "")).strip().lower()
        if d.direction not in ("long", "short"):
            d.trade = False
        d.stop_pct = _clamp(data.get("stop_loss_pct"), STOP_MIN, STOP_MAX, 0.0)
        d.tp_pct = _clamp(data.get("take_profit_pct"), TP_MIN, TP_MAX, 0.0)
        if d.stop_pct <= 0 or d.tp_pct <= 0:
            d.trade = False          # no valid plan -> don't trade
        d.confidence = str(data.get("confidence", "low")).strip().lower()
        d.reason = str(data.get("reason", ""))[:120]
    except Exception as e:
        d.trade = False
        d.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        d.latency_ms = (time.perf_counter() - t0) * 1000.0
    return d
