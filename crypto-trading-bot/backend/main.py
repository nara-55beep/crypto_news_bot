"""
main.py — FastAPI entry point.

Run it with:
    uvicorn backend.main:app --reload --port 8100
Then open http://127.0.0.1:8100  for the dashboard, or call the JSON API under /api.

The bot always boots STOPPED and in PAPER mode — nothing trades until you press
Start in the dashboard.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.config import settings
from backend.utils.logger import setup_logging, get_logger

setup_logging(settings.log_dir)
log = get_logger("main")

app = FastAPI(title="crypto-trading-bot", version="0.1.0",
              description="Rule-based, backtestable BTC/ETH/SOL research bot. Not financial advice.")

# Allow your existing website (any local origin) to call this API / embed the dashboard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

# JSON + WebSocket API under /api
app.include_router(router, prefix="/api")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "crypto-trading-bot"}


# Serve the simple dashboard at "/" (index.html). Mounted last so it doesn't shadow /api.
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
    log.info("Dashboard served from %s", _frontend)
else:
    log.warning("frontend/ not found at %s — API still works", _frontend)


@app.on_event("startup")
def _startup():
    log.info("crypto-trading-bot started · mode=paper · STOPPED (press Start in the dashboard)")
    log.warning("LIVE TRADING is OFF by default. This is a research tool, not a profit guarantee.")
