@echo off
rem Add Windows Firewall rules for the classroom server (needs administrator).
rem Keep this file ASCII-only; the Chinese prompts come from classroom.server.firewall.
setlocal
net session >nul 2>&1
if errorlevel 1 (
  rem Not elevated: relaunch this script as administrator
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0app"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0python\python.exe" -m classroom.server.firewall %*
pause
