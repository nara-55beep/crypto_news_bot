"""
================================================================================
 dedup.py  —  CROSS-SOURCE NEWS DE-DUPLICATION  (exact match, fast, no network)
================================================================================
The problem this solves: the SAME story often arrives from MORE THAN ONE source
(e.g. both @BWEnews and @Cointelegraph post "SEC approves spot Bitcoin ETF"). The
per-source de-dupe in the Telegram and RSS listeners only catches the SAME message
from the SAME channel — it can't see that two DIFFERENT channels said the same
thing. This module sits at the single `on_news` chokepoint (see main.py) and drops
the second copy.

Method: EXACT match (the simplest, fastest option). For each headline it:
  1. NORMALISES the text — lowercase, strip URLs / @handles / $ / punctuation /
     emoji, drop common labels like "BREAKING:" / "JUST IN:", collapse whitespace.
     Two copies that differ only in punctuation or a prefix collapse to one string.
  2. Checks whether that exact normalised string was already seen inside the last
     WINDOW_SECONDS. If yes -> duplicate, skip it. If no -> remember it and allow.

Honest limit: this only catches (near-)IDENTICAL text. A genuine REWORD —
"Bitcoin ETF approved by the SEC" vs "SEC approves spot Bitcoin ETF" — has
different text and will NOT be caught. If reworded duplicates leak through, switch
to word-overlap matching (raise the question again and we can upgrade this file).

Only the last WINDOW_SECONDS of headlines are compared, so the same phrasing
recurring hours later (usually a NEW development) is not suppressed. The store is
bounded, so it runs for days without growing.
================================================================================
"""

from __future__ import annotations

import re
import time
from collections import deque

# ---- tuning ---------------------------------------------------------------
WINDOW_SECONDS = 15 * 60     # only compare against headlines seen in this window
MAX_REMEMBERED = 2000        # bound memory: keep at most this many recent items

# Word-overlap thresholds: two headlines are "the same story" if their significant
# words overlap enough. We treat it as a duplicate if ANY of these holds:
JACCARD_THRESH      = 0.5    #   shared/total words >= 50%
CONTAIN_THRESH      = 0.6    #   OR the shorter headline is >= 60% inside the longer
MIN_SHARED          = 5      #   OR they share at least this many significant words ...
MIN_SHARED_CONTAIN  = 0.4    #   ... and still overlap >= 40% (guards against long texts
                             #       coincidentally sharing 5 common words)

# Common filler words ignored when comparing (so verbs/articles don't dilute the match).
_STOPWORDS = set((
    "the a an of to in on at as be is are was were for and or but by it its this that "
    "with from into over after before about will would says said say has have had not no "
    "new now amid more than then out up down off per via you your we our they their he she"
).split())

# leading labels to strip so "BREAKING: X" and "X" compare equal
_LABELS = (
    "breaking", "breaking news", "just in", "update", "alert", "urgent",
    "exclusive", "developing", "flash", "news",
)
_LABEL_RE = re.compile(
    r"^(?:" + "|".join(re.escape(x) for x in _LABELS) + r")\s*[:\-]\s*",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Lowercase, strip a leading label, drop URLs and punctuation, collapse
    whitespace. Two trivially-different copies collapse to the same string."""
    s = (text or "").strip()
    s = _LABEL_RE.sub("", s)        # remove ONE leading "BREAKING:"-style label
    s = s.lower()
    s = _URL_RE.sub(" ", s)
    s = _NONWORD_RE.sub(" ", s)     # drops @, $, #, punctuation, emoji, etc.
    return _WS_RE.sub(" ", s).strip()


def _tokens(norm: str) -> frozenset:
    """Significant words of an already-normalised headline (drop short filler and
    stopwords), as a set for overlap comparison."""
    return frozenset(w for w in norm.split() if len(w) >= 3 and w not in _STOPWORDS)


def _same_story(a: frozenset, b: frozenset) -> bool:
    """True if two significant-word sets describe the same story (see thresholds)."""
    if not a or not b:
        return False
    inter = len(a & b)
    if not inter:
        return False
    union = len(a | b)
    contain = inter / min(len(a), len(b))
    jaccard = inter / union
    if jaccard >= JACCARD_THRESH:
        return True
    if contain >= CONTAIN_THRESH:
        return True
    if inter >= MIN_SHARED and contain >= MIN_SHARED_CONTAIN:
        return True
    return False


class _Deduper:
    """Remembers recently-seen headlines and flags repeats — exact OR reworded
    (same story, different words) via significant-word overlap."""

    def __init__(self):
        # (timestamp, normalised_text, token_set); newest on the right, evict left
        self._items: deque = deque()
        self._seen: set[str] = set()

    def _evict(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._items and (self._items[0][0] < cutoff
                               or len(self._items) > MAX_REMEMBERED):
            _ts, old, _toks = self._items.popleft()
            self._seen.discard(old)

    def is_duplicate(self, text: str) -> bool:
        """True if this headline repeats one seen in the window — identical OR a
        reworded version of the same story. Records non-duplicates so the next
        copy (in any wording) is caught."""
        norm = _normalise(text)
        if not norm:
            return False
        now = time.time()
        self._evict(now)
        if norm in self._seen:              # fast path: exact (normalised) repeat
            return True
        toks = _tokens(norm)
        if toks:                            # fuzzy path: same story, different words
            for _ts, _norm, _toks in self._items:
                if _same_story(toks, _toks):
                    return True
        self._seen.add(norm)
        self._items.append((now, norm, toks))
        return False


# module-level singleton: one shared memory for the whole app
_DEDUPER = _Deduper()


def is_duplicate(text: str) -> bool:
    """True if `text` repeats (after normalising) a headline seen within the last
    WINDOW_SECONDS from ANY source. Records non-duplicates so the next copy is
    caught. Safe to call on the fast path — no network, microsecond-fast."""
    return _DEDUPER.is_duplicate(text)
