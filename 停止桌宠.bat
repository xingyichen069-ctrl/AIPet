@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ====================================================
rem  Stop the desktop pet from the outside.
rem
rem  Normally you do not need this: right-click the pet
rem  and pick the exit item. Use this when that is not
rem  reachable -- click-through is on, the tray icon is
rem  hidden in the Windows 11 overflow, or the UI thread
rem  is wedged and nothing inside the app can run.
rem
rem  It reads the PID from data\desktop.lock, kills that
rem  process, and removes the lock file so the next start
rem  is clean.
rem
rem  To just look without killing, run:
rem    python tools\status.py
rem
rem  NOTE  ASCII only, deliberately. cmd.exe seeks batch
rem  files by byte offset; any non-ASCII byte drifts the
rem  parser and it starts running fragments of comments
rem  as commands. All Chinese output comes from pet_ctl.py.
rem ====================================================

set "PY=runtime\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "tools\pet_ctl.py" stop

echo.
pause
