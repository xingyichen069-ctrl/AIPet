#!/usr/bin/env python3
"""
consolidate.py —— 「睡前整理」

整个记忆系统的分工是这样的：

    Python（机制层）           Agent / LLM（语义层）
    ─────────────────          ────────────────────
    存、检索、评分、衰减        理解、抽取事实、合并去重
    压缩、归档、可视化          判断什么重要、该怎么表述

Python 做不了"这句话说明了用户什么偏好"这种判断，LLM 做不了
"三年后这条记忆该不该还在"这种机械计算。所以分开。

本脚本负责给 agent 准备好素材，agent 读完后直接改写 persona/PROFILE.md。

用法：
    python src/consolidate.py brief    # 输出待整理素材（喂给 agent）
    python src/consolidate.py done     # 整理完了，打时间戳
    python src/consolidate.py status   # 看上次整理是什么时候
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory as M  # noqa: E402

if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


BRIEF_TEMPLATE = """\
# 记忆整理任务

你正在扮演一个长期陪伴用户的角色。现在是"睡前整理"时间——
把最近的经历内化成对用户的长期认识。

## 一、你当前对用户的认知（persona/PROFILE.md 现状）

```markdown
{profile}
```

## 二、{since} 以来发生的事

{entries}

## 三、你要做的

直接用 Edit / Write 工具改写 `{profile_path}`，遵守以下规则：

1. **合并优先于新增。** 新信息如果和已有条目说的是同一件事，
   *修改那条*，不要新增一条。比如已有"用户睡眠不好"，
   新信息是"用户昨天又熬到三点"，就把它改成
   "睡眠不好，经常凌晨两三点才睡" 并更新日期，而不是加一条新的。

2. **写事实，不写日志。** PROFILE 是当前状态快照。
   ❌ "9月14日用户说他下周三要交报告"
   ✅ "在准备课程设计报告（2026-09 起）"

3. **对号入座。** 把内容放进「身份 / 作息与节律 / 在做的事 / 喜好 /
   忌讳 / 重要日期 / 长期目标」里对应的分区。

4. **重要日期区用 permanent 语义。** 生日、纪念日、考试日期放这里，
   它们永远不会被遗忘。

5. **宁缺毋滥。** 拿不准是不是长期事实的，别写进去——
   它还在 journal 里，需要时检索得到。

6. **过期的要删。** 如果某个"在做的事"已经结束了，删掉或改写它。
   PROFILE 会膨胀，就是因为你只加不减。

7. **保留用户手写的内容。** 如果某条看起来不像你写的（语气不同、
   格式特别），不要动它。

8. **保持原有的分区标题和 Markdown 格式。**

9. **只写关于主人的事实。** 素材里凡是标了「群里有人说的」的条目，
   一律不要写进 PROFILE。那些是别人说的话，不是你主人的。
   把别人说的当成主人的事实写进来，比漏记严重得多。

改完文件就结束，不用输出解释。
"""


def _since() -> str:
    st = M.load_state()
    return st.get("last_consolidated") or "（从未整理过，这是第一次）"


def brief() -> str:
    st = M.load_state()
    since = st.get("last_consolidated")

    entries = M.load_journal()
    if since:
        entries = [e for e in entries if e["ts"] > since]

    if not entries:
        return ""

    # ★ 外人（群里其他人）的条目一律不进整理素材。
    # PROFILE.md 是"关于主人"的文件，群友随口说的任何事都不该写进去。
    # 它们的价值只在 journal 里——聊天时需要上下文就在那儿，不需要内化。
    owner_entries = [e for e in entries if e.get("speaker", "owner") == "owner"]
    skipped = len(entries) - len(owner_entries)

    if not owner_entries:
        return ""

    recent = "\n".join(
        f"- [{e['ts'][:10]}] (重要度{e.get('importance', 3)}) {e['text']}"
        + (f"  #{' #'.join(e['tags'])}" if e.get("tags") else "")
        for e in sorted(owner_entries, key=lambda x: x["ts"])
    )
    if skipped:
        recent += (f"\n\n（另有 {skipped} 条来自群聊中其他人的记录，"
                   f"已排除——那是别人说的话，不能当作关于主人的事实。）")

    prof_path = M._p("profile")
    profile = prof_path.read_text(encoding="utf-8") if prof_path.exists() else "（空）"

    return BRIEF_TEMPLATE.format(
        profile=profile,
        since=since or "（开始）",
        entries=recent,
        profile_path=prof_path,
    )


def mark_done() -> None:
    st = M.load_state()
    st["last_consolidated"] = M.now_iso()
    M.save_state(st)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"

    if cmd == "brief":
        out = brief()
        if not out:
            print("[没有新记忆需要整理]")
        else:
            print(out)

    elif cmd == "done":
        mark_done()
        print(f"已标记整理完成：{M.now_iso()}")

    elif cmd == "status":
        st = M.load_state()
        since = st.get("last_consolidated")
        entries = M.load_journal()
        pending = len([e for e in entries if not since or e["ts"] > since])
        print(json.dumps({
            "上次整理": since or "（从未）",
            "待整理条目": pending,
            "journal 总条目": len(entries),
        }, ensure_ascii=False, indent=2))

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
