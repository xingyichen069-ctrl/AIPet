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
  tavily   需要服务商 key，返回摘要；额度与收费以服务商当前方案为准。
  searxng  需要可用实例，额度与访问限制取决于部署。

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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402
import proxy as PROXY  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOOLS_CFG = M.CFG.get("tools", {})
CACHE_DIR = M.ROOT / "data" / "cache"


# ---------------------------------------------------------------- 缓存

def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def _cache_get(kind: str, key: str, ttl: int):
    p = _cache_path(kind, key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - blob.get("_ts", 0) > ttl:
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

# 预算在调用下一个后端前检查；ddgs 内部会并行/重试，不能当作墙钟上限。
NO_PROXY_BUDGET = 9.0
PROXY_BUDGET = 15.0
NO_PROXY_TIMEOUT = 3
_BLOCKED_MARKS = ("dnserror", "timeout", "timed out", "connection", "ssl", "unreachable")


def _blocked(tries: list[tuple[str, Exception]]) -> bool:
    return any(any(m in (type(e).__name__ + str(e)).lower() for m in _BLOCKED_MARKS)
               for _, e in tries)


def _pick_error(tries: list[tuple[str, Exception]]) -> tuple[str, Exception]:
    # 保留有诊断意义的连接错误，不让末尾的“无结果”盖掉它。
    for backend, exc in tries:
        if _blocked([(backend, exc)]):
            return backend, exc
    return tries[-1] if tries else ("", RuntimeError("没有返回结果"))


def _search_ddgs(query: str, n: int, kind: str) -> list[dict]:
    route = PROXY.detect()  # 非法/显式代理错误不能吞掉后改成直连
    has_proxy = bool(route)
    try:
        client = PROXY.ddgs_client(route, timeout=5 if has_proxy else NO_PROXY_TIMEOUT)
    except ImportError:
        raise PROXY.ProxyError("搜索依赖不完整，请重新运行项目的准备环境入口。") from None
    kw = {"max_results": n}
    tries: list[tuple[str, Exception]] = []
    deadline = time.monotonic() + (PROXY_BUDGET if has_proxy else NO_PROXY_BUDGET)

    def run_chain(d, func_name: str, backends: list[str], kwargs: dict):
        for backend in backends:
            if time.monotonic() > deadline:
                break
            try:
                got = getattr(d, func_name)(query, backend=backend, **kwargs)
                if got:
                    return got
            except Exception as exc:
                tries.append((backend, exc))
        return None

    if has_proxy:
        news_chain = ["auto", "bing", "duckduckgo", "yahoo"]
        text_chain = ["auto", "bing", "duckduckgo", "google", "yahoo", "mojeek"]
    else:
        news_chain = ["bing", "duckduckgo", "auto"]
        text_chain = ["bing", "google", "auto", "duckduckgo", "mojeek"]
    with client as d:
        if kind == "news":
            raw = run_chain(d, "news", news_chain, kw)
            if raw is None:
                raw = run_chain(d, "text", text_chain, {**kw, "timelimit": "w"})
        else:
            raw = run_chain(d, "text", text_chain, kw)
    if raw is None:
        who, err = _pick_error(tries)
        detail = f"{who}：{PROXY.error_text(err)}" if who else "未得到结果"
        if _blocked(tries):
            mode = "当前使用代理" if has_proxy else "当前使用直连"
            raise PROXY.ProxyError(f"搜索连接失败，{mode}（{detail}）。请检查网络、代理设置和搜索服务；"
                                   "自动模式可在桌宠右键 → 高级 → 重新检测代理后重试。") from None
        raise PROXY.ProxyError(f"搜索无结果或后端不可用（{detail}）") from None
    return [{
        "title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
        "snippet": r.get("body") or r.get("description", ""),
        "source": r.get("source", ""), "date": r.get("date", ""),
    } for r in raw]


def _search_tavily(query: str, n: int, kind: str) -> dict:
    key = TOOLS_CFG.get("tavily_key")
    if not key:
        raise PROXY.ProxyError("未配置 tavily_key（data/config.json）")

    topic = "news" if kind == "news" else "general"
    payload = json.dumps({
        "query": query, "max_results": n, "topic": topic,
        "include_answer": True, "search_depth": "basic",
    }).encode("utf-8")

    response = PROXY.request(
        "https://api.tavily.com/search", method="POST", content=payload, timeout=30,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    data = json.loads(response.text)

    results = [{
        "title": x.get("title", ""),
        "url": x.get("url", ""),
        "snippet": x.get("content", ""),
        "source": "", "date": x.get("published_date", ""),
    } for x in data.get("results", [])]

    return {"answer": data.get("answer", ""), "results": results}


def _search_searxng(query: str, n: int, kind: str) -> list[dict]:
    import urllib.parse

    base = TOOLS_CFG.get("searxng_url")
    if not base:
        raise PROXY.ProxyError("未配置 searxng_url（data/config.json）")

    qs = urllib.parse.urlencode({"q": query, "format": "json",
                                 "categories": "news" if kind == "news" else "general"})
    url = f"{base.rstrip('/')}/search?{qs}"
    data = json.loads(PROXY.request(url, timeout=30).text)

    return [{
        "title": x.get("title", ""), "url": x.get("url", ""),
        "snippet": x.get("content", ""),
        "source": x.get("engine", ""), "date": x.get("publishedDate", ""),
    } for x in data.get("results", [])[:n]]


# ---------------------------------------------------------------- 对外接口

def search(query: str, max_results: int = 5, kind: str = "text",
           backend: str | None = None, use_cache: bool = True) -> dict:
    """
    搜索网络。

    返回 {"backend":..., "answer":..., "results":[...], "cached":bool}
    """
    backend = backend or TOOLS_CFG.get("search_backend", "ddgs")
    ttl = TOOLS_CFG.get("cache_ttl", {}).get(
        "news" if kind == "news" else "general", 3600)

    ck = f"{backend}|{kind}|{query}|{max_results}"
    if use_cache:
        hit = _cache_get("search", ck, ttl)
        if hit is not None:
            return {**hit, "cached": True}

    if backend == "tavily":
        data = _search_tavily(query, max_results, kind)
        out = {"backend": backend, "answer": data["answer"],
               "results": data["results"], "cached": False}
    elif backend == "searxng":
        out = {"backend": backend, "answer": "",
               "results": _search_searxng(query, max_results, kind), "cached": False}
    else:
        out = {"backend": "ddgs", "answer": "",
               "results": _search_ddgs(query, max_results, kind), "cached": False}

    if use_cache and out["results"]:
        _cache_put("search", ck, {k: v for k, v in out.items() if k != "cached"})
    return out


def fetch(url: str, use_cache: bool = True) -> str:
    """抓取网页正文，转成 Markdown。"""
    ttl = TOOLS_CFG.get("cache_ttl", {}).get("page", 86400)
    if use_cache:
        hit = _cache_get("fetch", url, ttl)
        if hit is not None:
            return hit

    route = PROXY.detect()
    text = ""
    try:
        with PROXY.ddgs_client(route, timeout=30) as d:
            r = d.extract(url, fmt="text_markdown")
        text = r.get("content", "") if isinstance(r, dict) else str(r)
    except Exception:
        # 备用提取仍使用本次选定的路由，不能偷偷改成系统代理或直连。
        import re
        raw = PROXY.request(url, proxy_url=route, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"}).text
        raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        text = re.sub(r"[ \t\r\f\v]+", " ", raw)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    if use_cache and text:
        _cache_put("fetch", url, text)
    return text


def as_prompt_block(query: str, max_results: int = 5, kind: str = "text",
                    max_chars: int = 1800) -> str:
    """给 LLM 用的紧凑格式。"""
    try:
        r = search(query, max_results, kind)
    except Exception as e:
        return f"（搜索失败：{PROXY.error_text(e)}）"

    lines = [f"## 网络搜索：{query}"]
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
            print(f"搜索失败：{PROXY.error_text(e)}")
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
            print(f"抓取失败：{PROXY.error_text(e)}")
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
