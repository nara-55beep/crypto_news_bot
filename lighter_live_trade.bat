@echo off
title Lighter REAL-MONEY - trade menu
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
  echo.
  echo ERROR: the "venv" folder was not found. Run run.bat once first.
  echo.
  pause
  exit /b
)
call venv\Scripts\activate.bat

python lighter_live.py menu

echo.
pause
