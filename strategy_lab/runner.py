"""Single and batch strategy execution.

Batch behaviour that matters:

* one strategy raising must not stop the batch — every failure is captured on
  that strategy's row and the run continues;
* the job is cancellable between strategies and inside a long replay;
* identical (strategy, parameters, data, config) work is served from cache;
* results are only ranked when the sample is large enough to mean anything.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

from .catalog import Catalog, load_catalog
from .engine import RunConfig, RunResult, run_backtest
from .rules import run_rule
from .schema import TradingStrategy, resolve_parameters


def data_fingerprint(frame: pd.DataFrame) -> str:
    """Identify the exact bars used, so a cache hit really is the same test."""
    if frame.empty:
        return "empty"
    head, tail = frame.index[0], frame.index[-1]
    closes = frame["close"]
    payload = f"{head}|{tail}|{len(frame)}|{float(closes.iloc[0]):.6f}|{float(closes.iloc[-1]):.6f}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def run_id_for(strategy_id: str, params: dict[str, Any], config: RunConfig, fingerprint: str) -> str:
    payload = json.dumps(
        {"s": strategy_id, "p": params, "c": config.to_dict(), "d": fingerprint},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def run_strategy(
    strategy: TradingStrategy,
    frame: pd.DataFrame,
    config: RunConfig,
    *,
    overrides: dict[str, Any] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RunResult:
    """Run one strategy.  Never raises for an ordinary strategy failure."""
    if not strategy.is_executable:
        missing = list(strategy.external_data_requirements) or list(strategy.data_requirements)
        return RunResult(
            ok=False, strategy_id=strategy.id, config=config.to_dict(),
            error=(strategy.unsupported_reason
                   or f"not executable here ({strategy.implementation_status}); needs: "
                      + "; ".join(missing)),
        )
    try:
        params = resolve_parameters(strategy, overrides)
        signal = run_rule(strategy.rule_id, frame, params)
        return run_backtest(
            frame, signal.position, config, strategy_id=strategy.id,
            stop_atr=signal.stop_atr,
            atr_stop_multiple=signal.atr_stop_multiple,
            take_profit_multiple=signal.take_profit_multiple,
            max_bars_held=signal.max_bars_held,
            cancelled=cancelled,
        )
    except Exception as exc:  # isolation: one bad strategy must not stop a batch
        return RunResult(ok=False, strategy_id=strategy.id, config=config.to_dict(),
                         error=f"{type(exc).__name__}: {exc}")


LEADERBOARD_FIELDS = (
    "net_return_pct", "annualised_return_pct", "max_drawdown_pct", "sharpe", "sortino",
    "calmar", "win_rate_pct", "profit_factor", "expectancy", "trades", "avg_bars_held",
    "commission_cost", "spread_slippage_cost", "total_cost", "exposure_pct", "turnover",
    "benchmark_return_pct", "excess_return_pct", "sample_sufficient",
)


@dataclass
class BatchJob:
    id: str
    created_at: float
    request: dict[str, Any]
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    cached: int = 0
    status: str = "queued"
    error: str = ""
    cancelled: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def progress(self) -> int:
        done = self.completed + self.failed + self.skipped
        return int(100 * done / self.total) if self.total else 0

    def summary(self, *, include_rows: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id, "status": self.status, "progress": self.progress,
            "total": self.total, "completed": self.completed, "failed": self.failed,
            "skipped": self.skipped, "cached": self.cached, "cancelled": self.cancelled,
            "error": self.error, "request": self.request,
        }
        if include_rows:
            data["rows"] = sorted(
                self.rows,
                key=lambda r: (
                    not r.get("sample_sufficient", False),
                    -(r.get("net_return_pct") or -1e12),
                ),
            )
        return data


class BatchRunner:
    """Runs every executable strategy against one dataset, with a result cache."""

    def __init__(self, catalog: Catalog | None = None, *, max_jobs: int = 8,
                 concurrency: int = 4, cache_size: int = 4000) -> None:
        self.catalog = catalog or load_catalog()
        self.jobs: dict[str, BatchJob] = {}
        self.max_jobs = max_jobs
        self.concurrency = max(1, concurrency)
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_size = cache_size

    # ------------------------------------------------------------------ cache
    def cache_get(self, key: str) -> dict[str, Any] | None:
        return self._cache.get(key)

    def cache_put(self, key: str, row: dict[str, Any]) -> None:
        if len(self._cache) >= self._cache_size:
            for old in list(self._cache)[: self._cache_size // 4]:
                self._cache.pop(old, None)
        self._cache[key] = row

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------- rows
    def _row(self, strategy: TradingStrategy, result: RunResult, run_id: str,
             cached: bool) -> dict[str, Any]:
        row: dict[str, Any] = {
            "strategy_id": strategy.id,
            "name": strategy.display_name,
            "category": strategy.category,
            "subcategory": strategy.subcategory,
            "version": strategy.version,
            "evidence_level": strategy.evidence_level,
            "run_id": run_id,
            "cached": cached,
            "ok": result.ok,
            "error": result.error,
            "warnings": result.warnings,
            "status": "completed" if result.ok else "failed",
        }
        for key in LEADERBOARD_FIELDS:
            row[key] = result.metrics.get(key)
        return row

    def run_batch_sync(
        self,
        frame: pd.DataFrame,
        config: RunConfig,
        *,
        strategies: Iterable[TradingStrategy] | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_row: Callable[[dict[str, Any]], None] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Synchronous batch used by tests and by the async job wrapper."""
        picked = list(strategies if strategies is not None else self.catalog.executable())
        if limit > 0:
            picked = picked[:limit]
        fingerprint = data_fingerprint(frame)
        rows: list[dict[str, Any]] = []
        for strategy in picked:
            if cancelled is not None and cancelled():
                break
            params = dict(strategy.default_parameters)
            run_id = run_id_for(strategy.id, params, config, fingerprint)
            hit = self.cache_get(run_id)
            if hit is not None:
                row = dict(hit, cached=True)
            else:
                result = run_strategy(strategy, frame, config, cancelled=cancelled)
                row = self._row(strategy, result, run_id, cached=False)
                self.cache_put(run_id, dict(row, cached=False))
            rows.append(row)
            if on_row is not None:
                on_row(row)
        return rows

    # ------------------------------------------------------------------- jobs
    async def start(self, frame: pd.DataFrame, config: RunConfig,
                    request: dict[str, Any], *, limit: int = 0) -> BatchJob:
        job_id = hashlib.sha256(
            f"{data_fingerprint(frame)}|{json.dumps(request, sort_keys=True, default=str)}|{time.time_ns()}"
            .encode()).hexdigest()[:16]
        job = BatchJob(id=job_id, created_at=time.time(), request=request)
        self.jobs[job_id] = job
        self._trim()
        job.task = asyncio.create_task(self._run(job, frame, config, limit))
        return job

    async def _run(self, job: BatchJob, frame: pd.DataFrame, config: RunConfig, limit: int) -> None:
        loop = asyncio.get_running_loop()
        picked = self.catalog.executable()
        if limit > 0:
            picked = picked[:limit]
        job.total = len(picked)
        job.status = "running"
        chunk_size = max(1, len(picked) // (self.concurrency * 4) or 1)
        chunks = [picked[i:i + chunk_size] for i in range(0, len(picked), chunk_size)]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def do_chunk(group: list[TradingStrategy]) -> list[dict[str, Any]]:
            async with semaphore:
                if job.cancelled:
                    return []
                # Replays are CPU-bound pandas/numpy work, so they go to threads;
                # the event loop stays responsive for progress polling and cancel.
                return await loop.run_in_executor(
                    None,
                    lambda: self.run_batch_sync(
                        frame, config, strategies=group, cancelled=lambda: job.cancelled),
                )

        try:
            for coro in asyncio.as_completed([do_chunk(c) for c in chunks]):
                rows = await coro
                for row in rows:
                    job.rows.append(row)
                    if row["ok"]:
                        job.completed += 1
                    else:
                        job.failed += 1
                    if row.get("cached"):
                        job.cached += 1
            if job.cancelled:
                job.status = "cancelled"
                job.skipped = max(0, job.total - job.completed - job.failed)
            else:
                job.status = "completed"
        except asyncio.CancelledError:
            job.cancelled, job.status = True, "cancelled"
            raise
        except Exception as exc:
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

    def get(self, job_id: str) -> BatchJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise KeyError("batch run not found") from exc

    def cancel(self, job_id: str) -> BatchJob:
        job = self.get(job_id)
        if job.status in {"queued", "running"}:
            job.cancelled = True
            job.status = "cancelled"
        return job

    def _trim(self) -> None:
        while len(self.jobs) > self.max_jobs:
            oldest = sorted(self.jobs.values(), key=lambda j: j.created_at)
            for job in oldest:
                if job.status in {"completed", "error", "cancelled"}:
                    self.jobs.pop(job.id, None)
                    break
            else:
                self.jobs.pop(oldest[0].id, None)
