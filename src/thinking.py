#!/usr/bin/env python3
"""
thinking.py —— 思考强度引擎

桌面上那个控制面板背后就是它。一个滑块，五档强度。

═══════════════════════════════════════════════════════════════
  它到底控制什么
═══════════════════════════════════════════════════════════════

不是装样子。换档会真实改变六个维度：

  1. 推理投入      reasoning_effort: none → low → medium → high → max
  2. 回复长度      max_tokens: 300 → 800 → 2000 → 4000 → 8000
  3. 记忆深度      memory_budget: 350 → 9000 tokens（25 倍差距）
                   ↑ 这一条最关键：档位越高，它能想起的事越多
  4. 检索行为      search: off → on_demand → eager → always
  5. 是否自检      self_check: 回答前要不要回头验一遍
  6. 说话篇幅      verbosity: 从"一句话"到"尽可能完整"

第 3 条把思考强度和记忆系统接在了一起——
调到「深究」时，它不只是想得更久，是**真的会去翻更多的旧账**。

═══════════════════════════════════════════════════════════════
  自动模式
═══════════════════════════════════════════════════════════════

默认是 auto：按问题本身判断该用哪档。
"在吗" → 省电；"帮我分析一下这个架构的权衡" → 深究。

判断规则在 data/thinking.json 的 auto_rules 里，可以自己调权重。

═══════════════════════════════════════════════════════════════

用法：
    python src/thinking.py                      # 看当前档位
    python src/thinking.py set deep             # 切档
    python src/thinking.py auto "帮我分析这个设计"  # 看自动模式会判成什么
    python src/thinking.py params               # 看解析后的具体参数
    python src/thinking.py prompt "问题"         # 输出给 LLM 的系统提示块
    python src/thinking.py list                 # 列出所有档位
"""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

THINKING_FILE = M.ROOT / "data" / "thinking.json"
LEVELS = ["frugal", "daily", "serious", "deep", "max", "thunder"]

_cache: dict = {"mtime": 0, "data": None}


# ---------------------------------------------------------------- 读写

def load(force: bool = False) -> dict:
    """带 mtime 缓存的重载——面板改完文件，agent 这边自动跟上。"""
    try:
        mtime = THINKING_FILE.stat().st_mtime
    except OSError:
        return {"current": "daily", "presets": {}, "auto_rules": {}}

    if force or mtime != _cache["mtime"] or _cache["data"] is None:
        with open(THINKING_FILE, encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = mtime
    return _cache["data"]


def save(cfg: dict) -> None:
    THINKING_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache["mtime"] = 0  # 强制下次重载


def current_level() -> str:
    return load().get("current", "daily")


def set_level(level: str) -> str:
    cfg = load()
    if level != "auto" and level not in cfg.get("presets", {}):
        raise ValueError(f"未知档位：{level}（可选：auto, {', '.join(LEVELS)}）")
    cfg["current"] = level
    save(cfg)
    return level


def preset(level: str) -> dict:
    return load().get("presets", {}).get(level, {})


# ---------------------------------------------------------------- 自动判断

def classify(query: str) -> dict:
    """给问题打分，决定该用哪一档。返回 {level, score, hits}"""
    cfg = load()
    rules = cfg.get("auto_rules", {})
    sig = rules.get("signals", {})
    th = rules.get("thresholds", {})

    q = (query or "").strip()
    score = 0.0
    hits: list[str] = []

    # 长度：问得越长，通常越复杂
    per = sig.get("length_per_20_chars", 6)
    add = (len(q) // 20) * per
    if add:
        score += add
        hits.append(f"长度+{add}")

    def scan(group: str, label: str):
        nonlocal score
        for word, weight in (sig.get(group) or {}).items():
            if word.lower() in q.lower():
                score += weight
                hits.append(f"{label}:{word}{weight:+d}")

    scan("question_words", "疑问")
    scan("depth_words", "深度")
    scan("simple_words", "简单")
    scan("code_signals", "代码")

    score = max(0.0, score)

    if score < th.get("frugal", 8):
        level = "frugal"
    elif score < th.get("daily", 25):
        level = "daily"
    elif score < th.get("serious", 50):
        level = "serious"
    elif score < th.get("deep", 75):
        level = "deep"
    else:
        level = "max"

    return {"level": level, "score": round(score, 1), "hits": hits}


# ---------------------------------------------------------------- 解析

def resolve(query: str | None = None, level: str | None = None) -> dict:
    """
    把档位解析成一组具体参数。

    返回 {level, name, params, reason}
    """
    cfg = load()
    lv = level or cfg.get("current", "daily")

    if lv == "auto":
        if query:
            r = classify(query)
            lv = r["level"]
            reason = f"自动判定 {r['score']} 分 → {lv}"
        else:
            lv = "daily"
            reason = "自动模式但没有问题内容，退回日常档"
    else:
        reason = "手动指定"

    p = preset(lv).get("params")
    if not p:
        lv = "daily"
        p = preset("daily").get("params", {})
        reason += "（档位缺失，退回日常）"

    return {
        "level": lv,
        "name": preset(lv).get("name", lv),
        "params": p,
        "reason": reason,
    }


def apply_to_memory(level: str | None = None, query: str | None = None) -> dict:
    """
    解析当前档位并返回本轮记忆参数。

    这是「思考强度」和「记忆系统」的连接点。参数属于这一轮请求，
    不写回 memory 的全局配置，避免并发请求互相污染。
    """
    r = resolve(query, level)
    p = deepcopy(r["params"])
    return {**r, "params": p, "retrieval": {
        "token_budget": p["memory_budget"],
        "max_entries": p["memory_entries"],
        "recency_floor": p.get("memory_floor", 4),
        "min_score": M.CFG["retrieval"].get("min_score", 0.05),
    }}


# ---------------------------------------------------------------- 提示块

_EFFORT_HINT = {
    "none": "直接回答，不要在脑子里绕弯。",
    "low": "快速想一下再答，不要展开推理过程。",
    "medium": "想清楚再答。可以权衡，但不要把权衡过程说出来。",
    "high": "完整推理。考虑边界情况和反例，但只输出结论和关键理由。",
    "max": "从头到尾推一遍，包括你可能问的下一句。输出结论、关键理由、边界条件。",
}

_SEARCH_HINT = {
    "off": "不要联网搜索。",
    "on_demand": "需要事实性信息且不确定时才搜，别的事不搜。",
    "eager": "遇到任何可能需要外部信息的问题都先搜一下。",
    "always": "每次都搜，宁可多搜也别凭记忆答。",
}


def system_block(query: str | None = None, level: str | None = None,
                 resolved: dict | None = None) -> str:
    """生成要注入 system prompt 的思考强度块。"""
    r = resolved or resolve(query, level)
    p = r["params"]

    lines = [
        f"## 当前思考强度：{r['name']}",
        f"（{r['reason']}）",
        "",
        f"- {_EFFORT_HINT.get(p.get('reasoning_effort', 'low'), '')}",
        f"- 回复篇幅：{p.get('verbosity', '')}",
        f"- {_SEARCH_HINT.get(p.get('search', 'on_demand'), '')}",
    ]
    if p.get("self_check"):
        lines.append("- **回答前回头验一遍**：有没有事实错误、有没有漏掉对方的真实意图。")
    if p.get("search") in ("off",):
        lines.append("- 不确定的事就说不知道，不要编。")

    return "\n".join(lines)


def context(query: str, level: str | None = None) -> str:
    """思考强度块 + 记忆块，一次给全。"""
    r = apply_to_memory(level, query)
    return system_block(query, resolved=r) + "\n\n" + M.build_context(
        query, retrieval=r["retrieval"])


# ---------------------------------------------------------------- CLI

def main() -> None:
    args = sys.argv[1:]
    if not args:
        r = resolve()
        cfg = load()
        print(f"当前档位：{cfg.get('current')}"
              + (f"（解析为 {r['name']}）" if cfg.get("current") == "auto" else ""))
        print(f"记忆预算：{r['params'].get('memory_budget')} tokens / "
              f"{r['params'].get('memory_entries')} 条")
        return

    cmd = args[0]

    if cmd == "set":
        print(f"已切换到：{set_level(args[1] if len(args) > 1 else 'auto')}")

    elif cmd == "auto":
        q = args[1] if len(args) > 1 else ""
        r = classify(q)
        print(json.dumps({**r, "档位名": preset(r["level"]).get("name")},
                         ensure_ascii=False, indent=2))

    elif cmd == "params":
        print(json.dumps(resolve(args[1] if len(args) > 1 else None,
                                 args[2] if len(args) > 2 else None),
                         ensure_ascii=False, indent=2))

    elif cmd == "prompt":
        print(system_block(args[1] if len(args) > 1 else None))

    elif cmd == "context":
        print(context(args[1] if len(args) > 1 else ""))

    elif cmd == "list":
        for lv in LEVELS:
            p = preset(lv)
            pa = p.get("params", {})
            print(f"{p.get('icon','')} {lv:8s} {p.get('name',''):4s} "
                  f"{p.get('summary',''):12s} "
                  f"记忆{pa.get('memory_budget',0):>5} / "
                  f"推理{pa.get('reasoning_effort',''):7s} / "
                  f"搜索{pa.get('search','')}")
        print(f"\n当前：{current_level()}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
