"""
================================================================================
 rss_listener.py  —  RSS INGESTION  (free, polled)
================================================================================
Polls RSS feeds on an interval and forwards NEW entries to the bot. Two groups:

  * FAST_FEEDS  — built in below (e.g. FinancialJuice), polled every
    FAST_POLL_SECONDS (~1s) using conditional GETs. Edit this list to add/remove
    fast RSS sources WITHOUT touching config.py.
  * config.RSS_FEEDS — your optional macro feeds, polled every
    config.RSS_POLL_SECONDS (unchanged).

HONEST latency note: RSS is POLLED, so the best case is "feed update time +
poll interval". And FinancialJuice's FREE feed is DELAYED on purpose vs their
paid real-time squawk (their site literally says "GO REAL-TIME"). We cannot
remove that delay without a paid FinancialJuice login. So treat this as broad
COVERAGE, not your fastest signal — Telegram (instant, no paywall) is faster.
================================================================================
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import aiohttp
import feedparser

import config


# --- fast feeds: (display_name, url). Added here so you only drop in this file.
FAST_FEEDS = [
    ("FinancialJuice", "https://www.financialjuice.com/feed.ashx?xy=rss"),
]
FAST_POLL_SECONDS = 1.0
# Anti-flood: minimum seconds between forwarded items PER feed (0 = off, forward
# everything). Raise this (e.g. 5-10) if a feed like FinancialJuice floods you
# during big news, or just remove that feed from FAST_FEEDS above.
FAST_MIN_GAP_SECONDS = 0.0

# polite browser-like header; some feed servers reject the bare client default
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; news-bot/1.0)"}

# strip these leading labels from titles so the feed shows the clean headline
_TITLE_PREFIXES = ("FinancialJuice: ", "FinancialJuice:")


def _clean_title(title: str) -> str:
    title = title or ""
    for pre in _TITLE_PREFIXES:
        if title.startswith(pre):
            return title[len(pre):].strip()
    return title.strip()


def _norm(s: str) -> str:
    """Normalise a headline so trivially-different copies collapse to one key."""
    return " ".join((s or "").lower().split())


class _Recent:
    """A 'seen' set that remembers only the last `cap` keys (evicts the oldest),
    so it can run for days without growing without bound."""
    def __init__(self, cap: int = 5000):
        self.cap = cap
        self._s: set[str] = set()
        self._q: deque[str] = deque()

    def __contains__(self, k: str) -> bool:
        return k in self._s

    def add(self, k: str) -> None:
        if k in self._s:
            return
        self._s.add(k)
        self._q.append(k)
        if len(self._q) > self.cap:
            self._s.discard(self._q.popleft())


async def _run_feeds(session, feeds, interval, on_news, label):
    """Poll one group of feeds forever, forwarding only genuinely NEW entries.

    Backlog handling (the important bit): each feed's existing items are absorbed
    ONCE, on the first *successful* fetch of that feed — and we only mark it
    'seeded' after a fetch that actually parsed entries. So if the very first
    fetch on startup is slow, errors, or comes back empty, we DON'T flip the flag
    and accidentally replay the whole feed on the next poll. This is what stops a
    restart from dumping a wall of old headlines.

    Uses CONDITIONAL GETs (ETag / If-Modified-Since) so unchanged feeds return a
    tiny '304 Not Modified' and we can poll fast (1s) without hammering the server.
    """
    seen = _Recent()                 # dedups by BOTH id and headline text
    cond: dict = {}                  # url -> (etag, last_modified)
    seeded: set[str] = set()         # urls whose existing backlog we've absorbed
    last_fire: dict[str, float] = {}  # url -> monotonic ts of last forwarded item
    announced = False
    while True:
        for name, url in feeds:
            try:
                headers = dict(_HEADERS)
                etag, last_mod = cond.get(url, (None, None))
                if etag:
                    headers["If-None-Match"] = etag
                if last_mod:
                    headers["If-Modified-Since"] = last_mod
                async with session.get(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 304:
                        continue          # nothing new since last poll — skip cheaply
                    body = await r.text()
                    cond[url] = (r.headers.get("ETag", etag),
                                 r.headers.get("Last-Modified", last_mod))
                feed = feedparser.parse(body)
                entries = list(reversed(feed.entries))   # feedparser is newest-first
                if not entries:
                    continue              # parsed nothing — do NOT mark seeded; retry later
                seeding = url not in seeded               # first good fetch for this feed
                fired = 0
                for entry in entries:
                    title = _clean_title(entry.get("title", ""))
                    uid = (entry.get("id") or entry.get("guid")
                           or entry.get("link") or title)
                    # KEY by the headline text (the feed's ids are not stable, so
                    # the same headline can come back with a new id — that's the
                    # spam). If we've shown this id OR this exact headline, skip it.
                    k_uid = ("u:" + uid) if uid else None
                    k_txt = ("t:" + _norm(title)) if title else None
                    if (k_uid and k_uid in seen) or (k_txt and k_txt in seen):
                        continue
                    if k_uid:
                        seen.add(k_uid)
                    if k_txt:
                        seen.add(k_txt)
                    if seeding:
                        continue                 # absorb existing backlog silently
                    summary = (entry.get("summary", "") or "").strip()
                    text = (title + (". " + summary if summary else "")).strip()
                    if not text:
                        continue
                    if FAST_MIN_GAP_SECONDS > 0:          # optional anti-flood
                        t = time.monotonic()
                        if t - last_fire.get(url, 0.0) < FAST_MIN_GAP_SECONDS:
                            continue
                        last_fire[url] = t
                    src = name or feed.feed.get("title", url)
                    try:
                        await on_news(f"rss:{src}", text)
                        fired += 1
                    except Exception as e:
                        print(f"[rss] on_news error: {type(e).__name__}: {e}")
                seeded.add(url)                           # mark only after a real parse
                if seeding:
                    print(f"[rss] {name or url}: absorbed existing backlog on startup "
                          f"(won't replay it)")
            except Exception as e:
                print(f"[rss] error on {url}: {type(e).__name__}: {e}")
        if not announced:
            print(f"[rss] {label} live — polling {len(feeds)} feed(s) every {interval:g}s "
                  f"(conditional GET)")
            announced = True
        await asyncio.sleep(interval)


async def start_rss(on_news):
    """on_news(source, text) async callable. Runs forever. Polls the built-in
    FAST_FEEDS (~2s) and any config.RSS_FEEDS (slower) concurrently."""
    async with aiohttp.ClientSession() as session:
        loops = []
        general = [(None, u) for u in getattr(config, "RSS_FEEDS", []) or []]
        if general:
            loops.append(_run_feeds(session, general,
                                    getattr(config, "RSS_POLL_SECONDS", 20),
                                    on_news, "config feeds"))
        if FAST_FEEDS:
            loops.append(_run_feeds(session, FAST_FEEDS, FAST_POLL_SECONDS,
                                    on_news, "fast feeds"))
        if not loops:
            return
        await asyncio.gather(*loops)
