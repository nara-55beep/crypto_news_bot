"""
Replay the live Lucid paper bot over cached historical candles.

verify_lucid_parity.py checks first-signal parity against the research helpers.
This file goes further: it drives the live bot's real open/manage/partial/close
code through the same three-year ES/NQ/CL component candles and reports whether
the account-level behavior still matches the saved Lucid reports closely.
"""
from __future__ import annotations

import pathlib
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "ta_strat"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RESEARCH))

import lucid_pass_paper as live
from apex_lib import load_fut
from bt_ict_sm_tf import resample


COMPONENT_SPEC = {
    "ES_VWAP3": ("es", "3min"),
    "NQ_VWAP3": ("nq", "3min"),
    "CL_VWAP5": ("cl", "5min"),
    "NQ_TURTLE30": ("nq", "30min"),
    "CL_NR7_30": ("cl", "30min"),
}


def first_vwap_idx(today: pd.DataFrame) -> int | None:
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
        if h[i] >= vwap + live.VWAP_K * sig or lo[i] <= vwap - live.VWAP_K * sig:
            return i
    return None


def first_turtle_idx(today: pd.DataFrame, daily_before: pd.DataFrame) -> int | None:
    if len(daily_before) < live.TURTLE_LOOKBACK + live.TURTLE_RECENCY:
        return None
    tick = live.MARKETS["NQ=F"][1]
    look = daily_before.tail(live.TURTLE_LOOKBACK).reset_index(drop=True)
    lows = look["low"].astype(float).to_numpy()
    highs = look["high"].astype(float).to_numpy()
    prior_low = float(lows.min())
    prior_high = float(highs.max())
    session_index = len(daily_before)
    lo_age = session_index - 1 - int(np.argmin(lows))
    hi_age = session_index - 1 - int(np.argmax(highs))
    t = today.reset_index(drop=True)
    for i, row in t.iterrows():
        direction = 0
        if float(row["low"]) < prior_low and lo_age >= live.TURTLE_RECENCY:
            direction = 1
            entry = prior_low + live.TURTLE_BUF_TICKS * tick
        elif float(row["high"]) > prior_high and hi_age >= live.TURTLE_RECENCY:
            direction = -1
            entry = prior_high - live.TURTLE_BUF_TICKS * tick
        else:
            continue
        for j in range(i, len(t)):
            bar = t.iloc[j]
            if (
                (direction > 0 and float(bar["high"]) >= entry)
                or (direction < 0 and float(bar["low"]) <= entry)
            ):
                return j
        return None
    return None


def first_nr7_idx(today: pd.DataFrame, daily_before: pd.DataFrame) -> int | None:
    if len(daily_before) < 7:
        return None
    tick = live.MARKETS["CL=F"][1]
    last = daily_before.iloc[-1]
    prior6 = daily_before.iloc[-7:-1]
    if float(last["range"]) >= float(prior6["range"].min()):
        return None
    hi = float(last["high"])
    lo = float(last["low"])
    t = today.reset_index(drop=True)
    for i, row in t.iterrows():
        if float(row["high"]) >= hi + tick or float(row["low"]) <= lo - tick:
            return i
    return None


def first_signal_idx(key: str, today: pd.DataFrame, daily_before: pd.DataFrame) -> int | None:
    if key in {"ES_VWAP3", "NQ_VWAP3", "CL_VWAP5"}:
        return first_vwap_idx(today)
    if key == "NQ_TURTLE30":
        return first_turtle_idx(today, daily_before)
    if key == "CL_NR7_30":
        return first_nr7_idx(today, daily_before)
    raise KeyError(key)


class ReplayBot(live.LucidPassPaperBot):
    def __init__(self, stop_after_target: bool = True):
        self.stop_after_target = stop_after_target
        self.enabled = True
        self.balance = live.START_BALANCE
        self.peak = live.START_BALANCE
        self.locked = False
        self.floor = live.FLOOR_LOCK
        self.day_key = ""
        self.day_pnl = 0.0
        self.passed = False
        self.failed = False
        self.daily_stopped_day = ""
        self.pos = {}
        self.fired_keys = set()
        self.warning_keys = set()
        self.history = []
        self.log = []
        self.prices = {}
        self.setups = {}
        self.status = ""
        self.data_error = ""
        self.telegram_enabled = False
        self._telegram_client = None
        self._telegram_target = "me"
        self._telegram_bot_token = ""
        self._telegram_chat_id = ""
        self._last_alert_error = ""
        self._alert_queue = None
        self._alert_worker_task = None
        self._df = {}
        self._last_bar_ts = {}
        self._last_bar_sig = {}
        self._primed_keys = set(COMPONENT_SPEC)
        self.live_feed_status = "historical replay"
        self._enforce_live_open_guard = False
        self.replay_skips = defaultdict(int)
        self.replay_skip_examples = []

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"kind": kind, "msg": msg})

    def _alert(self, text: str):
        return None

    def _save(self):
        return None

    def _stops_after_target(self) -> bool:
        return self.stop_after_target

    def _can_open_key(self, key: str, cur: pd.Series) -> bool:
        if not self.enabled or self.failed:
            return False
        eq = self.equity()
        if self.stop_after_target and eq >= live.TARGET_BALANCE:
            self.passed = True
            return False
        if eq <= self.floor:
            self.failed = True
            return False
        if self.stop_after_target and self.day_pnl <= -live.DAILY_LOSS_LIMIT:
            self.daily_stopped_day = self.day_key
            return False
        bar_dt_utc = pd.Timestamp(cur["dt_utc"])
        return live._in_backtest_session_utc(bar_dt_utc)


def load_frames() -> dict[str, pd.DataFrame]:
    raw = {m: load_fut(m) for m in ["es", "nq", "cl"]}
    frames = {}
    for key, (market, rule) in COMPONENT_SPEC.items():
        # Resample through the same helper used by the original research scripts,
        # then convert back to the live bot's column layout.
        helper = resample(raw[market], rule).reset_index().rename(columns={"dt": "dt_utc"})
        helper["dt_utc"] = pd.to_datetime(helper["dt_utc"], utc=True)
        helper["dt_ny"] = helper["dt_utc"].dt.tz_convert(live.NY)
        helper = helper[["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"]]
        sec = {"3min": 180, "5min": 300, "30min": 1800}[rule]
        out = live._prepare(helper, sec, drop_incomplete=False)
        out["month"] = out["dt_utc"].dt.strftime("%Y-%m")
        frames[key] = out
    return frames


def build_context(frames: dict[str, pd.DataFrame]):
    ctx = {}
    for key, frame in frames.items():
        groups = frame.groupby("day", sort=True).indices
        daily_all = live._daily(frame)
        daily_by_day = {}
        for day in sorted(groups):
            idxs = groups[day]
            daily_by_day[day] = daily_all[daily_all["day"] < day].reset_index(drop=True)
            ctx[(key, day)] = (int(idxs[0]), int(idxs[-1]) + 1, daily_by_day[day])
    return ctx


def build_signal_map(frames: dict[str, pd.DataFrame], ctx) -> dict[tuple[str, int], dict]:
    signals = {}
    probe = ReplayBot(stop_after_target=False)
    for key, frame in frames.items():
        for day in sorted(frame["day"].unique()):
            start, end, prior_daily = ctx[(key, day)]
            today_full = frame.iloc[start:end].reset_index(drop=True)
            rel_idx = first_signal_idx(key, today_full, prior_daily)
            if rel_idx is None:
                continue
            idx = start + rel_idx
            today = frame.iloc[start: idx + 1].reset_index(drop=True)
            sig = probe._signal(key, today, prior_daily)
            if not sig or sig.get("spent"):
                raise AssertionError(f"{key} {day} trigger finder disagrees with production signal")
            signals[(key, idx)] = sig
    return signals


def event_stream(frames: dict[str, pd.DataFrame], month: str | None = None):
    events = []
    for key, frame in frames.items():
        d = frame
        if month is not None:
            d = d[d["month"] == month]
        for i, row in d[["dt_utc"]].iterrows():
            events.append((pd.Timestamp(row["dt_utc"]), key, int(i)))
    order = {key: i for i, key in enumerate(COMPONENT_SPEC)}
    events.sort(key=lambda x: (x[0], order[x[1]]))
    return events


def replay(frames: dict[str, pd.DataFrame], signals: dict[tuple[str, int], dict],
           month: str | None, stop_after_target: bool) -> ReplayBot:
    bot = ReplayBot(stop_after_target=stop_after_target)
    events = event_stream(frames, month)
    for _, key, idx in events:
        if bot.failed:
            break
        frame = frames[key]
        cur = frame.iloc[idx]
        if month is not None and cur["month"] != month:
            continue
        bot._roll_day(str(cur["day"]))
        bot.prices[key] = float(cur["close"])
        if key in bot.pos:
            bot._manage(key, cur)
        sig = signals.get((key, idx))
        if sig and key not in bot.pos and bot._can_open_key(key, cur):
            fired_key = f"{bot.day_key}:{key}"
            if fired_key not in bot.fired_keys:
                bot._open(sig, cur)
                if key in bot.pos:
                    bot._manage(key, cur)
                else:
                    bot.replay_skips["open_returned_no_position"] += 1
                    if len(bot.replay_skip_examples) < 10:
                        bot.replay_skip_examples.append((str(cur["dt_utc"]), key, "open_returned_no_position"))
            else:
                bot.replay_skips["already_fired"] += 1
                if len(bot.replay_skip_examples) < 10:
                    bot.replay_skip_examples.append((str(cur["dt_utc"]), key, "already_fired"))
        elif sig:
            fired_key = f"{bot.day_key}:{key}"
            if key in bot.pos:
                bot.replay_skips["position_still_open"] += 1
                if len(bot.replay_skip_examples) < 10:
                    p = bot.pos[key]
                    opened = str(pd.Timestamp(p.opened_bar, unit="s", tz="UTC"))
                    bot.replay_skip_examples.append((str(cur["dt_utc"]), key, "position_still_open", opened))
            elif bot.stop_after_target and bot.equity() >= live.TARGET_BALANCE:
                bot.passed = True
                bot.fired_keys.add(fired_key)
                bot.replay_skips["target_or_passed"] += 1
            elif (bot.stop_after_target and bot.day_pnl <= -live.DAILY_LOSS_LIMIT) or bot.failed:
                bot.fired_keys.add(fired_key)
                bot.replay_skips["risk_guard"] += 1
            else:
                bot.replay_skips["can_open_false"] += 1
                if len(bot.replay_skip_examples) < 10:
                    bot.replay_skip_examples.append((str(cur["dt_utc"]), key, "can_open_false"))
    for key in list(bot.pos.keys()):
        p = bot.pos[key]
        bot._close(key, p.last_close or p.entry, "eod")
    return bot


def month_rows(frames: dict[str, pd.DataFrame]):
    months = sorted({m for frame in frames.values() for m in frame["month"].unique()})
    return [m for m in months if "2023-07" <= m <= "2026-06"]


def assert_close(name: str, got: float, expected: float, tol: float) -> None:
    if abs(got - expected) > tol:
        raise AssertionError(f"{name}: got {got:.2f}, expected {expected:.2f}, tol {tol}")


def main() -> int:
    frames = load_frames()
    ctx = build_context(frames)
    signals = build_signal_map(frames, ctx)
    print(f"precomputed signals={len(signals)}")

    continuous = replay(frames, signals, None, stop_after_target=False)
    cont_profit = continuous.balance - live.START_BALANCE
    cont_trades = len(continuous.history)
    wins = sum(1 for h in continuous.history if h["pnl"] > 0)
    win_rate = 100.0 * wins / cont_trades
    print(
        f"continuous live-path final={continuous.balance:.2f} profit={cont_profit:.2f} "
        f"trades={cont_trades} win_rate={win_rate:.1f}%"
    )
    print(f"continuous skips={dict(continuous.replay_skips)} examples={continuous.replay_skip_examples}")
    assert_close("continuous profit", cont_profit, 246777.75, 350.0)
    if cont_trades != 2391:
        raise AssertionError(f"continuous trade count {cont_trades} != 2391")

    rows = []
    for month in month_rows(frames):
        bot = replay(frames, signals, month, stop_after_target=True)
        pnl = bot.balance - live.START_BALANCE
        worst = min((0.0, *(h["pnl"] for h in bot.history)), default=0.0)
        rows.append((month, pnl, len(bot.history), bot.passed, bot.failed, worst))
    passed = sum(1 for r in rows if r[3])
    failed = sum(1 for r in rows if r[4])
    total_pnl = sum(r[1] for r in rows)
    total_trades = sum(r[2] for r in rows)
    print(
        f"monthly pass live-path passed={passed}/{len(rows)} failed={failed} "
        f"total_pnl={total_pnl:.2f} avg_trades={total_trades/len(rows):.1f}"
    )
    if len(rows) != 36:
        raise AssertionError(f"monthly replay window count {len(rows)} != 36")
    if passed != len(rows) or failed:
        bad = [r for r in rows if not r[3] or r[4]][:5]
        raise AssertionError(f"monthly pass replay mismatch: {bad}")
    assert_close("monthly total pnl", total_pnl, 115073.26, 350.0)

    print("first/last monthly rows:")
    for row in rows[:3] + rows[-3:]:
        print(f"  {row[0]} pnl={row[1]:.2f} trades={row[2]} passed={row[3]} failed={row[4]}")
    print("Lucid live-path replay verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
