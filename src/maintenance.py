"""Small, UI-free maintenance services used by the single AIPet entry point."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app_paths import PATHS

ROOT = PATHS.install
BRIDGE = ROOT / "src" / "qq_bridge.py"
LOG = ROOT / "data" / "qq.log"
DESKTOP_LOCK = ROOT / "data" / "desktop.lock"
REQUIREMENTS = ROOT / "requirements.txt"


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


def _desktop_pid() -> int:
    try:
        return int(DESKTOP_LOCK.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return 0


def desktop_stop() -> int:
    """Stop a wedged desktop from the same AIPet.exe command boundary."""
    pid = _desktop_pid()
    if not pid:
        print("桌宠没在跑。")
        return 0
    if _pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)], check=False,
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.kill(pid, 9)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"桌宠停不掉：{exc}")
            return 1
        if _pid_alive(pid):
            print(f"已发停止指令，但 PID {pid} 仍在运行。")
            return 1
        print(f"桌宠已停止（PID {pid}）。")
    else:
        print(f"进程已经不在了（PID {pid}），只清理残留锁文件。")
    try:
        DESKTOP_LOCK.unlink()
    except OSError:
        pass
    return 0


def _imports_ok(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import PySide6, live2d.v3, ddgs"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _run(command: list[str]) -> bool:
    try:
        return subprocess.run(command, cwd=str(ROOT), check=False).returncode == 0
    except OSError:
        return False


def prepare() -> int:
    """Prepare a source checkout, or verify the runtime in a portable build."""
    portable = ROOT / "runtime" / "python.exe"
    if portable.exists():
        if _imports_ok(portable):
            print(f"便携运行环境已就绪：{portable}")
            return 0
        print("便携运行环境存在，但依赖检查没有通过。请重新解压完整发布包。")
        return 1

    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.exists() and _imports_ok(venv):
        print(f"源码运行环境已就绪：{venv}")
        return 0

    mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
    uv = shutil.which("uv")
    if uv:
        print("用 uv 创建源码环境…")
        if _run([uv, "venv", "--python", "3.12", ".venv"]):
            if _run([uv, "pip", "install", "--python", str(venv), "-r",
                     str(REQUIREMENTS), "--index-url", mirror]):
                if _imports_ok(venv):
                    print(f"环境已就绪：{venv}")
                    return 0
        print("uv 这条路没有完成，继续尝试系统 Python。")

    candidates: list[list[str]] = []
    py = shutil.which("py")
    if py and os.name == "nt":
        candidates.extend([[py, f"-{version}", "-m", "venv", ".venv"]
                           for version in ("3.14", "3.13", "3.12", "3.11", "3.10")])
    if sys.executable:
        candidates.append([sys.executable, "-m", "venv", ".venv"])
    created = venv.exists()
    for command in candidates:
        if created or _run(command):
            created = True
            break
    if not created:
        print("没有找到可用的 64 位 Python 3.10–3.14。")
        return 1

    print("安装依赖，首次可能需要几分钟…")
    if not _run([str(venv), "-m", "pip", "install", "--upgrade", "pip"]):
        print("pip 没有准备好。")
        return 1
    installed = _run([str(venv), "-m", "pip", "install", "-r",
                      str(REQUIREMENTS), "--index-url", mirror])
    if not installed:
        installed = _run([str(venv), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    if not installed or not _imports_ok(venv):
        print("依赖没有装完整，请检查网络和 Python 版本。")
        return 1
    print(f"环境已就绪：{venv}")
    return 0


def memory_show() -> int:
    """Print the same human-readable memory view formerly behind the batch file."""
    import memory as M

    entries = sorted(M.load_journal(), key=lambda item: item["ts"], reverse=True)
    if not entries:
        print("记忆库是空的。")
        return 0
    state = M.load_state()
    print(f"共 {len(entries)} 条 · 亲密度 {state.get('closeness', 0)} · "
          f"累计互动 {state.get('interaction_count', 0)} 次\n")
    for entry in entries:
        speaker = entry.get("speaker", "owner")
        who = "  " if speaker == "owner" else "群"
        print(f"  {who} [{entry['ts'][:16]}] ({entry.get('decay', 'normal'):<9}) "
              f"{'★' * entry.get('importance', 3):<5} {entry['text']}")
    return 0


def status(log_lines: int = 0) -> int:
    """Show the install-wide status without importing Qt."""
    desktop_pid = _desktop_pid()
    desktop_ok = _pid_alive(desktop_pid)
    print("AIPet 运行状态")
    print(f"  桌宠     {'在跑（PID ' + str(desktop_pid) + '）' if desktop_ok else '没在跑'}")
    qq_rc = qq_status()
    if log_lines:
        log = ROOT / "data" / "qq.log"
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-log_lines:]
        except OSError as exc:
            lines = [f"读不到日志：{exc}"]
        print(f"\nQQ 日志最后 {log_lines} 行：")
        print("\n".join(lines))
    return 0 if desktop_ok and qq_rc == 0 else 1


def watch_status(interval: float = 5.0, log_lines: int = 0) -> int:
    """Refresh status until Ctrl+C, replacing the old batch-file watch mode."""
    try:
        while True:
            print("\x1b[2J\x1b[H", end="")
            status(log_lines)
            time.sleep(max(.5, interval))
    except KeyboardInterrupt:
        print("\n已停止状态监视。")
        return 0


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


def qq_restart() -> int:
    result = qq_stop()
    if result:
        return result
    return qq_start()


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
