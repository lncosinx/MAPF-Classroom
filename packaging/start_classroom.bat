@echo off
rem MAPF classroom launcher (Windows portable package). Keep this file ASCII-only:
rem cmd.exe parses batch files in the OEM code page; all Chinese messages are printed by Python.
setlocal
cd /d "%~dp0app"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0python\python.exe" -m classroom.server --open-browser %*
echo.
echo ============================================================
echo  The classroom server has stopped. If you see an error above,
echo  take a photo of this window for troubleshooting.
echo ============================================================
pause
