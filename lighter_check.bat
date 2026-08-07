@echo off
title Lighter Account Check (read-only)
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
  echo.
  echo ERROR: the "venv" folder was not found. Run run.bat once first.
  echo.
  pause
  exit /b
)
call venv\Scripts\activate.bat

python lighter_check.py

echo.
pause
