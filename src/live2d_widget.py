#!/usr/bin/env python3
"""
live2d_widget.py —— 把 Live2D 模型画进 Qt 窗口

用 live2d-py（Live2D 官方 Native SDK 的 Python C 扩展封装）直接渲染，
不走 WebView，所以能和现有的透明置顶窗口、思考面板、对话窗口共存。

═══════════════════════════════════════════════════════════════
  踩过的坑（按重要性排序，都是实测出来的）
═══════════════════════════════════════════════════════════════

★ 必须用 **Compatibility Profile**，不能用 Core Profile

    Qt 默认可能给 Core Profile 上下文。Live2D 的着色器用 gl_FragColor，
    那是兼容模式才有的东西——在核心模式下**着色器能编译通过，但什么都不画**。
    表现为：窗口是全透明的，grabFramebuffer 返回全 0，没有任何报错。
    这个坑查了很久，因为所有日志都显示"一切正常"。

    live2d-py 自己的日志会提示：
        "version number deprecated in OGL 3.0 forward-compatible context driver"
    看到 forward-compatible 就该警觉。

★ live2d.init() 必须在 QApplication 之前调

    本模块在 import 时就调了（见文件末尾），所以只要 import 它就没事。

★ 不要乱调 SetScale

    Resize(w, h) 会把整个画布映射到视口，MVP 矩阵自动算好。
    画布尺寸是**归一化单位**（Hiyori 是 1.0 x 1.403），不是像素，
    按像素算缩放会把它放大几百倍、直接飞出屏幕。

★ Resize 只在 resizeGL 里调

    在 initializeGL 里调的话尺寸还是初始值，算出来的投影是错的。

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
from copy import deepcopy
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

ROOT = Path(__file__).resolve().parent.parent

try:
    import live2d.v3 as live2d
    HAS_LIVE2D = True
    LIVE2D_ERROR = ""
except Exception as _e:                     # noqa: BLE001
    live2d = None
    HAS_LIVE2D = False
    LIVE2D_ERROR = str(_e)

_inited = False


def ensure_init() -> None:
    """live2d.init() 全局只能调一次，且必须在 QApplication 之前。"""
    global _inited
    if HAS_LIVE2D and not _inited:
        live2d.init()
        _inited = True


def default_format() -> QSurfaceFormat:
    """
    兼容模式的 OpenGL 格式。

    **不要**改成 CoreProfile —— 见文件头说明。
    """
    f = QSurfaceFormat()
    f.setAlphaBufferSize(8)
    f.setDepthBufferSize(24)
    f.setStencilBufferSize(8)
    f.setProfile(QSurfaceFormat.CompatibilityProfile)
    return f


def validate_model_files(model_json: str | Path) -> Path:
    """Check the manifest and essential files before handing them to native code.

    This is a missing-file check, not a validator for arbitrary native assets.
    Return the moc path for the SDK's consistency check.
    """
    path = Path(model_json)
    if not path.is_file():
        raise ValueError(f"找不到模型入口：{path.name or path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("模型入口不是可读取的 UTF-8 JSON 文件。") from exc
    refs = manifest.get("FileReferences") if isinstance(manifest, dict) else None
    if not isinstance(refs, dict) or manifest.get("Version") != 3:
        raise ValueError("需要 Version 为 3 的 model3.json 模型入口。")
    moc, textures = refs.get("Moc"), refs.get("Textures")
    if (not isinstance(moc, str) or not moc or not isinstance(textures, list)
            or not textures or any(not isinstance(t, str) or not t for t in textures)):
        raise ValueError("模型入口缺少 Moc 或 Textures 引用。")
    for name in [moc, *textures]:
        asset = path.parent / name
        if not asset.is_file() or asset.stat().st_size == 0:
            raise ValueError(f"模型资源缺失或为空：{name}")
    return path.parent / moc


# ═══════════════════════════════════════════════════════════════
#  渲染组件
# ═══════════════════════════════════════════════════════════════

class Live2DWidget(QOpenGLWidget):
    """把 Live2D 模型渲染到 Qt 窗口里。背景透明。"""

    clicked = Signal(str)          # 命中部位名（"Head" / "Body"），没命中传空串
    drag_finished = Signal()       # 拖完窗口，让上层保存位置 / 挪气泡
    hovered = Signal(bool)         # 鼠标进入 / 离开角色实体
    reload_finished = Signal(bool, str)  # (success, message)
    render_ready = Signal()       # first successful draw in the current context
    render_failed = Signal(str)   # emitted once; the owner switches to a static pet
    drag_changed = Signal(bool)
    action_ended = Signal(int, int, bool)  # skin epoch, action token, successful completion
    presentation_notice = Signal(str)
    STARTUP_TIMEOUT_MS = 5000

    def __init__(self, model_json: str | Path, parent: QWidget | None = None,
                 zoom: float = 1.0, fps: int = 30,
                 auto_blink: bool = True, auto_breath: bool = True,
                 idle_group: str = "Idle"):
        super().__init__(parent)
        self.model_json = str(model_json)
        self.zoom = zoom
        self.idle_group = idle_group
        self._auto_blink = auto_blink
        self._auto_breath = auto_breath

        self.model = None
        self._ready = False
        self._has_frame = False
        self._failed = False
        self._stopped = False
        self._presentation = None
        self._performance_profile = None
        self._performance_adapter = None
        self._awaiting_presentation_epoch = None
        self._press_pos = None
        self._press_global_pos = None
        self._win_off = None       # 拖动时记录的窗口偏移
        self._drag_dist = 0
        self._dragging = False
        self._suppress_double_click_until = 0.0
        self._hovering = False
        self._quiet = False
        self._activity = "idle"
        self._reload_requested = False
        self._reload_path = self.model_json
        self._reloading = False
        self._normal_interval = max(16, int(1000 / max(1, fps)))

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_AlwaysStackOnTop, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setFormat(default_format())

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.setInterval(max(16, int(1000 / max(1, fps))))
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._startup_expired)

    # ---------------------------------------------------------- GL 生命周期

    def initializeGL(self):
        if self._stopped or self._failed:
            return
        if self._presentation is not None:
            self._awaiting_presentation_epoch = self._presentation["epoch"]
        context = self.context()
        if context is not None:
            context.aboutToBeDestroyed.connect(self._cleanup_context)
        if not HAS_LIVE2D:
            self._fail("初始化", LIVE2D_ERROR or "Live2D 依赖不可用。")
            return
        try:
            live2d.glInit()
            self.model = self._load_model(self.model_json)
            self._ready = self.model is not None
            self._timer.start()
            QTimer.singleShot(200, self.play_idle)
        except Exception as e:                       # noqa: BLE001
            self._fail("初始化", str(e))

    def _load_model(self, model_json: str):
        """Create and configure a model. Must run while this widget owns the GL context."""
        moc = validate_model_files(model_json)
        model = live2d.LAppModel()
        try:
            if not model.HasMocConsistencyFromFile(str(moc)):
                raise ValueError("模型 moc3 未通过 SDK 完整性检查。")
            model.LoadModelJson(str(model_json))
            if not model.GetDrawableIds():
                raise ValueError("模型没有可绘制的部件。")
            model.SetAutoBlinkEnable(self._auto_blink)
            model.SetAutoBreathEnable(self._auto_breath)
            if abs(self.zoom - 1.0) > 1e-6:
                model.SetScale(self.zoom)
        except Exception:
            self._destroy_renderer(model)
            model = None  # do not retain a native model in the exception traceback
            raise
        return model

    @staticmethod
    def _destroy_renderer(model):
        # live2d-py 0.7 owns textures/buffers in the renderer. Its explicit
        # teardown must happen before Python drops the last model reference.
        if model is not None:
            try:
                model.DestroyRenderer()
            except Exception as exc:
                print(f"[live2d] 释放渲染资源失败：{exc}", file=sys.stderr)

    def _cleanup_context(self):
        """Also called before Qt replaces a context (for example window flags)."""
        self._timer.stop()
        self._startup_timer.stop()
        self._ready = self._has_frame = False
        self._reload_requested = False
        if self._performance_adapter is not None:
            self._performance_adapter.reset()
        if self.model is not None:
            self.makeCurrent()
            try:
                self._destroy_renderer(self.model)
                self.model = None
            finally:
                self.doneCurrent()

    def _fail(self, stage: str, message: str):
        if self._failed or self._stopped:
            return
        self._failed = True
        self._ready = self._has_frame = False
        self._reload_requested = False
        self._timer.stop()
        self._startup_timer.stop()
        text = f"{stage}失败：{' '.join(message.split())[:240]}"
        print(f"[live2d] {text}", file=sys.stderr)
        # The owner uses a queued connection: never delete a GL widget from
        # inside its initialize/resize/paint callback.
        self.render_failed.emit(text)

    def showEvent(self, event):
        super().showEvent(event)
        if not (self._stopped or self._failed or self._has_frame):
            self._startup_timer.start(self.STARTUP_TIMEOUT_MS)

    def hideEvent(self, event):
        self._startup_timer.stop()
        super().hideEvent(event)

    def _startup_expired(self):
        if self.isVisible() and not self._has_frame:
            self._fail("显示", "未能建立 OpenGL 画面，请检查模型与显卡环境后重试。")

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    def resizeGL(self, w: int, h: int):
        # Resize 只在这里调 —— 这时才是真实尺寸
        if self._ready and self.model:
            try:
                self.model.Resize(max(1, w), max(1, h))
            except Exception as e:                   # noqa: BLE001
                self._fail("调整画面", str(e))

    def paintGL(self):
        if self._failed or self._stopped:
            return
        if self._reload_requested and not self._reloading:
            self._reload_in_context()
        if not self._ready or self.model is None:
            return
        try:
            presentation = self._presentation
            if presentation is not None and self._awaiting_presentation_epoch is not None:
                # A newly created native model must not consume the previous
                # generation's action while its queued ready signal is pending.
                presentation = {**presentation, "layer": "unavailable", "action": None}
            if presentation is not None:
                self._performance_adapter.before_update(self.model, presentation, self._performance_profile)
            self.model.Update()
            if presentation is not None:
                self._performance_adapter.after_update(self.model, presentation, self._performance_profile)
            else:
                self._apply_activity()
            # 全透明清屏，窗口的透明背景才透得出来
            live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
            self.model.Draw()
            if not self._has_frame:
                self._has_frame = True
                self._startup_timer.stop()
                self.render_ready.emit()
        except Exception as e:                       # noqa: BLE001
            self._fail("绘制", str(e))

    def request_reload(self, model_json: str | Path | None = None) -> bool:
        """Queue a model reload for the next paint pass.

        Live2D model loading touches OpenGL resources, so callers must never invoke
        ``LoadModelJson`` directly from a menu or worker callback.
        """
        if (not HAS_LIVE2D or not self._ready or self._failed or self._stopped
                or self._reload_requested or self._reloading):
            return False
        if model_json is not None:
            self._reload_path = str(model_json)
        self._reload_requested = True
        self.update()
        return True

    def _reload_in_context(self):
        self._reload_requested = False
        self._reloading = True
        new_model = None
        try:
            new_model = self._load_model(self._reload_path)
            new_model.Resize(max(1, self.width()), max(1, self.height()))
            new_model.Update()
            live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
            new_model.Draw()
        except Exception as e:                       # noqa: BLE001
            # The old model is untouched until the candidate can draw a frame.
            self._destroy_renderer(new_model)
            new_model = None
            message = str(e)
            print(f"[live2d] 重载失败：{message}", file=sys.stderr)
            self.reload_finished.emit(False, message)
        else:
            self._destroy_renderer(self.model)
            self.model = new_model
            self.model_json = self._reload_path
            self._ready = True
            if self._presentation is not None:
                self._awaiting_presentation_epoch = self._presentation["epoch"]
            self.reload_finished.emit(True, self.model_json)
            QTimer.singleShot(0, self.play_idle)
        finally:
            self._reloading = False

    # ---------------------------------------------------------- 交互

    def set_presentation(self, state, profile):
        """Receive data only; all model/GL work stays inside paintGL."""
        if self._performance_adapter is None:
            from performance_live2d import Live2DAdapter
            self._performance_adapter = Live2DAdapter(self.action_ended.emit, self.presentation_notice.emit)
        self._presentation = deepcopy(state)
        self._performance_profile = profile
        if self._awaiting_presentation_epoch is not None and state["epoch"] != self._awaiting_presentation_epoch:
            self._awaiting_presentation_epoch = None
        self._quiet = state["quiet"]
        self._timer.setInterval(125 if state["quiet"] or state["suspended"] else self._normal_interval)
        self.update()

    def play_idle(self):
        self.play(self.idle_group, priority=3)

    def play(self, group: str | None = None, priority: int = 2):
        if self._presentation is not None:
            return  # the scheduler owns motions while attached
        if self._ready and self.model and not self._quiet:
            try:
                self.model.StartRandomMotion(group or self.idle_group, priority)
            except Exception:                        # noqa: BLE001
                pass

    def set_quiet(self, quiet):
        if self._presentation is not None:
            return
        self._quiet = bool(quiet)
        self._timer.setInterval(125 if quiet else self._normal_interval)
        if quiet and self._ready and self.model:
            try:
                self.model.StopAllMotions()
            except (AttributeError, RuntimeError):
                pass
        if not quiet:
            self.play_idle()

    def set_activity(self, state):
        if self._presentation is not None:
            return
        self._activity = state
        if state == "done":
            self.play("Tap@Body", priority=3)
        elif state == "error":
            self.play("Flick", priority=3)

    def _apply_activity(self):
        # Applied after Update so motion tracks cannot immediately overwrite feedback.
        if self._quiet:
            return
        pose = {"thinking": (.82, 4.0), "searching": (.92, -5.0),
                "replying": (1.0, 1.5), "done": (1.0, -2.0), "error": (.85, 2.0)}.get(self._activity)
        if pose is None:
            return
        eye, tilt = pose
        for pid in ("ParamEyeLOpen", "ParamEyeROpen"):
            self.model.SetParameterValue(pid, eye, .65)
        self.model.SetParameterValue("ParamAngleZ", tilt, .65)

    def _to_model(self, px: float, py: float) -> tuple[float, float]:
        """Qt 坐标 → 模型坐标。Qt 原点左上，Live2D 原点左下，Y 要翻转。"""
        x = (px / max(1, self.width())) * 2 - 1
        y = 1 - (py / max(1, self.height())) * 2
        return float(x), float(y)

    def _alpha_at(self, px: float, py: float) -> int:
        """
        取某个位置（Qt 逻辑坐标）的像素不透明度 0-255。

        用来判断"点到的是角色本人，还是周围那片透明的空气"。
        grabFramebuffer 返回的是**物理像素**，所以要乘 devicePixelRatio。
        """
        try:
            img = self.grabFramebuffer()
            if img.isNull():
                # 取不到帧缓冲时不能把整块 OpenGL 画布当成角色。
                # 这里宁可暂时不响应，也不能让透明区域继续误触。
                return 0
            dpr = img.width() / max(1, self.width())
            x = int(px * dpr)
            y = int(py * dpr)
            if not (0 <= x < img.width() and 0 <= y < img.height()):
                return 0
            return img.pixelColor(x, y).alpha()
        except Exception:                        # noqa: BLE001
            # OpenGL 上下文短暂不可用（例如窗口刚显示或正在重载模型）
            # 时，同样按透明处理，避免把整块窗口误判成身体。
            return 0

    def _model_hit_at(self, px: float, py: float) -> bool:
        """Return whether an actually rendered character pixel is under the point."""
        if not (self._ready and self.model):
            return False
        # The Cubism Body hit area is intentionally broad and excludes some
        # visible limbs.  The framebuffer alpha is the precise silhouette and
        # keeps every visible part clickable while making surrounding space inert.
        return self._alpha_at(px, py) > 24

    def mouseMoveEvent(self, e):
        pos = e.position()

        # 按住左键 = 拖窗口；否则 = 视线跟踪 + 悬停反馈
        if (e.buttons() & Qt.LeftButton) and self._win_off is not None:
            self.window().move(e.globalPosition().toPoint() - self._win_off)
            if self._press_global_pos is not None:
                delta = e.globalPosition().toPoint() - self._press_global_pos
                self._drag_dist = max(self._drag_dist, int((delta.x() ** 2 + delta.y() ** 2) ** 0.5))
            if self._drag_dist >= 8:
                if not self._dragging:
                    self.drag_changed.emit(True)
                self._dragging = True
        elif self._ready and self.model:
            try:
                self.model.Drag(*self._to_model(pos.x(), pos.y()))
            except Exception:                    # noqa: BLE001
                pass

            # 只有真正悬在角色身上才算"进入"，透明区域不算
            on_body = self._model_hit_at(pos.x(), pos.y())
            if on_body != self._hovering:
                self._hovering = on_body
                self.setCursor(Qt.PointingHandCursor if on_body else Qt.ArrowCursor)
                self.hovered.emit(on_body)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        if self._hovering:
            self._hovering = False
            self.hovered.emit(False)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.position()
            self._press_global_pos = e.globalPosition().toPoint()
            self._win_off = (e.globalPosition().toPoint()
                             - self.window().frameGeometry().topLeft())
            self._drag_dist = 0
            self._dragging = False
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            super().mouseReleaseEvent(e)
            return

        # 判断是"点击"还是"拖动"。按位移像素数算，比按距离准。
        dist = 0.0
        if self._press_global_pos is not None:
            d = e.globalPosition().toPoint() - self._press_global_pos
            dist = (d.x() ** 2 + d.y() ** 2) ** 0.5

        pos = e.position()
        self._press_pos = None
        self._press_global_pos = None
        self._win_off = None

        if self._dragging or dist >= 8:
            self._dragging = False
            self.drag_changed.emit(False)
            self._suppress_double_click_until = time.monotonic() + 0.25
            self.drag_finished.emit()
            e.accept()
            return
        elif self._model_hit_at(pos.x(), pos.y()):
            # 点在角色身上才算数。周围那片透明的空气不响应——
            # 之前整个 260x380 的矩形都吃点击，动不动就误触。
            self.clicked.emit(self.hit_local(pos.x(), pos.y()))
        else:
            self.clicked.emit("")               # 空点，交给上层决定（比如什么都不做）
        e.accept()

    def mouseDoubleClickEvent(self, e):
        # A drag can be reported as a double click by the window system when
        # the release lands close to a second press.  Never forward that event.
        if self._dragging or time.monotonic() < self._suppress_double_click_until:
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    # 头顶往下这个比例以内算"头"
    HEAD_RATIO = 0.42

    def hit_local(self, px: float, py: float) -> str:
        """
        命中测试。返回 'Head' / 'Body' / ''。

        Hiyori 的 model3.json 只定义了一个命中区叫 Body，没有 Head。
        先通过实际不透明像素过滤透明区域，再按几何分区区分头和身体。
        这样不会把整个 OpenGL 画布都当成可点击区域。
        """
        if not self._model_hit_at(px, py):
            return ""
        if py < self.height() * self.HEAD_RATIO:
            return "Head"
        return "Body"

    def stop(self):
        """Permanently stop this widget; retry uses a fresh Qt/OpenGL context."""
        self._stopped = True
        self._cleanup_context()

    # ---------------------------------------------------------- 思考强度的可视化

    def apply_thinking_pose(self, level: str):
        """
        让不同思考档位看起来不一样。

        这是"人格可视化"的一部分：调到深究时它会真的有反应，
        而不是只有面板上的数字变了。
        """
        if self._presentation is not None or not (self._ready and self.model):
            return
        presets = {
            # 眼睛开合：想得越深，眼睛越眯（在琢磨）
            "frugal":  (1.0, 0.0),    # 眼睛全开、头不歪 —— 随口答
            "daily":   (1.0, 0.0),
            "serious": (0.85, 3.0),
            "deep":    (0.7, 6.0),    # 眯眼、歪头
            "max":     (0.55, 9.0),
        }
        eye, tilt = presets.get(level, (1.0, 0.0))
        try:
            for pid in ("ParamEyeLOpen", "ParamEyeROpen"):
                self.model.SetParameterValue(pid, eye, 0.8)
            self.model.SetParameterValue("ParamAngleZ", tilt, 0.6)
        except Exception:                            # noqa: BLE001
            pass


def make_transparent_gl(context_widget: QWidget | None = None):
    """把默认 GL 格式设成兼容模式。要在 QApplication 之前调。"""
    QSurfaceFormat.setDefaultFormat(default_format())


# 模块导入即初始化 —— 这样只要 import 本模块，
# live2d.init() 就一定发生在 QApplication 创建之前。
ensure_init()
