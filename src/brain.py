#!/usr/bin/env python3
"""
brain.py —— 独立大脑（插槽 B）

接上它之后，思考强度面板上的参数就**全部真正生效**了：

    model          ✅ 真的换模型
    max_tokens     ✅ 真的限制输出长度
    reasoning_effort ✅ 真的控制思考强度
    memory_budget  ✅ 真的控制检索深度
    temperature    ⚠️ 见下面的坑

═══════════════════════════════════════════════════════════════
  两个 DeepSeek 的坑（官方文档明确写了，不是我的推测）
═══════════════════════════════════════════════════════════════

坑一：思考模式下 temperature 无效

    "思考模式不支持 temperature、presence_penalty、frequency_penalty
     参数。请注意，为了兼容已有软件，设置参数不会报错，但也不会生效。"

    → 所以你的档位里 temperature 只在「省电」档（关闭思考）真正起作用。
      其余档位它会被静默忽略。这是 DeepSeek 的设计，不是 bug。

坑二：思考强度是漏斗形的

    请求传入        实际生效
    ─────────      ────────
    minimal    →   low
    low        →   low
    medium     →   high      ← 注意
    high       →   high
    xhigh      →   high
    max        →   max
    ultra      →   max

    → 五档预设里「认真(medium)」和「深究(high)」会落到同一档。
      要真正拉开差距，得改档位的 effort 值，或者接受这个现实。

另外：思考模式下 top_p 下限被抬到 0.95；非思考模式恒为 1.0。

═══════════════════════════════════════════════════════════════
  配置
═══════════════════════════════════════════════════════════════

API key 放在 data/secrets.json：

    { "deepseek_api_key": "sk-xxxxxxxx" }

也可以走环境变量 DEEPSEEK_API_KEY（优先级更高）。
secrets.json 不在备份范围内，这是故意的。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════

    python src/brain.py check              # 验证 key 能用
    python src/brain.py ask "在吗"          # 问一句
    python src/brain.py ask "帮我分析…" --level deep
    python src/brain.py chat               # 命令行对话
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M          # noqa: E402
import thinking as T        # noqa: E402

try:
    import local_tools as LT
    TOOLSPECS, HAS_TOOLS = LT.SPECS, True
except ImportError:
    LT, TOOLSPECS, HAS_TOOLS = None, [], False

# ★ 原来定的是 5。实测撞过：让她看一张图，她先猜错目录、列一次目录
#   找到文件、再读图，三步就到顶了 —— 结果话都没说出来就断了。
#   提到 8：够「找文件 → 读图 → 记一笔」这类多步操作，又不至于打转太久。
#   QQ 那边的真正闸门是 REPLY_BUDGET_S（240 秒），不是这个数。
MAX_TOOL_ROUNDS = 8         # 防止工具调用打转


if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SECRETS = M.ROOT / "data" / "secrets.json"
DEFAULT_BASE = "https://api.deepseek.com"

# 档位 effort → DeepSeek 实际接受的 reasoning_effort。
# 官方映射表见文件头。medium 会被映射到 high，这里直接写清楚，
# 免得你以为 medium 真的比 low 强一档。
EFFORT_MAP = {
    "none": None,       # None = 关闭思考模式
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
}

# 注意：官方文档给的 extra_body={"thinking": ...} 是 OpenAI **SDK** 的写法。
# SDK 会把它展开到请求体顶层。直连 REST API 时必须直接写顶层字段，
# 否则这个参数会被静默忽略 —— 后果是思考模式保持默认开启（effort=high），
# max_tokens 小的档位会把额度全烧在思维链上，正文返回空。
# 这个坑我踩过，记在这里。


def api_model(name: str) -> str:
    """
    把配置里的模型名归一化成 API 认的形式。

    thinking.json 里写的是 Cherry Studio 的格式（`deepseek::deepseek-flash`），
    因为它同时要给 MCP / agent 那条路用。但直连 DeepSeek API 时
    只认 `deepseek-flash`——带 `::` 会 400。

    两边的格式都接受，在这里统一。
    """
    name = (name or "deepseek-flash").strip()
    if "::" in name:
        name = name.split("::", 1)[1]
    # 旧名会被路由到 V4.1-Flash，但按官方建议用新名
    if name in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        name = "deepseek-flash"
    return name


# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

def load_secrets() -> dict:
    d = {}
    if SECRETS.exists():
        try:
            d = json.loads(SECRETS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return d


def api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY") or load_secrets().get("deepseek_api_key", "")


def base_url() -> str:
    return (os.environ.get("DEEPSEEK_BASE_URL")
            or load_secrets().get("deepseek_base_url")
            or DEFAULT_BASE)


def save_key(key: str) -> None:
    d = load_secrets()
    d["deepseek_api_key"] = key.strip()
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:                                   # 尽量收紧权限（Windows 上不一定生效）
        os.chmod(SECRETS, 0o600)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════
#  prompt 组装
# ═══════════════════════════════════════════════════════════════

def persona_text() -> str:
    """人格文本。剥离逻辑在 memory 里，桌宠和 MCP 共用同一份实现。"""
    return M.persona_text()


def build_system(query: str, level: str | None = None) -> tuple[str, dict]:
    """
    组装 system prompt，返回 (文本, 解析后的档位信息)。

    system = 人格设定 + 思考强度指令 + 记忆（关系状态/相关回忆/用户档案）
    """
    r = T.apply_to_memory(level, query)      # 这一步让 memory 用上当前档位的预算
    body = T.system_block(query, r["level"]) + "\n\n" + M.build_context(query)
    from companion import Store, task_description
    if (M.ROOT / "data" / "companion.sqlite3").exists():
        agreements = Store(M.ROOT).tasks()
        if agreements:
            body += "\n\n## 已保存的约定与陪伴\n" + "\n".join(task_description(t) for t in agreements[:20])
    return f"{persona_text()}\n\n---\n\n{body}", r


def build_payload(query: str, history: list[dict] | None = None,
                  level: str | None = None, stream: bool = True) -> tuple[dict, dict]:
    system, r = build_system(query, level)
    p = r["params"]

    messages = [{"role": "system", "content": system}]
    for h in (history or [])[-16:]:          # 只带最近 12 轮，够了
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": query})

    effort = EFFORT_MAP.get(p.get("reasoning_effort", "low"), "high")

    # 工具按档位裁剪：
    #   get_time / get_system / recall / remember 永远可用（都是本地调用，零成本）
    #   web_search 只在档位的 search != off 时给（省电档不给，省时间）
    tools = []
    if HAS_TOOLS:
        tools = [t for t in TOOLSPECS
                 if t["function"]["name"] != "web_search"
                 or p.get("search", "on_demand") != "off"]

    payload = {
        "model": api_model(p.get("model", "deepseek-flash")),
        "messages": messages,
        "max_tokens": p.get("max_tokens", 800),
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools

    if effort is None:
        # 关闭思考模式 —— 这时 temperature 才真正生效
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = p.get("temperature", 0.75)
    else:
        # 思考模式：temperature 会被静默忽略，不传更诚实
        payload["reasoning_effort"] = effort
        payload["thinking"] = {"type": "enabled"}

    return payload, r


# ═══════════════════════════════════════════════════════════════
#  调用
# ═══════════════════════════════════════════════════════════════

def _request(payload: dict, key: str):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url().rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=30)


def _explain(e: Exception) -> str:
    """把 HTTP 错误翻译成人话。"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read().decode("utf-8", errors="replace"))
            msg = detail.get("error", {}).get("message") or detail.get("message") or str(detail)
        except Exception:
            msg = e.reason or ""
        hints = {
            401: "API key 无效或已失效。检查 data/secrets.json。",
            402: "余额不足。去 platform.deepseek.com 充值。",
            429: "触发限流。等一会儿，或降低调用频率。",
            400: f"请求被拒：{msg}",
        }
        return hints.get(e.code, f"HTTP {e.code}：{msg}")
    if isinstance(e, urllib.error.URLError):
        return f"网络不通：{e.reason}。国内直连 api.deepseek.com 通常没问题，检查防火墙。"
    return f"{type(e).__name__}: {e}"


def stream(query: str, history: list[dict] | None = None,
           level: str | None = None, cancelled=None,
           system: str | None = None, max_tokens: int | None = None,
           block_tools: set[str] | None = None):
    """
    流式生成。产出 (类型, 文本)：
        ("level", 档位信息)  —— 只产一次，最先
        ("reasoning", 思维链片段)
        ("content", 正文片段)
        ("error", 错误说明)
    """
    key = api_key()
    if not key:
        yield ("error", "没有 API key。运行：python src/brain.py setkey sk-xxxx")
        return

    payload, r = build_payload(query, history, level, stream=True)
    # QQ 那条路的覆盖口：群聊要自带更短的 system，而且必须在**调用之前**
    # 就把 max_tokens 压下来 —— 被动回复只有 5 分钟，生成完再截断时间已经花掉了。
    if system is not None:
        payload["messages"][0] = {"role": "system", "content": system}
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    # ★ 工具黑名单。QQ 那条路用它挡住 see_image —— 那个工具会把整个文件
    #   发到校外服务器，不能让群里的人靠一句话就把谁的文件送出去。
    #   在**调用之前**摘掉，不是调用之后拦：模型看不见这个工具，
    #   就不会写出针对它的调用，也不会因为"我明明有这个工具"而反复试。
    if block_tools and payload.get("tools"):
        payload["tools"] = [t for t in payload["tools"]
                            if t["function"]["name"] not in block_tools]
        if not payload["tools"]:
            payload.pop("tools", None)

    yield ("level", r)

    messages = list(payload["messages"])
    text_total: list[str] = []
    tool_rounds = 0

    while True:
        if cancelled and cancelled():
            return
        body = {**payload, "messages": messages}
        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        calls: dict[int, dict] = {}          # index → {id, name, args}

        try:
            resp = _request(body, key)
        except Exception as e:
            yield ("error", _explain(e))
            return

        try:
            with resp:
                for raw in resp:
                    if cancelled and cancelled():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    rc = delta.get("reasoning_content")
                    if rc:
                        reasoning_buf.append(rc)
                        yield ("reasoning", rc)

                    c = delta.get("content")
                    if c:
                        content_buf.append(c)
                        text_total.append(c)
                        yield ("content", c)

                    # 流式下 tool_calls 是分片到达的，按 index 拼
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        slot = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except Exception as e:
            yield ("error", _explain(e))
            return

        if not calls:
            break                            # 没有工具调用 → 这就是最终回答

        tool_rounds += 1
        if tool_rounds > MAX_TOOL_ROUNDS:
            yield ("error", f"工具调用超过 {MAX_TOOL_ROUNDS} 轮，停止")
            return

        # 回传 assistant 消息。**必须带 reasoning_content**——
        # 官方明确：带 tools 的请求，后续所有请求都要完整回传，
        # 即使该轮没实际调用工具，否则 400。
        messages.append({
            "role": "assistant",
            "content": "".join(content_buf),
            "reasoning_content": "".join(reasoning_buf),
            "tool_calls": [
                {"id": s["id"] or f"call_{i}", "type": "function",
                 "function": {"name": s["name"], "arguments": s["args"] or "{}"}}
                for i, s in sorted(calls.items())
            ],
        })

        for i, s in sorted(calls.items()):
            if cancelled and cancelled():
                return
            try:
                args = json.loads(s["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
            yield ("tool", f"⚙ {s['name']}({shown})")

            result = LT.call(s["name"], args) if LT else "工具模块未加载"
            first = result.strip().splitlines()[0] if result.strip() else "(空)"
            yield ("tool", f"  → {first[:120]}")

            messages.append({
                "role": "tool",
                "tool_call_id": s["id"] or f"call_{i}",
                "content": result,
            })

    # 正文空但思维链很长 → max_tokens 被思考烧光了。别静默返回空。
    if not text_total and reasoning_buf:
        yield ("error", f"思考用光了全部 {payload['max_tokens']} token 的额度，"
                        f"没来得及说话（思维链 {len(''.join(reasoning_buf))} 字）。"
                        f"调大该档位的 max_tokens，或降低 reasoning_effort。")


def ask(query: str, history: list[dict] | None = None,
        level: str | None = None) -> tuple[str, str, dict]:
    """
    把流式结果收成整块。返回 (正文, 思维链, 档位信息)。

    走 stream() 而不是单独发一次非流式请求——这样工具调用、
    错误处理、额度诊断全都只有一份实现，不会两边行为不一致。
    """
    text: list[str] = []
    reasoning: list[str] = []
    r: dict = {}
    tools_used: list[str] = []

    for kind, val in stream(query, history, level):
        if kind == "content":
            text.append(val)
        elif kind == "reasoning":
            reasoning.append(val)
        elif kind == "level":
            r = val
        elif kind == "tool":
            tools_used.append(val)
        elif kind == "error":
            r = {**r, "error": val}

    if tools_used:
        r = {**r, "tools": tools_used}
    return "".join(text), "".join(reasoning), r


def ask_with_system(query: str, system: str,
                    history: list[dict] | None = None,
                    level: str | None = None,
                    max_tokens: int | None = None,
                    block_tools: set[str] | None = None) -> tuple[str, str, dict]:
    """
    自带 system 地问一次。返回 (正文, 思维链, 档位信息)。

    给 QQ 那条路用的 —— 那边的 system 和桌宠不一样（更短、带平台限制说明）。
    和 ask() 一样走 stream()，这样工具调用、停止回调、错误处理只有一份实现。
    """
    text: list[str] = []
    reasoning: list[str] = []
    r: dict = {}
    tools_used: list[str] = []

    for kind, val in stream(query, history, level,
                            system=system, max_tokens=max_tokens,
                            block_tools=block_tools):
        if kind == "content":
            text.append(val)
        elif kind == "reasoning":
            reasoning.append(val)
        elif kind == "level":
            r = val
        elif kind == "tool":
            tools_used.append(val)
        elif kind == "error":
            r = {**r, "error": val}

    if tools_used:
        r = {**r, "tools": tools_used}
    return "".join(text), "".join(reasoning), r


def check() -> bool:
    key = api_key()
    if not key:
        print("✗ 没有 API key")
        print(f"  放到 {SECRETS}：{{\"deepseek_api_key\": \"sk-xxxx\"}}")
        print("  或运行：python src/brain.py setkey sk-xxxx")
        return False

    print(f"✓ 读到 key：{key[:8]}…{key[-4:]}（共 {len(key)} 字符）")
    print(f"  base_url：{base_url()}")
    print("  正在测试连通性…")

    text, reasoning, r = ask("回复「通」一个字，不要标点。", level="frugal")
    if r.get("error"):
        print(f"✗ {r['error']}")
        return False
    print(f"✓ 模型回复：{text.strip()[:40]}")
    print(f"  实际模型：{r['params'].get('model')} · 档位 {r['name']}")
    return True


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]

    if cmd == "setkey":
        if len(args) < 2:
            print("用法：python src/brain.py setkey sk-xxxx")
            sys.exit(1)
        save_key(args[1])
        print(f"已写入 {SECRETS}")
        check()

    elif cmd == "check":
        sys.exit(0 if check() else 1)

    elif cmd == "ask":
        q = args[1] if len(args) > 1 else ""
        lv = None
        if "--level" in args:
            i = args.index("--level")
            if i + 1 < len(args):
                lv = args[i + 1]

        show_reasoning = "--show-reasoning" in args
        print(f"你：{q}\n")
        got_any = False
        for kind, val in stream(q, level=lv):
            if kind == "level":
                print(f"[档位 {val['name']} · {val['params'].get('model')} · "
                      f"effort={val['params'].get('reasoning_effort')} → "
                      f"{EFFORT_MAP.get(val['params'].get('reasoning_effort')) or '关闭思考'} · "
                      f"记忆{val['params'].get('memory_budget')}tok]", file=sys.stderr)
            elif kind == "reasoning":
                if show_reasoning:
                    if not got_any:
                        print("── 思维链 ──", file=sys.stderr)
                    print(val, end="", file=sys.stderr, flush=True)
            elif kind == "tool":
                print(f"\n{val}", file=sys.stderr, flush=True)
            elif kind == "content":
                if not got_any:
                    print("小日和：", end="")
                    got_any = True
                print(val, end="", flush=True)
            elif kind == "error":
                print(f"\n✗ {val}")
        print()

    elif cmd == "chat":
        print("命令行对话。输入 exit 退出，/level deep 换档。\n")
        history: list[dict] = []
        while True:
            try:
                q = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q or q == "exit":
                break
            if q.startswith("/level "):
                T.set_level(q.split(None, 1)[1].strip())
                print("已换档\n")
                continue

            buf = []
            for kind, val in stream(q, history):
                if kind == "content":
                    print(val, end="", flush=True)
                    buf.append(val)
                elif kind == "error":
                    print(f"\n✗ {val}")
            reply = "".join(buf)
            print("\n")
            if reply:
                history += [{"role": "user", "content": q},
                            {"role": "assistant", "content": reply}]
                # 落盘，和图形界面的行为保持一致。
                # 重要度给 2、衰减 normal —— 日常对话本来就该慢慢淡出，
                # 真正重要的事由睡前的整理环节提升上去。
                try:
                    M.add(f"用户说：{q}", 2, [], "", "normal", "chat")
                except Exception:
                    pass

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
