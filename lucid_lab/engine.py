"""Deterministic Lucid account, execution, sizing and import primitives."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .rules import AccountRules, INSTRUMENTS, Instrument


CENT = Decimal("0.01")
NY = ZoneInfo("America/New_York")


def D(value: Any) -> Decimal:
    """Convert through text so binary floats never set financial boundaries."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class BarExit:
    reason: Literal["stop", "target", "none"]
    price: Decimal | None


def resolve_bar_exit(
    *,
    side: Literal["long", "short"],
    stop: Any,
    target: Any,
    bar_open: Any,
    bar_high: Any,
    bar_low: Any,
    tick_size: Any,
    exit_slippage_ticks: Any = 1,
) -> BarExit:
    """Resolve one OHLC bar conservatively without inventing intrabar ordering."""
    stop, target, op = D(stop), D(target), D(bar_open)
    high, low, tick = D(bar_high), D(bar_low), D(tick_size)
    slip = D(exit_slippage_ticks) * tick
    if not (low <= op <= high) or low > high:
        raise ValueError("bar has impossible OHLC ordering")
    if side == "long":
        stopped, targeted = low <= stop, high >= target
        if stopped:  # stop wins when both are touched
            return BarExit("stop", min(stop, op) - slip)
        if targeted:
            return BarExit("target", target - slip)
    elif side == "short":
        stopped, targeted = high >= stop, low <= target
        if stopped:
            return BarExit("stop", max(stop, op) + slip)
        if targeted:
            return BarExit("target", target + slip)
    else:
        raise ValueError("side must be 'long' or 'short'")
    return BarExit("none", None)


def new_york_time(timestamp: str | datetime) -> datetime:
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else timestamp
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(NY)


@dataclass(frozen=True)
class ExecutionPreset:
    key: str
    label: str
    spread_ticks_rt: Decimal
    slippage_ticks_rt: Decimal
    stop_extra_ticks: Decimal = Decimal("0")
    missed_trade_pct: Decimal = Decimal("0")
    latency_ms: int = 0
    classification: str = "Conservative assumption"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("spread_ticks_rt", "slippage_ticks_rt", "stop_extra_ticks", "missed_trade_pct"):
            data[key] = str(data[key])
        return data


EXECUTION_PRESETS: dict[str, ExecutionPreset] = {
    "normal": ExecutionPreset("normal", "Normal conservative", D("1"), D("1")),
    "spread_50": ExecutionPreset("spread_50", "Spread +50%", D("1.5"), D("1")),
    "spread_2x": ExecutionPreset("spread_2x", "Spread doubled", D("2"), D("1")),
    "slippage_50": ExecutionPreset("slippage_50", "Slippage +50%", D("1"), D("1.5")),
    "slippage_2x": ExecutionPreset("slippage_2x", "Slippage doubled", D("1"), D("2")),
    "volatile_open": ExecutionPreset("volatile_open", "Volatile open", D("2"), D("3"), D("1"), D("5"), 250),
    "low_liquidity": ExecutionPreset("low_liquidity", "Low liquidity", D("2"), D("2"), D("1"), D("10"), 350),
    "delayed_stop": ExecutionPreset("delayed_stop", "Delayed stop", D("1"), D("1"), D("1"), D("0"), 500),
    "gap_event": ExecutionPreset("gap_event", "Gap-through-stop", D("1"), D("1"), D("4")),
    "missed_trades": ExecutionPreset("missed_trades", "10% missed trades", D("1"), D("1"), D("0"), D("10")),
    "severe": ExecutionPreset("severe", "Combined severe", D("2"), D("3"), D("4"), D("20"), 750),
}


@dataclass(frozen=True)
class OrderFill:
    timestamp: datetime
    price: Decimal
    quantity: int


@dataclass
class WorkingLimitOrder:
    """Deterministic trade-print-driven limit-order lifecycle.

    OHLC bars are deliberately insufficient for this model. A fill requires a
    timestamped trade after the latency watermark, price-through, and depletion
    of any explicitly supplied queue ahead. Equality at activation is rejected
    because millisecond timestamps cannot prove that the print followed the
    order.
    """

    order_id: str
    side: Literal["buy", "sell"]
    limit_price: Decimal
    quantity: int
    placed_at: datetime
    latency_ms: int = 0
    queue_ahead: Decimal = Decimal("0")
    filled_quantity: int = 0
    status: Literal["working", "partial", "filled", "cancelled", "rejected"] = "working"
    rejection_reason: str = ""
    cancelled_at: datetime | None = None
    fills: list[OrderFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.limit_price = D(self.limit_price)
        self.queue_ahead = D(self.queue_ahead)
        if self.side not in {"buy", "sell"}:
            raise ValueError("limit side must be buy or sell")
        if self.limit_price <= 0 or self.quantity <= 0:
            raise ValueError("limit price and quantity must be positive")
        if self.latency_ms < 0 or self.queue_ahead < 0:
            raise ValueError("latency and queue ahead cannot be negative")
        if self.placed_at.tzinfo is None:
            raise ValueError("order timestamp must include a timezone")

    @property
    def activation_at(self) -> datetime:
        return self.placed_at + timedelta(milliseconds=self.latency_ms)

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.quantity - self.filled_quantity)

    def reject(self, reason: str) -> None:
        if self.filled_quantity:
            raise RuntimeError("a partially filled order cannot be rejected")
        self.status = "rejected"
        self.rejection_reason = reason.strip() or "rejected by risk or venue"

    def cancel(self, timestamp: datetime) -> None:
        if timestamp.tzinfo is None:
            raise ValueError("cancellation timestamp must include a timezone")
        if timestamp < self.placed_at:
            raise ValueError("cancellation cannot precede placement")
        if self.status in {"filled", "rejected"}:
            return
        self.cancelled_at = timestamp
        self.status = "cancelled"

    def process_trade(self, *, timestamp: datetime, price: Any, quantity: Any) -> int:
        if timestamp.tzinfo is None:
            raise ValueError("trade timestamp must include a timezone")
        if self.status not in {"working", "partial"}:
            return 0
        if timestamp <= self.activation_at:
            return 0
        trade_price, available = D(price), D(quantity)
        if trade_price <= 0 or available <= 0:
            raise ValueError("trade price and quantity must be positive")
        reaches = trade_price <= self.limit_price if self.side == "buy" else trade_price >= self.limit_price
        if not reaches:
            return 0
        queue_used = min(self.queue_ahead, available)
        self.queue_ahead -= queue_used
        available -= queue_used
        if available <= 0:
            return 0
        executed = min(self.remaining_quantity, int(available.to_integral_value(rounding=ROUND_FLOOR)))
        if executed <= 0:
            return 0
        self.filled_quantity += executed
        self.fills.append(OrderFill(timestamp, self.limit_price, executed))
        self.status = "filled" if self.remaining_quantity == 0 else "partial"
        return executed


def marketable_fill_price(
    *, side: Literal["buy", "sell"], bid: Any, ask: Any,
    tick_size: Any, slippage_ticks: Any,
) -> Decimal:
    """Return an adverse executable price from a valid two-sided book."""
    bid, ask, tick, slippage = D(bid), D(ask), D(tick_size), D(slippage_ticks)
    if bid <= 0 or ask <= bid or tick <= 0 or slippage < 0:
        raise ValueError("a valid unlocked two-sided book and non-negative slippage are required")
    if side == "buy":
        return ask + slippage * tick
    if side == "sell":
        price = bid - slippage * tick
        if price <= 0:
            raise ValueError("adverse sell fill price must remain positive")
        return price
    raise ValueError("side must be buy or sell")


@dataclass(frozen=True)
class PositionSizeInput:
    instrument: str
    current_balance: Decimal
    drawdown_floor: Decimal
    daily_loss_remaining: Decimal | None
    stop_ticks: Decimal
    selected_risk_budget: Decimal
    safety_reserve: Decimal
    open_micro_equivalents: int = 0
    execution_preset: str = "normal"
    committed_stop_risk: Decimal = Decimal("0")
    committed_stop_risk_defaulted: bool = False


@dataclass(frozen=True)
class PositionSizeResult:
    tick_value: Decimal
    commission_round_trip: Decimal
    spread_cost_per_contract: Decimal
    slippage_cost_per_contract: Decimal
    usable_risk_buffer: Decimal
    safety_reserve: Decimal
    committed_stop_risk: Decimal
    risk_per_contract: Decimal
    maximum_by_account_cap: int
    maximum_by_risk: int
    final_quantity: int
    expected_cost: Decimal
    loss_if_stopped: Decimal
    remaining_buffer_after_stop: Decimal
    binding_constraint: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "tick_value", "commission_round_trip", "spread_cost_per_contract",
            "slippage_cost_per_contract", "usable_risk_buffer", "safety_reserve",
            "committed_stop_risk",
            "risk_per_contract", "expected_cost", "loss_if_stopped",
            "remaining_buffer_after_stop",
        ):
            data[key] = str(data[key])
        return data


def calculate_position_size(values: PositionSizeInput, rules: AccountRules) -> PositionSizeResult:
    try:
        instrument = INSTRUMENTS[values.instrument.upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported instrument: {values.instrument}") from exc
    try:
        preset = EXECUTION_PRESETS[values.execution_preset]
    except KeyError as exc:
        raise ValueError(f"unsupported execution preset: {values.execution_preset}") from exc

    balance = money(values.current_balance)
    floor = money(values.drawdown_floor)
    stop_ticks = D(values.stop_ticks)
    selected_budget = money(values.selected_risk_budget)
    reserve = max(Decimal("0"), money(values.safety_reserve))
    committed = money(values.committed_stop_risk)
    if stop_ticks <= 0:
        raise ValueError("stop distance must be greater than zero ticks")
    if balance <= floor:
        raise ValueError("current balance must be above the drawdown floor")
    if selected_budget <= 0:
        raise ValueError("risk budget must be greater than zero")
    if values.open_micro_equivalents < 0:
        raise ValueError("open contract usage cannot be negative")
    if committed < 0:
        raise ValueError("committed stop risk cannot be negative")

    commission_rt = instrument.commission_per_side * D("2")
    spread_cost = preset.spread_ticks_rt * instrument.tick_value
    slippage_cost = preset.slippage_ticks_rt * instrument.tick_value
    execution_cost = spread_cost + slippage_cost + commission_rt
    stop_cost = (stop_ticks + preset.stop_extra_ticks) * instrument.tick_value
    risk_per_contract = money(stop_cost + execution_cost)
    buffer = max(Decimal("0"), balance - floor - reserve - committed)
    daily_room = (
        buffer
        if values.daily_loss_remaining is None
        else max(Decimal("0"), money(values.daily_loss_remaining) - committed)
    )
    usable_budget = min(selected_budget, buffer, daily_room)
    maximum_by_risk = int((usable_budget / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))

    cap_room_micro = max(0, rules.max_micros - int(values.open_micro_equivalents))
    maximum_by_account_cap = cap_room_micro // instrument.cap_units
    final_quantity = max(0, min(maximum_by_risk, maximum_by_account_cap))
    expected_cost = money(execution_cost * final_quantity)
    loss_if_stopped = money(risk_per_contract * final_quantity)
    remaining = money(balance - floor - committed - loss_if_stopped)
    constraints = {
        "risk budget": maximum_by_risk,
        "Lucid contract cap": maximum_by_account_cap,
    }
    binding = min(constraints, key=constraints.get)
    warnings: list[str] = []
    if values.committed_stop_risk_defaulted:
        warnings.append("Committed stop risk defaulted to zero; enter the stop risk of every open position before using this quantity.")
    if final_quantity == 0:
        warnings.append("No whole contract fits the selected account risk.")
    if remaining < reserve:
        warnings.append("The calculated stop would consume the requested safety reserve.")
    if not rules.evidence_compatible:
        warnings.append("The selected configuration does not share the displayed historical evidence model.")
    return PositionSizeResult(
        tick_value=money(instrument.tick_value),
        commission_round_trip=money(commission_rt),
        spread_cost_per_contract=money(spread_cost),
        slippage_cost_per_contract=money(slippage_cost),
        usable_risk_buffer=money(usable_budget),
        safety_reserve=money(reserve),
        committed_stop_risk=committed,
        risk_per_contract=risk_per_contract,
        maximum_by_account_cap=maximum_by_account_cap,
        maximum_by_risk=maximum_by_risk,
        final_quantity=final_quantity,
        expected_cost=expected_cost,
        loss_if_stopped=loss_if_stopped,
        remaining_buffer_after_stop=remaining,
        binding_constraint=binding,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class TradeFill:
    session: str
    instrument: str
    quantity: int
    gross_pnl: Decimal
    commission: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    exit_reason: str = "strategy"
    forced_liquidation: bool = False
    # Set for a fill that flattens exposure opened before the account reached a
    # terminal state.  Such a fill must still be bookable so the session can be
    # closed out honestly.
    closes_open_exposure: bool = False
    intraday_peak_equity: Decimal | None = None
    intraday_low_equity: Decimal | None = None

    @property
    def net_pnl(self) -> Decimal:
        return money(self.gross_pnl - self.commission - self.spread_cost - self.slippage_cost)


@dataclass
class AccountSnapshot:
    session: str
    starting_balance: Decimal
    ending_balance: Decimal
    daily_net_pnl: Decimal
    ending_equity: Decimal
    unrealized_pnl: Decimal
    largest_profitable_day: Decimal
    consistency_pct: Decimal | None
    drawdown_floor: Decimal
    remaining_drawdown: Decimal
    permitted_micros: int
    open_micro_equivalents: int
    scaling_tier: str
    liquidation_deadline: str
    warnings: list[str]
    status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = str(value)
        return data


@dataclass
class LucidAccount:
    rules: AccountRules
    balance: Decimal = field(init=False)
    floor: Decimal = field(init=False)
    highest_qualifying_balance: Decimal = field(init=False)
    intraday_peak: Decimal = field(init=False)
    gross_pnl: Decimal = Decimal("0")
    commissions: Decimal = Decimal("0")
    spread_cost: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    current_session: str = ""
    session_start_balance: Decimal = field(init=False)
    daily_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    open_micro_equivalents: int = 0
    open_exposure_by_instrument: dict[str, int] = field(default_factory=dict)
    daily_profit_history: list[Decimal] = field(default_factory=list)
    trading_days: int = 0
    restricted: bool = False
    passed: bool = False
    breached: bool = False
    trail_locked: bool = False
    reason: str = "collecting"
    warnings: list[str] = field(default_factory=list)
    timeline: list[AccountSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.balance = money(self.rules.starting_balance)
        self.floor = money(self.rules.starting_balance - self.rules.max_loss)
        self.highest_qualifying_balance = self.balance
        self.intraday_peak = self.balance
        self.session_start_balance = self.balance

    @property
    def remaining_drawdown(self) -> Decimal:
        return money(self.current_equity - self.floor)

    @property
    def current_equity(self) -> Decimal:
        return money(self.balance + self.unrealized_pnl)

    @property
    def remaining_micros(self) -> int:
        return max(0, self.rules.max_micros - self.open_micro_equivalents)

    @property
    def scaling_tier(self) -> str:
        return "evaluation-fixed"

    def reserve_exposure(
        self,
        instrument: str,
        quantity: int,
        side: Literal["long", "short"] = "long",
    ) -> None:
        if self.passed or self.breached:
            raise RuntimeError("terminal account cannot open exposure")
        if self.restricted:
            raise RuntimeError("daily loss restriction blocks new exposure")
        symbol = instrument.upper()
        try:
            item = INSTRUMENTS[symbol]
        except KeyError as exc:
            raise ValueError(f"unsupported instrument: {instrument}") from exc
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        opposite = "short" if side == "long" else "long"
        # Lucid prohibits holding a contract long and short at the same time,
        # in one account or across accounts.  Minis against micros on the same
        # underlying are the one permitted case, so the guard keys on the exact
        # contract symbol rather than the underlying.
        if self.open_exposure_by_instrument.get(symbol, {}).get(opposite):
            raise ValueError(
                f"prohibited hedge: {symbol} is already open {opposite}; Lucid "
                "does not allow the same contract long and short"
            )
        requested = quantity * item.cap_units
        if requested > self.remaining_micros:
            raise ValueError("aggregate exposure exceeds the Lucid contract cap")
        self.open_micro_equivalents += requested
        book = self.open_exposure_by_instrument.setdefault(symbol, {})
        book[side] = book.get(side, 0) + quantity

    def open_quantity(self, instrument: str, side: str | None = None) -> int:
        """Open contracts for one symbol, optionally restricted to one side."""
        book = self.open_exposure_by_instrument.get(instrument.upper(), {})
        if side is None:
            return sum(book.values())
        return int(book.get(side, 0))

    def release_exposure(
        self,
        instrument: str,
        quantity: int,
        side: Literal["long", "short"] = "long",
    ) -> None:
        symbol = instrument.upper()
        try:
            item = INSTRUMENTS[symbol]
        except KeyError as exc:
            raise ValueError(f"unsupported instrument: {instrument}") from exc
        book = self.open_exposure_by_instrument.get(symbol, {})
        held = int(book.get(side, 0))
        released = quantity * item.cap_units
        if quantity <= 0 or quantity > held:
            raise ValueError("cannot release exposure that is not open")
        self.open_micro_equivalents -= released
        if quantity == held:
            book.pop(side, None)
        else:
            book[side] = held - quantity
        if not book:
            self.open_exposure_by_instrument.pop(symbol, None)
        if self.open_micro_equivalents == 0:
            self.unrealized_pnl = Decimal("0")

    def mark_to_market(
        self,
        unrealized_pnl: Any,
        *,
        observed_peak_equity: Any | None = None,
        observed_low_equity: Any | None = None,
    ) -> None:
        if self.open_micro_equivalents <= 0:
            raise RuntimeError("cannot mark an account with no open exposure")
        self.unrealized_pnl = money(unrealized_pnl)
        peak = self.current_equity if observed_peak_equity is None else money(observed_peak_equity)
        low = self.current_equity if observed_low_equity is None else money(observed_low_equity)
        self._advance_intraday_floor(peak)
        if low <= self.floor or self.current_equity <= self.floor:
            self.breached = True
            self.reason = "maximum loss limit reached by open equity"

    @property
    def total_profit(self) -> Decimal:
        return money(self.balance - self.rules.starting_balance)

    @property
    def largest_profitable_day(self) -> Decimal:
        positives = [pnl for pnl in self.daily_profit_history if pnl > 0]
        if self.daily_pnl > 0:
            positives.append(self.daily_pnl)
        return max(positives, default=Decimal("0"))

    @property
    def consistency_pct(self) -> Decimal | None:
        profit = self.total_profit
        if profit <= 0 or self.largest_profitable_day <= 0:
            return None
        return (self.largest_profitable_day / profit * D("100")).quantize(D("0.01"), rounding=ROUND_HALF_UP)

    def start_day(self, session: str) -> None:
        if self.current_session and self.current_session != session:
            raise ValueError("end the current session before starting another")
        if not self.current_session:
            self.current_session = session
            self.session_start_balance = self.balance
            self.daily_pnl = Decimal("0")
            self.restricted = False
            self.intraday_peak = self.balance

    def _advance_intraday_floor(self, observed_peak: Decimal) -> None:
        if self.rules.drawdown_type != "intraday":
            return
        self.intraday_peak = max(self.intraday_peak, money(observed_peak))
        if self.intraday_peak > self.rules.trail_trigger:
            self.floor = money(self.rules.locked_floor)
            self.trail_locked = True
        else:
            self.floor = max(self.floor, money(self.intraday_peak - self.rules.max_loss))

    def process_fill(self, fill: TradeFill) -> None:
        # A terminal account may still book a closing fill for exposure that was
        # already open when it passed or breached.  Refusing every fill left the
        # account unable to flatten and unable to end the session, because
        # end_day() also refuses to close over open exposure.
        closing = fill.closes_open_exposure
        if (self.passed or self.breached) and not closing:
            raise RuntimeError("terminal account cannot accept another fill")
        self.start_day(fill.session)
        if fill.session != self.current_session:
            raise ValueError("fill belongs to a different session")
        if fill.quantity <= 0:
            raise ValueError("quantity must be positive")
        try:
            instrument = INSTRUMENTS[fill.instrument.upper()]
        except KeyError as exc:
            raise ValueError(f"unsupported instrument: {fill.instrument}") from exc
        requested = fill.quantity * instrument.cap_units
        # The cap is an account-wide aggregate, so a fill has to be measured
        # against exposure already open in other instruments, not on its own.
        already_open = 0 if closing else self.open_micro_equivalents
        if requested + already_open > self.rules.max_micros:
            raise ValueError("quantity exceeds the Lucid aggregate contract cap")
        if self.restricted and not closing:
            raise RuntimeError("daily loss restriction blocks new trades until next session")

        was_terminal = self.passed or self.breached

        self.gross_pnl = money(self.gross_pnl + fill.gross_pnl)
        self.commissions = money(self.commissions + fill.commission)
        self.spread_cost = money(self.spread_cost + fill.spread_cost)
        self.slippage_cost = money(self.slippage_cost + fill.slippage_cost)
        self.balance = money(self.balance + fill.net_pnl)
        self.daily_pnl = money(self.daily_pnl + fill.net_pnl)

        if was_terminal:
            # The account is already decided; a flattening fill is recorded in
            # the ledger but cannot re-open, re-pass or re-breach it.
            if fill.forced_liquidation:
                self.warnings.append("Position was force-closed at the modeled cutoff.")
            return

        peak = fill.intraday_peak_equity if fill.intraday_peak_equity is not None else max(self.balance, self.intraday_peak)
        self._advance_intraday_floor(money(peak))
        observed_low = fill.intraday_low_equity if fill.intraday_low_equity is not None else self.balance
        if money(observed_low) <= self.floor or self.balance <= self.floor:
            self.breached = True
            self.reason = "maximum loss limit reached"
            return
        if self.rules.daily_loss_limit is not None and self.daily_pnl <= -self.rules.daily_loss_limit:
            self.restricted = True
            self.reason = "daily loss limit reached — restricted until next session"
        if fill.forced_liquidation:
            self.warnings.append("Position was force-closed at the modeled cutoff.")
        if self.rules.consistency_limit_pct is None and self._target_reached() and self.rules.minimum_trading_days <= self.trading_days + 1:
            self.passed = True
            self.reason = "profit target reached"

    def _target_reached(self) -> bool:
        return self.rules.profit_target is not None and self.total_profit >= self.rules.profit_target

    def _consistency_satisfied(self) -> bool:
        limit = self.rules.consistency_limit_pct
        if limit is None:
            return True
        pct = self.consistency_pct
        return pct is not None and pct <= limit

    def end_day(self) -> AccountSnapshot:
        if not self.current_session:
            raise ValueError("no active session")
        if self.open_micro_equivalents or self.unrealized_pnl:
            raise RuntimeError("all exposure must be force-liquidated before session end")
        session = self.current_session
        self.trading_days += 1
        self.daily_profit_history.append(self.daily_pnl)
        if self.rules.drawdown_type == "eod" and not self.breached:
            self.highest_qualifying_balance = max(self.highest_qualifying_balance, self.balance)
            if self.highest_qualifying_balance > self.rules.trail_trigger:
                self.floor = money(self.rules.locked_floor)
                self.trail_locked = True
            else:
                self.floor = max(self.floor, money(self.highest_qualifying_balance - self.rules.max_loss))
            if self.balance <= self.floor:
                self.breached = True
                self.reason = "maximum loss limit reached at session close"
        if not self.breached and self._target_reached():
            if self.trading_days < self.rules.minimum_trading_days:
                self.reason = "target reached; minimum trading days remain"
            elif not self._consistency_satisfied():
                self.reason = "target reached; consistency requirement not satisfied"
            else:
                self.passed = True
                self.reason = "profit target and evaluation rules satisfied"

        status = "BREACHED" if self.breached else ("PASSED" if self.passed else ("RESTRICTED" if self.restricted else "ACTIVE"))
        snapshot = AccountSnapshot(
            session=session,
            starting_balance=self.session_start_balance,
            ending_balance=self.balance,
            daily_net_pnl=self.daily_pnl,
            ending_equity=self.current_equity,
            unrealized_pnl=self.unrealized_pnl,
            largest_profitable_day=self.largest_profitable_day,
            consistency_pct=self.consistency_pct,
            drawdown_floor=self.floor,
            remaining_drawdown=self.remaining_drawdown,
            permitted_micros=self.rules.max_micros,
            open_micro_equivalents=self.open_micro_equivalents,
            scaling_tier=self.scaling_tier,
            liquidation_deadline=self.rules.forced_close_ny,
            warnings=list(self.warnings),
            status=status,
            reason=self.reason,
        )
        self.timeline.append(snapshot)
        self.current_session = ""
        self.daily_pnl = Decimal("0")
        self.restricted = False
        self.warnings.clear()
        return snapshot

    def state(self) -> dict[str, Any]:
        return {
            "balance": str(self.balance),
            "current_equity": str(self.current_equity),
            "unrealized_pnl": str(self.unrealized_pnl),
            "open_micro_equivalents": self.open_micro_equivalents,
            "open_exposure_by_instrument": dict(sorted(self.open_exposure_by_instrument.items())),
            "remaining_micros": self.remaining_micros,
            "scaling_tier": self.scaling_tier,
            "liquidation_deadline": self.rules.forced_close_ny,
            "gross_pnl": str(self.gross_pnl),
            "commissions": str(self.commissions),
            "spread_cost": str(self.spread_cost),
            "slippage_cost": str(self.slippage_cost),
            "net_profit": str(self.total_profit),
            "drawdown_floor": str(self.floor),
            "remaining_drawdown": str(self.remaining_drawdown),
            "highest_qualifying_balance": str(self.highest_qualifying_balance),
            "daily_pnl": str(self.daily_pnl),
            "active_session": self.current_session or None,
            "largest_profitable_day": str(self.largest_profitable_day),
            "consistency_pct": None if self.consistency_pct is None else str(self.consistency_pct),
            "trading_days": self.trading_days,
            "restricted": self.restricted,
            "passed": self.passed,
            "breached": self.breached,
            "trail_locked": self.trail_locked,
            "reason": self.reason,
            "timeline": [row.to_dict() for row in self.timeline],
        }


@dataclass(frozen=True)
class DataValidationReport:
    ok: bool
    format: str
    rows: int
    symbol: str
    timezone: str
    first_timestamp: str | None
    last_timestamp: str | None
    duplicate_rows: int
    missing_intervals: int
    out_of_order_rows: int
    invalid_price_rows: int
    invalid_volume_rows: int
    zero_volume_rows: int
    symbol_mismatch_rows: int
    expiration_errors: int
    expiration_before_bar_rows: int
    outside_permitted_session_rows: int
    incomplete_rth_sessions: int
    session_count: int
    contract_rollovers: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_frame(path: Path):
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required for market-data validation") from exc
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path), "csv"
    if suffix in {".parquet", ".pq"}:
        try:
            return pd.read_parquet(path), "parquet"
        except ImportError as exc:
            raise RuntimeError("Parquet import needs pyarrow or fastparquet") from exc
    raise ValueError("only CSV and Parquet market data are supported")


def validate_market_data(
    path: str | Path,
    *,
    expected_symbol: str,
    expected_interval_seconds: int = 60,
) -> DataValidationReport:
    """Validate imported bars without silently repairing or sorting evidence."""
    path = Path(path)
    frame, file_format = _read_frame(path)
    columns = {str(c).strip().lower(): c for c in frame.columns}
    timestamp_name = next((columns[key] for key in ("timestamp", "dt_utc", "datetime", "time") if key in columns), None)
    required = ["open", "high", "low", "close", "volume"]
    missing = [name for name in required if name not in columns]
    errors: list[str] = []
    warnings: list[str] = []
    if timestamp_name is None:
        errors.append("missing timestamp column (timestamp/dt_utc/datetime/time)")
    if missing:
        errors.append("missing required columns: " + ", ".join(missing))
    if errors:
        return DataValidationReport(
            False, file_format, len(frame), expected_symbol, "unknown", None, None,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            tuple(warnings), tuple(errors),
        )

    import pandas as pd
    raw_ts = frame[timestamp_name]
    raw_ts_text = raw_ts.astype(str).str.strip()
    explicit_zone = raw_ts_text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    parsed = pd.to_datetime(raw_ts, errors="coerce", utc=True)
    parse_bad = int(parsed.isna().sum())
    if parse_bad:
        errors.append(f"{parse_bad} timestamp(s) could not be parsed")
    timezone = "unknown"
    if not parsed.empty and not parsed.isna().all():
        timezone = "UTC"
        if bool((parsed.notna() & ~explicit_zone).any()):
            timezone = "naive"
            errors.append("timestamps have no timezone; explicit UTC/offset is required")

    out_of_order = 0
    duplicate_rows = 0
    missing_intervals = 0
    first = last = None
    outside_session_rows = 0
    incomplete_rth_sessions = 0
    session_count = 0
    if not parsed.isna().all():
        valid_ts = parsed.dropna()
        first = valid_ts.min().isoformat()
        last = valid_ts.max().isoformat()
        duplicate_rows = int(valid_ts.duplicated().sum())
        diffs = valid_ts.diff().dt.total_seconds()
        out_of_order = int((diffs < 0).sum())
        missing_intervals = int((diffs > expected_interval_seconds * 1.5).sum())
        if duplicate_rows:
            errors.append(f"{duplicate_rows} duplicate timestamp row(s)")
        if out_of_order:
            errors.append(f"{out_of_order} out-of-order timestamp transition(s)")
        if missing_intervals:
            warnings.append(f"{missing_intervals} interval gap(s) exceed {expected_interval_seconds} seconds")
        if timezone != "naive":
            ny = valid_ts.dt.tz_convert(NY)
            minute = ny.dt.hour * 60 + ny.dt.minute
            weekday = ny.dt.weekday
            permitted_clock = (minute >= 18 * 60) | (minute < 16 * 60 + 45)
            permitted_weekday = (
                ((weekday < 4) & permitted_clock)
                | ((weekday == 4) & (minute < 16 * 60 + 45))
                | ((weekday == 6) & (minute >= 18 * 60))
            )
            outside_session_rows = int((~(permitted_clock & permitted_weekday)).sum())
            if outside_session_rows:
                errors.append(f"{outside_session_rows} row(s) fall outside the conservative Lucid futures session")
            session_labels = ny.dt.date.astype(str)
            after_reopen = minute >= 18 * 60
            session_labels = pd.Series(
                pd.to_datetime(session_labels) + pd.to_timedelta(after_reopen.astype(int), unit="D"),
                index=valid_ts.index,
            ).dt.date.astype(str)
            session_count = int(session_labels.nunique())
            rth = (minute >= 9 * 60 + 30) & (minute < 16 * 60)
            if rth.any() and expected_interval_seconds == 60:
                rth_counts = pd.Series(1, index=valid_ts.index)[rth].groupby(session_labels[rth]).sum()
                incomplete_rth_sessions = int((rth_counts != 390).sum())
                if incomplete_rth_sessions:
                    warnings.append(
                        f"{incomplete_rth_sessions} RTH session(s) do not contain exactly 390 one-minute bars; early close, gap, or partial export must be resolved"
                    )

    numeric: dict[str, Any] = {}
    for name in required:
        numeric[name] = pd.to_numeric(frame[columns[name]], errors="coerce")
    invalid_price = (
        numeric["open"].isna() | numeric["high"].isna() | numeric["low"].isna() | numeric["close"].isna()
        | (numeric["open"] <= 0) | (numeric["high"] <= 0) | (numeric["low"] <= 0) | (numeric["close"] <= 0)
        | (numeric["low"] > numeric["high"])
        | (numeric["open"] < numeric["low"]) | (numeric["open"] > numeric["high"])
        | (numeric["close"] < numeric["low"]) | (numeric["close"] > numeric["high"])
    )
    invalid_price_rows = int(invalid_price.sum())
    invalid_volume_rows = int((numeric["volume"].isna() | (numeric["volume"] < 0)).sum())
    zero_volume_rows = int((numeric["volume"] == 0).sum())
    if invalid_price_rows:
        errors.append(f"{invalid_price_rows} row(s) have impossible OHLC prices")
    if invalid_volume_rows:
        errors.append(f"{invalid_volume_rows} row(s) have invalid volume")
    if zero_volume_rows:
        warnings.append(f"{zero_volume_rows} zero-volume row(s) require source-specific review")

    symbol_mismatch = 0
    if "symbol" in columns:
        symbols = frame[columns["symbol"]].astype(str).str.upper().str.strip()
        symbol_mismatch = int((symbols != expected_symbol.upper()).sum())
        if symbol_mismatch:
            errors.append(f"{symbol_mismatch} row(s) do not match {expected_symbol}")
    else:
        warnings.append("no symbol column; instrument identity must come from import metadata")

    expiration_errors = 0
    expiration_before_bar_rows = 0
    contract_rollovers = 0
    expiry_key = next((columns[key] for key in ("contract_expiration", "expiration", "expiry") if key in columns), None)
    if expiry_key is not None:
        expiry = pd.to_datetime(frame[expiry_key], errors="coerce")
        expiration_errors = int(expiry.isna().sum())
        if expiration_errors:
            errors.append(f"{expiration_errors} contract expiration value(s) are invalid")
        valid_expiry = expiry.notna() & parsed.notna()
        if valid_expiry.any():
            bar_dates = parsed[valid_expiry].dt.tz_convert(NY).dt.date
            expiry_dates = expiry[valid_expiry].dt.date
            expiration_before_bar_rows = int(sum(bar > exp for bar, exp in zip(bar_dates, expiry_dates)))
            if expiration_before_bar_rows:
                errors.append(f"{expiration_before_bar_rows} row(s) occur after contract expiration")
            contract_rollovers = max(0, int(expiry.dropna().astype(str).ne(expiry.dropna().astype(str).shift()).sum()) - 1)
            if contract_rollovers:
                warnings.append(f"{contract_rollovers} contract rollover transition(s) detected; adjustment policy must be supplied")
    else:
        warnings.append("no contract expiration column; continuous-contract rollover cannot be audited")

    return DataValidationReport(
        ok=not errors,
        format=file_format,
        rows=len(frame),
        symbol=expected_symbol,
        timezone=timezone,
        first_timestamp=first,
        last_timestamp=last,
        duplicate_rows=duplicate_rows,
        missing_intervals=missing_intervals,
        out_of_order_rows=out_of_order,
        invalid_price_rows=invalid_price_rows,
        invalid_volume_rows=invalid_volume_rows,
        zero_volume_rows=zero_volume_rows,
        symbol_mismatch_rows=symbol_mismatch,
        expiration_errors=expiration_errors,
        expiration_before_bar_rows=expiration_before_bar_rows,
        outside_permitted_session_rows=outside_session_rows,
        incomplete_rth_sessions=incomplete_rth_sessions,
        session_count=session_count,
        contract_rollovers=contract_rollovers,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def generated_daily_plan(rules: AccountRules, strategy: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate operating steps from rules/strategy instead of disconnected copy."""
    cutoff = strategy.get("forced_close_ny", "16:00")
    return [
        {"phase": "Before session", "checks": [
            "Confirm the selected Lucid program, stage and active MLL floor.",
            "Confirm a complete, current New York session and the exchange holiday schedule.",
            "Mark high-impact releases; do not open inside the strategy event filter.",
            "Reject stale/missing bars, symbol changes and rollover ambiguity.",
        ]},
        {"phase": "Before entry", "checks": [
            "Wait for the exact completed-bar setup; never anticipate the close.",
            "Recalculate whole-contract risk from stop ticks, commission, spread and slippage.",
            f"Respect the aggregate cap of {rules.max_minis} minis or {rules.max_micros} micros.",
            "Skip when the remaining MLL/DLL room cannot preserve the safety reserve.",
        ]},
        {"phase": "After each fill", "checks": [
            "Record gross P&L and every execution-cost component separately.",
            "Update balance, DLL usage, reserved stop risk and warning state.",
            "Stop new entries after the daily stop or consecutive-loss rule.",
        ]},
        {"phase": "End of day", "checks": [
            f"Flatten no later than {cutoff} New York (official outer cutoff {rules.forced_close_ny}).",
            "Advance the EOD MLL only from the qualifying session close.",
            "Review largest-day consistency where the selected program has it.",
            "Export the timeline and investigate every rejected/missed order before the next session.",
        ]},
    ]
