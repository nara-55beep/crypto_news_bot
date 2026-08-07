"""Targeted Dukascopy repair for incomplete NQ-proxy RTH dates.

The original bulk downloader occasionally lost complete UTC-hour files.  This
script refetches only dates whose cached 09:30--15:59 New York sequence is not
clock-contiguous.  Recovered bars are written to a separate overlay; the
original ten-year and seed files are never overwritten.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

import pandas as pd

import lucid_causal_rebuild as L
from fetch_lucid_10y import _ticks, _to_1m


INSTRUMENT = "USATECHIDXUSD"
HOURS = tuple(range(13, 21))
OVERLAY = os.path.join(L.CACHE, "nq_1m_rth_repair.csv")
PART = os.path.join(L.CACHE, "nq_1m_rth_repair.part.csv")


def _incomplete_dates() -> list[object]:
    frame = L._load_rth_minutes("nq")
    out = []
    for session, group in frame.groupby("day", sort=True):
        if len(group) < 60:
            continue
        local = group["dt_utc"].dt.tz_convert(L.NY)
        minute = (
            local.dt.hour * 60 + local.dt.minute - (9 * 60 + 30)
        ).to_numpy("int16")
        if not L._is_complete_rth_minutes(minute):
            out.append(session)
    return out


def main() -> int:
    affected = _incomplete_dates()
    print(f"affected_dates_before={len(affected)}", flush=True)
    if not affected:
        return 0

    # Refetch every affected date on every run.  Dedupe makes this idempotent,
    # and it lets a transiently failed hourly response heal on a later run.
    progress = [0]
    lock = threading.Lock()
    started = time.time()

    def work(job):
        rows = _ticks(*job)
        with lock:
            progress[0] += 1
            if progress[0] % 200 == 0:
                rate = progress[0] / max(time.time() - started, 1.0)
                remaining = len(affected) * len(HOURS) - progress[0]
                print(
                    f"files={progress[0]} rate={rate:.1f}/s "
                    f"eta={remaining / max(rate, 0.1) / 60:.1f}m",
                    flush=True,
                )
        return rows

    header = True
    with ThreadPoolExecutor(max_workers=8) as pool:
        for start in range(0, len(affected), 5):
            chunk = affected[start:start + 5]
            jobs = [
                (INSTRUMENT, day.year, day.month - 1, day.day, hour)
                for day in chunk
                for hour in HOURS
            ]
            rows = []
            for result in pool.map(work, jobs):
                rows.extend(result)
            if not rows:
                continue
            bars = _to_1m(rows)
            bars.to_csv(
                PART,
                mode="w" if header else "a",
                header=header,
                index=False,
            )
            header = False

    frames = []
    if os.path.exists(OVERLAY):
        frames.append(pd.read_csv(OVERLAY))
    if os.path.exists(PART):
        frames.append(pd.read_csv(PART))
    if not frames:
        raise RuntimeError("No repair bars downloaded")
    repaired = pd.concat(frames, ignore_index=True)
    repaired["dt_utc"] = pd.to_datetime(repaired["dt_utc"], utc=True)
    repaired = (
        repaired.drop_duplicates("dt_utc", keep="last")
        .sort_values("dt_utc")
        .reset_index(drop=True)
    )
    repaired.to_csv(OVERLAY, index=False)
    print(
        f"overlay_rows={len(repaired):,} "
        f"{repaired['dt_utc'].iloc[0]} -> {repaired['dt_utc'].iloc[-1]}",
        flush=True,
    )

    # Re-evaluate after the overlay is visible to the shared loader.
    remaining = _incomplete_dates()
    print(
        f"affected_dates_after={len(remaining)} "
        f"recovered={len(affected) - len(remaining)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
