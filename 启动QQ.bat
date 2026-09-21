@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ══════════════════════════════════════════════════════
rem  启动 QQ 桥（后台无窗口）
rem
rem  已经装了开机自启的话，开机就自动在跑了，
rem  不需要双击这个。双击之前它会先检查，不会起两个。
rem
rem  ⚠️ 跑之前必须停掉 Cherry Studio 的 QQ 通道
rem     （设置 → 频道 → QQ → 关掉那个开关）
rem     同一个 bot app 只能有一条网关连接，两条同时跑
rem     事件会随机分配，表现为"有时回有时不回"。
rem
rem  想看着日志跑（调试用）：tools\qq_ctl.py start --visible
rem  停：双击 停止QQ.bat
rem ══════════════════════════════════════════════════════

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -c "import sys; sys.path.insert(0,'src'); import qq_bot as Q; a,s=Q.secrets(); sys.exit(0 if a else 1)" 2>nul
if errorlevel 1 (
    echo.
    echo  [X] 没找到 QQ 凭据。
    echo      先跑一次： python tools\backup_cherry_qq.py
    echo.
    pause
    exit /b 1
)

"%PY%" "tools\qq_ctl.py" start
echo.
pause
