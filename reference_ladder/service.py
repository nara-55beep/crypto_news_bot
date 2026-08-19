"""Background-job service for Reference Ladder research runs."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import LadderConfig
from .data import BinanceMinuteLoader
from .research import run_research


class ReferenceLadderService:
    def __init__(self) -> None:
        self.loader = BinanceMinuteLoader()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reference-ladder")
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.output_dir = Path("output/reference_ladder")

    def config(self) -> dict[str, Any]:
        return {"ok": True, "config": LadderConfig().to_dict()}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            active = next((job for job in self.jobs.values() if job["status"] in {"queued", "running"}), None)
            if active:
                return {"ok": True, "job_id": active["id"], "status": active["status"],
                        "reused": True}
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id, "status": "queued", "created_at": time.time(),
                "started_at": None, "finished_at": None, "error": "", "result": None,
            }
            self.jobs[job_id] = job
            future = self.executor.submit(self._execute, job_id, dict(payload))
            job["future"] = future
            return {"ok": True, "job_id": job_id, "status": "queued", "reused": False}

    def _execute(self, job_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = time.time()
        try:
            overrides = payload.get("config") or {}
            if not isinstance(overrides, dict):
                raise ValueError("config must be an object")
            config = LadderConfig().with_overrides(overrides)
            frame = self.loader.load(
                str(payload.get("start") or "2018-01-01"),
                str(payload.get("end") or ""),
                refresh=bool(payload.get("refresh", False)),
            )
            result = run_research(frame, config, full=bool(payload.get("full", True)))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.output_dir / "latest.json.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.output_dir / "latest.json")
            with self._lock:
                job = self.jobs[job_id]
                job["result"] = result
                job["status"] = "complete"
                job["finished_at"] = time.time()
        except Exception as exc:
            with self._lock:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["finished_at"] = time.time()

    def state(self, job_id: str, *, include_result: bool = True) -> dict[str, Any]:
        with self._lock:
            if job_id not in self.jobs:
                raise KeyError("Reference Ladder job not found")
            job = self.jobs[job_id]
            result = {
                "ok": job["status"] != "failed", "job_id": job_id,
                "status": job["status"], "created_at": job["created_at"],
                "started_at": job["started_at"], "finished_at": job["finished_at"],
                "error": job["error"],
            }
            if include_result and job["status"] == "complete":
                result["result"] = job["result"]
            return result

    def latest(self) -> dict[str, Any]:
        path = self.output_dir / "latest.json"
        if not path.exists():
            return {"ok": True, "result": None}
        with open(path, encoding="utf-8") as handle:
            return {"ok": True, "result": json.load(handle)}
