"""
penny_quotes.py - execution-side quote capture for the penny-stock evidence archive.

Measuring a round trip needs the book on BOTH sides. Entry quotes arrive with the scan,
but the exit leg was never observed at all: outcomes ran the last TRADE to a future
close and subtracted the entry-time spread once, as though the whole round trip had been
paid on the way in. A name quoted 1.00/1.20 that closed at 1.20 booked +1.82% when the
executable ask->bid trip was -0.83%. This module captures a timestamped bid/ask for
every tracked signal, so the sell side becomes an observation instead of an assumption.

FEED IDENTITY IS PART OF THE OBSERVATION.
Alpaca's free tier is IEX: a single venue, not the consolidated SIP tape. An IEX quote
is a real quote - it is NOT the NBBO, and must never be recorded or described as one.
On a thin penny name the national best bid can sit well inside or outside what IEX alone
shows, and that gap is exactly the quantity being measured. Every observation therefore
carries the feed it came from, so an audit can separate iex from sip rather than pooling
them into one number that means nothing.

Read-only market data. Nothing here places an order.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

NY = ZoneInfo("America/New_York")
MARKET_OPEN_MINUTE = 9 * 60 + 30
MARKET_CLOSE_MINUTE = 16 * 60
# A quote is the session's CLOSE only if it was taken at the close. An opening or midday
# book is a different price at a different time, and calling one a closing exit compares
# it against a daily bar it never belonged to.
CLOSING_WINDOW_MINUTES = (15 * 60 + 55, 16 * 60 + 5)
# Venue books. Yahoo's blended quote is explicitly not one of these: its freshness is
# derived from the last TRADE timestamp rather than the bid/ask, so it cannot establish
# what a buy would have paid.
EXECUTION_FEEDS = {"iex", "sip"}
# Alpaca stamps nanoseconds; fromisoformat accepts microseconds at most.
_SUBSECOND = re.compile(r"^(.*\.\d{6})\d+(.*)$")

try:
    import aiohttp
except Exception:                                   # optional at import time
    aiohttp = None

# What each feed actually is, kept next to the data so a reader cannot mistake one for
# the other. The label travels with the observation into the archive.
FEED_DESCRIPTIONS = {
    "iex": "IEX only - single venue, NOT the consolidated NBBO",
    "sip": "SIP - consolidated tape (all venues)",
    "yfinance": "Yahoo delayed/blended book - not an execution feed",
}
QUOTE_TIMEOUT_SEC = 10
# Alpaca accepts a comma-separated symbol list; keep requests small enough to stay well
# inside URL limits when a lot of names are being tracked at once.
MAX_SYMBOLS_PER_REQUEST = 100


def feed_name() -> str:
    return str(getattr(config, "ALPACA_FEED", "") or "").strip().lower()


def feed_description(feed: str | None = None) -> str:
    key = (feed or feed_name()).strip().lower()
    return FEED_DESCRIPTIONS.get(key, f"{key or 'unknown'} - unrecognised feed")


def is_consolidated(feed: str | None = None) -> bool:
    """Whether a feed is the full tape. Only SIP is; IEX is one venue."""
    return (feed or feed_name()).strip().lower() == "sip"


def trading_url() -> str:
    """Alpaca's TRADING host. /v2/calendar does not live on the data host."""
    return str(getattr(config, "ALPACA_TRADING_URL", "")
               or "https://api.alpaca.markets").rstrip("/")


async def fetch_calendar(start: date, end: date) -> tuple[dict, str]:
    """Real session times from Alpaca's market calendar, keyed by date.

    Returns ({"YYYY-MM-DD": {"open_minute", "close_minute"}}, error). This is the
    authority on when a session ends, including early closures - a hand-written holiday
    rule is a guess about the NYSE calendar, and one wrong guess a year is a session
    whose closing book is never captured.

    A date ABSENT from a successful response is not a trading day. That distinction
    matters: a holiday must stop capture outright, not run it against a dark market and
    record nothing while believing it tried.
    """
    if not configured():
        return {}, ("alpaca calendar unavailable: no ALPACA_API_KEY / ALPACA_SECRET_KEY"
                    if aiohttp is not None else
                    "alpaca calendar unavailable: aiohttp is not installed")
    url = trading_url() + "/v2/calendar"
    params = {"start": start.isoformat(), "end": end.isoformat()}
    timeout = aiohttp.ClientTimeout(total=QUOTE_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(headers=_headers(), timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    body = (await response.text())[:160]
                    return {}, f"HTTP {response.status}: {body}"
                payload = await response.json()
    except Exception as e:
        return {}, f"{type(e).__name__}: {str(e)[:120]}"

    def minutes(text) -> int | None:
        parts = str(text or "").split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        return int(parts[0]) * 60 + int(parts[1])

    sessions = {}
    for row in (payload or []):
        day = str((row or {}).get("date") or "").strip()
        close = minutes((row or {}).get("close"))
        if not day or close is None:
            continue
        sessions[day] = {"open_minute": minutes(row.get("open")),
                         "close_minute": close}
    return sessions, ""


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The nth given weekday of a month (Monday == 0)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def is_us_half_day(day: date) -> bool:
    """Whether the U.S. tape closes early (13:00 ET) on this session.

    A fallback for when the provider cannot tell us, not the primary source. The three
    recurring 1pm closes are the day after Thanksgiving, Christmas Eve, and the weekday
    around Independence Day. Getting this wrong in the SAFE direction (thinking a full
    day is a half day) only makes the capture window early; getting it wrong the other
    way misses the close entirely, which is why the provider is asked first.
    """
    if day.weekday() >= 5:
        return False
    # day after the fourth Thursday in November
    if day.month == 11 and day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1):
        return True
    # Christmas Eve, but only Mon-Thu. On a Friday the 24th, Christmas falls on the
    # Saturday and the 24th IS the observed holiday - a full closure, not a 13:00
    # session, so treating it as a half day opens a capture window on a dark market.
    if (day.month, day.day) == (12, 24) and day.weekday() <= 3:
        return True
    # July 3 when Independence Day is observed on the 4th; July 5 is a full day
    if (day.month, day.day) == (7, 3) and date(day.year, 7, 4).weekday() < 5:
        return True
    return False


def scheduled_close_minute(day: date | None = None,
                           payload: dict | None = None) -> tuple[int, str]:
    """Today's close in New York minutes-since-midnight, and where it came from.

    The U.S. tape closes at 13:00 on half days. A hardcoded 16:00 closing window would
    miss every one of those sessions - and a missed exit book cannot be recovered later,
    so the failure is permanent rather than merely inconvenient.

    Provider first ("provider"), then the recurring half-day rules ("calendar"), then
    16:00 ("default"). The source travels with the observation so a later reader can see
    which of the three decided that a quote counted as a close.
    """
    target = day or datetime.now(NY).date()
    reported = (payload or {}).get("close")
    when = ny_time(reported) if reported else None
    # The provider rolls to the NEXT session's close once today's has passed. Taking it
    # for the requested date turned a 13:00 half day into a 16:00 session, and opened
    # the capture window two and a half hours after the book stopped existing.
    if when is not None and when.date() == target:
        return when.hour * 60 + when.minute, "provider"
    if is_us_half_day(target):
        return 13 * 60, "calendar"
    return MARKET_CLOSE_MINUTE, "default"


def closing_window(close_minute: int) -> tuple[int, int]:
    """The +/- 5 minute band around a session's actual close."""
    return close_minute - 5, close_minute + 5


def ny_time(stamp) -> datetime | None:
    """An ISO timestamp in market time, or None when it cannot be parsed.

    Unparseable is never "assume now". A quote whose time is unknown cannot be placed in
    a session at all, and guessing is how a stale book got relabelled into today.
    """
    text = str(stamp or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    trimmed = _SUBSECOND.match(text)
    if trimmed:
        text = trimmed.group(1) + trimmed.group(2)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(NY)


def session_date(stamp) -> str | None:
    """The market-time calendar date a quote belongs to, from the EXCHANGE stamp."""
    when = ny_time(stamp)
    return when.strftime("%Y-%m-%d") if when else None


def session_minute(stamp) -> int | None:
    when = ny_time(stamp)
    return when.hour * 60 + when.minute if when else None


def in_regular_session(stamp) -> bool:
    minute = session_minute(stamp)
    return minute is not None and MARKET_OPEN_MINUTE <= minute <= MARKET_CLOSE_MINUTE


def in_closing_window(stamp, close_minute: int | None = None) -> bool:
    """Whether a quote may stand as the session's closing book.

    ``close_minute`` is the close that was in force for THAT session - passed in rather
    than assumed, so a 13:00 half-day quote is judged against 13:00 and not against a
    16:00 that never happened.
    """
    minute = session_minute(stamp)
    if minute is None:
        return False
    low, high = closing_window(MARKET_CLOSE_MINUTE if close_minute is None
                               else int(close_minute))
    return low <= minute <= high


def age_seconds(stamp, received) -> float | None:
    """Seconds between an exchange timestamp and when we received it.

    Negative means the exchange stamp is in the FUTURE relative to our receipt, which is
    a clock or feed problem, not a fresh quote.
    """
    when, got = ny_time(stamp), ny_time(received)
    if when is None or got is None:
        return None
    return (got - when).total_seconds()


def is_execution_feed(feed) -> bool:
    """Whether a feed is a real venue book. Yahoo's blended quote is not."""
    return str(feed or "").strip().lower() in EXECUTION_FEEDS


def is_known_feed(feed) -> bool:
    return str(feed or "").strip().lower() in FEED_DESCRIPTIONS


def configured() -> bool:
    key = str(getattr(config, "ALPACA_API_KEY", "") or "")
    secret = str(getattr(config, "ALPACA_SECRET_KEY", "") or "")
    return bool(key and secret and "PASTE" not in key and "PASTE" not in secret
                and aiohttp is not None)


def _headers() -> dict:
    return {"APCA-API-KEY-ID": config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY}


def _observation(symbol: str, quote: dict, feed: str) -> dict | None:
    """One archived observation, or None when the payload is not a two-sided book.

    A one-sided or crossed book is not a quote. Recording it as one would put the same
    class of artifact into the evidence that the entry path already refuses.
    """
    try:
        bid = float(quote.get("bp") or 0)
        ask = float(quote.get("ap") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    stamp = str(quote.get("t") or "").strip()
    if not stamp:
        return None
    mid = (bid + ask) / 2
    return {
        "ticker": symbol,
        "bid": round(bid, 6),
        "ask": round(ask, 6),
        "mid": round(mid, 6),
        "half_spread_pct": round((ask - bid) / 2 / mid * 100, 6) if mid > 0 else None,
        "spread_pct": round((ask - bid) / mid * 100, 6) if mid > 0 else None,
        "at": stamp,
        "source": "alpaca",
        "feed": feed,
        "feed_description": feed_description(feed),
        "is_consolidated": is_consolidated(feed),
        "bid_size": quote.get("bs"),
        "ask_size": quote.get("as"),
    }


async def latest_quotes(symbols) -> tuple[dict, str]:
    """Latest bid/ask per symbol from Alpaca, plus an error string ("" when clean).

    Returns ({symbol: observation}, error). Symbols with no usable two-sided book are
    simply absent - a missing observation is honest, an invented one is not.
    """
    wanted = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    wanted = list(dict.fromkeys(wanted))
    if not wanted:
        return {}, ""
    if not configured():
        return {}, ("alpaca quotes unavailable: no ALPACA_API_KEY / ALPACA_SECRET_KEY"
                    if aiohttp is not None else
                    "alpaca quotes unavailable: aiohttp is not installed")

    feed = feed_name() or "iex"
    url = str(config.ALPACA_DATA_URL).rstrip("/") + "/v2/stocks/quotes/latest"
    out: dict[str, dict] = {}
    errors: list[str] = []
    timeout = aiohttp.ClientTimeout(total=QUOTE_TIMEOUT_SEC)
    async with aiohttp.ClientSession(headers=_headers(), timeout=timeout) as session:
        for start in range(0, len(wanted), MAX_SYMBOLS_PER_REQUEST):
            batch = wanted[start:start + MAX_SYMBOLS_PER_REQUEST]
            params = {"symbols": ",".join(batch), "feed": feed}
            try:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        body = (await response.text())[:160]
                        errors.append(f"HTTP {response.status}: {body}")
                        continue
                    payload = await response.json()
            except Exception as e:
                errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                continue
            for symbol, quote in (payload.get("quotes") or {}).items():
                seen = _observation(str(symbol).upper(), quote or {}, feed)
                if seen:
                    out[seen["ticker"]] = seen
            await asyncio.sleep(0.2)
    return out, "; ".join(errors)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. No numpy dependency for four numbers."""
    clean = sorted(float(x) for x in values if x is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = max(1, min(len(clean), int(round(pct / 100.0 * len(clean) + 0.5))))
    return clean[rank - 1]


def median(values: list[float]) -> float | None:
    clean = sorted(float(x) for x in values if x is not None)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2


def _price_bucket(price) -> str:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0.30:  return "<$0.30"
    if value < 1.00:  return "$0.30-1"
    if value < 3.00:  return "$1-3"
    if value < 10.0:  return "$3-10"
    return ">=$10"


def _dollar_volume_bucket(dollar_volume) -> str:
    try:
        value = float(dollar_volume)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 50_000_000: return ">=$50M"
    if value >= 10_000_000: return "$10-50M"
    if value >= 2_000_000:  return "$2-10M"
    if value >= 500_000:    return "$0.5-2M"
    return "<$0.5M"


def _session_bucket(stamp) -> str:
    """First hour / midday / last hour, in market time, from an ISO timestamp."""
    when = ny_time(stamp)
    if when is None:
        return "unknown"
    minutes = when.hour * 60 + when.minute
    if minutes < 9 * 60 + 30:  return "pre-open"
    if minutes < 10 * 60 + 30: return "open-hour"
    if minutes < 15 * 60:      return "midday"
    if minutes <= 16 * 60:     return "close-hour"
    return "post-close"


def _score(records: list[dict]) -> dict:
    """Bias of the proxy against observed spreads, in percentage POINTS.

    bias = proxy - observed. Negative bias is the dangerous direction: the proxy
    promised a cheaper round trip than the book showed, which is precisely how a name
    gets admitted at a cost nobody could have traded.
    """
    biases = [float(r["proxy_pct"]) - float(r["observed_pct"]) for r in records]
    under = [-b for b in biases if b < 0]        # magnitude of the understatement
    return {
        "observations": len(records),
        "median_bias_pts": (round(median(biases), 4) if biases else None),
        "median_observed_pct": round(median([r["observed_pct"] for r in records]), 4),
        "median_proxy_pct": round(median([r["proxy_pct"] for r in records]), 4),
        "understated_share": (round(len(under) / len(biases), 4) if biases else None),
        # how bad the understatement gets in the tail, which is what actually kills you
        "p90_understatement_pts": (round(percentile(under, 90), 4) if under else 0.0),
        "p95_understatement_pts": (round(percentile(under, 95), 4) if under else 0.0),
    }


def adv_proxy_audit(observations: list[dict], min_per_bucket: int = 20) -> dict:
    """Score the ADV spread proxy against forward-held observed quotes.

    Each observation needs ``proxy_pct`` (what the heuristic predicted), ``observed_pct``
    (the quoted round trip actually seen) and, for the breakdowns, ``price``,
    ``dollar_volume`` and ``at``. Buckets under ``min_per_bucket`` are reported but
    flagged: a median over four quotes is not a calibration.

    This returns the measurement, never a blessing. Until it runs on real forward-held
    observations the proxy stays uncalibrated, and every outcome that leans on it is
    stamped cost_evidentiary: false.
    """
    usable = []
    for row in (observations or []):
        try:
            proxy = float(row.get("proxy_pct"))
            observed = float(row.get("observed_pct"))
        except (TypeError, ValueError):
            continue
        if observed <= 0:
            continue
        usable.append({**row, "proxy_pct": proxy, "observed_pct": observed})

    if not usable:
        return {"observations": 0, "calibrated": False,
                "reason": "no forward-held quote observations yet; the ADV proxy "
                          "remains an assumption and cannot be scored",
                "overall": None, "by_price": {}, "by_dollar_volume": {},
                "by_session_time": {}, "underpowered_buckets": []}

    def group(key):
        out: dict[str, list] = {}
        for row in usable:
            out.setdefault(key(row), []).append(row)
        return {name: _score(rows) for name, rows in sorted(out.items())}

    by_price = group(lambda r: _price_bucket(r.get("price")))
    by_volume = group(lambda r: _dollar_volume_bucket(r.get("dollar_volume")))
    by_time = group(lambda r: _session_bucket(r.get("at")))
    # Split by feed as well: an IEX book and a SIP book are different observations of
    # different things, and a pooled median across them measures neither.
    by_feed = group(lambda r: str(r.get("feed") or "unknown").lower())
    thin = sorted(
        f"{label}:{name}"
        for label, table in (("price", by_price), ("dollar_volume", by_volume),
                             ("session_time", by_time), ("feed", by_feed))
        for name, score in table.items() if score["observations"] < min_per_bucket
    )
    return {
        "observations": len(usable),
        # One audit does not calibrate anything on its own; a human still has to accept
        # it. Reported so no caller can mistake "measured" for "validated".
        "calibrated": False,
        "reason": "measured against forward-held observations; acceptance is a "
                  "separate, human decision",
        "overall": _score(usable),
        "by_price": by_price,
        "by_dollar_volume": by_volume,
        "by_session_time": by_time,
        "by_feed": by_feed,
        "underpowered_buckets": thin,
        "min_per_bucket": min_per_bucket,
    }
