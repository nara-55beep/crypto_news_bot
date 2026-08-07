@echo off
REM ============================================================
REM  FIX_PORTS.bat  -  ONE-TIME fix. Run as Administrator.
REM
REM  A power cut / dirty reboot can make Windows (the Hyper-V
REM  "winnat" service) reserve a block of ports that swallows
REM  8000 and 8100, so the bot's website cannot start
REM  ("WinError 10013 ... forbidden").
REM
REM  This permanently reserves the bot's ports for the bot so
REM  Windows never grabs them again. You only need to run this ONCE.
REM ============================================================
echo.
echo Reserving the bot's ports (8000 website, 8100 research)...
echo.

net stop winnat
netsh int ipv4 add excludedportrange protocol=tcp startport=8000 numberofports=1 store=persistent
netsh int ipv4 add excludedportrange protocol=tcp startport=8100 numberofports=1 store=persistent
net start winnat

echo.
echo ============================================================
echo  Done. Ports 8000 and 8100 are now permanently reserved
echo  for the bot. Windows will not steal them again.
echo.
echo  Now: close the bot window (Ctrl-C) and start it again
echo  with run.bat (or dev.bat).
echo ============================================================
echo.
pause
