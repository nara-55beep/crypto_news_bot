@echo off
title Crypto News Bot
cd /d "%~dp0"

REM --- check the virtual environment exists (created during one-time setup) ---
if not exist "venv\Scripts\activate.bat" (
  echo.
  echo ERROR: the "venv" folder was not found next to this file.
  echo Do the one-time setup first, in this folder:
  echo     python -m venv venv
  echo     venv\Scripts\activate.bat
  echo     pip install -r requirements.txt
  echo.
  pause
  exit /b
)

REM --- turn on the virtual environment ---
call venv\Scripts\activate.bat

REM --- Lucid realtime bridge: exact Dukascopy live feed via JForex ---
REM JForex must be running with the LucidBridgeStrategy loaded, or the
REM Lucid bots will show "bridge not ready" and refuse live entries.
set LUCID_LIVE_SOURCE=local_bridge
set LUCID_LOCAL_BRIDGE_SOURCE_FAMILY=dukascopy_tick_proxy
start "Lucid Bridge Receiver" /min cmd /c "venv\Scripts\python.exe tools\lucid_bridge_receiver.py"

echo.
echo ================================================================
echo   Starting the Crypto News Bot
echo   The chart will open in your browser automatically (~8 seconds).
echo   Code updates auto-restart the bot so deployed fixes are not stale.
echo   FIRST RUN ONLY: type your phone number + Telegram code below.
echo   Keep this window open. Press Ctrl-C to stop the bot.
echo ================================================================
echo.

REM --- open the dashboard in the browser after a short delay (server needs a moment) ---
start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 10; $p='8000'; if (Test-Path 'data\dashboard_port.txt') { $p=(Get-Content 'data\dashboard_port.txt' -Raw).Trim() }; Start-Process ('http://127.0.0.1:'+$p)"

REM --- supervise main.py so merged Python fixes replace the running process too ---
REM dev_reload.py has a standard-library fallback if watchfiles is unavailable.
python dev_reload.py

echo.
echo Bot stopped. Press any key to close this window.
pause >nul
