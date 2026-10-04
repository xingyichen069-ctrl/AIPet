#!/usr/bin/env python3
"""
update.py —— 看看 GitHub 上有没有新版本

═══════════════════════════════════════════════════════════════
  它做什么
═══════════════════════════════════════════════════════════════

读本机的 VERSION，核对 GitHub 指定分支的提交及 VERSION，比一比，报结果。

**它不下载、不覆盖、不动任何文件。** 只告诉你有还是没有，
更不更新是你自己的事。

为什么不自动更新：这套 AIPet 在本机是**拷贝而不是 git 检出**，
所以「自动更新」只能是下载源码 zip 覆盖一遍。覆盖到一半失败、
或者新版本本身有 bug，桌宠当场就坏 —— 拿一个能砸掉整个安装的
风险换省一次点击，不划算。给你版本号和地址，你自己决定。

═══════════════════════════════════════════════════════════════
  版本号从哪来、怎么比
═══════════════════════════════════════════════════════════════

本机版本读根目录的 `VERSION`（约定的位置，见 docs/版本管理.md）。
不去 CHANGELOG 或 README 里抠 —— 那两处是给人看的，改起来容易漏，
事实上已经漏过一次。

远端默认查 main：先固定分支提交，再读那个提交的 VERSION。
下载入口指向同一提交；其他分支的标签不参加比较。比大小按 semver：
**预发布版比同号的正式版小**（0.4.0-beta.6 < 0.4.0），
beta.10 > beta.9（按数字比，不按字符串）。

═══════════════════════════════════════════════════════════════
  配哪个仓库
═══════════════════════════════════════════════════════════════

默认查项目自己的仓库。fork 出去想查自己的，在 data/config.json 里加：

    "update": { "repo": "你的名字/AIPet", "branch": "main" }

同号只能说明 VERSION 相同，不能证明本机文件与远端提交相同。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/update.py            # 查一次
    python src/update.py selftest   # 自检（版本比较那段不联网）
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
import time
import urllib.error
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import proxy as PX  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

VERSION_FILE = M.ROOT / "VERSION"
DEFAULT_REPO = "xingyichen069-ctrl/AIPet"
DEFAULT_BRANCH = "main"
TIMEOUT = 20


def repo() -> str:
    return target()[0]


def target() -> tuple[str, str]:
    settings = M.CFG.get("update", {})
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("config.json 的 update 必须是对象。")
    repository = settings.get("repo", DEFAULT_REPO)
    branch = settings.get("branch", DEFAULT_BRANCH)
    if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("update.repo 应为 GitHub 的 owner/repository。")
    if (not isinstance(branch, str) or not branch or len(branch) > 255
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
            or branch.startswith(("/", ".")) or branch.endswith(("/", ".", ".lock"))
            or ".." in branch or "//" in branch or "@{" in branch):
        raise ValueError("update.branch 必须是有效分支名，默认使用 main。")
    return repository, branch


# ═══════════════════════════════════════════════════════════════
#  版本号
# ═══════════════════════════════════════════════════════════════

def current() -> str:
    """Read only VERSION; an unreadable file is unknown, not version 0.0.0."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


_NUM = re.compile(r"^\d+$")
_VERSION = re.compile(
    r"[vV]?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def parse(v: str) -> tuple:
    """Strict SemVer ordering; build metadata does not affect precedence."""
    if not isinstance(v, str) or len(v) > 128:
        raise ValueError("VERSION 必须是有效的语义版本号。")
    match = _VERSION.fullmatch(v.strip())
    if match is None:
        raise ValueError("VERSION 应类似 0.5.0-beta.2，不能使用分支名或说明文字。")
    nums = tuple(int(x) for x in match.group(1, 2, 3))
    pre = match.group(4)
    if not pre:
        return nums, (1,)
    segs = []
    for seg in pre.split("."):
        if _NUM.fullmatch(seg):
            if len(seg) > 1 and seg.startswith("0"):
                raise ValueError("VERSION 的预发布数字段不能有前导零。")
            segs.append((0, int(seg), ""))
        else:
            segs.append((1, 0, seg))
    return nums, (0, tuple(segs))


def is_newer(remote: str, local: str) -> bool:
    return parse(remote) > parse(local)


# ═══════════════════════════════════════════════════════════════
#  问 GitHub
# ═══════════════════════════════════════════════════════════════

def fetch_tags(timeout: float = TIMEOUT) -> list[str]:
    """Legacy diagnostic helper; tags are never used to recommend updates."""
    url = f"https://api.github.com/repos/{repo()}/tags?per_page=100"
    response = PX.request(url, timeout=timeout, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "AIPet-update-check",
    })
    data = json.loads(response.text)
    return [str(t.get("name") or "") for t in data if isinstance(t, dict) and t.get("name")]


def fetch_source(repository: str, branch: str, timeout: float = TIMEOUT) -> dict:
    """Pin a branch commit before reading its VERSION to avoid a moving ref."""
    deadline = time.monotonic() + float(timeout)
    api = f"https://api.github.com/repos/{repository}"

    def get(path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("核对分支与版本时超时。")
        response = PX.request(api + path, timeout=remaining, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AIPet-update-check",
        })
        data = json.loads(response.text)
        if not isinstance(data, dict):
            raise ValueError("GitHub 返回的版本来源格式不正确。")
        return data

    ref = get("/git/ref/heads/" + quote(branch, safe="/"))
    obj = ref.get("object")
    if (ref.get("ref") != f"refs/heads/{branch}" or not isinstance(obj, dict)
            or obj.get("type") != "commit"
            or not re.fullmatch(r"[0-9a-fA-F]{40}", str(obj.get("sha", "")))):
        raise ValueError("无法确认指定分支的提交；未改查其他分支或标签。")
    commit = obj["sha"].lower()
    file = get(f"/contents/VERSION?ref={commit}")
    if (file.get("type") != "file" or file.get("path") != "VERSION"
            or file.get("encoding") != "base64" or not isinstance(file.get("content"), str)
            or len(file["content"]) > 1024):
        raise ValueError("该分支没有可读取的普通 VERSION 文件。")
    try:
        raw = base64.b64decode("".join(file["content"].split()), validate=True)
        if len(raw) > 256:
            raise ValueError("VERSION 文件过长。")
        version = raw.decode("utf-8-sig").strip()
        parse(version)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("远端 VERSION 无效，未推荐下载。") from exc
    return {"repo": repository, "branch": branch, "commit": commit,
            "latest": version, "latest_clean": version.removeprefix("v").removeprefix("V"),
            "url": f"https://github.com/{repository}/tree/{commit}",
            "download_url": f"https://github.com/{repository}/archive/{commit}.zip"}


def check(timeout: float = TIMEOUT) -> dict:
    """
    查一次。**不抛异常** —— 调用方是界面和 QQ，拿到异常没处放。

    ok=False 时 error 里是给人看的原因，不是堆栈。
    """
    cur = current()
    out = {
        "ok": False, "current": cur, "latest": "", "latest_clean": "", "newer": False,
        "repo": "", "branch": "", "commit": "", "comparison": "unknown",
        "url": "", "download_url": "", "error": "",
    }
    try:
        repository, branch = target()
        out.update(repo=repository, branch=branch)
        source = fetch_source(repository, branch, timeout)
    except urllib.error.HTTPError as e:
        out["error"] = f"GitHub 回了 HTTP {e.code}" + (
            "，查得太频了，等一会儿再试。" if e.code == 403 else "。")
        if e.code == 404:
            out["error"] += "请核对仓库、分支和 VERSION 文件是否存在；未回退到标签列表。"
        return out
    except ValueError as e:
        out["error"] = f"版本来源无法核对：{PX.error_text(e)}"
        return out
    except Exception as e:
        out["error"] = f"连不上 GitHub：{PX.error_text(e)}"
        return out
    out.update(source)
    out["ok"] = True
    try:
        local, remote = parse(cur), parse(out["latest"])
    except ValueError:
        out["comparison"] = "local_unknown"
    else:
        out["newer"] = remote > local
        out["comparison"] = ("remote_newer" if remote > local else
                             "local_ahead" if remote < local else "same_version")
    return out


def describe(r: dict) -> str:
    """一句话结论。QQ 和命令行共用 —— 两边都不许出现 URL（QQ 会拒收整条）。"""
    if not r["ok"]:
        return f"查不了更新：{r['error']}"
    source = f"来源：{r['repo']} / {r['branch']}（{r['commit'][:7]}）。"
    if r["comparison"] == "local_unknown":
        message = f"远端标注 {r['latest_clean']}；本机 VERSION 缺失或无效，无法比较。"
    elif r["comparison"] == "local_ahead":
        message = (f"本机 {r['current']} 的版本号高于该分支的 {r['latest_clean']}。"
                   "这不能确认两份代码的来源或兼容性。")
    elif r["comparison"] == "same_version":
        message = (f"本机与该分支的版本号相同：{r['current']}。"
                   "同号可能有不同提交，本次未比较本机文件。")
    else:
        message = f"该分支有较新版本 {r['latest_clean']}，本机是 {r['current']}。"
    return f"{message}\n{source}"


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

    # 真查一次
    r = check()
    if not r["ok"]:
        print(f"\n  真查那步跳过了：{r['error']}")
    else:
        print(f"\n  {describe(r)}")

        check_("远端来源固定到提交", bool(re.fullmatch(r"[0-9a-f]{40}", r["commit"])),
               f"{r['branch']} / {r['commit'][:7]}")

    print()
    print("全部通过" if not fails else f"{fails} 项未通过")
    return fails


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        sys.exit(selftest())
    r = check()
    print(f"本机：{r['current']}")
    if r["ok"]:
        print(f"远端：{r['latest_clean']}")
    print(describe(r))
    if r["ok"] and r["newer"]:
        print(f"\n下载页：{r['url']}")
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
