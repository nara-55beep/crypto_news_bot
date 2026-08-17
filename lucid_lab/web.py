"""aiohttp route integration for the Lucid Strategy Lab."""
from __future__ import annotations

from pathlib import Path
import tempfile

from aiohttp import web

from .engine import validate_market_data
from .page import LUCID_LAB_HTML
from .service import EvidenceError, LucidLabService, SimulationRegistry


SERVICE = LucidLabService()
SIMULATIONS = SimulationRegistry(SERVICE)
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PAPER_BOT = None


def set_paper_bot(bot) -> None:
    """Attach the paper-only runtime created by ``main.py``."""
    global PAPER_BOT
    PAPER_BOT = bot


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def page(request: web.Request) -> web.Response:
    return web.Response(text=LUCID_LAB_HTML, content_type="text/html", headers=NO_CACHE)


async def snapshot(request: web.Request) -> web.Response:
    query = request.query
    try:
        data = SERVICE.snapshot(
            program=query.get("program", "lucidpro"),
            stage=query.get("stage", "evaluation"),
            size=int(query.get("size", "25000")),
            daily_drawdown=query.get("daily_drawdown", "eod"),
            daily_loss_enabled=query.get("daily_loss_enabled", "true").lower() in {"1", "true", "yes", "on"},
        )
        return web.json_response(data, headers=NO_CACHE)
    except ValueError as exc:
        return _error(str(exc), 400)
    except EvidenceError as exc:
        return _error(str(exc), 503)


async def position_size(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return web.json_response(SERVICE.position_size(payload), headers=NO_CACHE)
    except (ValueError, ArithmeticError) as exc:
        return _error(str(exc), 400)
    except EvidenceError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"invalid calculator request: {type(exc).__name__}: {exc}", 400)


async def plan(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return web.json_response(SERVICE.plan(payload), headers=NO_CACHE)
    except (ValueError, ArithmeticError) as exc:
        return _error(str(exc), 400)
    except EvidenceError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"invalid plan request: {type(exc).__name__}: {exc}", 400)


async def compliance(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return web.json_response(SERVICE.compliance(payload), headers=NO_CACHE)
    except (ValueError, ArithmeticError) as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"invalid compliance request: {type(exc).__name__}: {exc}", 400)


async def paper_state(request: web.Request) -> web.Response:
    if PAPER_BOT is None:
        return web.json_response({
            "ok": True, "running": False, "paper_only": True,
            "live_order_routing": False, "status": "NOT_STARTED",
            "sleeves": [], "positions": [], "history": [], "log": [],
        }, headers=NO_CACHE)
    return web.json_response(PAPER_BOT.state(), headers=NO_CACHE)


async def paper_toggle(request: web.Request) -> web.Response:
    if PAPER_BOT is None:
        return _error("Lucid Lab paper runtime is not started", 503)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return web.json_response(PAPER_BOT.set_enabled(bool(payload.get("enabled"))), headers=NO_CACHE)
    except ValueError as exc:
        return _error(str(exc), 400)


async def paper_reset(request: web.Request) -> web.Response:
    if PAPER_BOT is None:
        return _error("Lucid Lab paper runtime is not started", 503)
    try:
        return web.json_response(PAPER_BOT.reset(), headers=NO_CACHE)
    except RuntimeError as exc:
        return _error(str(exc), 409)


async def start_simulation(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        result = await SIMULATIONS.start(payload)
        return web.json_response({"ok": True, **result}, status=202, headers=NO_CACHE)
    except (ValueError, EvidenceError) as exc:
        return _error(str(exc), 400 if isinstance(exc, ValueError) else 503)


async def simulation_state(request: web.Request) -> web.Response:
    try:
        return web.json_response({"ok": True, **SIMULATIONS.get(request.match_info["job_id"])}, headers=NO_CACHE)
    except KeyError as exc:
        return _error(str(exc), 404)


async def cancel_simulation(request: web.Request) -> web.Response:
    try:
        return web.json_response({"ok": True, **SIMULATIONS.cancel(request.match_info["job_id"])}, headers=NO_CACHE)
    except KeyError as exc:
        return _error(str(exc), 404)


async def validate_data(request: web.Request) -> web.Response:
    symbol = request.query.get("symbol", "").strip().upper()
    if not symbol:
        return _error("expected symbol is required", 400)
    if not request.content_type.startswith("multipart/"):
        return _error("multipart file upload is required", 400)
    try:
        reader = await request.multipart()
        part = await reader.next()
        while part is not None and part.name != "file":
            part = await reader.next()
        if part is None or not part.filename:
            return _error("file field is required", 400)
        suffix = Path(part.filename).suffix.lower()
        if suffix not in {".csv", ".parquet", ".pq"}:
            return _error("only CSV and Parquet files are supported", 400)
        with tempfile.TemporaryDirectory(prefix="lucid-lab-import-") as tmp:
            path = Path(tmp).resolve() / ("upload" + suffix)
            if path.parent != Path(tmp).resolve():
                return _error("invalid temporary upload path", 400)
            total = 0
            with path.open("wb") as handle:
                while True:
                    chunk = await part.read_chunk(size=256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        return _error("file exceeds the 25 MB validation limit", 413)
                    handle.write(chunk)
            report = validate_market_data(path, expected_symbol=symbol)
            return web.json_response({"ok": True, "report": report.to_dict()}, headers=NO_CACHE)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"data validation failed: {type(exc).__name__}: {exc}", 400)


def routes() -> list[web.AbstractRouteDef]:
    return [
        web.get("/lucid-lab", page),
        web.get("/api/lucid-lab/snapshot", snapshot),
        web.post("/api/lucid-lab/position-size", position_size),
        web.post("/api/lucid-lab/plan", plan),
        web.post("/api/lucid-lab/compliance", compliance),
        web.get("/api/lucid-lab/paper/state", paper_state),
        web.post("/api/lucid-lab/paper/toggle", paper_toggle),
        web.post("/api/lucid-lab/paper/reset", paper_reset),
        web.post("/api/lucid-lab/simulations", start_simulation),
        web.get("/api/lucid-lab/simulations/{job_id}", simulation_state),
        web.post("/api/lucid-lab/simulations/{job_id}/cancel", cancel_simulation),
        web.post("/api/lucid-lab/validate-data", validate_data),
    ]
