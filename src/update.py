#!/usr/bin/env python3
"""
update.py —— 看看 GitHub 上有没有新版本

═══════════════════════════════════════════════════════════════
  它做什么
═══════════════════════════════════════════════════════════════

读本机的 VERSION，问一次 GitHub 的 tag 列表，比一比，报结果。

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

    python src/update.py            # 查一次
    python src/update.py selftest   # 自检（版本比较那段不联网）
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
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
