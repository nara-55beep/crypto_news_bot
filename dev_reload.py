"""
================================================================================
 dev_reload.py  —  AUTO-RESTART ON SAVE  (development only)
================================================================================
Runs `python main.py` and watches THIS folder's own .py files. The moment you
save a change, it stops the bot and starts it again automatically — so you see
your change without restarting by hand.

It deliberately IGNORES venv/, data/ and __pycache__/ so only YOUR code edits
trigger a restart (not the thousands of library files, and not the data the bot
writes while running, which would cause restart loops).

Honest note: this is a FULL restart, not in-process hot-swapping. Each restart
reconnects to Binance and re-resolves Telegram (~10-20s before fully live). True
no-restart hot-reload (jurigged) does not work on this machine's Python 3.14.

Run it with dev.bat. Stop it with Ctrl-C.
================================================================================
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

try:
    from watchfiles import watch, PythonFilter
    WATCHFILES_IMPORT_ERROR = ""
except ImportError as exc:
    # Development mode should not become unusable just because this optional native
    # dependency is missing or an interrupted install left only an empty package folder.
    # A small stdlib poller below provides the same save-and-restart behaviour.
    watch = None
    PythonFilter = None
    WATCHFILES_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

ROOT = os.path.dirname(os.path.abspath(__file__))
IGNORE = (
    os.path.join(ROOT, "venv"),
    os.path.join(ROOT, "data"),
    os.path.join(ROOT, "__pycache__"),
)
IGNORE_DIR_NAMES = {"venv", "data", "__pycache__", ".git", ".pytest_cache"}


def _python_snapshot() -> dict[str, tuple[int, int]]:
    """Return timestamps/sizes for project Python files, excluding generated trees."""
    files: dict[str, tuple[int, int]] = {}
    for current, dirs, names in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in IGNORE_DIR_NAMES]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(current, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files[path] = (stat.st_mtime_ns, stat.st_size)
    return files


def _stdlib_watch(interval_sec: float = 1.0):
    """Yield changed Python paths using only the standard library."""
    previous = _python_snapshot()
    while True:
        time.sleep(interval_sec)
        current = _python_snapshot()
        changed = {
            path for path in previous.keys() | current.keys()
            if previous.get(path) != current.get(path)
        }
        previous = current
        if changed:
            # Match the tuple shape returned by watchfiles; callers only need the path.
            yield [(None, path) for path in sorted(changed)]


def _start() -> subprocess.Popen:
    print("\n[dev] starting:  python main.py\n", flush=True)
    # a list (not a string) avoids Windows path/quoting problems
    return subprocess.Popen([sys.executable, "main.py"], cwd=ROOT)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # A venv python.exe is a launcher which spawns the real interpreter. Calling
        # terminate() on only the launcher orphaned main.py, so every save left an
        # old dashboard writing the same state files on a fallback port. taskkill is
        # invoked without a shell and is scoped to the exact child PID/tree.
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            check=False, capture_output=True, text=True,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    print("[dev] watching your .py files - save any file to auto-restart the bot.")
    print("[dev] (Ctrl-C to stop.)\n", flush=True)
    proc = _start()
    try:
        if watch is None:
            print("[dev] watchfiles is unavailable; using the built-in 1-second poller.")
            print(f"[dev] dependency detail: {WATCHFILES_IMPORT_ERROR}\n", flush=True)
            changes_iter = _stdlib_watch()
        else:
            # Polling is reliable for synced/network folders where filesystem events can
            # be missed. poll_delay_ms controls how often watchfiles checks for edits.
            changes_iter = watch(
                ROOT,
                watch_filter=PythonFilter(ignore_paths=IGNORE),
                force_polling=True,
                poll_delay_ms=1000,
            )
        for changes in changes_iter:
            files = ", ".join(sorted({os.path.basename(p) for _kind, p in changes}))
            print(f"\n[dev] change detected ({files}) -> restarting the bot...",
                  flush=True)
            _stop(proc)
            proc = _start()
    except KeyboardInterrupt:
        print("\n[dev] stopping.", flush=True)
    finally:
        _stop(proc)
