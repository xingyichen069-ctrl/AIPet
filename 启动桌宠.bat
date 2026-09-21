@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\pythonw.exe" (
    start "" "runtime\pythonw.exe" "src\windows_launcher.py"
    exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\python.exe" -c "import PySide6, live2d.v3, ddgs" >nul 2>&1
    if not errorlevel 1 (
        start "" ".venv\Scripts\pythonw.exe" "src\windows_launcher.py"
        exit /b
    )
)
where uv >nul 2>&1
if not errorlevel 1 (
    uv venv --python 3.12 .venv
    if errorlevel 1 goto failed
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
) else (
    goto use_py
)
if errorlevel 1 goto failed
goto launch
:use_py
py -3.12 -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1
if errorlevel 1 goto use_python
py -3.12 -m venv .venv
if errorlevel 1 goto failed
goto install
:use_python
python -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8" >nul 2>&1
if errorlevel 1 goto failed
python -m venv .venv
if errorlevel 1 goto failed
:install
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
:launch
start "" ".venv\Scripts\pythonw.exe" "src\windows_launcher.py"
exit /b
:failed
echo 启动环境准备失败。便携包请完整解压；源码版请安装 64 位 Python 3.12 或 uv 后重试。
pause
