@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
rem Keep ASCII and CRLF. Chinese guidance is printed by qq_ctl.py.
set "PY=runtime\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -X utf8 "tools\qq_ctl.py" start
set "AIPET_QQ_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %AIPET_QQ_RESULT%
