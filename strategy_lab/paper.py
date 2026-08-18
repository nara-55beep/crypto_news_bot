"""Live paper-trading desk: one persistent paper account per strategy.

This is the difference between the backtest tab and this one. A backtest answers
"what would this have done"; a desk account is *walked forward bar by bar* and its
balance, position and trade history persist across restarts, exactly like the bots
on the Paper Trading page.

Each account records the last bar it processed. A tick loads the shared price
history once and advances every account through whatever bars it has not seen yet.
The first tick therefore walks the whole history (which is what gives the account a
real equity curve, win rate and P&L immediately), and every tick after that advances
one bar at a time as new data arrives. It is the same code path either way, so a
freshly seeded account and a long-running one are not special cases of each other.

Fills use the same conservative contract as the backtest engine: a signal read from
a completed bar is filled at the NEXT bar's open, with half-spread, slippage and
commission charged. No broker order is ever placed.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import pandas as pd

from .catalog import Catalog, load_catalog
from .engine import CostModel
from .rules import run_rule
from .schema import TradingStrategy


START_BALANCE = 100_000.0
MAX_HISTORY = 40
MAX_LOG = 25
# A short's loss is unbounded, so the desk models a maintenance requirement the way
# a real margin account does: if equity falls under this share of the position's
# market value the position is force-closed instead of running the balance negative.
MAINTENANCE_MARGIN = 0.25
# Shorts are sized smaller than longs because the downside is not capped at 100%.
SHORT_FRACTION = 0.5


@dataclass
class PaperTrade:
    opened: str
    closed: str
    side: str
    shares: float
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    bars: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperAccount:
    """One strategy's paper account.  Serialisable so it survives a restart."""

    strategy_id: str
    balance: float = START_BALANCE
    position: int = 0
    shares: float = 0.0
    entry_price: float = 0.0
    entry_bar: str = ""
    entry_index: int = -1
    last_bar: str = ""
    processed: int = 0
    realized_pnl: float = 0.0
    commission_paid: float = 0.0
    edge_paid: float = 0.0
    wins: int = 0
    losses: int = 0
    peak_equity: float = START_BALANCE
    max_drawdown_pct: float = 0.0
    mark_price: float = 0.0
    started: str = ""
    error: str = ""
    busted: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    # ---------------------------------------------------------------- views
    @property
    def trades(self) -> int:
        return self.wins + self.losses

    def unrealized(self) -> float:
        if not self.position or not self.shares or not self.mark_price:
            return 0.0
        return (self.mark_price - self.entry_price) * self.shares * self.position

    def equity(self) -> float:
        return self.balance + self.unrealized()

    def position_value(self) -> float:
        return abs(self.shares * (self.mark_price or self.entry_price))

    def win_rate(self) -> float:
        return 100.0 * self.wins / self.trades if self.trades else 0.0

    def to_state(self, strategy: TradingStrategy) -> dict[str, Any]:
        equity = self.equity()
        open_position = None
        if self.position:
            open_position = {
                "side": "long" if self.position > 0 else "short",
                "shares": round(self.shares, 2),
                "entry": round(self.entry_price, 2),
                "since": self.entry_bar,
                "mark": round(self.mark_price, 2),
                "unrealized": round(self.unrealized(), 2),
            }
        return {
            "strategy_id": self.strategy_id,
            "name": strategy.display_name,
            "category": strategy.category,
            "direction": strategy.direction,
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "start_balance": START_BALANCE,
            "net_pnl": round(equity - START_BALANCE, 2),
            "net_pnl_pct": round((equity / START_BALANCE - 1.0) * 100.0, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized(), 2),
            "win_rate": round(self.win_rate(), 1),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "costs": round(self.commission_paid + self.edge_paid, 2),
            "position": open_position,
            "busted": self.busted,
            "bars_processed": self.processed,
            "last_bar": self.last_bar,
            "started": self.started,
            "error": self.error,
            "history": self.history[:MAX_HISTORY],
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["history"] = self.history[:MAX_HISTORY]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperAccount":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ------------------------------------------------------------ mechanics
    def advance(self, frame: pd.DataFrame, signal: pd.Series, costs: CostModel,
                fraction: float = 0.95) -> int:
        """Walk the account forward through every bar it has not processed yet."""
        opens = frame["open"].to_numpy(dtype=float)
        closes = frame["close"].to_numpy(dtype=float)
        target = signal.reindex(frame.index).fillna(0.0).clip(-1.0, 1.0).to_numpy(dtype=float)
        dates = [str(x)[:10] for x in frame.index]
        total = len(frame)
        if self.processed >= total:
            self.mark_price = float(closes[-1]) if total else 0.0
            return 0
        if not self.started and total:
            self.started = dates[0]

        moved = 0
        # Start at max(1, processed): bar 0 can never be acted on, because acting
        # requires a signal from the PREVIOUS bar.
        for bar in range(max(1, self.processed), total):
            self.mark_price = float(closes[bar])
            if self.busted:
                break
            # Margin call before any new decision: a short that has run against the
            # account is closed here rather than being allowed to go negative.
            if self.position and self.equity() < self.position_value() * MAINTENANCE_MARGIN:
                self._close(bar, float(opens[bar]), dates[bar], costs, "margin-call")
            wanted = int(math.copysign(1, target[bar - 1])) if target[bar - 1] else 0
            if wanted != self.position:
                if self.position:
                    self._close(bar, float(opens[bar]), dates[bar], costs, "signal")
                if wanted:
                    self._open(bar, float(opens[bar]), dates[bar], costs, wanted, fraction)
                moved += 1
            equity = self.equity()
            if equity <= 0:
                if self.position:
                    self._close(bar, float(closes[bar]), dates[bar], costs, "account-bust")
                self.balance = max(0.0, self.balance)
                self.busted = True
                self.last_bar = dates[bar]
                self.processed = total
                return moved
            self.peak_equity = max(self.peak_equity, equity)
            if self.peak_equity > 0:
                drop = (equity / self.peak_equity - 1.0) * 100.0
                self.max_drawdown_pct = min(self.max_drawdown_pct, drop)
            self.last_bar = dates[bar]
        self.processed = total
        return moved

    def _open(self, bar: int, price: float, date: str, costs: CostModel,
              side: int, fraction: float) -> None:
        fill = costs.fill_price(price, "buy" if side > 0 else "sell")
        usable = fraction if side > 0 else min(fraction, SHORT_FRACTION)
        quantity = math.floor(self.balance * usable / fill) if fill > 0 else 0
        if quantity <= 0:
            return
        fee = costs.commission(quantity, fill)
        if fee >= self.balance:
            return
        self.balance -= fee
        self.commission_paid += fee
        self.edge_paid += abs(fill - price) * quantity
        self.position, self.shares = side, float(quantity)
        self.entry_price, self.entry_bar, self.entry_index = fill, date, bar

    def _close(self, bar: int, price: float, date: str, costs: CostModel,
               reason: str) -> None:
        if not self.position or not self.shares:
            return
        fill = costs.fill_price(price, "sell" if self.position > 0 else "buy")
        fee = costs.commission(self.shares, fill)
        gross = (fill - self.entry_price) * self.shares * self.position
        net = gross - fee
        self.balance = max(0.0, self.balance + net)
        self.realized_pnl += net
        self.commission_paid += fee
        self.edge_paid += abs(fill - price) * self.shares
        risked = self.entry_price * self.shares
        if net > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.history.insert(0, PaperTrade(
            opened=self.entry_bar, closed=date,
            side="long" if self.position > 0 else "short",
            shares=round(self.shares, 2), entry=round(self.entry_price, 4),
            exit=round(fill, 4), pnl=round(net, 2),
            pnl_pct=round(100.0 * net / risked, 3) if risked else 0.0,
            bars=bar - self.entry_index, reason=reason,
        ).to_dict())
        del self.history[MAX_HISTORY:]
        self.position, self.shares, self.entry_price = 0, 0.0, 0.0
        self.entry_bar, self.entry_index = "", -1


class PaperDesk:
    """Every executable strategy, each with its own persistent paper account."""

    def __init__(self, catalog: Catalog | None = None, *, symbol: str = "SPY",
                 state_path: str | None = None, costs: CostModel | None = None) -> None:
        self.catalog = catalog or load_catalog()
        self.symbol = symbol.upper()
        self.costs = costs or CostModel()
        self.accounts: dict[str, PaperAccount] = {}
        self.enabled = True
        self.status = "not started"
        self.data_error = ""
        self.last_tick = 0.0
        self.last_bar = ""
        self.bars = 0
        self.ticks = 0
        self._lock = threading.RLock()
        self._path = state_path or self._default_path()
        self.load()

    # ------------------------------------------------------------ persistence
    @staticmethod
    def _default_path() -> str:
        try:
            import config
            base = getattr(config, "DATA_DIR", "data")
        except Exception:
            base = "data"
        return os.path.join(base, "strategy_lab_paper.json")

    def load(self) -> None:
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("symbol") != self.symbol:
                # A different instrument is a different experiment; do not silently
                # continue an SPY account against QQQ bars.
                return
            self.enabled = bool(data.get("enabled", True))
            self.ticks = int(data.get("ticks", 0))
            for row in data.get("accounts", []):
                account = PaperAccount.from_dict(row)
                if account.strategy_id in self.catalog.by_id:
                    self.accounts[account.strategy_id] = account
        except Exception as exc:
            self.data_error = f"paper state load failed: {type(exc).__name__}: {exc}"

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            temporary = self._path + ".tmp"
            payload = {
                "symbol": self.symbol,
                "enabled": self.enabled,
                "ticks": self.ticks,
                "saved_at": time.time(),
                "accounts": [a.to_dict() for a in self.accounts.values()],
            }
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except Exception as exc:
            self.data_error = f"paper state save failed: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ tick
    def account_for(self, strategy_id: str) -> PaperAccount:
        account = self.accounts.get(strategy_id)
        if account is None:
            account = PaperAccount(strategy_id=strategy_id)
            self.accounts[strategy_id] = account
        return account

    def tick(self, frame: pd.DataFrame, *, cancelled: Callable[[], bool] | None = None,
             save: bool = True) -> dict[str, Any]:
        """Advance every executable strategy's account over ``frame``."""
        with self._lock:
            if not self.enabled:
                self.status = "paused"
                return self.summary()
            if frame is None or len(frame) < 30:
                self.status = "waiting for market data"
                self.data_error = "not enough bars to trade"
                return self.summary()

            self.bars = len(frame)
            self.last_bar = str(frame.index[-1])[:10]
            advanced = failed = 0
            for strategy in self.catalog.executable():
                if cancelled is not None and cancelled():
                    break
                account = self.account_for(strategy.id)
                # Computing the rule is the expensive part, so skip it entirely when
                # this account has already seen every bar in the frame.
                if account.processed >= len(frame) or account.busted:
                    account.mark_price = float(frame["close"].iloc[-1])
                    continue
                try:
                    signal = run_rule(strategy.rule_id, frame, dict(strategy.default_parameters))
                    advanced += account.advance(frame, signal.position, self.costs)
                    account.error = ""
                except Exception as exc:
                    # One broken strategy must not stop the desk.
                    account.error = f"{type(exc).__name__}: {exc}"
                    failed += 1
            self.ticks += 1
            self.last_tick = time.time()
            self.data_error = ""
            self.status = (f"live · {len(self.accounts)} paper accounts on {self.symbol} "
                           f"· {self.bars} bars")
            if save:
                self.save()
            return self.summary(advanced=advanced, failed=failed)

    # --------------------------------------------------------------- reporting
    def summary(self, *, advanced: int = 0, failed: int = 0) -> dict[str, Any]:
        rows = self.rows()
        traded = [r for r in rows if r["trades"] > 0]
        profitable = [r for r in traded if r["net_pnl"] > 0]
        total_equity = sum(r["equity"] for r in rows)
        return {
            "running": True,
            "enabled": self.enabled,
            "paper_only": True,
            "live_order_routing": False,
            "name": f"Strategy Library paper desk · {self.symbol}",
            "status": self.status,
            "symbol": self.symbol,
            "timeframe": "1d",
            "bars": self.bars,
            "last_bar": self.last_bar,
            "ticks": self.ticks,
            "accounts": len(rows),
            "with_trades": len(traded),
            "profitable": len(profitable),
            "unprofitable": len(traded) - len(profitable),
            "in_position": sum(1 for r in rows if r["position"]),
            "busted": sum(1 for r in rows if r.get("busted")),
            "advanced": advanced,
            "failed": failed,
            "start_balance_each": START_BALANCE,
            "total_equity": round(total_equity, 2),
            "total_net_pnl": round(total_equity - START_BALANCE * len(rows), 2),
            "data_error": self.data_error,
            "costs": self.costs.to_dict(),
        }

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for strategy_id, account in self.accounts.items():
            strategy = self.catalog.by_id.get(strategy_id)
            if strategy is not None:
                out.append(account.to_state(strategy))
        out.sort(key=lambda r: -r["net_pnl"])
        return out

    def detail(self, strategy_id: str) -> dict[str, Any]:
        strategy = self.catalog.get(strategy_id)
        return self.account_for(strategy_id).to_state(strategy)

    # ----------------------------------------------------------------- control
    def set_enabled(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self.enabled = bool(on)
            self.status = "live" if self.enabled else "paused"
            self.save()
            return self.summary()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.accounts.clear()
            self.ticks = 0
            self.status = "reset"
            self.save()
            return self.summary()
