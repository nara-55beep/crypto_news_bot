"""
Evidence-guided, strictly causal intraday futures research for LucidPro 50K.

Families:
  1. close_momentum
     Published "rest of day predicts last half-hour" effect. Decide on the completed
     minute immediately before a fixed entry time, enter next minute, exit by 15:59 NY.
  2. timely_orb
     Clock-limited, optionally volume-confirmed opening-range breakout. A completed
     bar must CLOSE outside the known range; entry is the following one-minute open.
  3. cross_prior
     NQ prior-range momentum only when completed ES information confirms direction.
  4. morning_regime
     Fixed-time opening drive, gap continuation/fill, or prior-day continuation.

This module imports the same fills, sizing, data, and Lucid evaluator as
lucid_causal_rebuild.py. It is research-only and cannot enable a paper bot.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

import lucid_causal_rebuild as L


@dataclass(frozen=True)
class PConfig:
    family: str
    market: str
    tf: int = 1
    entry_minute: int = 360
    threshold: float = 0.0
    stop_mult: float = 1.0
    target_rr: float | None = None
    or_min: int = 15
    cutoff: int = 120
    volume_ratio: float = 0.0
    stop_mode: str = "bar"
    k: float = 0.25
    confirm: str = "sign"
    reverse: bool = False
    mode: str = "drive"
    location: float = 0.70

    @property
    def label(self) -> str:
        if self.family == "close_momentum":
            target = "eod" if self.target_rr is None else f"r{self.target_rr:g}"
            return (
                f"{self.market}_{'closerev' if self.reverse else 'closemom'}_enter{self.entry_minute}_"
                f"th{self.threshold:g}_s{self.stop_mult:g}_{target}"
            )
        if self.family == "timely_orb":
            return (
                f"{self.market}_torb_or{self.or_min}_tf{self.tf}_cut{self.cutoff}_"
                f"v{self.volume_ratio:g}_{self.stop_mode}_r{self.target_rr:g}"
            )
        if self.family == "morning_regime":
            return (
                f"{self.market}_morning_{self.mode}_m{self.entry_minute}_"
                f"th{self.threshold:g}_loc{self.location:g}_{self.stop_mode}_r{self.target_rr:g}"
            )
        return (
            f"{self.market}_crossprior_tf{self.tf}_k{self.k:g}_"
            f"{self.stop_mode}_{self.confirm}_r{self.target_rr:g}"
        )


def _timed_trade(
    day: L.Day,
    signal_i: int,
    side: int,
    stop_points: float,
    strategy: str,
    *,
    end_minute: int = 389,
    target_rr: float | None = None,
) -> L.Trade | None:
    """Next-open entry, gap-aware stop, optional target, and deterministic timed exit."""
    fi = signal_i + 1
    if fi >= len(day.cl):
        return None
    tick = L.MARKETS[day.market]["tick"]
    pv = L.MARKETS[day.market]["pv"]
    entry = float(day.op[fi]) + side * tick
    stop_points = max(float(stop_points), tick)
    stop = entry - side * stop_points
    target = entry + side * target_rr * stop_points if target_rr is not None else None
    eligible = np.flatnonzero(day.minute <= end_minute)
    if not len(eligible) or eligible[-1] < fi:
        return None
    end_i = int(eligible[-1])
    raw_exit = float(day.cl[end_i])
    reason = "time"
    exit_i = end_i
    for j in range(fi, end_i + 1):
        stopped = day.lo[j] <= stop if side > 0 else day.hi[j] >= stop
        targeted = (
            False if target is None
            else day.hi[j] >= target if side > 0
            else day.lo[j] <= target
        )
        if stopped:
            raw_exit = min(stop, float(day.op[j])) if side > 0 else max(stop, float(day.op[j]))
            reason = "stop"
            exit_i = j
            break
        if targeted:
            raw_exit = float(target)
            reason = "target"
            exit_i = j
            break
    exit_px = raw_exit - side * tick
    gross = side * (exit_px - entry) * pv
    return L.Trade(
        market=day.market,
        strategy=strategy,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[fi]),
        exit_ts=pd.Timestamp(day.ts[exit_i]),
        side=side,
        entry=entry,
        stop=stop,
        target=float(target) if target is not None else math.nan,
        exit=exit_px,
        reason=reason,
        risk_per_micro=(stop_points + tick) * pv,
        gross_per_micro=gross,
    )


def close_momentum(days: list[L.Day], cfg: PConfig) -> list[L.Trade]:
    """Trade in the sign of prior-close -> pre-entry return during the final session."""
    out = []
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        prior_close = float(prior.cl[-1])
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        move = float(day.cl[i]) - prior_close
        if prior_range <= 0 or abs(move) < cfg.threshold * prior_range:
            continue
        side = 1 if move > 0 else -1
        if cfg.reverse:
            side *= -1
        # Scale a one-minute ATR to the remaining horizon.
        remaining = max(1, 390 - cfg.entry_minute)
        stop_points = cfg.stop_mult * float(day.atr[i]) * math.sqrt(remaining)
        trade = _timed_trade(
            day,
            i,
            side,
            stop_points,
            cfg.label,
            target_rr=cfg.target_rr,
        )
        if trade is not None:
            out.append(trade)
    return out


def _prior_clock_volume(prior: list[L.Day], minute_lo: int, minute_hi: int) -> float | None:
    vals = []
    for day in prior[-20:]:
        mask = (day.minute >= minute_lo) & (day.minute <= minute_hi)
        if np.any(mask):
            vals.append(float(np.sum(day.vol[mask])))
    return float(np.median(vals)) if len(vals) >= 10 else None


def timely_orb(days: list[L.Day], cfg: PConfig) -> list[L.Trade]:
    out = []
    prior: list[L.Day] = []
    tick = L.MARKETS[cfg.market]["tick"]
    for day in days:
        opening = day.minute < cfg.or_min
        if not np.any(opening):
            prior.append(day)
            continue
        or_hi = float(np.max(day.hi[opening]))
        or_lo = float(np.min(day.lo[opening]))
        or_mid = (or_hi + or_lo) / 2.0
        if or_hi <= or_lo:
            prior.append(day)
            continue
        prev_close = float(day.cl[int(np.flatnonzero(opening)[-1])])
        for i in L._sample_indices(day, cfg.tf, start=cfg.or_min + cfg.tf - 1):
            if day.minute[i] >= cfg.cutoff:
                break
            side = 0
            if prev_close <= or_hi and day.cl[i] > or_hi:
                side = 1
            elif prev_close >= or_lo and day.cl[i] < or_lo:
                side = -1
            if not side:
                prev_close = float(day.cl[i])
                continue
            if cfg.volume_ratio > 0:
                lo_min = int(day.minute[i]) - cfg.tf + 1
                now_vol = float(np.sum(day.vol[max(0, i - cfg.tf + 1):i + 1]))
                benchmark = _prior_clock_volume(prior, lo_min, int(day.minute[i]))
                if benchmark is None or now_vol < cfg.volume_ratio * benchmark:
                    prev_close = float(day.cl[i])
                    continue
            if cfg.stop_mode == "opposite":
                stop = or_lo if side > 0 else or_hi
            elif cfg.stop_mode == "mid":
                stop = or_mid
            else:
                stop = (
                    L._block_extreme(day.lo, int(i), cfg.tf, np.min) - tick
                    if side > 0 else
                    L._block_extreme(day.hi, int(i), cfg.tf, np.max) + tick
                )
            trade = L._make_trade(
                day,
                int(i),
                side,
                float(stop),
                rr=float(cfg.target_rr),
                strategy=cfg.label,
            )
            if trade is not None:
                out.append(trade)
                break
            prev_close = float(day.cl[i])
        prior.append(day)
    return out


def _at_or_before(day: L.Day, ts: np.datetime64) -> int | None:
    idx = np.flatnonzero(day.ts <= ts)
    return int(idx[-1]) if len(idx) else None


def cross_prior(nq_days: list[L.Day], es_days: list[L.Day], cfg: PConfig) -> list[L.Trade]:
    """NQ prior-range breakout with ES confirmation known at the NQ signal close."""
    es_by_date = {d.day: d for d in es_days}
    out = []
    for n in range(1, len(nq_days)):
        day, prior = nq_days[n], nq_days[n - 1]
        es = es_by_date.get(day.day)
        if es is None:
            continue
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        if prior_range <= 0:
            continue
        tick = L.MARKETS["nq"]["tick"]
        long_level = float(day.op[0]) + cfg.k * prior_range
        short_level = float(day.op[0]) - cfg.k * prior_range
        prev_i = None
        for i in L._sample_indices(day, cfg.tf, start=cfg.tf - 1):
            prev_close = float(day.op[0]) if prev_i is None else float(day.cl[prev_i])
            side = (
                1 if prev_close <= long_level and day.cl[i] > long_level
                else -1 if prev_close >= short_level and day.cl[i] < short_level
                else 0
            )
            prev_i = int(i)
            if not side:
                continue
            ei = _at_or_before(es, day.ts[i])
            if ei is None:
                continue
            es_move = float(es.cl[ei] - es.op[0])
            confirmed = es_move * side > 0
            if cfg.confirm == "vwap":
                confirmed = confirmed and (float(es.cl[ei] - es.vwap[ei]) * side > 0)
            elif cfg.confirm == "ema":
                confirmed = confirmed and (float(es.ema9[ei] - es.ema20[ei]) * side > 0)
            if not confirmed:
                continue
            if cfg.stop_mode == "open":
                stop = float(day.op[0])
            else:
                stop = (
                    L._block_extreme(day.lo, int(i), cfg.tf, np.min) - tick
                    if side > 0 else
                    L._block_extreme(day.hi, int(i), cfg.tf, np.max) + tick
                )
            trade = L._make_trade(
                day,
                int(i),
                side,
                float(stop),
                rr=float(cfg.target_rr),
                strategy=cfg.label,
            )
            if trade is not None:
                out.append(trade)
                break
    return out


def morning_regime(days: list[L.Day], cfg: PConfig) -> list[L.Trade]:
    """Fixed-time morning classification with no later-in-day signal selection."""
    out = []
    tick = L.MARKETS[cfg.market]["tick"]
    for n in range(1, len(days)):
        day, prior = days[n], days[n - 1]
        signal_minute = cfg.entry_minute - 1
        idx = np.flatnonzero(day.minute <= signal_minute)
        if not len(idx) or day.minute[idx[-1]] != signal_minute:
            continue
        i = int(idx[-1])
        ph = float(np.max(day.hi[:i + 1]))
        pl = float(np.min(day.lo[:i + 1]))
        opening_range = ph - pl
        prior_range = float(np.max(prior.hi) - np.min(prior.lo))
        if opening_range <= 0 or prior_range <= 0:
            continue
        op = float(day.op[0])
        close = float(day.cl[i])
        gap = op - float(prior.cl[-1])
        drive = close - op
        prior_move = float(prior.cl[-1] - prior.op[0])
        if cfg.mode == "drive":
            basis = drive
            side = 1 if basis > 0 else -1
        elif cfg.mode == "gap_go":
            basis = gap
            side = 1 if basis > 0 else -1
            if drive * side <= 0:
                continue
        elif cfg.mode == "gap_fill":
            basis = gap
            side = -1 if basis > 0 else 1
            if drive * side <= 0:
                continue
        else:
            basis = prior_move
            side = 1 if basis > 0 else -1
            if drive * side <= 0:
                continue
        if basis == 0 or abs(basis) < cfg.threshold * prior_range:
            continue
        location = (close - pl) / opening_range
        if (side > 0 and location < cfg.location) or (
            side < 0 and location > 1.0 - cfg.location
        ):
            continue
        if cfg.stop_mode == "open":
            stop = op
        elif cfg.stop_mode == "range":
            stop = pl - tick if side > 0 else ph + tick
        else:
            lo = max(0, i - min(15, cfg.entry_minute) + 1)
            stop = (
                float(np.min(day.lo[lo:i + 1])) - tick
                if side > 0 else
                float(np.max(day.hi[lo:i + 1])) + tick
            )
        trade = L._make_trade(
            day,
            i,
            side,
            float(stop),
            rr=float(cfg.target_rr),
            strategy=cfg.label,
        )
        if trade is not None:
            out.append(trade)
    return out


def all_configs(markets: Iterable[str]) -> list[PConfig]:
    out = []
    for market in markets:
        for entry_minute in (300, 330, 360):
            for threshold in (0.0, 0.10, 0.25):
                for stop_mult in (0.75, 1.25, 2.0):
                    for target in (None, 2.0):
                        for reverse in (False, True):
                            out.append(PConfig(
                                "close_momentum",
                                market,
                                entry_minute=entry_minute,
                                threshold=threshold,
                                stop_mult=stop_mult,
                                target_rr=target,
                                reverse=reverse,
                            ))
        for or_min in (5, 15, 30):
            for tf in (1, 5):
                for cutoff in (60, 120):
                    for volume_ratio in (0.0, 1.25):
                        for stop_mode in ("mid", "bar"):
                            for rr in (1.5, 2.0):
                                out.append(PConfig(
                                    "timely_orb",
                                    market,
                                    tf=tf,
                                    or_min=or_min,
                                    cutoff=cutoff,
                                    volume_ratio=volume_ratio,
                                    stop_mode=stop_mode,
                                    target_rr=rr,
                                ))
        for entry_minute in (15, 30, 60):
            for mode in ("drive", "gap_go", "gap_fill", "prior_cont"):
                for threshold in (0.0, 0.10, 0.25):
                    for location in (0.60, 0.80):
                        for stop_mode in ("open", "range", "bar"):
                            for rr in (1.5, 2.0):
                                out.append(PConfig(
                                    "morning_regime",
                                    market,
                                    entry_minute=entry_minute,
                                    mode=mode,
                                    threshold=threshold,
                                    location=location,
                                    stop_mode=stop_mode,
                                    target_rr=rr,
                                ))
    if "nq" in markets and "es" in markets:
        for tf in (3, 5, 15):
            for k in (0.25, 0.50):
                for stop_mode in ("open", "bar"):
                    for confirm in ("sign", "vwap", "ema"):
                        out.append(PConfig(
                            "cross_prior",
                            "nq",
                            tf=tf,
                            k=k,
                            stop_mode=stop_mode,
                            confirm=confirm,
                            target_rr=2.0,
                        ))
    return out


def _stats(trades: list[L.Trade], risk: float, lo: date | None, hi: date | None) -> dict:
    return L.basic_stats(L.size_trades(L._slice(trades, lo, hi), risk))


def _score(train: dict, valid: dict) -> float:
    if min(train["n"], valid["n"]) < 30:
        return -1e9
    return min(train["pf"], valid["pf"]) * math.log1p(min(train["n"], valid["n"])) + min(
        train["avg"], valid["avg"]
    ) / 100.0


def run_config(
    cfg: PConfig,
    days: dict[str, list[L.Day]],
) -> list[L.Trade]:
    if cfg.family == "close_momentum":
        return close_momentum(days[cfg.market], cfg)
    if cfg.family == "timely_orb":
        return timely_orb(days[cfg.market], cfg)
    if cfg.family == "morning_regime":
        return morning_regime(days[cfg.market], cfg)
    return cross_prior(days["nq"], days["es"], cfg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["es", "nq", "cl"])
    ap.add_argument(
        "--families",
        nargs="+",
        choices=["close_momentum", "timely_orb", "cross_prior", "morning_regime"],
        default=["close_momentum", "timely_orb", "cross_prior", "morning_regime"],
    )
    ap.add_argument("--risk", type=float, default=300.0)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    markets = list(dict.fromkeys(args.markets))
    days = {m: L.load_days(m) for m in markets}
    configs = [c for c in all_configs(markets) if c.family in args.families]
    print(f"{len(configs)} evidence-guided configurations", flush=True)
    rows = []
    cache = {}
    for n, cfg in enumerate(configs, 1):
        trades = run_config(cfg, days)
        cache[cfg.label] = trades
        train = _stats(trades, args.risk, None, L.TRAIN_END)
        valid = _stats(trades, args.risk, date(2022, 1, 1), L.VALID_END)
        rows.append({
            "cfg": cfg,
            "label": cfg.label,
            "trades": trades,
            "train": train,
            "valid": valid,
            "score": _score(train, valid),
        })
        if n % 100 == 0:
            print(f"  {n}/{len(configs)}", flush=True)
    rows.sort(key=lambda x: x["score"], reverse=True)
    finalists = rows[: args.top]

    def fmt(s):
        return (
            f"n{s['n']:4} PF{s['pf']:.2f} net{s['net']:+9.0f} "
            f"avg{s['avg']:+6.1f} DD{s['maxdd']:8.0f}"
        )

    print("\nFINALISTS SELECTED ON TRAIN + VALIDATION ONLY")
    for row in finalists:
        test = _stats(row["trades"], args.risk, date(2024, 1, 1), None)
        row["test"] = test
        print(f"{row['label']:<66} train {fmt(row['train'])}")
        print(f"{'':66} valid {fmt(row['valid'])}")
        print(f"{'':66} TEST  {fmt(test)}")

    robust = [
        r for r in finalists
        if min(r["train"]["pf"], r["valid"]["pf"], r["test"]["pf"]) > 1.10
        and min(r["train"]["n"], r["valid"]["n"], r["test"]["n"]) >= 30
    ]
    print("\nROBUST FINALISTS")
    if not robust:
        print("NONE")
        return 0

    # Do not combine correlated variants. Take the top development-selected member
    # of each family/market only, then require its test gate.
    selected = []
    used = set()
    for row in finalists:
        key = (row["cfg"].family, row["cfg"].market)
        if key in used:
            continue
        used.add(key)
        if row in robust:
            selected.append(row)
            print(row["label"])

    if not selected:
        print("No top development-selected family member survived test.")
        return 0
    portfolio = L.non_overlapping(
        trade for row in selected for trade in row["trades"]
    )
    all_days = sorted({d.day for m in markets for d in days[m]})
    print("\nPORTFOLIO LUCID WINDOWS")
    for period, lo, hi in (
        ("train", None, L.TRAIN_END),
        ("valid", date(2022, 1, 1), L.VALID_END),
        ("test", date(2024, 1, 1), None),
    ):
        ds = [d for d in all_days if (lo is None or d >= lo) and (hi is None or d <= hi)]
        raw = L._slice(portfolio, lo, hi)
        print(period)
        for risk in (300.0, 400.0, 500.0, 600.0):
            sized = L.size_trades(raw, risk)
            e12 = L.eval_lucid(sized, ds, 12)
            e30 = L.eval_lucid(sized, ds, 30)
            print(
                f"  ${risk:.0f}: 12d pass {e12['pass_all']*100:5.1f}% "
                f"fail {e12['fails']/e12['starts']*100 if e12['starts'] else 0:5.1f}% | "
                f"30d pass {e30['pass_all']*100:5.1f}% "
                f"fail {e30['fails']/e30['starts']*100 if e30['starts'] else 0:5.1f}% "
                f"median {e30['median_days']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
