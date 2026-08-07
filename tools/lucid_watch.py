"""
lucid_watch.py — robust live watcher for the Lucid bridge + continuous bot.

Prints ONE line per meaningful change (for the Monitor tool). Distinguishes a
genuine JForex disconnect from the normal end-of-day index feed pause:

  * Oil (LIGHTCMDUSD) streams the whole session 13:00-20:59 UTC, so oil freshness
    is the reliable "JForex alive" signal. ES/NQ index proxies stop ~20:14 UTC in
    summer — that is NOT a disconnect and must not alarm.
  * A real disconnect = oil silent > 180s while 13:05-20:55 UTC. Debounced: needs
    2 consecutive bad polls before alarming (ignores single transient timeouts).

Trade events (open/close, trade count) are always reported.
Does its own HTTP (urllib) so there is no shell-quoting fragility.
"""
from __future__ import annotations
import datetime
import json
import time
import urllib.request

RECV = "http://127.0.0.1:8765/health"
BOT = "http://127.0.0.1:8000/api/lucidcont/state"
PASS = "http://127.0.0.1:8000/api/lucidpass/state"
POLL_SEC = 90
OIL_STALE_SEC = 180
SESSION_START_MIN = 13 * 60 + 5     # 13:05 UTC
OIL_LAST_MIN = 20 * 60 + 55         # 20:55 UTC (oil should be live until ~20:59)


def _get(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "lucid-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def poll():
    """Return (event_key, event_text) or (None, None) if nothing notable."""
    now = _now()
    umin = now.hour * 60 + now.minute

    # --- oil freshness = JForex-alive signal ---
    # Only a real problem on a WEEKDAY during active oil-session hours. Markets are
    # closed Sat/Sun (and the index feed pauses ~20:14 UTC), so silence then is normal.
    is_trading_window = now.weekday() < 5 and SESSION_START_MIN <= umin < OIL_LAST_MIN
    oil_problem = None
    try:
        h = _get(RECV, timeout=12)   # receiver can be briefly busy under heavy tick load
        cl = h["markets"]["cl"]["latest_dt_utc"]
        age = (now - datetime.datetime.fromisoformat(cl.replace(" ", "T"))).total_seconds()
        if is_trading_window and age > OIL_STALE_SEC:
            oil_problem = f"oil feed silent {age:.0f}s during session"
    except Exception as e:
        if is_trading_window:
            oil_problem = f"receiver :8765 unreachable ({type(e).__name__})"

    # --- milestone state (NOT per-trade): pass-bot passed, floor breach ---
    milestone = None
    try:
        p = _get(PASS)
        if p.get("passed"):
            milestone = f"BUY TRIGGER: paper Pass bot reached +$3,000 (balance ${p.get('balance')})"
        elif p.get("failed"):
            milestone = f"ALERT: Pass bot breached the $48,000 floor (balance ${p.get('balance')})"
    except Exception:
        pass
    return oil_problem, milestone


def main():
    # Quiet monitor: emits ONLY on a real disconnect (debounced) or a milestone
    # (Pass bot hits +$3k / breaches floor). Per-trade opens/closes are intentionally
    # silent — day-level review is done on request, not per trade.
    last_key = None
    bad_streak = 0
    while True:
        oil_problem, milestone = poll()
        if oil_problem:
            bad_streak += 1
        else:
            bad_streak = 0
        parts = []
        disc = oil_problem and bad_streak >= 3   # ~4.5 min of real silence before alarming
        if disc:
            parts.append("REAL PROBLEM: " + oil_problem)
        if milestone:
            parts.append(milestone)
        text = " || ".join(parts)
        key = ("DISC" if disc else "ok") + "|" + (milestone or "")
        if text and key != last_key:
            print(text, flush=True)
            last_key = key
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
