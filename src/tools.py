#!/usr/bin/env python3
"""
tools.py —— 网络搜索 / 网页抓取

给两条路线共用：
  · 插槽 A（Cherry Studio agent）：其实用不上——agent 自带搜索工具。
    但 agent 可以用这个脚本做批量、带缓存的检索。
  · 插槽 B（独立进程）：这是它的网络层。

后端说明（2026 年现状）：
  ddgs     免费、无需 API key，聚合 bing/brave/google/duckduckgo 等。
           **默认后端。** 需要 pip install ddgs
  tavily   1000 次/月免费，无需信用卡，返回 LLM 就绪的摘要，质量最好。
  searxng  自建实例，完全自主，无限额。需要你自己部署。

缓存：同一 query 在 TTL 内直接读本地缓存，不重复请求。
      这是省额度最有效的手段——比任何优化都管用。

用法：
    python src/tools.py search "Claude 最新模型"
    python src/tools.py search "天气预报" -n 3 --news
    python src/tools.py fetch https://example.com
    python src/tools.py cache --clear
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOOLS_CFG = M.CFG.get("tools", {})
CACHE_DIR = M.ROOT / "data" / "cache"

# 查询归一化不依赖第三方分词包。中文按连续片段和常见专名保留，
# 英文按单词保留；停用词只用于生成搜索词，不会改变用户看到的原问题。
QUERY_STOPWORDS = {
    "请", "帮我", "帮忙", "一下", "现在", "最近", "目前", "什么", "怎么",
    "如何", "可以", "能否", "有没有", "告诉我", "查询", "搜索", "关于", "信息", "的",
    "了", "吗", "呢", "啊", "和", "与", "及", "在", "是", "我", "你", "他",
    "the", "a", "an", "and", "or", "of", "to", "for", "is", "are", "what",
    "how", "please", "tell", "me", "about",
}
QUERY_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9_+.#-]*|\d+(?:\.\d+)?")
PHRASE_RE = re.compile(r'"([^"\n]{2,80})"|“([^”\n]{2,80})”|「([^」\n]{2,80})」')
COMMON_CJK_TERMS = (
    "国内网站", "网络搜索", "关键词", "视频", "评论", "分析", "字幕", "文字",
    "语音", "增量", "时间", "热度", "代理", "污染", "直连", "模型", "代码",
    "网页", "新闻", "教程", "版本", "天气", "价格", "下载", "读取",
)


def normalize_query(query: str) -> str:
    """规范空白、标点和大小写，保证同一问题命中同一个缓存。"""
    q = (query or "").replace("\u3000", " ").strip()
    q = re.sub(r"[\t\r\n]+", " ", q)
    q = re.sub(r"\s+", " ", q)
    return q.lower()


def extract_keywords(query: str, max_terms: int = 12) -> list[str]:
    """提取稳定的中英文关键词，保留引号短语、数字和专名。"""
    q = normalize_query(query)
    # 先移除提问套话，避免「请帮我搜索」「现在能不能」被当成主题词。
    q_tokens = re.sub(
        r"请帮我搜索|请搜索|帮我搜索|请帮我|帮我|告诉我|搜索一下|搜索|查询|查找|请问|现在|最近|目前|最新|相关信息",
        " ", q)
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
    for token in QUERY_RE.findall(q_tokens):
        # 中文的虚词和过短片段噪声很大；四字以上通常是有效主题，
        # 两三个字则保留，避免人名、型号被过滤。
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if token in QUERY_STOPWORDS:
                continue
            cleaned = re.sub(r"[的在和与及了吗呢啊请我你他其这那要能否]", "", token)
            found = []
            for term in COMMON_CJK_TERMS:
                if term in cleaned:
                    found.append(term)
                    cleaned = cleaned.replace(term, " ")
            for term in found:
                add(term)
            if not cleaned.strip():
                continue
            token = cleaned.replace(" ", "")
            if len(token) > 8:
                # 长句拆成相邻 2~4 字片段，搜索引擎更容易命中。
                for size in (4, 3):
                    for i in range(0, len(token) - size + 1, size):
                        add(token[i:i + size])
            else:
                add(token)
        else:
            add(token)
        if len(out) >= max_terms:
            break
    return out[:max_terms]


def prepare_query(query: str) -> tuple[str, list[str]]:
    """返回用于后端的查询和关键词；没有有效词时回退到原问题。"""
    original = normalize_query(query)
    words = extract_keywords(original)
    return (" ".join(words) if words else original), words


# ---------------------------------------------------------------- 缓存

def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def _cache_read(kind: str, key: str):
    p = _cache_path(kind, key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return blob


def _cache_get(kind: str, key: str, ttl: int):
    blob = _cache_read(kind, key)
    if not blob or time.time() - blob.get("_ts", 0) > ttl:
        return None
    return blob.get("data")


def _cache_put(kind: str, key: str, data) -> None:
    p = _cache_path(kind, key)
    p.write_text(json.dumps({"_ts": time.time(), "data": data},
                            ensure_ascii=False), encoding="utf-8")


def clear_cache() -> int:
    n = 0
    for f in CACHE_DIR.rglob("*.json"):
        f.unlink()
        n += 1
    return n


# ---------------------------------------------------------------- 后端

def _search_ddgs(query: str, n: int, kind: str, freshness: str = "") -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        raise RuntimeError(
            "未安装 ddgs。运行：pip install ddgs\n"
            "（或改用 tavily 后端：在 data/config.json 里填 tavily_key）"
        )

    # 代理：配置填了就用配置的，填 "auto"（或留空）就自动探测。
    # 自动探测会读系统代理、扫常见端口，并实际验证一次能不能出境外。
    kw = {"max_results": n}
    if freshness in {"d", "day", "w", "week", "m", "month", "y", "year"}:
        kw["timelimit"] = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(freshness, freshness)
    try:
        import proxy as PROXY
        p = PROXY.proxy_for_url("https://www.google.com")
        if p:
            kw["proxy"] = p
    except Exception:
        pass

    def run_chain(d, func_name: str, backends: list[str], kwargs: dict):
        """依次试后端，返回第一个有结果的。全失败则返回 (None, 最后错误)。"""
        last = None
        for backend in backends:
            try:
                fn = getattr(d, func_name)
                got = fn(query, backend=backend, **kwargs)
                if got:
                    return got, None
            except Exception as e:
                last = e
        return None, last

    # 后端顺序按国内实测可达性排的，不是随便写的：
    #   bing / google  → text 可用（实测通过）
    #   brave / startpage → DNSError，典型的 DNS 污染特征
    #   yahoo / mojeek → 网络不可达
    #   duckduckgo → 返回空
    # 把能用的排前面，省掉无谓的超时等待。
    # 开了代理之后这些限制会变，届时可以调回 ["auto", ...]。
    has_proxy = bool(kw.get("proxy"))
    if has_proxy:
        # 有代理时实测所有后端都通，连 news 也恢复（原来是全线失败）。
        # 交给 ddgs 自己挑，它选的最快。
        news_chain = ["auto", "bing", "duckduckgo", "yahoo"]
        text_chain = ["auto", "bing", "duckduckgo", "google", "yahoo", "mojeek"]
    else:
        # 直连时只有 bing / google 能用，其余会白等超时
        news_chain = ["bing", "duckduckgo", "auto"]
        text_chain = ["bing", "google", "auto", "duckduckgo", "mojeek"]

    raw, last_err = None, None
    with DDGS() as d:
        if kind == "news":
            raw, last_err = run_chain(d, "news", news_chain, kw)
            if raw is None:
                # ddgs 的 news 后端在国内基本不通（实测英文超时、中文无结果）。
                # 退回 text + 一周内的时间过滤，效果接近新闻且稳定得多。
                kw_fallback = {**kw, "timelimit": "w"}
                raw, last_err = run_chain(d, "text", text_chain, kw_fallback)
        else:
            raw, last_err = run_chain(d, "text", text_chain, kw)

    if raw is None:
        raise RuntimeError(f"搜索无结果或后端不可用（最后：{last_err}）")

    out = []
    for r in raw or []:
        out.append({
            "title": r.get("title", ""),
            "url": r.get("href") or r.get("url", ""),
            "snippet": r.get("body") or r.get("description", ""),
            "source": r.get("source", ""),
            "date": r.get("date", ""),
        })
    return out


def _search_tavily(query: str, n: int, kind: str, freshness: str = "") -> dict:
    import urllib.request

    key = TOOLS_CFG.get("tavily_key")
    if not key:
        raise RuntimeError("未配置 tavily_key（data/config.json）")

    topic = "news" if kind == "news" else "general"
    payload_data = {
        "query": query, "max_results": n, "topic": topic,
        "include_answer": True, "search_depth": "basic",
    }
    if freshness in {"day", "week", "month", "year"}:
        payload_data["time_range"] = freshness
    payload = json.dumps(payload_data).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))

    results = [{
        "title": x.get("title", ""),
        "url": x.get("url", ""),
        "snippet": x.get("content", ""),
        "source": "", "date": x.get("published_date", ""),
    } for x in data.get("results", [])]

    return {"answer": data.get("answer", ""), "results": results}


def _search_searxng(query: str, n: int, kind: str, freshness: str = "") -> list[dict]:
    import urllib.parse
    import urllib.request

    base = TOOLS_CFG.get("searxng_url")
    if not base:
        raise RuntimeError("未配置 searxng_url（data/config.json）")

    params = {"q": query, "format": "json",
              "categories": "news" if kind == "news" else "general"}
    if freshness in {"day", "week", "month", "year"}:
        params["time_range"] = freshness
    qs = urllib.parse.urlencode(params)
    url = f"{base.rstrip('/')}/search?{qs}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))

    return [{
        "title": x.get("title", ""), "url": x.get("url", ""),
        "snippet": x.get("content", ""),
        "source": x.get("engine", ""), "date": x.get("publishedDate", ""),
    } for x in data.get("results", [])[:n]]


def _search_google(query: str, n: int, kind: str, freshness: str = "") -> list[dict]:
    """Google News RSS 搜索；比 Google 网页页适合无浏览器的本地 agent。"""
    import html
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET

    q = query
    if freshness in {"d", "day"}:
        q += " when:1d"
    elif freshness in {"w", "week"}:
        q += " when:7d"
    params = urllib.parse.urlencode({
        "q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans",
    })
    url = f"https://news.google.com/rss/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        opener = __import__("proxy").opener_for_url(url)
    except Exception:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=20) as r:
        root = ET.fromstring(r.read())

    out = []
    for item in root.findall(".//item")[:max(1, int(n))]:
        def clean(value: str | None) -> str:
            value = html.unescape(value or "")
            return re.sub(r"<[^>]+>", " ", value).strip()
        out.append({
            "title": clean(item.findtext("title")),
            "url": item.findtext("link", ""),
            "snippet": clean(item.findtext("description")),
            "source": clean(item.findtext("source")),
            "date": item.findtext("pubDate", ""),
        })
    if not out:
        raise RuntimeError("Google News 没有返回结果")
    return out


# ---------------------------------------------------------------- 对外接口

def _result_key(item: dict) -> str:
    url = (item.get("url") or "").strip()
    if url:
        # 去掉追踪参数，避免同一页面被不同广告参数重复计数。
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
    """按 URL/title 去重，优先新结果，同时保留旧结果实现增量合并。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for item in list(new) + list(old):
        if not isinstance(item, dict):
            continue
        key = _result_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:max(1, int(limit))], max(0, len(merged) - len(old))


def search(query: str, max_results: int = 5, kind: str = "text",
           backend: str | None = None, use_cache: bool = True,
           incremental: bool = True, freshness: str = "") -> dict:
    """
    搜索网络。

    返回 {"backend":..., "answer":..., "results":[...], "cached":bool}
    """
    original_query = normalize_query(query)
    prepared, keywords = prepare_query(original_query)
    backend = backend or TOOLS_CFG.get("search_backend", "ddgs")
    ttl = TOOLS_CFG.get("cache_ttl", {}).get(
        "news" if kind == "news" else "general", 3600)

    ck = f"{backend}|{kind}|{prepared}|{max_results}|{freshness}"
    if use_cache:
        hit = _cache_get("search", ck, ttl)
        if hit is not None:
            return {**hit, "cached": True, "incremental": False,
                    "keywords": keywords, "query": original_query}

    if backend == "tavily":
        data = (_search_tavily(prepared, max_results, kind, freshness)
                if freshness else _search_tavily(prepared, max_results, kind))
        out = {"backend": backend, "answer": data["answer"],
               "results": data["results"], "cached": False}
    elif backend == "searxng":
        out = {"backend": backend, "answer": "",
               "results": (_search_searxng(prepared, max_results, kind, freshness)
                            if freshness else _search_searxng(prepared, max_results, kind)), "cached": False}
    elif backend == "google":
        out = {"backend": backend, "answer": "",
               "results": _search_google(prepared, max_results, kind, freshness),
               "cached": False}
    else:
        out = {"backend": "ddgs", "answer": "",
               "results": (_search_ddgs(prepared, max_results, kind, freshness)
                            if freshness else _search_ddgs(prepared, max_results, kind)), "cached": False}

    previous = []
    if incremental and use_cache:
        blob = _cache_read("search", ck)
        if blob and isinstance(blob.get("data"), dict):
            previous = blob["data"].get("results") or []
    merged, added = _merge_results(previous, out.get("results", []), max_results)
    out["results"] = merged
    out["query"] = original_query
    out["keywords"] = keywords
    out["incremental"] = bool(previous)
    out["new_results"] = added

    if use_cache and out["results"]:
        _cache_put("search", ck, {k: v for k, v in out.items()
                                   if k not in ("cached", "incremental", "new_results")})
    return out


def fetch(url: str, use_cache: bool = True) -> str:
    """抓取网页正文，转成 Markdown。"""
    ttl = TOOLS_CFG.get("cache_ttl", {}).get("page", 86400)
    if use_cache:
        hit = _cache_get("fetch", url, ttl)
        if hit is not None:
            return hit

    text = ""
    try:
        from ddgs import DDGS
        with DDGS() as d:
            r = d.extract(url, fmt="text_markdown")
        text = r.get("content", "") if isinstance(r, dict) else str(r)
    except Exception:
        # 退回标准库抓取
        import re
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            import proxy as PROXY
            opener = PROXY.opener_for_url(url)
        except Exception:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        text = re.sub(r"[ \t\r\f\v]+", " ", raw)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    if use_cache and text:
        _cache_put("fetch", url, text)
    return text


def as_prompt_block(query: str, max_results: int = 5, kind: str = "text",
                    max_chars: int = 1800, incremental: bool = True,
                    freshness: str = "", backend: str | None = None) -> str:
    """给 LLM 用的紧凑格式。"""
    try:
        r = search(query, max_results, kind, backend=backend,
                   incremental=incremental, freshness=freshness)
    except Exception as e:
        return f"（搜索失败：{e}）"

    lines = [f"## 网络搜索：{query}"]
    if r.get("keywords"):
        lines.append(f"关键词：{'、'.join(r['keywords'])}")
    if r.get("answer"):
        lines.append(f"\n**摘要**：{r['answer']}\n")
    for i, x in enumerate(r["results"], 1):
        date = f" ({x['date'][:10]})" if x.get("date") else ""
        lines.append(f"{i}. **{x['title']}**{date}\n   {x['url']}\n   {x['snippet']}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit("\n", 1)[0] + "\n…（已截断）"
    return out


# ---------------------------------------------------------------- CLI

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]

    if cmd == "search":
        q = args[1] if len(args) > 1 else ""
        n, kind = 5, "text"
        i = 2
        while i < len(args):
            if args[i] in ("-n", "--num") and i + 1 < len(args):
                n = int(args[i + 1]); i += 2
            elif args[i] == "--news":
                kind = "news"; i += 1
            else:
                i += 1
        try:
            r = search(q, n, kind)
        except Exception as e:
            print(f"搜索失败：{e}")
            return
        tag = "（缓存）" if r["cached"] else ""
        print(f"[{r['backend']}] {q} {tag}\n")
        if r.get("answer"):
            print(f"摘要：{r['answer']}\n")
        for i, x in enumerate(r["results"], 1):
            d = f" · {x['date'][:10]}" if x.get("date") else ""
            print(f"{i}. {x['title']}{d}")
            print(f"   {x['url']}")
            if x["snippet"]:
                print(f"   {x['snippet'][:160]}")
            print()

    elif cmd == "fetch":
        url = args[1] if len(args) > 1 else ""
        try:
            t = fetch(url)
        except Exception as e:
            print(f"抓取失败：{e}")
            return
        print(f"[{len(t)} 字符]\n")
        print(t[:3000] + ("\n…（截断）" if len(t) > 3000 else ""))

    elif cmd == "cache":
        if "--clear" in args:
            print(f"已清除 {clear_cache()} 个缓存文件")
        else:
            files = list(CACHE_DIR.rglob("*.json")) if CACHE_DIR.exists() else []
            size = sum(f.stat().st_size for f in files)
            print(f"缓存文件 {len(files)} 个，共 {size / 1024:.1f} KB")

    elif cmd == "prompt":
        print(as_prompt_block(args[1] if len(args) > 1 else ""))

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
