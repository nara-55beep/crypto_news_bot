from __future__ import annotations

from dataclasses import replace

import lucid_causal_rebuild as L
import lucid_noise_area_search as N
import lucid_opening_horizon_search as H


def test_opening_horizon_enters_after_completed_observation() -> None:
    days = L.load_days("nq")[:80]
    cfg = H.HConfig("nq", 1, 240, "opening", False, 0.50)
    trades = H.generate(days, cfg)
    assert trades
    by_date = {day.day: day for day in days}
    for trade in trades:
        day = by_date[trade.day]
        entry_i = next(
            i for i, timestamp in enumerate(day.ts)
            if timestamp == trade.entry_ts
        )
        assert int(day.minute[entry_i]) == 1
        assert trade.entry_ts <= trade.exit_ts
        assert (
            trade.stop < trade.entry
            if trade.side > 0 else trade.stop > trade.entry
        )


def test_scheduled_exit_occurs_before_exit_minutes_high_low() -> None:
    days = L.load_days("nq")[:120]
    cfg = H.HConfig("nq", 1, 60, "opening", False, 0.50)
    trades = H.generate(days, cfg)
    assert trades
    by_date = {day.day: day for day in days}
    for trade in trades:
        if trade.reason != "time_open":
            continue
        day = by_date[trade.day]
        exit_i = next(
            i for i, timestamp in enumerate(day.ts)
            if timestamp == trade.exit_ts
        )
        assert int(day.minute[exit_i]) == 60
        expected = float(day.op[exit_i]) - trade.side * L.MARKETS["nq"]["tick"]
        assert trade.exit == expected


def test_noise_sigma_never_reads_current_session() -> None:
    days = L.load_days("nq")[:90]
    day_index = 40
    original = N._sigma(days, day_index, 29, 14)
    assert original is not None
    changed = list(days)
    current = changed[day_index]
    changed[day_index] = replace(
        current,
        cl=current.cl * 100.0,
    )
    # A new list identity bypasses the cache and proves the formula itself
    # excludes day_index rather than merely returning a cached answer.
    mutated = N._sigma(changed, day_index, 29, 14)
    assert mutated == original


def test_noise_entries_follow_fixed_checkpoint_close() -> None:
    days = L.load_days("nq")[:120]
    cfg = N.NConfig("nq", 14, 0.75, 30, True, "boundary")
    trades = N.generate(days, cfg)
    assert trades
    by_date = {day.day: day for day in days}
    for trade in trades:
        day = by_date[trade.day]
        entry_i = next(
            i for i, timestamp in enumerate(day.ts)
            if timestamp == trade.entry_ts
        )
        # A 09:59 close (minute 29), for example, can only enter the
        # minute-30 open. Later rebalances follow the same rule.
        assert int(day.minute[entry_i]) % 30 == 0
        assert int(day.minute[entry_i]) >= 30
        assert (
            trade.stop < trade.entry
            if trade.side > 0 else trade.stop > trade.entry
        )
