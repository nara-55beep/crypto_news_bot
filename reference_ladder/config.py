"""Validated configuration for the Reference Ladder backtester."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Literal


SizingMode = Literal["auto", "fixed"]
DistanceMode = Literal["fixed", "atr"]


@dataclass(frozen=True)
class LadderConfig:
    symbol: str = "BTCUSDT"
    timeframe: str = "1m"
    starting_capital: float = 100_000.0

    trigger_distance: float = 800.0
    ladder_step: float = 500.0
    max_levels: int = 4
    distance_mode: DistanceMode = "fixed"
    trigger_atr_multiple: float = 8.0
    step_atr_multiple: float = 5.0

    sizing_mode: SizingMode = "auto"
    fixed_ladder_sizes: tuple[float, ...] = (10.0, 20.0, 40.0, 80.0)
    auto_btc_per_1000: float = 0.05
    level_multipliers: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

    leverage: float = 100.0
    maintenance_margin_rate: float = 0.005
    spread_round_turn_usd: float = 10.0
    commission_rate: float = 0.0004
    base_slippage_usd: float = 1.0
    slippage_usd_per_btc: float = 0.35
    slippage_power: float = 0.5
    funding_rate_8h: float = 0.0001

    profit_target_points: float | None = None
    max_loss_per_cycle_pct: float | None = None
    max_cycle_duration_hours: float | None = None
    trading_start_hour_utc: int = 0
    trading_end_hour_utc: int = 24
    weekends: bool = True

    signal_name: str = "bollinger-rsi-sma"
    bb_length: int = 20
    bb_deviations: float = 2.0
    rsi_length: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    trend_sma_length: int = 200
    allow_short_signals: bool = False
    regime_filter: bool = False
    regime_slope_lookback: int = 240
    max_regime_slope_pct: float = 3.0

    curve_every_bars: int = 60

    def validate(self) -> "LadderConfig":
        if self.symbol != "BTCUSDT" or self.timeframe != "1m":
            raise ValueError("Reference Ladder is BTCUSDT 1-minute only")
        positive = {
            "starting_capital": self.starting_capital,
            "trigger_distance": self.trigger_distance,
            "ladder_step": self.ladder_step,
            "max_levels": self.max_levels,
            "leverage": self.leverage,
            "bb_length": self.bb_length,
            "rsi_length": self.rsi_length,
            "trend_sma_length": self.trend_sma_length,
            "curve_every_bars": self.curve_every_bars,
        }
        for name, value in positive.items():
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_levels > 4:
            raise ValueError("max_levels cannot exceed 4")
        if self.sizing_mode not in {"auto", "fixed"}:
            raise ValueError("sizing_mode must be auto or fixed")
        if self.distance_mode not in {"fixed", "atr"}:
            raise ValueError("distance_mode must be fixed or atr")
        if len(self.fixed_ladder_sizes) < self.max_levels or len(self.level_multipliers) < self.max_levels:
            raise ValueError("the selected size sequence must cover max_levels")
        if any(float(value) <= 0 for value in self.fixed_ladder_sizes[: self.max_levels]):
            raise ValueError("ladder sizes and multipliers must be positive")
        if any(float(value) <= 0 for value in self.level_multipliers[: self.max_levels]):
            raise ValueError("ladder sizes and multipliers must be positive")
        for name in ("trigger_atr_multiple", "step_atr_multiple", "auto_btc_per_1000"):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "maintenance_margin_rate", "spread_round_turn_usd", "commission_rate",
            "base_slippage_usd", "slippage_usd_per_btc", "funding_rate_8h",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} cannot be negative")
        if not (1.0 <= self.leverage <= 200.0):
            raise ValueError("leverage must be between 1 and 200")
        if not (0.0 <= self.maintenance_margin_rate < 1.0):
            raise ValueError("maintenance_margin_rate must be in [0, 1)")
        if not (0 <= self.trading_start_hour_utc <= 23):
            raise ValueError("trading_start_hour_utc must be 0..23")
        if not (1 <= self.trading_end_hour_utc <= 24):
            raise ValueError("trading_end_hour_utc must be 1..24")
        if self.trading_start_hour_utc >= self.trading_end_hour_utc:
            raise ValueError("trading_start_hour_utc must precede trading_end_hour_utc")
        if self.profit_target_points is not None and self.profit_target_points <= 0:
            raise ValueError("profit_target_points must be positive")
        if self.max_loss_per_cycle_pct is not None and not (0 < self.max_loss_per_cycle_pct <= 1):
            raise ValueError("max_loss_per_cycle_pct must be a fraction in (0, 1]")
        if self.max_cycle_duration_hours is not None and self.max_cycle_duration_hours <= 0:
            raise ValueError("max_cycle_duration_hours must be positive")
        return self

    def sizes_for_equity(self, equity: float) -> tuple[float, ...]:
        if self.sizing_mode == "fixed":
            return tuple(float(value) for value in self.fixed_ladder_sizes[: self.max_levels])
        base = max(0.0, float(equity)) / 1_000.0 * self.auto_btc_per_1000
        return tuple(base * float(multiplier) for multiplier in self.level_multipliers[: self.max_levels])

    def with_overrides(self, values: dict[str, Any]) -> "LadderConfig":
        allowed = set(asdict(self))
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown Reference Ladder setting(s): {sorted(unknown)}")
        clean = dict(values)
        for key in ("fixed_ladder_sizes", "level_multipliers"):
            if key in clean:
                raw = clean[key]
                if isinstance(raw, str):
                    raw = [part.strip() for part in raw.split(",") if part.strip()]
                clean[key] = tuple(float(value) for value in raw)
        return replace(self, **clean).validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
