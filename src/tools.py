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
import sys
import time
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

def _search_ddgs(query: str, n: int, kind: str) -> list[dict]:
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
    try:
        import proxy as PROXY
        p = PROXY.detect()
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


def _search_tavily(query: str, n: int, kind: str) -> dict:
    import urllib.request

    key = TOOLS_CFG.get("tavily_key")
    if not key:
        raise RuntimeError("未配置 tavily_key（data/config.json）")

    topic = "news" if kind == "news" else "general"
    payload = json.dumps({
        "query": query, "max_results": n, "topic": topic,
        "include_answer": True, "search_depth": "basic",
    }).encode("utf-8")

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


def _search_searxng(query: str, n: int, kind: str) -> list[dict]:
    import urllib.parse
    import urllib.request

    base = TOOLS_CFG.get("searxng_url")
    if not base:
        raise RuntimeError("未配置 searxng_url（data/config.json）")

    qs = urllib.parse.urlencode({"q": query, "format": "json",
                                 "categories": "news" if kind == "news" else "general"})
    url = f"{base.rstrip('/')}/search?{qs}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))

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
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
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
        return f"（搜索失败：{e}）"

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
