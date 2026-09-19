#!/usr/bin/env python3
"""
mcp_server.py —— 把 AIPet 挂到 Cherry Studio 的 agent 上

═══════════════════════════════════════════════════════════════
  这解决了什么问题
═══════════════════════════════════════════════════════════════

在此之前：
    面板写 thinking.json ✅
    thinking.py 能解析它  ✅
    但没有任何"大脑"在读它 ❌

这个 MCP server 就是那个缺失的接口。挂上之后，
agent 每轮对话可以调用 aipet_context(问题)，一次拿到：

    · 当前的思考强度档位（面板上你选的那个）
    · 关系状态（亲密度、未兑现的承诺、多久没说话）
    · 相关回忆（按当前档位的记忆预算检索出来的）
    · 用户档案（PROFILE.md 的事实分区）

于是面板上的滑块就真的接进了对话流程。

═══════════════════════════════════════════════════════════════
  零依赖
═══════════════════════════════════════════════════════════════

没有用官方 mcp 包，直接实现 MCP 的 stdio 协议
（JSON-RPC 2.0 + 换行分隔）。理由：

  · 和 AIPet 其余部分一致——不引入外部依赖
  · 避开 Python 3.14 的第三方兼容问题
  · 需要的只是 initialize / tools/list / tools/call 三个方法

调试信息一律走 stderr。stdout 只允许出现 JSON-RPC 报文，
否则会破坏协议——这是写 MCP server 最常见的翻车点。

═══════════════════════════════════════════════════════════════
  手动测试
═══════════════════════════════════════════════════════════════

    python src/mcp_server.py --selftest     # 自检，不需要客户端
    python src/mcp_server.py                # 正常模式，等 stdin
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROTOCOL_VERSIONS = ["2025-06-18", "2024-11-05"]
DEFAULT_PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "aipet", "version": "0.5.0"}

_log = lambda *a: print(*a, file=sys.stderr, flush=True)  # noqa: E731


def _force_utf8() -> None:
    """
    把三个标准流强制切到 UTF-8。**必须在读第一行之前调用。**

    为什么非有不可：Windows 中文环境的 locale 是 cp936，Python 在没开
    UTF-8 模式时，stdin 就用 cp936 解码。而客户端（Cherry Studio）发过来
    的是 UTF-8 字节，拿 GBK 解一遍就坏：

      · 解得开的字节 → 变成别的字（静默乱码）
      · 解不开的字节 → 变成孤立代理项（lone surrogate）

    第二种最要命：代理项写文件时抛
        UnicodeEncodeError: surrogates not allowed

    所以症状是分层的——
        aipet_remember  写中文直接崩
        aipet_recall    不崩，但查询词是乱码，检索悄悄失效
        aipet_context   同上，你根本看不出来

    stdout 同理：回给客户端的中文也会变成 GBK/UTF-8 混合的缝合字节。

    errors="replace" 是保险——真收到坏字节就换成 U+FFFD，
    而不是让整个 server 崩掉。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  工具实现
# ═══════════════════════════════════════════════════════════════

def _t_context(query: str) -> str:
    """
    ⭐ 主力工具：一次拿全**人格 + 思考强度 + 记忆 + 档案**。

    ★ 人格是这里加进去的，不在 T.context() 里。因为桌宠那条路
      （brain.build_system）已经自己前置了一份 persona_text()，
      再塞进 thinking.context() 会重复两遍。
      MCP 这条路没有别的地方给她人格，所以补在这儿。

    加这个的直接原因：提示词里一直写着"aipet_context 会返回你的人格设定"，
    但实际不返回 —— 只靠系统提示词里那几句摘要，她读不到完整的 SOUL。
    """
    import thinking as T
    parts = [T.context(query)]
    p = _t_persona()
    if p:
        parts.insert(0, p)
    return "\n\n---\n\n".join(parts)


def _t_recall(query: str, limit: int | None = None) -> str:
    import memory as M
    import thinking as T
    T.apply_to_memory(query)          # 让检索深度跟随面板档位
    hits = M.retrieve(query, top_k=limit)
    if not hits:
        return "（没有相关记忆）"
    out = []
    for e in hits:
        when = M.parse_ts(e["ts"]).strftime("%m月%d日")
        mark = "★" * e.get("importance", 3)
        tag = f" #{' #'.join(e['tags'])}" if e.get("tags") else ""
        out.append(f"- [{when}] {e['text']} {mark}{tag}")
    return "\n".join(out)


def _t_remember(text: str, importance: int = 3, tags: str = "",
                emotion: str = "", decay: str = "normal",
                speaker: str = "owner") -> str:
    import memory as M
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    e = M.add(text, importance, tag_list, emotion, decay, speaker=speaker)
    if e is None:
        return "未记录（命中隐私过滤，或内容为空）"
    if e.get("speaker") == "guest":
        return (f"已记录 {e['id']}（标为群友记忆，会自动降权并快速淡出）：{e['text']}")
    return f"已记录 {e['id']}：{e['text']}"


def _t_thinking_get() -> str:
    import thinking as T
    cfg = T.load()
    cur = cfg.get("current", "daily")
    r = T.resolve()
    p = r["params"]
    lines = [f"当前档位：{cur}" + (f"（解析为 {r['name']}）" if cur == "auto" else ""),
             "",
             f"- 记忆预算：{p.get('memory_budget')} tokens / {p.get('memory_entries')} 条",
             f"- 推理投入：{p.get('reasoning_effort')}",
             f"- 回复长度上限：{p.get('max_tokens')} tokens",
             f"- 温度：{p.get('temperature')}",
             f"- 联网：{p.get('search')}",
             f"- 自检：{'开' if p.get('self_check') else '关'}",
             f"- 篇幅要求：{p.get('verbosity')}"]
    return "\n".join(lines)


def _t_thinking_set(level: str) -> str:
    import thinking as T
    try:
        T.set_level(level)
    except ValueError as e:
        return f"失败：{e}"
    cfg = T.load()
    nxt = cfg.get("current")
    if nxt == "auto":
        return "已切到自动档（按问题难度自己挑）"
    pr = T.preset(nxt)
    return f"已切到「{pr.get('name')}」——{pr.get('feels_like', '')}"


def _t_persona() -> str:
    """
    人格全文。

    ★ 走 M.persona_text()，不要自己 read_text —— 它会剥掉给用户看的
    编辑说明（<!-- -->）。直接读原文的话，agent 会读到
    "这是你最该动手改的文件"，然后把自己理解成一份待编辑的文档。
    """
    import memory as M
    return M.persona_text() or "（人格文件缺失）"


def _t_profile() -> str:
    import memory as M
    return M._profile_facts()


def _t_search(query: str, kind: str = "text", max_results: int = 5) -> str:
    import tools
    return tools.as_prompt_block(query, max_results, kind)


def _t_mood(action: str = "get", key: str = "", hours: float | None = None,
            why: str = "", force: bool = False) -> str:
    """
    心理点：她自己挑一个状态停一会儿，到点自动散。

    "到点自动散"不是靠定时器，是每次读的时候拿当前时间跟 until 比。
    所以关掉电脑再开，它照样是散的——时间戳是绝对的。
    """
    import memory as M
    import mood as MD
    action = (action or "get").lower()

    if action == "list":
        lines = ["可选的心理点：", ""]
        for k, v in MD.catalog().items():
            lines.append(f"· {k}（默认 {v['hours']} 小时）—— {v.get('feel','')}")
        lines += ["", "挑一个用 action=set。挑之前先看 aipet_mood(action=get) "
                      "确认现在没停着别的。"]
        return "\n".join(lines)

    if action == "clear":
        return "散了。" if MD.clear("她自己收的") else "本来就没停在哪。"

    if action == "set":
        if not key:
            return "要给她一个 key。先 action=list 看有哪些。"
        try:
            e = MD.set_mood(key, hours, why, force=force)
        except ValueError as exc:
            return f"没设成：{exc}"
        return (f"已停在「{e['key']}」，"
                f"{M.parse_ts(e['until']):%H:%M} 左右自己散。")

    # get（默认）
    return MD.status()


def _t_stats() -> str:
    import memory as M
    import thinking as T
    s = M.stats()
    r = T.resolve()
    return (f"记忆条目：{s['条目数']}（{s['总 token']} tokens）\n"
            f"按衰减类别：{s['按衰减类别']}\n"
            f"压缩摘要：{s['摘要文件']} 个 · 归档：{s['归档文件']} 个\n"
            f"当前思考档位：{r['name']}（记忆预算 {r['params'].get('memory_budget')}）")


TOOLS = [
    {
        "name": "aipet_context",
        "description": (
            "【首选】一次拿到 AIPet 的全部上下文：**完整人格文档（SOUL + BOUNDARIES）**、"
            "当前思考强度档位、关系状态（亲密度/未兑现的承诺/距上次互动）、"
            "按当前档位检索出的相关回忆、以及用户档案。"
            "回复用户前调用它。不调用的话你既没有记忆也没有性格。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "用户当前的问题或对话内容"}},
            "required": ["query"],
        },
    },
    {
        "name": "aipet_recall",
        "description": "只检索记忆库，返回相关回忆。想单独看它记得什么时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "limit": {"type": "integer", "description": "最多返回几条"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "aipet_remember",
        "description": (
            "往记忆库写一条。适合记录用户提到的事实、承诺、偏好、忌讳。"
            "importance 1-5；decay 用 permanent（永不遗忘，如生日）、"
            "slow（长期，如目标偏好）、normal（日常事件）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记的内容，写成陈述句"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5,
                               "description": "重要度 1-5，默认 3"},
                "tags": {"type": "string", "description": "标签，逗号分隔"},
                "emotion": {"type": "string", "description": "情绪标签，可留空"},
                "decay": {"type": "string", "enum": ["permanent", "slow", "normal"],
                          "description": "衰减类别，默认 normal"},
                "speaker": {
                    "type": "string", "enum": ["owner", "guest"],
                    "description":
                        "谁说的。**默认 owner**（你的主人本人）。"
                        "★ 在群聊里，如果是群里其他人说的，必须传 guest。"
                        "把别人的事记成主人的，不是权重低的问题——是错的，"
                        "它会跟主人的真实信息打架。"
                        "拿不准是谁说的，一律传 guest。",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "aipet_thinking_get",
        "description": "查看当前思考强度档位和它对应的具体参数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aipet_thinking_set",
        "description": (
            "切换思考强度档位。可选：auto（自动）、frugal（省电）、daily（日常）、"
            "serious（认真）、deep（深究）、max（极限）。"
            "用户说「想仔细点」「别想太久」这类话时可以调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"level": {"type": "string",
                                     "enum": ["auto", "frugal", "daily",
                                              "serious", "deep", "max"]}},
            "required": ["level"],
        },
    },
    {
        "name": "aipet_persona",
        "description": "读取角色的人格设定和行为边界。需要拿捏说话风格时调用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aipet_profile",
        "description": "读取用户档案（已内化的长期事实，不含时间线）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aipet_search",
        "description": "联网搜索。需要时效性信息时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["text", "news"],
                         "description": "默认 text"},
                "max_results": {"type": "integer", "description": "默认 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "aipet_mood",
        "description": (
            "小日和的「心理点」——她自己挑一个状态停一会儿，到点自动散"
            "（不用手动恢复，系统时间一到就淡回平常）。"
            "这是**外套不是内核**：只影响默认语气，不影响判断力，"
            "该冷该硬该较真时随时翻得回去。\n"
            "action=get 看现在停在哪儿；action=list 看有哪些可选；"
            "action=set 挑一个（要 key，可选 hours 和 why）；action=clear 提前收。\n"
            "★ 由她自己决定要不要挑。被什么真的触到了才挑，"
            "不要每轮都换，那样就不像状态了。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["get", "list", "set", "clear"],
                           "description": "默认 get"},
                "key": {"type": "string",
                        "description": "状态名，如 起雾/软毛/低电量/手痒/较真/偏心/走神"},
                "hours": {"type": "number",
                          "description": "停多久，默认按该状态的常规时长（1-8 小时）"},
                "why": {"type": "string",
                        "description": "由头——什么让你想停在这儿。写一句，自己以后看得见"},
                "force": {
                    "type": "boolean",
                    "description":
                        "跳过冷却和每日上限。**只在主人明确开口要求换的时候用**"
                        "（他说了'可爱一点''正常点''收一收'这类话）。"
                        "冷却拦的是你自己频频换，不是拦他——他有权。",
                },
            },
        },
    },
    {
        "name": "aipet_stats",
        "description": "记忆库和思考档位的统计信息。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

DISPATCH = {
    "aipet_context": lambda a: _t_context(a.get("query", "")),
    "aipet_recall": lambda a: _t_recall(a.get("query", ""), a.get("limit")),
    "aipet_remember": lambda a: _t_remember(
        a.get("text", ""), int(a.get("importance", 3)), a.get("tags", ""),
        a.get("emotion", ""), a.get("decay", "normal"),
        a.get("speaker", "owner")),
    "aipet_thinking_get": lambda a: _t_thinking_get(),
    "aipet_thinking_set": lambda a: _t_thinking_set(a.get("level", "auto")),
    "aipet_persona": lambda a: _t_persona(),
    "aipet_profile": lambda a: _t_profile(),
    "aipet_search": lambda a: _t_search(
        a.get("query", ""), a.get("kind", "text"), int(a.get("max_results", 5))),
    "aipet_mood": lambda a: _t_mood(
        a.get("action", "get"), a.get("key", ""), a.get("hours"),
        a.get("why", ""), bool(a.get("force", False))),
    "aipet_stats": lambda a: _t_stats(),
}


# ═══════════════════════════════════════════════════════════════
#  JSON-RPC / MCP 协议
# ═══════════════════════════════════════════════════════════════

def _result(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    # 通知类：没有 id，不需要回复
    if method == "notifications/initialized" or method == "initialized":
        return None
    if method == "notifications/cancelled":
        return None

    if method == "initialize":
        want = params.get("protocolVersion")
        ver = want if want in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return _result(rid, {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "ping":
        return _result(rid, {})

    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            return _error(rid, -32602, f"未知工具：{name}")
        try:
            text = fn(args)
            return _result(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            _log(traceback.format_exc())
            # 工具执行失败用 isError 返回，而不是协议级错误——
            # 这样 agent 能看见失败原因，而不是整个会话崩掉
            return _result(rid, {
                "content": [{"type": "text", "text": f"执行失败：{e}"}],
                "isError": True,
            })

    if rid is None:
        return None
    return _error(rid, -32601, f"不支持的方法：{method}")


def serve() -> None:
    _force_utf8()
    _log("[aipet-mcp] 已启动，等待 stdio 报文")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"[aipet-mcp] 报文解析失败：{e}")
            continue

        try:
            resp = handle(msg)
        except Exception:
            _log(traceback.format_exc())
            resp = _error(msg.get("id"), -32603, "服务端内部错误")

        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    """不需要客户端，直接把协议走一遍。"""
    _force_utf8()

    fails = 0

    def check(label: str, cond: bool, extra: str = ""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    print("MCP 自检\n")

    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "clientInfo": {"name": "test", "version": "0"}}})
    check("initialize 握手", r and "result" in r)
    check("返回 serverInfo", r["result"]["serverInfo"]["name"] == "aipet")
    check("声明 tools 能力", "tools" in r["result"]["capabilities"])

    r = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    check("tools/list", len(names) == len(TOOLS), f"{len(names)} 个工具")

    r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "aipet_context",
                           "arguments": {"query": "报告 考研"}}})
    txt = r["result"]["content"][0]["text"]
    check("aipet_context 可用", "相关回忆" in txt)
    check("含关系状态", "关系状态" in txt)
    check("跟随面板档位", "思考强度" in txt)

    r = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "aipet_thinking_get", "arguments": {}}})
    check("aipet_thinking_get 可用", "当前档位" in r["result"]["content"][0]["text"])

    r = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "aipet_persona", "arguments": {}}})
    check("aipet_persona 读到 SOUL", "小日和" in r["result"]["content"][0]["text"])

    r = handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "aipet_search",
                           "arguments": {"query": "test", "max_results": 1}}})
    check("aipet_search 有降级（未装 ddgs 也不崩）",
          "content" in r["result"])

    r = handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "不存在的工具", "arguments": {}}})
    check("未知工具报错", "error" in r)

    check("通知类不回复",
          handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    serve()
