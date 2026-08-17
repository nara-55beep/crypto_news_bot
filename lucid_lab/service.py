"""Framework-agnostic application service for the Lucid Strategy Lab."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .engine import (
    EXECUTION_PRESETS,
    ComplianceMonitor,
    ComplianceTrade,
    PositionSizeInput,
    calculate_position_size,
    generated_daily_plan,
    plan_evaluation,
)
from .rules import (
    INSTRUMENTS,
    RULES_LAST_CHECKED,
    get_account_rules,
    official_sources,
    public_evaluation_options,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_PATH = ROOT / "research" / "ta_strat" / "results" / "lucid_lab_validation.json"


class EvidenceError(RuntimeError):
    pass


class EvidenceStore:
    def __init__(self, path: str | Path = DEFAULT_EVIDENCE_PATH):
        self.path = Path(path)
        self._mtime_ns = -1
        self._data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise EvidenceError(f"Lucid validation artifact is unavailable: {exc}") from exc
        if self._data is not None and stat.st_mtime_ns == self._mtime_ns:
            return self._data
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"Lucid validation artifact cannot be read: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise EvidenceError("Lucid validation artifact has an unsupported schema")
        if data.get("status") not in {"EXPERIMENTAL_PROXY", "NO_GO", "VALIDATED"}:
            raise EvidenceError("Lucid validation artifact has an invalid status")
        required = (
            "run_id", "selected", "data", "horizons", "stresses", "candidates", "charts",
            "account_comparison", "monte_carlo", "walk_forward", "split_results",
            "sensitivity", "signal_sensitivity", "trade_statistics", "risk_controls",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise EvidenceError("Lucid validation artifact is incomplete: " + ", ".join(missing))
        self._data = data
        self._mtime_ns = stat.st_mtime_ns
        return data


def _moment(value: Any) -> datetime:
    """Parse an ISO timestamp, insisting on an explicit timezone."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {value!r} needs an explicit timezone")
    return moment


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class LucidLabService:
    def __init__(self, evidence_path: str | Path = DEFAULT_EVIDENCE_PATH):
        self.evidence = EvidenceStore(evidence_path)

    def snapshot(
        self,
        *,
        program: str = "lucidpro",
        stage: str = "evaluation",
        size: int = 25_000,
        daily_drawdown: str = "eod",
        daily_loss_enabled: bool = True,
    ) -> dict[str, Any]:
        rules = get_account_rules(
            program,
            stage,
            size,
            daily_drawdown=daily_drawdown,
            daily_loss_enabled=daily_loss_enabled,
        )
        evidence = self.evidence.load()
        applies = (
            rules.program_id == "lucidpro"
            and rules.stage == "evaluation"
            and rules.drawdown_type == "eod"
            and rules.account_size in {25_000, 50_000, 100_000, 150_000}
        )
        selected = evidence["selected"]
        daily_plan = generated_daily_plan(rules, selected)
        return {
            "ok": True,
            "page_version": "lucid_strategy_lab_v1",
            "rules_last_checked": RULES_LAST_CHECKED,
            "program_options": public_evaluation_options(),
            "account": rules.to_dict(),
            "instruments": [instrument.to_dict() for instrument in INSTRUMENTS.values()],
            "execution_presets": [preset.to_dict() for preset in EXECUTION_PRESETS.values()],
            "evidence": evidence,
            "evidence_applies": applies,
            "evidence_scope_note": (
                "The historical artifact directly models this LucidPro EOD evaluation size."
                if applies else
                "Rules and sizing are available, but the historical pass statistics do not validate this program/drawdown selection."
            ),
            "daily_plan": daily_plan,
            "official_sources": official_sources(),
            "disclaimer": "Research evidence, not a promise of profit or an evaluation pass. No order-routing capability is present.",
        }

    def position_size(self, payload: dict[str, Any]) -> dict[str, Any]:
        rules = get_account_rules(
            payload.get("program", "lucidpro"),
            payload.get("stage", "evaluation"),
            int(payload.get("account_size", 25_000)),
            daily_drawdown=payload.get("daily_drawdown", "eod"),
            daily_loss_enabled=_bool(payload.get("daily_loss_enabled"), True),
        )
        current = Decimal(str(payload.get("current_balance", rules.starting_balance)))
        floor = Decimal(str(payload.get("drawdown_floor", rules.starting_balance - rules.max_loss)))
        daily_raw = payload.get("daily_loss_remaining")
        daily = None if daily_raw in (None, "", "null") else Decimal(str(daily_raw))
        values = PositionSizeInput(
            instrument=str(payload.get("instrument", "MNQ")),
            current_balance=current,
            drawdown_floor=floor,
            daily_loss_remaining=daily,
            stop_ticks=Decimal(str(payload.get("stop_ticks", "40"))),
            selected_risk_budget=Decimal(str(payload.get("risk_budget", "400"))),
            safety_reserve=Decimal(str(payload.get("safety_reserve", "100"))),
            open_micro_equivalents=int(payload.get("open_micro_equivalents", 0)),
            execution_preset=str(payload.get("execution_preset", "normal")),
            committed_stop_risk=Decimal(str(payload.get("committed_stop_risk", "0"))),
            committed_stop_risk_defaulted="committed_stop_risk" not in payload,
        )
        return {"ok": True, "result": calculate_position_size(values, rules).to_dict()}

    def _rules_from(self, payload: dict[str, Any]):
        return get_account_rules(
            payload.get("program", "lucidpro"),
            payload.get("stage", "evaluation"),
            int(payload.get("account_size", 25_000)),
            daily_drawdown=payload.get("daily_drawdown", "eod"),
            daily_loss_enabled=_bool(payload.get("daily_loss_enabled"), True),
        )

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Answer what is still required and what may still be risked."""
        rules = self._rules_from(payload)
        result = plan_evaluation(
            rules,
            current_balance=payload.get("current_balance", rules.starting_balance),
            drawdown_floor=payload.get(
                "drawdown_floor", rules.starting_balance - rules.max_loss
            ),
            sessions_remaining=int(payload.get("sessions_remaining", 20)),
            trades_per_session=int(payload.get("trades_per_session", 3)),
            loss_streak_tolerance=int(payload.get("loss_streak_tolerance", 3)),
            reward_to_risk=payload.get("reward_to_risk", 2),
            safety_reserve=payload.get("safety_reserve", 100),
            daily_loss_used=payload.get("daily_loss_used", 0),
        )
        return {"ok": True, "result": result.to_dict()}

    def compliance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Score supplied trades and order times against prohibited activity."""
        rules = self._rules_from(payload)
        monitor = ComplianceMonitor()
        for row in payload.get("trades") or []:
            if not isinstance(row, dict):
                raise ValueError("each trade must be an object")
            try:
                monitor.record_trade(ComplianceTrade(
                    entry=_moment(row["entry"]),
                    exit=_moment(row["exit"]),
                    net_pnl=Decimal(str(row.get("net_pnl", 0))),
                    instrument=str(row.get("instrument", "")),
                    side="short" if str(row.get("side", "long")).lower() == "short" else "long",
                ))
            except KeyError as exc:
                raise ValueError(f"trade is missing {exc.args[0]}") from exc
        for stamp in payload.get("order_times") or []:
            monitor.record_order(_moment(stamp))
        return {"ok": True, "result": monitor.report(rules)}


@dataclass
class SimulationJob:
    id: str
    created_at: float
    request: dict[str, Any]
    status: str = "queued"
    progress: int = 0
    result: dict[str, Any] | None = None
    error: str = ""
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "request": self.request,
            "result": self.result,
            "error": self.error,
            "cancelled": self.cancelled,
        }


class SimulationRegistry:
    """Small cancellable registry for deterministic historical scenario replays.

    The expensive research stays offline.  A runtime job selects and verifies a
    source-stamped scenario from that artifact; it never manufactures new evidence.
    """

    def __init__(self, service: LucidLabService, max_jobs: int = 25):
        self.service = service
        self.max_jobs = max_jobs
        self.jobs: dict[str, SimulationJob] = {}

    async def start(self, request: dict[str, Any]) -> dict[str, Any]:
        preset = str(request.get("execution_preset", "normal"))
        if preset not in EXECUTION_PRESETS:
            raise ValueError(f"unsupported execution preset: {preset}")
        evidence = self.service.evidence.load()
        account_size = int(request.get("account_size", 25_000))
        if account_size not in {25_000, 50_000, 100_000, 150_000}:
            raise ValueError("unsupported account size")
        digest = hashlib.sha256(
            f"{evidence['run_id']}|{preset}|{account_size}|{time.time_ns()}".encode()
        ).hexdigest()[:16]
        job = SimulationJob(digest, time.time(), {"execution_preset": preset, "account_size": account_size})
        self.jobs[job.id] = job
        self._trim()
        job.task = asyncio.create_task(self._run(job))
        return job.to_dict()

    async def _run(self, job: SimulationJob) -> None:
        try:
            job.status, job.progress = "running", 10
            await asyncio.sleep(0)
            if job.cancelled:
                return
            evidence = self.service.evidence.load()
            job.progress = 55
            await asyncio.sleep(0)
            if job.cancelled:
                return
            preset = job.request["execution_preset"]
            scenario = next(row for row in evidence["stresses"] if row["id"] == preset)
            account = next(
                row for row in evidence["account_comparison"]
                if row["account_size"] == f"{job.request['account_size'] // 1000}K"
            )
            job.result = {
                "source_run_id": evidence["run_id"],
                "scenario": scenario,
                "account_comparison": account,
                "reproducible": True,
                "note": "Selected from the immutable historical artifact; no live market data or synthetic evidence was used.",
            }
            job.progress, job.status = 100, "completed"
        except asyncio.CancelledError:
            job.cancelled, job.status = True, "cancelled"
            job.progress = min(job.progress, 99)
        except Exception as exc:  # surfaced to API; never swallowed
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "error"

    def get(self, job_id: str) -> dict[str, Any]:
        try:
            return self.jobs[job_id].to_dict()
        except KeyError as exc:
            raise KeyError("simulation run not found") from exc

    def cancel(self, job_id: str) -> dict[str, Any]:
        try:
            job = self.jobs[job_id]
        except KeyError as exc:
            raise KeyError("simulation run not found") from exc
        if job.status not in {"completed", "error", "cancelled"}:
            job.cancelled, job.status = True, "cancelled"
            if job.task is not None:
                job.task.cancel()
        return job.to_dict()

    def _trim(self) -> None:
        if len(self.jobs) <= self.max_jobs:
            return
        removable = sorted(self.jobs.values(), key=lambda row: row.created_at)
        for job in removable:
            if len(self.jobs) <= self.max_jobs:
                break
            if job.status in {"completed", "error", "cancelled"}:
                self.jobs.pop(job.id, None)
