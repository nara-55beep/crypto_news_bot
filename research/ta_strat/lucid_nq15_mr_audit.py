"""
Literal audit of the repository's "NQ 15m Mean Reversion" bundle.

The legacy headline is reproduced with its original replay code.  The causal
replay below keeps the same three setups and parameters, but:

* forms signals only after a completed 15-minute RTH candle;
* enters at the following one-minute open plus one adverse MNQ tick;
* uses only integer MNQ contracts, with a 40-micro aggregate cap;
* charges Lucid's $0.50/side MNQ commission and one adverse exit tick;
* fills gap-through stops at the worse open and gives stops priority when a
  stop and target occur in the same one-minute candle;
* takes an executable integer partial at +1R, moves the runner stop to
  breakeven starting with the next minute, and exits by 15:55 New York;
* evaluates rolling LucidPro 50K windows over every session, including
  sessions with no trades.

This is research-only and does not import or mutate the paper bot.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import mean, median

import numpy as np
import pandas as pd


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TOOLS = os.path.join(ROOT, "tools")
for path in (HERE, TOOLS):
    if path not in sys.path:
        sys.path.insert(0, path)

import lucid_causal_rebuild as L


TICK = 0.25
POINT_VALUE = 2.0
COMMISSION_RT = 1.00
RISK_USD = 600.0
MAX_MICROS = 40

TARGET_PROFIT = 3_000.0
MAX_LOSS = 2_000.0
LOCK_TRIGGER = 2_100.0
LOCKED_FLOOR = 100.0
DAILY_LOSS_LIMIT = 1_200.0
FLAT_MINUTE = 385  # 15:55 New York relative to the 09:30 RTH open.

TRAIN_END = date(2021, 12, 31)
VALID_END = date(2023, 12, 31)


@dataclass(frozen=True)
class Bar15:
    start_i: int
    end_i: int
    op: float
    hi: float
    lo: float
    cl: float
    vol: float


@dataclass(frozen=True)
class CausalTrade:
    strategy: str
    day: date
    entry_ts: pd.Timestamp
    partial_ts: pd.Timestamp | None
    exit_ts: pd.Timestamp
    side: int
    entry: float
    initial_stop: float
    target: float
    tp1: float
    partial_exit: float | None
    final_exit: float
    reason: str

    @property
    def stop_risk_per_micro(self) -> float:
        """Worst modeled initial stop loss, including commission."""
        return (abs(self.entry - self.initial_stop) + TICK) * POINT_VALUE + COMMISSION_RT

    @property
    def runner_risk_per_micro(self) -> float:
        """Reserved risk after +1R arms the breakeven stop."""
        return TICK * POINT_VALUE + COMMISSION_RT

    @property
    def partial_gross_per_micro(self) -> float:
        if self.partial_exit is None:
            return 0.0
        return self.side * (self.partial_exit - self.entry) * POINT_VALUE

    @property
    def final_gross_per_micro(self) -> float:
        return self.side * (self.final_exit - self.entry) * POINT_VALUE

    def split_qty(self, qty: int) -> tuple[int, int]:
        # One MNQ cannot be split.  For odd quantities, close the smaller integer
        # half and leave the larger integer half as the runner.
        partial = qty // 2 if self.partial_exit is not None else 0
        return partial, qty - partial

    def pnl(self, qty: int) -> float:
        partial, runner = self.split_qty(qty)
        return (
            partial * (self.partial_gross_per_micro - COMMISSION_RT)
            + runner * (self.final_gross_per_micro - COMMISSION_RT)
        )


@dataclass
class OpenPosition:
    trade: CausalTrade
    initial_qty: int
    remaining_qty: int
    partial_done: bool = False

    @property
    def reserved_loss(self) -> float:
        risk = (
            self.trade.runner_risk_per_micro
            if self.partial_done
            else self.trade.stop_risk_per_micro
        )
        return self.remaining_qty * risk


def bars15(day: L.Day) -> list[Bar15]:
    out = []
    for end_i in L._sample_indices(day, 15, start=14):
        end_i = int(end_i)
        end_minute = int(day.minute[end_i])
        start_minute = end_minute - 14
        start_i = int(np.searchsorted(day.minute, start_minute))
        expected = np.arange(start_minute, end_minute + 1)
        if (
            start_i < 0
            or end_i - start_i + 1 != 15
            or not np.array_equal(day.minute[start_i:end_i + 1], expected)
        ):
            continue
        vol = float(np.sum(day.vol[start_i:end_i + 1]))
        out.append(
            Bar15(
                start_i=start_i,
                end_i=end_i,
                op=float(day.op[start_i]),
                hi=float(np.max(day.hi[start_i:end_i + 1])),
                lo=float(np.min(day.lo[start_i:end_i + 1])),
                cl=float(day.cl[end_i]),
                vol=vol if vol > 0 else 1.0,
            )
        )
    return out


def _exit_price(raw: float, side: int) -> float:
    return float(raw) - side * TICK


def _make_trade(
    day: L.Day,
    signal_i: int,
    strategy: str,
    side: int,
    stop: float,
    target: float,
) -> CausalTrade | None:
    """Enter only after the signal candle, then replay on one-minute bars."""
    fi = signal_i + 1
    if fi >= len(day.cl) or int(day.minute[fi]) >= FLAT_MINUTE:
        return None
    entry = float(day.op[fi]) + side * TICK
    stop = float(stop)
    target = float(target)
    if (side > 0 and (stop >= entry or target <= entry)) or (
        side < 0 and (stop <= entry or target >= entry)
    ):
        # The level was skipped by the next open.  Do not invent a fill at it,
        # and do not select a later signal with hindsight.
        return None
    risk_points = abs(entry - stop)
    if risk_points < TICK:
        return None
    tp1 = entry + side * risk_points
    current_stop = stop
    partial_ts = None
    partial_exit = None
    exit_i = len(day.cl) - 1
    final_exit = _exit_price(float(day.cl[-1]), side)
    reason = "eod"

    for j in range(fi, len(day.cl)):
        if int(day.minute[j]) >= FLAT_MINUTE:
            exit_i = j
            final_exit = _exit_price(float(day.op[j]), side)
            reason = "eod"
            break
        stopped = day.lo[j] <= current_stop if side > 0 else day.hi[j] >= current_stop
        targeted = day.hi[j] >= target if side > 0 else day.lo[j] <= target
        if stopped:
            raw = (
                min(current_stop, float(day.op[j]))
                if side > 0
                else max(current_stop, float(day.op[j]))
            )
            exit_i = j
            final_exit = _exit_price(raw, side)
            reason = "be" if partial_ts is not None else "stop"
            break
        if targeted:
            exit_i = j
            final_exit = _exit_price(target, side)
            reason = "target"
            break
        if partial_ts is None:
            hit_tp1 = day.hi[j] >= tp1 if side > 0 else day.lo[j] <= tp1
            if hit_tp1:
                partial_ts = pd.Timestamp(day.ts[j])
                partial_exit = _exit_price(tp1, side)
                current_stop = entry
                # The breakeven stop starts on the next one-minute candle, which
                # avoids manufacturing high/low ordering inside this candle.
                continue

    return CausalTrade(
        strategy=strategy,
        day=day.day,
        entry_ts=pd.Timestamp(day.ts[fi]),
        partial_ts=partial_ts,
        exit_ts=pd.Timestamp(day.ts[exit_i]),
        side=side,
        entry=entry,
        initial_stop=stop,
        target=target,
        tp1=tp1,
        partial_exit=partial_exit,
        final_exit=final_exit,
        reason=reason,
    )


def vwap_trade(day: L.Day) -> CausalTrade | None:
    """Literal 2-sigma setup, made causal by entering after the signal candle."""
    cv = cp = cp2 = 0.0
    for number, bar in enumerate(bars15(day)):
        tp = (bar.hi + bar.lo + bar.cl) / 3.0
        cv += bar.vol
        cp += tp * bar.vol
        cp2 += tp * tp * bar.vol
        if number < 15:
            continue
        vwap = cp / cv
        sigma = math.sqrt(max(cp2 / cv - vwap * vwap, 0.0))
        if sigma <= 0:
            continue
        upper = vwap + 2.0 * sigma
        lower = vwap - 2.0 * sigma
        if bar.hi >= upper:
            return _make_trade(
                day, bar.end_i, "VWAP2s", -1, vwap + 3.0 * sigma, vwap
            )
        if bar.lo <= lower:
            return _make_trade(
                day, bar.end_i, "VWAP2s", 1, vwap - 3.0 * sigma, vwap
            )
    return None


def turtle_trade(day: L.Day, prior: list[L.Day]) -> CausalTrade | None:
    """Literal 20-session false break/recovery, entered after recovery is known."""
    if len(prior) < 24:
        return None
    look = prior[-20:]
    prior_low = min(float(np.min(x.lo)) for x in look)
    prior_high = max(float(np.max(x.hi)) for x in look)
    sweep_side = 0
    recovery = stop = target = 0.0
    for bar in bars15(day):
        if sweep_side == 0:
            if bar.lo < prior_low:
                sweep_side = 1
                recovery = prior_low + 8 * TICK
                stop = float(np.min(day.lo[:bar.end_i + 1])) - TICK
            elif bar.hi > prior_high:
                sweep_side = -1
                recovery = prior_high - 8 * TICK
                stop = float(np.max(day.hi[:bar.end_i + 1])) + TICK
            else:
                continue
            modeled_risk = abs(recovery - stop)
            if modeled_risk <= 0:
                return None
            target = recovery + sweep_side * 2.0 * modeled_risk
        recovered = bar.hi >= recovery if sweep_side > 0 else bar.lo <= recovery
        if recovered:
            return _make_trade(
                day, bar.end_i, "TurtleSoup", sweep_side, stop, target
            )
    return None


def eighty_twenty_trade(day: L.Day, prior: list[L.Day]) -> CausalTrade | None:
    """Literal prior-session 80/20 push/recovery, filled after recovery closes."""
    if not prior:
        return None
    yesterday = prior[-1]
    ph = float(np.max(yesterday.hi))
    pl = float(np.min(yesterday.lo))
    po = float(yesterday.op[0])
    pc = float(yesterday.cl[-1])
    pr = ph - pl
    if pr <= 0:
        return None
    if po >= ph - 0.2 * pr and pc <= pl + 0.2 * pr:
        side, level = 1, pl
    elif po <= pl + 0.2 * pr and pc >= ph - 0.2 * pr:
        side, level = -1, ph
    else:
        return None
    trigger = level - side * 10 * TICK
    pushed = False
    for bar in bars15(day):
        if not pushed and (
            (side > 0 and bar.lo <= trigger)
            or (side < 0 and bar.hi >= trigger)
        ):
            pushed = True
        if not pushed:
            continue
        recovered = bar.hi >= level if side > 0 else bar.lo <= level
        if recovered:
            stop = (
                float(np.min(day.lo[:bar.end_i + 1])) - TICK
                if side > 0
                else float(np.max(day.hi[:bar.end_i + 1])) + TICK
            )
            modeled_risk = abs(level - stop)
            if modeled_risk <= 0:
                return None
            target = level + side * 2.0 * modeled_risk
            return _make_trade(
                day, bar.end_i, "80-20", side, stop, target
            )
    return None


def generate(days: list[L.Day]) -> list[CausalTrade]:
    trades = []
    prior = []
    for day in days:
        candidates = (
            vwap_trade(day),
            turtle_trade(day, prior),
            eighty_twenty_trade(day, prior),
        )
        trades.extend(t for t in candidates if t is not None)
        prior.append(day)
    return sorted(trades, key=lambda t: (t.entry_ts, t.strategy, t.exit_ts))


def entry_qty(trade: CausalTrade, risk: float = RISK_USD) -> int:
    return min(MAX_MICROS, int(math.floor(risk / trade.stop_risk_per_micro)))


def basic_stats(trades: list[CausalTrade], risk: float = RISK_USD) -> dict:
    rows = [(t, entry_qty(t, risk)) for t in trades]
    pnl = np.array([t.pnl(q) for t, q in rows if q > 0], dtype=float)
    if not len(pnl):
        return {"n": 0, "net": 0.0, "pf": 0.0, "win": 0.0, "maxdd": 0.0}
    gp = float(pnl[pnl > 0].sum())
    gl = float(-pnl[pnl <= 0].sum())
    curve = np.cumsum(pnl)
    peaks = np.maximum.accumulate(np.r_[0.0, curve])[:-1]
    return {
        "n": len(pnl),
        "net": float(pnl.sum()),
        "pf": gp / gl if gl else math.inf,
        "win": float(np.mean(pnl > 0)),
        "maxdd": float(np.min(curve - peaks)),
    }


def _priority(trade: CausalTrade) -> int:
    return {"VWAP2s": 0, "TurtleSoup": 1, "80-20": 2}[trade.strategy]


def simulate_window(
    trades_by_day: dict[date, list[CausalTrade]],
    sessions: list[date],
    risk: float = RISK_USD,
) -> tuple[str, int]:
    balance = 0.0
    eod_peak = 0.0
    floor = -MAX_LOSS
    for used, session in enumerate(sessions, 1):
        positions: list[OpenPosition] = []
        day_pnl = 0.0
        dll_locked = False
        day_trades = sorted(
            trades_by_day.get(session, []),
            key=lambda t: (t.entry_ts, _priority(t), t.exit_ts),
        )
        event_times = sorted(
            {t.entry_ts for t in day_trades}
            | {t.exit_ts for t in day_trades}
            | {t.partial_ts for t in day_trades if t.partial_ts is not None}
        )
        for ts in event_times:
            # Existing positions are reduced/closed before allocating contracts
            # to entries stamped at the same minute.
            for pos in list(positions):
                trade = pos.trade
                partial_qty, _ = trade.split_qty(pos.initial_qty)
                if (
                    not pos.partial_done
                    and partial_qty > 0
                    and trade.partial_ts == ts
                    and ts >= trade.entry_ts
                ):
                    pnl = partial_qty * (
                        trade.partial_gross_per_micro - COMMISSION_RT
                    )
                    balance += pnl
                    day_pnl += pnl
                    pos.remaining_qty -= partial_qty
                    pos.partial_done = True
                    if balance >= TARGET_PROFIT:
                        return "pass", used
                if trade.exit_ts == ts and ts >= trade.entry_ts:
                    pnl = pos.remaining_qty * (
                        trade.final_gross_per_micro - COMMISSION_RT
                    )
                    balance += pnl
                    day_pnl += pnl
                    positions.remove(pos)
                    if balance <= floor:
                        return "fail", used
                    if balance >= TARGET_PROFIT:
                        return "pass", used
                if day_pnl <= -DAILY_LOSS_LIMIT:
                    dll_locked = True

            for trade in (t for t in day_trades if t.entry_ts == ts):
                if dll_locked:
                    continue
                per_micro = trade.stop_risk_per_micro
                requested = int(math.floor(risk / per_micro))
                cap_room = MAX_MICROS - sum(p.remaining_qty for p in positions)
                # The named strategy always requests its flat $600 allocation.  It
                # has no account-floor-aware sizing rule, so adding one here would
                # silently rescue losing windows and cease to be a literal test.
                qty = max(0, min(requested, cap_room))
                if qty > 0:
                    positions.append(OpenPosition(trade, qty, qty))

            # A trade may hit +1R or exit during its entry minute.
            for pos in positions:
                trade = pos.trade
                partial_qty, _ = trade.split_qty(pos.initial_qty)
                if (
                    not pos.partial_done
                    and partial_qty > 0
                    and trade.entry_ts == ts
                    and trade.partial_ts == ts
                ):
                    pnl = partial_qty * (
                        trade.partial_gross_per_micro - COMMISSION_RT
                    )
                    balance += pnl
                    day_pnl += pnl
                    pos.remaining_qty -= partial_qty
                    pos.partial_done = True
                    if balance >= TARGET_PROFIT:
                        return "pass", used
            for pos in list(positions):
                if pos.trade.entry_ts == ts and pos.trade.exit_ts == ts:
                    pnl = pos.remaining_qty * (
                        pos.trade.final_gross_per_micro - COMMISSION_RT
                    )
                    balance += pnl
                    day_pnl += pnl
                    positions.remove(pos)
                    if balance <= floor:
                        return "fail", used
                    if balance >= TARGET_PROFIT:
                        return "pass", used
        if positions:
            raise AssertionError("Every trade must be flat intraday")
        eod_peak = max(eod_peak, balance)
        floor = LOCKED_FLOOR if eod_peak > LOCK_TRIGGER else eod_peak - MAX_LOSS
    return "undecided", len(sessions)


def evaluate(
    trades: list[CausalTrade],
    all_sessions: list[date],
    horizon: int = 30,
    risk: float = RISK_USD,
) -> dict:
    by_day: dict[date, list[CausalTrade]] = defaultdict(list)
    for trade in trades:
        by_day[trade.day].append(trade)
    outcomes = []
    used = []
    for i in range(max(0, len(all_sessions) - horizon + 1)):
        outcome, days = simulate_window(
            by_day, all_sessions[i:i + horizon], risk=risk
        )
        outcomes.append(outcome)
        used.append(days)
    pass_days = [d for o, d in zip(outcomes, used) if o == "pass"]
    restricted = [d if o == "pass" else horizon for o, d in zip(outcomes, used)]
    n = len(outcomes)
    return {
        "starts": n,
        "passes": outcomes.count("pass"),
        "fails": outcomes.count("fail"),
        "undecided": outcomes.count("undecided"),
        "pass_rate": outcomes.count("pass") / n if n else 0.0,
        "fail_rate": outcomes.count("fail") / n if n else 0.0,
        "median_pass_days": median(pass_days) if pass_days else None,
        "mean_pass_days": mean(pass_days) if pass_days else None,
        "restricted_mean_days": mean(restricted) if restricted else None,
    }


def reproduce_legacy() -> dict:
    """Run the exact legacy three-year functions behind the dashboard claim."""
    from apex_lib import load_fut
    from apex_strats2 import eighty_twenty, turtle_soup, vwap_fade
    from bt_ict_sm_tf import resample
    from monthly_apex_combo_test import as_dollar, eval_months, merge

    d15 = resample(load_fut("nq"), "15min")
    parts = {}
    for name, fn in (
        ("VWAP2s", vwap_fade),
        ("TurtleSoup", turtle_soup),
        ("80-20", eighty_twenty),
    ):
        parts[name] = fn(d15, "nq", manage="partial")[0]
    bundle = merge(*parts.values())
    dollar = as_dollar(bundle, "base_15m_nqmr_600", RISK_USD)
    pnl = np.array([x["_usd"] for x in dollar], dtype=float)
    gp = float(pnl[pnl > 0].sum())
    gl = float(-pnl[pnl <= 0].sum())
    months = eval_months(dollar)
    local = d15.index.tz_convert(L.NY)
    return {
        "start": str(d15.index.min()),
        "end": str(d15.index.max()),
        "session_clock_min": str(min(local.time)),
        "session_clock_max": str(max(local.time)),
        "trades": len(bundle),
        "net": float(pnl.sum()),
        "win": float(np.mean(pnl > 0)),
        "pf": gp / gl if gl else math.inf,
        "monthly_starts": len(months),
        "monthly_passes": sum(x["passed"] for x in months),
        "monthly_breaches": sum(x["breached"] for x in months),
    }


def _slice(
    trades: list[CausalTrade],
    sessions: list[date],
    lo: date | None,
    hi: date | None,
) -> tuple[list[CausalTrade], list[date]]:
    keep = lambda d: (lo is None or d >= lo) and (hi is None or d <= hi)
    return [t for t in trades if keep(t.day)], [d for d in sessions if keep(d)]


def _fmt_stats(stats: dict) -> str:
    return (
        f"n={stats['n']} net=${stats['net']:,.0f} PF={stats['pf']:.3f} "
        f"win={stats['win']:.1%} maxDD=${stats['maxdd']:,.0f}"
    )


def _fmt_eval(result: dict) -> str:
    med = "-" if result["median_pass_days"] is None else f"{result['median_pass_days']:g}"
    return (
        f"pass={result['passes']}/{result['starts']} ({result['pass_rate']:.1%}) "
        f"fail={result['fails']} undecided={result['undecided']} "
        f"median-pass={med} restricted-mean={result['restricted_mean_days']:.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=RISK_USD)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--skip-legacy", action="store_true")
    args = ap.parse_args()

    if not args.skip_legacy:
        print("LEGACY REPRODUCTION (biased/original engine)")
        print(reproduce_legacy())

    days = L.load_days("nq")
    trades = generate(days)
    sessions = [d.day for d in days]
    gap_days = sum(
        bool(np.any(np.diff(day.minute.astype(int)) != 1)) for day in days
    )
    print(
        f"\nCAUSAL DATA {sessions[0]}..{sessions[-1]} sessions={len(sessions)} "
        f"accepted_sessions_with_1m_gaps={gap_days}"
    )
    print(
        "SOURCE WARNING: cached NQ history is Dukascopy USATECHIDXUSD proxy data, "
        "not CME NQ/MNQ trades and volume."
    )
    for strategy in ("VWAP2s", "TurtleSoup", "80-20"):
        subset = [t for t in trades if t.strategy == strategy]
        print(f"{strategy:12s} {_fmt_stats(basic_stats(subset, args.risk))}")

    periods = (
        ("all", None, None),
        ("train", None, TRAIN_END),
        ("validation", date(2022, 1, 1), VALID_END),
        ("test", date(2024, 1, 1), None),
    )
    print("\nCAUSAL BUNDLE")
    for label, lo, hi in periods:
        period_trades, period_sessions = _slice(trades, sessions, lo, hi)
        stats = basic_stats(period_trades, args.risk)
        result = evaluate(
            period_trades,
            period_sessions,
            horizon=args.horizon,
            risk=args.risk,
        )
        print(f"{label:10s} {_fmt_stats(stats)}")
        print(f"{'':10s} {args.horizon}-session LucidPro: {_fmt_eval(result)}")

    yearly = {}
    for year in sorted({d.year for d in sessions}):
        ytrades, _ = _slice(
            trades, sessions, date(year, 1, 1), date(year, 12, 31)
        )
        yearly[year] = basic_stats(ytrades, args.risk)["net"]
    print("\nYEARLY NET")
    print(" ".join(f"{year}:{pnl:+,.0f}" for year, pnl in yearly.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
