#!/usr/bin/env python3
"""
backup.py —— 记忆备份

为什么需要它：会话会丢，文件不会。

Cherry Studio 的会话有上下文上限，长对话会被摘要或截断；
会话本身也可能被误删。但 AIPet 的记忆**不在会话里**——
它在 persona/ 和 memory/ 这些纯文本文件里。

这个脚本把那几个文件打包快照，任何时候都能回到某个时间点。

用法：
    python src/backup.py create        # 建一个快照
    python src/backup.py list          # 看有哪些快照
    python src/backup.py restore 名字   # 回滚（会先自动备份当前状态）
    python src/backup.py auto          # 建快照并清理旧的（给定时任务用）
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BACKUP_DIR = M.ROOT / "backups"
KEEP = 30                      # auto 模式保留最近多少个

# 真正需要备份的东西。assets/ view/ 都是可再生的，不备。
SOURCES = [
    ("data", "companion.sqlite3"),  # 对话、约定和记忆撤销记录

    ("persona", "*.md"),
    ("memory", "*.jsonl"),
    ("memory", "*.json"),
    ("memory/summaries", "*.md"),
    ("memory/archive", "*.jsonl"),
    ("data", "config.json"),
    ("data", "thinking.json"),
    ("data", "mood.json"),      # 她当前停在哪个心理点
]


def _collect() -> list[tuple[Path, str]]:
    out = []
    for sub, pat in SOURCES:
        d = M.ROOT / sub
        if not d.exists():
            continue
        for f in sorted(d.glob(pat)):
            out.append((f, f"{sub}/{f.name}"))
    return out


def create(label: str = "") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = M.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}{('-' + label) if label else ''}.zip"
    dest = BACKUP_DIR / name

    files = _collect()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f, arc in files:
            if f.name == "companion.sqlite3":
                # Take a consistent SQLite snapshot even if the desktop is running.
                source = sqlite3.connect(f)
                snapshot = sqlite3.connect(":memory:")
                try:
                    source.backup(snapshot)
                    z.writestr(arc, snapshot.serialize())
                finally:
                    snapshot.close()
                    source.close()
            else:
                z.write(f, arc)
        # 附一份清单，方便回滚时知道当时是什么状态
        z.writestr("_manifest.json", json.dumps({
            "created": M.now_iso(),
            "files": [a for _, a in files],
            "journal_entries": len(M.load_journal()),
        }, ensure_ascii=False, indent=2))

    return dest


def entries_in(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read("memory/journal.jsonl").decode("utf-8")
        return len([l for l in raw.splitlines() if l.strip()])
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeDecodeError):
        return -1


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("*.zip"), reverse=True):
        out.append({
            "name": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "journal_entries": entries_in(f),
            "mtime": M.now().fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return out


def restore(name: str) -> int:
    src = BACKUP_DIR / name
    if not src.exists():
        matches = list(BACKUP_DIR.glob(f"*{name}*")) if BACKUP_DIR.exists() else []
        if not matches:
            print(f"找不到备份：{name}")
            return 1
        src = matches[0]

    # 回滚前先把当前状态存一份，免得回滚本身变成事故
    safety = create("prerestore")
    print(f"已先备份当前状态 → {safety.name}")

    n = 0
    with zipfile.ZipFile(src) as z:
        for arc in z.namelist():
            if arc == "_manifest.json" or arc.endswith("/"):
                continue
            dest = M.ROOT / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(arc) as fh, open(dest, "wb") as out:
                shutil.copyfileobj(fh, out)
            n += 1
    print(f"已从 {src.name} 恢复 {n} 个文件")
    return 0


def prune(keep: int = KEEP) -> int:
    files = sorted(BACKUP_DIR.glob("*.zip"), reverse=True)
    removed = 0
    for f in files[keep:]:
        f.unlink()
        removed += 1
    return removed


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    if cmd == "create":
        p = create(args[1] if len(args) > 1 else "")
        print(f"已备份 → {p.name}  ({p.stat().st_size / 1024:.1f} KB)")

    elif cmd == "auto":
        p = create("auto")
        n = prune()
        print(f"自动备份 → {p.name}；清理了 {n} 个旧快照；"
              f"当前保留 {len(list_backups())} 个")

    elif cmd == "restore":
        if len(args) < 2:
            print("用法：python src/backup.py restore <备份名>")
            sys.exit(1)
        sys.exit(restore(args[1]))

    elif cmd == "list":
        bs = list_backups()
        if not bs:
            print("还没有备份。运行 python src/backup.py create")
            return
        print(f"{'名称':<28} {'大小':>8}  {'记忆条数':>8}  时间")
        print("─" * 60)
        for b in bs[:20]:
            print(f"{b['name']:<28} {b['size_kb']:>6.1f}KB "
                  f"{b['journal_entries']:>8}  {b['mtime']}")
        if len(bs) > 20:
            print(f"… 还有 {len(bs) - 20} 个")
        print(f"\n共 {len(bs)} 个备份，位于 {BACKUP_DIR}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
