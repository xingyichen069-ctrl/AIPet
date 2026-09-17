#!/usr/bin/env python3
"""
migrate.py —— 换新装的时候，把私人内容搬过去

═══════════════════════════════════════════════════════════════
  什么时候需要它
═══════════════════════════════════════════════════════════════

**先说清楚：直接把新版的 zip 解压到旧目录上覆盖，什么都不会丢。**
仓库里的 zip 根本不含 `persona/` `memory/` `data/`（都在 .gitignore 里），
而解压不会删掉压缩包里没有的文件。那条路不用这个脚本。

需要它的是另一种换法：**解压到一个新目录，然后把旧的整个丢掉**。
那时候人格、记忆、密钥、信道绑定全在新目录里没有，得搬。

上一次（v0.3.1 → v0.4.0）是手工搬的，这次写成脚本。

═══════════════════════════════════════════════════════════════
  它搬什么、不搬什么
═══════════════════════════════════════════════════════════════

搬的是**代码之外、重建不出来的东西**：人格、记忆、密钥、QQ 绑定、
你的调参、心理点日志、约定、知识库、历史备份。

不搬的是**能重建或不该带的**：运行环境（重跑一次准备环境.bat）、
搜索缓存、运行时状态文件。

★ 特别说一条：`data/secrets.json` 在这里**是要搬的**。
   `src/backup.py` 故意把它排除在外 —— 那个 zip 可能被拷走、发出去，
   密钥不该跟着跑。这里不一样，是同一台机器上的目录到目录，
   不搬的话你就得重新填一遍 API key 和 QQ 凭据，那正是这个脚本要
   解决的问题。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python tools/migrate.py D:\\旧版\\AIPet            # 先看清单，问你要不要搬
    python tools/migrate.py D:\\旧版\\AIPet --dry-run  # 只看，不动
    python tools/migrate.py D:\\旧版\\AIPet --yes      # 不问，直接搬
    python tools/migrate.py D:\\旧版\\AIPet --into D:\\新版\\AIPet

目标默认是**这个脚本所在的那个安装**（也就是新装自己）。
真的开搬之前，目标里被覆盖的文件会先存进 `backups/迁移前-<时间>/`，
搬错了还能翻回来。

    python tools/migrate.py selftest    # 自检（造两个假目录真搬一遍）
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  清单
# ═══════════════════════════════════════════════════════════════

# (源里的通配, 类别, 一句说明)
ITEMS: list[tuple[str, str, str]] = [
    ("persona/*.md",              "人格",   "SOUL / BOUNDARIES / PROFILE —— 她是谁"),
    # ★ 必须写 **/*，不能只写 **。pathlib 的 "memory/**" 只吐目录，
    #   一个文件都不给，"记忆会搬" 那条会静默地什么都不搬。
    ("memory/**/*",               "记忆",   "时间线、人物卡、压缩归档"),
    ("data/secrets.json",         "密钥",   "DeepSeek key、QQ 凭据、读图接口"),
    ("data/qq_token.json",        "密钥",   "QQ access_token 缓存"),
    ("data/qq.json",              "信道",   "主人绑定 + 认领口令状态"),
    ("data/qq_history.json",      "信道",   "各会话最近对话"),
    ("data/config.json",          "配置",   "搜索后端、代理、沙箱根、隐私黑名单"),
    ("data/thinking.json",        "配置",   "五档预设 + 自动判定规则"),
    ("data/appearance.json",      "外观",   "主题、字号、玻璃质感"),
    ("data/desktop_ui.json",      "外观",   "话题草稿、附件快照、窗口布局"),
    ("data/pet_state.json",       "外观",   "桌宠位置、穿透、置顶"),
    ("data/mood.json",            "心理点", "她此刻停在哪"),
    ("data/mood_log.jsonl",       "心理点", "每轮记的那一笔"),
    ("data/companion.sqlite3",    "陪伴",   "约定、安静陪伴、话题"),
    ("data/knowledge_index.json", "知识库", "本地资料的索引"),
    ("knowledge/**/*",            "知识库", "你自己的资料"),
    ("backups/*.zip",             "备份",   "历史备份包（--no-backups 可跳过）"),
]

# 目标里出现这些，说明它不是一份干净的新装，得 --force 才动
FOOTPRINT = ("persona/SOUL.md", "memory/journal.jsonl", "data/secrets.json")

# (路径, 为什么不搬)
SKIP: list[tuple[str, str]] = [
    ("runtime/  .venv/",                 "运行环境。新装里跑一次 准备环境.bat 就有"),
    ("data/cache/",                      "搜索缓存、启动日志、预览图，会自己重建"),
    ("data/qq.log  qq.pid  qq_status.json", "运行时状态。搬过去会让桌宠以为 QQ 还连着"),
    ("view/",                            "记忆面板，程序生成"),
    ("themes/  assets/",                 "在仓库里，新版自带"),
    ("__pycache__/",                     "字节码"),
]


def _looks_like_install(root: Path) -> str:
    """像不像一份 AIPet。返回空串表示像，否则是原因。"""
    if not root.is_dir():
        return f"{root} 不是目录"
    if not (root / "src" / "pet.py").exists():
        return f"{root} 里没有 src/pet.py"
    return ""


def collect(source: Path, with_backups: bool = True) -> tuple[list[tuple[Path, str, str]], list[str]]:
    """
    按清单从旧装里挑出要搬的文件。

    返回 (文件列表, 没找到的条目)。文件列表每项是 (路径, 类别, 说明)。
    """
    found: list[tuple[Path, str, str]] = []
    missing: list[str] = []
    seen: set[Path] = set()

    for pattern, kind, note in ITEMS:
        if not with_backups and kind == "备份":
            continue
        hits = sorted(p for p in source.glob(pattern) if p.is_file())
        if not hits:
            missing.append(f"{pattern}（{kind}）")
            continue
        for p in hits:
            if p in seen:
                continue           # memory/** 和别的条目可能重叠
            seen.add(p)
            found.append((p, kind, note))
    return found, missing


def plan(source: Path, target: Path, with_backups: bool = True):
    """算出要搬什么、覆盖什么、目标的备份放哪。"""
    files, missing = collect(source, with_backups)
    rows = []
    for p, kind, note in files:
        rel = p.relative_to(source)
        dst = target / rel
        rows.append({
            "src": p, "rel": rel, "dst": dst, "kind": kind, "note": note,
            "exists": dst.exists(),
            "size": p.stat().st_size,
        })
    return rows, missing


def footprint(target: Path) -> list[str]:
    """目标里已有的私人痕迹。空列表 = 干净的。"""
    return [rel for rel in FOOTPRINT if (target / rel).exists()]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def apply(rows, target: Path) -> tuple[int, int, Path | None]:
    """
    真搬。返回 (搬了几个, 覆盖了几个, 备份目录)。

    覆盖前先把目标里那份存到 backups/迁移前-<时间>/ —— 搬错了能翻回来。
    """
    over = [r for r in rows if r["exists"]]
    backup_dir = None
    if over:
        backup_dir = target / "backups" / f"迁移前-{stamp()}"
        for r in over:
            dst_backup = backup_dir / r["rel"]
            dst_backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r["dst"], dst_backup)

    copied = 0
    for r in rows:
        r["dst"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(r["src"], r["dst"])
        copied += 1
    return copied, len(over), backup_dir


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    import tempfile

    fails = 0

    def check_(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("迁移自检\n")

    with tempfile.TemporaryDirectory(prefix="aipet_migrate_") as tmp:
        old = Path(tmp) / "旧装"
        new = Path(tmp) / "新装"

        # ── 造一份「旧装」──
        (old / "src").mkdir(parents=True)
        (old / "src" / "pet.py").write_text("# fake", encoding="utf-8")
        (old / "persona").mkdir()
        (old / "persona" / "SOUL.md").write_text("我是旧人格", encoding="utf-8")
        (old / "memory").mkdir()
        (old / "memory" / "journal.jsonl").write_text('{"id":"1"}\n', encoding="utf-8")
        (old / "data").mkdir()
        (old / "data" / "secrets.json").write_text('{"deepseek_api_key":"sk-OLD"}', encoding="utf-8")
        (old / "data" / "config.json").write_text('{"tools":{}}', encoding="utf-8")
        (old / "data" / "qq.json").write_text('{"bindings":{}}', encoding="utf-8")
        # ★ 旧装的 thinking.json 要和仓库默认值**不一样**，否则「被覆盖」
        #   那条断言是白测的：两边一样，压根分不出搬没搬。
        (old / "data" / "thinking.json").write_text('{"presets":{"deep":{"调过的":true}}}',
                                                    encoding="utf-8")
        (old / "data" / "cache").mkdir()
        (old / "data" / "cache" / "search.json").write_text("{}", encoding="utf-8")
        (old / "data" / "qq.log").write_text("log", encoding="utf-8")
        (old / "backups").mkdir()
        (old / "backups" / "20260101-000000.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        # ── 造一份「新装」（仓库里的样子：只有代码和 data/thinking.json）──
        (new / "src").mkdir(parents=True)
        (new / "src" / "pet.py").write_text("# new", encoding="utf-8")
        (new / "data").mkdir()
        (new / "data" / "thinking.json").write_text('{"presets":{}}', encoding="utf-8")

        check_("认出旧装", _looks_like_install(old) == "")
        check_("认出非安装目录", _looks_like_install(new / "src") != "")
        check_("新装里没有私人痕迹", footprint(new) == [])

        rows, missing = plan(old, new)
        rels = {str(r["rel"]) for r in rows}

        check_("人格会搬", "persona\\SOUL.md" in rels or "persona/SOUL.md" in rels)
        check_("记忆会搬", any("journal" in r for r in rels))
        check_("★ 密钥会搬", any("secrets.json" in r for r in rels))
        check_("信道绑定会搬", any("qq.json" in r for r in rels))
        check_("缓存不搬", not any("cache" in r for r in rels))
        check_("日志不搬", not any(r.endswith("qq.log") for r in rels))

        copied, over, backup_dir = apply(rows, new)
        check_("真搬完了", copied == len(rows), f"{copied} 个")
        check_("人格到位",
              (new / "persona" / "SOUL.md").read_text(encoding="utf-8") == "我是旧人格")
        check_("密钥到位",
              "sk-OLD" in (new / "data" / "secrets.json").read_text(encoding="utf-8"))
        check_("★ 新装的 thinking.json 被旧的覆盖（那是你的调参）",
              "调过的" in (new / "data" / "thinking.json").read_text(encoding="utf-8"))
        check_("★ 被覆盖的那份留了底，是仓库默认值",
              bool(backup_dir)
              and (backup_dir / "data" / "thinking.json").exists()
              and "调过的" not in (backup_dir / "data" / "thinking.json").read_text(encoding="utf-8"),
              str(backup_dir.name) if backup_dir else "（没建备份）")

        # 再跑一次：这次目标已经有私人内容了
        check_("目标再次迁移时会被认出「不干净」", footprint(new) != [])

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def _lnk_points_to(link: Path, needle: str) -> bool:
    """
    这个 .lnk 指不指着 needle 那个目录。

    ★ 不去精确解析 .lnk 格式，也不用 COM（那就得拖进 pywin32）。
      目标路径在文件里是以明文存着的，local ANSI 一份、UTF-16LE 一份，
      在原始字节里按两种编码找一遍就够了 —— 这是个警告，不是判据。

      值得这么做是因为：一开始我把 Startup 里所有 .lnk 都列出来了，
      连 Ollama 的都在里面。那种警告没人会信，列了等于没列。
    """
    try:
        raw = link.read_bytes()
    except OSError:
        return False
    want = needle.lower()
    for enc in ("utf-16-le", "latin-1"):
        try:
            if want in raw.decode(enc, "ignore").lower():
                return True
        except (UnicodeError, LookupError):
            continue
    return False


def _startup_hint(old: Path) -> list[str]:
    """
    开机自启里有没有还指着旧目录的快捷方式。

    上次换目录（v0.3.1 → v0.4.0）踩过这个：代码和记忆都搬好了，
    开机自启却还指着旧路径，重启一次 QQ 就再也上不来。
    """
    import os
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    if not startup.is_dir():
        return []
    return [f"  {p.name}" for p in sorted(startup.glob("*.lnk"))
            if _lnk_points_to(p, str(old))]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="把旧装里的私人内容搬到新装（人格、记忆、密钥、信道、配置）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="旧目录的路径，比如 D:\\AIPet-old")
    ap.add_argument("--into", default=None, help="新目录，默认是这份脚本所在的安装")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不搬")
    ap.add_argument("--yes", action="store_true", help="不问，直接搬")
    ap.add_argument("--force", action="store_true", help="目标已经有私人内容时也照搬")
    ap.add_argument("--no-backups", action="store_true", help="不搬 backups/ 里的历史备份包")
    args = ap.parse_args()

    if args.source == "selftest":
        sys.exit(selftest())

    if not args.source:
        ap.print_help()
        print("""
  没给旧目录。怎么用：

    双击 迁移私人内容.bat    —— 把【旧版目录】拖到那个文件上就行
    或者命令行：python tools\\migrate.py D:\\旧版\\AIPet

  先只看清单不搬，加 --dry-run。

  ★ 如果你是直接把新版 zip 解压覆盖旧目录 ——
    那不需要这个脚本，什么都不会丢。仓库的 zip 根本不含
    persona/ memory/ data/（都在 .gitignore 里），解压也不会
    删掉压缩包里没有的文件。
    需要它的是另一种换法：解压到新目录，然后把旧的整个丢掉。""")
        sys.exit(2)

    source = Path(args.source).expanduser().resolve()
    target = Path(args.into).expanduser().resolve() if args.into \
        else Path(__file__).resolve().parent.parent

    why = _looks_like_install(source)
    if why:
        print(f"旧目录不对劲：{why}")
        sys.exit(2)
    why = _looks_like_install(target)
    if why:
        print(f"新目录不对劲：{why}")
        sys.exit(2)
    if source == target:
        print("旧目录和新目录是同一个。")
        sys.exit(2)
    if target in source.parents or source in target.parents:
        print(f"两个目录套在一起了（{source} / {target}），会搬出乱子。")
        sys.exit(2)

    rows, missing = plan(source, target, with_backups=not args.no_backups)
    if not rows:
        print("旧目录里一件私人内容都没找到 —— 是不是指错地方了？")
        sys.exit(2)

    day = datetime.now().strftime("%Y-%m-%d")
    print(f"\n  {day}  迁移私人内容")
    print(f"  从  {source}")
    print(f"  到  {target}\n")

    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    order = [k for _, k, _ in ITEMS if k in by_kind]
    for kind in dict.fromkeys(order):
        group = by_kind[kind]
        size = sum(r["size"] for r in group) / 1024
        over = sum(1 for r in group if r["exists"])
        tail = f"，其中 {over} 个会覆盖掉目标里已有的" if over else ""
        print(f"  {kind:<6} {len(group):>3} 个  {size:>8.1f} KB   {group[0]['note']}{tail}")

    total = sum(r["size"] for r in rows) / 1024
    print(f"\n  合计 {len(rows)} 个文件，{total:.1f} KB")

    if missing:
        print(f"\n  旧目录里没有（跳过）：{'、'.join(missing)}")

    print("\n  不搬：")
    for path, why_not in SKIP:
        print(f"    {path:<38} {why_not}")

    links = _startup_hint(source)
    if links:
        print(f"\n  ★ 开机自启里还有快捷方式，它们可能指着旧目录 {source}：")
        for line in links:
            print(line)
        print("    搬完记得在快捷方式属性里把目标改成新目录，或者删掉重装一份。")

    if args.dry_run:
        print("\n  干跑，什么都没动。\n")
        sys.exit(0)

    dirty = footprint(target)
    if dirty and not args.force:
        print(f"\n  新目录里已经有私人内容了：{'、'.join(dirty)}")
        print("  这么搬会把它们盖掉。确认要这么做就再加 --force。")
        sys.exit(3)

    if not args.yes:
        try:
            ans = input("\n  搬吗？(y/N) ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "是"):
            print("  算了，什么都没动。\n")
            sys.exit(0)

    copied, over, backup_dir = apply(rows, target)
    print(f"\n  搬了 {copied} 个，覆盖 {over} 个。")
    if backup_dir:
        print(f"  覆盖前的那份存在：{backup_dir}")
    print("\n  接下来：")
    print("    1. 在新目录跑一次 准备环境.bat（运行环境不搬）")
    print("    2. 确认 data\\secrets.json 在，再启动桌宠")
    print("    3. 旧目录先别删，跑顺了再删\n")


if __name__ == "__main__":
    main()
