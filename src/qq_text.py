#!/usr/bin/env python3
"""
qq_text.py —— QQ 平台的内容限制兜底

═══════════════════════════════════════════════════════════════
  为什么要有这个文件
═══════════════════════════════════════════════════════════════

QQ 开放平台有几条硬限制，违反了整条消息发不出去：

    40054010  不允许发送 URL        整条被拒
    40054007  消息长度超限          轻则报错，重则静默截断
    （Markdown 不报错，只是显示成一堆星号，很难看）

这些规则已经写进提示词里告诉模型了。**但提示词是"请求"，不是"保证"。**
模型有概率不听话 —— 尤其在被要求给个来源、或者解释一段代码的时候。

所以这里有第二道闸门：出站前机械地洗一遍。

★ sanitize() 在 qq_bot._post() 里被调用，不在 bridge 里。
  这是刻意的 —— 它是**最后一米**。就算提示词组装出了 bug、
  某个新工具忘了过滤、模型编了个链接出来，也从这里漏不出去。

═══════════════════════════════════════════════════════════════
  关于"2000 字节"
═══════════════════════════════════════════════════════════════

这个数字**不是官方的**。QQ 的文档只在错误码表里写了
"40054007 消息长度超限"，从头到尾没公布过具体阈值。
2000 是社区反复试出来的经验值。

拿经验值当契约是不负责任的，所以这里按 **1000 字节**保守走
（约 330 个中文字）。代价是长回复会被截短一点，
收益是不会踩那条看不见的线 —— 静默截断比报错更难查。

═══ 用法 ═══
    python src/qq_text.py              # 自检
    python src/qq_text.py "要洗的文本"   # 洗一遍看结果
"""

from __future__ import annotations

import re
import sys

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 保守值。官方只给了错误码，没给数字。见文件头。
QQ_SAFE_BYTES = 1000


# ── URL ─────────────────────────────────────────────────────
# 三层，从确定到模糊。
URL_RE = re.compile(r"https?://\S+|ftp://\S+", re.I)
WWW_RE = re.compile(r"\bwww\.[a-z0-9-]+(\.[a-z0-9-]+)+\S*", re.I)
# 裸域名。宁可误伤 —— 40054010 是整条消息被拒，代价不对称。
# （"read.me" 这种会被误伤，无所谓；"v1.2.0" 不会，因为 .2 不在 TLD 表里）
TLDS = ("com", "cn", "net", "org", "io", "dev", "me", "top", "xyz",
        "info", "cc", "tv", "edu", "gov", "co", "app", "ai", "gg")
BARE_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.(?:" + "|".join(TLDS) + r")\b(?:/\S*)?",
    re.I)

URL_PLACEHOLDER = "（链接就不贴了）"

# ── Markdown ────────────────────────────────────────────────
# QQ 不渲染。留着的话用户看到的是 `**这样**`，比不加还难看。
MD_RULES = [
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),
    (re.compile(r"___(.+?)___", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`{3}[a-z0-9]*\n(.*?)`{3}", re.S | re.I), r"\1"),
    (re.compile(r"`(.+?)`", re.S), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.M), ""),              # 标题
    (re.compile(r"^\s*[-*+]\s+", re.M), "· "),          # 无序列表 → 中文点
    (re.compile(r"^\s*(\d+)\.\s+", re.M), r"\1. "),     # 有序列表保留
    (re.compile(r"^\s*>\s?", re.M), ""),                # 引用
    (re.compile(r"^\s*[-*_]{3,}\s*$", re.M), ""),       # 分隔线
    (re.compile(r"!\[.*?\]\(.*?\)"), ""),               # 图片
    (re.compile(r"\[(.+?)\]\(.*?\)"), r"\1"),           # 链接 → 只留文字
]

# ── 断点 ────────────────────────────────────────────────────
# 截断时优先在这些地方断，别把一句话劈成两半。
#
# ★ 分两级。人格文档现在教她"句尾别老打句号"（那确实更像真人），
#   副作用是长消息里可能一个句号都没有 —— 只用一级断点的话
#   会找不到地方断，退化成硬切，正好切在词中间。
#   所以还有逗号这一级垫着。
SENT_END = re.compile(r"(?<=[。！？!?；;\n])")
CLAUSE_END = re.compile(r"(?<=[，,、…])")


def byte_len(text: str) -> int:
    """QQ 的限长是按字节算的，一个中文字 3 字节。"""
    return len(text.encode("utf-8"))


def strip_urls(text: str) -> tuple[str, int]:
    """剥掉 URL 和裸域名。返回 (干净文本, 剥了几个)。"""
    n = 0
    for pat in (URL_RE, WWW_RE, BARE_DOMAIN_RE):
        text, k = pat.subn(URL_PLACEHOLDER, text)
        n += k
    if n:
        # 连着好几条链接会留下一串占位符，收成一个
        text = re.sub(r"(?:%s)(?:[\s、，,]*%s)+" % (
            re.escape(URL_PLACEHOLDER), re.escape(URL_PLACEHOLDER)),
            URL_PLACEHOLDER, text)
    return text, n


def strip_markdown(text: str) -> tuple[str, int]:
    """剥掉 Markdown 标记，保留文字。返回 (干净文本, 改了几处)。"""
    n = 0
    for pat, rep in MD_RULES:
        text, k = pat.subn(rep, text)
        n += k
    # 列表符号换成了 ·，可能连出一串
    text = re.sub(r"(?m)^·\s*$", "", text)
    return text, n


def clamp_bytes(text: str, limit: int = QQ_SAFE_BYTES) -> tuple[str, bool]:
    """
    按字节截断。先在句子边界断，实在不行才硬切。

    返回 (文本, 是否截断了)。
    """
    if byte_len(text) <= limit:
        return text, False

    # 一级：按句子切，贪心地拼到接近 limit
    parts = [p for p in SENT_END.split(text) if p]
    out: list[str] = []
    used = 0
    for p in parts:
        b = byte_len(p)
        if used + b > limit:
            break
        out.append(p)
        used += b

    if out:
        return "".join(out).rstrip(), True

    # 二级：整段都没有句号（她现在的风格就容易这样），退到逗号断
    parts = [p for p in CLAUSE_END.split(text) if p]
    out, used = [], 0
    for p in parts:
        b = byte_len(p)
        if used + b > limit:
            break
        out.append(p)
        used += b

    if out:
        # 用逗号断开的地方，末尾的逗号留着比去掉更自然
        return "".join(out).rstrip(), True

    # 第一句就超长（比如一坨没有标点的代码），只能硬切。
    # 按字节切，再退到不破坏 UTF-8 的位置。
    raw = text.encode("utf-8")[:limit - 3]
    while raw:
        try:
            return raw.decode("utf-8").rstrip() + "…", True
        except UnicodeDecodeError:
            raw = raw[:-1]
    return "…", True


def split_messages(text: str, limit: int = QQ_SAFE_BYTES) -> list[str]:
    """超长时切成多条。被动回复最多 5 条，所以自己先别切太多。"""
    if byte_len(text) <= limit:
        return [text] if text else []

    parts = [p for p in SENT_END.split(text) if p]
    out: list[str] = []
    cur = ""
    for p in parts:
        if byte_len(cur + p) > limit and cur:
            out.append(cur.rstrip())
            cur = p
        else:
            cur += p
    if cur.strip():
        out.append(cur.rstrip())

    # 还是超（单句过长），硬切兜底
    final: list[str] = []
    for m in out:
        while byte_len(m) > limit:
            cut, _ = clamp_bytes(m, limit)
            final.append(cut)
            m = m[len(cut.rstrip("…")):]
        if m.strip():
            final.append(m)
    return final


def sanitize(text: str, limit: int = QQ_SAFE_BYTES) -> tuple[str, list[str]]:
    """
    ★ 唯一的出站闸门。

    返回 (能发的文本, 做了哪些改动的说明)。说明是给日志看的，
    方便回头查"为什么她这次没贴链接"。
    """
    if not text:
        return "", []

    notes: list[str] = []

    text, n = strip_urls(text)
    if n:
        notes.append(f"剥掉 {n} 处链接")

    text, n = strip_markdown(text)
    if n:
        notes.append(f"去掉 {n} 处 Markdown")

    # 连续的空白收一收，别让消息里一堆空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)

    text, cut = clamp_bytes(text, limit)
    if cut:
        notes.append(f"按 {limit} 字节截断")

    return text, notes


# ═══════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════

def selftest() -> int:
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails += 1

    print("QQ 文本兜底自检\n")

    # ── 字节 ──
    check("中文字节数", byte_len("你好") == 6)
    check("混排字节数", byte_len("hi你好") == 8)

    # ── URL ──
    for src in ["看这个 https://example.com/a?b=1 挺好玩",
                "去 www.baidu.com 搜",
                "见 example.com 那篇",
                "文档在 docs.python.org/3/library 里"]:
        out, n = strip_urls(src)
        check(f"剥链接：{src[:18]}…", n > 0 and "http" not in out and "www." not in out,
              f"→ {out}")

    check("真域名全剥干净",
          all(t not in strip_urls("a.com b.cn c.io d.top")[0]
              for t in ("a.com", "b.cn", "c.io", "d.top")))
    check("版本号不误伤", strip_urls("升到 v1.2.0 了")[1] == 0)
    check("纯中文不误伤", strip_urls("今天天气不错")[1] == 0)

    # ── Markdown ──
    md = "## 标题\n**加粗**和`代码`\n- 列表项\n1. 有序\n> 引用\n---"
    out, n = strip_markdown(md)
    check("Markdown 全剥掉", n >= 5 and "**" not in out and "##" not in out, f"→ {out!r}")
    check("列表变成中文点", "· 列表项" in out)
    check("有序列表保留数字", "1. 有序" in out)
    check("链接只留文字", strip_markdown("[点这里](http://x.com)")[0] == "点这里")

    # ── 截断 ──
    long = "第一句。第二句。第三句。第四句。第五句。" * 20
    out, cut = clamp_bytes(long)
    check("超长会截断", cut and byte_len(out) <= QQ_SAFE_BYTES, f"{byte_len(out)} 字节")
    check("在句子边界断", out.endswith("。"), f"结尾 {out[-6:]!r}")

    # ★ 没有句号的长文本（她现在少用句号了）要退到逗号断，
    #   不能硬切在词中间。
    noperiod = "今天天气还行，我出门转了一圈，路上碰到个卖花的，" * 40
    o2, c2 = clamp_bytes(noperiod)
    check("★ 没句号时退到逗号断",
          c2 and byte_len(o2) <= QQ_SAFE_BYTES and o2.rstrip().endswith(("，", "。", "、", "…")),
          f"结尾 {o2[-8:]!r}")

    nocomma = "啊" * 2000
    o3, c3 = clamp_bytes(nocomma)
    check("连逗号都没有才硬切",
          c3 and byte_len(o3) <= QQ_SAFE_BYTES)

    short = "很短"
    check("不超长就不动", clamp_bytes(short) == (short, False))

    # 单句超长（没有标点）要能硬切且不炸
    blob = "啊" * 2000
    out, cut = clamp_bytes(blob)
    check("无标点硬切不炸", cut and byte_len(out) <= QQ_SAFE_BYTES, f"{byte_len(out)} 字节")
    check("硬切不破坏编码", isinstance(out.encode("utf-8").decode("utf-8"), str))

    # 英文超长（按字节切可能落在字符中间）
    en = "a" * 3000
    out_en, _ = clamp_bytes(en)
    check("长英文截断", byte_len(out_en) <= QQ_SAFE_BYTES)

    # ── 分条 ──
    msgs = split_messages(long)
    check("超长会分条", len(msgs) > 1, f"{len(msgs)} 条")
    check("每条都不超", all(byte_len(m) <= QQ_SAFE_BYTES for m in msgs))
    check("不超长就一条", split_messages("你好") == ["你好"])

    # ── 总闸门 ──
    dirty = "**重点**：见 https://a.com/x 那篇。`代码`"
    out, notes = sanitize(dirty)
    check("sanitize 一次全清",
          "http" not in out and "**" not in out and "`" not in out, f"→ {out!r}")
    check("sanitize 有说明", len(notes) >= 2, f"{notes}")
    check("空输入不炸", sanitize("") == ("", []))

    check("超长输入 sanitize 后合规",
          byte_len(sanitize(long)[0]) <= QQ_SAFE_BYTES)

    print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        t, notes = sanitize(" ".join(sys.argv[1:]))
        print(t)
        print(f"\n--- {byte_len(t)} 字节 ---")
        for n in notes:
            print(f"· {n}")
    else:
        sys.exit(selftest())
