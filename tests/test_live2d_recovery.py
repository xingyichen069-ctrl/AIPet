"""Fault injection with fake native models; no OpenGL driver or API is needed."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import live2d_widget as L
import pet as P
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPixmap, QPainter, QShowEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

APP = QApplication.instance() or QApplication([])


def model_fixture(root):
    path = root / "model.model3.json"
    path.write_text(json.dumps({"Version": 3, "FileReferences": {
        "Moc": "body.moc3", "Textures": ["texture.png"]}}), encoding="utf-8")
    # These bytes are never passed to native code in unit tests.
    (root / "body.moc3").write_bytes(b"fixture")
    (root / "texture.png").write_bytes(b"fixture")
    return path


class ModelFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = model_fixture(self.root)

    def test_essential_files_are_resolved_relative_to_the_manifest(self):
        self.assertEqual(L.validate_model_files(self.path), self.root / "body.moc3")
        for name in ("body.moc3", "texture.png"):
            asset = self.root / name
            original = asset.read_bytes()
            asset.unlink()
            with self.assertRaisesRegex(ValueError, "缺失"):
                L.validate_model_files(self.path)
            asset.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "为空"):
                L.validate_model_files(self.path)
            asset.write_bytes(original)

    def test_missing_or_malformed_manifest_never_reaches_the_sdk(self):
        widget = L.Live2DWidget(self.path)
        self.addCleanup(widget.deleteLater)
        with patch.object(L, "live2d") as sdk:
            for text in ("{broken", "[]", '{}', '{"Version": 2}',
                         '{"Version": 3, "FileReferences": {"Moc": "body.moc3", "Textures": []}}'):
                self.path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    widget._load_model(str(self.path))
            self.path.unlink()
            with self.assertRaises(ValueError):
                widget._load_model(str(self.path))
            sdk.LAppModel.assert_not_called()

    def test_sdk_rejection_or_partial_load_releases_candidate_renderer(self):
        widget = L.Live2DWidget(self.path)
        self.addCleanup(widget.deleteLater)
        for stage in ("consistency", "load", "empty", "configure"):
            model = Mock()
            model.HasMocConsistencyFromFile.return_value = stage != "consistency"
            model.GetDrawableIds.return_value = [] if stage == "empty" else ["body"]
            if stage == "load":
                model.LoadModelJson.side_effect = RuntimeError("load failed")
            if stage == "configure":
                model.SetAutoBlinkEnable.side_effect = RuntimeError("configure failed")
            with self.subTest(stage=stage), patch.object(L, "live2d") as sdk:
                sdk.LAppModel.return_value = model
                with self.assertRaises((ValueError, RuntimeError)):
                    widget._load_model(str(self.path))
                model.DestroyRenderer.assert_called_once()


class RendererRecovery(unittest.TestCase):
    def setUp(self):
        self.widget = L.Live2DWidget("fixture.model3.json")
        self.failures, self.ready, self.reloads = [], [], []
        self.widget.render_failed.connect(self.failures.append)
        self.widget.render_ready.connect(lambda: self.ready.append(True))
        self.widget.reload_finished.connect(lambda ok, msg: self.reloads.append((ok, msg)))
        for name, value in (("HAS_LIVE2D", True), ("live2d", Mock())):
            handle = patch.object(L, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        self.widget.makeCurrent = Mock()
        self.widget.doneCurrent = Mock()

    def tearDown(self):
        self.widget.stop()
        self.widget.deleteLater()
        APP.processEvents()

    def healthy(self):
        model = Mock()
        with patch.object(self.widget, "_load_model", return_value=model):
            self.widget.initializeGL()
        self.widget.resizeGL(260, 380)
        self.widget.paintGL()
        return model

    def test_initialization_failure_reports_once_and_stops_all_frame_work(self):
        with patch.object(self.widget, "_load_model", side_effect=RuntimeError("bad model")):
            self.widget.initializeGL()
        for _ in range(5):
            self.widget.paintGL()
            self.widget.resizeGL(260, 380)
        self.assertEqual(len(self.failures), 1)
        self.assertFalse(self.widget._ready)
        self.assertFalse(self.widget._timer.isActive())
        self.assertFalse(self.widget.request_reload())
        self.assertEqual(self.ready, [])

    def test_first_draw_reports_ready_once_and_cancels_startup_watchdog(self):
        self.widget.showEvent(QShowEvent())
        self.assertTrue(self.widget._startup_timer.isActive())
        self.healthy()
        self.widget.paintGL()
        self.assertEqual(self.ready, [True])
        self.assertFalse(self.widget._startup_timer.isActive())
        self.assertEqual(self.failures, [])

    def test_missing_context_is_detected_without_initialize_gl_callback(self):
        with patch.object(self.widget, "isVisible", return_value=True):
            self.widget._startup_expired()
            self.widget._startup_expired()
        self.assertEqual(len(self.failures), 1)
        self.assertIn("OpenGL", self.failures[0])

    def test_update_draw_and_resize_errors_stop_repeated_native_calls(self):
        for stage in ("Update", "Draw", "Resize"):
            with self.subTest(stage=stage):
                # Each subcase represents a fresh renderer, as a real retry does.
                self.widget._failed = False
                model = self.healthy()
                self.failures.clear()
                getattr(model, stage).side_effect = RuntimeError(stage + " failed")
                if stage == "Resize":
                    self.widget.resizeGL(200, 300)
                else:
                    self.widget.paintGL()
                calls = model.mock_calls[:]
                for _ in range(4):
                    self.widget.paintGL()
                self.assertEqual(model.mock_calls, calls)
                self.assertEqual(len(self.failures), 1)
                self.assertFalse(self.widget._timer.isActive())
                self.widget._cleanup_context()

    def test_bad_replacement_keeps_the_healthy_model_for_load_resize_update_and_draw(self):
        old = self.healthy()
        for stage in ("load", "Resize", "Update", "Draw"):
            candidate = Mock()
            if stage != "load":
                getattr(candidate, stage).side_effect = RuntimeError(stage + " failed")
            with self.subTest(stage=stage), patch.object(self.widget, "_load_model",
                    side_effect=RuntimeError("bad path") if stage == "load" else None,
                    return_value=candidate):
                self.assertTrue(self.widget.request_reload("replacement.model3.json"))
                self.assertFalse(self.widget.request_reload("another.model3.json"))
                self.widget.paintGL()
                self.assertIs(self.widget.model, old)
                self.assertTrue(self.widget._ready)
                self.assertEqual(self.widget.model_json, "fixture.model3.json")
                old.DestroyRenderer.assert_not_called()
                self.assertFalse(self.reloads[-1][0])
                if stage != "load":
                    candidate.DestroyRenderer.assert_called_once()
        self.assertEqual(self.failures, [])
        self.assertGreaterEqual(old.Draw.call_count, 5)

    def test_successful_replacement_draws_before_releasing_old_renderer(self):
        old = self.healthy()
        candidate = Mock()
        order = []
        candidate.Draw.side_effect = lambda: order.append("new frame")
        old.DestroyRenderer.side_effect = lambda: order.append("old released")
        with patch.object(self.widget, "_load_model", return_value=candidate):
            self.widget.request_reload("new.model3.json")
            self.widget.paintGL()
        self.assertEqual(order[:2], ["new frame", "old released"])
        self.assertIs(self.widget.model, candidate)
        self.assertEqual(self.widget.model_json, "new.model3.json")
        self.assertEqual(self.reloads, [(True, "new.model3.json")])

    def test_stop_releases_renderer_between_make_current_and_done_current_once(self):
        model = self.healthy()
        order = []
        self.widget.makeCurrent.side_effect = lambda: order.append("current")
        model.DestroyRenderer.side_effect = lambda: order.append("released")
        self.widget.doneCurrent.side_effect = lambda: order.append("done")
        self.widget.stop()
        self.widget.stop()
        self.widget.paintGL()
        self.assertEqual(order, ["current", "released", "done"])
        self.assertIsNone(self.widget.model)
        self.assertFalse(self.widget._timer.isActive())

    def test_context_recreation_can_initialize_again_without_resuming_a_stopped_widget(self):
        first = self.healthy()
        self.widget._cleanup_context()
        second = self.healthy()
        self.assertIsNot(first, second)
        self.assertTrue(self.widget._ready)
        first.DestroyRenderer.assert_called_once()
        self.widget.stop()
        with patch.object(self.widget, "_load_model") as load:
            self.widget.initializeGL()
        load.assert_not_called()


class FakeRenderer(QWidget):
    clicked = Signal(str)
    drag_finished = Signal()
    hovered = Signal(bool)
    render_ready = Signal()
    render_failed = Signal(str)
    reload_finished = Signal(bool, str)

    def __init__(self, path, parent, **kwargs):
        super().__init__(parent)
        self._ready = False
        self.stop = Mock()
        self.set_quiet = Mock()
        self.set_activity = Mock()
        self.request_reload = Mock(return_value=True)
        self.play_idle = Mock()


class BarePet(P.PetWindow):
    def __init__(self):
        QWidget.__init__(self)
        self.gl = None
        self.state = {"click_through": True}
        self._live2d_error = ""
        self._announce_live2d_ready = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedSize(P.CHAR_SIZE, P.CHAR_SIZE)
        self.pix = QPixmap(100, 100)
        self.pix.fill(Qt.transparent)
        painter = QPainter(self.pix)
        painter.fillRect(25, 25, 50, 50, QColor("#cc4455"))
        painter.end()
        self.show_bubble = Mock()
        self._sync_model_pose = Mock()
        self.open_chat = Mock()
        self.panel = Mock()
        self.panel.isVisible.return_value = False
        self._dragging = False
        self._suppress_double_click_until = 0
        self.companion = SimpleNamespace(_quiet=True, _activity="thinking")


class PetRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = model_fixture(self.root)
        for obj, name, value in ((P, "HAS_LIVE2D", True), (P, "Live2DWidget", FakeRenderer),
                                  (P, "L2D_CFG", {"enabled": True, "model": str(self.path)}),
                                  (P, "save_state", Mock())):
            handle = patch.object(obj, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        self.pet = BarePet()
        self.pet.show()

    def tearDown(self):
        self.pet._stop_live2d()
        self.pet.hide()
        self.pet.deleteLater()
        APP.processEvents()

    def test_queued_failure_restores_visible_clickable_static_pet_and_retry(self):
        self.pet._start_live2d()
        failed = self.pet.gl
        failed.render_failed.emit("draw failed")
        self.assertIs(self.pet.gl, failed)  # never delete during a GL callback
        APP.processEvents()
        self.assertIsNone(self.pet.gl)
        failed.stop.assert_called_once()
        self.assertFalse(failed.isVisible())
        self.assertFalse(self.pet.state["click_through"])
        self.assertFalse(self.pet.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(self.pet.width(), P.CHAR_SIZE)
        center = self.pet.rect().center()
        self.assertTrue(self.pet._body_at(QPointF(center)))
        self.assertFalse(self.pet._body_at(QPointF(1, 1)))
        frame = self.pet.grab().toImage()
        self.assertEqual(frame.pixelColor(center).name(), "#cc4455")
        QTest.mouseDClick(self.pet, Qt.LeftButton, pos=center)
        self.pet.open_chat.assert_called_once()
        self.pet._reload_model()
        self.assertIsNotNone(self.pet.gl)
        self.assertIsNot(self.pet.gl, failed)
        self.pet.gl._ready = True
        self.pet.gl.render_ready.emit()
        APP.processEvents()
        self.pet.gl.set_quiet.assert_called_with(True)
        self.pet.gl.set_activity.assert_called_with("thinking")
        self.assertEqual(self.pet._live2d_error, "")

    def test_missing_file_has_retry_and_recovers_after_files_are_restored(self):
        original = self.path.read_bytes()
        self.path.unlink()
        self.pet._start_live2d()
        self.assertIsNone(self.pet.gl)
        self.assertIn("找不到模型入口", self.pet._live2d_error)
        self.path.write_bytes(original)
        self.pet._reload_model()
        self.assertIsInstance(self.pet.gl, FakeRenderer)

    def test_late_failure_from_retired_renderer_cannot_discard_retry(self):
        self.pet._start_live2d()
        retired = self.pet.gl
        retired.render_failed.emit("old queued error")
        self.pet._fallback_live2d("first failure")
        self.pet._reload_model()
        current = self.pet.gl
        APP.processEvents()
        self.assertIs(self.pet.gl, current)
        self.assertIsNot(current, retired)

    def test_failed_hot_reload_does_not_switch_to_static(self):
        self.pet._start_live2d()
        healthy = self.pet.gl
        healthy._ready = True
        healthy.reload_finished.emit(False, "candidate missing")
        APP.processEvents()
        self.assertIs(self.pet.gl, healthy)
        healthy.stop.assert_not_called()
        self.assertIn("继续使用原模型", self.pet.show_bubble.call_args.args[0])

    def test_unavailable_dependency_leaves_static_controls_available(self):
        with patch.object(P, "HAS_LIVE2D", False):
            self.pet._reload_model()
        self.assertIsNone(self.pet.gl)
        self.assertIn("修复环境后需重启", self.pet._live2d_error)
        self.assertFalse(self.pet.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_fallback_survives_cleanup_and_state_write_errors(self):
        self.pet._start_live2d()
        failed = self.pet.gl
        failed.stop.side_effect = RuntimeError("context already gone")
        with patch.object(P, "save_state", side_effect=OSError("read only")):
            self.pet._fallback_live2d("draw failure")
        self.assertIsNone(self.pet.gl)
        self.assertFalse(failed.isVisible())
        self.assertFalse(self.pet.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertTrue(self.pet._body_at(QPointF(self.pet.rect().center())))

    def test_broken_fallback_image_uses_in_memory_placeholder(self):
        self.pet.pix = QPixmap()
        with patch.object(P, "ensure_asset") as disk_asset:
            self.pet._fallback_live2d("draw failure")
        disk_asset.assert_not_called()
        self.assertFalse(self.pet.pix.isNull())
        self.assertTrue(self.pet._body_at(QPointF(self.pet.rect().center())))

    def test_static_fallback_remains_draggable_and_saves_its_position(self):
        self.pet._fallback_live2d("draw failure")
        self.pet.move(200, 200)
        pos = QPointF(self.pet.rect().center())
        start = QPointF(self.pet.mapToGlobal(pos.toPoint()))
        self.pet.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pos, start,
                                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        self.pet.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pos, start + QPointF(30, 20),
                                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        self.pet.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, pos, start + QPointF(30, 20),
                                              Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        self.assertEqual(self.pet.pos(), QPoint(230, 220))
        self.assertEqual((self.pet.state["x"], self.pet.state["y"]), (230, 220))
