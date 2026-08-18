"""aiohttp routes for the Strategy Library, mounted from dashboard.routes()."""
from __future__ import annotations

from typing import Any, Callable

from aiohttp import web

from .page import STRATEGY_LAB_HTML
from .service import MarketDataError, StrategyLabService


SERVICE: StrategyLabService | None = None
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0"}


def service() -> StrategyLabService:
    """Built on first use so importing the module never blocks startup."""
    global SERVICE
    if SERVICE is None:
        SERVICE = StrategyLabService()
    return SERVICE


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status, headers=NO_CACHE)


async def _json_body(request: web.Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


async def _guard(handler: Callable[[], Any]) -> web.Response:
    try:
        return web.json_response(handler(), headers=NO_CACHE)
    except KeyError as exc:
        return _error(str(exc).strip("'"), 404)
    except MarketDataError as exc:
        return _error(str(exc), 503)
    except (ValueError, ArithmeticError) as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"{type(exc).__name__}: {exc}", 500)


async def page(request: web.Request) -> web.Response:
    return web.Response(text=STRATEGY_LAB_HTML, content_type="text/html", headers=NO_CACHE)


async def overview(request: web.Request) -> web.Response:
    return await _guard(lambda: service().overview())


async def browse(request: web.Request) -> web.Response:
    return await _guard(lambda: service().browse(dict(request.query)))


async def detail(request: web.Request) -> web.Response:
    return await _guard(lambda: service().detail(request.match_info["strategy_id"]))


async def run_one(request: web.Request) -> web.Response:
    try:
        payload = await _json_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    return await _guard(lambda: service().run_one(payload))


async def compare(request: web.Request) -> web.Response:
    try:
        payload = await _json_body(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    return await _guard(lambda: service().compare(payload))


async def start_batch(request: web.Request) -> web.Response:
    try:
        payload = await _json_body(request)
        result = await service().start_batch(payload)
        return web.json_response(result, status=202, headers=NO_CACHE)
    except MarketDataError as exc:
        return _error(str(exc), 503)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        if isinstance(exc, web.HTTPException):
            raise
        return _error(f"{type(exc).__name__}: {exc}", 500)


async def batch_state(request: web.Request) -> web.Response:
    rows = request.query.get("rows", "1").lower() not in {"0", "false", "no"}
    return await _guard(
        lambda: service().batch_state(request.match_info["job_id"], include_rows=rows))


async def cancel_batch(request: web.Request) -> web.Response:
    return await _guard(lambda: service().cancel_batch(request.match_info["job_id"]))


def routes() -> list[web.AbstractRouteDef]:
    return [
        web.get("/strategies", page),
        web.get("/api/strategies/overview", overview),
        web.get("/api/strategies/browse", browse),
        web.get("/api/strategies/detail/{strategy_id}", detail),
        web.post("/api/strategies/run", run_one),
        web.post("/api/strategies/compare", compare),
        web.post("/api/strategies/batch", start_batch),
        web.get("/api/strategies/batch/{job_id}", batch_state),
        web.post("/api/strategies/batch/{job_id}/cancel", cancel_batch),
    ]
