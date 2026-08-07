"""Resumable Dukascopy NQ-proxy download for 03:00-08:30 New York."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from fetch_lucid_10y import _ticks, _to_1m


HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
START = pd.Timestamp("2016-06-27")
END = pd.Timestamp("2026-07-31")
HOURS = range(7, 14)  # Covers 03:00-08:30 ET across EST and EDT.
INSTRUMENT = "USATECHIDXUSD"


def main() -> int:
    os.makedirs(CACHE, exist_ok=True)
    part = os.path.join(CACHE, "nq_1m_london_10y.part.csv")
    final = os.path.join(CACHE, "nq_1m_london_10y.csv")
    completed: set[object] = set()
    if os.path.exists(part):
        prior = pd.read_csv(part, usecols=["dt_utc"])
        completed = set(pd.to_datetime(prior["dt_utc"], utc=True).dt.date)
    days = [
        day for day in pd.date_range(START, END, freq="D")
        if day.weekday() < 5 and day.date() not in completed
    ]
    print(
        f"NQ London: {len(days)} weekdays x {len(tuple(HOURS))} UTC hours",
        flush=True,
    )
    progress = [0]
    lock = threading.Lock()
    started = time.time()

    def work(job: tuple[str, int, int, int, int]):
        rows = _ticks(*job)
        with lock:
            progress[0] += 1
            if progress[0] % 500 == 0:
                rate = progress[0] / max(time.time() - started, 1.0)
                remaining = len(days) * len(tuple(HOURS)) - progress[0]
                print(
                    f"  files={progress[0]} rate={rate:.1f}/s "
                    f"eta={remaining / max(rate, 0.1) / 60:.1f}m",
                    flush=True,
                )
        return rows

    header = not os.path.exists(part)
    chunk_size = 20
    with ThreadPoolExecutor(max_workers=12) as pool:
        for start in range(0, len(days), chunk_size):
            chunk = days[start:start + chunk_size]
            jobs = [
                (
                    INSTRUMENT,
                    day.year,
                    day.month - 1,
                    day.day,
                    hour,
                )
                for day in chunk for hour in HOURS
            ]
            rows = []
            for result in pool.map(work, jobs):
                rows.extend(result)
            if rows:
                bars = _to_1m(rows)
                bars.to_csv(
                    part, mode="a", header=header, index=False
                )
                header = False

    frame = pd.read_csv(part)
    frame["dt_utc"] = pd.to_datetime(frame["dt_utc"], utc=True)
    frame = (
        frame.drop_duplicates("dt_utc", keep="last")
        .sort_values("dt_utc")
        .reset_index(drop=True)
    )
    frame.to_csv(final, index=False)
    print(
        f"done rows={len(frame):,} {frame['dt_utc'].iloc[0]} "
        f"-> {frame['dt_utc'].iloc[-1]}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
