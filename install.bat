@echo off
title Install bot dependencies (into the venv)
cd /d "%~dp0"

REM --- install straight into the venv's python, so it works no matter
REM --- whether you're in PowerShell or Command Prompt (no activation needed) ---
if not exist "venv\Scripts\python.exe" (
  echo.
  echo ERROR: the "venv" folder was not found next to this file.
  echo Run run.bat once first to create it, then run this again.
  echo.
  pause
  exit /b
)

echo Installing required packages into the venv the bot uses...
echo (this is where ccxt needs to live)
echo.
venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo ================================================================
echo  Done. Next: double-click diagnose.bat to confirm ccxt is found
echo  and see which exchanges your PC can reach. Then run run.bat.
echo ================================================================
echo.
pause
