"""Verified portable-package installer, also runnable from the staged runtime.

This module uses only the standard library. It never loads user configuration,
personality, memories or Qt while planning or applying a file transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

import update_lifecycle as lifecycle


MANIFEST = "release-manifest.json"
ASSET_NAME = "AIPet-windows-x64.zip"
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * MAX_PACKAGE_BYTES
PUBLIC_DIRS = {"src", "runtime", "assets", "themes", "hiyori_zh-Hans"}
PUBLIC_FILES = {
    "AIPet.exe", "VERSION", "requirements.txt", "README.md", "CHANGELOG.md",
    "Windows使用说明.md", "新增功能说明.md", "使用说明.txt", MANIFEST,
}
DEFAULT_FILES = {"data/config.example.json", "data/thinking.json"}
CUSTOM_FILES = {"assets/character.png", "themes/appearance.json", "themes/custom.qss"}
REQUIRED_FILES = {
    "AIPet.exe", "VERSION", "runtime/python.exe", "runtime/pythonw.exe",
    "src/app_entry.py", "src/windows_launcher.py", "src/portable_update.py",
    "src/update_lifecycle.py", "src/app_paths.py", *DEFAULT_FILES,
}


def digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(name: str) -> str:
    """ZIP/manifest paths must also be unambiguous on Windows."""
    if not isinstance(name, str) or not name or name.startswith(("/", "\\")):
        raise ValueError(f"更新包路径不安全：{name!r}")
    parts = name.replace("\\", "/").split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                or part.split(".", 1)[0].upper() in {
                    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
                }):
            raise ValueError(f"更新包路径不安全：{name!r}")
    return "/".join(parts)


def public_file(name: str) -> bool:
    return (name in PUBLIC_FILES or name in DEFAULT_FILES
            or name.split("/", 1)[0] in PUBLIC_DIRS)


def _safe_destination(root: Path, name: str) -> Path:
    destination = root / safe_name(name)
    for path in (destination, *destination.parents):
        if path == root:
            break
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"更新目标不能经过链接或目录联接：{path}")
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"更新目标越过安装目录：{destination}")
    return destination


def read_manifest(root: Path) -> dict:
    try:
        value = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("更新包缺少有效的发布清单。") from exc
    if (not isinstance(value, dict) or value.get("format") != 1
            or value.get("app") != "AIPet" or value.get("architecture") != "64-bit"
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(value.get("python", "")))
            or not str(value.get("version", "")).strip()
            or not isinstance(value.get("files"), list)):
        raise ValueError("更新包的格式、应用名或运行时架构不兼容。")
    seen: set[str] = set()
    total = 0
    for item in value["files"]:
        if not isinstance(item, dict):
            raise ValueError("发布清单含无效文件项。")
        name = safe_name(item.get("path"))
        if name != item["path"] or name.casefold() in seen or name == MANIFEST:
            raise ValueError(f"发布清单路径重复或不规范：{name}")
        if not public_file(name):
            raise ValueError(f"发布清单不允许覆盖私人目录：{name}")
        if (type(item.get("size")) is not int or item["size"] < 0
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
            raise ValueError(f"发布清单缺少大小或 SHA-256：{name}")
        total += item["size"]
        seen.add(name.casefold())
    if total > MAX_UNPACKED_BYTES or not REQUIRED_FILES <= {item["path"] for item in value["files"]}:
        raise ValueError("更新包不完整或解压体积超过限制。")
    return value


def verify_package(root: Path) -> dict:
    manifest = read_manifest(root)
    expected = {item["path"] for item in manifest["files"]} | {MANIFEST}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("更新包文件与发布清单不一致。")
    for item in manifest["files"]:
        path = _safe_destination(root, item["path"])
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"更新包校验失败：{item['path']}")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != manifest["version"]:
        raise ValueError("VERSION 与发布清单的版本不一致。")
    return manifest


def unpack(archive: Path, destination: Path) -> Path:
    if archive.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("更新包超过 1 GiB。")
    with zipfile.ZipFile(archive) as zf:
        members: list[tuple[zipfile.ZipInfo, str]] = []
        seen: set[str] = set()
        total = 0
        for info in zf.infolist():
            name = safe_name(info.filename[:-1] if info.is_dir() else info.filename)
            key = name.casefold()
            if key in seen:
                raise ValueError(f"更新包含重复路径：{name}")
            seen.add(key)
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise ValueError("更新包不能包含符号链接。")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise ValueError("更新包解压后超过 2 GiB。")
            members.append((info, name))
        roots = {name.split("/", 1)[0] for _, name in members}
        if len(roots) != 1 or not any(name == f"{next(iter(roots))}/{MANIFEST}" for _, name in members):
            raise ValueError("完整发布包必须包含一个根目录和发布清单。")
        for info, name in members:
            target = _safe_destination(destination, name)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out, length=1024 * 1024)
    root = destination / next(iter(roots))
    verify_package(root)
    return root


def health_check(root: Path, manifest: dict) -> None:
    """Import native dependencies and compile app source without user-data writes."""
    code = (
        "import pathlib, platform, sys; "
        "root=pathlib.Path(sys.argv[1]); "
        "assert platform.python_version()==sys.argv[2]; "
        "assert sys.maxsize>2**32; "
        "[compile(p.read_bytes(), str(p), 'exec') for p in (root/'src').rglob('*.py')]; "
        "import PySide6.QtWidgets, live2d.v3, ddgs; "
        "sys.path.insert(0,str(root/'src')); import app_entry; print('PACKAGE_OK')"
    )
    result = subprocess.run(
        [str(root / "runtime" / "python.exe"), "-I", "-B", "-c", code,
         str(root), manifest["python"]], cwd=root, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or "PACKAGE_OK" not in result.stdout:
        raise RuntimeError(f"新版本启动检查失败：{(result.stderr or result.stdout)[-600:]}")


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + f".update-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def _rollback(target: Path, backup: Path, journal: dict) -> None:
    errors = []
    for name in reversed(journal["applied"]):
        destination = _safe_destination(target, name)
        try:
            if name in journal["existed"]:
                _copy_atomic(backup / name, destination)
            else:
                destination.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    journal["state"] = "rollback_failed" if errors else "rolled_back"
    journal["rollback_errors"] = errors
    lifecycle.write_json(backup / "transaction.json", journal)
    if errors:
        raise RuntimeError(f"自动恢复有文件未完成，备份保留在 {backup}：{'；'.join(errors)[:500]}")


def install_staged(staged: Path, target: Path) -> dict:
    """Apply only manifest-owned public files; keep a durable rollback journal."""
    staged, target = staged.resolve(), target.resolve()
    if staged == target or staged.is_relative_to(target / "runtime"):
        raise ValueError("更新进程必须使用独立暂存目录。")
    manifest = verify_package(staged)
    health_check(staged, manifest)
    skipped = []
    incoming = []
    for item in manifest["files"]:
        name = item["path"]
        path = _safe_destination(target, name)
        if name in CUSTOM_FILES | DEFAULT_FILES and path.exists():
            skipped.append(name)
        elif not path.is_file() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            incoming.append(name)
    removed = []
    if (target / MANIFEST).exists():
        old_manifest = read_manifest(target)
        new_names = {item["path"].casefold() for item in manifest["files"]}
        for item in old_manifest["files"]:
            name = item["path"]
            path = _safe_destination(target, name)
            if (name.casefold() not in new_names and path.is_file()
                    and name not in CUSTOM_FILES | DEFAULT_FILES):
                removed.append(name)
    incoming.append(MANIFEST)
    changes = incoming + removed
    backup = _safe_destination(target, f"backups/package-update-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    backup.mkdir(parents=True)
    journal = {"target": str(target), "state": "prepared", "version": manifest["version"],
               "existed": [], "applied": [], "changed": incoming, "removed": removed}
    for name in changes:
        destination = _safe_destination(target, name)
        if destination.exists():
            _copy_atomic(destination, backup / name)
            journal["existed"].append(name)
    lifecycle.write_json(backup / "transaction.json", journal)
    try:
        for name in changes:
            destination = _safe_destination(target, name)
            journal["state"] = "installing"
            journal["applied"].append(name)
            lifecycle.write_json(backup / "transaction.json", journal)
            if name in removed:
                destination.unlink()
            else:
                _copy_atomic(staged / name, destination)
        health_check(target, manifest)
    except Exception:
        _rollback(target, backup, journal)
        raise
    journal["state"] = "complete"
    lifecycle.write_json(backup / "transaction.json", journal)
    return {"ok": True, "updated": True, "version": manifest["version"],
            "changed": incoming, "removed": removed, "skipped": skipped, "backup": str(backup)}


def stage_archive(archive: Path, target: Path, expected_sha256: str = "") -> Path:
    if expected_sha256 and digest(archive) != expected_sha256.lower():
        raise ValueError("发布包 SHA-256 与下载信息不一致。")
    work = _safe_destination(target.resolve(), "work/package-updates")
    work.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="stage-", dir=work))
    return unpack(archive, directory)


def launch_staged(staged: Path, target: Path, *, wait_pids=(), restart: bool = True) -> dict:
    manifest = verify_package(staged)
    health_check(staged, manifest)
    lifecycle.ensure_available(target)
    result = target / "data" / "update-result.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    ready = staged.parent / "worker-ready.json"
    command = [str(staged / "runtime" / "python.exe"), "-I", "-B",
               str(staged / "src" / "portable_update.py"), "apply", "--target", str(target),
               "--ready", str(ready)]
    for pid in wait_pids:
        if pid > 0:
            command += ["--wait-pid", str(pid)]
    if restart:
        command.append("--restart")
    log = staged.parent / "update-worker.log"
    with log.open("ab") as out:
        process = subprocess.Popen(command, cwd=staged, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=out, close_fds=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.is_file():
            return {"ok": True, "updated": False, "pending": True,
                    "version": manifest["version"], "result": str(result), "log": str(log)}
        if process.poll() is not None:
            raise RuntimeError(f"独立更新进程未启动，请查看 {log}")
        time.sleep(0.1)
    raise TimeoutError(f"独立更新进程没有就绪，请查看 {log}")


def _restart(target: Path, services: set[str]) -> None:
    for service in sorted(services):
        if service == "desktop":
            command = [str(target / "runtime" / "pythonw.exe"), str(target / "src" / "windows_launcher.py")]
        elif service == "qq":
            command = [str(target / "runtime" / "pythonw.exe"), str(target / "src" / "qq_bridge.py"), "run"]
        else:
            continue
        subprocess.Popen(command, cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description="AIPet 内部整包更新器")
    parser.add_argument("action", choices=["apply", "rollback"])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--wait-pid", type=int, action="append", default=[])
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    target = args.target.resolve()
    staged = Path(__file__).resolve().parent.parent
    result_file = target / "data" / "update-result.json"
    services = set(lifecycle.active_processes(target).values())
    result = {"ok": False, "updated": False}
    try:
        with lifecycle.update_gate(target, result_file):
            if args.ready:
                lifecycle.write_json(args.ready, {"pid": os.getpid()})
            lifecycle.write_json(result_file, {**result, "pending": True, "stage": str(staged)})
            lifecycle.wait_for_exit(target, args.wait_pid)
            if args.action == "rollback":
                if not args.backup or not args.backup.resolve().is_relative_to(target / "backups"):
                    raise ValueError("恢复目录必须位于本安装的 backups 下。")
                journal = lifecycle.read_json(args.backup / "transaction.json")
                if journal.get("target") != str(target) or journal.get("state") == "complete":
                    raise ValueError("只能恢复本安装未完成的更新事务。")
                _rollback(target, args.backup, journal)
                result = {"ok": True, "updated": False, "rolled_back": True}
            else:
                result = install_staged(staged, target)
    except Exception as exc:
        result = {"ok": False, "updated": False, "error": f"{type(exc).__name__}: {exc}", "stage": str(staged)}
    lifecycle.write_json(result_file, result)
    if args.restart and (result.get("ok") or result.get("error")):
        # A rolled-back installation can start again; a failed rollback remains
        # visible in the result and the normal startup log.
        _restart(target, services)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    # -I deliberately excludes the script directory. Add only our own sibling
    # modules for the independent standard-library updater.
    raise SystemExit(main())
