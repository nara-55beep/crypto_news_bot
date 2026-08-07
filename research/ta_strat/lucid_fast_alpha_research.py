"""
Causal futures adaptation of Zarattini/Pagani's 2026 fast-alpha overlay.

Published baseline:
  * At the RTH open, form bands at open +/- 0.5 * prior daily ATR(14).
  * Inspect the market only on completed 15-minute intervals.
  * Enter in the direction of a close outside a band.
  * The session open is the failed-breakout exit; flatten at the close.

Published overlay:
  * After a 15-minute breakout, wait for one completed opposite-direction
    five-minute bar before entering.
  * After the session-open exit is breached, wait for one completed
    counter-move five-minute bar before liquidating.

Execution safeguards in this research implementation:
  * completed bars can only execute at the next one-minute open;
  * entry and exit each receive one adverse tick;
  * breached stops fill at the worse of the stop or available minute open;
  * overlay exits have a deeper, pre-declared hard stop because an indefinitely
    delayed liquidation is incompatible with a Lucid maximum-loss rule;
  * positions are flattened at the 15:59 New York open.

The source paper tests SPY.  These tests use the local Dukascopy USA500/USATECH
cash-index proxies, so a passing result would still require confirmation on
real CME MES/MNQ data before trading.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L
import lucid_eval_scalper_research as E


@dataclass(frozen=True)
class FastConfig:
    market: str
    atr_len: int
    band_mult: float
    entry_mode: str
    exit_mode: str
    streak: int
    hard_atr: float

    @property
    def label(self) -> str:
        return (
            f"{self.market}_atr_breakout_a{self.atr_len}_"
            f"k{self.band_mult:g}_en{self.entry_mode}_"
            f"ex{self.exit_mode}_n{self.streak}_h{self.hard_atr:g}"
        )


def configs(market: str) -> list[FastConfig]:
    out: list[FastConfig] = []
    for atr_len in (10, 14, 20):
        for band_mult in (0.35, 0.50, 0.65):
            # The baseline has no streak or delayed-stop parameters.
            out.append(
                FastConfig(
                    market, atr_len, band_mult, "immediate",
                    "immediate", 1, 0.0,
                )
            )
            for streak in (1, 2):
                for exit_mode in ("immediate", "overlay"):
                    hard_values = (0.25, 0.50) if exit_mode == "overlay" else (0.0,)
                    for hard_atr in hard_values:
                        out.append(
                            FastConfig(
                                market, atr_len, band_mult, "overlay",
                                exit_mode, streak, hard_atr,
                            )
                        )
    return out


def _prior_atr(days: list[L.Day], length: int) -> np.ndarray:
    """Simple daily ATR known before each session opens."""
    tr = np.full(len(days), np.nan)
    for i, day in enumerate(days):
        high = float(np.max(day.hi))
        low = float(np.min(day.lo))
        if i == 0:
            tr[i] = high - low
        else:
            prior_close = float(days[i - 1].cl[-1])
            tr[i] = max(
                high - low,
                abs(high - prior_close),
                abs(low - prior_close),
            )
    out = np.full(len(days), np.nan)
    for i in range(length, len(days)):
        out[i] = float(np.mean(tr[i - length:i]))
    return out


def _five_minute_sign(day: L.Day, i: int) -> int:
    """Sign of the just-completed clock-aligned five-minute return."""
    start = i - 4
    if start < 0:
        return 0
    change = float(day.cl[i]) - float(day.op[start])
    return int(change > 0) - int(change < 0)


def _streak_matches(day: L.Day, i: int, side: int, streak: int) -> bool:
    """Entry wants bars opposite side; delayed exit wants caller to flip side."""
    for offset in range(streak):
        end = i - offset * 5
        if end < 4 or _five_minute_sign(day, end) != -side:
            return False
    return True


def _append_trade(
    out: list[L.Trade],
    day: L.Day,
    cfg: FastConfig,
    side: int,
    entry_i: int,
    entry: float,
    hard_stop: float,
    exit_i: int,
    raw_exit: float,
    reason: str,
) -> None:
    tick = L.MARKETS[cfg.market]["tick"]
    pv = L.MARKETS[cfg.market]["pv"]
    exit_px = float(raw_exit) - side * tick
    out.append(
        L.Trade(
            market=cfg.market,
            strategy=cfg.label,
            day=day.day,
            entry_ts=pd.Timestamp(day.ts[entry_i]),
            exit_ts=pd.Timestamp(day.ts[exit_i]),
            side=side,
            entry=entry,
            stop=hard_stop,
            target=math.nan,
            exit=exit_px,
            reason=reason,
            risk_per_micro=(abs(entry - hard_stop) + tick) * pv,
            gross_per_micro=side * (exit_px - entry) * pv,
        )
    )


def generate(days: list[L.Day], cfg: FastConfig) -> list[L.Trade]:
    out: list[L.Trade] = []
    atr = _prior_atr(days, cfg.atr_len)
    tick = L.MARKETS[cfg.market]["tick"]

    for n in range(cfg.atr_len, len(days)):
        day = days[n]
        daily_atr = float(atr[n])
        if not math.isfinite(daily_atr) or daily_atr <= tick:
            continue
        open_level = float(day.op[0])
        upper = open_level + cfg.band_mult * daily_atr
        lower = open_level - cfg.band_mult * daily_atr
        final_matches = np.flatnonzero(day.minute >= 389)
        if not len(final_matches):
            continue
        final_i = int(final_matches[0])

        side = 0
        entry_i = -1
        entry = hard_stop = 0.0
        pending_side = 0
        scheduled_entry = 0
        scheduled_exit = False
        soft_breached = False

        for i in range(final_i):
            # Orders inferred from a completed bar execute at this minute's open.
            if scheduled_exit and side:
                _append_trade(
                    out, day, cfg, side, entry_i, entry, hard_stop,
                    i, float(day.op[i]), "overlay_exit",
                )
                side = 0
                scheduled_exit = False
                soft_breached = False

            if scheduled_entry and side == 0:
                side = scheduled_entry
                scheduled_entry = 0
                pending_side = 0
                entry_i = i
                entry = float(day.op[i]) + side * tick
                if cfg.exit_mode == "overlay":
                    hard_stop = open_level - side * cfg.hard_atr * daily_atr
                else:
                    hard_stop = open_level
                # Do not manufacture a position whose risk boundary is already
                # beyond the slipped entry.
                if (
                    (side > 0 and hard_stop >= entry)
                    or (side < 0 and hard_stop <= entry)
                ):
                    side = 0

            # Intraminute risk is evaluated only after an opening fill.
            if side:
                hard_hit = (
                    float(day.lo[i]) <= hard_stop
                    if side > 0
                    else float(day.hi[i]) >= hard_stop
                )
                if hard_hit:
                    raw = (
                        min(hard_stop, float(day.op[i]))
                        if side > 0
                        else max(hard_stop, float(day.op[i]))
                    )
                    _append_trade(
                        out, day, cfg, side, entry_i, entry, hard_stop,
                        i, raw,
                        "open_stop" if cfg.exit_mode == "immediate" else "hard_stop",
                    )
                    side = 0
                    soft_breached = False
                    scheduled_exit = False
                elif cfg.exit_mode == "overlay":
                    soft_hit = (
                        float(day.lo[i]) <= open_level
                        if side > 0
                        else float(day.hi[i]) >= open_level
                    )
                    soft_breached = soft_breached or soft_hit

            minute = int(day.minute[i])
            five_close = minute % 5 == 4
            fifteen_close = minute % 15 == 14

            # A newly observed 15-minute signal remains eligible while the
            # overlay waits for a micro-pullback.  An opposite breakout replaces it.
            if side == 0 and fifteen_close and i + 1 < final_i:
                close = float(day.cl[i])
                if close > upper:
                    pending_side = 1
                elif close < lower:
                    pending_side = -1

                if cfg.entry_mode == "immediate" and pending_side:
                    scheduled_entry = pending_side

            if (
                side == 0
                and cfg.entry_mode == "overlay"
                and pending_side
                and five_close
                and i + 1 < final_i
                and _streak_matches(day, i, pending_side, cfg.streak)
            ):
                scheduled_entry = pending_side

            if (
                side
                and cfg.exit_mode == "overlay"
                and soft_breached
                and five_close
                and i + 1 < final_i
                # For an open long, the published delayed exit waits for an up bar;
                # _streak_matches therefore receives the opposite of position side.
                and _streak_matches(day, i, -side, cfg.streak)
            ):
                scheduled_exit = True

        if side:
            _append_trade(
                out, day, cfg, side, entry_i, entry, hard_stop,
                final_i, float(day.op[final_i]), "eod",
            )

    return sorted(out, key=lambda trade: (trade.entry_ts, trade.exit_ts))


def _stats(
    trades: list[L.Trade],
    risk: float,
    lo: date | None,
    hi: date | None,
) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", choices=("es", "nq"), default=("es", "nq"))
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    rows = []
    for market in args.markets:
        days = L.load_days(market)
        grid = configs(market)
        print(f"{market}: {len(grid)} ATR-breakout configurations", flush=True)
        for number, cfg in enumerate(grid, 1):
            trades = generate(days, cfg)
            train = _stats(trades, args.risk, None, L.TRAIN_END)
            valid = _stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
            score = E.development_score(train, valid)
            rows.append((score, cfg, trades, train, valid))
            if number % 20 == 0:
                print(f"  completed {number}/{len(grid)}", flush=True)

    rows.sort(key=lambda row: row[0], reverse=True)
    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for score, cfg, trades, train, valid in rows[:args.top]:
        test = _stats(trades, args.risk, date(2024, 1, 1), None)
        print(f"\n{cfg.label} score {score:.3f}")
        for label, result in (("train", train), ("valid", valid), ("TEST", test)):
            print(
                f"  {label:<5} n{result['n']:5} PF{result['pf']:.2f} "
                f"net{result['net']:+9.0f} avg{result['avg']:+6.1f} "
                f"DD{result['maxdd']:8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
