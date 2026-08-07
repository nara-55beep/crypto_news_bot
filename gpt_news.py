"""
================================================================================
 gpt_news.py  —  AI (Groq / gpt-oss-120b) news → BTC trade decision  ("GPTnews")
================================================================================
Same job as gemini_trader.py (decide trade/skip, direction, stop-loss, take-profit
for BTC), but the transport is GROQ's OpenAI-compatible endpoint instead of Gemini.
Free + fast. Reuses the exact same trading prompt + JSON parsing as the Gemini
strategy, so the two are directly comparable.

Config lives in config.py: GROQ_API_KEY / GROQ_BASE_URL / GROQ_MODEL.
================================================================================
"""

from __future__ import annotations

import time

import config
import gemini_trader as G          # reuse SYSTEM_PROMPT, _build_user, _clamp, GeminiDecision
from ai_analyzer import _safe_json

# Groq speaks the OpenAI dialect, so we use the already-installed openai library
# pointed at Groq's endpoint — no extra dependency.
from openai import AsyncOpenAI

_client = AsyncOpenAI(
    api_key=getattr(config, "GROQ_API_KEY", ""),
    base_url=getattr(config, "GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    timeout=getattr(config, "GROQ_TIMEOUT_SEC", 8.0),
)
_MODEL = getattr(config, "GROQ_MODEL", "openai/gpt-oss-120b")


async def decide(news: str, snap: dict) -> "G.GeminiDecision":
    """Ask Groq for a full BTC trade plan. Never raises — returns a non-trade
    decision on any failure so the bot simply skips. (Same shape as Gemini's.)"""
    t0 = time.perf_counter()
    d = G.GeminiDecision()
    messages = [
        {"role": "system", "content": G.SYSTEM_PROMPT},
        {"role": "user", "content": G._build_user(news, snap)},
    ]
    # gpt-oss is a reasoning model; keep reasoning light so it answers fast.
    kwargs = dict(model=_MODEL, messages=messages, temperature=0.2,
                  max_tokens=2048, extra_body={"reasoning_effort": "low"})
    try:
        try:                                   # try strict JSON mode first
            resp = await _client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs)
        except Exception:                      # some models reject it → plain call
            resp = await _client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        data = _safe_json(text or "{}")
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


# ---- live exit manager: the AI watches the OPEN trade and decides when to get out ----
from dataclasses import dataclass as _dc


@_dc
class ManageDecision:
    action: str = "hold"     # "hold" | "close"
    reason: str = ""
    error: str = ""
    latency_ms: float = 0.0


MANAGE_PROMPT = """You are a professional crypto desk trader actively MANAGING an open \
BTC perpetual-futures position you took on a news catalyst. You are watching it live, \
tick by tick. Your only job right now: decide whether to HOLD the position or CLOSE it \
RIGHT NOW. There is no fixed stop-loss or take-profit — YOUR judgment is the exit.

Think like a pro running real risk:
- CLOSE to take profit when the move from the news has largely played out, momentum is \
fading, or price stalls after running your way (don't give back a good gain).
- CLOSE to cut the trade when price is turning against you, the news reaction failed, \
or the edge is gone — cut fast, don't hope.
- HOLD while the move is clearly still going your way and momentum is with you — let a \
winner run.
Bias slightly toward protecting capital: a flat or fading trade is usually a CLOSE. \
You can always re-enter on the next catalyst.

Answer with ONLY this JSON and nothing else:
{"action": "hold" or "close", "reason": "one short clause"}"""


def _build_manage_user(ctx: dict) -> str:
    return (
        f"OPEN {str(ctx.get('side','')).upper()} taken on this news: {ctx.get('news','')}\n"
        f"entry price        = {ctx.get('entry')}\n"
        f"current price       = {ctx.get('price')}\n"
        f"unrealized (in your favor) = {ctx.get('pnl_pct')}%\n"
        f"held for            = {ctx.get('held_s')}s\n"
        f"price move last 10s = {ctx.get('move_10s')}%\n"
        f"taker flow (1m)     = {ctx.get('flow_buy_pct')}% buy"
    )


async def manage(ctx: dict) -> "ManageDecision":
    """Ask the AI whether to HOLD or CLOSE the open position. Never raises — on any
    failure returns HOLD (so a flaky AI call can't accidentally close a good trade;
    the bot's catastrophic backstop handles the worst case)."""
    t0 = time.perf_counter()
    m = ManageDecision()
    messages = [
        {"role": "system", "content": MANAGE_PROMPT},
        {"role": "user", "content": _build_manage_user(ctx)},
    ]
    kwargs = dict(model=_MODEL, messages=messages, temperature=0.2,
                  max_tokens=1024, extra_body={"reasoning_effort": "low"})
    try:
        try:
            resp = await _client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs)
        except Exception:
            resp = await _client.chat.completions.create(**kwargs)
        data = _safe_json(resp.choices[0].message.content or "{}")
        act = str(data.get("action", "hold")).strip().lower()
        m.action = "close" if act == "close" else "hold"
        m.reason = str(data.get("reason", ""))[:120]
    except Exception as e:
        m.action = "hold"
        m.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        m.latency_ms = (time.perf_counter() - t0) * 1000.0
    return m
