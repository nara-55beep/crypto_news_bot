"""
logger.py — one place to configure logging for the whole bot.

Writes to both the console and a rotating file under the log directory, so you
always have a record of every signal, trade and error.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_CONFIGURED = False


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> None:
    """Configure root logging once. Safe to call multiple times.

    Set BOT_NO_CONSOLE_LOG=1 to skip the console handler — used when this server
    runs embedded inside another app (e.g. the main bot) so it doesn't spam that
    app's console; logs still go to the file.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    # Console (skipped when embedded)
    if not os.environ.get("BOT_NO_CONSOLE_LOG"):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    # Rotating file (5 files x 2 MB)
    fileh = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # ccxt can be very chatty at DEBUG — keep it calm.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Get a named logger (call setup_logging() once at startup first)."""
    return logging.getLogger(name)
