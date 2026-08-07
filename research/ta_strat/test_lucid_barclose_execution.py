from dataclasses import replace

import pandas as pd

import lucid_barclose_execution as B
import lucid_causal_rebuild as L
from test_lucid_causal_rebuild import make_day


def seed_trade(day: L.Day) -> L.Trade:
    return L.Trade(
        market="nq",
        strategy="seed",
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[1]),
        exit_ts=pd.Timestamp(day.ts[-1]),
        side=1,
        entry=100.25,
        stop=99.0,
        target=102.0,
        exit=100.0,
        reason="seed",
        risk_per_micro=3.0,
        gross_per_micro=0.0,
    )


def test_intrabar_low_does_not_trigger_close_managed_stop():
    day = make_day(
        op=[100, 100, 101, 101],
        hi=[100, 101, 102, 102],
        lo=[100, 98, 100, 100],
        cl=[100, 100.5, 101, 101],
    )
    converted = B.convert(day, seed_trade(day))
    assert converted is not None
    assert converted.reason == "eod"
    assert converted.exit_ts == pd.Timestamp(day.ts[3])


def test_single_protective_stop_triggers_intrabar_without_target_ordering():
    day = make_day(
        op=[100, 100, 101, 101],
        hi=[100, 103, 102, 102],
        lo=[100, 98, 100, 100],
        cl=[100, 102.5, 101, 101],
    )
    converted = B.convert(day, seed_trade(day), protective_stop=True)
    assert converted is not None
    assert converted.reason == "stop_protective"
    assert converted.exit_ts == pd.Timestamp(day.ts[1])
    assert converted.exit == 98.75


def test_completed_stop_close_exits_at_next_open_with_slippage():
    day = make_day(
        op=[100, 100, 97, 97],
        hi=[100, 101, 98, 98],
        lo=[100, 98, 96, 96],
        cl=[100, 98.5, 97, 97],
    )
    converted = B.convert(day, seed_trade(day))
    assert converted is not None
    assert converted.reason == "stop_close"
    assert converted.exit_ts == pd.Timestamp(day.ts[2])
    assert converted.exit == 96.75


def test_completed_target_close_exits_at_next_open_not_target_level():
    day = make_day(
        op=[100, 100, 103, 103],
        hi=[100, 103, 104, 104],
        lo=[100, 99.5, 102, 102],
        cl=[100, 102.5, 103, 103],
    )
    converted = B.convert(day, seed_trade(day))
    assert converted is not None
    assert converted.reason == "target_close"
    assert converted.exit_ts == pd.Timestamp(day.ts[2])
    assert converted.exit == 102.75


def test_short_uses_symmetric_close_rules():
    day = make_day(
        op=[100, 100, 103, 103],
        hi=[100, 102, 104, 104],
        lo=[100, 99, 102, 102],
        cl=[100, 101.5, 103, 103],
    )
    trade = replace(
        seed_trade(day),
        side=-1,
        entry=99.75,
        stop=101.0,
        target=98.0,
    )
    converted = B.convert(day, trade)
    assert converted is not None
    assert converted.reason == "stop_close"
    assert converted.exit_ts == pd.Timestamp(day.ts[2])
    assert converted.exit == 103.25
