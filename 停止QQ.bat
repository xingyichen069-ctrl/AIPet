@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

rem 开机自启是**无窗口**跑的，任务管理器里认不出哪个 pythonw 是它。
rem 所以靠 PID 文件 —— 这个脚本读那个文件干活。

"%PY%" "tools\qq_ctl.py" stop
echo.
pause
