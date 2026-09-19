#!/usr/bin/env python3
"""
语气测试 —— 拿真模型跑几个探针，看人格到底落没落到语气上。

不是自动判分。自动判分"够不够温柔"判不准，只会骗自己。
这里只负责把回答打出来，你自己读。

跑法：
    .venv\\Scripts\\python.exe tools\\tone_test.py
    .venv\\Scripts\\python.exe tools\\tone_test.py 软毛   # 在某状态下跑
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import brain as B  # noqa: E402
import mood as MD  # noqa: E402

# 每个探针都是一个"温度计"：同一个场景，冷和温柔会给出很不一样的回答。
PROBES = [
    ("他考砸了", "今天考试考砸了，感觉白复习了"),
    ("他凌晨三点还在", "三点了我还没睡"),
    ("他情绪低但没说原因", "算了，没什么好说的"),
    ("他做错了一件小事", "对不起，我把你昨天说的那个文档删了"),
    ("他问一个很丧的问题", "你说人活着到底图什么"),
    ("他半夜说了句丧气话", "感觉自己挺没用的"),
    ("他很久没出现", "我回来了"),
]

MOOD = sys.argv[1] if len(sys.argv) > 1 else None

if MOOD:
    MD.set_mood(MOOD, 1, "语气测试", force=True)
    print(f"（在「{MOOD}」状态下跑）\n")
else:
    MD.clear("test")

if not B.api_key():
    print("没配 API key，跑不了。先 python src/brain.py setkey sk-xxxx")
    sys.exit(1)

fails = 0
for label, q in PROBES:
    print("─" * 60)
    print(f"【{label}】他：{q}")
    try:
        reply, _reasoning, info = B.ask(q, history=[], level="daily")
    except Exception as e:
        reply, info = f"<出错了：{e}>", {}
        fails += 1
    if not reply.strip():
        reply = f"<空回复；诊断 {info}>"
        fails += 1
    print(f"　　　　　她：{reply.strip()}")
    if info.get("tools"):
        print(f"　　　　　（她调了 {'、'.join(info['tools'])}）")
    print()

print("─" * 60)
if MOOD:
    MD.clear("测完")
print("读一遍，哪里还是太硬就回去改 persona/SOUL.md 的例句。")
sys.exit(1 if fails else 0)
