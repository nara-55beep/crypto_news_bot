"""
Verify the live Lucid paper bot logic against the historical backtest helpers.

This is intentionally offline: it reads research/ta_strat/cache/*_1m_3y.csv and
does not touch the running website or any broker/API. It guards the exact
5-strategy basket that produced the saved 36/36 Lucid-style report.
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "ta_strat"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RESEARCH))

import lucid_pass_paper as live
from apex_lib import load_fut
from apex_strats2 import nr7_orb, turtle_soup, vwap_fade
from bt_ict_sm_tf import resample


EXPECTED = {
    "ES_VWAP3": {"trades": 714, "pnl_R": 358.97},
    "NQ_VWAP3": {"trades": 675, "pnl_R": 385.79},
    "CL_VWAP5": {"trades": 603, "pnl_R": 259.74},
    "NQ_TURTLE30": {"trades": 286, "pnl_R": 176.13},
    "CL_NR7_30": {"trades": 113, "pnl_R": 53.27},
}


COMPONENT_SPEC = {
    "ES_VWAP3": ("es", "3min"),
    "NQ_VWAP3": ("nq", "3min"),
    "CL_VWAP5": ("cl", "5min"),
    "NQ_TURTLE30": ("nq", "30min"),
    "CL_NR7_30": ("cl", "30min"),
}


class BareLiveBot(live.LucidPassPaperBot):
    def __init__(self):
        self.enabled = False
        self.failed = False
        self.pos = {}
        self.setups = {}
        self.fired_keys = set()
        self.warning_keys = set()
        self.day_key = "verify"
        self.balance = live.START_BALANCE
        self.floor = live.FLOOR_LOCK
        self.day_pnl = 0.0
        self.passed = False
        self.daily_stopped_day = ""

    def _warn_signal(self, *args, **kwargs):
        return None


def _raw_to_live_frame(raw: pd.DataFrame, rule: str) -> pd.DataFrame:
    df = raw.reset_index().rename(columns={"dt": "dt_utc"})
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(live.NY)
    df = df[["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"]]
    sec = {"3min": 180, "5min": 300, "30min": 1800}[rule]
    return live._prepare(live._resample(df, rule), sec, drop_incomplete=False)


def _daily(df: pd.DataFrame) -> pd.DataFrame:
    return live._daily(df)


def _same(a: float, b: float, tol: float = 1e-7) -> bool:
    return abs(float(a) - float(b)) <= tol


def expected_vwap(today: pd.DataFrame) -> dict | None:
    h = today["high"].astype(float).to_numpy()
    lo = today["low"].astype(float).to_numpy()
    c = today["close"].astype(float).to_numpy()
    v = today["volume"].astype(float).to_numpy()
    tp = (h + lo + c) / 3.0
    cum_v = cum_pv = cum_p2v = 0.0
    for i in range(len(today)):
        vv = v[i] if v[i] > 0 else 1.0
        cum_v += vv
        cum_pv += tp[i] * vv
        cum_p2v += tp[i] * tp[i] * vv
        if cum_v <= 0 or i < live.VWAP_MIN_BARS:
            continue
        vwap = cum_pv / cum_v
        sig = max(cum_p2v / cum_v - vwap * vwap, 0.0) ** 0.5
        if sig <= 0:
            continue
        up = vwap + live.VWAP_K * sig
        dn = vwap - live.VWAP_K * sig
        if h[i] >= up:
            return {
                "side": "short",
                "entry": up,
                "stop": vwap + (live.VWAP_K + 1.0) * sig,
                "target": vwap,
                "idx": i,
            }
        if lo[i] <= dn:
            return {
                "side": "long",
                "entry": dn,
                "stop": vwap - (live.VWAP_K + 1.0) * sig,
                "target": vwap,
                "idx": i,
            }
    return None


def expected_turtle(today: pd.DataFrame, before: pd.DataFrame) -> dict | None:
    db = _daily(before)
    if len(db) < live.TURTLE_LOOKBACK + live.TURTLE_RECENCY:
        return None
    tick = live.MARKETS["NQ=F"][1]
    look = db.tail(live.TURTLE_LOOKBACK).reset_index(drop=True)
    lows = look["low"].astype(float).to_numpy()
    highs = look["high"].astype(float).to_numpy()
    prior_low = float(lows.min())
    prior_high = float(highs.max())
    session_index = len(db)
    lo_age = session_index - 1 - int(np.argmin(lows))
    hi_age = session_index - 1 - int(np.argmax(highs))
    t = today.reset_index(drop=True)
    for i, row in t.iterrows():
        direction = 0
        if float(row["low"]) < prior_low and lo_age >= live.TURTLE_RECENCY:
            direction = 1
            entry = prior_low + live.TURTLE_BUF_TICKS * tick
            stop = float(t.iloc[: i + 1]["low"].min()) - tick
        elif float(row["high"]) > prior_high and hi_age >= live.TURTLE_RECENCY:
            direction = -1
            entry = prior_high - live.TURTLE_BUF_TICKS * tick
            stop = float(t.iloc[: i + 1]["high"].max()) + tick
        else:
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + direction * 2.0 * risk
        for j in range(i, len(t)):
            bar = t.iloc[j]
            if (
                (direction > 0 and float(bar["high"]) >= entry)
                or (direction < 0 and float(bar["low"]) <= entry)
            ):
                return {
                    "side": "long" if direction > 0 else "short",
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "idx": j,
                }
        return None
    return None


def expected_nr7(today: pd.DataFrame, before: pd.DataFrame) -> dict | None:
    db = _daily(before)
    if len(db) < 7:
        return None
    tick = live.MARKETS["CL=F"][1]
    last = db.iloc[-1]
    prior6 = db.iloc[-7:-1]
    if float(last["range"]) >= float(prior6["range"].min()):
        return None
    hi = float(last["high"])
    lo = float(last["low"])
    t = today.reset_index(drop=True)
    for i, row in t.iterrows():
        if float(row["high"]) >= hi + tick:
            entry = hi + tick
            stop = lo - tick
            return {
                "side": "long",
                "entry": entry,
                "stop": stop,
                "target": entry + 2.0 * abs(entry - stop),
                "idx": i,
            }
        if float(row["low"]) <= lo - tick:
            entry = lo - tick
            stop = hi + tick
            return {
                "side": "short",
                "entry": entry,
                "stop": stop,
                "target": entry - 2.0 * abs(entry - stop),
                "idx": i,
            }
    return None


def _expected_signal(key: str, today: pd.DataFrame, before: pd.DataFrame) -> dict | None:
    if key in {"ES_VWAP3", "NQ_VWAP3", "CL_VWAP5"}:
        return expected_vwap(today)
    if key == "NQ_TURTLE30":
        return expected_turtle(today, before)
    if key == "CL_NR7_30":
        return expected_nr7(today, before)
    raise KeyError(key)


def verify_component_stats(raw: dict[str, pd.DataFrame]) -> None:
    helper_recs = {
        "ES_VWAP3": vwap_fade(resample(raw["es"], "3min"), "es", k_band=2.5, manage="partial")[0],
        "NQ_VWAP3": vwap_fade(resample(raw["nq"], "3min"), "nq", k_band=2.5, manage="partial")[0],
        "CL_VWAP5": vwap_fade(resample(raw["cl"], "5min"), "cl", k_band=2.5, manage="partial")[0],
        "NQ_TURTLE30": turtle_soup(
            resample(raw["nq"], "30min"),
            "nq",
            lookback=10,
            recency=4,
            buf_ticks=8,
            manage="partial",
        )[0],
        "CL_NR7_30": nr7_orb(resample(raw["cl"], "30min"), "cl", manage="partial")[0],
    }
    for key, recs in helper_recs.items():
        got_n = len(recs)
        got_pnl = round(float(sum(r["pnl_R"] for r in recs)), 2)
        exp = EXPECTED[key]
        assert got_n == exp["trades"], f"{key} helper trades {got_n} != {exp['trades']}"
        assert abs(got_pnl - exp["pnl_R"]) <= 0.01, f"{key} helper pnl_R {got_pnl} != {exp['pnl_R']}"
        print(f"helper {key:<12} trades={got_n:4d} pnl_R={got_pnl:8.2f}")


def verify_frames_and_signals(raw: dict[str, pd.DataFrame]) -> None:
    bot = BareLiveBot()
    for key, (market, rule) in COMPONENT_SPEC.items():
        helper_frame = resample(raw[market], rule)
        live_frame = _raw_to_live_frame(raw[market], rule)
        live_indexed = live_frame.set_index("dt_utc")[["open", "high", "low", "close", "volume"]]
        assert len(helper_frame) == len(live_indexed), f"{key} frame row count mismatch"
        assert str(helper_frame.index[0]) == str(live_indexed.index[0]), f"{key} first row mismatch"
        assert str(helper_frame.index[-1]) == str(live_indexed.index[-1]), f"{key} last row mismatch"

        mismatches = 0
        checked = 0
        for day in sorted(live_frame["day"].unique()):
            today = live_frame[live_frame["day"] == day].reset_index(drop=True)
            before = live_frame[live_frame["day"] < day]
            if today.empty:
                continue
            expected = _expected_signal(key, today, before)
            actual = bot._signal(key, today, _daily(before))
            checked += 1
            ok = expected is None and actual is None
            if expected is not None and actual is not None:
                ok = (
                    expected["side"] == actual["side"]
                    and _same(expected["entry"], actual["entry"])
                    and _same(expected["stop"], actual["stop"])
                    and _same(expected["target"], actual["target"])
                    and bool(actual.get("spent")) == (expected["idx"] < len(today) - 1)
                )
            if not ok:
                mismatches += 1
                if mismatches <= 3:
                    print(f"mismatch {key} {day}: expected={expected} actual={actual}")
        assert mismatches == 0, f"{key} signal mismatches={mismatches}"
        print(f"live   {key:<12} frame_rows={len(live_indexed):6d} signal_days={checked:4d} mismatches=0")


def main() -> int:
    raw = {market: load_fut(market) for market in ("es", "nq", "cl")}
    verify_component_stats(raw)
    verify_frames_and_signals(raw)
    print("Lucid parity verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
