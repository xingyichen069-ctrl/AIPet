#!/usr/bin/env python3
"""
update.py —— 检查并安装 GitHub 上的新版本

═══════════════════════════════════════════════════════════════
  它做什么
═══════════════════════════════════════════════════════════════

读本机的 VERSION，问一次 GitHub 的 tag 列表，比一比，报结果。
也可以把 GitHub 的源码 zip 下载到临时目录，备份将被覆盖的代码文件后
安装到当前目录。个人数据、密钥、记忆和运行环境不会从更新包覆盖。

更新器不需要 git，也不会重新 clone 仓库。安装中途出错会用更新前的备份
恢复已经写过的文件；备份保存在 `backups/update-<时间>/`，方便回滚。

═══════════════════════════════════════════════════════════════
  版本号从哪来、怎么比
═══════════════════════════════════════════════════════════════

本机版本读根目录的 `VERSION`（约定的位置，见 docs/版本管理.md）。
不去 CHANGELOG 或 README 里抠 —— 那两处是给人看的，改起来容易漏，
事实上已经漏过一次。

远端版本取 GitHub tag 列表里最大的那个。比大小按 semver：
**预发布版比同号的正式版小**（0.4.0-beta.6 < 0.4.0），
beta.10 > beta.9（按数字比，不按字符串）。

═══════════════════════════════════════════════════════════════
  配哪个仓库
═══════════════════════════════════════════════════════════════

默认查项目自己的仓库。fork 出去想查自己的，在 data/config.json 里加：

    "update": { "repo": "你的名字/AIPet" }

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/update.py                  # 查一次
    python src/update.py selftest         # 自检
    python src/update.py update --yes     # 安装最新 tag
    python src/update.py update --branch all-round --yes
"""

from __future__ import annotations

import contextlib
import argparse
import compileall
import filecmp
import json
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

VERSION_FILE = M.ROOT / "VERSION"
DEFAULT_REPO = "xingyichen069-ctrl/AIPet"
TIMEOUT = 20
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

# 更新包只负责代码和文档。下面这些目录是本机运行资料或用户自定义内容，
# 即使它们偶然出现在 zip 里也不覆盖。更新前备份仍写进 backups/。
PROTECTED_DIRS = {
    ".git", ".venv", "runtime", "data", "memory", "persona", "backups",
    "work", "view", "knowledge", "__pycache__",
}
PROTECTED_FILES = {
    "assets/character.png",
    "themes/appearance.json",
    "themes/custom.qss",
}


def repo() -> str:
    r = (M.CFG.get("update") or {}).get("repo")
    return str(r).strip() if r else DEFAULT_REPO


# ═══════════════════════════════════════════════════════════════
#  版本号
# ═══════════════════════════════════════════════════════════════

def current() -> str:
    """本机版本。VERSION 读不到就回 0.0.0 —— 那会让任何 tag 都算「更新」，
    比假装自己是最新版安全。"""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


_NUM = re.compile(r"^\d+$")


def parse(v: str) -> tuple:
    """
    排得动大小的一把钥匙。

        0.4.0        → ((0,4,0), (1,))
        0.4.0-beta.6 → ((0,4,0), (0, ((0,0,'beta'), (1,6,''))))

    正式版的第二个元素是 (1,)，预发布版是 (0, ...) —— 靠这一位保证
    「0.4.0-beta.6 < 0.4.0」。数字段按数字比，所以 beta.10 > beta.9。
    """
    s = str(v).strip().lstrip("vV")
    core, _, pre = s.partition("-")
    nums = [int(x) if _NUM.match(x) else 0 for x in core.split(".")]
    nums = (nums + [0, 0, 0])[:3]
    if not pre:
        return tuple(nums), (1,)
    segs = []
    for seg in pre.split("."):
        segs.append((1, int(seg), "") if _NUM.match(seg) else (0, 0, seg.lower()))
    return tuple(nums), (0, tuple(segs))


def is_newer(remote: str, local: str) -> bool:
    return parse(remote) > parse(local)


# ═══════════════════════════════════════════════════════════════
#  问 GitHub
# ═══════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _opener():
    """
    带上检测到的代理。

    http/https 代理 urllib 自己认；**SOCKS 不认**，得靠 PySocks 把全局
    socket 换掉。换全局是有副作用的，所以用完立刻换回来 —— 这个函数
    会在桌宠进程里被调用，不能留下一地的全局状态。
    """
    import proxy as PX
    px = PX.detect()
    if px and px.startswith("socks"):
        try:
            import socks
        except ImportError:
            raise RuntimeError("SOCKS 代理需要 PySocks：pip install pysocks")
        u = urllib.parse.urlparse(px)
        kind = socks.SOCKS4 if u.scheme.startswith("socks4") else socks.SOCKS5
        old = socket.socket
        socks.set_default_proxy(kind, u.hostname, u.port, rdns=u.scheme.endswith("h"))
        socket.socket = socks.socksocket
        try:
            yield urllib.request.build_opener()
        finally:
            socket.socket = old
        return

    handlers = [urllib.request.ProxyHandler({"http": px, "https": px})] if px else []
    yield urllib.request.build_opener(*handlers)


def fetch_tags(timeout: float = TIMEOUT) -> list[str]:
    """仓库的 tag 名列表。未登录的 GitHub 每小时 60 次，手动查够用。"""
    url = f"https://api.github.com/repos/{repo()}/tags?per_page=100"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "AIPet-update-check",
    })
    with _opener() as op:
        with op.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    return [str(t.get("name") or "") for t in data if isinstance(t, dict) and t.get("name")]


def archive_url(ref: str, kind: str = "tag") -> str:
    """返回 GitHub 源码压缩包地址；ref 只能作为一个 URL 路径段。"""
    ref = str(ref or "").strip()
    if not ref or any(part in ref for part in ("..", "\\", "\x00")):
        raise ValueError("版本引用名不安全。")
    if kind not in {"tag", "branch"}:
        raise ValueError("版本引用类型只能是 tag 或 branch。")
    quoted = urllib.parse.quote(ref, safe="")
    return f"https://github.com/{repo()}/archive/refs/{'tags' if kind == 'tag' else 'heads'}/{quoted}.zip"


def download_archive(ref: str, kind: str = "tag", destination: Path | None = None,
                     timeout: float = TIMEOUT) -> tuple[Path, str]:
    """下载源码 zip，返回 (本地 zip 路径, 下载 URL)。"""
    url = archive_url(ref, kind)
    own_destination = destination is None
    if destination is None:
        handle = tempfile.NamedTemporaryFile(prefix="aipet-update-", suffix=".zip", delete=False)
        destination = Path(handle.name)
        handle.close()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/zip",
            "User-Agent": "AIPet-update",
        })
        with _opener() as op, op.open(req, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_ARCHIVE_BYTES:
                raise ValueError("更新包超过 200 MiB，已拒绝下载。")
            total = 0
            with destination.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("更新包超过 200 MiB，已拒绝下载。")
                    out.write(chunk)
        return destination, url
    except Exception:
        if own_destination:
            try:
                destination.unlink()
            except OSError:
                pass
        else:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def _safe_member_name(name: str) -> tuple[str, ...]:
    normalized = str(name or "").replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(f"更新包含有不安全路径：{name!r}")
    if ":" in parts[0] or parts[0].startswith("~"):
        raise ValueError(f"更新包含有不安全路径：{name!r}")
    return parts


def _archive_root(zf: zipfile.ZipFile) -> str:
    roots = set()
    for info in zf.infolist():
        parts = _safe_member_name(info.filename)
        if parts:
            roots.add(parts[0])
    if len(roots) != 1:
        raise ValueError("更新包结构不符合预期：需要一个统一的根目录。")
    return next(iter(roots))


def _is_protected(rel: Path) -> bool:
    posix = rel.as_posix()
    return (not rel.parts or rel.parts[0] in PROTECTED_DIRS
            or posix in PROTECTED_FILES)


def _unpack_public(archive: Path, destination: Path) -> tuple[Path, int]:
    """安全解压公开文件，返回解压根目录和被跳过的文件数。"""
    total_size = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            total_size += max(0, int(info.file_size))
            if total_size > MAX_ARCHIVE_BYTES:
                raise ValueError("更新包解压后超过 200 MiB，已拒绝安装。")
        root_name = _archive_root(zf)
        root = destination / root_name
        skipped = 0
        for info in zf.infolist():
            parts = _safe_member_name(info.filename)
            if parts[0] != root_name:
                raise ValueError("更新包根目录不一致。")
            rel_parts = parts[1:]
            if not rel_parts:
                continue
            rel = Path(*rel_parts)
            if _is_protected(rel):
                skipped += 1
                continue
            # GitHub 源码包通常没有符号链接；拒绝它们，避免更新包借解压写出根目录。
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"更新包不允许符号链接：{info.filename}")
            target = root / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return root, skipped


def install_archive(archive: Path, target: Path = M.ROOT) -> dict:
    """把更新包覆盖到 target，保留私人目录并在失败时回滚。"""
    archive = Path(archive)
    target = Path(target).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"找不到更新包：{archive}")
    with tempfile.TemporaryDirectory(prefix="aipet-unpack-") as tmp:
        root, skipped = _unpack_public(archive, Path(tmp))
        version_path = root / "VERSION"
        version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
        files = [p for p in root.rglob("*") if p.is_file()]
        if not files:
            raise ValueError("更新包里没有可安装的公开文件。")
        source_dir = root / "src"
        if source_dir.is_dir() and not compileall.compile_dir(source_dir, quiet=1):
            raise ValueError("更新包里的 Python 代码无法通过语法检查。")

        backup_dir = target / "backups" / f"update-{time.strftime('%Y%m%d-%H%M%S')}"
        backups: dict[Path, Path | None] = {}
        changed: list[str] = []
        for src in files:
            rel = src.relative_to(root)
            dst = target / rel
            if dst.exists() and filecmp.cmp(src, dst, shallow=False):
                continue
            if dst.exists():
                backup = backup_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup)
                backups[dst] = backup
            else:
                backups[dst] = None
            changed.append(rel.as_posix())

        applied: list[Path] = []
        try:
            for src in files:
                rel = src.relative_to(root)
                dst = target / rel
                if rel.as_posix() not in changed:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                applied.append(dst)
                shutil.copy2(src, dst)
        except Exception:
            for dst in reversed(applied):
                backup = backups.get(dst)
                try:
                    if backup and backup.exists():
                        shutil.copy2(backup, dst)
                    elif dst.exists():
                        dst.unlink()
                except OSError:
                    pass
            raise

        return {
            "ok": True,
            "version": version,
            "changed": changed,
            "skipped": skipped,
            "backup": str(backup_dir) if backups else "",
        }


def update(ref: str = "", kind: str = "tag", target: Path = M.ROOT) -> dict:
    """下载并安装一个 tag/branch；未给 ref 时只安装最新 tag。"""
    requested = str(ref or "").strip()
    if requested:
        selected, selected_kind = requested, kind
        check_result = None
    else:
        check_result = check()
        if not check_result.get("ok"):
            return {**check_result, "updated": False}
        if not check_result.get("newer"):
            return {**check_result, "updated": False}
        selected, selected_kind = check_result["latest"], "tag"

    archive = None
    before = current()
    try:
        archive, url = download_archive(selected, selected_kind)
        installed = install_archive(archive, target)
        return {
            "ok": True,
            "updated": True,
            "ref": selected,
            "kind": selected_kind,
            "url": url,
            **installed,
            "current_before": before,
        }
    except Exception as e:
        return {
            "ok": False,
            "updated": False,
            "ref": selected,
            "kind": selected_kind,
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        }
    finally:
        if archive:
            try:
                archive.unlink()
            except OSError:
                pass


def check(timeout: float = TIMEOUT) -> dict:
    """
    查一次。**不抛异常** —— 调用方是界面和 QQ，拿到异常没处放。

    ok=False 时 error 里是给人看的原因，不是堆栈。
    """
    cur = current()
    out = {
        "ok": False, "current": cur, "latest": "", "newer": False,
        "url": f"https://github.com/{repo()}/tags", "error": "",
    }
    try:
        tags = fetch_tags(timeout)
    except urllib.error.HTTPError as e:
        out["error"] = f"GitHub 回了 HTTP {e.code}" + (
            "，查得太频了，等一会儿再试。" if e.code == 403 else "。")
        return out
    except Exception as e:
        out["error"] = f"连不上 GitHub：{type(e).__name__}: {str(e)[:80]}"
        return out
    if not tags:
        out["error"] = "这个仓库一个 tag 都没有。"
        return out

    out["ok"] = True
    out["latest"] = max(tags, key=parse)
    out["latest_clean"] = out["latest"].lstrip("vV")
    out["newer"] = is_newer(out["latest"], cur)
    return out


def describe(r: dict) -> str:
    """一句话结论。QQ 和命令行共用 —— 两边都不许出现 URL（QQ 会拒收整条）。"""
    if not r["ok"]:
        return f"查不了更新：{r['error']}"
    if not r["newer"]:
        return f"已经是最新的，{r['current']}。"
    return (f"有新版本 {r['latest_clean']}，本机还停在 {r['current']}。\n"
            f"在电脑上右键桌宠 → 高级 → 检查更新，那里能打开下载页。")


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    fails = 0

    def check_(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'[OK]' if cond else '[!!]'} {label}" + (f"   {extra}" if extra else ""))
        if not cond:
            fails += 1

    print("检查更新自检\n")

    cases = [
        # (远端, 本机, 期望「有新版本」)
        ("v0.4.0-beta.7", "0.4.0-beta.6", True),
        ("0.4.0-beta.6", "0.4.0-beta.6", False),
        ("v0.4.0-beta.5", "0.4.0-beta.6", False),
        ("v0.4.0-beta.10", "0.4.0-beta.9", True),    # 数字比，不是字符串比
        ("v0.4.0", "0.4.0-beta.6", True),            # 正式版 > 同号预发布
        ("v0.4.0-beta.1", "0.4.0", False),
        ("v0.4.1-beta.1", "0.4.0", True),
        ("v0.5.0", "0.4.9", True),
        ("v0.3.9", "0.4.0", False),
        ("v1.0.0", "0.99.99", True),
    ]
    for remote, local, want in cases:
        got = is_newer(remote, local)
        check_(f"{remote} vs {local} → {'新' if want else '不新'}", got == want,
               "" if got == want else f"实际算成 {'新' if got else '不新'}")

    check_("VERSION 读得到", bool(current()) and current() != "0.0.0", current())
    check_("仓库名有默认值", "/" in repo(), repo())

    # 真正解压一份假更新包：公开代码会更新，个人目录和受保护的主题不会动。
    with tempfile.TemporaryDirectory(prefix="aipet_update_") as tmp:
        base = Path(tmp)
        target = base / "AIPet"
        (target / "data").mkdir(parents=True)
        (target / "src").mkdir(parents=True)
        (target / "themes").mkdir(parents=True)
        (target / "src" / "new.py").write_text("old", encoding="utf-8")
        (target / "data" / "secrets.json").write_text("keep", encoding="utf-8")
        (target / "themes" / "custom.qss").write_text("custom", encoding="utf-8")
        archive = base / "update.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("AIPet-main/VERSION", "9.9.9")
            zf.writestr("AIPet-main/src/new.py", "print('new')")
            zf.writestr("AIPet-main/data/secrets.json", "replace-me")
            zf.writestr("AIPet-main/themes/custom.qss", "replace-me")
        installed = install_archive(archive, target)
        check_("公开文件已覆盖", (target / "src" / "new.py").read_text(encoding="utf-8") == "print('new')")
        check_("私人密钥未被覆盖", (target / "data" / "secrets.json").read_text(encoding="utf-8") == "keep")
        check_("自定义主题未被覆盖", (target / "themes" / "custom.qss").read_text(encoding="utf-8") == "custom")
        check_("更新前备份目录已返回", bool(installed["backup"]) and Path(installed["backup"]).is_dir())

        bad = base / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("AIPet-main/../outside.txt", "bad")
        try:
            install_archive(bad, target)
        except ValueError:
            check_("拒绝压缩包穿越路径", True)
        else:
            check_("拒绝压缩包穿越路径", False)

    # 真查一次
    r = check()
    if not r["ok"]:
        print(f"\n  真查那步跳过了：{r['error']}")
    else:
        print(f"\n  本机 {r['current']}，远端最新 {r['latest_clean']}"
              f"（{'有新版本' if r['newer'] else '已是最新'}）")

        # ★ 别断言「远端不比本机旧」—— 本机 VERSION 先改、tag 后推是常态，
        #   那时候本机就是比远端新，断言会红，但什么都没坏。
        #   要盯的是「远端那串版本号解得动」：解不动说明命名换了规矩，
        #   而 parse() 对认不出的段是**静默当 0** 的，比大小会算错还不报错。
        tags = fetch_tags()
        bad = [t for t in tags if not isinstance(parse(t)[0], tuple) or parse(t) == ((0, 0, 0), (0, ()))]
        check_("远端 tag 都解得动", not bad, f"{len(tags)} 个" + (f"，认不出：{bad[:3]}" if bad else ""))

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        sys.exit(selftest())
    if args and args[0] == "update":
        parser = argparse.ArgumentParser(description="下载并安装 AIPet 更新")
        parser.add_argument("update")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--tag", help="安装指定 tag，例如 v0.5.0")
        group.add_argument("--branch", help="安装指定分支，例如 all-round")
        parser.add_argument("--yes", action="store_true", help="确认覆盖代码文件")
        ns = parser.parse_args(args)
        if not ns.yes:
            print("这会覆盖公开代码文件，并在 backups/update-<时间>/ 留一份备份。")
            print("确认后重跑：python src/update.py update --yes")
            sys.exit(2)
        ref = ns.tag or ns.branch or ""
        kind = "branch" if ns.branch else "tag"
        result = update(ref, kind)
        if not result.get("ok"):
            print(f"更新失败：{result.get('error') or '未知错误'}")
            sys.exit(1)
        if not result.get("updated"):
            print(describe(result))
            sys.exit(0)
        print(f"已安装 {result.get('ref')}，改动 {len(result.get('changed') or [])} 个公开文件。")
        if result.get("backup"):
            print(f"更新前备份：{result['backup']}")
        sys.exit(0)
    r = check()
    print(f"本机：{r['current']}")
    if r["ok"]:
        print(f"远端：{r['latest_clean']}")
    print(describe(r))
    if r["ok"] and r["newer"]:
        print(f"\n下载页：{r['url']}")
        print("命令行安装：python src/update.py update --yes")
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
