"""
================================================================================
 web_research.py  —  FREE WEB SEARCH  (DuckDuckGo HTML, no key)
================================================================================
Gives the AI live context for a headline. Used in the "two-pass" flow:
  pass 1: AI says which queries it wants (e.g. "what is the Iran nuclear deal")
  here:   we fetch search-result snippets for those queries (free)
  pass 2: AI re-decides WITH that context

REALITY CHECK (you chose free scraping, so know the trade-offs):
  - DuckDuckGo's free HTML endpoint rate-limits and sometimes blocks bots.
  - During busy news it may return nothing. That's expected.
  - On ANY failure we return "" and the bot just lets the AI decide on its own
    knowledge -> never worse than having no search at all.
  - Each search adds ~1-3s. With a few queries per researched headline, a TRADE
    decision takes a few seconds. (You accepted this for researched trades.)
================================================================================
"""

from __future__ import annotations

import asyncio
import re

import aiohttp

DDG_HTML = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}

# How many result snippets to keep per query, and overall caps (keep the AI
# prompt small + fast).
RESULTS_PER_QUERY = 4
MAX_QUERIES = 3
PER_QUERY_TIMEOUT = 6.0
SNIPPET_CHARS = 220


async def _search_one(session: aiohttp.ClientSession, query: str) -> list[str]:
    """Return a few text snippets for one query. [] on any failure."""
    try:
        async with session.post(
                DDG_HTML, data={"q": query},
                timeout=aiohttp.ClientTimeout(total=PER_QUERY_TIMEOUT)) as r:
            if r.status != 200:
                return []
            html = await r.text()
    except Exception:
        return []

    # DuckDuckGo HTML puts result text in <a class="result__snippet">...</a>.
    # We strip tags crudely (no extra libraries needed).
    snippets = []
    for m in re.findall(r'class="result__snippet".*?>(.*?)</a>', html, re.S):
        text = re.sub(r"<.*?>", "", m)          # remove inner tags
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            snippets.append(text[:SNIPPET_CHARS])
        if len(snippets) >= RESULTS_PER_QUERY:
            break
    return snippets


async def research(queries: list[str]) -> str:
    """Run up to MAX_QUERIES searches and return a compact context block.
    Returns "" if nothing useful came back (caller then just lets the AI decide)."""
    queries = [q for q in (queries or []) if q and q.strip()][:MAX_QUERIES]
    if not queries:
        return ""
    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        results = await asyncio.gather(*[_search_one(session, q) for q in queries])

    blocks = []
    for q, snips in zip(queries, results):
        if snips:
            joined = " | ".join(snips)
            blocks.append(f"Search '{q}':\n{joined}")
    return "\n\n".join(blocks)
