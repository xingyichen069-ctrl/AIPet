@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ====================================================
rem  Show whether the desktop pet and the QQ bridge are
rem  alive, and whether the QQ gateway is still connected.
rem
rem  Read-only. It never starts or stops anything --
rem  use tools\qq_ctl.py for that.
rem
rem  Double-click for a one-shot report (with the last
rem  10 lines of the QQ log). From a terminal you can
rem  also pass:
rem
rem    python tools\status.py --watch        refresh every 5s
rem    python tools\status.py --watch 2      every 2s
rem    python tools\status.py --log 40       more log lines
rem
rem  NOTE  ASCII only, deliberately. cmd.exe seeks batch
rem  files by byte offset; any non-ASCII byte drifts the
rem  parser and it starts running fragments of comments
rem  as commands. All Chinese output comes from status.py.
rem ====================================================

set "PY=runtime\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1"=="" (
    "%PY%" "tools\status.py" --log 10
) else (
    "%PY%" "tools\status.py" %*
)

echo.
pause
