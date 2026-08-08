"""
================================================================================
 ai_client.py  —  THE AI TRANSPORT  (works with ANY provider)
================================================================================
This is the ONE place that knows how to talk to your AI API. The rest of the bot
just calls `call_ai(system_prompt, user_prompt)` and gets back a string. You do
NOT need to know your provider in advance.

WHY this works for unknown providers: in 2026 almost every AI API speaks one of
two "dialects". Set AI_PROVIDER in config.py to pick:

  "openai"    OpenAI-compatible  POST {base_url}/chat/completions
              -> OpenAI, OpenRouter (one key for Claude/GPT/Llama/...), Groq,
                 Together, Fireworks, DeepSeek, xAI/Grok, Mistral, AND every
                 local model server (Ollama, llama.cpp, vLLM). If a provider's
                 docs mention an "OpenAI-compatible endpoint", use this. ~95% case.

  "anthropic" Anthropic native   POST {base_url}/messages
              -> only if you call Claude directly and not through OpenRouter.

  "raw"       A plain HTTP POST you shape by hand, for the rare provider whose
              format matches neither. Copy their docs' example body into _call_raw.

For ALL of them you set AI_API_KEY, AI_BASE_URL, AI_MODEL in config.py. To swap
providers later you change those + AI_PROVIDER. Nothing else in the bot changes.

LATENCY: a hosted call is ~300-3000 ms. A LOCAL model (set AI_PROVIDER="openai",
AI_BASE_URL="http://localhost:11434/v1") is ~50-300 ms — that's your fast path.
================================================================================
"""

from __future__ import annotations

import json

import config


# ---------------------------------------------------------------------------
# DISPATCH — the function the rest of the bot calls.
# ---------------------------------------------------------------------------
async def call_ai(system_prompt: str, user_prompt: str) -> str:
    """Return the model's raw text reply. Raises on failure (caller handles it)."""
    provider = config.AI_PROVIDER.lower()
    if provider == "openai":
        return await _call_openai_compatible(system_prompt, user_prompt)
    if provider == "anthropic":
        return await _call_anthropic(system_prompt, user_prompt)
    if provider == "raw":
        return await _call_raw(system_prompt, user_prompt)
    raise ValueError(f"Unknown AI_PROVIDER: {config.AI_PROVIDER!r}")


# ===========================================================================
# ADAPTER 1 — OpenAI-compatible (the default; covers almost everything)
# ===========================================================================
# pip install openai   (already in requirements.txt)
from openai import AsyncOpenAI

# Built on first use, not at import. Constructing it eagerly meant a missing key raised
# while this module was being imported, which took down every importer with it - the bot
# could not start and the test suite could not even be collected. A credential problem
# belongs at the call that needs the credential, in a message that names the fix.
_oai: AsyncOpenAI | None = None


def _client() -> AsyncOpenAI:
    global _oai
    if _oai is None:
        if not config.AI_API_KEY:
            raise RuntimeError(
                'No AI key configured. Set the GROQ_API_KEY environment variable '
                '(setx GROQ_API_KEY "...") or populate AI_API_KEY in config.py. '
                'Scanning and the risk gates run without it; only AI review is lost.'
            )
        _oai = AsyncOpenAI(
            api_key=config.AI_API_KEY,
            base_url=config.AI_BASE_URL,
            timeout=config.AI_TIMEOUT_SEC,
        )
    return _oai


async def _call_openai_compatible(system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Try forcing JSON output (supported by OpenAI + many others). Some providers
    # / local models reject this param, so fall back to a plain call if it errors.
    try:
        resp = await _client().chat.completions.create(
            model=config.AI_MODEL, messages=messages,
            temperature=0.2, max_tokens=1200,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = await _client().chat.completions.create(
            model=config.AI_MODEL, messages=messages,
            temperature=0.2, max_tokens=1200,
        )
    return resp.choices[0].message.content or ""


# ===========================================================================
# ADAPTER 2 — Anthropic native  (Claude, called directly)
# ===========================================================================
# pip install anthropic     (only needed if AI_PROVIDER="anthropic")
# Find the CURRENT model name to put in config.AI_MODEL (don't hardcode a stale
# one) at: https://docs.claude.com/en/api/overview
async def _call_anthropic(system_prompt: str, user_prompt: str) -> str:
    from anthropic import AsyncAnthropic   # imported lazily so it's optional
    client = AsyncAnthropic(api_key=config.AI_API_KEY, timeout=config.AI_TIMEOUT_SEC)
    msg = await client.messages.create(
        model=config.AI_MODEL,
        max_tokens=1200,
        system=system_prompt,                      # Anthropic puts system here
        messages=[{"role": "user", "content": user_prompt}],
    )
    # response content is a list of blocks; grab the text
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return "".join(parts)


# ===========================================================================
# ADAPTER 3 — RAW HTTP  (for ANY provider whose format is neither of the above)
# ===========================================================================
# This is your escape hatch for an API I can't predict. Steps:
#   1. Open your provider's "create a chat/generation" docs.
#   2. Copy their example request BODY into `payload` below and substitute the
#      system/user text + your model.
#   3. Copy their example RESPONSE shape and update the one line that extracts the
#      text (marked EXTRACT below).
# Auth is usually a Bearer token; change the header if your provider differs.
import aiohttp


async def _call_raw(system_prompt: str, user_prompt: str) -> str:
    url = f"{config.AI_BASE_URL.rstrip('/')}/chat/completions"  # <-- adjust path
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",         # <-- adjust if needed
        "Content-Type": "application/json",
    }
    # ---- EDIT THIS BODY to match your provider's docs --------------------
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    # ----------------------------------------------------------------------
    timeout = aiohttp.ClientTimeout(total=config.AI_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as r:
            data = await r.json()
    # ---- EXTRACT: change this to match your provider's response shape ----
    # Default assumes OpenAI-style: data["choices"][0]["message"]["content"]
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        # fall back to dumping the JSON so you can see the shape and fix the line
        return json.dumps(data)
    # ----------------------------------------------------------------------
