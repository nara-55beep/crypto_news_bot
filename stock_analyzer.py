"""
================================================================================
 stock_analyzer.py  —  THE STOCK AI BRAIN  (Gemini decides: does news move a stock?)
================================================================================
The stock twin of ai_analyzer.py. Same idea, same Gemini transport (ai_client),
same Signal contract — but the persona trades STOCKS and maps headlines to the
tickers in config.STOCK_SYMBOLS. Single-pass (no web research) to keep AI calls
light on the free tier.

The AI itself decides TRADE vs SKIP. No numeric thresholds.
================================================================================
"""

from __future__ import annotations

import time

import config
import ai_client
from ai_analyzer import Signal, _safe_json   # reuse the exact same types/parser


def _system_prompt() -> str:
    tickers = " ".join(config.STOCK_SYMBOLS)
    return (
        "You are a seasoned professional equities trader with 20 years on a top "
        "desk. A live news headline crosses your desk and you decide, on your own "
        "judgment, whether to take a STOCK position right now in one of the names "
        "you cover.\n\n"
        "Think like a real trader: what does this headline mean for a specific "
        "company or sector? Company-specific news (earnings, guidance, a product, "
        "an SEC action, a lawsuit, an analyst call, M&A, a CEO change) moves THAT "
        "stock directly. Macro news (CPI, jobs, the Fed, rates, tariffs, war, oil) "
        "moves the broad market — trade SPY (S&P 500) or QQQ (Nasdaq 100) for that, "
        "and remember rate cuts / soft inflation / risk-on is broadly bullish, "
        "while hawkish Fed / hot inflation / risk-off is broadly bearish.\n\n"
        "Your decision is YOURS — no fixed scoring. Weigh: How big is this? Is it "
        "concrete or vague? A genuine surprise or already priced in? One source or "
        "confirmed? Actionable in the next minutes-to-days? A real pro SKIPS the "
        "vast majority of headlines. Truncated/cut-off headlines: SKIP. A flat "
        "market after a headline usually means it does not matter.\n\n"
        "Only TRADE when you would put real money on it: a clear, market-moving, "
        "not-priced-in catalyst with a direction you believe in, affecting a stock "
        "you can name from this list. Otherwise SKIP.\n\n"
        f"TRADABLE TICKERS (use these exact symbols): {tickers}. "
        "If the affected company is not in this list, SKIP (you can only trade "
        "these). Broad-market news -> SPY or QQQ.\n\n"
        "Answer with ONLY this JSON object and nothing else. Keep every string "
        "SHORT (one clause). Always close the JSON:\n"
        "{\n"
        '  "decision": "TRADE" or "SKIP",\n'
        '  "direction": "bullish" or "bearish" or "neutral",\n'
        '  "affected_assets": ["TICKERS you would trade, empty if SKIP"],\n'
        '  "conviction": "low" or "medium" or "high",\n'
        '  "horizon": "minutes" or "hours" or "days",\n'
        '  "market_read": "one short clause on the company/sector/market impact",\n'
        '  "causal_chain": "short path from headline to the stock move",\n'
        '  "reasoning": "short: why you would trade this or skip it"\n'
        "}"
    )


def _build_user_message(news: str, snapshot: dict) -> str:
    lines = [f"NEWS: {news.strip()}", "", "LIVE PRICES (watched stocks):"]
    if snapshot:
        for sym, snap in snapshot.items():
            lines.append(f"  {sym}: price={snap.get('price')}, "
                         f"1m_change={snap.get('change_1m_pct')}%")
    else:
        lines.append("  (market closed or prices unavailable right now)")
    return "\n".join(lines)


def _apply(sig: Signal, data: dict):
    sig.decision = str(data.get("decision", "SKIP")).upper()
    sig.direction = str(data.get("direction", "neutral")).lower()
    sig.affected_assets = [str(a).upper() for a in data.get("affected_assets", [])]
    sig.conviction = str(data.get("conviction", "low")).lower()
    sig.horizon = str(data.get("horizon", "hours")).lower()
    sig.market_read = str(data.get("market_read", ""))
    sig.causal_chain = str(data.get("causal_chain", ""))
    sig.reasoning = str(data.get("reasoning", ""))


async def analyze(news: str, snapshot: dict) -> Signal:
    """One-pass AI decision for stocks. Never raises — returns SKIP on failure."""
    t0 = time.perf_counter()
    sig = Signal(raw=news)
    try:
        text = await ai_client.call_ai(_system_prompt(), _build_user_message(news, snapshot))
        sig.raw_response = (text or "")[:600]
        data = _safe_json(text or "{}")
        if not data:
            sig.error = "AI reply could not be parsed as JSON"
        _apply(sig, data)
        # only keep tickers we actually cover
        sig.affected_assets = [a for a in sig.affected_assets if a in config.STOCK_SYMBOLS]
        if not sig.affected_assets and sig.decision == "TRADE":
            sig.decision = "SKIP"          # said trade but named nothing we hold
    except Exception as e:
        sig.decision = "SKIP"
        sig.error = f"{type(e).__name__}: {e}"
    finally:
        sig.latency_ms = (time.perf_counter() - t0) * 1000.0
    return sig
