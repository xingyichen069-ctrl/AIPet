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
import os
import re
import subprocess
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

# 国内站点走直连，减少代理绕行和 DNS 污染；列表可在 tools.domestic_domains
# 中覆盖。命中规则只影响路由，不会修改系统 DNS。
DEFAULT_DOMESTIC_DOMAINS = {
    "baidu.com", "bdimg.com", "bilibili.com", " bilivideo.com".strip(),
    "qq.com", "weixin.qq.com", "tencent.com", "taobao.com", "tmall.com",
    "jd.com", "zhihu.com", "douyin.com", "kuaishou.com", "163.com",
    "126.com", "sina.com.cn", "weibo.com", "csdn.net", "cnblogs.com",
    "gov.cn", "edu.cn", "miit.gov.cn", "aliyun.com", "alipay.com",
}

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


def from_environment() -> str | None:
    """读取常见代理环境变量，兼容大小写和 NO_PROXY。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value and not value.lower().startswith("http://default.mitmproxy"):
            return value
    return None


def from_macos_networksetup() -> str | None:
    """读取 macOS 当前网络服务的 Web/Secure Web Proxy。"""
    if sys.platform != "darwin":
        return None
    try:
        services = subprocess.run(["networksetup", "-listallnetworkservices"],
                                  capture_output=True, text=True, timeout=4)
        names = [x.strip().lstrip("*") for x in services.stdout.splitlines()[1:]
                 if x.strip() and not x.strip().startswith("An asterisk")]
        for service in names:
            for kind in ("webproxy", "securewebproxy"):
                r = subprocess.run(["networksetup", f"-get{kind}", service],
                                   capture_output=True, text=True, timeout=4)
                vals = {}
                for line in r.stdout.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        vals[k.strip().lower()] = v.strip()
                if vals.get("enabled", "no").lower() == "yes" and vals.get("server"):
                    return f"http://{vals['server']}:{vals.get('port', '8080')}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


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
    if url.lower().startswith("socks"):
        try:
            import socks
        except ImportError:
            return False, "SOCKS 代理需要 PySocks：pip install pysocks"

    restore_socket = None
    if url.lower().startswith("socks"):
        import socks
        parsed = urllib.parse.urlsplit(url)
        ptype = socks.SOCKS5 if parsed.scheme.lower() in ("socks5", "socks5h") else socks.SOCKS4
        socks.set_default_proxy(ptype, parsed.hostname or "127.0.0.1",
                                parsed.port or 1080,
                                rdns=parsed.scheme.lower() == "socks5h",
                                username=parsed.username, password=parsed.password)
        restore_socket = socket.socket
        socket.socket = socks.socksocket
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = opener_for_url(CHECK_URL, url)
    req = urllib.request.Request(CHECK_URL, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status in (200, 204), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 能收到 HTTP 错误也说明链路是通的
        return True, f"HTTP {e.code}（链路可达）"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        if restore_socket is not None:
            socket.socket = restore_socket


def _socks_open(url: str):
    import socks
    parsed = urllib.parse.urlsplit(url)
    proxy_type = socks.SOCKS5 if parsed.scheme.lower() in ("socks5", "socks5h") else socks.SOCKS4
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1080
    # socks5h 让代理端解析域名，降低本地 DNS 污染的影响。
    rdns = parsed.scheme.lower() == "socks5h"
    return socks.socksocket, (proxy_type, host, port, rdns,
                              parsed.username, parsed.password)


def opener_for_url(url: str, proxy: str | None = None):
    """按域名路由请求；SOCKS 代理用 PySocks 临时接管 socket。"""
    import urllib.parse
    chosen = proxy if proxy is not None else proxy_for_url(url)
    if not chosen:
        return urllib.request.build_opener()
    if chosen.lower().startswith("socks"):
        try:
            import socks
        except ImportError:
            return urllib.request.build_opener()
        # urllib 没有 SOCKS handler。通过 socksocket 的默认代理配置，
        # 在真正打开请求时由 verify/调用方负责恢复全局 socket。
        parsed = urllib.parse.urlsplit(chosen)
        ptype = socks.SOCKS5 if parsed.scheme.lower() in ("socks5", "socks5h") else socks.SOCKS4
        socks.set_default_proxy(ptype, parsed.hostname or "127.0.0.1",
                                parsed.port or 1080,
                                rdns=parsed.scheme.lower() == "socks5h",
                                username=parsed.username, password=parsed.password)
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": chosen, "https": chosen}))


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

    candidates = [("环境变量", from_environment()),
                  ("macOS 系统代理", from_macos_networksetup()),
                  ("Windows 系统代理", from_windows_registry()),
                  ("端口探测", from_port_scan())]
    seen = set()
    for label, cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        ok, msg = verify(cand)
        say(f"[proxy] {label} {cand} → {'可用' if ok else '不通：' + msg}")
        if ok:
            _cache["value"] = cand
            return cand

    say("[proxy] 没找到可用代理，将直连")
    _cache["value"] = None
    return None


def refresh() -> str | None:
    return detect(verbose=True, force=True)


def status() -> dict:
    env = from_environment()
    return {
        "配置值": M.CFG.get("tools", {}).get("proxy") or "(空)",
        "macOS系统代理": from_macos_networksetup() or "(未开启)",
        "Windows系统代理": from_windows_registry() or "(未开启)",
        "端口探测": from_port_scan() or "(没探到)",
        "环境变量": env or "(无)",
        "最终使用": detect() or "(直连)",
    }


def _domestic_domains() -> set[str]:
    configured = M.CFG.get("tools", {}).get("domestic_domains")
    if isinstance(configured, list) and configured:
        return {str(x).lower().lstrip(".") for x in configured if str(x).strip()}
    return DEFAULT_DOMESTIC_DOMAINS


def is_domestic_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _domestic_domains())


def proxy_for_url(url: str) -> str | None:
    """国内域名默认直连，其他域名使用已验证代理。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return None
    try:
        import ipaddress
        if ipaddress.ip_address(host).is_private:
            return None
    except ValueError:
        pass
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if any(host == x.strip().lstrip(".") or host.endswith("." + x.strip().lstrip("."))
           for x in no_proxy.split(",") if x.strip()):
        return None
    cfg = M.CFG.get("tools", {})
    if is_domestic_host(host) and not bool(cfg.get("proxy_domestic", False)):
        return None
    return detect()


def dns_diagnose(host: str) -> dict:
    """只诊断当前解析，不改系统设置。"""
    host = (host or "").strip()
    if not host:
        return {"host": "", "error": "缺少域名"}
    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = sorted({x[4][0] for x in rows})
    except socket.gaierror as e:
        return {"host": host, "addresses": [], "error": str(e), "pollution_suspected": True}
    bad = {"4.36.66.178", "8.7.198.45", "37.61.54.158", "46.82.174.68",
           "59.24.3.173", "203.98.7.65", "243.185.187.39"}
    return {"host": host, "addresses": addresses,
            "domestic_route": is_domestic_host(host),
            "pollution_suspected": bool(set(addresses) & bad)}


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

    if args and args[0] == "dns":
        host = args[1] if len(args) > 1 else ""
        print(json.dumps(dns_diagnose(host), ensure_ascii=False, indent=2))
        return

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
