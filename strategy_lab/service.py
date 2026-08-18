"""Framework-agnostic application service for the Strategy Library.

Holds the search index, the filter/sort/paginate logic and the market-data
loader.  The web layer stays a thin translation of HTTP to these calls, which is
what makes the whole feature testable without a server.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .catalog import RESEARCH_DATE, load_catalog
from .engine import CostModel, RunConfig
from .paper import PaperDesk
from .rules import RULES
from .runner import BatchRunner, run_id_for, run_strategy, data_fingerprint
from .schema import StrategyValidationError, resolve_parameters


DEFAULT_SYMBOL = "SPY"
MAX_PAGE_SIZE = 200
CACHE_TTL_SECONDS = 60 * 30


class MarketDataError(RuntimeError):
    """Raised when usable bars cannot be obtained."""


@dataclass
class _CachedFrame:
    frame: pd.DataFrame
    stamp: float


class MarketDataLoader:
    """Daily OHLCV via yfinance, cached in memory.

    Prices are split- and dividend-adjusted by the provider, which is what makes
    a long backtest comparable; the trade-off is that historical fills are stated
    in adjusted rather than as-traded prices, and that is recorded on the result.
    """

    def __init__(self, ttl: int = CACHE_TTL_SECONDS) -> None:
        self._cache: dict[str, _CachedFrame] = {}
        self._lock = threading.Lock()
        self.ttl = ttl

    def load(self, symbol: str, start: str = "", end: str = "",
             interval: str = "1d") -> pd.DataFrame:
        symbol = (symbol or DEFAULT_SYMBOL).strip().upper()
        if not symbol or len(symbol) > 12 or not symbol.replace(".", "").replace("-", "").isalnum():
            raise MarketDataError(f"invalid symbol: {symbol!r}")
        key = f"{symbol}|{start}|{end}|{interval}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and (time.time() - hit.stamp) < self.ttl:
                return hit.frame.copy()
        frame = self._download(symbol, start, end, interval)
        with self._lock:
            self._cache[key] = _CachedFrame(frame, time.time())
        return frame.copy()

    def _download(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        try:
            import yfinance
        except ImportError as exc:
            raise MarketDataError(
                "yfinance is not installed; run: pip install -r requirements.txt"
            ) from exc
        kwargs: dict[str, Any] = {"interval": interval, "auto_adjust": True, "progress": False}
        if start:
            kwargs["start"] = start
        if end:
            kwargs["end"] = end
        if not start and not end:
            kwargs["period"] = "10y"
        try:
            raw = yfinance.download(symbol, **kwargs)
        except Exception as exc:
            raise MarketDataError(f"market data download failed: {type(exc).__name__}: {exc}") from exc
        return self.normalise(raw, symbol)

    @staticmethod
    def normalise(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if raw is None or len(raw) == 0:
            raise MarketDataError(f"no bars returned for {symbol}")
        frame = raw.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        wanted = ["open", "high", "low", "close", "volume"]
        missing = [c for c in wanted if c not in frame.columns]
        if missing:
            raise MarketDataError(f"{symbol}: market data is missing {missing}")
        frame = frame[wanted].apply(pd.to_numeric, errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = frame["volume"].fillna(0.0)
        frame = frame[~frame.index.duplicated(keep="first")].sort_index()
        # Reject bars that cannot exist rather than silently trading them.
        valid = (
            (frame["low"] <= frame["high"])
            & (frame["open"].between(frame["low"], frame["high"]))
            & (frame["close"].between(frame["low"], frame["high"]))
            & (frame[wanted[:4]] > 0).all(axis=1)
        )
        dropped = int((~valid).sum())
        frame = frame[valid]
        if len(frame) < 30:
            raise MarketDataError(
                f"{symbol}: only {len(frame)} usable bars after validation "
                f"({dropped} impossible bars removed); at least 30 are required"
            )
        frame.attrs["dropped_bars"] = dropped
        frame.attrs["symbol"] = symbol
        return frame


def _cost_model(payload: dict[str, Any]) -> CostModel:
    base = CostModel()
    return CostModel(
        commission_per_share=float(payload.get("commission_per_share", base.commission_per_share)),
        commission_minimum=float(payload.get("commission_minimum", base.commission_minimum)),
        spread_bps=float(payload.get("spread_bps", base.spread_bps)),
        slippage_bps=float(payload.get("slippage_bps", base.slippage_bps)),
        short_borrow_bps_annual=float(
            payload.get("short_borrow_bps_annual", base.short_borrow_bps_annual)),
    )


def build_config(payload: dict[str, Any]) -> RunConfig:
    sizing = str(payload.get("sizing", "fixed-fraction"))
    if sizing not in {"fixed-fraction", "volatility-target", "fixed-shares"}:
        raise ValueError(f"unsupported sizing mode: {sizing}")
    capital = float(payload.get("starting_capital", 100_000))
    if capital <= 0:
        raise ValueError("starting capital must be positive")
    return RunConfig(
        symbol=str(payload.get("symbol", DEFAULT_SYMBOL)).strip().upper(),
        start=str(payload.get("start", "")),
        end=str(payload.get("end", "")),
        timeframe=str(payload.get("timeframe", "1d")),
        starting_capital=capital,
        costs=_cost_model(payload),
        allow_long=bool(payload.get("allow_long", True)),
        allow_short=bool(payload.get("allow_short", True)),
        sizing=sizing,  # type: ignore[arg-type]
        position_fraction=float(payload.get("position_fraction", 0.95)),
        target_volatility=float(payload.get("target_volatility", 0.15)),
        fixed_shares=int(payload.get("fixed_shares", 100)),
        max_position_fraction=float(payload.get("max_position_fraction", 1.0)),
    )


SORT_KEYS = {
    # Default: what someone can actually run comes first, then category. Sorting
    # by category alone buried all 614 runnable entries under the academic ones.
    "relevance": lambda s: (0 if s.implementation_status == "executable" else 1,
                            s.category, s.subcategory, s.display_name.lower()),
    "name": lambda s: s.display_name.lower(),
    "category": lambda s: (s.category, s.subcategory, s.display_name.lower()),
    "age": lambda s: (s.origin_year or 9999, s.display_name.lower()),
    "newest": lambda s: (-(s.origin_year or 0), s.display_name.lower()),
    "status": lambda s: (s.implementation_status, s.display_name.lower()),
    "evidence": lambda s: (s.evidence_level, s.display_name.lower()),
    "complexity": lambda s: (s.complexity, s.display_name.lower()),
}


class StrategyLabService:
    PAPER_MIN_INTERVAL = 30.0   # seconds between automatic desk ticks

    def __init__(self, paper_symbol: str = DEFAULT_SYMBOL) -> None:
        self.catalog = load_catalog()
        self.data = MarketDataLoader()
        self.runner = BatchRunner(self.catalog)
        self.desk = PaperDesk(self.catalog, symbol=paper_symbol)
        self._paper_lock = threading.Lock()
        self._paper_ticked = 0.0

    # ------------------------------------------------------------- catalog
    def overview(self) -> dict[str, Any]:
        stats = self.catalog.stats()
        stats["rule_engines"] = len(RULES)
        return {
            "ok": True,
            "page_version": "strategy_lab_v1",
            "research_date": RESEARCH_DATE,
            "stats": stats,
            "facets": self.facets(),
            "default_symbol": DEFAULT_SYMBOL,
            "default_costs": CostModel().to_dict(),
            "disclaimer": (
                "Paper trading and historical replay only. No broker order can be placed from "
                "this page. A positive backtest is not evidence that a strategy will make money."
            ),
        }

    def facets(self) -> dict[str, list[str]]:
        def uniq(values: Any) -> list[str]:
            return sorted({v for v in values if v})
        return {
            "category": uniq(s.category for s in self.catalog.strategies),
            "subcategory": uniq(s.subcategory for s in self.catalog.strategies),
            "implementation_status": uniq(s.implementation_status for s in self.catalog.strategies),
            "evidence_level": uniq(s.evidence_level for s in self.catalog.strategies),
            "direction": uniq(s.direction for s in self.catalog.strategies),
            "complexity": uniq(s.complexity for s in self.catalog.strategies),
            "trade_frequency": uniq(s.trade_frequency for s in self.catalog.strategies),
            "timeframe": uniq(t for s in self.catalog.strategies for t in s.timeframes),
            "tag": uniq(t for s in self.catalog.strategies for t in s.tags)[:120],
        }

    def browse(self, query: dict[str, Any]) -> dict[str, Any]:
        items = self.catalog.strategies
        text = str(query.get("q", "")).strip().lower()
        if text:
            terms = [t for t in text.split() if t]
            index = self.catalog.search_index
            items = [s for s in items if all(t in index(s.id) for t in terms)]
        for field in ("category", "subcategory", "implementation_status", "evidence_level",
                      "direction", "complexity", "trade_frequency"):
            wanted = str(query.get(field, "")).strip()
            if wanted:
                items = [s for s in items if getattr(s, field) == wanted]
        timeframe = str(query.get("timeframe", "")).strip()
        if timeframe:
            items = [s for s in items if timeframe in s.timeframes]
        tag = str(query.get("tag", "")).strip().lower()
        if tag:
            items = [s for s in items if tag in [t.lower() for t in s.tags]]
        if str(query.get("executable_only", "")).lower() in {"1", "true", "yes", "on"}:
            items = [s for s in items if s.is_executable]

        sort = str(query.get("sort", "relevance"))
        items = sorted(items, key=SORT_KEYS.get(sort, SORT_KEYS["relevance"]))

        page = max(1, int(query.get("page", 1) or 1))
        size = min(MAX_PAGE_SIZE, max(1, int(query.get("page_size", 50) or 50)))
        total = len(items)
        start = (page - 1) * size
        window = items[start:start + size]
        return {
            "ok": True, "total": total, "page": page, "page_size": size,
            "pages": max(1, (total + size - 1) // size),
            "items": [s.summary_dict() for s in window],
        }

    def detail(self, strategy_id: str) -> dict[str, Any]:
        resolved = self.catalog.resolve_alias(strategy_id) or strategy_id
        strategy = self.catalog.get(resolved)
        return {"ok": True, "strategy": strategy.to_dict()}

    # ----------------------------------------------------------------- runs
    def run_one(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy_id = str(payload.get("strategy_id", "")).strip()
        if not strategy_id:
            raise ValueError("strategy_id is required")
        strategy = self.catalog.get(self.catalog.resolve_alias(strategy_id) or strategy_id)
        config = build_config(payload)
        overrides = payload.get("parameters") or {}
        if not isinstance(overrides, dict):
            raise ValueError("parameters must be an object")
        if strategy.is_executable:
            try:
                resolve_parameters(strategy, overrides)
            except StrategyValidationError as exc:
                raise ValueError(str(exc)) from exc
        frame = self.data.load(config.symbol, config.start, config.end, config.timeframe)
        result = run_strategy(strategy, frame, config, overrides=overrides)
        payload_out = result.to_dict()
        payload_out["run_id"] = run_id_for(
            strategy.id, dict(overrides), config, data_fingerprint(frame))
        payload_out["provenance"] = self._provenance(strategy, frame, config)
        return {"ok": result.ok, "result": payload_out}

    def _provenance(self, strategy: Any, frame: pd.DataFrame, config: RunConfig) -> dict[str, Any]:
        """Everything a reader needs to judge whether a number means anything."""
        return {
            "data_source": "yfinance daily bars, split and dividend adjusted",
            "symbol": config.symbol,
            "bars": len(frame),
            "first_bar": str(frame.index[0])[:10] if len(frame) else None,
            "last_bar": str(frame.index[-1])[:10] if len(frame) else None,
            "dropped_invalid_bars": int(frame.attrs.get("dropped_bars", 0)),
            "timeframe": config.timeframe,
            "starting_capital": config.starting_capital,
            "costs": config.costs.to_dict(),
            "sizing": config.sizing,
            "benchmark": "buy and hold the same symbol over the same window",
            "strategy_version": getattr(strategy, "version", "1.0.0"),
            "result_kind": "historical backtest",
            "adjustment_note": (
                "Adjusted prices make long windows comparable but differ from the prices that "
                "traded on the day."
            ),
        }

    async def start_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = build_config(payload)
        frame = self.data.load(config.symbol, config.start, config.end, config.timeframe)
        limit = int(payload.get("limit", 0) or 0)
        request = {
            "symbol": config.symbol, "start": config.start, "end": config.end,
            "timeframe": config.timeframe, "starting_capital": config.starting_capital,
            "limit": limit, "bars": len(frame),
            "costs": config.costs.to_dict(),
        }
        job = await self.runner.start(frame, config, request, limit=limit)
        return {"ok": True, **job.summary(include_rows=False)}

    def batch_state(self, job_id: str, *, include_rows: bool = True) -> dict[str, Any]:
        return {"ok": True, **self.runner.get(job_id).summary(include_rows=include_rows)}

    def cancel_batch(self, job_id: str) -> dict[str, Any]:
        return {"ok": True, **self.runner.cancel(job_id).summary(include_rows=False)}

    # ------------------------------------------------------------ paper desk
    def paper_tick(self, *, force: bool = False) -> dict[str, Any]:
        """Advance every paper account over the latest bars.

        Rate-limited so page polling cannot hammer the data provider; the desk
        short-circuits anyway when no account has an unprocessed bar.
        """
        with self._paper_lock:
            fresh = (time.time() - self._paper_ticked) >= self.PAPER_MIN_INTERVAL
            if not force and not fresh:
                return self.desk.summary()
            try:
                frame = self.data.load(self.desk.symbol)
            except MarketDataError as exc:
                self.desk.data_error = str(exc)
                self.desk.status = "market data unavailable"
                return self.desk.summary()
            self._paper_ticked = time.time()
            return self.desk.tick(frame)

    def paper_state(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        """Desk summary plus a filtered, paginated slice of the accounts."""
        query = query or {}
        summary = self.paper_tick()
        rows = self.desk.rows()

        text = str(query.get("q", "")).strip().lower()
        if text:
            rows = [r for r in rows if text in r["name"].lower()
                    or text in r["strategy_id"].lower() or text in r["category"].lower()]
        category = str(query.get("category", "")).strip()
        if category:
            rows = [r for r in rows if r["category"] == category]
        view = str(query.get("view", "all")).strip()
        if view == "profitable":
            rows = [r for r in rows if r["net_pnl"] > 0]
        elif view == "unprofitable":
            rows = [r for r in rows if r["net_pnl"] < 0]
        elif view == "in-position":
            rows = [r for r in rows if r["position"]]
        elif view == "traded":
            rows = [r for r in rows if r["trades"] > 0]
        elif view == "idle":
            rows = [r for r in rows if r["trades"] == 0]

        sort = str(query.get("sort", "net_pnl"))
        keys = {"net_pnl": lambda r: -r["net_pnl"], "equity": lambda r: -r["equity"],
                "win_rate": lambda r: -r["win_rate"], "trades": lambda r: -r["trades"],
                "drawdown": lambda r: r["max_drawdown_pct"], "name": lambda r: r["name"].lower()}
        rows = sorted(rows, key=keys.get(sort, keys["net_pnl"]))

        page = max(1, int(query.get("page", 1) or 1))
        size = min(MAX_PAGE_SIZE, max(1, int(query.get("page_size", 60) or 60)))
        total = len(rows)
        start = (page - 1) * size
        return {"ok": True, **summary, "total": total, "page": page, "page_size": size,
                "pages": max(1, (total + size - 1) // size),
                "rows": [dict(r, history=r["history"][:5]) for r in rows[start:start + size]]}

    def paper_detail(self, strategy_id: str) -> dict[str, Any]:
        resolved = self.catalog.resolve_alias(strategy_id) or strategy_id
        return {"ok": True, "account": self.desk.detail(resolved)}

    def paper_toggle(self, enabled: bool) -> dict[str, Any]:
        return {"ok": True, **self.desk.set_enabled(enabled)}

    def paper_reset(self) -> dict[str, Any]:
        self._paper_ticked = 0.0
        return {"ok": True, **self.desk.reset()}

    def compare(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids = payload.get("strategy_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("strategy_ids must be a non-empty list")
        if len(ids) > 8:
            raise ValueError("compare at most 8 strategies at a time")
        config = build_config(payload)
        frame = self.data.load(config.symbol, config.start, config.end, config.timeframe)
        out = []
        for strategy_id in ids:
            strategy = self.catalog.get(self.catalog.resolve_alias(str(strategy_id)) or str(strategy_id))
            result = run_strategy(strategy, frame, config)
            out.append({
                "strategy_id": strategy.id, "name": strategy.display_name,
                "category": strategy.category, "ok": result.ok, "error": result.error,
                "metrics": result.metrics,
                "equity_curve": result.equity_curve[:: max(1, len(result.equity_curve) // 400)],
                "warnings": result.warnings,
            })
        return {"ok": True, "symbol": config.symbol, "bars": len(frame), "results": out}
