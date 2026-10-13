"""Explicit search API adapters; no routing, fallback, cache or GUI side effects.

Integration contract for the search orchestrator::

    search(root, tools_config, "brave", query, n=5, kind="general",
           request_deadline=deadline)

``tools_config`` is config.json["tools"], not the whole document. Tavily keeps
its existing ``tools.tavily`` settings, credential precedence and reservations.
Brave/Serper read ``tools.search_apis.<id>.timeout_seconds`` (default 12, 1..20)
and their private key from secrets.json, then the named environment variable.
They have no local quota counter; account limits belong to their dashboards.

Returns {"answer": "", "results": [{"title", "url", "snippet", "source", "date"}]}.
``kind`` is general/news (``text`` is the existing tool's alias for general);
results are capped at five. All adapters make at most
one fixed-host HTTPS request, without redirects, retries or provider fallback.
The optional monotonic request deadline leaves 20 seconds for downstream work,
matching the existing Tavily adapter. Failures expose credential-free messages.
Importing this module or editing settings never sends a request.
"""
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit

import tavily_search as T


@dataclass(frozen=True)
class Provider:
    name: str
    summary: str
    account_url: str
    docs_url: str
    host: str
    path: str
    news_path: str
    method: str
    key_field: str
    env_var: str
    auth_header: str
    integrated: bool = False

    @property
    def endpoint(self):
        return f"{self.method} https://{self.host}{self.path}"


PROVIDERS = {
    "tavily": Provider(
        "Tavily", "适合为 AI 查找资料，返回网页摘要和来源；已有 Tavily 账号可继续使用。",
        "https://app.tavily.com", "https://docs.tavily.com/documentation/api-reference/endpoint/search",
        "api.tavily.com", "/search", "/search", "POST",
        "tavily_api_key", "TAVILY_API_KEY", "Authorization", integrated=True),
    "brave": Provider(
        "Brave Search", "使用 Brave 独立搜索索引，适合通用网页与新闻检索。",
        "https://api-dashboard.search.brave.com", "https://api.search.brave.com/app/documentation/web-search/get-started",
        "api.search.brave.com", "/res/v1/web/search", "/res/v1/news/search", "GET",
        "brave_search_api_key", "BRAVE_API_KEY", "X-Subscription-Token"),
    "serper": Provider(
        "Serper", "适合需要 Google 搜索结果的用户；由 Serper 第三方提供，并非 Google 官方 API。",
        "https://serper.dev", "https://serper.dev/playground",
        "google.serper.dev", "/search", "/news", "POST",
        "serper_api_key", "SERPER_API_KEY", "X-API-KEY"),
}
MAX_RESPONSE = 512 * 1024


class SearchAPIError(RuntimeError):
    """Safe for user-facing output; never wrap raw HTTP bodies or exceptions."""


def get_provider(provider):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("请选择 Tavily、Brave Search 或 Serper 搜索接口。")
    return PROVIDERS[provider]


def validate_key(provider, value):
    """Check paste/format errors only, not account validity. Blank is optional."""
    info = get_provider(provider)
    if not isinstance(value, str):
        raise ValueError(f"{info.name} API key 必须是文本。")
    value = value.strip()
    if len(value) > 512 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError(f"{info.name} API key 不能含空格、换行或中文，长度不超过 512 字符。")
    return value


def timeout_seconds(provider, config):
    """Validate only the requested adapter's settings; preserve unknown fields."""
    info = get_provider(provider)
    try:
        settings = config.get("search_apis", {}).get(provider, {})
        value = settings.get("timeout_seconds", 12)
    except (AttributeError, TypeError):
        raise ValueError(f"{info.name} 搜索接口配置必须是对象。") from None
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError(f"{info.name} 请求超时须为 1–20 秒的整数。")
    return value


def load_key(root, config, provider):
    info = get_provider(provider)
    if provider == "tavily":
        try:
            return T.load_key(root, config)
        except T.TavilyError as error:
            raise SearchAPIError(str(error)) from None
    try:
        path = Path(root) / "data/secrets.json"
        if path.exists():
            if path.stat().st_size > 128 * 1024:
                raise ValueError()
            secrets = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(secrets, dict):
                raise ValueError()
        else:
            secrets = {}
        key = validate_key(provider, secrets.get(info.key_field) or os.environ.get(info.env_var) or "")
    except (OSError, ValueError):
        raise SearchAPIError(f"{info.name} 密钥配置无法读取，请检查 data/secrets.json。") from None
    if not key:
        raise SearchAPIError(f"请在配置 → 搜索与文件填写 {info.name} API key（{info.key_field}）。")
    return key


def _time_left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SearchAPIError("搜索接口请求超时，未自动重试。")
    return remaining


def _request(info, path, payload, key, deadline):
    connection = http.client.HTTPSConnection(info.host, timeout=_time_left(deadline))
    response = None
    try:
        connection.request(info.method, path,
            body=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     info.auth_header: key})
        sock = connection.sock
        sock.settimeout(_time_left(deadline))
        response = connection.getresponse()
        if response.status != 200:
            if response.status in (401, 403):
                raise SearchAPIError(f"{info.name} 密钥无效或无权访问，请检查账号权限。")
            if response.status in (402, 429):
                raise SearchAPIError(f"{info.name} 额度不足或请求受限，请在服务商后台查看；未自动重试。")
            raise SearchAPIError(f"{info.name} 暂不可用（HTTP {response.status}），未自动重试。")
        raw = bytearray()
        while not response.isclosed():
            sock.settimeout(_time_left(deadline))
            block = response.read1(min(32768, MAX_RESPONSE + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
            if len(raw) > MAX_RESPONSE:
                raise SearchAPIError(f"{info.name} 响应过大，已停止读取。")
        _time_left(deadline)
        return json.loads(raw.decode("utf-8"))
    finally:
        if response is not None:
            response.close()
        connection.close()


def _normalize(provider, data, n, kind):
    if not isinstance(data, dict):
        raise ValueError()
    if provider == "brave":
        rows = data.get("results") if kind == "news" else data.get("web", {}).get("results")
    else:
        rows = data.get("news" if kind == "news" else "organic")
    if not isinstance(rows, list):
        raise ValueError()
    results = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError()
        def field(name, size):
            value = item.get(name)
            return value[:size] if isinstance(value, str) else ""
        url = field("url" if provider == "brave" else "link", 2048)
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            continue
        source = field("source", 120)
        if provider == "brave":
            meta = item.get("meta_url")
            if isinstance(meta, dict) and isinstance(meta.get("hostname"), str):
                source = meta["hostname"][:120]
        results.append({"title": field("title", 240), "url": url,
            "snippet": field("description" if provider == "brave" else "snippet", 900),
            "source": source, "date": field("age" if provider == "brave" else "date", 48)})
        if len(results) >= n:
            break
    return {"answer": "", "results": results}


def search(root, config, provider, query, n=5, kind="general", *, request_deadline=None):
    """Call exactly the requested provider; orchestration must opt in explicitly."""
    info = get_provider(provider)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("搜索词需为 1–500 字符的文本。")
    if type(n) is not int or n < 1:
        raise ValueError("搜索结果数量须为正整数。")
    if kind not in ("general", "text", "news"):
        raise ValueError("搜索类型须为 general、text 或 news。")
    if not isinstance(config, dict):
        raise ValueError("搜索配置必须是 tools 对象。")
    query, n = query.strip(), min(n, 5)
    if provider == "tavily":
        try:
            return T.search(root, config, query, n, kind, request_deadline=request_deadline)
        except T.TavilyError as error:
            raise SearchAPIError(str(error)) from None
    deadline = time.monotonic() + timeout_seconds(provider, config)
    if request_deadline is not None:
        deadline = min(deadline, request_deadline - 20)
    if _time_left(deadline) < 1:
        raise SearchAPIError("本轮剩余时间不足，未发起搜索接口请求。")
    key = load_key(root, config, provider)
    path = info.news_path if kind == "news" else info.path
    if provider == "brave":
        path += "?" + urlencode({"q": query, "count": n})
        payload = None
    else:
        payload = {"q": query, "num": n}
    try:
        return _normalize(provider, _request(info, path, payload, key, deadline), n, kind)
    except SearchAPIError:
        raise
    except (OSError, http.client.HTTPException):
        raise SearchAPIError(f"{info.name} 连接失败或超时，未自动重试。") from None
    except Exception:
        raise SearchAPIError(f"{info.name} 返回格式异常，本次搜索未完成。") from None
