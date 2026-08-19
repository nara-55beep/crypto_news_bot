"""aiohttp routes for the Reference Ladder research page."""
from __future__ import annotations

from typing import Any

from aiohttp import web

from .page import REFERENCE_LADDER_HTML
from .service import ReferenceLadderService


SERVICE: ReferenceLadderService | None = None
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0"}


def service() -> ReferenceLadderService:
    global SERVICE
    if SERVICE is None:
        SERVICE = ReferenceLadderService()
    return SERVICE


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status, headers=NO_CACHE)


async def page(request: web.Request) -> web.Response:
    return web.Response(text=REFERENCE_LADDER_HTML, content_type="text/html", headers=NO_CACHE)


async def config(request: web.Request) -> web.Response:
    return web.json_response(service().config(), headers=NO_CACHE)


async def start(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return web.json_response(service().start(payload), status=202, headers=NO_CACHE)
    except ValueError as exc:
        return _error(str(exc), 400)


async def state(request: web.Request) -> web.Response:
    try:
        return web.json_response(service().state(request.match_info["job_id"]), headers=NO_CACHE)
    except KeyError as exc:
        return _error(str(exc).strip("'"), 404)


async def latest(request: web.Request) -> web.Response:
    return web.json_response(service().latest(), headers=NO_CACHE)


def routes() -> list[web.AbstractRouteDef]:
    return [
        web.get("/reference-ladder", page),
        web.get("/api/reference-ladder/config", config),
        web.post("/api/reference-ladder/run", start),
        web.get("/api/reference-ladder/jobs/{job_id}", state),
        web.get("/api/reference-ladder/latest", latest),
    ]
