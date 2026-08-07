@echo off
title Crypto News Bot (DEV - auto restart on save)
cd /d "%~dp0"

REM ============================================================
REM  dev.bat  —  same as run.bat, but AUTO-RESTARTS on save.
REM  Edit any .py file in this folder and SAVE — the bot stops
REM  and starts again automatically so you see your change. No
REM  need to restart it by hand.
REM
REM  Note: this is a FULL restart (reconnects Binance + Telegram,
REM  ~10-20s each time), not in-process hot-swapping. True
REM  no-restart hot-reload (jurigged) doesn't work on Python 3.14.
REM ============================================================

if not exist "venv\Scripts\activate.bat" (
  echo.
  echo ERROR: the "venv" folder was not found next to this file.
  echo Do the one-time setup first, in this folder:
  echo     python -m venv venv
  echo     venv\Scripts\activate.bat
  echo     pip install -r requirements.txt
  echo     pip install watchfiles
  echo.
  pause
  exit /b
)

call venv\Scripts\activate.bat

REM --- Lucid realtime bridge: exact Dukascopy live feed via JForex ---
set LUCID_LIVE_SOURCE=local_bridge
set LUCID_LOCAL_BRIDGE_SOURCE_FAMILY=dukascopy_tick_proxy
start "Lucid Bridge Receiver" /min cmd /c "venv\Scripts\python.exe tools\lucid_bridge_receiver.py"

echo.
echo ================================================================
echo   Starting the Crypto News Bot  (DEV MODE - auto restart ON)
echo   Save any .py file and the bot restarts itself automatically.
echo   The chart opens in your browser automatically (~8 seconds).
echo   Keep this window open. Press Ctrl-C to stop the bot.
echo ================================================================
echo.

start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 10; $p='8000'; if (Test-Path 'data\dashboard_port.txt') { $p=(Get-Content 'data\dashboard_port.txt' -Raw).Trim() }; Start-Process ('http://127.0.0.1:'+$p)"

REM  the watcher runs main.py and restarts it whenever a .py file is saved
python dev_reload.py

echo.
echo Bot stopped. Press any key to close this window.
pause >nul
