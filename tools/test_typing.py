#!/usr/bin/env python3
"""
验证两件事：
  1. 工具调用不再往对话里写气泡
  2. 等待指示是三个会动的点（不是静止的"…"），而且收得干净

跑法：
    .venv\\Scripts\\python.exe tools\\test_typing.py
截的图落在 tools/_typing_*.png，自己看一眼。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QApplication(sys.argv)
import pet as P  # noqa: E402


class FakePet:
    proxy_url = ""
    def refresh_face(self): pass


fails = 0
def check(label, cond, extra=""):
    global fails
    print(f"  {'✓' if cond else '✗'} {label}{('  ' + extra) if extra else ''}")
    if not cond:
        fails += 1


print("桌宠对话窗 · 工具调用隐藏 + 三点动画\n")

w = P.ChatWindow(FakePet())
w.refresh_head()
w.show()
app.processEvents()

# ── 1. 发消息 → 应该出现三点，而不是"…" ─────────────────────
w.input.setText("现在几点了？")
w.send()
app.processEvents()

check("发送后挂上等待指示", w.waiting is not None)
row = w.waiting
dots = getattr(row, "dots", None)
check("等待指示是 TypingDots", isinstance(dots, P.TypingDots))

# 文字类气泡里不该再出现"…"
texts = [b.text() for b in w.findChildren(P.Bubble)]
check("没有静止的「…」占位", "…" not in texts, str(texts))

# ── 2. 三点真的在动 ─────────────────────────────────────────
seen = []
for _ in range(6):
    dots._tick()
    seen.append(dots._phase)
check("相位在循环", len(set(seen)) == P.TypingDots.PHASES,
      f"phases={sorted(set(seen))}")

# 抓三个不同相位的图，确认画面确实在变
shots = []
for ph in range(P.TypingDots.PHASES):
    dots._phase = ph
    dots.repaint()
    app.processEvents()
    img = dots.grab().toImage()
    shots.append(bytes(img.bits()))
check("三个相位的画面互不相同", len(set(shots)) == P.TypingDots.PHASES,
      f"{len(set(shots))} 种")
dots.grab().save(str(ROOT / "tools" / "_typing_dots.png"))

# ── 3. 工具调用不写进对话 ───────────────────────────────────
before = len(w.findChildren(P.Bubble))
w.on_chunk("tool", "⚙ get_time()")
w.on_chunk("tool", "⚙ web_search(北京天气)")
app.processEvents()
after = len(w.findChildren(P.Bubble))
check("工具调用不产生气泡", before == after, f"{before} → {after}")

texts = [b.text() for b in w.findChildren(P.Bubble)]
check("对话里搜不到工具名",
      not any("get_time" in t or "web_search" in t for t in texts), str(texts))
check("工具调用期间三点还在转", w.waiting is not None)

# ── 4. 正文到了 → 三点让位给正文气泡 ────────────────────────
w.on_chunk("content", "下午三点")
app.processEvents()
check("正文到达后三点收掉", w.waiting is None)
texts = [b.text() for b in w.findChildren(P.Bubble)]
check("正文正常显示", "下午三点" in texts, str(texts))

# ── 5. 收干净了 ─────────────────────────────────────────────
check("控件已从布局摘除", row.parent() is None)
check("定时器已停", not dots._timer.isActive())

# ── 6. 回答中途又来一轮工具 → 三点该回来 ────────────────────
w.on_chunk("tool", "⚙ recall(天气)")
app.processEvents()
check("中途工具调用后三点重新出现", w.waiting is not None)
w.on_done("现在几点了？")
app.processEvents()
check("结束后三点收掉", w.waiting is None)

# ── 7. 出错时仍然看得见 ─────────────────────────────────────
w.on_chunk("error", "网络断了")
app.processEvents()
texts = [b.text() for b in w.findChildren(P.Bubble)]
check("错误照常显示", "网络断了" in texts, str(texts))

w.grab().save(str(ROOT / "tools" / "_typing_chat.png"))
print(f"\n{'全部通过' if fails == 0 else str(fails) + ' 项失败'}")
sys.exit(1 if fails else 0)
