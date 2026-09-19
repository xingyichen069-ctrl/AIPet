#!/usr/bin/env python3
"""
pet.py —— 桌面端角色窗口 + 思考强度控制面板

需要 PySide6：  pip install PySide6
或临时跑：      uv run --with PySide6 --no-project python src/pet.py

═══════════════════════════════════════════════════════════════
  界面结构
═══════════════════════════════════════════════════════════════

      ┌──────────┐   ┌─────────────────────┐
      │          │   │  思考强度            │
      │   角色   │   │  ○ ◔ ◑ ◕ ●  [自动]  │
      │  图片    │   │  ─────────────────  │
      │          │   │  深究 · 完整推理+自检 │
      └──────────┘   │  记忆 5000 / 推理 high│
                     └─────────────────────┘
       可拖动          点角色切换显隐

  两个独立窗口。角色窗口只有角色那么大，透明区域不挡鼠标；
  面板窗口单独存在，所以角色本身可以很小很干净。

═══════════════════════════════════════════════════════════════
  操作
═══════════════════════════════════════════════════════════════

  左键拖动      移动角色（面板跟着走）
  左键单击      显示 / 隐藏思考强度面板
  右键          菜单：鼠标穿透、置顶、换形象、退出
  Ctrl+Shift+T  全局切换面板显隐
  Esc           隐藏面板

  位置会自动记住，存在 data/pet_state.json

═══════════════════════════════════════════════════════════════
  换形象
═══════════════════════════════════════════════════════════════

  把图片丢进 assets/ 覆盖 character.png 即可（建议 240×240 左右的透明 PNG）。
  没有图片时程序会画一个占位形象，不是错误。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PySide6.QtCore import (QEasingCurve, QFileSystemWatcher, QPoint, QPropertyAnimation,
                                QRect, QRectF, Qt, QThread, QTimer, Signal)
    from PySide6.QtGui import (QAction, QBrush, QColor, QCursor, QFont, QFontMetrics,
                               QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath,
                               QPen, QPixmap, QRadialGradient, QShortcut, QTextCursor)
    from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu,
                                   QPushButton, QScrollArea, QSizePolicy, QTextEdit,
                                   QVBoxLayout, QWidget)
except ImportError:
    print("需要 PySide6：\n"
          "    pip install PySide6\n"
          "或临时运行：\n"
          "    uv run --with PySide6 --no-project python src/pet.py")
    sys.exit(1)

import memory as M          # noqa: E402
import thinking as T        # noqa: E402
from ui_theme import is_daytime
from desktop_state import Appearance
from theme_widgets import ThemeMenu, add_appearance_menu

ROOT = M.ROOT
ASSETS = ROOT / "assets"
CHAR_PNG = ASSETS / "character.png"
STATE_FILE = ROOT / "data" / "pet_state.json"

CHAR_SIZE = 240          # 角色显示尺寸
PANEL_W, PANEL_H = 268, 190


# ═══════════════════════════════════════════════════════════════
#  占位形象
# ═══════════════════════════════════════════════════════════════

def draw_placeholder(size: int = 960) -> QPixmap:
    """画一个石头小东西。不是美术，但比空白强，而且随时能换掉。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    u = size / 240.0          # 以 240 为设计基准
    cx = cy = size / 2

    def S(v: float) -> float:
        return v * u

    # ── 地面投影（最底层） ──────────────────────────────────
    p.setPen(Qt.NoPen)
    sh = QRadialGradient(cx, cy + S(74), S(62))
    sh.setColorAt(0.0, QColor(0, 0, 0, 96))
    sh.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setBrush(QBrush(sh))
    p.drawEllipse(QRectF(cx - S(66), cy + S(58), S(132), S(32)))

    # ── 脚（画在身体之前，才会从底下探出来） ────────────────
    for dx in (-30, 30):
        p.setBrush(QColor("#333a43"))
        p.setPen(QPen(QColor("#1d2228"), S(2.4)))
        p.drawEllipse(QRectF(cx + S(dx) - S(21), cy + S(46), S(42), S(26)))
        # 脚上的高光
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawEllipse(QRectF(cx + S(dx) - S(13), cy + S(51), S(26), S(9)))

    # ── 身体：压扁的鹅卵石，底部平 ──────────────────────────
    body = QPainterPath()
    body.moveTo(cx - S(70), cy + S(30))
    body.cubicTo(cx - S(84), cy - S(22), cx - S(54), cy - S(68), cx - S(12), cy - S(72))
    body.cubicTo(cx + S(36), cy - S(76), cx + S(77), cy - S(42), cx + S(77), cy + S(2))
    body.cubicTo(cx + S(77), cy + S(38), cx + S(46), cy + S(57), cx + S(4), cy + S(57))
    body.cubicTo(cx - S(32), cy + S(57), cx - S(62), cy + S(50), cx - S(70), cy + S(30))
    body.closeSubpath()

    grad = QLinearGradient(cx, cy - S(80), cx, cy + S(66))
    grad.setColorAt(0.0, QColor("#616b78"))
    grad.setColorAt(0.40, QColor("#48525d"))
    grad.setColorAt(1.0, QColor("#2f363e"))
    p.setPen(QPen(QColor("#1d2228"), S(2.8)))
    p.setBrush(QBrush(grad))
    p.drawPath(body)

    # ── 顶部高光 ────────────────────────────────────────────
    p.setPen(Qt.NoPen)
    hl = QRadialGradient(cx - S(22), cy - S(48), S(50))
    hl.setColorAt(0.0, QColor(255, 255, 255, 42))
    hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(hl))
    p.drawEllipse(QRectF(cx - S(70), cy - S(92), S(140), S(92)))

    # ── 苔藓：聚在左上一片，不要散成麻子 ────────────────────
    for dx, dy, rx, ry, a in [(-46, -22, 11, 7, 150), (-33, -12, 8, 5, 120),
                              (-54, -6, 7, 5, 105), (-38, 4, 6, 4, 90),
                              (52, 16, 7, 5, 80)]:
        c = QColor("#6f9174")
        c.setAlpha(a)
        p.setBrush(c)
        p.drawEllipse(QRectF(cx + S(dx) - S(rx), cy + S(dy) - S(ry),
                             S(rx * 2), S(ry * 2)))

    # ── 眼睛：半睁，一副"我 tolerate 你"的表情 ──────────────
    for dx in (-27, 27):
        ex, ey = cx + S(dx), cy - S(6)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#15191e"))
        p.drawEllipse(QRectF(ex - S(16), ey - S(15), S(32), S(30)))

        p.setBrush(QColor("#e0a44a"))
        p.drawEllipse(QRectF(ex - S(12), ey - S(11), S(24), S(24)))

        p.setBrush(QColor("#12151a"))
        p.drawEllipse(QRectF(ex - S(4), ey - S(9), S(8), S(18)))

        p.setBrush(QColor(255, 255, 255, 225))
        p.drawEllipse(QRectF(ex - S(9.5), ey - S(8), S(5), S(5)))

        # 上眼睑：盖住眼睛上缘约 40%，做出半睁的眼型
        lid_full = QPainterPath()
        lid_full.addEllipse(QRectF(ex - S(17), ey - S(16), S(34), S(32)))
        lid_clip = QPainterPath()
        lid_clip.addRect(QRectF(ex - S(18), ey - S(17.5), S(36), S(13)))
        p.setBrush(QColor("#515b67"))
        p.drawPath(lid_full.intersected(lid_clip))

        # 眼睑下缘的一道暗线，让边缘干净
        p.setPen(QPen(QColor("#1d2228"), S(1.6)))
        p.drawLine(QPoint(int(ex - S(16)), int(ey - S(4.5))),
                   QPoint(int(ex + S(16)), int(ey - S(4.5))))
        p.setPen(Qt.NoPen)

    p.end()
    return pm


def ensure_asset() -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    if not CHAR_PNG.exists():
        draw_placeholder().save(str(CHAR_PNG), "PNG")
    return CHAR_PNG


# ═══════════════════════════════════════════════════════════════
#  状态持久化
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    d = {"x": None, "y": None, "click_through": False, "topmost": True}
    if STATE_FILE.exists():
        try:
            d.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return d


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
#  对话：后台线程 + 窗口
# ═══════════════════════════════════════════════════════════════

try:
    import brain as BRAIN
    HAS_BRAIN = True
except ImportError:
    BRAIN = None
    HAS_BRAIN = False

# Live2D。这个 import 有副作用：它会调 live2d.init()，
# 而那必须在 QApplication 创建之前发生 —— 所以不能挪到函数里。
try:
    from live2d_widget import Live2DWidget, HAS_LIVE2D, make_transparent_gl
except Exception as _e:                     # noqa: BLE001
    Live2DWidget = None
    HAS_LIVE2D = False
    make_transparent_gl = None

L2D_CFG = M.CFG.get("live2d", {})


def live2d_ready() -> bool:
    """Live2D 是否可用且启用。"""
    if not (HAS_LIVE2D and L2D_CFG.get("enabled", False)):
        return False
    return (M.ROOT / L2D_CFG.get("model", "")).exists()


# ★ 等 worker 收尾的上限（秒）。超了就走 os._exit，不再等 ——
#   brain.stream 的取消点只在数据块之间，一次卡住的 urlopen 要等 30 秒
#   socket 超时，没有上限的话窗口就僵在那儿，看起来是"退出没反应"。
QUIT_GRACE_S = 5.0


class ProxyProbe(QThread):
    """
    启动时探测代理。

    为什么要开机探测而不是等用到再探：代理要真去连一次境外端点才知道通不通，
    那个过程要几秒。等用户问问题时才探，第一句话就会卡住。
    提前在后台探好，之后搜索直接用缓存结果。

    也会被右键菜单「重新检测代理」触发（比如你刚把 VPN 打开）。
    """
    done = Signal(str)          # 传代理地址，空字符串表示直连

    def run(self):
        try:
            import proxy as PX
            p = PX.refresh()
            self.done.emit(p or "")
        except Exception as e:
            self.done.emit(f"ERR:{e}")


class UpdateCheck(QThread):
    """
    后台问一次 GitHub 有没有新版本。

    必须开线程：那次请求要过代理、要等 GitHub 回，几秒钟。
    在主线程里做，右键菜单会当场僵住。

    ★ 它只查，不下载也不覆盖 —— 见 update.py 开头那段。
    """
    done = Signal(dict)         # update.check() 的结果

    def run(self):
        try:
            import update as UP
            self.done.emit(UP.check())
        except Exception as e:
            self.done.emit({"ok": False, "current": "", "latest": "", "latest_clean": "",
                            "newer": False, "url": "",
                            "error": f"{type(e).__name__}: {e}"})


class BrainWorker(QThread):
    """在后台线程里调 API，避免阻塞界面。"""
    chunk = Signal(str, str)     # (kind, text)  kind: content / reasoning / error
    level = Signal(dict)

    # 参数名不能叫 level —— 那会在 __init__ 里把上面的 level 信号覆盖成 None。
    # 这个坑踩过一次，改叫 level_override。
    def __init__(self, query: str, history: list[dict],
                 level_override: str | None = None):
        super().__init__()
        self.query, self.history = query, history
        self.level_override = level_override
        # ChatWindow fills in the session-specific key and a Qt signal callback.
        # Keeping this on the worker preserves compatibility with test workers and
        # lets the same brain context work for the desktop window and QQ bridge.
        self.task_context = {}
        self.task_callback = None

    def run(self):
        if not HAS_BRAIN:
            self.chunk.emit("error", "brain.py 未加载")
            return
        token = M.ACTIVE_MESSAGE.set(getattr(self, "memory_message_id", None))
        try:
            import local_tools as LT
            context = {
                "source": "local",
                "event": None,
                "query": self.query,
                "conversation_key": "local:desktop",
                "actor_id": "local",
                "actor_name": "本地窗口",
                "is_owner": True,
            }
            context.update(dict(getattr(self, "task_context", {}) or {}))
            callback = getattr(self, "task_callback", None)
            if callback and not context.get("task_notify"):
                context["task_notify"] = callback
            with LT.bind_context(**context):
                for kind, val in BRAIN.stream(self.query, self.history,
                                              self.level_override,
                                              cancelled=self.isInterruptionRequested):
                    if kind == "level":
                        self.level.emit(val)
                    elif kind in ("content", "reasoning", "error", "tool"):
                        self.chunk.emit(kind, val)
        except Exception as e:
            self.chunk.emit("error", f"{type(e).__name__}: {e}")
        finally:
            M.ACTIVE_MESSAGE.reset(token)


# 浅色主题配色。柔和、低对比，长时间看不累。
LIGHT = {
    "bg":       "#f7f8f9",
    "bubble":   "#ffffff",
    "border":   "#e9ecf0",
    "text":     "#3a4351",
    "dim":      "#9aa4b2",
    "user_bg":  "#dcefe9",
    "user_fg":  "#2a443f",
    "accent":   "#6cbfb0",
    "warn":     "#c98a3c",
    "err":      "#c2665c",
}

# ── 思考面板 / 右键菜单的主题 ────────────────────────────────
# 按本地时间自动切，与对话窗口共用昼夜判断。
# 档位颜色写在 thinking.json 里，日间模式会自动压暗，否则亮青色在白底上看不清。
PANEL_THEME = {
    "night": {
        "bg":      (18, 22, 28, 244),
        "border":  "#343e4a",
        "title":   "#8b97a8",
        "detail":  "#8b97a8",
        "muted":   "#6f7c8c",
        "sep":     "#252c35",
        "btn_bg_active_alpha": 56,
        "btn_bg_hover_alpha": 30,
        "btn_border_idle": "#2e373f",
        "btn_text_idle":   "#98a4b4",
        "day": False,
    },
    "day": {
        "bg":      (252, 253, 254, 246),
        "border":  "#dfe5eb",
        "title":   "#9aa6b4",
        "detail":  "#5b6675",
        "muted":   "#8a95a3",
        "sep":     "#e8edf1",
        "btn_bg_active_alpha": 38,
        "btn_bg_hover_alpha": 20,
        "btn_border_idle": "#dfe5eb",
        "btn_text_idle":   "#6b7684",
        "day": True,
    },
}


def theme() -> dict:
    """当前该用哪套主题。每次调用都重新判断，跨过饭点会自动切。"""
    return PANEL_THEME["day" if is_daytime() else "night"]


def QQ_STATUS() -> dict:
    """
    读 QQ 桥的连接状态，给右键菜单显示。

    QQ 桥是**另一个进程**（src/qq_bridge.py run），桌宠不拉起它、
    也不管它死活 —— 只是读一个它写的状态文件。这样 QQ 挂了
    不会把桌宠带崩，桌宠没开也不影响 QQ。

    进程被 kill 的时候来不及写"我停了"，所以靠时间戳判断新鲜度：
    超过三分钟没更新就当没在跑。
    """
    try:
        import qq_bot as QB
        st = QB.read_status()
    except ImportError:
        return {"state": "off", "label": "QQ：读不到状态"}

    state = st.get("state", "off")
    if state == "off":
        return {**st, "label": "QQ：没在跑"}

    label = {
        "ready": "QQ：已连接",
        "connecting": "QQ：连接中…",
        "open": "QQ：握手中…",
        "identify": "QQ：握手中…",
        "resume": "QQ：恢复会话中…",
        "waiting": f"QQ：重连中（第 {st.get('attempts', 0)} 次）",
        "reconnect": "QQ：重连中…",
        "invalid": "QQ：会话失效，重连中…",
        "closed": "QQ：断开了",
        "error": "QQ：出错了",
        "stopped": "QQ：已停止",
        "fatal": "QQ：出错停了，要处理",
    }.get(state, f"QQ：{state}")

    if state != "ready" and st.get("detail"):
        label += f"（{st['detail'][:24]}）"

    if state == "ready":
        try:
            import people as P
            n = len(P.cards())
            label += f"　·　认得 {n} 人" if n else "　·　还没认到人"
        except Exception:
            pass

    st["label"] = label
    return st


def shade(color, day: bool) -> QColor:
    """
    档位颜色在浅底上要压暗，否则 #5eead4 这种亮色在白底上几乎看不见。
    darker(145) 大约是压暗 45%，肉眼效果刚好。
    """
    c = color if isinstance(color, QColor) else QColor(color)
    return c.darker(148) if day else c


class Bubble(QLabel):
    """一条消息气泡。自动换行、可选中、圆角不对称（朝向说话者那侧更圆）。"""

    def __init__(self, text: str, role: str, maxw: int):
        super().__init__(text)
        self.setWordWrap(True)
        self.setMaximumWidth(maxw)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)

        if role == "user":
            self.setStyleSheet(f"""
                background:{LIGHT['user_bg']}; color:{LIGHT['user_fg']};
                border-radius:14px; border-top-right-radius:5px;
                padding:9px 13px; font-size:13px;""")
        elif role == "sys":
            self.setStyleSheet(
                f"background:transparent; color:{LIGHT['dim']};"
                "font-size:11.5px; padding:1px 6px;")
        elif role == "err":
            self.setStyleSheet(
                f"background:#fdf1ef; color:{LIGHT['err']};"
                "border-radius:10px; padding:8px 12px; font-size:12.5px;")
        else:                                   # pet
            self.setStyleSheet(f"""
                background:{LIGHT['bubble']}; color:{LIGHT['text']};
                border:1px solid {LIGHT['border']};
                border-radius:14px; border-top-left-radius:5px;
                padding:9px 13px; font-size:13px;""")


class TypingDots(QWidget):
    """
    三个跳动的点，表示"它在想"。

    做成控件而不是文字，是因为文字版（"…"）看不出还在不在动 ——
    网速慢的时候用户会以为卡死了。三个点轮流亮，一眼就知道活着。
    """

    PHASES = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(160)
        self.setFixedSize(56, 36)
        self.setStyleSheet("background:transparent;")

    def _tick(self):
        self._phase = (self._phase + 1) % self.PHASES
        self.update()

    def stop(self):
        self._timer.stop()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 白底圆角，跟"它"的气泡同款，但左上角更圆（这是纯装饰，不指向谁）
        card = QPainterPath()
        card.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1),
                            14, 14)
        p.setBrush(QColor(LIGHT["bubble"]))
        p.setPen(QPen(QColor(LIGHT["border"]), 1))
        p.drawPath(card)

        r = 4.0
        cy = self.height() / 2
        first_x = self.width() / 2 - 13
        for i in range(3):
            lit = (self._phase == i)
            c = QColor(LIGHT["accent"]) if lit else QColor("#c3cad2")
            rr = r + 1.4 if lit else r
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(QRectF(first_x + i * 13 - rr, cy - rr, rr * 2, rr * 2))
        p.end()


from companion_ui import ChatWindow, CompanionController, open_local


class ThinkingPanel(QWidget):
    def __init__(self, pet: "PetWindow"):
        super().__init__()
        self.pet = pet
        self.cfg = T.load()
        self.hovered: str | None = None
        self.buttons: list[tuple[str, QRectF]] = []
        self._drag_from: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._dragged = False

        # Use a normal window: macOS Tool windows float above regular apps.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(PANEL_W, PANEL_H)
        self.setMouseTracking(True)
        self.setWindowOpacity(0.0)

        self.anim = QPropertyAnimation(self, b"windowOpacity")
        self.anim.setDuration(150)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.finished.connect(self._after_fade)   # 只连一次

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.fade_out)

        # 主题定时器：每分钟看一眼是不是跨过早晚分界了。
        # 只在"白天/黑夜"真的翻转时才重绘，不是每分钟都刷。
        self._was_day = is_daytime()
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._check_theme)
        self._theme_timer.start(60_000)

    def _check_theme(self):
        now_day = is_daytime()
        if now_day != self._was_day:
            self._was_day = now_day
            self.update()

    # ---------------------------------------------------------- 布局

    def _row_rects(self) -> list[tuple[str, QRectF]]:
        """两行：第一行 auto + 五档，第二行不放按钮。"""
        keys = ["auto"] + T.LEVELS
        pad, gap = 14.0, 6.0
        n = len(keys)
        w = (PANEL_W - pad * 2 - gap * (n - 1)) / n
        y = 44.0
        out = []
        for i, k in enumerate(keys):
            x = pad + i * (w + gap)
            out.append((k, QRectF(x, y, w, 46)))
        return out

    # ---------------------------------------------------------- 绘制

    def paintEvent(self, _):
        self.buttons = self._row_rects()
        cfg = T.load()
        cur = cfg.get("current", "daily")
        th = theme()
        day = th["day"]

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 卡片底 —— 深/浅由系统时间决定
        card = QPainterPath()
        card.addRoundedRect(QRectF(0.5, 0.5, PANEL_W - 1, PANEL_H - 1), 14, 14)
        p.setBrush(QColor(*th["bg"]))
        p.setPen(QPen(QColor(th["border"]), 1.4))
        p.drawPath(card)

        # 标题
        p.setPen(QColor(th["title"]))
        f = QFont("Microsoft YaHei UI", 8)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.4)
        p.setFont(f)
        p.drawText(QRectF(16, 14, 160, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   "思 考 强 度")

        # 当前档位标签
        cur_name = "自动" if cur == "auto" else T.preset(cur).get("name", cur)
        p.setPen(shade("#5eead4", day))
        f2 = QFont("Microsoft YaHei UI", 8)
        p.setFont(f2)
        p.drawText(QRectF(PANEL_W - 90, 14, 74, 20),
                   Qt.AlignRight | Qt.AlignVCenter, f"当前 {cur_name}")

        # 档位按钮
        for key, r in self.buttons:
            if key == "auto":
                icon, name, color = "⟳", "自动", "#7dd3fc"
            else:
                pr = cfg["presets"].get(key, {})
                icon = pr.get("icon", "?")
                name = pr.get("name", key)
                color = pr.get("color", "#8b97a8")

            c = shade(color, day)            # 浅底上要压暗
            active = (cur == key)
            hov = (self.hovered == key)

            bg = QColor(c)
            bg.setAlpha(th["btn_bg_active_alpha"] if active
                        else (th["btn_bg_hover_alpha"] if hov else 0))
            p.setBrush(bg)
            p.setPen(QPen(c if active else
                          (shade("#5a6674", day) if hov
                           else QColor(th["btn_border_idle"])),
                          1.5 if active else 1.0))
            rr = QPainterPath()
            rr.addRoundedRect(r, 9, 9)
            p.drawPath(rr)

            p.setPen(c if active or hov else QColor(th["btn_text_idle"]))
            p.setFont(QFont("Segoe UI Symbol", 13))
            p.drawText(QRectF(r.x(), r.y() + 5, r.width(), 20),
                       Qt.AlignCenter, icon)
            p.setFont(QFont("Microsoft YaHei UI", 8))
            p.drawText(QRectF(r.x(), r.y() + 24, r.width(), 18),
                       Qt.AlignCenter, name)

        # ── 详情区 ─────────────────────────────────────────
        det_key = self.hovered or (cur if cur != "auto" else None)
        if det_key and det_key in cfg.get("presets", {}):
            pr = cfg["presets"][det_key]
            pa = pr.get("params", {})
            y = 104

            p.setPen(QColor(th["sep"]))
            p.drawLine(14, y - 8, PANEL_W - 14, y - 8)

            p.setPen(shade(pr.get("color", "#e6eaf0"), day))
            p.setFont(QFont("Microsoft YaHei UI", 9, QFont.DemiBold))
            p.drawText(QRectF(16, y, 120, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       f"{pr.get('name','')} · {pr.get('summary','')}")

            p.setPen(QColor(th["detail"]))
            p.setFont(QFont("Microsoft YaHei UI", 8))
            p.drawText(QRectF(16, y + 20, PANEL_W - 32, 16),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       f"记忆 {pa.get('memory_budget',0)} tok · "
                       f"{pa.get('memory_entries',0)} 条 · 推理 {pa.get('reasoning_effort','')}")

            p.setPen(QColor(th["muted"]))
            p.setFont(QFont("Microsoft YaHei UI", 8))
            fl = pr.get("feels_like", "")
            p.drawText(QRectF(16, y + 38, PANEL_W - 32, 30),
                       Qt.AlignLeft | Qt.TextWordWrap, fl)
        else:
            y = 112
            p.setPen(QColor(th["sep"]))
            p.drawLine(14, y - 8, PANEL_W - 14, y - 8)
            p.setPen(QColor("#8b97a8"))
            p.setFont(QFont("Microsoft YaHei UI", 8))
            p.drawText(QRectF(16, y, PANEL_W - 32, 60), Qt.AlignLeft | Qt.TextWordWrap,
                       "自动：按问题难度自己挑档。\n悬停任意档位看详情。")
        p.end()

    # ---------------------------------------------------------- 交互

    def mouseMoveEvent(self, e):
        if self._drag_from is not None and e.buttons() & Qt.LeftButton:
            delta = e.globalPosition().toPoint() - self._drag_from
            if self._dragged or delta.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
                self.move(self._bounded_position(self._drag_start + delta))
                self.setCursor(Qt.ClosedHandCursor)
            self.hide_timer.stop()
            return
        pos = e.position()
        prev = self.hovered
        self.hovered = None
        for key, r in self.buttons:
            if r.contains(pos):
                self.hovered = key
                break
        if self.hovered != prev:
            self.update()
        self.setCursor(Qt.PointingHandCursor if self.hovered else Qt.OpenHandCursor)
        self.hide_timer.stop()

    def leaveEvent(self, _):
        self.hovered = None
        self.update()
        if self._drag_from is None:
            self._restart_hide_timer()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.hide_timer.stop()
        pos = e.position()
        for key, r in self._row_rects():
            if r.contains(pos):
                T.set_level(key)
                self.pet.on_level_changed(key)
                self.update()
                return
        self._drag_from = e.globalPosition().toPoint()
        self._drag_start = self.pos()
        self._dragged = False
        self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._drag_from is None:
            return
        if self._dragged:
            self.pet.state['thinking_panel_position'] = {'x': self.x(), 'y': self.y()}
            save_state(self.pet.state)
        self._drag_from = self._drag_start = None
        self._dragged = False
        self.setCursor(Qt.OpenHandCursor)
        if not self.underMouse():
            self._restart_hide_timer()

    def _restart_hide_timer(self):
        secs = T.load().get("ui", {}).get("auto_hide_seconds", 12)
        if secs and secs > 0:
            self.hide_timer.start(int(secs * 1000))

    # ---------------------------------------------------------- 显隐

    def fade_in(self):
        self.reposition()
        self.show()
        self.raise_()
        self.anim.stop()
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(1.0)
        self.anim.start()
        self._restart_hide_timer()

    def fade_out(self):
        self.anim.stop()
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _after_fade(self):
        if self.windowOpacity() < 0.05:
            self.hide()

    def reposition(self):
        if self._drag_from is not None:
            return
        saved = self.pet.state.get('thinking_panel_position')
        if isinstance(saved, dict) and 'x' in saved and 'y' in saved:
            self.move(self._bounded_position(QPoint(int(saved['x']), int(saved['y']))))
            return
        ui = T.load().get("ui", {})
        side = ui.get("position", "right")
        off = ui.get("offset", 12)
        g = self.pet.frameGeometry()
        scr = QApplication.primaryScreen().availableGeometry()

        if side == "left":
            x, y = g.left() - PANEL_W - off, g.top() + (g.height() - PANEL_H) // 2
        elif side == "top":
            x, y = g.left() + (g.width() - PANEL_W) // 2, g.top() - PANEL_H - off
        elif side == "bottom":
            x, y = g.left() + (g.width() - PANEL_W) // 2, g.bottom() + off
        else:
            x, y = g.right() + off, g.top() + (g.height() - PANEL_H) // 2

        # 贴边时翻到另一侧
        if x + PANEL_W > scr.right():
            x = g.left() - PANEL_W - off
        if x < scr.left():
            x = g.right() + off
        y = max(scr.top() + 4, min(y, scr.bottom() - PANEL_H - 4))

        self.move(int(x), int(y))

    def _bounded_position(self, pos):
        center = pos + QPoint(self.width() // 2, self.height() // 2)
        screen = QApplication.screenAt(center) or QApplication.screenAt(pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        return QPoint(max(area.left(), min(pos.x(), area.right() - self.width() + 1)),
                      max(area.top(), min(pos.y(), area.bottom() - self.height() + 1)))


# ═══════════════════════════════════════════════════════════════
#  角色窗口
# ═══════════════════════════════════════════════════════════════

class BubbleWindow(QWidget):
    """
    说话气泡，独立窗口。

    为什么不做成 PetWindow 里画：Live2D 走 OpenGL 渲染，
    会盖住同一窗口里 QPainter 画的东西。独立窗口最干净，
    而且静态图模式下也能用同一套。
    """

    def __init__(self, pet: "PetWindow"):
        super().__init__()
        self.pet = pet
        self.text = ""
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # 不挡鼠标
        self.hide()

    def show_text(self, text: str, ms: int = 2600):
        self.text = text
        self._resize_to_fit()
        self.reposition()
        self.show()
        self.reposition()  # 原生窗口首次显示后可能调整位置，再对齐人物。
        self.raise_()
        self.update()
        QTimer.singleShot(ms, self.hide)

    def _resize_to_fit(self):
        m = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        maxw = 240
        r = m.boundingRect(QRect(0, 0, maxw, 400),
                           Qt.AlignLeft | Qt.TextWordWrap, self.text)
        self._tw = r.width() + 24
        self._th = r.height() + 18
        self.setFixedSize(int(self._tw), int(self._th) + 10)   # +10 给尖角

    def reposition(self):
        g = self.pet.frameGeometry()
        x = g.center().x() - self.width() // 2
        y = g.top() - self.height() - 2
        screen = QApplication.screenAt(g.center()) or self.pet.screen() or QApplication.primaryScreen()
        scr = screen.availableGeometry()
        x = max(scr.left() + 4, min(x, scr.right() - self.width() - 4))
        if y < scr.top() + 4:
            y = g.bottom() + 2                     # 贴顶了改到下面
        y = max(scr.top() + 4, min(y, scr.bottom() - self.height() - 4))
        self.move(int(x), int(y))

    def paintEvent(self, _):
        if not self.text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self._tw, self._th

        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
        tail = QPainterPath()
        tail.moveTo(w / 2 - 7, h - 1)
        tail.lineTo(w / 2, h + 8)
        tail.lineTo(w / 2 + 7, h - 1)
        tail.closeSubpath()
        path = path.united(tail)

        p.setBrush(QColor(24, 29, 36, 246))
        p.setPen(QPen(QColor(70, 82, 96), 1.3))
        p.drawPath(path)

        p.setPen(QColor("#e6eaf0"))
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.drawText(QRectF(12, 9, w - 24, h - 18),
                   Qt.AlignLeft | Qt.TextWordWrap, self.text)
        p.end()


class PetWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.state = load_state()
        self.pix = QPixmap(str(ensure_asset()))
        self.drag_from: QPoint | None = None
        self.dragged = False

        flags = (Qt.FramelessWindowHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        if self.state.get("topmost", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("小日和")
        self.setMouseTracking(True)

        # 尺寸：Live2D 是竖构图（Hiyori 画布 1:1.403），静态图是正方形
        if live2d_ready():
            w = int(L2D_CFG.get("width", 260))
            h = int(L2D_CFG.get("height", 380))
        else:
            w = h = CHAR_SIZE
        self.setFixedSize(w, h)

        # 角色本体：Live2D 或静态图
        self.gl: Live2DWidget | None = None
        if live2d_ready():
            model_path = M.ROOT / L2D_CFG["model"]
            self.gl = Live2DWidget(
                model_path, self,
                zoom=float(L2D_CFG.get("zoom", 1.0)),
                fps=int(L2D_CFG.get("fps", 30)),
                auto_blink=bool(L2D_CFG.get("auto_blink", True)),
                auto_breath=bool(L2D_CFG.get("auto_breath", True)),
                idle_group=str(L2D_CFG.get("idle_group", "Idle")),
            )
            self.gl.setGeometry(0, 0, w, h)
            self.gl.clicked.connect(self._on_model_clicked)
            self.gl.drag_finished.connect(self._on_dragged)
            self.gl.hovered.connect(self._on_model_hover)
            # 启动后把当前档位的表情应用上
            QTimer.singleShot(1200, lambda: self._sync_model_pose())

        # 气泡是独立窗口 —— Live2D 是 OpenGL 绘制，会盖住同窗口内的 QPainter 内容
        self.bubble_win: BubbleWindow | None = None

        self.panel = ThinkingPanel(self)
        self.chat: ChatWindow | None = None
        self.proxy_url: str = ""
        self.prober: ProxyProbe | None = None
        self.updater: UpdateCheck | None = None
        self._restore_pos()
        self._watch_config()
        self.companion = CompanionController(self)
        self.appearance = Appearance(ROOT, self)

    # ---------------------------------------------------------- 代理

    def probe_proxy(self, announce: bool = True):
        """后台探测代理。启动时自动跑一次，右键菜单可重跑。"""
        if self.prober and self.prober.isRunning():
            return
        self.prober = ProxyProbe(self)
        self.prober.done.connect(
            lambda r: self.on_proxy_done(r, announce))
        self.prober.start()

    def on_proxy_done(self, result: str, announce: bool):
        if result.startswith("ERR:"):
            self.proxy_url = ""
            if announce:
                self.show_bubble("代理检测出错了。")
            return

        self.proxy_url = result
        if self.chat:
            self.chat.refresh_head()
        if not announce:
            return
        # 用 SOUL 的语气说一句，不报参数
        self.show_bubble("网络走代理了。" if result else "网线直着，也行。", 2400)

    # ── 检查更新 ────────────────────────────────────────────────
    # 只在手动点的时候查，平时零网络请求。查到有新版也不自己下 ——
    # 只给版本号和下载页，更不更新是人的事（update.py 开头写了为什么）。

    def _check_update(self):
        if self.updater and self.updater.isRunning():
            self.show_bubble("还在查呢。", 1600)
            return
        self.show_bubble("去看一眼。", 1600)
        self.updater = UpdateCheck(self)
        self.updater.done.connect(self.on_update_done)
        self.updater.start()

    def on_update_done(self, r: dict):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QMessageBox

        if not r.get("ok"):
            QMessageBox.warning(self, "检查更新", f"没查成。\n\n{r.get('error', '')}")
            return
        if not r.get("newer"):
            QMessageBox.information(self, "检查更新",
                                    f"已经是最新的了。\n\n本机 {r['current']}。")
            return

        box = QMessageBox(self)
        box.setWindowTitle("检查更新")
        box.setText(f"有新版本 {r['latest_clean']}。")
        box.setInformativeText(
            f"本机是 {r['current']}。\n\n"
            f"这里不会替你下载或覆盖任何东西 —— 打开下载页，你自己决定。")
        go = box.addButton("打开下载页", QMessageBox.AcceptRole)
        box.addButton("以后再说", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is go:
            QDesktopServices.openUrl(QUrl(r["url"]))

    # ---------------------------------------------------------- 对话窗口

    def open_chat(self):
        visit = self.chat is None or not self.chat.isVisible()
        if self.chat is None:
            self.chat = ChatWindow(self, BrainWorker)
        self.chat.refresh_head()
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()
        self.companion.tick(visit=visit)
        if self.chat.input.toPlainText() == "":
            self.chat.input.setFocus()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.open_chat()
            self.dragged = True          # 别让双击后的 release 触发面板切换

    # ---------------------------------------------------------- 位置

    def _restore_pos(self):
        scr = QApplication.primaryScreen().availableGeometry()
        x = self.state.get("x")
        y = self.state.get("y")
        if x is None or y is None:
            x = scr.right() - self.width() - 40
            y = scr.bottom() - self.height() - 60
        # 别跑出屏幕
        x = max(scr.left(), min(int(x), scr.right() - self.width() + 1))
        y = max(scr.top(), min(int(y), scr.bottom() - self.height() + 1))
        self.move(x, y)

    def reveal(self):
        """Recover a hidden/offscreen desktop pet and show its conversation."""
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.state["click_through"] = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.move(area.left() + 40, area.top() + max(20, (area.height()-self.height())//2))
        self.show()
        self.raise_()
        self._persist()
        self.open_chat()
        self.chat.move(min(self.x()+self.width()+24, area.right()-self.chat.width()+1),
                       max(area.top(), area.top()+(area.height()-self.chat.height())//2))
        self.chat.showNormal()
        self.chat.raise_()
        self.chat.activateWindow()
        if self.chat.windowHandle():
            self.chat.windowHandle().requestActivate()

    def _persist(self):
        self.state["x"] = self.x()
        self.state["y"] = self.y()
        save_state(self.state)

    # ---------------------------------------------------------- 配置热重载

    def _watch_config(self):
        self.watcher = QFileSystemWatcher([str(T.THINKING_FILE)], self)
        self.watcher.fileChanged.connect(self._on_config_changed)

    def _on_config_changed(self):
        # 有些编辑器是「替换文件」，得重新加回监视
        if str(T.THINKING_FILE) not in self.watcher.files():
            self.watcher.addPath(str(T.THINKING_FILE))
        T.load(force=True)
        self.panel.update()

    # ---------------------------------------------------------- 绘制

    def paintEvent(self, _):
        # Live2D 模式下窗口内容由 QOpenGLWidget 画，这里什么都不做
        if self.gl is not None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.drawPixmap(0, 0, self.width(), self.height(), self.pix)
        p.end()

    def _ensure_bubble(self) -> "BubbleWindow":
        if self.bubble_win is None:
            self.bubble_win = BubbleWindow(self)
        return self.bubble_win

    def show_bubble(self, text: str, ms: int = 2600):
        self._ensure_bubble().show_text(text, ms)

    def moveEvent(self, event):
        super().moveEvent(event)
        # Live2D 和静态形象都移动外层窗口；移动时立即跟随，不等松开鼠标。
        bubble = getattr(self, "bubble_win", None)
        if bubble is not None and bubble.isVisible():
            bubble.reposition()

    def _on_dragged(self):
        """拖完了 —— 存位置，并把气泡挪过去。"""
        self._persist()
        if self.bubble_win and self.bubble_win.isVisible():
            self.bubble_win.reposition()
        if self.panel.isVisible():
            self.panel.reposition()

    def _sync_model_pose(self):
        """把当前思考档位同步到模型表情。"""
        if not (self.gl and L2D_CFG.get("animate_thinking", True)):
            return
        r = T.resolve()
        self.gl.apply_thinking_pose(r["level"])

    def _on_model_hover(self, entered: bool):
        """鼠标悬到 / 离开角色身上的反馈。"""
        if not self.gl or not L2D_CFG.get("hover_feedback", True):
            return
        if entered and not self.companion._quiet:
            # 只给动作，不弹气泡 —— 每次经过都说话会很吵
            self.gl.play("Flick", priority=1)

    def _on_model_clicked(self, area: str):
        """点模型的头或身体 —— 给个反应，让它像活的。"""
        if self.companion._quiet:
            return
        if area == "Head":
            self._ensure_bubble().show_text("别戳头。", 1500)
            if self.gl:
                self.gl.play("Flick", priority=3)
            return

        if area == "Body":
            if self.gl:
                self.gl.play("Tap@Body", priority=3)
            self._toggle_panel()
            return

        # area == "" —— 点在透明的空气上。
        # 之前这里也会 toggle 面板，导致角色周围一大片空白都能触发。
        # 现在什么都不做，静静待着。
        return

    # ---------------------------------------------------------- 交互

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.dragged = False
        elif e.button() == Qt.RightButton:
            self._menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self.drag_from is not None and (e.buttons() & Qt.LeftButton):
            self.dragged = True
            self.move(e.globalPosition().toPoint() - self.drag_from)
            if self.panel.isVisible():
                self.panel.reposition()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.dragged:
                self._persist()
            else:
                self._toggle_panel()
            self.drag_from = None

    def _toggle_panel(self):
        if self.panel.isVisible() and self.panel.windowOpacity() > 0.5:
            self.panel.fade_out()
        else:
            self.panel.fade_in()

    def on_level_changed(self, key: str):
        """换档：模型表情跟着变，气泡里说一句。"""
        # 让它"看起来"也在换档 —— 调到深究会眯眼歪头
        self._sync_model_pose()
        if self.gl:
            self.gl.play_idle()

        if not T.load().get("notify", {}).get("announce", True):
            return
        if key == "auto":
            self.show_bubble("我自己看着办。")
        else:
            self.show_bubble(T.preset(key).get("feels_like", ""))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.panel.fade_out()

    # ---------------------------------------------------------- 右键菜单

    def _menu(self, gpos: QPoint):
        m = self._build_menu()
        m.exec(gpos)
        m.deleteLater()

    def _build_menu(self):
        m = ThemeMenu(self.appearance, self, heading=True)
        m.addAction("打开对话", self.open_chat)
        m.addAction("查看约定", self.companion.show_tasks)
        if self.companion.store.focus():
            m.addAction("结束陪伴", self.companion.stop_focus)
        else:
            m.addAction("安静陪伴半小时", self._start_company)
        m.addSeparator()
        m.addAction("打开记忆面板", self._open_memory_view)

        # ── QQ ──────────────────────────────────────────────
        # 状态从 data/qq_status.json 读。QQ 桥是**独立进程**，
        # 可能压根没跑 —— 看时间戳就知道它是活的还是被 kill 了。
        qs = QQ_STATUS()
        qa = m.addAction(qs["label"])
        qa.setEnabled(False)
        if qs["state"] == "ready":
            m.addAction("查看群里的人", self._show_people)
        else:
            off = m.addAction("QQ 没在跑（用 tools/qq_ctl.py start 启动）")
            off.setEnabled(False)
        add_appearance_menu(m, self.appearance)
        advanced = m.addMenu("高级")
        advanced.addAction("显示 / 隐藏思考面板", self._toggle_panel)
        levels = advanced.addMenu("思考强度")
        cfg = T.load()
        for lv in ["auto"] + T.LEVELS:
            name = "自动" if lv == "auto" else T.preset(lv).get("name", lv)
            a = levels.addAction(name)
            a.setCheckable(True)
            a.setChecked(cfg.get("current") == lv)
            a.triggered.connect(lambda _=False, k=lv: self._set_level(k))
        for label, key, callback in [
            ("鼠标穿透", "click_through", self._toggle_click_through),
            ("总在最前", "topmost", self._toggle_topmost),
            ("显示陪伴计时", "show_focus_timer", self._toggle_focus_timer)]:
            a = advanced.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.state.get(key, key != "click_through"))
            a.triggered.connect(callback)
        advanced.addAction("重新检测代理", lambda: self.probe_proxy(announce=True))
        advanced.addAction("检查更新", self._check_update)
        if self.gl:
            advanced.addAction("重载 Live2D 模型", self._reload_model)
        advanced.addAction("打开配置文件", self._open_config)
        m.addSeparator()
        m.addAction("退出", self.quit_safely)
        return m

    def _show_people(self):
        """把群里认得的人列出来。纯文本就够，不值得为它做个窗口。"""
        from PySide6.QtWidgets import QMessageBox
        try:
            import people as P
            cards = P.cards()
        except ImportError:
            return
        if not cards:
            QMessageBox.information(self, "群里的人", "还没认到任何人。")
            return

        lines = []
        owners = set(P.owner_openids())
        for p in cards[:30]:
            mark = "★主人 " if (p.get("openid") in owners or p.get("is_owner")) else "      "
            roles = {s.get("role") for s in p.get("scenes", {}).values() if s.get("role")}
            role = "管理员" if roles & {"owner", "admin"} else ""
            lines.append(f"{mark}{P.label(p['openid'])}　见过 {p.get('count', 0)} 次　{role}")
            if p.get("traits"):
                lines.append(f"        特点：{'、'.join(p['traits'])}")
            notes = P.notes_for(p["openid"])
            if notes:
                lines.append(f"        刚才：{notes[-1]['text'][:36]}")
        if len(cards) > 30:
            lines.append(f"……还有 {len(cards) - 30} 人")

        QMessageBox.information(self, f"群里的人（{len(cards)}）", "\n".join(lines))

    def _start_company(self):
        try:
            self.companion.store.create_task("安静陪伴", seconds=1800, kind="focus")
            self.companion.tick()
        except ValueError as e:
            self.show_bubble(str(e))

    def _toggle_focus_timer(self):
        self.state["show_focus_timer"] = not self.state.get("show_focus_timer", True)
        save_state(self.state)
        self.companion.tick()

    def quit_safely(self):
        """
        退出。第二次点会直接硬退，不再等。

        ★ 原来这里只等 worker 自己停，**没有上限**，于是"退不掉"：
          取消信号只在两个数据块之间才被看到（brain.stream 里的取消点都在
          循环头），一次 urlopen 卡住就得等 socket 超时 —— 30 秒。
          这期间窗口停在那儿，再点退出会被开头那个 _quitting 判断直接
          return 掉，什么都发生不了。看起来就是死了。

          现在两道保障：等满 QUIT_GRACE_S 就走 os._exit；中途再点一次
          也走 os._exit。os._exit 跳过清理，所以只在等不到的时候用 ——
          记忆、草稿、状态都是随手写盘的，丢不了什么。
        """
        if getattr(self, "_quitting", False):
            os._exit(0)                     # 第二次点 = 不等了
        self._quitting = True

        workers = [w for w in (self.chat.worker if self.chat else None,
                               self.prober, self.updater)
                   if w and w.isRunning()]
        if not workers:
            QApplication.quit()
            return

        for worker in workers:
            worker.requestInterruption()
        if self.chat:
            self.chat.head.setText(f"正在结束请求…（最多等 {QUIT_GRACE_S:g} 秒，再点一次退出就直接关）")
            self.chat.input.setEnabled(False)
            self.chat.btn.setEnabled(False)

        deadline = time.monotonic() + QUIT_GRACE_S

        def tick():
            if not any(w.isRunning() for w in workers):
                QApplication.quit()
            elif time.monotonic() >= deadline:
                os._exit(0)                 # worker 卡在网络里，等不到了

        self._quit_timer = QTimer(self)
        self._quit_timer.timeout.connect(tick)
        self._quit_timer.start(100)

    def _set_level(self, key: str):
        T.set_level(key)
        self.panel.update()
        self.on_level_changed(key)

    def _toggle_click_through(self):
        v = not self.state.get("click_through", False)
        self.state["click_through"] = v
        save_state(self.state)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, v)
        self.show_bubble("现在点不到我了。" if v else "能点到了。")

    def _toggle_topmost(self):
        v = not self.state.get("topmost", True)
        self.state["topmost"] = v
        save_state(self.state)
        flags = self.windowFlags()
        flags = (flags | Qt.WindowStaysOnTopHint) if v else (flags & ~Qt.WindowStaysOnTopHint)
        self.setWindowFlags(flags)
        self.show()
        if v:
            self.panel.raise_()

    def _regen_asset(self):
        """重新生成占位形象（只对静态图模式有意义）。"""
        if self.gl is not None:
            self.show_bubble("现在是 Live2D，不用占位图。")
            return
        if CHAR_PNG.exists():
            CHAR_PNG.unlink()
        self.pix = QPixmap(str(ensure_asset()))
        self.update()

    def _reload_model(self):
        """重载 Live2D 模型 —— 换了模型文件之后用。"""
        if self.gl is None:
            self.show_bubble("当前是静态图模式。")
            return
        try:
            self.gl.model.LoadModelJson(str(M.ROOT / L2D_CFG["model"]))
            self.gl.play_idle()
            self.show_bubble("换好了。")
        except Exception as e:                       # noqa: BLE001
            self.show_bubble(f"换模型失败：{e}")

    def _open_config(self):
        import os
        open_local(T.THINKING_FILE)

    def _open_memory_view(self):
        """
        打开记忆面板。

        ★ 必须先重新生成再打开。
        之前这里直接打开 view/memory.html，但那是个静态文件 ——
        只有跑 render.py（比如每晚的定时任务）才更新。结果就是
        面板显示的是几小时前的内容，刚写进去的记忆根本看不到。
        """
        import os
        try:
            import render
            render.render()                     # 先按当前记忆重建
        except Exception as e:                  # noqa: BLE001
            self.show_bubble(f"面板生成失败：{e}")
            return
        p = M._p("view")
        if p.exists():
            open_local(p)


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    # ★ 必须在 QApplication 之前 —— 设成兼容模式，否则 Live2D 什么都不画
    if make_transparent_gl and live2d_ready():
        make_transparent_gl()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QMessageBox
    app_lock = QLockFile(str(ROOT / "data" / "desktop.lock"))
    app_lock.setStaleLockTime(0)
    if not app_lock.tryLock(0):
        QMessageBox.information(None, "小日和", "桌宠已经运行，可以从托盘打开对话。")
        return
    pet = PetWindow()
    pet.show()
    from PySide6.QtWidgets import QSystemTrayIcon
    app.setQuitOnLastWindowClosed(False)
    if QSystemTrayIcon.isSystemTrayAvailable():
        pet.tray = QSystemTrayIcon(QIcon(str(CHAR_PNG)), pet)
        tray_menu = ThemeMenu(pet.appearance, pet, heading=True)
        tray_menu.addAction("打开对话", pet.open_chat)
        tray_menu.addAction("显示桌宠 / 恢复点击", pet.reveal)
        add_appearance_menu(tray_menu, pet.appearance)
        tray_menu.addAction("退出", pet.quit_safely)
        pet.tray.setContextMenu(tray_menu)
        pet.tray.setToolTip("小日和")
        pet.tray.show()

    from desktop_state import DesktopState
    saved_layout = DesktopState(ROOT).data.get('layout', {})
    if "--show-chat" in sys.argv or (isinstance(saved_layout, dict) and saved_layout.get('open')):
        QTimer.singleShot(350, pet.open_chat)
        def report_visibility():
            print("[desktop]", {
                "app_state": app.applicationState().name,
                "pet_visible": pet.isVisible(),
                "pet_exposed": pet.windowHandle().isExposed(),
                "pet_geometry": pet.geometry().getRect(),
                "chat_visible": bool(pet.chat and pet.chat.isVisible()),
                "chat_exposed": bool(pet.chat and pet.chat.windowHandle().isExposed()),
                "model_ready": bool(pet.gl and pet.gl._ready),
            }, flush=True)
        QTimer.singleShot(1600, report_visibility)
        QTimer.singleShot(8000, report_visibility)

    # 启动就打一次招呼
    QTimer.singleShot(700, lambda: pet.show_bubble("在。", 2000) if not pet.companion.store.focus() else None)

    # 代理探测放后台跑。不 announce —— 刚启动就弹一句"网络走代理了"
    # 太吵，结果会显示在对话窗口标题和右键菜单里。
    QTimer.singleShot(1200, lambda: pet.probe_proxy(announce=False))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
