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

from watchfiles import watch, PythonFilter

ROOT = os.path.dirname(os.path.abspath(__file__))
IGNORE = (
    os.path.join(ROOT, "venv"),
    os.path.join(ROOT, "data"),
    os.path.join(ROOT, "__pycache__"),
)


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
        # force_polling=True: OneDrive (and other synced/network folders) do NOT emit reliable
        # file-change events, so the default event watcher silently MISSES your saves. Polling
        # stats the files every second instead — slower to notice, but it ACTUALLY fires on
        # OneDrive. poll_delay_ms is how often it checks.
        for changes in watch(ROOT, watch_filter=PythonFilter(ignore_paths=IGNORE),
                             force_polling=True, poll_delay_ms=1000):
            files = ", ".join(sorted({os.path.basename(p) for _kind, p in changes}))
            print(f"\n[dev] change detected ({files}) -> restarting the bot...",
                  flush=True)
            _stop(proc)
            proc = _start()
    except KeyboardInterrupt:
        print("\n[dev] stopping.", flush=True)
    finally:
        _stop(proc)
