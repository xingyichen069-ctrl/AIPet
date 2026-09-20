#!/usr/bin/env python3
"""AIPet 网络搜索与网页抓取。

搜索链路固定为：意图解析 → 缓存策略 → 后端 → 结果归一化 → 时效排序 → prompt 渲染。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

TOOLS_CFG = M.CFG.get("tools", {})
CACHE_DIR = M.ROOT / "data" / "cache"

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class SearchBackendUnavailable(RuntimeError):
    pass


class SearchNoResults(RuntimeError):
    pass


class SearchTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchIntent:
    original_query: str
    backend_query: str
    kind: str = "text"
    freshness: str | None = None
    force_refresh: bool = False
    incremental: bool = True
    temporal_mode: str = "normal"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str = ""
    published_ts: float | None = None


QUERY_STOPWORDS = {
    "请", "帮我", "帮忙", "一下", "什么", "怎么", "如何", "可以", "能否",
    "有没有", "告诉我", "查询", "搜索", "关于", "信息", "的", "了", "吗",
    "呢", "啊", "和", "与", "及", "在", "是", "我", "你", "他",
    "the", "a", "an", "and", "or", "of", "to", "for", "is", "are",
    "what", "how", "please", "tell", "me", "about",
}
QUERY_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9_+.#-]*|\d+(?:\.\d+)?")
PHRASE_RE = re.compile(r'"([^"\n]{2,80})"|“([^”\n]{2,80})”|「([^」\n]{2,80})」')


def normalize_query(query: str) -> str:
    q = (query or "").replace("\u3000", " ").strip()
    q = re.sub(r"[\t\r\n]+", " ", q)
    return re.sub(r"\s+", " ", q).lower()


def _strip_request_words(query: str) -> str:
    return re.sub(
        r"请帮我搜索|请搜索|帮我搜索|请帮我|帮我|告诉我|搜索一下|搜索|查询|查找|请问|相关信息",
        " ", query)


def resolve_search_intent(query: str, kind: str = "auto", freshness: str = "auto",
                          force_refresh: bool = False, incremental: bool = True) -> SearchIntent:
    """先识别时效语义，再生成后端查询词；绝不把“最新”当普通停用词丢掉。"""
    original = normalize_query(query)
    q = original
    explicit_kind = (kind or "auto").lower()
    explicit_freshness = (freshness or "auto").lower()
    is_news = explicit_kind == "news" or bool(
        re.search(r"新闻|消息|进展|报道|赛况|赛果|比分|比赛|news|breaking", q))
    resolved = None if explicit_freshness in ("", "auto", "none") else explicit_freshness
    temporal = "normal"
    if re.search(r"今天|今日|刚刚|刚发布|刚发生|实时|即时", q):
        resolved, temporal = "day", "breaking_news"
    elif re.search(r"最近|近期|这几天|本周|过去一周", q):
        resolved, temporal = "week", "recent_general"
    elif re.search(r"本月|这个月", q):
        resolved, temporal = "month", "recent_general"
    elif re.search(r"今年|本年度", q):
        resolved, temporal = "year", "recent_general"
    if re.search(r"最新(版本|稳定版|发行版)|当前版本|latest version|current version", q):
        resolved, temporal, force_refresh = None, "latest_version", True
        suffix = " latest version current release"
    else:
        suffix = ""
        if re.search(r"最新(消息|新闻|进展|比赛|赛况|赛果|比分|事件)|最新$|当前比赛进展", q):
            resolved = resolved or "day"
            temporal = temporal if temporal != "normal" else "breaking_news"
            is_news, force_refresh = True, True
    if re.search(r"重新搜|刷新|再查一次|再搜一次|refresh", q):
        force_refresh = True
    backend_query = re.sub(r"\s+", " ", _strip_request_words(q)).strip() + suffix
    freshness_mode = resolved or is_news or force_refresh
    return SearchIntent(
        original_query=original,
        backend_query=backend_query.strip(),
        kind="news" if is_news else "text",
        freshness=resolved,
        force_refresh=bool(force_refresh),
        incremental=bool(incremental) and not freshness_mode,
        temporal_mode=temporal,
    )


def extract_keywords(query: str, max_terms: int = 12) -> list[str]:
    """仅提取稳定关键词；时间词保留在原查询和 SearchIntent 中。"""
    q = normalize_query(query)
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip(" \t\r\n,，。！？!?;；:：()（）[]【】{}《》")
        if len(term) < 2 or term in QUERY_STOPWORDS or term in seen:
            return
        seen.add(term)
        out.append(term)

    for m in PHRASE_RE.finditer(q):
        add(next((x for x in m.groups() if x), ""))
    for token in QUERY_RE.findall(_strip_request_words(q)):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            cleaned = re.sub(r"[的在和与及了吗呢啊请我你他其这那要能否]", "", token)
            if cleaned:
                add(cleaned)
        else:
            add(token)
        if len(out) >= max_terms:
            break
    return out[:max_terms]


def prepare_query(query: str) -> tuple[str, list[str]]:
    intent = resolve_search_intent(query)
    words = extract_keywords(intent.backend_query)
    return (" ".join(words) if words else intent.backend_query), words


# ---------------------------------------------------------------- 缓存

def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def _cache_read(kind: str, key: str):
    try:
        return json.loads(_cache_path(kind, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_get(kind: str, key: str, ttl: int):
    blob = _cache_read(kind, key)
    if not blob or time.time() - blob.get("_ts", 0) > ttl:
        return None
    return blob.get("data")


def _cache_put(kind: str, key: str, data) -> None:
    _cache_path(kind, key).write_text(
        json.dumps({"_ts": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")


def clear_cache() -> int:
    n = 0
    if CACHE_DIR.exists():
        for f in CACHE_DIR.rglob("*.json"):
            try:
                f.unlink(); n += 1
            except OSError:
                pass
    return n


def _cache_key(intent: SearchIntent, backend: str, max_results: int) -> str:
    return json.dumps({"backend": backend, "kind": intent.kind,
                       "query": intent.backend_query, "freshness": intent.freshness,
                       "temporal_mode": intent.temporal_mode,
                       "max_results": int(max_results)}, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 结果时间与归一化

def parse_result_time(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    text = str(value).strip()
    m = re.search(r"(\d+)\s*(分钟|小时|天|minutes?|hours?|days?)\s*(前|ago)", text, re.I)
    if m:
        amount = int(m.group(1)); unit = m.group(2).lower()
        seconds = amount * (60 if unit.startswith(("分钟", "minute")) else
                            3600 if unit.startswith(("小时", "hour")) else 86400)
        return now.timestamp() - seconds
    m = re.search(r"(\d+)\s*(分钟前|小时前|天前)", text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        seconds = amount * (60 if unit.startswith("分钟") else
                            3600 if unit.startswith("小时") else 86400)
        return now.timestamp() - seconds
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y"):
        try:
            dt = datetime.strptime(text[:25], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def normalize_results(items: list[dict]) -> list[dict]:
    out = []
    for item in items or []:
        x = dict(item)
        x["published_at"] = x.get("published_at") or x.get("date") or ""
        x["published_ts"] = parse_result_time(x["published_at"])
        x["date"] = x.get("date") or x["published_at"]
        out.append(x)
    return out


def _result_key(item: dict) -> str:
    url = (item.get("url") or "").strip()
    if url:
        try:
            p = urllib.parse.urlsplit(url)
            qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                  if not k.lower().startswith(("utm_", "spm", "from"))]
            url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path,
                                           urllib.parse.urlencode(qs), ""))
        except ValueError:
            pass
        return url.rstrip("/").lower()
    return re.sub(r"\W+", "", (item.get("title") or "").lower())


def _merge_results(old: list[dict], new: list[dict], limit: int) -> tuple[list[dict], int]:
    old_keys = {_result_key(x) for x in old if _result_key(x)}
    new_keys = {_result_key(x) for x in new if _result_key(x)}
    added = len(new_keys - old_keys)
    merged: list[dict] = []
    seen: set[str] = set()
    for item in list(new) + list(old):
        key = _result_key(item)
        if not key or key in seen:
            continue
        seen.add(key); merged.append(item)
    return merged[:max(1, int(limit))], added


def _apply_freshness(results: list[dict], freshness: str | None) -> list[dict]:
    if not freshness:
        return results
    days = {"day": 1, "week": 7, "month": 31, "year": 366}.get(freshness)
    if not days:
        return results
    cutoff = time.time() - days * 86400
    dated = [x for x in results if x.get("published_ts") is not None and x["published_ts"] >= cutoff]
    undated = [x for x in results if x.get("published_ts") is None]
    # 缺日期不能冒充最新，但保留作明确的“未标日期”候选。
    return sorted(dated, key=lambda x: x["published_ts"], reverse=True) + undated


# ---------------------------------------------------------------- 后端

def _proxy_url() -> str | None:
    try:
        import proxy as PROXY
        return PROXY.proxy_for_url("https://www.google.com") or None
    except Exception:
        return None


def _search_ddgs(query: str, n: int, kind: str, freshness: str | None = None) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError as e:
        raise SearchBackendUnavailable("未安装 ddgs。请安装依赖或切换 Tavily。") from e
    timelimit = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(freshness)
    kw = {"max_results": n}
    if timelimit: kw["timelimit"] = timelimit
    proxy_url = _proxy_url()
    has_proxy = bool(proxy_url)
    news_chain = ["auto", "bing", "duckduckgo", "yahoo"] if has_proxy else ["bing", "duckduckgo", "auto"]
    text_chain = ["auto", "bing", "duckduckgo", "google", "yahoo"] if has_proxy else ["bing", "google", "auto"]

    def run_chain(d, method, chain, kwargs):
        last = None
        for backend in chain:
            try:
                got = getattr(d, method)(query, backend=backend, **kwargs)
                if got: return got
            except Exception as e:
                last = e
        raise SearchBackendUnavailable(f"DDGS 后端不可用：{last}")

    try:
        with DDGS(proxy=proxy_url) as d:
            if kind == "news":
                try:
                    raw = run_chain(d, "news", news_chain, kw)
                except SearchBackendUnavailable:
                    # 新闻聚合器在国内常常没有结果；回退到普通文本搜索，
                    # 但完整保留用户指定的 timelimit，不能把 day 悄悄放大成 week。
                    raw = run_chain(d, "text", text_chain, kw)
            else:
                raw = run_chain(d, "text", text_chain, kw)
    except (TimeoutError, socket.timeout) as e:
        raise SearchTimeout("DDGS 搜索超时") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise SearchBackendUnavailable(f"DDGS 网络不可用：{e}") from e
    out = []
    for r in raw or []:
        out.append({"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                    "snippet": r.get("body") or r.get("description", ""),
                    "source": r.get("source", ""), "date": r.get("date", "")})
    if not out: raise SearchNoResults("DDGS 没有结果")
    return out


def _search_tavily(query: str, n: int, kind: str, freshness: str | None = None) -> dict:
    key = TOOLS_CFG.get("tavily_key") or __import__("os").environ.get("TAVILY_API_KEY", "")
    if not key: raise SearchBackendUnavailable("未配置 tavily_key")
    payload = {"query": query, "max_results": n,
               "topic": "news" if kind == "news" else "general",
               "include_answer": True, "search_depth": "basic"}
    if freshness in {"day", "week", "month", "year"}: payload["time_range"] = freshness
    req = urllib.request.Request("https://api.tavily.com/search",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: data = json.loads(r.read().decode())
    except (TimeoutError, socket.timeout) as e:
        raise SearchTimeout("Tavily 搜索超时") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise SearchBackendUnavailable(f"Tavily 网络不可用：{e}") from e
    out = [{"title": x.get("title", ""), "url": x.get("url", ""),
            "snippet": x.get("content", ""), "source": "", "date": x.get("published_date", "")}
            for x in data.get("results", [])]
    if not out: raise SearchNoResults("Tavily 没有结果")
    return {"answer": data.get("answer", "") or "", "results": out}


def _search_searxng(query: str, n: int, kind: str, freshness: str | None = None) -> list[dict]:
    base = TOOLS_CFG.get("searxng_url")
    if not base: raise SearchBackendUnavailable("未配置 searxng_url")
    params = {"q": query, "format": "json", "categories": "news" if kind == "news" else "general"}
    if freshness: params["time_range"] = freshness
    url = f"{base.rstrip('/')}/search?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r: data = json.loads(r.read().decode())
    except (TimeoutError, socket.timeout) as e:
        raise SearchTimeout("SearXNG 搜索超时") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise SearchBackendUnavailable(f"SearXNG 网络不可用：{e}") from e
    out = [{"title": x.get("title", ""), "url": x.get("url", ""),
            "snippet": x.get("content", ""), "source": x.get("engine", ""),
            "date": x.get("publishedDate", "")} for x in data.get("results", [])[:n]]
    if not out: raise SearchNoResults("SearXNG 没有结果")
    return out


def _search_google(query: str, n: int, kind: str, freshness: str | None = None) -> list[dict]:
    q = query + ({"day": " when:1d", "week": " when:7d"}.get(freshness, ""))
    params = urllib.parse.urlencode({"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"})
    url = f"https://news.google.com/rss/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: root = ET.fromstring(r.read())
    except (TimeoutError, socket.timeout) as e:
        raise SearchTimeout("Google News 超时") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise SearchBackendUnavailable(f"Google News 网络不可用：{e}") from e
    out = []
    for item in root.findall(".//item")[:max(1, int(n))]:
        def clean(v): return re.sub(r"<[^>]+>", " ", html.unescape(v or "")).strip()
        out.append({"title": clean(item.findtext("title")), "url": item.findtext("link", ""),
                    "snippet": clean(item.findtext("description")), "source": clean(item.findtext("source")),
                    "date": item.findtext("pubDate", "")})
    if not out: raise SearchNoResults("Google News 没有结果")
    return out


# ---------------------------------------------------------------- 对外接口

def search(query: str, max_results: int = 5, kind: str = "auto", backend: str | None = None,
           use_cache: bool = True, incremental: bool = True, freshness: str = "auto",
           force_refresh: bool = False) -> dict:
    intent = resolve_search_intent(query, kind, freshness, force_refresh, incremental)
    requested_freshness = None if (freshness or "auto").lower() in ("", "auto", "none") else freshness
    backend = backend or TOOLS_CFG.get("search_backend", "ddgs")
    ttl = TOOLS_CFG.get("cache_ttl", {}).get("news" if intent.kind == "news" else "general", 3600)
    key = _cache_key(intent, backend, max_results)
    # 时效结果写入独立的 freshness key，并按 news/general TTL 管理；只有明确刷新
    # 才跳过读取缓存，这样连续询问“最新消息”可以命中几分钟内的同一份结果。
    skip_cache = intent.force_refresh
    if use_cache and not skip_cache:
        hit = _cache_get("search", key, ttl)
        if hit is not None:
            return {**hit, "cached": True, "intent": intent.__dict__, "new_results": 0}
    adapters = {"ddgs": _search_ddgs, "tavily": _search_tavily,
                "searxng": _search_searxng, "google": _search_google}
    adapter = adapters.get(backend)
    if not adapter: raise SearchBackendUnavailable(f"未知搜索后端：{backend}")
    try:
        raw = adapter(intent.backend_query, int(max_results), intent.kind, intent.freshness)
    except (SearchBackendUnavailable, SearchNoResults, SearchTimeout):
        raise
    answer = ""
    if isinstance(raw, dict):
        answer = str(raw.get("answer") or "")
        raw = raw.get("results") or []
    results = _apply_freshness(normalize_results(raw), intent.freshness)
    previous = []
    if intent.incremental and use_cache:
        blob = _cache_read("search", key)
        if blob and isinstance(blob.get("data"), dict): previous = blob["data"].get("results") or []
    merged, added = _merge_results(previous, results, max_results) if intent.incremental else (results[:max_results], len({_result_key(x) for x in results}))
    out = {"backend": backend, "answer": answer, "results": merged, "cached": False,
           "query": intent.original_query, "keywords": extract_keywords(intent.backend_query),
           "incremental": intent.incremental, "new_results": added,
           "intent": intent.__dict__, "requested_freshness": requested_freshness,
           "effective_freshness": intent.freshness}
    if use_cache and results: _cache_put("search", key, out)
    return out


def fetch(url: str, use_cache: bool = True) -> str:
    ttl = TOOLS_CFG.get("cache_ttl", {}).get("page", 86400)
    if use_cache:
        hit = _cache_get("fetch", url, ttl)
        if hit is not None: return hit
    try:
        from ddgs import DDGS
        with DDGS(proxy=_proxy_url()) as d:
            r = d.extract(url, fmt="text_markdown")
        text = r.get("content", "") if isinstance(r, dict) else str(r)
    except Exception:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r: raw = r.read().decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
    if use_cache and text: _cache_put("fetch", url, text)
    return text


def as_prompt_block(query: str, max_results: int = 5, kind: str = "auto", max_chars: int = 1800,
                    incremental: bool = True, freshness: str = "auto", backend: str | None = None,
                    force_refresh: bool = False) -> str:
    try:
        r = search(query, max_results, kind, backend=backend, incremental=incremental,
                   freshness=freshness, force_refresh=force_refresh)
    except SearchBackendUnavailable as e:
        return f"（搜索后端不可用：{e}）"
    except SearchTimeout as e:
        return f"（搜索超时：{e}）"
    except SearchNoResults as e:
        return f"（搜索无结果：{e}）"
    intent = r.get("intent", {})
    lines = ["## 网络搜索", f"原查询：{query}",
             f"模式：{intent.get('kind', kind)} · 时间范围：{intent.get('freshness') or '不限'} · "
             f"缓存：{'命中' if r.get('cached') else '跳过/新请求'}"]
    if r.get("answer"): lines.append(f"摘要：{r['answer']}")
    results = r.get("results", [])
    if not results: lines.append("没有返回带日期的可靠结果。")
    fixed = sum(len(x) for x in lines) + 40
    budget = max(80, (max_chars - fixed) // max(1, len(results)))
    for i, x in enumerate(results, 1):
        date = x.get("published_at") or x.get("date") or "未标日期"
        body = re.sub(r"\s+", " ", x.get("snippet", ""))
        body = body[:budget]
        domain = urllib.parse.urlsplit(x.get("url", "")).netloc
        lines.append(f"{i}. [{date}] {x.get('title', '')}\n   {domain}\n   {body}")
    # 不对完整文本再做硬切片，否则最后几条会被截成半个标题或半个 URL。
    # 每条摘要已经按预算截短，标题、日期和来源始终完整保留。
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    if not args: print(__doc__); return
    cmd = args[0]
    if cmd == "search":
        q = args[1] if len(args) > 1 else ""; n = 5; kind = "auto"; freshness = "auto"; refresh = False
        i = 2
        while i < len(args):
            if args[i] in ("-n", "--num") and i + 1 < len(args): n = int(args[i + 1]); i += 2
            elif args[i] == "--news": kind = "news"; i += 1
            elif args[i] == "--freshness" and i + 1 < len(args): freshness = args[i + 1]; i += 2
            elif args[i] in ("--refresh", "--force-refresh"): refresh = True; i += 1
            else: i += 1
        print(as_prompt_block(q, n, kind=kind, freshness=freshness, force_refresh=refresh, max_chars=6000))
    elif cmd == "fetch":
        print(fetch(args[1] if len(args) > 1 else "")[:3000])
    elif cmd == "cache":
        if "--clear" in args: print(f"已清除 {clear_cache()} 个缓存文件")
        else:
            files = list(CACHE_DIR.rglob("*.json")) if CACHE_DIR.exists() else []
            print(f"缓存文件 {len(files)} 个，共 {sum(f.stat().st_size for f in files) / 1024:.1f} KB")
    elif cmd == "prompt": print(as_prompt_block(args[1] if len(args) > 1 else ""))
    else: print(__doc__)


if __name__ == "__main__": main()
