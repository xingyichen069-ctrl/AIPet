@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\python.exe" (
    "runtime\python.exe" "src\pet.py" --show-chat
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "src\pet.py" --show-chat
) else (
    echo 请先双击“启动桌宠.bat”。
)
pause
