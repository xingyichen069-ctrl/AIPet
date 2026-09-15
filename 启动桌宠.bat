@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\python.exe" -c "import PySide6, live2d.v3, ddgs" >nul 2>&1
    if not errorlevel 1 (
        start "" ".venv\Scripts\pythonw.exe" "src\pet.py"
        exit /b
    )
)
where uv >nul 2>&1
if not errorlevel 1 (
    uv venv --python 3.12 .venv
    if errorlevel 1 goto failed
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
) else (
    python -m venv .venv
    if errorlevel 1 goto failed
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if errorlevel 1 goto failed
start "" ".venv\Scripts\pythonw.exe" "src\pet.py"
exit /b
:failed
echo 启动环境准备失败，请安装 Python 3.12 或 uv 后重试。
pause
