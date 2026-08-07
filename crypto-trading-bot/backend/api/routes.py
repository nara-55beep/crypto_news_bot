"""
routes.py — the HTTP + WebSocket API the dashboard (and your website) call.

Endpoints (all under /api):
  GET  /status            full bot status (mode, balance, pnl, positions, signal…)
  GET  /settings          current strategy/risk settings
  POST /settings          edit runtime-editable settings  {key: value, …}
  POST /start             start the engine
  POST /stop              stop the engine
  POST /emergency_stop    flatten everything + stop
  POST /mode              {"mode": "paper"|"live"}
  GET  /trades            recent closed trades
  GET  /signals           recent signals
  GET  /equity            equity curve points
  GET  /logs              recent log lines
  POST /backtest          {"symbols": [...], "days": N}  (runs in a worker thread)
  POST /live/arm          arm live trading (real orders)
  POST /live/disarm       disarm live trading
  WS   /ws                pushes /status every 2s
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.api.controller import Controller

router = APIRouter()
controller = Controller()   # the one and only engine controller


# ------------------------------- request models -------------------------------
class ModeBody(BaseModel):
    mode: str


class BacktestBody(BaseModel):
    symbols: list[str] | None = None
    days: int | None = None


# ------------------------------- status / data --------------------------------
@router.get("/status")
def get_status():
    return controller.status()


@router.get("/settings")
def get_settings():
    return controller.s.public_dict()


@router.post("/settings")
def post_settings(patch: dict):
    return controller.update_settings(patch)


@router.get("/trades")
def get_trades(limit: int = 100, mode: str | None = None):
    return {"trades": controller.db.recent_trades(limit=limit, mode=mode)}


@router.get("/signals")
def get_signals(limit: int = 50):
    return {"signals": controller.db.recent_signals(limit=limit)}


@router.get("/equity")
def get_equity(mode: str | None = None):
    return {"equity": controller.db.equity_curve(mode or controller.mode)}


@router.get("/logs")
def get_logs(limit: int = 100):
    return {"logs": controller.db.recent_logs(limit=limit)}


# ------------------------------- control --------------------------------------
@router.post("/start")
def post_start():
    return controller.start()


@router.post("/stop")
def post_stop():
    return controller.stop()


@router.post("/emergency_stop")
def post_emergency():
    return controller.emergency_stop()


@router.post("/mode")
def post_mode(body: ModeBody):
    return controller.set_mode(body.mode)


@router.post("/live/arm")
def post_arm():
    return controller.arm()


@router.post("/live/disarm")
def post_disarm():
    return controller.disarm()


# ------------------------------- backtest -------------------------------------
# Defined as a normal (sync) function so FastAPI runs it in a worker thread —
# backtests do blocking network + CPU work and must not block the event loop.
@router.post("/backtest")
def post_backtest(body: BacktestBody):
    return controller.backtest(symbols=body.symbols, days=body.days)


# ------------------------------- websocket ------------------------------------
@router.websocket("/ws")
async def ws_status(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(controller.status())
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()
