"""One paper-trading / historical-replay engine shared by every strategy.

The contract that keeps results honest:

* A strategy sees bars ``0..i`` and emits a target position for bar ``i``.
* The engine fills that target at bar ``i+1``'s **open**, never at bar ``i``'s
  close.  A signal derived from a close cannot also be filled at that close.
* Costs are charged on every fill: half-spread, slippage and commission.
* Intrabar stop and target are resolved adversely.  When one bar touches both,
  the stop wins, because OHLC cannot prove which came first.  A gap through a
  stop fills at the open, not at the stop price.

No strategy gets its own result generator; a bug here shows up everywhere at
once, which is the point.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

import numpy as np
import pandas as pd


TRADING_DAYS = 252.0


@dataclass(frozen=True)
class CostModel:
    """Execution costs.  Defaults are deliberately not zero."""

    commission_per_share: float = 0.005
    commission_minimum: float = 1.00
    spread_bps: float = 5.0          # full spread; the engine charges half per side
    slippage_bps: float = 2.0
    short_borrow_bps_annual: float = 50.0

    def commission(self, shares: float, price: float) -> float:
        return max(self.commission_minimum, abs(shares) * self.commission_per_share) if shares else 0.0

    def fill_price(self, price: float, side: Literal["buy", "sell"]) -> float:
        """Adverse fill: cross half the spread, then pay slippage on top."""
        edge = price * (self.spread_bps / 2.0 + self.slippage_bps) / 10_000.0
        return price + edge if side == "buy" else price - edge

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunConfig:
    symbol: str = "SPY"
    start: str = ""
    end: str = ""
    timeframe: str = "1d"
    starting_capital: float = 100_000.0
    costs: CostModel = field(default_factory=CostModel)
    allow_long: bool = True
    allow_short: bool = True
    sizing: Literal["fixed-fraction", "volatility-target", "fixed-shares"] = "fixed-fraction"
    position_fraction: float = 0.95      # of equity when fully in
    target_volatility: float = 0.15      # annualised, for volatility-target sizing
    fixed_shares: int = 100
    max_position_fraction: float = 1.0
    benchmark: str = "buy-and-hold"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["costs"] = self.costs.to_dict()
        return data


@dataclass(frozen=True)
class Trade:
    entry_index: int
    exit_index: int
    entry_date: str
    exit_date: str
    direction: Literal["long", "short"]
    shares: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    return_pct: float
    bars_held: int
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MIN_TRADES_FOR_RANKING = 30
MIN_BARS_FOR_ANNUALISING = 126  # ~6 months of daily bars


@dataclass
class RunResult:
    ok: bool
    strategy_id: str
    config: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _empty_metrics() -> dict[str, Any]:
    return {
        "net_return_pct": 0.0, "annualised_return_pct": None, "max_drawdown_pct": 0.0,
        "sharpe": None, "sortino": None, "calmar": None, "win_rate_pct": 0.0,
        "profit_factor": None, "expectancy": 0.0, "trades": 0, "avg_bars_held": 0.0,
        "commission_cost": 0.0, "spread_slippage_cost": 0.0, "total_cost": 0.0,
        "exposure_pct": 0.0, "turnover": 0.0, "benchmark_return_pct": 0.0,
        "excess_return_pct": 0.0, "final_equity": 0.0, "bars": 0,
        "sample_sufficient": False, "evidence_note": "no trades",
    }


def _annualisation(index: pd.Index, bars: int) -> float:
    """Bars per year, inferred from the actual timestamps."""
    if bars < 2 or not isinstance(index, pd.DatetimeIndex):
        return TRADING_DAYS
    span_days = (index[-1] - index[0]).total_seconds() / 86_400.0
    if span_days <= 0:
        return TRADING_DAYS
    return max(1.0, bars / (span_days / 365.25))


def run_backtest(
    frame: pd.DataFrame,
    signal: pd.Series,
    config: RunConfig,
    *,
    strategy_id: str = "",
    stop_atr: pd.Series | None = None,
    atr_stop_multiple: float = 0.0,
    take_profit_multiple: float = 0.0,
    max_bars_held: int = 0,
    cancelled: Callable[[], bool] | None = None,
) -> RunResult:
    """Replay ``signal`` (target position in {-1,0,1} at each bar) over ``frame``."""
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        return RunResult(False, strategy_id, config.to_dict(),
                         error=f"market data is missing column(s): {sorted(missing)}")
    if len(frame) < 30:
        return RunResult(False, strategy_id, config.to_dict(),
                         error=f"only {len(frame)} bars available; at least 30 are required")

    target = signal.reindex(frame.index).fillna(0.0).clip(-1.0, 1.0)
    if not config.allow_short:
        target = target.clip(lower=0.0)
    if not config.allow_long:
        target = target.clip(upper=0.0)

    opens = frame["open"].to_numpy(dtype=float)
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    targets = target.to_numpy(dtype=float)
    stops = (stop_atr.reindex(frame.index).to_numpy(dtype=float)
             if stop_atr is not None else np.full(len(frame), np.nan))
    dates = [str(x)[:19] for x in frame.index]
    costs = config.costs

    cash = float(config.starting_capital)
    shares = 0.0
    direction = 0
    entry_price = 0.0
    entry_index = 0
    stop_price = math.nan
    target_price = math.nan
    commission_paid = 0.0
    edge_paid = 0.0
    trades: list[Trade] = []
    equity_points: list[dict[str, Any]] = []
    exposed_bars = 0
    traded_notional = 0.0

    returns = pd.Series(closes, index=frame.index).pct_change()
    realised_vol = returns.rolling(20, min_periods=20).std(ddof=0) * math.sqrt(TRADING_DAYS)
    vol_array = realised_vol.to_numpy(dtype=float)

    def size_for(price: float, equity: float, bar: int) -> float:
        if price <= 0:
            return 0.0
        if config.sizing == "fixed-shares":
            return float(config.fixed_shares)
        fraction = config.position_fraction
        if config.sizing == "volatility-target":
            vol = vol_array[bar]
            if not np.isfinite(vol) or vol <= 0:
                return 0.0
            fraction = min(config.max_position_fraction, config.target_volatility / vol)
        fraction = max(0.0, min(fraction, config.max_position_fraction))
        return math.floor(equity * fraction / price)

    def close_position(bar: int, raw_price: float, reason: str) -> None:
        nonlocal cash, shares, direction, entry_price, stop_price, target_price
        nonlocal commission_paid, edge_paid
        if direction == 0 or shares == 0:
            return
        side = "sell" if direction > 0 else "buy"
        fill = costs.fill_price(raw_price, side)
        fee = costs.commission(shares, fill)
        edge = abs(fill - raw_price) * shares
        gross = (fill - entry_price) * shares * direction
        borrow = 0.0
        if direction < 0:
            held_years = max(0, bar - entry_index) / TRADING_DAYS
            borrow = entry_price * shares * (costs.short_borrow_bps_annual / 10_000.0) * held_years
        cash += gross - fee - borrow
        commission_paid += fee
        edge_paid += edge
        risked = entry_price * shares
        trades.append(Trade(
            entry_index=entry_index, exit_index=bar,
            entry_date=dates[entry_index], exit_date=dates[bar],
            direction="long" if direction > 0 else "short",
            shares=float(shares), entry_price=round(float(entry_price), 6),
            exit_price=round(float(fill), 6),
            gross_pnl=round(float(gross), 2), costs=round(float(fee + borrow), 2),
            net_pnl=round(float(gross - fee - borrow), 2),
            return_pct=round(float(100.0 * (gross - fee - borrow) / risked), 4) if risked else 0.0,
            bars_held=bar - entry_index, exit_reason=reason,
        ))
        shares, direction, entry_price = 0.0, 0, 0.0
        stop_price = target_price = math.nan

    def open_position(bar: int, raw_price: float, wanted: int) -> None:
        nonlocal cash, shares, direction, entry_price, entry_index, stop_price, target_price
        nonlocal commission_paid, edge_paid, traded_notional
        side = "buy" if wanted > 0 else "sell"
        fill = costs.fill_price(raw_price, side)
        equity = cash
        quantity = size_for(fill, equity, bar)
        if quantity <= 0:
            return
        fee = costs.commission(quantity, fill)
        if fee >= equity:
            return
        cash -= fee
        commission_paid += fee
        edge_paid += abs(fill - raw_price) * quantity
        traded_notional += fill * quantity
        shares, direction, entry_price, entry_index = float(quantity), wanted, fill, bar
        if atr_stop_multiple > 0 and np.isfinite(stops[bar]):
            offset = stops[bar] * atr_stop_multiple
            stop_price = fill - offset if wanted > 0 else fill + offset
            if take_profit_multiple > 0:
                reward = offset * take_profit_multiple
                target_price = fill + reward if wanted > 0 else fill - reward

    for bar in range(len(frame)):
        if cancelled is not None and bar % 512 == 0 and cancelled():
            return RunResult(False, strategy_id, config.to_dict(), error="run cancelled")

        # 1. Intrabar risk exits come first and resolve adversely.
        if direction != 0:
            hit_stop = hit_target = False
            if np.isfinite(stop_price):
                hit_stop = lows[bar] <= stop_price if direction > 0 else highs[bar] >= stop_price
            if np.isfinite(target_price):
                hit_target = highs[bar] >= target_price if direction > 0 else lows[bar] <= target_price
            if hit_stop:
                # A gap through the stop fills at the open, which is worse.
                gapped = (opens[bar] < stop_price) if direction > 0 else (opens[bar] > stop_price)
                close_position(bar, opens[bar] if gapped else stop_price, "stop")
            elif hit_target:
                close_position(bar, target_price, "target")
            elif max_bars_held > 0 and (bar - entry_index) >= max_bars_held:
                close_position(bar, closes[bar], "time-stop")

        # 2. Act on the PREVIOUS bar's signal at THIS bar's open.
        if bar > 0:
            wanted = int(np.sign(targets[bar - 1]))
            if wanted != direction:
                if direction != 0:
                    close_position(bar, opens[bar], "signal")
                if wanted != 0:
                    open_position(bar, opens[bar], wanted)

        mark = cash + (closes[bar] - entry_price) * shares * direction if direction else cash
        if direction != 0:
            exposed_bars += 1
        equity_points.append({"date": dates[bar], "equity": round(float(mark), 2)})

    if direction != 0:
        close_position(len(frame) - 1, closes[-1], "end-of-test")
        equity_points[-1]["equity"] = round(float(cash), 2)

    metrics = _compute_metrics(
        frame=frame, equity_points=equity_points, trades=trades, config=config,
        commission_paid=commission_paid, edge_paid=edge_paid,
        exposed_bars=exposed_bars, traded_notional=traded_notional,
    )
    warnings = _sample_warnings(metrics, len(frame), config)
    return RunResult(
        ok=True, strategy_id=strategy_id, config=config.to_dict(), metrics=metrics,
        trades=[t.to_dict() for t in trades], equity_curve=equity_points, warnings=warnings,
    )


def _compute_metrics(*, frame, equity_points, trades, config, commission_paid,
                     edge_paid, exposed_bars, traded_notional) -> dict[str, Any]:
    metrics = _empty_metrics()
    bars = len(frame)
    metrics["bars"] = bars
    if not equity_points:
        return metrics

    equity = pd.Series([p["equity"] for p in equity_points], dtype=float)
    start = float(config.starting_capital)
    final = float(equity.iloc[-1])
    metrics["final_equity"] = round(final, 2)
    metrics["net_return_pct"] = round(100.0 * (final / start - 1.0), 4) if start else 0.0

    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    metrics["max_drawdown_pct"] = round(100.0 * float(drawdown.min()), 4)

    periodic = equity.pct_change().dropna()
    per_year = _annualisation(frame.index, bars)
    if len(periodic) > 2 and float(periodic.std(ddof=0)) > 0:
        mean, std = float(periodic.mean()), float(periodic.std(ddof=0))
        metrics["sharpe"] = round(mean / std * math.sqrt(per_year), 4)
        downside = periodic[periodic < 0]
        dstd = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
        metrics["sortino"] = round(mean / dstd * math.sqrt(per_year), 4) if dstd > 0 else None

    if bars >= MIN_BARS_FOR_ANNUALISING and start > 0 and final > 0:
        years = bars / per_year
        if years > 0:
            annual = 100.0 * ((final / start) ** (1.0 / years) - 1.0)
            metrics["annualised_return_pct"] = round(annual, 4)
            mdd = abs(metrics["max_drawdown_pct"])
            metrics["calmar"] = round(annual / mdd, 4) if mdd > 1e-9 else None

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    metrics["trades"] = len(trades)
    if trades:
        metrics["win_rate_pct"] = round(100.0 * len(wins) / len(trades), 4)
        metrics["expectancy"] = round(sum(t.net_pnl for t in trades) / len(trades), 4)
        metrics["avg_bars_held"] = round(sum(t.bars_held for t in trades) / len(trades), 2)
        gain = sum(t.net_pnl for t in wins)
        pain = abs(sum(t.net_pnl for t in losses))
        metrics["profit_factor"] = round(gain / pain, 4) if pain > 1e-9 else None

    metrics["commission_cost"] = round(commission_paid, 2)
    metrics["spread_slippage_cost"] = round(edge_paid, 2)
    metrics["total_cost"] = round(commission_paid + edge_paid, 2)
    metrics["exposure_pct"] = round(100.0 * exposed_bars / bars, 2) if bars else 0.0
    metrics["turnover"] = round(traded_notional / start, 3) if start else 0.0

    first_open, last_close = float(frame["open"].iloc[0]), float(frame["close"].iloc[-1])
    bench = 100.0 * (last_close / first_open - 1.0) if first_open else 0.0
    metrics["benchmark_return_pct"] = round(bench, 4)
    metrics["excess_return_pct"] = round(metrics["net_return_pct"] - bench, 4)

    metrics["sample_sufficient"] = bool(
        len(trades) >= MIN_TRADES_FOR_RANKING and bars >= MIN_BARS_FOR_ANNUALISING
    )
    metrics["evidence_note"] = (
        "sufficient sample for ranking" if metrics["sample_sufficient"]
        else f"insufficient sample: {len(trades)} trades over {bars} bars"
    )
    return metrics


def _sample_warnings(metrics: dict[str, Any], bars: int, config: RunConfig) -> list[str]:
    out: list[str] = []
    count = metrics["trades"]
    if count == 0:
        out.append("No trades were generated; this strategy produced no signal on this data.")
    elif count < MIN_TRADES_FOR_RANKING:
        out.append(
            f"Only {count} trades. Below {MIN_TRADES_FOR_RANKING} the statistics are noise, "
            "not evidence, and this result must not be ranked as reliable."
        )
    if bars < MIN_BARS_FOR_ANNUALISING:
        out.append(
            f"Only {bars} bars tested. Annualised figures are suppressed below "
            f"{MIN_BARS_FOR_ANNUALISING} bars because they would exaggerate a short sample."
        )
    if metrics["total_cost"] == 0:
        out.append("No execution cost was charged; check the cost model before trusting this.")
    if config.costs.spread_bps <= 0 and config.costs.slippage_bps <= 0:
        out.append("Spread and slippage are both zero, which no real venue offers.")
    return out
