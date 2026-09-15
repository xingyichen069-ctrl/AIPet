@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\python.exe" (
    "runtime\python.exe" "src\memory.py" show
) else (
    ".venv\Scripts\python.exe" "src\memory.py" show
)
pause
