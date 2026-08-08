"""Credential-free environment loading, shared by config.py and config.example.py.

Windows' ``setx`` writes to the registry and only reaches processes started afterwards.
A long-lived shell - or a server launched from one - keeps the environment it was born
with, so a key can be correctly installed and simultaneously invisible to the program
that needs it. That is not hypothetical: the penny desk ran with AI review silently
disabled while the key sat valid in the user registry, and every candidate showed a blank
verdict that looked like a broken model.

This module contains no secrets and is safe to track. ``config.py`` is gitignored, so any
helper that lives only there ships to nobody; a fresh clone gets ``config.example.py``,
which imports this.
"""
from __future__ import annotations

import os

__all__ = ["env_or_registry"]


def env_or_registry(name: str, default: str = "") -> str:
    """Return ``name`` from the process environment, else Windows' persisted user env.

    A value found in the registry is also written back into ``os.environ`` so child
    processes inherit it without repeating the lookup. Any failure is non-fatal: the
    caller simply gets ``default``, exactly as it would from ``os.getenv``.
    """
    value = os.getenv(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, _ = winreg.QueryValueEx(key, name)
                if stored:
                    os.environ[name] = str(stored)
                    return str(stored)
        except (OSError, ImportError):
            pass
    return default
