"""
================================================================================
 news_paper_engine.py — deterministic, rule-based decision engine
================================================================================
PURE FUNCTIONS ONLY. No randomness, no AI. Given a news item (+ market context
the bot supplies), it:
  1. classify(news)  -> which of the 9 categories it is, the asset, the direction
  2. score(news, cls, ctx) -> a transparent impact_score + confidence + TRADE/SKIP

The bot (news_paper_bot.py) owns market data, dedup state and account rules; it
calls classify() to learn the symbol/direction, computes market confirmation for
that symbol, then calls score(). Keeping this file pure makes it trivial to test.

impact_score = source_weight + event_weight + urgency_weight + novelty_weight
             + market_confirmation_score
             - duplicate_penalty - ambiguity_penalty - already_moved_penalty
================================================================================
"""

from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# 9 categories. Order matters: the FIRST matching tradeable category wins, so
# more specific / higher-impact events are listed first. symbol=None means a
# whole-market event (we then look for a named coin, else default BTC).
# ---------------------------------------------------------------------------
CATEGORIES = [
    {"name": "geopolitical_escalation", "dir": "SHORT", "symbol": None, "event_weight": 3,
     "keywords": ["missile", "strike", "airstrike", "bombing", "bombs", "attack", "invasion",
                  "invade", "explosion", "war breaks", "declares war", "at war", "nuclear",
                  "iran", "israel", "russia", "ukraine", "taiwan", "escalat"]},
    {"name": "geopolitical_deescalation", "dir": "LONG", "symbol": None, "event_weight": 3,
     "keywords": ["ceasefire", "cease-fire", "peace deal", "peace agreement", "truce",
                  "talks successful", "war ends", "agreement reached", "de-escalat", "deescalat"]},
    {"name": "inflation_rates_bearish", "dir": "SHORT", "symbol": None, "event_weight": 3,
     "keywords": ["cpi higher", "cpi comes in hot", "cpi hot", "hotter than expected",
                  "ppi higher", "inflation higher", "fed hawkish", "hawkish", "rate hike",
                  "raises rates", "higher for longer", "no rate cut", "dollar jumps",
                  "dollar surges", "yields rise", "yields jump"]},
    {"name": "inflation_rates_bullish", "dir": "LONG", "symbol": None, "event_weight": 3,
     "keywords": ["cpi lower", "cpi cooler", "cooler than expected", "ppi lower",
                  "inflation lower", "fed dovish", "dovish", "rate cut", "cuts rates",
                  "liquidity injection", "quantitative easing", " qe ", "yields fall", "yields drop"]},
    {"name": "crypto_adoption_bullish", "dir": "LONG", "symbol": None, "event_weight": 3,
     "keywords": ["etf approved", "etf approval", "approves spot", "spot etf", "etf inflow",
                  "blackrock buys", "blackrock adds", "institutional adoption", "adopts bitcoin",
                  "legal tender", "buys btc", "buys bitcoin", "buys eth", "buys ethereum",
                  "strategic bitcoin reserve", "adds bitcoin"]},
    {"name": "crypto_regulation_bearish", "dir": "SHORT", "symbol": None, "event_weight": 3,
     "keywords": ["sec lawsuit", "sec sues", "sec charges", "exchange ban", "crypto ban",
                  "bans crypto", "stablecoin crackdown", "crackdown", "investigation",
                  "delisting", "delist", "enforcement action"]},
    {"name": "hack_exploit_depeg", "dir": "SHORT", "symbol": None, "event_weight": 3,
     "keywords": ["hacked", "exploited", "exploit", "depeg", "de-peg", "withdrawals halted",
                  "halts withdrawals", "insolvency", "insolvent", "bankruptcy", "bankrupt",
                  "funds drained", "drained", "stolen"]},
    {"name": "solana_bad", "dir": "SHORT", "symbol": "SOLUSDT", "event_weight": 2,
     "keywords": ["solana outage", "solana network halt", "solana halted", "network halted",
                  "validator issue", "sol exploit", "solana down", "solana offline"]},
]

# Category 9 = noise / not tradeable. If a headline is clearly noise we skip even
# if a stray keyword matched.
NOISE_KEYWORDS = ["analyst says", "may go", "could go", "price prediction", "predicts",
                  "in my opinion", "sponsored", "educational", "how to", "beginner",
                  "giveaway", "airdrop", "thread", "top 5", "top 10"]

URGENT_WORDS = ["breaking", "urgent", "confirmed", "just in", "alert", "developing"]
VAGUE_WORDS = ["rumor", "rumour", "unconfirmed", "reportedly", "may ", "might ", "could ",
               "possibly", "speculation", "allegedly", "appears to"]

# Resolve a specific coin named in a whole-market headline (else default BTC).
ASSET_HINTS = [
    ("ETHUSDT", ["ethereum", "ether ", " eth", "$eth"]),
    ("SOLUSDT", ["solana", " sol", "$sol"]),
    ("BTCUSDT", ["bitcoin", " btc", "$btc"]),
]

SUPPORTED = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Solana issues are often worded loosely ("Solana network suffers major outage"),
# so we detect them by "solana/SOL near a problem word" rather than exact phrases.
SOLANA_ISSUE_WORDS = ["outage", "halt", "halted", "down", "offline", "stalled", "degraded",
                      "stops producing", "stopped", "exploit", "validator", "congest"]
_SOL_CAT = next(c for c in CATEGORIES if c["name"] == "solana_bad")


# ---------------------------------------------------------------------------
# Normalisation + hashing (for dedup): lowercase, strip urls/punctuation/emojis.
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"http\S+", " ", t)              # drop urls
    t = re.sub(r"[^a-z0-9 ]", " ", t)           # punctuation + emojis -> space
    t = re.sub(r"\s+", " ", t).strip()
    return t


def headline_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def is_noise(norm: str) -> bool:
    return any(k in norm for k in NOISE_KEYWORDS)


def detect_category(norm: str):
    """First matching tradeable category, or (None, None)."""
    # Special case: Solana-specific trouble worded loosely (outage/halt/down near "solana").
    if ("solana" in norm or re.search(r"\bsol\b", norm)) and any(w in norm for w in SOLANA_ISSUE_WORDS):
        return _SOL_CAT, "solana+issue"
    for cat in CATEGORIES:
        for kw in cat["keywords"]:
            if kw in norm:
                return cat, kw
    return None, None


def pick_symbol(cat, norm: str) -> str:
    if cat and cat.get("symbol"):
        return cat["symbol"]
    for sym, hints in ASSET_HINTS:                 # whole-market event: named coin?
        if any(h in norm for h in hints):
            return sym
    return "BTCUSDT"                               # default the whole market to BTC


def source_weight(source: str) -> float:
    s = (source or "").lower()
    official = ["reuters", "bloomberg", "associated press", "wsj", "cnbc", "sec.gov",
                "federalreserve", "whitehouse", "coindesk", "the block", "ap"]
    trusted = ["tree of alpha", "treeofalpha", "walterbloomberg", "financialjuice",
               "watcher.guru", "decrypt", "tg:", "telegram"]
    if any(o in s for o in official):
        return 3.0
    if any(t in s for t in trusted):
        return 2.0
    if s and s not in ("?", "unknown"):
        return 1.0
    return 0.5


def urgency_weight(norm: str) -> float:
    if any(w in norm for w in URGENT_WORDS):
        return 1.5
    if any(w in norm for w in VAGUE_WORDS):
        return -1.0
    return 0.5


def ambiguity_penalty(norm: str) -> float:
    return 2.0 if any(w in norm for w in VAGUE_WORDS) else 0.0


def base_confidence(urgency: float, ambiguous: bool) -> float:
    """0.80-0.95 very clear breaking event; 0.70-0.80 clear; lower if vague."""
    c = 0.72                                       # clear but not urgent
    if urgency >= 1.5:
        c = 0.88                                   # breaking / confirmed
    if ambiguous:
        c -= 0.32
    return round(max(0.0, min(0.95, c)), 2)


# ---------------------------------------------------------------------------
def classify(news: dict) -> dict:
    """Decide category, asset and direction — no scoring. Pure."""
    headline = news.get("headline") or news.get("text") or ""
    body = news.get("body") or ""
    norm = normalize(headline + " " + body)
    if not norm:
        return {"category": "empty", "symbol": None, "direction": "SKIP",
                "noise": True, "matched_kw": None, "norm": "", "event_weight": 0}
    if is_noise(norm):
        return {"category": "noise", "symbol": None, "direction": "SKIP",
                "noise": True, "matched_kw": None, "norm": norm, "event_weight": 0}
    cat, kw = detect_category(norm)
    if cat is None:
        return {"category": "uncategorized", "symbol": None, "direction": "SKIP",
                "noise": False, "matched_kw": None, "norm": norm, "event_weight": 0}
    return {"category": cat["name"], "symbol": pick_symbol(cat, norm), "direction": cat["dir"],
            "noise": False, "matched_kw": kw, "norm": norm, "event_weight": cat["event_weight"]}


def score(news: dict, cls: dict, ctx: dict) -> dict:
    """Compute impact_score + confidence and the signal-quality TRADE/SKIP.
       ctx = {settings, is_duplicate, similar_recent, confirmation_score,
              already_moved_pct, confirmation_limited}."""
    s = ctx["settings"]
    sl_pct, tp_pct = s["stop_loss_pct"], s["take_profit_pct"]

    # Not tradeable -> SKIP with low confidence.
    if cls["direction"] == "SKIP":
        noise = cls.get("noise")
        return {"trade": False, "symbol": None, "direction": "SKIP", "category": cls["category"],
                "impactScore": 0.0, "confidence": 0.20 if noise else 0.30,
                "reason": "noise / not tradeable" if noise else "no tradeable event matched",
                "scoreBreakdown": "", "stopLossPct": sl_pct, "takeProfitPct": tp_pct}

    norm = cls["norm"]
    urgency = urgency_weight(norm)
    ambiguous = ambiguity_penalty(norm) > 0

    source_w = source_weight(news.get("source", ""))
    event_w = float(cls["event_weight"])
    novelty_w = -2.0 if ctx.get("similar_recent") else 1.0
    confirm = float(ctx.get("confirmation_score", 0.0))
    dup_pen = 3.0 if ctx.get("is_duplicate") else 0.0
    amb_pen = ambiguity_penalty(norm)
    moved_pen = 3.0 if ctx.get("already_moved_pct", 0.0) > s["max_already_moved_pct"] else 0.0

    impact = source_w + event_w + urgency + novelty_w + confirm - dup_pen - amb_pen - moved_pen
    confidence = base_confidence(urgency, ambiguous)

    breakdown = (f"src{source_w:+g} evt{event_w:+g} urg{urgency:+g} nov{novelty_w:+g} "
                 f"confirm{confirm:+g} dup{-dup_pen:+g} amb{-amb_pen:+g} moved{-moved_pen:+g} "
                 f"= {impact:.1f}")
    if ctx.get("confirmation_limited"):
        breakdown += " (confirmation: limited price data)"

    # Signal-quality gates (the bot adds operational gates: ON, max-open, daily-loss, price).
    ok, fail = True, None
    if impact < s["min_impact_score"]:
        ok, fail = False, f"impact {impact:.1f} < min {s['min_impact_score']:g}"
    elif confidence < s["min_confidence"]:
        ok, fail = False, f"confidence {confidence:.2f} < min {s['min_confidence']:.2f}"
    elif ctx.get("is_duplicate"):
        ok, fail = False, "duplicate news within cooldown"
    elif confirm < 0:
        ok, fail = False, f"market confirmation negative ({confirm:+g})"
    elif moved_pen > 0:
        ok, fail = False, f"price already moved > {s['max_already_moved_pct']:g}%"

    return {"trade": ok, "symbol": cls["symbol"], "direction": cls["direction"],
            "category": cls["category"], "impactScore": round(impact, 2), "confidence": confidence,
            "reason": breakdown if ok else fail, "scoreBreakdown": breakdown,
            "stopLossPct": sl_pct, "takeProfitPct": tp_pct}
