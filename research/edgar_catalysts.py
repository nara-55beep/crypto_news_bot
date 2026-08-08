"""Point-in-time catalyst calendars from SEC EDGAR.

Everything else in this project's research is hampered by the same problem: the free
data describes today, not the past. Yahoo's news feed has no usable history, fundamentals
are a current snapshot, and reconstructed earnings estimates are today's numbers wearing
a past date.

EDGAR is the exception. A filing's ``filingDate`` is the day the document actually hit
the wire, recorded contemporaneously and never restated. That makes it the one genuinely
point-in-time catalyst source available here for free, and it is exactly the kind of
event the live desk now insists on before it will call anything a setup.

Two calendars are built per symbol:

* **news catalysts** - 8-K (material event) and its amendments. This is the closest free
  proxy for "something actually happened today".
* **dilution events** - S-1/S-3/F-1/F-3/424B/EFFECT. The live bot already refuses names
  with imminent dilution; this lets that rule be tested rather than assumed.

Survivorship still applies: EDGAR is complete for the companies in the panel, but the
panel itself only contains names listed today. This narrows what the data can prove, and
it does not undo the improvement in *timing* fidelity.

SEC asks for a descriptive User-Agent and no more than 10 requests/second; both are
honoured below.
"""
from __future__ import annotations

import json
import re
import os
import pickle
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_PATH = ROOT / "research" / "cache" / "edgar_filings.pkl"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
USER_AGENT = os.getenv("SEC_USER_AGENT", "crypto-news-bot research dht5152@gmail.com")

NEWS_FORMS = ("8-K", "8-K/A")
DILUTION_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B", "EFFECT")
SCHEMA_VERSION = 2
FILING_FIELDS = (
    "form",
    "filingDate",
    "acceptanceDateTime",
    "reportDate",
    "accessionNumber",
    "primaryDocument",
    "items",
)

# 8-K item codes are materially different events.  Keeping these sets here gives the
# live/research layers one canonical, testable interpretation instead of treating a
# delisting notice and an earnings release as the same "fresh filing" catalyst.
NEGATIVE_8K_ITEMS = frozenset({"1.03", "2.04", "2.06", "3.01", "3.02", "4.02"})
EARNINGS_8K_ITEMS = frozenset({"2.02"})
AGREEMENT_8K_ITEMS = frozenset({"1.01", "2.01"})

_MIN_INTERVAL = 1.0 / 8.0     # stay under SEC's 10/s ceiling
_last_call = [0.0]
_throttle_lock = threading.Lock()
_ticker_cache: tuple[float, dict[str, str]] = (0.0, {})
_feed_cache: tuple[float, list[dict[str, object]]] = (0.0, [])
_cache_lock = threading.Lock()
CURRENT_FEED_TTL_SEC = 5 * 60
# The service polls continuously.  Reading the newest page each cycle catches new
# disclosures with bounded latency; walking ten historical pages blocked the scanner
# for roughly 100 seconds and mostly rediscovered stale, non-penny issuers.
CURRENT_FEED_MAX_ENTRIES = 100
CURRENT_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&company=&dateb=&owner=include&start={start}&count=100&output=atom"
)


def _request(url: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        # ``download`` is threaded.  Without a lock every worker observes the same
        # timestamp and they all fire together, defeating the SEC rate limit despite
        # the apparent delay.  Serialize only the short throttle window, not I/O.
        with _throttle_lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _get(url: str, retries: int = 3):
    raw = _request(url, retries=retries)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def ticker_to_cik(force: bool = False) -> dict[str, str]:
    global _ticker_cache
    with _cache_lock:
        if not force and _ticker_cache[1] and time.time() - _ticker_cache[0] < 86400:
            return dict(_ticker_cache[1])
    payload = _get(TICKER_MAP_URL) or {}
    out = {}
    for row in payload.values():
        symbol = str(row.get("ticker") or "").strip().upper()
        if symbol:
            out[symbol] = str(row.get("cik_str") or "").zfill(10)
    if out:
        with _cache_lock:
            _ticker_cache = (time.time(), dict(out))
    return out


def _feed_entries(raw: bytes) -> list[dict[str, str]]:
    """Parse the SEC current-filings Atom feed without trusting HTML text as data."""
    root = ET.fromstring(raw)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns)
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        identity = entry.findtext("atom:id", default="", namespaces=ns)
        summary = entry.findtext("atom:summary", default="", namespaces=ns)
        link = entry.find("atom:link", ns)
        href = link.attrib.get("href", "") if link is not None else ""
        cik_match = re.search(r"\((\d{6,10})\)\s+\(Filer\)", title)
        if not cik_match:
            cik_match = re.search(r"/data/(\d+)/", href)
        accession_match = re.search(r"accession-number=([0-9-]+)", identity)
        items = re.findall(r"Item\s+([0-9]+\.[0-9]+)\s*:", summary or "")
        out.append({
            "cik": str(cik_match.group(1)).zfill(10) if cik_match else "",
            "company": re.sub(r"^8-K(?:/A)?\s*-\s*", "", title).split(" (")[0].strip(),
            "accepted_at": updated,
            "accessionNumber": accession_match.group(1) if accession_match else "",
            "url": href,
            "items": ",".join(items),
        })
    return out


def current_8k_events(max_age_hours: float = 72.0, force: bool = False) -> list[dict[str, object]]:
    """Recent 8-Ks from SEC's real-time feed, mapped to current ticker symbols.

    This is a discovery source, not a bullish label.  Every row retains its item codes
    so adverse events can be rejected and mixed disclosures are not called catalysts.
    Results are cached because the scanner runs every minute when a setup is hot.
    """
    global _feed_cache
    now = pd.Timestamp.now(tz="UTC")
    with _cache_lock:
        if (not force and _feed_cache[1]
                and time.time() - _feed_cache[0] < CURRENT_FEED_TTL_SEC):
            return [dict(x) for x in _feed_cache[1]
                    if float(x.get("age_hours", 1e9)) <= max_age_hours]

    by_cik: dict[str, list[str]] = {}
    for symbol, cik in ticker_to_cik().items():
        by_cik.setdefault(cik, []).append(symbol)

    events: list[dict[str, object]] = []
    stop = False
    for start in range(0, CURRENT_FEED_MAX_ENTRIES, 100):
        raw = _request(CURRENT_FEED_URL.format(start=start))
        if not raw:
            break
        entries = _feed_entries(raw)
        if not entries:
            break
        for entry in entries:
            accepted = pd.to_datetime(entry.get("accepted_at"), utc=True, errors="coerce")
            if pd.isna(accepted):
                continue
            age_hours = max(0.0, float((now - accepted).total_seconds() / 3600.0))
            if age_hours > max_age_hours:
                stop = True
                continue
            classification = classify_8k_items(entry.get("items"))
            for symbol in by_cik.get(str(entry.get("cik") or ""), []):
                events.append(dict(entry, symbol=symbol, age_hours=round(age_hours, 3),
                                   **classification))
        if stop:
            break

    # De-duplicate amendments/feed pagination by ticker+accession while preserving newest.
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for event in sorted(events, key=lambda x: str(x.get("accepted_at") or ""), reverse=True):
        unique.setdefault((str(event.get("symbol")), str(event.get("accessionNumber"))), event)
    value = list(unique.values())
    with _cache_lock:
        _feed_cache = (time.time(), [dict(x) for x in value])
    return value


def current_8k_tickers(max_age_hours: float = 72.0) -> list[str]:
    """Ticker discovery ordered newest first; safety classification happens later."""
    return list(dict.fromkeys(str(x["symbol"]) for x in current_8k_events(max_age_hours)
                              if x.get("symbol")))


def current_8k_for_symbol(symbol: str, max_age_hours: float = 72.0) -> list[dict[str, object]]:
    wanted = str(symbol or "").strip().upper()
    return [x for x in current_8k_events(max_age_hours) if x.get("symbol") == wanted]


def parse_items(value) -> tuple[str, ...]:
    """Return normalized SEC 8-K item codes without inventing missing metadata."""
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    return tuple(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))


def classify_8k_items(value) -> dict[str, object]:
    """Small deterministic taxonomy used by both the audit and live safety gate."""
    items = frozenset(parse_items(value))
    return {
        "items": sorted(items),
        "earnings": bool(items & EARNINGS_8K_ITEMS),
        "agreement_or_asset_sale": bool(items & AGREEMENT_8K_ITEMS),
        "negative": bool(items & NEGATIVE_8K_ITEMS),
        "negative_items": sorted(items & NEGATIVE_8K_ITEMS),
    }


def _aligned_rows(frames: list[dict]) -> list[dict[str, str]]:
    """Convert SEC column arrays to aligned rows.

    EDGAR occasionally omits a value from an optional column.  Extending ``form`` and
    ``filingDate`` independently (the old implementation) silently loses every other
    point-in-time field and makes later joins impossible.  Indexing each frame keeps an
    accession, acceptance timestamp, document and item list tied to the same filing.
    """
    rows: list[dict[str, str]] = []
    for frame in frames:
        forms = frame.get("form") or []
        for index, form in enumerate(forms):
            row = {}
            for field in FILING_FIELDS:
                values = frame.get(field) or []
                row[field] = str(values[index] or "") if index < len(values) else ""
            if row["form"] and row["filingDate"]:
                rows.append(row)
    return rows


def _filings_for(cik: str) -> dict | None:
    """Recent filings plus the older archive pages EDGAR splits long histories into."""
    payload = _get(SUBMISSIONS_URL.format(cik=cik))
    if not payload:
        return None
    frames = [payload.get("filings", {}).get("recent") or {}]
    for extra in (payload.get("filings", {}).get("files") or []):
        name = extra.get("name")
        if not name:
            continue
        older = _get(f"https://data.sec.gov/submissions/{name}")
        if older:
            frames.append(older)
    rows = _aligned_rows(frames)
    if not rows:
        return None
    return {field: [row[field] for row in rows] for field in FILING_FIELDS}


def download(symbols: list[str], workers: int = 6) -> dict:
    """Fetch filing histories. Threaded, but the shared throttle keeps SEC's limit."""
    mapping = ticker_to_cik()
    wanted = {s: mapping[s] for s in symbols if s in mapping}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_filings_for, cik): sym for sym, cik in wanted.items()}
        for fut in as_completed(futures):
            symbol = futures[fut]
            try:
                got = fut.result()
            except Exception:
                got = None
            if got:
                out[symbol] = got

    payload = {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "created_at": pd.Timestamp.utcnow().isoformat(),
            "source": "SEC EDGAR submissions API",
            "point_in_time_timestamps": True,
            "has_acceptance_timestamps": True,
            "has_8k_item_codes": True,
            "survivorship_free": False,
            "requested": len(symbols),
            "mapped_to_cik": len(wanted),
            "with_filings": len(out),
        },
        "filings": out,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, CACHE_PATH)
    return payload


def load(symbols: list[str] | None = None, refresh: bool = False) -> dict:
    if refresh or not CACHE_PATH.exists():
        if not symbols:
            raise ValueError("symbols are required to build the EDGAR cache")
        return download(symbols)
    with CACHE_PATH.open("rb") as f:
        return pickle.load(f)


def calendars(payload: dict) -> dict[str, dict[str, pd.DatetimeIndex]]:
    """Turn raw filing lists into sorted news/dilution date indexes per symbol."""
    out: dict[str, dict[str, pd.DatetimeIndex]] = {}
    for symbol, rec in (payload.get("filings") or {}).items():
        forms = rec.get("form") or []
        dates = rec.get("filingDate") or []
        news, dilution = [], []
        for form, date in zip(forms, dates):
            f = str(form).upper()
            if f in NEWS_FORMS:
                news.append(date)
            elif any(f.startswith(p) for p in DILUTION_FORMS):
                dilution.append(date)
        out[symbol] = {
            "news": pd.DatetimeIndex(pd.to_datetime(news, errors="coerce")).dropna().sort_values(),
            "dilution": pd.DatetimeIndex(pd.to_datetime(dilution, errors="coerce")).dropna().sort_values(),
        }
    return out


def filing_rows(payload: dict, symbol: str) -> list[dict[str, object]]:
    """Return aligned filing records for one ticker, including item classification.

    Old version-1 caches remain readable: absent fields are returned as empty strings.
    Research that requires exact acceptance time or item codes must explicitly require
    ``metadata.schema_version >= 2`` and refresh instead of pretending old data qualify.
    """
    rec = (payload.get("filings") or {}).get(str(symbol).upper()) or {}
    rows = _aligned_rows([rec])
    out: list[dict[str, object]] = []
    for row in rows:
        value: dict[str, object] = dict(row)
        if row["form"].upper() in NEWS_FORMS:
            value.update(classify_8k_items(row.get("items")))
        else:
            value.update({
                "items": list(parse_items(row.get("items"))),
                "earnings": False,
                "agreement_or_asset_sale": False,
                "negative": False,
                "negative_items": [],
            })
        out.append(value)
    return out


def days_since(calendar: pd.DatetimeIndex, when: pd.Timestamp) -> float:
    """Sessions-agnostic age in calendar days of the most recent filing at or before
    ``when``. Strictly backward-looking: a filing on a later date is never visible."""
    if calendar is None or len(calendar) == 0:
        return float("inf")
    pos = calendar.searchsorted(when, side="right") - 1
    if pos < 0:
        return float("inf")
    return float((when - calendar[pos]).days)
