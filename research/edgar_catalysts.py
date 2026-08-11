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
_feed_refresh_lock = threading.Lock()
_feed_status: dict[str, object] = {
    "status": "EMPTY", "last_attempt_at": 0.0, "last_success_at": 0.0,
    "error": "", "served_stale": False,
}
# Deep scans run every ten seconds. A five-minute feed cache made a newly filed 8-K
# invisible to roughly thirty scans, defeating catalyst-first discovery. One request
# every thirty seconds is still far below SEC's ten-requests/second fair-access limit.
CURRENT_FEED_TTL_SEC = 30
CURRENT_FEED_ERROR_RETRY_SEC = 30
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


def _events_with_current_age(events: list[dict[str, object]], max_age_hours: float,
                             now=None) -> list[dict[str, object]]:
    """Re-age cached filings so an old cache cannot freeze a filing inside the window."""
    current = now if now is not None else pd.Timestamp.now(tz="UTC")
    out: list[dict[str, object]] = []
    for event in events:
        accepted = pd.to_datetime(event.get("accepted_at"), utc=True, errors="coerce")
        if pd.isna(accepted):
            continue
        age_hours = max(0.0, float((current - accepted).total_seconds() / 3600.0))
        if age_hours <= max_age_hours:
            out.append(dict(event, age_hours=round(age_hours, 3)))
    return out


def _cached_current_feed(max_age_hours: float, wall_now: float, utc_now,
                         force: bool) -> list[dict[str, object]] | None:
    """Return a fresh cache or a failure-backoff cache; ``None`` means refresh."""
    if force:
        return None
    with _cache_lock:
        created, cached = _feed_cache
        status = dict(_feed_status)
    cache_fresh = created > 0 and wall_now - created < CURRENT_FEED_TTL_SEC
    retry_wait = (
        status.get("status") in ("DEGRADED", "FAILED")
        and wall_now - float(status.get("last_attempt_at") or 0)
        < CURRENT_FEED_ERROR_RETRY_SEC
    )
    if not cache_fresh and not retry_wait:
        return None
    return _events_with_current_age(cached, max_age_hours, utc_now)


def _feed_failure(error: str, previous: list[dict[str, object]], wall_now: float,
                  utc_now, max_age_hours: float) -> list[dict[str, object]]:
    """Keep last-known filings on a transient outage and expose the degraded state."""
    with _cache_lock:
        _feed_status.update({
            "status": "DEGRADED" if previous else "FAILED",
            "last_attempt_at": wall_now,
            "error": str(error)[:180],
            "served_stale": bool(previous),
        })
    return _events_with_current_age(previous, max_age_hours, utc_now)


def current_feed_status() -> dict[str, object]:
    """Read-only freshness telemetry for the scanner dashboard."""
    wall_now = time.time()
    with _cache_lock:
        created, cached = _feed_cache
        status = dict(_feed_status)
    cache_age = max(0.0, wall_now - created) if created > 0 else None
    current_events = _events_with_current_age(cached, 72.0)
    accepted = [str(row.get("accepted_at") or "") for row in current_events
                if str(row.get("accepted_at") or "")]
    if status.get("status") == "COMPLETE" and cache_age is not None \
            and cache_age >= CURRENT_FEED_TTL_SEC:
        status["status"] = "STALE"
    status.update({
        "ttl_sec": CURRENT_FEED_TTL_SEC,
        "cache_age_sec": round(cache_age, 1) if cache_age is not None else None,
        "event_count": len(current_events),
        "newest_accepted_at": max(accepted, default=""),
    })
    return status


def current_8k_events(max_age_hours: float = 72.0, force: bool = False) -> list[dict[str, object]]:
    """Recent 8-Ks from SEC's real-time feed, mapped to current ticker symbols.

    This is a discovery source, not a bullish label.  Every row retains its item codes
    so adverse events can be rejected and mixed disclosures are not called catalysts.
    Results are cached briefly because the deep scanner runs every ten seconds.
    """
    global _feed_cache
    wall_now = time.time()
    utc_now = pd.Timestamp.now(tz="UTC")
    cached = _cached_current_feed(max_age_hours, wall_now, utc_now, force)
    if cached is not None:
        return cached

    # Full scans used to call current_8k_tickers directly and indirectly through
    # screen() at the same time. Coalesce those callers into one SEC request.
    with _feed_refresh_lock:
        wall_now = time.time()
        utc_now = pd.Timestamp.now(tz="UTC")
        cached = _cached_current_feed(max_age_hours, wall_now, utc_now, force)
        if cached is not None:
            return cached
        with _cache_lock:
            previous = [dict(row) for row in _feed_cache[1]]

        ticker_map = ticker_to_cik()
        if not ticker_map:
            return _feed_failure(
                "SEC ticker map unavailable", previous, wall_now, utc_now,
                max_age_hours)
        by_cik: dict[str, list[str]] = {}
        for symbol, cik in ticker_map.items():
            by_cik.setdefault(cik, []).append(symbol)

        events: list[dict[str, object]] = []
        stop = False
        for start in range(0, CURRENT_FEED_MAX_ENTRIES, 100):
            raw = _request(CURRENT_FEED_URL.format(start=start))
            if not raw:
                return _feed_failure(
                    "SEC current 8-K feed unavailable", previous, wall_now,
                    utc_now, max_age_hours)
            try:
                entries = _feed_entries(raw)
            except (ET.ParseError, TypeError, ValueError) as error:
                return _feed_failure(
                    f"SEC current 8-K feed malformed: {type(error).__name__}",
                    previous, wall_now, utc_now, max_age_hours)
            if not entries:
                break
            for entry in entries:
                accepted = pd.to_datetime(
                    entry.get("accepted_at"), utc=True, errors="coerce")
                if pd.isna(accepted):
                    continue
                age_hours = max(
                    0.0, float((utc_now - accepted).total_seconds() / 3600.0))
                if age_hours > max_age_hours:
                    stop = True
                    continue
                classification = classify_8k_items(entry.get("items"))
                for symbol in by_cik.get(str(entry.get("cik") or ""), []):
                    events.append(dict(
                        entry, symbol=symbol, age_hours=round(age_hours, 3),
                        **classification))
            if stop:
                break

        # De-duplicate amendments/feed pagination by ticker+accession while preserving newest.
        unique: dict[tuple[str, str], dict[str, object]] = {}
        for event in sorted(
            events, key=lambda x: str(x.get("accepted_at") or ""), reverse=True
        ):
            unique.setdefault(
                (str(event.get("symbol")), str(event.get("accessionNumber"))), event)
        value = list(unique.values())
        completed_at = time.time()
        with _cache_lock:
            _feed_cache = (completed_at, [dict(x) for x in value])
            _feed_status.update({
                "status": "COMPLETE",
                "last_attempt_at": completed_at,
                "last_success_at": completed_at,
                "error": "",
                "served_stale": False,
            })
        return _events_with_current_age(value, max_age_hours, utc_now)


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
