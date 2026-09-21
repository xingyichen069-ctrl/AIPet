@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ====================================================
rem  Run this once before the first launch.
rem
rem  Tries in order:
rem    1. runtime\  bundled with the portable build
rem    2. .venv\   already set up and complete
rem    3. uv        will fetch Python 3.12 by itself
rem    4. system Python, any of 3.10-3.14
rem
rem  After this, double-click the launcher .bat.
rem  Safe to re-run, it will not download twice.
rem
rem  Deps install from the Tsinghua mirror by default. Direct pypi.org
rem  measured at ~50 kB/s here, and PySide6 alone is 250 MB. Delete the
rem  MIRROR line below to use the official index instead.
rem
rem  NOTE: keep every comment in this file ASCII. cmd seeks bat files by
rem  byte offset, and non-ASCII comment lines shift that offset, which
rem  makes it split a following line in half and run the tail as a
rem  command. Chinese inside echo lines is fine.
rem ====================================================

set "MIRROR=--index-url https://pypi.tuna.tsinghua.edu.cn/simple"

echo.
echo   小日和 · 环境准备
echo   ----------------------------------
echo.

if exist "runtime\pythonw.exe" (
    echo   [OK] 检测到便携版自带的 runtime\
    echo        不用装任何东西，直接双击 启动桌宠.bat
    goto :done
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import PySide6, live2d.v3, ddgs" >nul 2>&1
    if not errorlevel 1 (
        echo   [OK] .venv 已就绪，直接双击 启动桌宠.bat
        goto :done
    )
    echo   [--] .venv 存在但依赖不全，重新安装
    echo.
)

where uv >nul 2>&1
if errorlevel 1 goto :no_uv

echo   [--] 用 uv 创建环境（会自动下载 Python 3.12）
uv venv --python 3.12 .venv
if errorlevel 1 goto :uv_failed

echo   [--] 安装依赖，下载约 300 MB，第一次比较久
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt %MIRROR%
if errorlevel 1 goto :uv_failed
goto :verify

:uv_failed
echo   [!!] uv 这条路没走通，改用系统 Python
echo.

:no_uv
rem Try newest first. Any 64-bit wheel works, it does not have to be 3.12.
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    if not exist ".venv\Scripts\python.exe" call :try_py %%V
)

if not exist ".venv\Scripts\python.exe" (
    python -c "import sys,struct;assert (3,10)<=sys.version_info[:2]<(3,15) and struct.calcsize('P')==8" >nul 2>&1
    if not errorlevel 1 (
        echo   [--] 用 PATH 里的 python 创建环境
        python -m venv .venv
    )
)

if not exist ".venv\Scripts\python.exe" goto :no_python

echo   [--] 安装依赖，下载约 300 MB，第一次比较久
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt %MIRROR%
if not errorlevel 1 goto :verify
echo   [!!] 镜像没装上，改用官方源重试
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not errorlevel 1 goto :verify
goto :failed

:verify
".venv\Scripts\python.exe" -c "import PySide6, live2d.v3, ddgs" >nul 2>&1
if errorlevel 1 goto :failed

echo.
echo   [OK] 环境就绪，双击 启动桌宠.bat
goto :done

rem --- subroutines ------------------------------------

:try_py
py -%1 -c "import struct;assert struct.calcsize('P')==8" >nul 2>&1
if errorlevel 1 exit /b
echo   [--] 用 Python %1 创建环境
py -%1 -m venv .venv
exit /b

:no_python
echo.
echo   [X] 没找到可用的 64 位 Python。
echo.
echo       装下面任意一个都行：
echo         uv            https://docs.astral.sh/uv/
echo         Python 3.12   https://www.python.org/downloads/
echo.
echo       装 Python 时记得勾 "Add python.exe to PATH"，然后重新双击本脚本。
echo.
pause
exit /b 1

:failed
echo.
echo   [X] 依赖没装上。两个常见原因：
echo.
echo       网络不通  挂上代理再试，或者手动跑：
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo       版本不对  需要 3.10 到 3.14 之间的 64 位 Python。
echo.
pause
exit /b 1

:done
echo.
pause
