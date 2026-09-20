"""Small, UI-free maintenance services used by the single AIPet entry point."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "src" / "qq_bridge.py"
LOG = ROOT / "data" / "qq.log"
DESKTOP_LOCK = ROOT / "data" / "desktop.lock"


def _python(console: bool = False) -> Path:
    candidates = (
        ("runtime", "python.exe" if console else "pythonw.exe"),
        (".venv/Scripts", "python.exe" if console else "pythonw.exe"),
    )
    for directory, name in candidates:
        path = ROOT / directory / name
        if path.exists():
            return path
    return Path(sys.executable)


def qq_status() -> int:
    import qq_bot as QB

    pid = QB.read_pid()
    state = QB.read_status()
    print("QQ 桥")
    print(f"  进程     {'在跑（PID ' + str(pid) + '）' if pid else '没在跑'}")
    print(f"  网关     {state.get('state') or 'unknown'}"
          f"{('　' + state['detail']) if state.get('detail') else ''}")
    if state.get("fresh"):
        print(f"  最后活动 {state.get('age', 0):.0f} 秒前")
    print(f"  日志      {LOG}")
    return 0 if pid else 1


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in (result.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def status() -> int:
    """Show the install-wide status without importing Qt."""
    desktop_pid = 0
    try:
        desktop_pid = int(DESKTOP_LOCK.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        pass
    desktop_ok = _pid_alive(desktop_pid)
    print("AIPet 运行状态")
    print(f"  桌宠     {'在跑（PID ' + str(desktop_pid) + '）' if desktop_ok else '没在跑'}")
    qq_rc = qq_status()
    return 0 if desktop_ok and qq_rc == 0 else 1


def qq_start() -> int:
    import qq_bot as QB

    if QB.read_pid():
        print("QQ 桥已经在跑了。")
        return 0
    py = _python(console=False)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as stdout:
        subprocess.Popen([str(py), str(BRIDGE), "run"], cwd=str(ROOT),
                         creationflags=flags, stdin=stdin, stdout=stdout,
                         stderr=stdout, close_fds=True)
    for _ in range(20):
        time.sleep(0.5)
        if QB.read_pid():
            print(f"QQ 桥已启动（PID {QB.read_pid()}）。")
            return 0
    print(f"QQ 桥没有起来，请检查 {LOG}")
    return 1


def qq_stop() -> int:
    import qq_bot as QB

    pid = QB.read_pid()
    if not pid:
        print("QQ 桥本来就没在跑。")
        return 0
    QB.kill_pid(pid)
    for _ in range(10):
        time.sleep(0.3)
        if not QB.read_pid():
            break
    try:
        QB.PID_FILE.unlink()
    except OSError:
        pass
    QB.write_status("stopped", "手动停的")
    print(f"QQ 桥已停止（PID {pid}）。")
    return 0


def diagnose() -> int:
    """Run checks that do not import Qt or start the desktop window."""
    import importlib.util
    import platform

    checks = [
        ("安装目录", ROOT.is_dir()),
        ("配置模板", (ROOT / "data" / "config.example.json").is_file()),
        ("思考配置", (ROOT / "data" / "thinking.json").is_file()),
        ("Python 64 位", sys.maxsize > 2**32),
    ]
    print(f"AIPet 诊断：{ROOT}")
    print(f"Python：{platform.python_version()}（{'64' if sys.maxsize > 2**32 else '32'} 位）")
    for name, ok in checks:
        print(f"  {'[OK]' if ok else '[!!]'} {name}")
    for name in ("PySide6", "live2d", "ddgs"):
        ok = importlib.util.find_spec(name) is not None
        print(f"  {'[OK]' if ok else '[!!]'} 依赖 {name}")
    try:
        qq_status()
    except Exception as exc:  # diagnostics should report, not open a UI error box
        print(f"  [!!] QQ 状态：{type(exc).__name__}: {exc}")
        return 1
    return 0 if all(ok for _, ok in checks) else 1
