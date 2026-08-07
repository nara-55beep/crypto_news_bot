"""
research_server.py — auto-launch the standalone "Strategy Research Bot"
(the FastAPI app in crypto-trading-bot/) INSIDE the main bot's process.

Why: so you only ever start ONE thing (your normal `python main.py`). This spins
the research dashboard up on http://127.0.0.1:8100 in a background daemon thread,
which the /paper page embeds in an iframe. No separate command, no second bot to
start by hand.

It is isolated from the main bot:
  * runs in its own thread with its own asyncio event loop (uvicorn),
  * writes its db/logs/cache inside crypto-trading-bot/ (not the main folder),
  * logs to its own file only (BOT_NO_CONSOLE_LOG) so it won't spam this console,
  * never raises into the main bot — if it can't start, the main bot is unaffected.
"""

from __future__ import annotations

import os
import sys
import threading

# crypto-trading-bot/ lives next to this file.
RESEARCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto-trading-bot")
RESEARCH_HOST = "127.0.0.1"
# A power-cut/dirty reboot can make Windows (Hyper-V winnat) reserve 8100 so it
# cannot be bound. Try 8100 first, then fall back to free ports.
RESEARCH_PORT_CANDIDATES = [8100, 5100, 5150, 3100, 8181]
RESEARCH_PORT = 8100   # set at startup by _pick_research_port()

_STARTED = False


def _pick_research_port() -> int:
    import socket as _socket
    for cand in RESEARCH_PORT_CANDIDATES:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind((RESEARCH_HOST, cand))
            s.close()
            return cand
        except OSError:
            s.close()
            continue
    return RESEARCH_PORT_CANDIDATES[0]


def _run() -> None:
    try:
        # Keep the research bot's data/logs/results inside its own folder, and silence
        # its console output (it logs to crypto-trading-bot/logs/bot.log instead).
        os.environ.setdefault("BOT_DB_PATH", os.path.join(RESEARCH_DIR, "trading_bot.db"))
        os.environ.setdefault("BOT_LOG_DIR", os.path.join(RESEARCH_DIR, "logs"))
        os.environ.setdefault("BOT_DATA_CACHE_DIR", os.path.join(RESEARCH_DIR, "data_cache"))
        os.environ.setdefault("BOT_RESULTS_DIR", os.path.join(RESEARCH_DIR, "results"))
        os.environ.setdefault("BOT_NO_CONSOLE_LOG", "1")

        if RESEARCH_DIR not in sys.path:
            sys.path.insert(0, RESEARCH_DIR)

        import uvicorn
        from backend.main import app  # the FastAPI app (imports the whole research backend)

        global RESEARCH_PORT
        RESEARCH_PORT = _pick_research_port()
        if RESEARCH_PORT != RESEARCH_PORT_CANDIDATES[0]:
            print(f"[research] port {RESEARCH_PORT_CANDIDATES[0]} blocked by Windows; using {RESEARCH_PORT}")

        # log_level=warning + access_log off keeps it quiet; it serves the dashboard at /.
        config = uvicorn.Config(
            app, host=RESEARCH_HOST, port=RESEARCH_PORT,
            log_level="warning", access_log=False,
        )
        uvicorn.Server(config).run()  # blocks in THIS thread (its own event loop)
    except OSError as e:
        # Most common: port already in use because you started it manually. Harmless.
        print(f"[research] not started (port :{RESEARCH_PORT} busy?): {e}")
    except Exception as e:
        print(f"[research] could not start research server: {type(e).__name__}: {e}")


def start_research_server() -> None:
    """Launch the research dashboard in a daemon thread. Non-blocking; safe to call once."""
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    if not os.path.isdir(RESEARCH_DIR):
        print(f"[research] folder not found at {RESEARCH_DIR} — skipping research server")
        return
    threading.Thread(target=_run, name="research-server", daemon=True).start()
    print(f"[research] Strategy Research Bot -> http://{RESEARCH_HOST}:{RESEARCH_PORT} "
          f"(auto-started; embedded in the /paper page)")
