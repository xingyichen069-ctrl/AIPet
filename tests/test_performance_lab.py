"""Offline workbench controls, replay clock and renderer retirement."""
from copy import deepcopy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import performance_lab as L
from performance_scenarios import SCENARIOS, scenario
from PySide6.QtCore import QEvent, Signal, Qt
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

APP = QApplication.instance() or QApplication([])


class FakeNative(QWidget):
    render_ready = Signal()
    render_failed = Signal(str)
    action_ended = Signal(int, int, bool)
    presentation_notice = Signal(str)
    drag_changed = Signal(bool)

    def __init__(self, path):
        super().__init__()
        self.stop = Mock()
        self.set_presentation = Mock()


class Workbench(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.lab = L.PerformanceLab(clock=lambda: self.clock[0], auto_start=False)
        self.lab.show()
        APP.processEvents()

    def tearDown(self):
        self.lab.close()
        self.lab.deleteLater()
        APP.processEvents()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_controls_record_work_mood_action_and_drag_recovery(self):
        self.lab.begin_turn()
        self.lab.mood_box.setCurrentIndex(self.lab.mood_box.findData("happy"))
        self.lab.dispatch("mood.set", mood=self.lab.mood_box.currentData())
        self.lab.dispatch("action.request", action="drink_tea", source="user")
        self.clock[0] = 1
        QTest.mousePress(self.lab.simulation, Qt.LeftButton)
        self.lab.phase("replying")
        self.assertEqual(self.lab.recording.engine.snapshot()["layer"], "drag")
        QTest.mouseRelease(self.lab.simulation, Qt.LeftButton)
        state = self.lab.recording.engine.snapshot()
        self.assertEqual((state["mood"], state["activity"], state["action"]), ("happy", "replying", None))
        self.assertGreater(self.lab.table.rowCount(), 0)
        self.assertFalse(self.lab.simulation.grab().isNull())

    def test_capability_switch_cancels_actions_and_explains_unsupported_tea(self):
        self.lab.dispatch("action.request", action="drink_tea", source="user")
        self.lab.profile_box.setCurrentIndex(self.lab.profile_box.findData("hiyori"))
        self.assertIsNone(self.lab.recording.engine.active)
        result = self.lab.dispatch("action.request", action="drink_tea", source="user")
        self.assertEqual(result["reason"], "unsupported_action")
        self.assertIn("未提供", self.lab.status.text())

    def test_all_scenarios_reach_identical_results_at_all_playback_speeds(self):
        for name in SCENARIOS:
            data = scenario(name)
            for speed in (1, 2, 4):
                with self.subTest(name=name, speed=speed):
                    self.assertTrue(self.lab.load_replay(data))
                    self.lab.speed_box.setCurrentIndex(self.lab.speed_box.findData(speed))
                    self.lab.toggle_play()
                    self.clock[0] += data["end_ms"] / 1000 / speed + .01
                    self.lab.tick()
                    self.assertFalse(self.lab._playing)
                    self.assertEqual(self.lab.recording.export(), data)
                    self.assertFalse(self.lab.controls.isEnabled())
                    self.assertFalse(self.lab.native_box.isEnabled())

    def test_pause_speed_change_seek_and_restart_preserve_the_event_clock(self):
        self.lab.load_replay(scenario("interrupt"))
        self.lab.toggle_play()
        self.clock[0] = .5
        self.lab.speed_box.setCurrentIndex(self.lab.speed_box.findData(2))
        self.assertEqual(self.lab.recording.engine.now, 500)
        self.clock[0] = 1
        self.lab.toggle_play()
        self.assertEqual(self.lab.recording.engine.now, 1500)
        self.clock[0] = 10
        self.lab.tick()
        self.assertEqual(self.lab.recording.engine.now, 1500)
        self.lab.slider.setValue(2600)
        self.assertEqual(self.lab.recording.engine.snapshot()["layer"], "drag")
        self.lab.slider.setValue(500)
        self.assertEqual(self.lab.recording.engine.active["name"], "drink_tea")
        self.lab.reset_recording()
        self.assertIsNone(self.lab.trace)
        self.assertEqual(self.lab.recording.engine.now, 0)
        self.assertEqual(self.lab.recording.events, [])
        self.assertFalse(self.lab.timer.isActive())

    def test_invalid_import_preserves_current_unsaved_recording(self):
        self.lab.begin_turn()
        before = self.lab.recording.export()
        bad = deepcopy(scenario("reply"))
        bad["expected"]["state"]["mood"] = "sad"
        self.assertFalse(self.lab.load_replay(bad))
        self.assertEqual(self.lab.recording.export(), before)
        self.assertIsNone(self.lab.trace)
        self.assertIn("不一致", self.lab.status.text())
        manual = scenario("reply")
        del manual["expected"]
        self.assertTrue(self.lab.load_replay(manual))
        self.assertIn("无预期结果", self.lab.status.text())

    def test_retired_native_callbacks_cannot_enter_new_recording_and_cleanup_failure_recovers(self):
        with patch.object(L, "Live2DWidget", FakeNative), patch.object(L, "HAS_LIVE2D", True):
            self.lab.profile_box.setCurrentIndex(self.lab.profile_box.findData("hiyori"))
            self.lab.native_box.setChecked(True)
            old = self.lab._native
            old.render_ready.emit()
            APP.processEvents()
            self.lab.dispatch("action.request", action="greet", source="user")
            active = self.lab.recording.engine.active
            old.action_ended.emit(active["epoch"], active["token"], True)
            old.stop.side_effect = RuntimeError("injected cleanup failure")
            self.lab.reset_recording()
            before = self.lab.recording.export()
            APP.processEvents()
            self.assertEqual(self.lab.recording.export(), before)
            self.assertIsNone(self.lab._native)
            self.assertFalse(self.lab.native_box.isChecked())
            self.assertTrue(self.lab.simulation.isVisible())

    def test_native_failure_uses_simulator_and_close_is_terminal(self):
        with patch.object(L, "Live2DWidget", FakeNative), patch.object(L, "HAS_LIVE2D", True):
            self.lab.profile_box.setCurrentIndex(self.lab.profile_box.findData("hiyori"))
            self.lab.native_box.setChecked(True)
            self.lab._native.render_failed.emit("injected draw failure")
            APP.processEvents()
            self.assertIsNone(self.lab._native)
            self.assertTrue(self.lab.recording.engine.available)
            self.assertIn("恢复示意绘制", self.lab.status.text())
        self.lab.close()
        self.assertEqual(self.lab.recording.engine.snapshot()["layer"], "closed")
        self.assertFalse(self.lab.timer.isActive())

    def test_standalone_gui_imports_no_brain_memory_or_personal_configuration(self):
        script = r'''
import importlib.abc, socket, sys
class BlockPrivate(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'brain', 'memory', 'thinking', 'companion', 'companion_ui', 'pet', 'persona_runtime'}:
            raise AssertionError('Offline lab imported ' + fullname)
sys.meta_path.insert(0, BlockPrivate())
def offline(*args, **kwargs):
    raise AssertionError('Offline lab tried a network connection')
socket.socket.connect = socket.create_connection = offline
sys.path.insert(0, sys.argv[1])
from performance_lab import PerformanceLab
from PySide6.QtWidgets import QApplication
app = QApplication([])
lab = PerformanceLab(auto_start=False)
lab.begin_turn()
lab.close()
print('OFFLINE_LAB_OK')
'''
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script, str(ROOT / "src")],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE_LAB_OK", result.stdout)
