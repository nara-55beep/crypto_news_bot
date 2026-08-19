"""Event-driven Reference Ladder backtester.

The engine is intentionally separate from ``strategy_lab.engine``. A target
position series cannot express a notional reference signal followed by several
independently filled entries, nor can that generic engine represent free margin
and liquidation. This module keeps those semantics explicit and testable.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import LadderConfig
from .signals import BollingerRsiSmaSignal, ReferenceSignal


@dataclass
class LadderEntry:
    level: int
    time: str
    raw_price: float
    fill_price: float
    qty_btc: float
    commission: float
    required_margin: float


@dataclass
class LadderResult:
    ok: bool
    config: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    cycles: list[dict[str, Any]] = field(default_factory=list)
    curve: list[dict[str, Any]] = field(default_factory=list)
    distributions: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LadderBacktester:
    def __init__(self, config: LadderConfig | None = None,
                 signal: ReferenceSignal | None = None) -> None:
        self.config = (config or LadderConfig()).validate()
        self.signal = signal or BollingerRsiSmaSignal()

    def _edge(self, qty: float) -> float:
        c = self.config
        return (
            c.spread_round_turn_usd / 2.0
            + c.base_slippage_usd
            + c.slippage_usd_per_btc * abs(qty) ** c.slippage_power
        )

    def _fill(self, raw_price: float, side: str, qty: float) -> tuple[float, float]:
        edge = self._edge(qty)
        fill = raw_price + edge if side == "buy" else raw_price - edge
        return fill, abs(fill - raw_price) * abs(qty)

    @staticmethod
    def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.attrs.get("reference_ladder_normalized"):
            return frame
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"market data is missing {sorted(missing)}")
        if len(frame) < 2:
            raise ValueError("at least two one-minute bars are required")
        out = frame[list(sorted(required))].copy()
        out = out.rename(columns={column: column.lower() for column in out.columns})
        out = out[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce",
        ).dropna(subset=["open", "high", "low", "close"])
        out["volume"] = out["volume"].fillna(0.0)
        if not isinstance(out.index, pd.DatetimeIndex):
            raise ValueError("market data must use a DatetimeIndex")
        if out.index.tz is None:
            out.index = out.index.tz_localize("UTC")
        else:
            out.index = out.index.tz_convert("UTC")
        out = out[~out.index.duplicated(keep="last")].sort_index()
        valid = (
            (out["low"] <= out["high"])
            & out["open"].between(out["low"], out["high"])
            & out["close"].between(out["low"], out["high"])
            & (out[["open", "high", "low", "close"]] > 0).all(axis=1)
        )
        dropped = int((~valid).sum())
        out = out[valid]
        out.attrs.update(frame.attrs)
        out.attrs["dropped_invalid_bars"] = dropped
        out.attrs["reference_ladder_normalized"] = True
        return out

    def run(self, frame: pd.DataFrame, *, signal_override: pd.Series | None = None,
            start_trading_at: str | pd.Timestamp | None = None) -> LadderResult:
        try:
            return self._run(frame, signal_override=signal_override,
                             start_trading_at=start_trading_at)
        except Exception as exc:
            return LadderResult(
                ok=False, config=self.config.to_dict(),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _run(self, frame: pd.DataFrame, *, signal_override: pd.Series | None,
             start_trading_at: str | pd.Timestamp | None) -> LadderResult:
        c = self.config
        data = self._normalise_frame(frame)
        if signal_override is None:
            generated = self.signal.generate(data, c)
        else:
            generated = signal_override.reindex(data.index).fillna(0)
        signals = generated.clip(-1, 1).astype(np.int8).to_numpy()

        opens = data["open"].to_numpy(dtype=float)
        highs = data["high"].to_numpy(dtype=float)
        lows = data["low"].to_numpy(dtype=float)
        closes = data["close"].to_numpy(dtype=float)
        index = data.index
        if c.distance_mode == "atr":
            true_range = pd.concat([
                data["high"] - data["low"],
                (data["high"] - data["close"].shift()).abs(),
                (data["low"] - data["close"].shift()).abs(),
            ], axis=1).max(axis=1)
            atr = true_range.rolling(14, min_periods=14).mean().to_numpy(dtype=float)
        else:
            atr = np.full(len(data), np.nan)

        start_gate = pd.Timestamp(start_trading_at) if start_trading_at is not None else None
        if start_gate is not None:
            start_gate = start_gate.tz_localize("UTC") if start_gate.tz is None else start_gate.tz_convert("UTC")
        hours = index.hour.to_numpy()
        weekdays = index.weekday.to_numpy()
        trading_allowed = (
            (hours >= c.trading_start_hour_utc)
            & (hours < c.trading_end_hour_utc)
            & ((weekdays < 5) | c.weekends)
        )
        if start_gate is not None:
            trading_allowed &= (index >= start_gate)

        balance = float(c.starting_capital)
        balance_peak = balance
        equity_peak = balance
        max_balance_dd_usd = max_balance_dd_pct = 0.0
        max_equity_dd_usd = max_equity_dd_pct = 0.0
        peak_exposure_btc = peak_exposure_usd = peak_exposure_vs_equity = 0.0
        active: dict[str, Any] | None = None
        cycles: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        daily_equity: list[float] = []
        normalised_days = index.normalize().asi8
        day_end = np.r_[normalised_days[1:] != normalised_days[:-1], True]

        def iso(bar: int) -> str:
            return index[bar].isoformat()

        def position_values(cycle: dict[str, Any], mark: float) -> tuple[float, float, float]:
            entries: list[LadderEntry] = cycle["entries"]
            quantity = sum(entry.qty_btc for entry in entries)
            if quantity <= 0:
                return 0.0, 0.0, 0.0
            average = sum(entry.fill_price * entry.qty_btc for entry in entries) / quantity
            unrealized = cycle["direction"] * quantity * (mark - average)
            return quantity, average, unrealized

        def mark_equity(cycle: dict[str, Any] | None, mark: float) -> tuple[float, float]:
            if cycle is None:
                return max(0.0, balance), 0.0
            _, _, floating = position_values(cycle, mark)
            return max(0.0, balance + floating), floating

        def update_adverse(cycle: dict[str, Any], adverse: float) -> tuple[float, float, float, float]:
            quantity, average, floating = position_values(cycle, adverse)
            adverse_equity = max(0.0, balance + floating)
            loss = adverse_equity - cycle["starting_balance"]
            cycle["deepest_floating_loss_usd"] = min(cycle["deepest_floating_loss_usd"], loss)
            move = cycle["direction"] * (cycle["reference_entry_price"] - adverse)
            cycle["max_adverse_excursion_points"] = max(
                cycle["max_adverse_excursion_points"], move,
            )
            return quantity, average, floating, adverse_equity

        def is_liquidated(cycle: dict[str, Any], mark: float) -> bool:
            quantity, _, floating = position_values(cycle, mark)
            if quantity <= 0:
                return False
            equity = balance + floating
            notional = quantity * mark
            maintenance = notional * c.maintenance_margin_rate
            used_margin = notional / c.leverage
            return equity <= maintenance or equity < used_margin

        def close_cycle(cycle: dict[str, Any], bar: int, raw_price: float,
                        reason: str, *, liquidated: bool = False) -> None:
            nonlocal balance
            quantity, average, _ = position_values(cycle, raw_price)
            exit_fill = raw_price
            gross = 0.0
            exit_commission = 0.0
            if quantity > 0:
                side = "sell" if cycle["direction"] > 0 else "buy"
                exit_fill, edge_cost = self._fill(raw_price, side, quantity)
                gross = cycle["direction"] * quantity * (exit_fill - average)
                exit_commission = abs(exit_fill * quantity) * c.commission_rate
                cycle["execution_cost"] += edge_cost + exit_commission
                balance = max(0.0, balance + gross - exit_commission)
            ending_balance = balance
            net = ending_balance - cycle["starting_balance"]
            duration_hours = (index[bar] - index[cycle["start_bar"]]).total_seconds() / 3600.0
            record = {
                "id": cycle["id"],
                "signal_time": cycle["signal_time"],
                "close_time": iso(bar),
                "direction": "long" if cycle["direction"] > 0 else "short",
                "reference_entry_price": round(cycle["reference_entry_price"], 4),
                "average_entry_price": round(average, 4) if quantity else None,
                "exit_price": round(exit_fill, 4) if quantity else None,
                "levels_reached": len(cycle["entries"]),
                "entries": [asdict(entry) for entry in cycle["entries"]],
                "peak_qty_btc": round(cycle["peak_qty_btc"], 8),
                "peak_exposure_usd": round(cycle["peak_exposure_usd"], 2),
                "gross_pnl": round(gross, 2),
                "net_pnl": round(net, 2),
                "return_pct": round(net / cycle["starting_balance"] * 100.0, 4)
                if cycle["starting_balance"] else 0.0,
                "deepest_floating_loss_usd": round(cycle["deepest_floating_loss_usd"], 2),
                "deepest_floating_loss_pct": round(
                    cycle["deepest_floating_loss_usd"] / cycle["starting_balance"] * 100.0, 4,
                ) if cycle["starting_balance"] else 0.0,
                "max_adverse_excursion_points": round(cycle["max_adverse_excursion_points"], 2),
                "duration_hours": round(duration_hours, 3),
                "recovered": reason in {"reference_exit", "profit_target"},
                "liquidated": bool(liquidated),
                "reason": reason,
                "funding_cost": round(cycle["funding_cost"], 2),
                "execution_cost": round(cycle["execution_cost"], 2),
                "starting_balance": round(cycle["starting_balance"], 2),
                "ending_balance": round(ending_balance, 2),
            }
            cycles.append(record)

        def crossed(direction: int, adverse: float, level: float) -> bool:
            return adverse <= level if direction > 0 else adverse >= level

        for bar in range(len(data)):
            if active is not None:
                quantity, _, _ = position_values(active, closes[bar])
                if quantity > 0:
                    funding = quantity * closes[bar] * c.funding_rate_8h / 480.0
                    balance = max(0.0, balance - funding)
                    active["funding_cost"] += funding

                adverse = lows[bar] if active["direction"] > 0 else highs[bar]
                _, _, _, adverse_equity = update_adverse(active, adverse)
                if is_liquidated(active, adverse):
                    close_cycle(active, bar, adverse, "liquidation", liquidated=True)
                    active = None
                elif (
                    c.max_loss_per_cycle_pct is not None
                    and adverse_equity - active["starting_balance"]
                    <= -active["starting_balance"] * c.max_loss_per_cycle_pct
                ):
                    close_cycle(active, bar, adverse, "max_cycle_loss")
                    active = None

                while active is not None and active["next_level"] < c.max_levels:
                    level_number = active["next_level"]
                    level_price = active["levels"][level_number]
                    if not crossed(active["direction"], adverse, level_price):
                        break
                    quantity_to_add = active["sizes"][level_number]
                    # A gap fills at the opening price. Otherwise the trigger
                    # level is the first observable crossing price.
                    raw_fill = (
                        min(opens[bar], level_price) if active["direction"] > 0
                        else max(opens[bar], level_price)
                    )
                    side = "buy" if active["direction"] > 0 else "sell"
                    fill, edge_cost = self._fill(raw_fill, side, quantity_to_add)
                    existing_qty, _, existing_floating = position_values(active, adverse)
                    current_equity = max(0.0, balance + existing_floating)
                    used_margin = existing_qty * adverse / c.leverage
                    free_margin = max(0.0, current_equity - used_margin)
                    required_margin = abs(fill * quantity_to_add) / c.leverage
                    if required_margin > free_margin:
                        close_cycle(active, bar, adverse, "margin_liquidation", liquidated=True)
                        active = None
                        break
                    commission = abs(fill * quantity_to_add) * c.commission_rate
                    balance = max(0.0, balance - commission)
                    entry = LadderEntry(
                        level=level_number + 1, time=iso(bar), raw_price=round(raw_fill, 6),
                        fill_price=round(fill, 6), qty_btc=round(quantity_to_add, 8),
                        commission=round(commission, 6), required_margin=round(required_margin, 6),
                    )
                    active["entries"].append(entry)
                    active["next_level"] += 1
                    active["execution_cost"] += edge_cost + commission
                    total_qty, _, _ = position_values(active, adverse)
                    exposure_usd = total_qty * adverse
                    active["peak_qty_btc"] = max(active["peak_qty_btc"], total_qty)
                    active["peak_exposure_usd"] = max(active["peak_exposure_usd"], exposure_usd)
                    peak_exposure_btc = max(peak_exposure_btc, total_qty)
                    peak_exposure_usd = max(peak_exposure_usd, exposure_usd)
                    ratio = exposure_usd / max(current_equity, 1e-9)
                    peak_exposure_vs_equity = max(peak_exposure_vs_equity, ratio)
                    update_adverse(active, adverse)
                    if is_liquidated(active, adverse):
                        close_cycle(active, bar, adverse, "liquidation_after_add", liquidated=True)
                        active = None
                        break
                    _, _, _, adverse_equity = update_adverse(active, adverse)
                    if (
                        c.max_loss_per_cycle_pct is not None
                        and adverse_equity - active["starting_balance"]
                        <= -active["starting_balance"] * c.max_loss_per_cycle_pct
                    ):
                        close_cycle(active, bar, adverse, "max_cycle_loss")
                        active = None
                        break

                if active is not None:
                    quantity, average, _ = position_values(active, closes[bar])
                    if quantity > 0:
                        reference = active["reference_entry_price"]
                        candidates = [reference]
                        if c.profit_target_points is not None:
                            candidates.append(
                                average + active["direction"] * c.profit_target_points,
                            )
                        # When both favorable prices trade in one OHLC bar, use
                        # the less profitable one instead of assuming a best path.
                        exit_target = min(candidates) if active["direction"] > 0 else max(candidates)
                        target_hit = (
                            highs[bar] >= exit_target if active["direction"] > 0
                            else lows[bar] <= exit_target
                        )
                        if target_hit:
                            reason = "reference_exit" if exit_target == reference else "profit_target"
                            close_cycle(active, bar, exit_target, reason)
                            active = None

                if active is not None and c.max_cycle_duration_hours is not None:
                    duration = (index[bar] - index[active["start_bar"]]).total_seconds() / 3600.0
                    if duration >= c.max_cycle_duration_hours:
                        close_cycle(active, bar, closes[bar], "max_duration")
                        active = None

            if active is None and balance > 0 and trading_allowed[bar] and signals[bar] != 0:
                direction = int(signals[bar])
                reference = closes[bar]
                trigger = c.trigger_distance
                step = c.ladder_step
                if c.distance_mode == "atr" and np.isfinite(atr[bar]) and atr[bar] > 0:
                    trigger = atr[bar] * c.trigger_atr_multiple
                    step = atr[bar] * c.step_atr_multiple
                levels = [
                    reference - direction * (trigger + level * step)
                    for level in range(c.max_levels)
                ]
                active = {
                    "id": uuid.uuid4().hex[:10], "signal_time": iso(bar),
                    "start_bar": bar, "reference_entry_price": reference,
                    "direction": direction, "levels": levels,
                    "sizes": c.sizes_for_equity(balance), "next_level": 0,
                    "entries": [], "starting_balance": balance,
                    "deepest_floating_loss_usd": 0.0,
                    "max_adverse_excursion_points": 0.0,
                    "funding_cost": 0.0, "execution_cost": 0.0,
                    "peak_qty_btc": 0.0, "peak_exposure_usd": 0.0,
                }

            equity, floating = mark_equity(active, closes[bar])
            balance_peak = max(balance_peak, balance)
            equity_peak = max(equity_peak, equity)
            balance_dd = balance_peak - balance
            equity_dd = equity_peak - equity
            max_balance_dd_usd = max(max_balance_dd_usd, balance_dd)
            max_balance_dd_pct = max(max_balance_dd_pct, balance_dd / max(balance_peak, 1e-9) * 100.0)
            max_equity_dd_usd = max(max_equity_dd_usd, equity_dd)
            max_equity_dd_pct = max(max_equity_dd_pct, equity_dd / max(equity_peak, 1e-9) * 100.0)
            if day_end[bar]:
                daily_equity.append(equity)
            if bar % c.curve_every_bars == 0 or bar == len(data) - 1:
                curve.append({
                    "time": iso(bar), "balance": round(balance, 2),
                    "equity": round(equity, 2), "floating_pnl": round(floating, 2),
                })

        if active is not None:
            reason = "end_of_data" if active["entries"] else "no_fill_end_of_data"
            close_cycle(active, len(data) - 1, closes[-1], reason)
            active = None
            curve[-1].update({"balance": round(balance, 2), "equity": round(balance, 2),
                              "floating_pnl": 0.0})

        metrics = self._metrics(
            data, cycles, balance, daily_equity,
            max_balance_dd_usd=max_balance_dd_usd,
            max_balance_dd_pct=max_balance_dd_pct,
            max_equity_dd_usd=max_equity_dd_usd,
            max_equity_dd_pct=max_equity_dd_pct,
            peak_exposure_btc=peak_exposure_btc,
            peak_exposure_usd=peak_exposure_usd,
            peak_exposure_vs_equity=peak_exposure_vs_equity,
        )
        entered = [cycle for cycle in cycles if cycle["levels_reached"] > 0]
        warnings: list[str] = []
        if not entered:
            warnings.append("No ladder entry was filled during this window.")
        if len(entered) < 30:
            warnings.append("Fewer than 30 entered cycles; treat summary statistics as a small sample.")
        if c.max_loss_per_cycle_pct is None:
            warnings.append("No cycle stop is enabled; liquidation is the ultimate loss control.")
        provenance = {
            "symbol": c.symbol, "timeframe": c.timeframe,
            "first_bar": iso(0), "last_bar": iso(len(data) - 1), "bars": len(data),
            "signal": getattr(self.signal, "name", type(self.signal).__name__),
            "data_source": data.attrs.get("data_source", "caller-supplied OHLCV"),
            "data_quality": data.attrs.get("data_quality", {}),
            "execution_order": "adverse extreme -> liquidation/loss -> ladder adds -> favorable exit",
        }
        return LadderResult(
            ok=True, config=c.to_dict(), metrics=metrics, cycles=cycles,
            curve=curve,
            distributions={
                "duration_hours": [cycle["duration_hours"] for cycle in entered],
                "return_pct": [cycle["return_pct"] for cycle in entered],
            },
            provenance=provenance, warnings=warnings,
        )

    def _metrics(self, frame: pd.DataFrame, cycles: list[dict[str, Any]],
                 final_balance: float, daily_equity: list[float], **risk: float) -> dict[str, Any]:
        c = self.config
        entered = [cycle for cycle in cycles if cycle["levels_reached"] > 0]
        pnls = np.array([cycle["net_pnl"] for cycle in entered], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        gross_profit = float(wins.sum()) if len(wins) else 0.0
        gross_loss = float(-losses.sum()) if len(losses) else 0.0
        span_years = max((frame.index[-1] - frame.index[0]).total_seconds() / (365.25 * 86_400), 0.0)
        total_return = final_balance / c.starting_capital - 1.0
        cagr = None
        if span_years > 0 and final_balance > 0:
            cagr = (final_balance / c.starting_capital) ** (1.0 / span_years) - 1.0
        daily = pd.Series(daily_equity, dtype=float)
        daily_returns = daily.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        sharpe = None
        if len(daily_returns) > 1 and float(daily_returns.std(ddof=0)) > 0:
            sharpe = float(daily_returns.mean() / daily_returns.std(ddof=0) * math.sqrt(365.0))
        level_counts = {
            str(level): sum(cycle["levels_reached"] >= level for cycle in entered)
            for level in range(1, c.max_levels + 1)
        }
        worst = min(entered, key=lambda cycle: cycle["deepest_floating_loss_usd"], default=None)
        durations = np.array([cycle["duration_hours"] for cycle in entered], dtype=float)
        returns = np.array([cycle["return_pct"] for cycle in entered], dtype=float)
        return {
            "total_return_pct": round(total_return * 100.0, 4),
            "cagr_pct": round(cagr * 100.0, 4) if cagr is not None else None,
            "final_balance": round(final_balance, 2),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
            "win_rate_pct": round(len(wins) / len(entered) * 100.0, 4) if entered else 0.0,
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
            "cycles": len(cycles), "entered_cycles": len(entered),
            "max_balance_drawdown_usd": round(risk["max_balance_dd_usd"], 2),
            "max_balance_drawdown_pct": round(risk["max_balance_dd_pct"], 4),
            "max_equity_drawdown_usd": round(risk["max_equity_dd_usd"], 2),
            "max_equity_drawdown_pct": round(risk["max_equity_dd_pct"], 4),
            "level_counts": level_counts,
            "liquidations": sum(bool(cycle["liquidated"]) for cycle in entered),
            "liquidation_dates": [cycle["close_time"] for cycle in entered if cycle["liquidated"]],
            "worst_cycle": worst,
            "peak_exposure_btc": round(risk["peak_exposure_btc"], 8),
            "peak_exposure_usd": round(risk["peak_exposure_usd"], 2),
            "peak_exposure_vs_equity": round(risk["peak_exposure_vs_equity"], 4),
            "total_funding_cost": round(sum(cycle["funding_cost"] for cycle in entered), 2),
            "total_execution_cost": round(sum(cycle["execution_cost"] for cycle in entered), 2),
            "duration_hours_p50": round(float(np.median(durations)), 3) if len(durations) else None,
            "duration_hours_p90": round(float(np.quantile(durations, 0.9)), 3) if len(durations) else None,
            "cycle_return_pct_p10": round(float(np.quantile(returns, 0.1)), 4) if len(returns) else None,
            "cycle_return_pct_p50": round(float(np.median(returns)), 4) if len(returns) else None,
        }
