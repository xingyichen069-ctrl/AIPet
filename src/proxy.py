#!/usr/bin/env python3
"""
proxy.py —— 代理自动检测

解决的问题：你开着 Clash / v2rayN 之类的工具，但 AIPet 的搜索还是连不上
境外站点——因为 Python 进程并不知道代理存在。

这个模块让它自己找到代理，不用你手动填端口。

═══════════════════════════════════════════════════════════════
  检测顺序（找到第一个能用的就停）
═══════════════════════════════════════════════════════════════

  1. 配置里显式写的     data/config.json → tools.proxy
                        填了就用，填 "off" 则完全不用代理

  2. Windows 系统代理   从注册表读 Internet Settings
                        ↑ 绝大多数代理软件会写这里，最可靠

  3. 探测常见端口       逐个 TCP 探测本机常见代理端口
                        Clash / v2rayN / sing-box / Surge / SS …

找到候选之后**会实际验证一次**：通过它访问一个境外端点，
通了才算数。光探测到端口开着不算——那可能是别的程序。

验证用的端点：https://www.google.com/generate_204
成功返回 204 说明这条代理真的能出去。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/proxy.py            # 检测并验证
    python src/proxy.py test       # 只看当前用的代理通不通
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 本机常见代理端口。顺序按国内流行度排。
COMMON_PORTS = [
    (7890, "http"),    # Clash / mihomo / Clash for Windows
    (7897, "http"),    # Clash Verge Rev
    (10809, "http"),   # v2rayN
    (1080, "socks5"),  # Shadowsocks / 通用
    (7891, "socks5"),  # Clash SOCKS
    (10808, "socks5"), # v2rayN SOCKS
    (2080, "http"),    # sing-box / Nekoray
    (6152, "http"),    # Surge
    (8889, "http"),    # 杂项
    (8080, "http"),    # 通用
]

CHECK_URL = "https://www.google.com/generate_204"
_cache: dict = {"value": "\x00"}      # \x00 = 还没检测过


# ═══════════════════════════════════════════════════════════════
#  候选来源
# ═══════════════════════════════════════════════════════════════

def from_config() -> str | None:
    raw = (M.CFG.get("tools", {}).get("proxy") or "").strip()
    if not raw:
        return None
    if raw.lower() in ("off", "none", "false", "0"):
        return "off"                       # 显式禁用
    if raw.lower() == "auto":
        return None                        # 走自动检测
    return raw


def from_windows_registry() -> str | None:
    """读系统代理设置。代理软件基本都会写这里。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            try:
                enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            except FileNotFoundError:
                return None
            if not enabled:
                return None
            try:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
            except FileNotFoundError:
                return None
    except (ImportError, OSError):
        return None

    if not server:
        return None

    # ProxyServer 可能是 "127.0.0.1:7890"，
    # 也可能是 "http=127.0.0.1:7890;https=127.0.0.1:7890"
    if "=" in server:
        parts = dict(
            p.split("=", 1) for p in server.split(";") if "=" in p
        )
        server = parts.get("https") or parts.get("http") or ""
    server = server.strip()
    if not server:
        return None
    return f"http://{server}" if "://" not in server else server


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex(("127.0.0.1", port)) == 0


def from_port_scan() -> str | None:
    for port, scheme in COMMON_PORTS:
        if _port_open(port):
            return f"{scheme}://127.0.0.1:{port}"
    return None


# ═══════════════════════════════════════════════════════════════
#  验证
# ═══════════════════════════════════════════════════════════════

def verify(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """
    真的走一次代理访问境外端点。返回 (通不通, 说明)。

    注意：SOCKS 代理 urllib 不原生支持（需要 PySocks）。
    探测到 socks5 且没有 PySocks 时，会明确说出来，
    而不是含糊地报"不通"让人以为是网络问题。
    """
    if url.startswith("socks"):
        try:
            import socks  # noqa: F401
        except ImportError:
            return False, "SOCKS 代理需要 PySocks：pip install pysocks"

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))
    req = urllib.request.Request(CHECK_URL, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status in (200, 204), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 能收到 HTTP 错误也说明链路是通的
        return True, f"HTTP {e.code}（链路可达）"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"


# ═══════════════════════════════════════════════════════════════
#  对外接口
# ═══════════════════════════════════════════════════════════════

def detect(verbose: bool = False, force: bool = False) -> str | None:
    """
    返回可用的代理 URL，没有则 None。
    结果会缓存，避免每次搜索都重新探测。force=True 强制重测。
    """
    if not force and _cache["value"] != "\x00":
        return _cache["value"]

    say = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    explicit = from_config()
    if explicit == "off":
        say("[proxy] 配置里显式关闭了代理")
        _cache["value"] = None
        return None
    if explicit:
        ok, msg = verify(explicit)
        say(f"[proxy] 配置指定 {explicit} → {'可用' if ok else '不通：' + msg}")
        _cache["value"] = explicit if ok else None
        return _cache["value"]

    cand = from_windows_registry()
    if cand:
        ok, msg = verify(cand)
        say(f"[proxy] 系统代理 {cand} → {'可用' if ok else '不通：' + msg}")
        if ok:
            _cache["value"] = cand
            return cand
    else:
        say("[proxy] 系统代理未开启")

    cand = from_port_scan()
    if cand:
        ok, msg = verify(cand)
        say(f"[proxy] 端口探测 {cand} → {'可用' if ok else '不通：' + msg}")
        if ok:
            _cache["value"] = cand
            return cand

    say("[proxy] 没找到可用代理，将直连")
    _cache["value"] = None
    return None


def refresh() -> str | None:
    return detect(verbose=True, force=True)


def status() -> dict:
    import os
    env = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
           or os.environ.get("ALL_PROXY"))
    return {
        "配置值": M.CFG.get("tools", {}).get("proxy") or "(空)",
        "系统代理": from_windows_registry() or "(未开启)",
        "端口探测": from_port_scan() or "(没探到)",
        "环境变量": env or "(无)",
        "最终使用": detect() or "(直连)",
    }


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def apply_to_git(proxy: str | None, unset: bool = False) -> int:
    """
    把检测到的代理写进 git 的全局配置。

    为什么需要：**git 不像浏览器那样读 Windows 系统代理。**
    你把 VPN 开成「系统代理」模式，浏览器能出墙，但 git clone/fetch
    照样超时——它只认自己的 http.proxy 配置，或者 TUN 模式。

    用 `git config --global` 写用户级配置，随时能撤销。
    """
    import subprocess

    def run(*a):
        return subprocess.run(["git", "config", "--global", *a],
                              capture_output=True, text=True, timeout=15)

    if unset or proxy is None:
        run("--unset", "http.proxy")
        run("--unset", "https.proxy")
        print("已清除 git 的代理配置（恢复直连）")
        return 0

    for k in ("http.proxy", "https.proxy"):
        r = run(k, proxy)
        if r.returncode != 0:
            print(f"设置 {k} 失败：{r.stderr.strip()}")
            return 1

    print(f"git 已走代理：{proxy}")
    print("验证：git ls-remote https://github.com/git/git HEAD")
    print("撤销：python src/proxy.py git --off")
    return 0


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "git":
        if "--off" in args:
            sys.exit(apply_to_git(None, unset=True))
        p = detect(verbose=True, force=True)
        if not p:
            print("没检测到可用代理。先把 VPN 打开，再跑一次。")
            print("（如果是 Clash 的「系统代理」模式，检测得到；")
            print("  TUN 模式也行，那种情况下其实不用设 git）")
            sys.exit(1)
        sys.exit(apply_to_git(p))

    if args and args[0] == "test":
        p = detect()
        if not p:
            print("当前：直连（没有可用代理）")
            sys.exit(1)
        ok, msg = verify(p)
        print(f"当前：{p} → {'通' if ok else '不通'}（{msg}）")
        sys.exit(0 if ok else 1)

    print("代理检测\n")
    print(json.dumps(status(), ensure_ascii=False, indent=2))

    p = detect(force=True, verbose=True)
    print()
    if p:
        ok, msg = verify(p)
        print(f"结论：用 {p}（{msg}）")
        print("\n搜索时会自动走这条代理，不需要改配置。")
    else:
        print("结论：没有可用代理，将直连。")
        print("如果代理确实开着，把它的地址填进 data/config.json 的 tools.proxy，")
        print("例如 \"http://127.0.0.1:7890\"。")


if __name__ == "__main__":
    main()
