"""
Replay the actual Lucid production _tick scanner over historical candles.

verify_lucid_replay.py proves trade math using precomputed signal locations.
This verifier feeds rolling historical candle windows into LucidPassPaperBot._tick
so the same live event scanner, day rolling, fired-key logic, open, partial, and
close code is exercised together. Signal locations come from the parity-checked
production signal map to keep this verifier fast enough to run locally.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lucid_pass_paper as live
import verify_lucid_replay as ref


KEEP_DAYS = 20
EXPECTED_CONTINUOUS_PROFIT = 246_777.75
EXPECTED_CONTINUOUS_TRADES = 2_391
EXPECTED_MONTHLY_TOTAL = 115_073.26


class TickReplayBot(ref.ReplayBot):
    """Historical replay bot that uses production _tick instead of prebuilt signals."""

    def __init__(self, stop_after_target: bool, signal_by_ts: dict[tuple[str, int], dict]):
        super().__init__(stop_after_target=stop_after_target)
        self.signal_by_ts = signal_by_ts
        self.opened_events: list[tuple[str, int]] = []
        self.live_feed_status = "historical production tick replay"

    def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame) -> dict | None:
        if today.empty:
            return None
        ts = int(pd.Timestamp(today.iloc[-1]["dt_utc"]).timestamp())
        sig = self.signal_by_ts.get((key, ts))
        return dict(sig) if sig else None

    def _can_open_key(self, key: str, cur: pd.Series) -> bool:
        ts = int(pd.Timestamp(cur["dt_utc"]).timestamp())
        if (key, ts) not in self.signal_by_ts:
            return False
        return super()._can_open_key(key, cur)

    def _open(self, sig: dict, cur: pd.Series):
        super()._open(sig, cur)
        ts = int(pd.Timestamp(cur["dt_utc"]).timestamp())
        if sig.get("key") in self.pos and self.pos[sig["key"]].opened_bar == ts:
            self.opened_events.append((sig["key"], ts))


def _days(frames: dict[str, pd.DataFrame]) -> list:
    return sorted({day for frame in frames.values() for day in frame["day"].unique()})


def _prime_last_bar(bot: TickReplayBot, frames: dict[str, pd.DataFrame],
                    first_day, month: str | None) -> None:
    for key, frame in frames.items():
        before = frame[frame["day"] < first_day]
        if month is not None:
            month_start = pd.Timestamp(f"{month}-01", tz="UTC")
            before = before[before["dt_utc"] < month_start]
        if before.empty:
            first_ts = pd.Timestamp(frame.iloc[0]["dt_utc"])
            bot._last_bar_ts[key] = int(first_ts.timestamp()) - int(live.COMPONENTS[key]["bar_sec"])
            continue
        cur = before.iloc[-1]
        bot._last_bar_ts[key] = int(pd.Timestamp(cur["dt_utc"]).timestamp())
        bot._last_bar_sig[key] = bot._bar_signature(cur)


def _rolling_frames(frames: dict[str, pd.DataFrame], day) -> dict[str, pd.DataFrame]:
    out = {}
    for key, frame in frames.items():
        prior_days = sorted(d for d in frame["day"].unique() if d <= day)
        keep = set(prior_days[-KEEP_DAYS:])
        out[key] = frame[frame["day"].isin(keep)].reset_index(drop=True)
    return out


def _rolling_frame_cache(frames: dict[str, pd.DataFrame]) -> dict:
    cache: dict = {}
    for key, frame in frames.items():
        groups = frame.groupby("day", sort=True).indices
        days = sorted(groups)
        for pos, day in enumerate(days):
            keep_days = days[max(0, pos - KEEP_DAYS + 1):pos + 1]
            start = min(int(groups[d][0]) for d in keep_days)
            end = max(int(groups[d][-1]) + 1 for d in keep_days)
            cache.setdefault(day, {})[key] = frame.iloc[start:end].reset_index(drop=True)
    return cache


def signal_by_ts(frames: dict[str, pd.DataFrame]) -> dict[tuple[str, int], dict]:
    ctx = ref.build_context(frames)
    by_index = ref.build_signal_map(frames, ctx)
    out = {}
    for (key, idx), sig in by_index.items():
        ts = int(pd.Timestamp(frames[key].iloc[idx]["dt_utc"]).timestamp())
        out[(key, ts)] = sig
    return out


def tick_replay(frames: dict[str, pd.DataFrame], signals: dict[tuple[str, int], dict], month: str | None,
                rolling_cache: dict,
                stop_after_target: bool) -> TickReplayBot:
    bot = TickReplayBot(stop_after_target=stop_after_target, signal_by_ts=signals)
    days = _days(frames)
    if month is not None:
        days = [d for d in days if str(d)[:7] == month]
    if not days:
        raise AssertionError(f"no replay days for month={month}")
    _prime_last_bar(bot, frames, days[0], month)
    for day in days:
        if bot.failed:
            break
        bot._df = rolling_cache[day]
        bot._tick()
    for key in list(bot.pos.keys()):
        p = bot.pos[key]
        bot._close(key, p.last_close or p.entry, "eod")
    return bot


def assert_close(name: str, got: float, expected: float, tol: float) -> None:
    if abs(got - expected) > tol:
        raise AssertionError(f"{name}: got {got:.2f}, expected {expected:.2f}, tol {tol}")


def main() -> int:
    frames = ref.load_frames()
    signals = signal_by_ts(frames)
    rolling_cache = _rolling_frame_cache(frames)
    print(f"production signal timestamps={len(signals)}")

    continuous = tick_replay(frames, signals, None, rolling_cache, stop_after_target=False)
    cont_profit = continuous.balance - live.START_BALANCE
    cont_trades = len(continuous.history)
    print(
        f"production tick continuous final={continuous.balance:.2f} "
        f"profit={cont_profit:.2f} trades={cont_trades}"
    )
    missing = sorted(set(signals) - set(continuous.opened_events), key=lambda x: (x[1], x[0]))
    if missing:
        examples = [
            (key, str(pd.Timestamp(ts, unit="s", tz="UTC")))
            for key, ts in missing[:10]
        ]
        print(f"production tick missing opens={len(missing)} examples={examples}")
    assert_close("production tick continuous profit", cont_profit, EXPECTED_CONTINUOUS_PROFIT, 350.0)
    if cont_trades != EXPECTED_CONTINUOUS_TRADES:
        raise AssertionError(
            f"production tick continuous trades {cont_trades} != {EXPECTED_CONTINUOUS_TRADES}"
        )

    rows = []
    for month in ref.month_rows(frames):
        bot = tick_replay(frames, signals, month, rolling_cache, stop_after_target=True)
        rows.append((month, bot.balance - live.START_BALANCE, len(bot.history), bot.passed, bot.failed))
    passed = sum(1 for row in rows if row[3])
    failed = sum(1 for row in rows if row[4])
    total = sum(row[1] for row in rows)
    trades = sum(row[2] for row in rows)
    print(
        f"production tick monthly passed={passed}/{len(rows)} failed={failed} "
        f"total_pnl={total:.2f} avg_trades={trades/len(rows):.1f}"
    )
    if passed != len(rows) or failed:
        bad = [row for row in rows if not row[3] or row[4]][:5]
        raise AssertionError(f"production tick monthly replay mismatch: {bad}")
    assert_close("production tick monthly total pnl", total, EXPECTED_MONTHLY_TOTAL, 350.0)

    for row in rows[:3] + rows[-3:]:
        print(f"  {row[0]} pnl={row[1]:.2f} trades={row[2]} passed={row[3]} failed={row[4]}")
    print("Lucid production tick replay verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
