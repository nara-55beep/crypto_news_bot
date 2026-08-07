@echo off
title CCXT Diagnostic
cd /d "%~dp0"

REM --- use the SAME venv the bot uses, so we test the right Python ---
if not exist "venv\Scripts\activate.bat" (
  echo.
  echo ERROR: the "venv" folder was not found next to this file.
  echo Run run.bat once first to do the one-time setup.
  echo.
  pause
  exit /b
)
call venv\Scripts\activate.bat

python diagnose.py

echo.
pause
