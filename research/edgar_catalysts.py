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
import os
import pickle
import sys
import time
import urllib.error
import urllib.request
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

_MIN_INTERVAL = 1.0 / 8.0     # stay under SEC's 10/s ceiling
_last_call = [0.0]


def _get(url: str, retries: int = 3):
    for attempt in range(retries):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def ticker_to_cik() -> dict[str, str]:
    payload = _get(TICKER_MAP_URL) or {}
    out = {}
    for row in payload.values():
        symbol = str(row.get("ticker") or "").strip().upper()
        if symbol:
            out[symbol] = str(row.get("cik_str") or "").zfill(10)
    return out


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
    forms, dates = [], []
    for frame in frames:
        forms.extend(frame.get("form") or [])
        dates.extend(frame.get("filingDate") or [])
    if not forms:
        return None
    return {"form": forms, "filingDate": dates}


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
            "created_at": pd.Timestamp.utcnow().isoformat(),
            "source": "SEC EDGAR submissions API",
            "point_in_time_timestamps": True,
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


def days_since(calendar: pd.DatetimeIndex, when: pd.Timestamp) -> float:
    """Sessions-agnostic age in calendar days of the most recent filing at or before
    ``when``. Strictly backward-looking: a filing on a later date is never visible."""
    if calendar is None or len(calendar) == 0:
        return float("inf")
    pos = calendar.searchsorted(when, side="right") - 1
    if pos < 0:
        return float("inf")
    return float((when - calendar[pos]).days)
