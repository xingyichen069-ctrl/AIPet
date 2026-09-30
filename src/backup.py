#!/usr/bin/env python3
"""Private snapshots; restoration exports a separate directory by default.

    python src/backup.py create [label]
    python src/backup.py list
    python src/backup.py restore <name>
    python src/backup.py restore <name> --in-place
    python src/backup.py auto

Exit the desktop and other users of its files before an in-place restoration.
Credential files and caches are excluded, but archives remain private: they
contain conversations, personas and configuration with any embedded keys.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
import zipfile
import zlib

# Importing memory would initialize live configuration even for list/export.
BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"
KEEP = 30
SQLITE_BUSY_SECONDS = 3.0
DATABASE = "data/companion.sqlite3"
SOURCES = [
    ("data", "companion.sqlite3"),
    ("data", "qq.json"),
    ("data", "qq_history.json"),
    ("data", "desktop_ui.json"),
    ("data", "appearance.json"),
    ("persona", "*.md"),
    ("persona", "active.json"),
    ("persona/characters", "**/*"),
    ("memory", "*.jsonl"),
    ("memory", "*.json"),
    ("memory/summaries", "*.md"),
    ("memory/archive", "*.jsonl"),
    ("data", "config.json"),
    ("data", "thinking.json"),
    ("data", "mood.json"),
    ("data", "mood_catalog.json"),
    ("data", "mood_log.jsonl"),
    ("data", "persona_moods.json"),
    ("data/moods", "*.json"),
]


class BackupError(ValueError):
    """An actionable error which never includes configuration values."""


def _root() -> Path:
    # BACKUP_DIR belongs to one installation; tests substitute that whole tree.
    return BACKUP_DIR.parent.resolve()


def _arc(name: str) -> str:
    """Require a portable ZIP path, including Windows rules on other OSes."""
    if not isinstance(name, str) or not name or "\\" in name or PureWindowsPath(name).drive:
        raise BackupError("备份含不安全的成员路径。")
    parts = name.split("/")
    if any(not p or p in (".", "..") or p.rstrip(" .") != p
           or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
           or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", p.split(".")[0])
           for p in parts):
        raise BackupError("备份含不安全或不能在 Windows 使用的成员路径。")
    return PurePosixPath(name).as_posix()


def _link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _safe_path(root: Path, relative: str) -> Path:
    relative = _arc(relative)
    root = root.resolve()
    path = root
    for part in relative.split("/"):
        path = path / part
        if _link(path):
            raise BackupError(f"路径经过符号链接或目录联接，不能自动处理：{relative}")
    if not path.resolve().is_relative_to(root):
        raise BackupError(f"路径位于安装目录外：{relative}")
    return path


def _area(name: str) -> Path:
    path = _safe_path(_root(), name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _allowed(name: str) -> bool:
    parts = name.split("/")
    if any(p.lower() in ("cache", "__pycache__", "secrets.json", "qq_token.json")
           or p.startswith(".") for p in parts):
        return False
    if Path(name).suffix.lower() in (".tmp", ".py", ".pyc", ".pyo", ".bat", ".cmd", ".exe", ".dll",
                                    ".ps1", ".vbs", ".sh", ".command", ".js", ".lnk", ".key", ".pem"):
        return False
    for sub, pattern in SOURCES:
        if name.startswith(sub + "/"):
            tail = name[len(sub) + 1:]
            if pattern == "**/*" or ("/" not in tail and fnmatch.fnmatchcase(tail, pattern)):
                return True
    return False


def _collect() -> list[tuple[Path, str]]:
    root = _root()
    out = {}
    folded = set()
    for sub, pattern in SOURCES:
        directory = _safe_path(root, sub)
        for path in sorted(directory.glob(pattern)):
            name = path.relative_to(root).as_posix()
            if not _allowed(name):
                continue
            path = _safe_path(root, name)
            if not path.is_file() or name in out:
                continue
            if name.casefold() in folded:
                raise BackupError("私人资料含大小写冲突的文件名，不能制作跨平台备份。")
            folded.add(name.casefold())
            out[name] = path
    return [(path, name) for name, path in out.items()]


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _copy_sqlite(source: Path, target: Path) -> None:
    """SQLite owns database/WAL changes; never replace a live database file."""
    for database in (source, target):
        for suffix in ("", "-wal", "-shm", "-journal"):
            if _link(Path(str(database) + suffix)):
                raise BackupError("数据库或日志经过符号链接，不能自动备份或恢复。")
    last_progress = time.monotonic()

    def progress(status, remaining, total):
        nonlocal last_progress
        if status not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress >= SQLITE_BUSY_SECONDS:
            raise BackupError("数据库仍被占用，操作已停止。请退出桌宠及其他使用此目录的程序后重试。")

    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as old:
            with closing(sqlite3.connect(target, timeout=0.1)) as new:
                old.backup(new, pages=128, progress=progress, sleep=0.02)
    except sqlite3.Error as exc:
        raise BackupError("无法读取或写入陪伴数据库。请保留原文件，退出相关程序后检查其副本。") from exc


def _snapshot(source: Path, target: Path, name: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if name == DATABASE:
        _copy_sqlite(source, target)
    else:
        before = _digest(source)
        shutil.copy2(source, target)
        if _digest(target) != before or _digest(source) != before:
            raise BackupError(f"备份时文件发生变化：{name}。请先退出相关程序再试。")


def _publish(source: Path, target: Path) -> None:
    """Publish a finished file atomically without replacing an existing one."""
    if os.name == "nt":
        os.rename(source, target)  # Windows rename refuses an existing target.
    else:
        os.link(source, target)
        source.unlink()


def _unique(label: str, suffix: str = "") -> str:
    label = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", label).strip("-")[:60]
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    return f"{stamp}{('-' + label) if label else ''}-{uuid.uuid4().hex[:12]}{suffix}"


def create(label: str = "") -> Path:
    backup_dir = _area("backups")
    files = _collect()
    with tempfile.TemporaryDirectory(prefix=".creating-", dir=backup_dir) as temporary:
        stage = Path(temporary)
        hashes = {}
        for source, name in files:
            _snapshot(_safe_path(_root(), name), stage / name, name)
            hashes[name] = _digest(stage / name)
        journal = stage / "memory/journal.jsonl"
        manifest = {"format_version": 2, "created": datetime.now().astimezone().isoformat(),
                    "files": [name for _, name in files], "sha256": hashes,
                    "journal_entries": len(journal.read_bytes().splitlines()) if journal.exists() else 0}
        archive = stage / "complete.zip"
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
            for _, name in files:
                z.write(stage / name, name)
            z.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        with archive.open("rb+") as handle:
            os.fsync(handle.fileno())
        for _ in range(10):
            target = backup_dir / _unique(label, ".zip")
            try:
                _publish(archive, target)
                return target
            except FileExistsError:
                continue
        raise BackupError("无法取得独立的备份文件名；现有备份保持不动。")


def entries_in(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read("memory/journal.jsonl").decode("utf-8")
        return len([line for line in raw.splitlines() if line.strip()])
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeError, RuntimeError, zlib.error):
        return -1


def list_backups() -> list[dict]:
    backup_dir = _safe_path(_root(), "backups")
    if not backup_dir.exists():
        return []
    return [{"name": path.name, "size_kb": round(path.stat().st_size / 1024, 1),
             "journal_entries": entries_in(path),
             "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")}
            for path in sorted(backup_dir.glob("*.zip"), reverse=True) if path.is_file() and not _link(path)]


def _find(name: str) -> Path:
    if "/" in name or "\\" in name or _arc(name) != name:
        raise BackupError("请使用 backups 中的备份文件名，不要填写安装外路径。")
    exact = _safe_path(_root(), "backups/" + name)
    if exact.is_file() and exact.suffix.lower() == ".zip":
        return exact
    matches = [p for p in BACKUP_DIR.glob("*.zip") if name in p.name and p.is_file()]
    if len(matches) != 1:
        raise BackupError("找不到唯一的备份；请运行 list 并填写完整的 ZIP 文件名。")
    return _safe_path(_root(), "backups/" + matches[0].name)


def _validate_payload(path: Path, name: str) -> None:
    if name == DATABASE:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("备份内的陪伴数据库未通过完整性检查。")
                required = {"sessions": {"id", "created", "active"},
                            "messages": {"id", "session", "role", "text", "context", "status", "created"},
                            "tasks": {"id", "kind", "title", "due", "next_visit", "status", "created"}}
                for table, columns in required.items():
                    actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    if not columns <= actual:
                        raise BackupError("备份内的陪伴数据库结构不受支持；请保留旧包并人工核对。")
        except sqlite3.Error as exc:
            raise BackupError("备份内的陪伴数据库无法读取，未恢复任何当前资料。") from exc
    elif path.suffix.lower() in (".json", ".jsonl"):
        try:
            text = path.read_text(encoding="utf-8-sig")
            if path.suffix.lower() == ".jsonl":
                if any(not isinstance(json.loads(line), dict) for line in text.splitlines() if line.strip()):
                    raise ValueError()
                return
            value = json.loads(text)
            object_names = {"config.json", "thinking.json", "active.json", "MOODS.json",
                            "mood.json", "mood_catalog.json", "persona_moods.json",
                            "desktop_ui.json", "appearance.json", "qq.json", "qq_history.json"}
            expected = (list if path.name == "DIALOGUE.json" else
                        dict if path.name in object_names or name.startswith("data/moods/") else (dict, list))
            if not isinstance(value, expected):
                raise ValueError()
            if name == "persona/active.json" and (not isinstance(value.get("id"), str)
                    or not re.fullmatch(r"[a-zA-Z0-9_\-\u4e00-\u9fff]+", value["id"])):
                raise ValueError()
            if name == "data/config.json":
                for section in ("paths", "retrieval", "compression", "privacy", "tools", "knowledge", "live2d"):
                    if section in value and not isinstance(value[section], dict):
                        raise ValueError()
            if name == "data/thinking.json" and (not isinstance(value.get("presets"), dict)
                    or not value["presets"] or not isinstance(value.get("current"), str)
                    or (value["current"] != "auto" and value["current"] not in value["presets"])):
                raise ValueError()
        except (ValueError, UnicodeError) as exc:
            raise BackupError(f"备份中的 {name} 格式或结构不对；当前资料未改动。") from exc


def _stage_archive(source: Path, stage: Path) -> tuple[list[str], bool]:
    names = []
    seen = set()
    with zipfile.ZipFile(source) as z:
        infos = z.infolist()
        for info in infos:
            original = info.orig_filename
            name = _arc(original[:-1] if info.is_dir() else original)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or (info.external_attr & 0x400):
                raise BackupError("备份含链接或特殊文件，不能自动恢复。")
            if name.casefold() in seen:
                raise BackupError("备份含重复或大小写冲突的成员路径。")
            seen.add(name.casefold())
            if info.is_dir():
                if name.split("/")[0] not in ("persona", "memory", "data"):
                    raise BackupError("备份包含不允许的目录。")
                continue
            if name != "_manifest.json" and not _allowed(name):
                raise BackupError(f"备份含未允许恢复的文件：{name}")
            names.append(name)
        files = [name for name in names if name != "_manifest.json"]
        if not files:
            raise BackupError("备份中没有可恢复的私人资料。")
        file_keys = {name.casefold() for name in files}
        for name in files:
            if any(parent.as_posix().casefold() in file_keys for parent in PurePosixPath(name).parents):
                raise BackupError("备份成员同时被用作文件和目录。")
        if sum(info.file_size for info in infos) > shutil.disk_usage(stage).free:
            raise BackupError("没有足够空间暂存并校验整个备份。")
        # Reading each complete member verifies CRC before any live write.
        for name in names:
            target = _safe_path(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
    manifest_path = stage / "_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if (not isinstance(manifest, dict) or type(manifest.get("format_version", 1)) is not int
                    or manifest.get("format_version", 1) not in (1, 2)):
                raise ValueError()
            declared = manifest["files"]
            if not isinstance(declared, list) or any(not isinstance(n, str) for n in declared) or sorted(declared) != sorted(files):
                raise ValueError()
            if manifest.get("format_version") == 2:
                hashes = manifest["sha256"]
                if not isinstance(hashes, dict) or set(hashes) != set(files):
                    raise ValueError()
                if any(hashes[name] != _digest(stage / name) for name in files):
                    raise ValueError()
        except (ValueError, KeyError, UnicodeError, TypeError) as exc:
            raise BackupError("备份清单或文件摘要不匹配，未恢复任何当前资料。") from exc
    for name in files:
        _validate_payload(stage / name, name)
    if "persona/active.json" in files:
        active = json.loads((stage / "persona/active.json").read_text(encoding="utf-8-sig"))["id"]
        if f"persona/characters/{active}/SOUL.md" not in files:
            raise BackupError("备份缺少当前人格的 SOUL，不能将它当作完整人格快照恢复。")
    legacy = "persona/SOUL.md" in files and not any(n.startswith("persona/characters/") for n in files)
    return files, legacy


def _export(stage: Path, names: list[str], legacy: bool, source: Path) -> Path:
    backup_dir = _area("backups")
    with tempfile.TemporaryDirectory(prefix=".export-", dir=backup_dir) as temporary:
        tree = Path(temporary) / "files"
        tree.mkdir()
        for name in names:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stage / name, target)
        if (stage / "_manifest.json").exists():
            shutil.copy2(stage / "_manifest.json", tree / "_manifest.json")
        note = (f"来源：{source.name}\n这是独立导出的资料目录，供核对和比较；当前安装未修改。\n"
                "它不是可直接启动的完整安装，也没有自动更换当前人格。\n")
        if legacy:
            note += "旧格式只备份根 SOUL，没有保存新版角色库；不要覆盖现有角色库来推测恢复结果。\n"
        (tree / "RESTORE_INFO.txt").write_text(note, encoding="utf-8")
        target = backup_dir / ("restored-" + _unique("compare"))
        if target.exists():
            raise BackupError("导出目录已存在，请重新运行以生成独立目录。")
        os.rename(tree, target)
        return target


def _write_staged(source: Path, target: Path, name: str, existed: bool) -> None:
    if name == DATABASE:
        _copy_sqlite(source, target)
    elif existed:
        os.replace(source, target)
    else:
        _publish(source, target)


def _restore_in_place(stage: Path, names: list[str], legacy: bool) -> tuple[Path, Path]:
    root = _root()
    if legacy and any((root / "persona/characters").glob("*/SOUL.md")):
        raise BackupError("这是只含根 SOUL 的旧备份，当前已有角色库。请省略 --in-place 导出比较，"
                          "不要将旧根文件恢复误认为当前人格已回滚。")
    for name in names:
        target = _safe_path(root, name)
        if target.exists() and not target.is_file():
            raise BackupError(f"恢复目标不是普通文件：{name}")
    safety = create("prerestore")
    transaction = Path(tempfile.mkdtemp(prefix="restore-before-", dir=_area("backups")))
    items = []
    for name in names:
        target = _safe_path(root, name)
        existed = target.exists()
        if existed:
            _snapshot(target, transaction / name, name)
        items.append({"path": name, "existed": existed})
    manifest = {"status": "ready", "safety": safety.name, "files": items}

    def record(status):
        manifest["status"] = status
        (transaction / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    record("ready")
    committed = []
    try:
        for item in items:
            name, existed = item["path"], item["existed"]
            target = _safe_path(root, name)
            if target.exists() != existed:
                raise BackupError("恢复期间目标文件发生变化，请先退出所有相关程序。")
            if existed and name != DATABASE and _digest(target) != _digest(transaction / name):
                raise BackupError("恢复期间目标文件内容发生变化，请先退出所有相关程序。")
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == DATABASE and not existed:
                target.open("xb").close()
                committed.append(item)
            _write_staged(stage / name, target, name, existed)
            if name != DATABASE or existed:
                committed.append(item)
        record("complete")
    except Exception as exc:
        failures = []
        for item in reversed(committed):
            name = item["path"]
            try:
                target = _safe_path(root, name)
                if item["existed"]:
                    if name == DATABASE:
                        _copy_sqlite(transaction / name, target)
                    else:
                        rollback = stage / "rollback" / name
                        rollback.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(transaction / name, rollback)
                        os.replace(rollback, target)
                else:
                    if name == DATABASE and any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                        raise BackupError("新数据库仍被使用，保留它供人工核对。")
                    target.unlink(missing_ok=True)
            except Exception:
                failures.append(name)
        manifest["rollback_failed"] = failures
        try:
            record("rollback-incomplete" if failures else "rolled-back")
        except OSError:
            failures.append("manifest.json")
        detail = "部分回退失败，请按清单人工恢复" if failures else "本次写入已回退"
        reason = str(exc) if isinstance(exc, BackupError) else "写入失败，请检查权限和磁盘空间。"
        raise BackupError(f"恢复中断，{detail}。{reason}\n安全快照：{safety}；原文件与清单：{transaction}") from exc
    return safety, transaction


def restore(name: str, *, in_place: bool = False) -> int:
    try:
        source = _find(name)
        with tempfile.TemporaryDirectory(prefix="restore-check-", dir=_area("work")) as temporary:
            stage = Path(temporary)
            names, legacy = _stage_archive(source, stage)
            if not in_place:
                target = _export(stage, names, legacy, source)
                print(f"已校验并导出 {len(names)} 个文件 → {target}\n当前安装未修改，请先比较再决定如何使用。")
                if legacy:
                    print("这是旧格式根 SOUL 备份，没有完整角色库；导出不会更换当前人格。")
            else:
                print("原地恢复要求桌宠及相关程序已经退出；数据库被占用时会停止。", flush=True)
                safety, transaction = _restore_in_place(stage, names, legacy)
                print(f"已恢复包中的 {len(names)} 个文件，未删除包外资料。\n"
                      f"恢复前完整快照：{safety}\n回退清单：{transaction}\n"
                      "请重启后核对人格、情绪、历史话题和约定；恢复不等于已经完成这些实测。")
        return 0
    except (BackupError, OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
        print(f"未完成恢复：{exc}")
        return 1


def prune(keep: int = KEEP) -> int:
    files = sorted(_safe_path(_root(), "backups").glob("*.zip"), reverse=True)
    removed = 0
    for path in files[max(0, keep):]:
        _safe_path(_root(), "backups/" + path.name).unlink()
        removed += 1
    return removed


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="私人资料备份；默认恢复到独立目录供比较")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list")
    create_parser = commands.add_parser("create")
    create_parser.add_argument("label", nargs="?", default="")
    commands.add_parser("auto")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("name", help="backups 中的 ZIP 文件名")
    restore_parser.add_argument("--in-place", action="store_true", help="确认已退出相关程序，备份后覆盖当前资料")
    args = parser.parse_args()
    try:
        if args.command == "restore":
            return restore(args.name, in_place=args.in_place)
        if args.command in ("create", "auto"):
            path = create(args.label if args.command == "create" else "auto")
            print(f"已备份 → {path.name} ({path.stat().st_size / 1024:.1f} KB)")
            if args.command == "auto":
                print(f"已清理 {prune()} 个旧快照；保留 {len(list_backups())} 个。")
        else:
            rows = list_backups()
            for row in rows[:20]:
                count = f"{row['journal_entries']} 条" if row["journal_entries"] >= 0 else "未知（无日志或包不可读取）"
                print(f"{row['name']}  {row['size_kb']} KB  记忆 {count}  {row['mtime']}")
            print(f"共 {len(rows)} 个备份，位于 {BACKUP_DIR}")
        return 0
    except (BackupError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"备份未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
