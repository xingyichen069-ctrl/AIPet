"""Offline Qt workbench. Imports no brain, memory, persona or private config."""
from bisect import bisect_right
import math
from pathlib import Path
import sys
import time

from performance_profile import load_profile
from performance_trace import Recording, read_trace, replay
from performance_scenarios import SCENARIOS, scenario
from PySide6.QtCore import QPointF, QRectF, Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout,
                              QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                              QScrollArea, QSlider, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

try:  # SDK initialization, if available, occurs before QApplication.
    from live2d_widget import Live2DWidget, HAS_LIVE2D, make_transparent_gl
except Exception:
    Live2DWidget, HAS_LIVE2D, make_transparent_gl = None, False, None

ROOT = Path(__file__).resolve().parents[1]
MOOD_LABELS = {"calm": "平静", "happy": "开心", "sad": "低落", "curious": "好奇", "worried": "担心"}
ACTION_LABELS = {"greet": "打招呼", "acknowledge": "回应", "react": "触碰反馈", "drink_tea": "喝茶"}
STATE_LABELS = {"idle": "待机", "thinking": "思考中", "searching": "处理工具", "replying": "回复中",
                "done": "回复完成", "error": "出错", "closed": "已关闭", "unavailable": "渲染不可用",
                "suspended": "暂停表演", "quiet": "安静", "drag": "拖拽中", "action": "短动作",
                "activity": "工作状态", "mood": "基础心情"}
REASONS = {"applied": "已应用", "unsupported_action": "此皮套未提供该动作", "blocked": "当前状态阻止短动作",
           "duplicate_action": "重复动作，未叠加", "reply_action_budget": "本轮回复动作已达上限",
           "queue_full": "等待队列已满", "action_preempted": "高优先级动作打断旧动作",
           "action_started": "开始动作", "action_queued": "进入等待队列", "stale_turn": "拒绝旧轮次或已结束轮次",
           "stale_action": "拒绝旧模型或旧动作回调", "closed": "控制器已关闭", "mood_unavailable": "保留心情，画面退回平静",
           "action_finished": "动画通知结束", "renderer_declined_action": "模型未能执行动作",
           "queue_expired": "等待动作过期", "turn_timed_out": "本轮长时间无进度，表演状态已结束",
           "feedback_elapsed": "完成提示结束", "drag_lease_elapsed": "拖拽超时，恢复基础状态",
           "action_elapsed": "动作达到时限", "queued_action_started": "开始等待中的动作"}


def reason_label(reason):
    if reason.startswith("turn_ended:"):
        return "本轮结束；" + REASONS.get(reason.split(":", 1)[1], reason)
    return REASONS.get(reason, reason)


class SimulationView(QWidget):
    dragging = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(270, 300)
        self.state = None
        self.profile = load_profile("simulator")
        self._pressed = False

    def present(self, state, profile):
        self.state, self.profile = state, profile
        self.update()

    def paintEvent(self, event):
        if self.state is None:
            return
        state = self.state
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#f0f5f8"))
        p.setPen(QColor("#617388"))
        p.setFont(QFont("Microsoft YaHei UI", 10))
        p.drawText(QRectF(12, 12, self.width()-24, 42), Qt.AlignCenter,
                   "动作示意 · 可按住角色模拟拖拽")
        p.save()
        scale = min(self.width() / 320, (self.height()-65) / 340)
        p.translate(self.width()/2, self.height()/2 + 12)
        p.scale(scale, scale)
        pose = self.profile.pose(state)
        angle = pose.get("ParamAngleZ", 0)
        action = state["action"]
        progress = 0
        if action:
            progress = max(0, min(1, (state["at_ms"]-action["started_ms"]) /
                                 (action["ends_ms"]-action["started_ms"])))
            if action["name"] == "acknowledge":
                angle += math.sin(progress * math.pi * 2) * 9
        if state["layer"] == "drag":
            angle = -12
        p.rotate(angle)
        p.setPen(QPen(QColor("#304d5f"), 3))
        p.setBrush(QColor("#9fcdbb") if state["layer"] not in ("closed", "unavailable") else QColor("#c9ced3"))
        p.drawRoundedRect(QRectF(-64, 25, 128, 120), 45, 45)
        p.setBrush(QColor("#f9e7cb"))
        p.drawEllipse(QRectF(-86, -116, 172, 163))
        eye = pose.get("ParamEyeLOpen", 1)
        if state["layer"] in ("quiet", "suspended", "closed"):
            eye = .12
        for x in (-32, 32):
            p.setBrush(QColor("#304d5f"))
            p.drawEllipse(QRectF(x-6, -49, 12, max(2, 22*eye)))
        mouth = QPainterPath(QPointF(-18, -5))
        curve = pose.get("ParamMouthForm", 0)
        mouth.quadTo(QPointF(0, 7 + curve*20), QPointF(18, -5))
        p.setBrush(Qt.NoBrush)
        p.drawPath(mouth)
        if action and action["name"] == "drink_tea":
            p.setBrush(QColor("#fffdf7"))
            y = 58 - 32 * math.sin(min(1, progress*2) * math.pi / 2)
            p.drawRoundedRect(QRectF(-31, y, 62, 40), 7, 7)
            p.drawArc(QRectF(21, y+6, 28, 23), -90*16, 180*16)
            p.setPen(QPen(QColor("#7ba08a"), 2))
            p.drawLine(QPointF(-18, y+7), QPointF(20, y+7))
        elif action and action["name"] in ("greet", "react"):
            p.save()
            p.translate(60, 56)
            p.rotate(-35 + 22 * math.sin(progress*math.pi*6))
            p.setBrush(QColor("#f9e7cb"))
            p.drawRoundedRect(QRectF(0, -70, 23, 80), 12, 12)
            p.restore()
        p.restore()
        p.setPen(QColor("#304d5f"))
        label = ACTION_LABELS[action["name"]] if action else STATE_LABELS[state["layer"]]
        p.drawText(QRectF(12, self.height()-45, self.width()-24, 28), Qt.AlignCenter, label)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.dragging.emit(True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pressed:
            self._pressed = False
            self.dragging.emit(False)


class PerformanceLab(QWidget):
    def __init__(self, *, clock=time.monotonic, auto_start=True):
        super().__init__()
        self.setWindowTitle("AIPet · 表演控制测试台")
        self.resize(1120, 780)
        self.clock = clock
        self._auto_start = auto_start
        self.recording = Recording(load_profile("simulator"))
        self.trace = None
        self._playing = False
        self._speed = 1
        self._index = 0
        self._anchor, self._base = clock(), 0
        self._native = None
        self._history_marker = None
        self.setStyleSheet("""
            QWidget { color:#233748; font-family:'Microsoft YaHei UI'; font-size:13px; }
            PerformanceLab { background:#f7f9fc; }
            QGroupBox { border:1px solid #d9e2e8; border-radius:8px; margin-top:12px; padding-top:15px; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; }
            QPushButton { background:#ffffff; border:1px solid #b8c9d3; border-radius:5px; padding:7px 10px; }
            QPushButton:hover { background:#e8f2f1; }
            QPushButton:disabled { color:#96a0ac; background:#f0f2f5; }
            QComboBox { padding:5px; } QTableWidget { background:white; gridline-color:#e5ebf0; }
        """)
        outer = QVBoxLayout(self)
        title = QLabel("表演控制测试台")
        title.setStyleSheet("font-size:22px; font-weight:600;")
        outer.addWidget(title)
        outer.addWidget(QLabel("离线运行 · 模拟回复不连接 AI · 记录只包含状态事件 · 喝茶是示意动作"))
        top = QHBoxLayout()
        self.profile_box = QComboBox()
        self.profile_box.addItem("模拟角色（含喝茶）", "simulator")
        self.profile_box.addItem("Hiyori 现有能力", "hiyori")
        self.profile_box.setMinimumWidth(215)
        top.addWidget(QLabel("能力映射"))
        top.addWidget(self.profile_box)
        self.native_box = QCheckBox("原生 Hiyori 预览（实时模式）")
        self.native_box.setEnabled(False)
        top.addWidget(self.native_box)
        top.addStretch()
        reset = QPushButton("新建记录")
        reset.clicked.connect(self.reset_recording)
        top.addWidget(reset)
        outer.addLayout(top)
        split = QSplitter(Qt.Horizontal)
        preview = QWidget()
        self.preview_layout = QVBoxLayout(preview)
        self.preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QLabel("模拟角色")
        self.preview_label.setTextFormat(Qt.PlainText)
        self.preview_layout.addWidget(self.preview_label)
        self.simulation = SimulationView()
        self.preview_layout.addWidget(self.simulation, 1)
        split.addWidget(preview)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(5, 0, 0, 0)
        self.controls = QGroupBox("模拟回复与动作")
        grid = QGridLayout(self.controls)
        actions = [("开始新一轮", self.begin_turn), ("模拟工具处理", lambda: self.phase("searching")),
                   ("开始回复", lambda: self.phase("replying")), ("回复完成", lambda: self.end_turn("complete")),
                   ("回复出错", lambda: self.end_turn("error")), ("取消本轮", lambda: self.end_turn("cancelled"))]
        for index, (label, callback) in enumerate(actions):
            button = QPushButton(label)
            button.clicked.connect(callback)
            grid.addWidget(button, index//3, index % 3)
        self.mood_box, self.action_box = QComboBox(), QComboBox()
        for key, label in MOOD_LABELS.items():
            self.mood_box.addItem(label, key)
        for key, label in ACTION_LABELS.items():
            self.action_box.addItem(label, key)
        grid.addWidget(self.mood_box, 2, 0)
        mood = QPushButton("设置基础心情")
        mood.clicked.connect(lambda: self.dispatch("mood.set", mood=self.mood_box.currentData()))
        grid.addWidget(mood, 2, 1, 1, 2)
        grid.addWidget(self.action_box, 3, 0)
        action = QPushButton("播放短动作")
        action.clicked.connect(lambda: self.dispatch("action.request", action=self.action_box.currentData(), source="user"))
        grid.addWidget(action, 3, 1, 1, 2)
        self.quiet_box, self.suspend_box = QCheckBox("安静模式"), QCheckBox("暂停表演")
        grid.addWidget(self.quiet_box, 4, 0)
        grid.addWidget(self.suspend_box, 4, 1)
        drag = QPushButton("按住模拟拖拽")
        drag.pressed.connect(lambda: self.dispatch("drag.begin"))
        drag.released.connect(lambda: self.dispatch("drag.end"))
        grid.addWidget(drag, 4, 2)
        right_layout.addWidget(self.controls)
        state_group = QGroupBox("当前状态")
        state_layout = QGridLayout(state_group)
        self.state_labels = {}
        for index, (key, label) in enumerate((("mode", "时间 / 模式"), ("turn", "回复轮次"),
                ("activity", "工作状态"), ("mood", "基础心情"), ("action", "临时动作"),
                ("queue", "等待动作"), ("layer", "当前显示"))):
            value = QLabel()
            value.setTextFormat(Qt.PlainText)
            value.setWordWrap(True)
            self.state_labels[key] = value
            row, col = (0, 0) if index == 0 else (1 + (index-1)//2, 2*((index-1) % 2))
            state_layout.addWidget(QLabel(label), row, col)
            state_layout.addWidget(value, row, col+1, 1, 3 if index == 0 else 1)
        right_layout.addWidget(state_group)
        right_layout.addStretch()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.NoFrame)
        right_scroll.setWidget(right)
        split.addWidget(right_scroll)
        split.setSizes([340, 690])
        outer.addWidget(split, 2)
        replay_row = QHBoxLayout()
        self.scenario_box = QComboBox()
        for key, label in SCENARIOS.items():
            self.scenario_box.addItem(label, key)
        replay_row.addWidget(self.scenario_box)
        demo = QPushButton("载入示例")
        demo.clicked.connect(lambda: self.load_replay(scenario(self.scenario_box.currentData())))
        replay_row.addWidget(demo)
        current = QPushButton("回放当前记录")
        current.clicked.connect(lambda: self.load_replay(self.recording.export()) if self.trace is None else self.seek(0))
        replay_row.addWidget(current)
        self.play_button = QPushButton("播放")
        self.play_button.clicked.connect(self.toggle_play)
        replay_row.addWidget(self.play_button)
        self.speed_box = QComboBox()
        for value in (1, 2, 4):
            self.speed_box.addItem(f"{value} 倍速", value)
        self.speed_box.currentIndexChanged.connect(self._change_speed)
        replay_row.addWidget(self.speed_box)
        export = QPushButton("导出记录")
        export.clicked.connect(self.export_dialog)
        replay_row.addWidget(export)
        load = QPushButton("导入并核对")
        load.clicked.connect(self.import_dialog)
        replay_row.addWidget(load)
        outer.addLayout(replay_row)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self.seek)
        outer.addWidget(self.slider)
        self.status = QLabel("可手动操作，也可载入示例后播放。新建记录会清空当前未导出的记录。")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "事件", "结果", "处理原因", "工作状态", "临时动作"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setMinimumHeight(120)
        self.table.setMaximumHeight(150)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        outer.addWidget(self.table, 1)
        self.profile_box.currentIndexChanged.connect(self.change_profile)
        self.native_box.toggled.connect(self.toggle_native)
        self.quiet_box.toggled.connect(lambda value: self.dispatch("quiet.set", value=value))
        self.suspend_box.toggled.connect(lambda value: self.dispatch("suspend.set", value=value))
        self.simulation.dragging.connect(lambda value: self.dispatch("drag.begin" if value else "drag.end"))
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        if auto_start:
            self.timer.start()
        self.refresh()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(1120, area.width()-40), min(780, area.height()-60))

    def now(self):
        factor = self._speed if self.trace is not None else 1
        return max(self.recording.engine.now, self._base + int((self.clock()-self._anchor)*1000*factor))

    def _reanchor(self):
        self._base, self._anchor = self.recording.engine.now, self.clock()

    def _status(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color:#9b3e30;" if error else "color:#365c67;")

    def dispatch(self, kind, **fields):
        if self.trace is not None:
            return None
        try:
            result = self.recording.dispatch({"kind": kind, **fields}, self.now())
            self._status(reason_label(result["reason"]), not result["accepted"])
            self.refresh()
            return result
        except (ValueError, TypeError) as exc:
            self._status(str(exc), True)
            return None

    def begin_turn(self):
        return self.dispatch("turn.begin", turn_id=self.recording.engine.turn_id+1)

    def phase(self, phase):
        if not self.recording.engine.turn_id:
            self._status("请先开始一轮模拟回复。", True)
            return
        self.dispatch("turn.phase", turn_id=self.recording.engine.turn_id, phase=phase)

    def end_turn(self, outcome):
        if not self.recording.engine.turn_id:
            self._status("请先开始一轮模拟回复。", True)
            return
        self.dispatch("turn.end", turn_id=self.recording.engine.turn_id, outcome=outcome)

    def tick(self):
        try:
            if self.trace is None:
                self.recording.advance(self.now())
            elif self._playing:
                target = min(self.now(), self.trace["end_ms"])
                events = self.trace["events"]
                while self._index < len(events) and events[self._index]["at_ms"] <= target:
                    row = events[self._index]
                    self.recording.dispatch(row["event"], row["at_ms"])
                    self._index += 1
                self.recording.advance(target)
                if target == self.trace["end_ms"]:
                    self._playing = False
                    self._status("回放结束。状态由事件重新计算，原生逐帧画面不作为回放依据。")
            self.refresh()
        except (ValueError, TypeError) as exc:
            self._playing = False
            self.timer.stop()
            self._status(str(exc) + " 请导出或新建记录。", True)

    def refresh(self):
        state, profile = self.recording.engine.snapshot(), self.recording.engine.profile
        self.simulation.present(state, profile)
        if self._native is not None:
            self._native.set_presentation(state, profile)
        self.preview_label.setText(profile.label + (" · 原生模型" if self._native else " · 示意绘制"))
        active = state["action"]
        values = {"mode": f"{state['at_ms']/1000:.2f} 秒 · " + ("回放" if self.trace is not None else "实时记录"),
                  "turn": f"{state['turn_id']} · " + ("进行中" if state['turn_open'] else "未进行"),
                  "activity": STATE_LABELS[state["activity"]], "mood": MOOD_LABELS[state["mood"]],
                  "action": ACTION_LABELS[active["name"]] if active else "无",
                  "queue": "、".join(ACTION_LABELS[item["name"]] for item in state["queue"]) or "无",
                  "layer": STATE_LABELS[state["layer"]]}
        for key, text in values.items():
            self.state_labels[key].setText(text)
        for box, value in ((self.quiet_box, state["quiet"]), (self.suspend_box, state["suspended"])):
            with QSignalBlocker(box):
                box.setChecked(value)
        self.play_button.setText("暂停" if self._playing else "播放")
        self.play_button.setEnabled(self.trace is not None)
        self.controls.setEnabled(self.trace is None)
        self.profile_box.setEnabled(self.trace is None)
        if self.trace is not None:
            index = self.profile_box.findData(profile.id)
            if index >= 0:
                with QSignalBlocker(self.profile_box):
                    self.profile_box.setCurrentIndex(index)
        self.native_box.setEnabled(self.trace is None and profile.id == "hiyori" and HAS_LIVE2D)
        with QSignalBlocker(self.slider):
            self.slider.setRange(0, self.trace["end_ms"] if self.trace is not None else 0)
            self.slider.setValue(state["at_ms"] if self.trace is not None else 0)
        marker = self.recording.engine.audit_sha256
        if marker != self._history_marker:
            self._history_marker = marker
            rows = list(self.recording.engine.history)[-60:]
            self.table.setRowCount(len(rows))
            for row, record in enumerate(rows):
                action = record["state"]["action"]
                cells = (f"{record['at_ms']/1000:.2f}", record["kind"], "接受" if record["accepted"] else "拒绝",
                         reason_label(record["reason"]), STATE_LABELS[record["state"]["activity"]],
                         ACTION_LABELS[action["name"]] if action else "无")
                for col, text in enumerate(cells):
                    self.table.setItem(row, col, QTableWidgetItem(text))
            self.table.scrollToBottom()

    def reset_recording(self):
        self.retire_native()
        with QSignalBlocker(self.native_box):
            self.native_box.setChecked(False)
        self.trace, self._playing = None, False
        self.recording = Recording(load_profile(self.profile_box.currentData()))
        self._history_marker = None
        self._reanchor()
        if self._auto_start:
            self.timer.start()
        self._status("已新建记录。旧文件不受影响，当前未导出的记录已清空。")
        self.refresh()

    def change_profile(self):
        self.retire_native()
        with QSignalBlocker(self.native_box):
            self.native_box.setChecked(False)
        self.dispatch("skin.change", profile=load_profile(self.profile_box.currentData()).data())
        self.dispatch("renderer.set", value=True)

    def load_replay(self, data):
        try:
            has_expected = isinstance(data, dict) and "expected" in data
            verified = replay(data)  # verify the full record before replacing live state
            data = verified.export()
        except (ValueError, TypeError) as exc:
            self._status(str(exc), True)
            return False
        self.retire_native()
        with QSignalBlocker(self.native_box):
            self.native_box.setChecked(False)
        self.trace = data
        self.seek(0)
        checked = "已核对原记录结果" if has_expected else "原记录无预期结果，已重新计算"
        self._status(f"{checked}，共 {len(data['events'])} 个事件。点击播放，或拖动时间条检查状态。")
        if self._auto_start:
            self.timer.start()
        return True

    def seek(self, at_ms):
        if self.trace is None:
            return
        self._playing = False
        self.recording = replay(self.trace, until_ms=at_ms)
        self._index = bisect_right([row["at_ms"] for row in self.trace["events"]], at_ms)
        self._history_marker = None
        self._reanchor()
        self.refresh()

    def toggle_play(self):
        if self.trace is None:
            return
        if self._playing:
            self.tick()
            self._playing = False
        else:
            if self.recording.engine.now >= self.trace["end_ms"]:
                self.seek(0)
            self._playing = True
        self._reanchor()
        self.refresh()

    def _change_speed(self):
        if self._playing:
            self.tick()  # account for elapsed wall time at the previous speed
        self._speed = self.speed_box.currentData()
        self._reanchor()

    def export_dialog(self):
        folder = ROOT / "work/performance-traces"
        folder.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "导出表演记录", str(folder / "performance.json"), "JSON (*.json)")
        if not path:
            return
        try:
            target = replay(self.trace) if self.trace is not None else self.recording
            target.save(path)
            self._status("已导出完整记录。文件只包含状态事件、能力映射和计算结果。")
        except (OSError, ValueError) as exc:
            self._status(str(exc), True)

    def import_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入并核对表演记录", str(ROOT / "work"), "JSON (*.json)")
        if path:
            try:
                self.load_replay(read_trace(path))
            except (OSError, ValueError) as exc:
                self._status(str(exc), True)

    def toggle_native(self, enabled):
        self.retire_native()
        if not enabled:
            self.dispatch("renderer.set", value=True)
            return
        if self.trace is not None or self.recording.engine.profile.id != "hiyori" or not HAS_LIVE2D:
            return
        self.dispatch("renderer.set", value=False)
        try:
            widget = Live2DWidget(ROOT / "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json")
            self._native = widget
            widget.setMinimumSize(270, 300)
            widget.render_ready.connect(self._native_ready, Qt.QueuedConnection)
            widget.render_failed.connect(self._native_failed, Qt.QueuedConnection)
            widget.action_ended.connect(self._native_action_end, Qt.QueuedConnection)
            widget.presentation_notice.connect(self._native_notice, Qt.QueuedConnection)
            widget.drag_changed.connect(self._native_drag)
            widget.set_presentation(self.recording.engine.snapshot(), self.recording.engine.profile)
            self.simulation.hide()
            self.preview_layout.addWidget(widget, 1)
            widget.show()
        except Exception as exc:
            self._fallback_native(str(exc))
        self.refresh()

    def retire_native(self):
        widget, self._native = self._native, None
        if widget is not None:
            try:
                widget.stop()
            except Exception as exc:
                print(f"[performance-lab] 原生预览清理失败：{exc}", file=sys.stderr)
            self.preview_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self.simulation.show()

    def _native_ready(self):
        if self.sender() is self._native and self._native is not None:
            self.dispatch("renderer.set", value=True)

    def _native_failed(self, message):
        if self.sender() is self._native and self._native is not None:
            self._fallback_native(message)

    def _fallback_native(self, message):
        self.dispatch("renderer.set", value=False)
        self.retire_native()
        with QSignalBlocker(self.native_box):
            self.native_box.setChecked(False)
        self.dispatch("renderer.set", value=True)
        self._status("原生预览失败，已恢复示意绘制：" + message, True)

    def _native_action_end(self, epoch, token, success):
        if self.sender() is self._native and self._native is not None:
            self.dispatch("action.end", epoch=epoch, token=token, success=success)

    def _native_notice(self, message):
        if self.sender() is self._native and self._native is not None:
            self._status(message, True)

    def _native_drag(self, value):
        if self.sender() is self._native and self._native is not None:
            self.dispatch("drag.begin" if value else "drag.end")

    def closeEvent(self, event):
        self.timer.stop()
        if self.trace is None:
            self.dispatch("close")
        self.retire_native()
        super().closeEvent(event)


def main():
    if make_transparent_gl:
        make_transparent_gl()
    app = QApplication(sys.argv)
    app.setApplicationName("AIPet Performance Lab")
    window = PerformanceLab()
    window.show()
    return app.exec()
