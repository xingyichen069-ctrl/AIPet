#!/usr/bin/env python3
"""搜索、网页读取及检查更新共用的代理选择与请求客户端。

配置 tools.proxy：auto/空值依次探测 Windows 系统代理和本机常见端口；
off 强制这些请求直连；手填地址始终使用该代理，失败不会偷偷切成直连。
自动检测只证明检测端点可达，不保证各搜索服务都可用。
所有路由只作用于独立客户端，不改环境变量、系统设置或全局 socket。

python src/proxy.py        # 查看选择并重新检测
python src/proxy.py test   # 重新检测，失败返回非零退出码
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import socket
import sys
import threading
import time
import urllib.error
from urllib.parse import urlsplit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

COMMON_PORTS = [
    (7890, "http"), (7897, "http"), (10809, "http"), (1080, "socks5h"),
    (7891, "socks5h"), (10808, "socks5h"), (2080, "http"), (6152, "http"),
    (8889, "http"), (8080, "http"),
]
CHECK_URL = "https://www.google.com/generate_204"
PROBE_TIMEOUT = 2.0
POSITIVE_TTL = 60.0
NEGATIVE_TTL = 10.0
_cache = {"value": None, "expires": 0.0}
_detect_lock = threading.Lock()
_AUTO = object()


class ProxyError(RuntimeError):
    """只包含可展示给用户的文字，不附带代理凭据或原始网络异常。"""


def from_config() -> str | None:
    raw = M.CFG.get("tools", {}).get("proxy")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ProxyError('tools.proxy 请填写 "auto"、"off" 或完整代理地址。')
    raw = raw.strip()
    if not raw or raw.lower() == "auto":
        return None
    if raw.lower() in ("off", "none", "false", "0"):
        return "off"
    return normalize_url(raw)


def normalize_url(url: str) -> str:
    """只接受客户端共同支持的协议。非法地址不能被当成直连。"""
    try:
        if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError
        u = urlsplit(url)
        if (u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname
                or u.path not in ("", "/") or u.query or u.fragment
                or (u.port is not None and not 1 <= u.port <= 65535)):
            raise ValueError
        # 触发无效 IPv6 / 端口的校验；原样保留已编码的用户名和密码。
        return u._replace(path="").geturl()
    except (ValueError, TypeError, AttributeError):
        raise ProxyError("代理地址格式不正确。支持 http://、https://、socks5:// 或 "
                         "socks5h://，例如 http://127.0.0.1:7890。") from None


def display_url(url: str | None) -> str:
    """屏幕和日志不输出代理用户名或密码，包括无效地址。"""
    if not url:
        return "(直连)"
    if url.lower() in ("auto", "off", "none", "false", "0"):
        return url.lower()
    try:
        u = urlsplit(normalize_url(url))
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://{host}{port}" + ("（凭据已隐藏）" if u.username is not None else "")
    except ProxyError:
        return "(地址格式错误)"


def error_text(error: Exception) -> str:
    """保留错误类别，避免依赖库把含凭据的 URL 回显到对话或日志。"""
    if isinstance(error, ProxyError):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    detail = (type(error).__name__ + str(error)).lower()
    if "timeout" in detail or "timed out" in detail:
        return "连接超时"
    if any(word in detail for word in ("certificate", "tls", "ssl")):
        return "TLS 连接或证书校验失败"
    if "no results" in detail:
        return "没有返回结果"
    return f"连接或服务请求失败（{type(error).__name__}）"


def make_client(proxy_url: str | None, timeout: float = 8.0, **kwargs):
    """固定 primp 版本的每客户端路由；None 明确直连，不继承环境代理。"""
    import primp
    route = normalize_url(proxy_url) if proxy_url else None
    client = primp.Client(proxy=route, timeout=timeout, **kwargs)
    # primp 构造器的 None 会读系统/环境代理；setter 的 None 才清除它们。
    # 不可通过暂改 os.environ 实现，否则并发大脑/搜索/更新会互相影响。
    client.proxy = route
    return client


def request(url: str, *, proxy_url=_AUTO, timeout: float = 30.0,
            method: str = "GET", **kwargs):
    route = detect() if proxy_url is _AUTO else proxy_url
    try:
        response = make_client(route, timeout=timeout).request(method, url, **kwargs)
    except Exception as exc:
        path = "代理连接或目标服务" if route else "直连或目标服务"
        raise ProxyError(f"{path}请求失败：{error_text(exc)}。请检查网络、代理地址和服务状态。") from None
    if response.status_code >= 400:
        raise urllib.error.HTTPError(url, response.status_code, f"HTTP {response.status_code}", None, None)
    return response


def ddgs_client(proxy_url: str | None, timeout: float = 5.0):
    # 顶层 ddgs.DDGS 是惰性代理类，不能继承；它会丢弃子类的 override。
    # 以下适配依赖锁定的 ddgs 9.16.0 / primp 2.0.1，升级需跑真实库回归。
    from ddgs.ddgs import DDGS
    route = normalize_url(proxy_url) if proxy_url else None

    class RoutedDDGS(DDGS):
        def __init__(self):
            super().__init__(proxy=route, timeout=timeout)
            self._proxy = route  # 阻止构造器的 DDGS_PROXY 回退
            self._routed_engines = set()

        def _get_engines(self, category, backend):
            engines = super()._get_engines(category, backend)
            for engine in engines:
                if id(engine) not in self._routed_engines:
                    engine.http_client.client.proxy = route
                    self._routed_engines.add(id(engine))
            return engines

        def extract(self, url, fmt="text_markdown"):
            # 原实现会再建一个隐式继承环境的客户端；这里沿用同一选择。
            if fmt not in ("text_markdown", "text_plain", "text_rich", "text", "content"):
                raise ValueError("不支持的网页提取格式")
            response = request(url, proxy_url=route, timeout=timeout)
            return {"url": url, "content": getattr(response, fmt)}

    return RoutedDDGS()


def from_windows_registry() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        if not isinstance(server, str) or not server.strip():
            return None
        scheme = "http"
        if "=" in server:
            parts = dict(p.strip().split("=", 1) for p in server.split(";") if "=" in p)
            server = parts.get("https") or parts.get("http") or ""
            if not server and parts.get("socks"):
                server, scheme = parts["socks"], "socks5h"
        server = server.strip()
        return (server if "://" in server else f"{scheme}://{server}") if server else None
    except (ImportError, OSError):
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def from_port_scan() -> list[str]:
    """所有监听候选，不把 TCP 端口开放等同于代理可用。"""
    return [f"{scheme}://127.0.0.1:{port}" for port, scheme in COMMON_PORTS if _port_open(port)]


def verify(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        client = make_client(url, timeout=timeout, follow_redirects=False)
        result = client.get(CHECK_URL)
        ok = result.status_code == 204 or (result.status_code == 200 and not result.content.strip())
        msg = f"HTTP {result.status_code}"
        if result.status_code == 200 and not ok:
            msg += "（检测页内容不符，可能是登录页或拦截页）"
        return ok, msg
    except Exception as exc:
        return False, error_text(exc)


def detect(verbose: bool = False, force: bool = False) -> str | None:
    """选择路由。显式地址不受检测站点可达性约束；auto 缓存短期有效。"""
    explicit = from_config()  # 每次先读模式，off/显式配置不会被旧缓存盖过
    if explicit == "off":
        return None
    if explicit:
        return explicit
    with _detect_lock:
        # 等待另一次后台检测期间，配置可能已被切换。
        explicit = from_config()
        if explicit is not None:
            return None if explicit == "off" else explicit
        if not force and time.monotonic() < _cache["expires"]:
            return _cache["value"]
        candidates = list(dict.fromkeys(filter(None, [from_windows_registry(), *from_port_scan()])))
        selected = None
        if candidates:
            # 同时最多四个验证；按来源优先级取首个成功结果。每个都有超时，
            # 避免十个无效监听端口逐个等待；未开始的探测会取消。
            pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="proxy-probe")
            futures = [pool.submit(verify, url, PROBE_TIMEOUT) for url in candidates]
            try:
                for candidate, future in zip(candidates, futures):
                    ok, msg = future.result()
                    if verbose:
                        print(f"[proxy] {display_url(candidate)} → {msg}", file=sys.stderr)
                    if ok:
                        selected = candidate
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        explicit = from_config()
        if explicit is not None:
            return None if explicit == "off" else explicit
        _cache.update(value=selected, expires=time.monotonic() + (POSITIVE_TTL if selected else NEGATIVE_TTL))
        if verbose and not selected:
            print("[proxy] 本次未检测到可用代理，选择直连；稍后会自动重试。", file=sys.stderr)
        return selected


def refresh() -> str | None:
    route = detect(verbose=True, force=True)
    if route and from_config() not in (None, "off"):
        ok, msg = verify(route)
        if not ok:
            raise ProxyError(f"手填代理未通过检测：{msg}。搜索仍使用该地址，不会改为直连；"
                             "请检查代理，检测站点也可能暂时不可用。")
    return route


def status(force: bool = False) -> dict:
    raw = M.CFG.get("tools", {}).get("proxy")
    out = {"配置值": display_url(raw) if isinstance(raw, str) and raw else "auto",
           "系统代理候选": display_url(from_windows_registry()),
           "说明": "只影响搜索、网页读取和检查更新；不继承环境代理，不修改系统设置。"}
    try:
        out["选择"] = display_url(refresh() if force else detect())
    except ProxyError as exc:
        out["选择"] = "配置或检测失败，请按提示检查"
        out["错误"] = str(exc)
    return out


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
            print(f"设置 {k} 失败（Git 返回 {r.returncode}）")
            return 1

    print(f"git 已走代理：{display_url(proxy)}")
    print("验证：git ls-remote https://github.com/git/git HEAD")
    print("撤销：python src/proxy.py git --off")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("test", "git"):
        if args[0] == "git" and "--off" in args:
            raise SystemExit(apply_to_git(None, unset=True))
        try:
            route = refresh()
        except ProxyError as exc:
            print(str(exc))
            raise SystemExit(1)
        if not route:
            print('当前选择直连：tools.proxy 为 off，或自动检测未找到可用代理。')
            raise SystemExit(1)
        if args[0] == "git":
            raise SystemExit(apply_to_git(route))
        print(f"检测端点可达：{display_url(route)}。搜索服务是否可用还需实际查询。")
        return
    result = status(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "错误" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
