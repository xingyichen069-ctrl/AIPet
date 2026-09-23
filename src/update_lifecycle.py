"""Cooperative update gate shared by desktop, QQ and the detached installer.

No GUI imports or personal configuration are needed here. The installer never
kills a PID: existing services close themselves and subprocesses are awaited.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import time


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence that the process has stopped.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def request_path(root: Path) -> Path:
    return Path(root) / "data" / "update-request.json"


def pending(root: Path) -> bool:
    value = read_json(request_path(root))
    try:
        return pid_alive(int(value.get("pid", 0)))
    except (TypeError, ValueError):
        return False


def ensure_available(root: Path) -> None:
    if pending(root):
        raise RuntimeError("正在安装更新，请等待更新完成后再启动。")


def active_processes(root: Path) -> dict[int, str]:
    data = Path(root) / "data"
    candidates: dict[int, str] = {}
    for name, service in (("desktop.lock", "desktop"), ("qq.pid", "qq")):
        try:
            candidates[int((data / name).read_text(encoding="utf-8").splitlines()[0])] = service
        except (OSError, ValueError, IndexError):
            pass
    for path in (data / "processes").glob("*.json"):
        value = read_json(path)
        try:
            candidates[int(value.get("pid", 0))] = str(value.get("service", "task"))
        except (TypeError, ValueError):
            pass
    return {pid: name for pid, name in candidates.items() if pid_alive(pid)}


@contextlib.contextmanager
def registered(root: Path, service: str, pid: int | None = None):
    pid = os.getpid() if pid is None else pid
    path = Path(root) / "data" / "processes" / f"{pid}.json"
    write_json(path, {"pid": pid, "service": service})
    try:
        ensure_available(root)
        yield
    finally:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def update_gate(root: Path, result: Path):
    """Serialize installers with an OS lock; publish a cooperative stop request."""
    data = Path(root) / "data"
    data.mkdir(parents=True, exist_ok=True)
    handle = (data / "update.lock").open("a+b")
    locked = False
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        write_json(request_path(root), {"pid": os.getpid(), "result": str(result)})
        yield
    finally:
        if locked:
            request_path(root).unlink(missing_ok=True)
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def wait_for_exit(root: Path, wait_pids=(), timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while True:
        active = active_processes(root)
        active.update({pid: "updater" for pid in wait_pids if pid_alive(pid)})
        active.pop(os.getpid(), None)
        if not active:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("桌宠、QQ 或后台任务尚未退出，程序文件没有改动。")
        time.sleep(0.25)
