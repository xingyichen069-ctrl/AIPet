@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ====================================================
rem  Move personal content from an OLD install into THIS
rem  one: persona, memory, API keys, QQ bindings, your
rem  configs, mood log, companion data, knowledge.
rem
rem  You only need this when you unpacked the new version
rem  into a NEW folder. Unzipping over the old folder
rem  loses nothing, so this is not needed there.
rem
rem  USAGE
rem    Drag the OLD folder onto this file.
rem    Or:  python tools\migrate.py D:\old\AIPet
rem
rem  It prints the plan first and asks Y/N before copying.
rem  Anything it would overwrite is saved to backups\.
rem
rem  NOTE  This file is deliberately 100% ASCII. cmd.exe
rem  seeks batch files by byte offset, so any non-ASCII
rem  byte drifts the parser and it starts running fragments
rem  of comments as commands. All Chinese output comes from
rem  migrate.py instead -- Python has no such problem.
rem ====================================================

set "PY=runtime\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "tools\migrate.py" %*

echo.
pause
