"""Qt signals and desktop lifecycle connected to the real scheduler, no API."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtCore import QEvent, QObject, Signal, Qt
from PySide6.QtWidgets import QApplication, QWidget
from performance_profile import load_profile
from performance_runtime import PerformanceRuntime
import companion_ui as UI
import test_companion as TC

APP = QApplication.instance() or QApplication([])


class ControlledWorker(QObject):
    chunk = Signal(str, str)
    finished = Signal()

    def __init__(self, query, history):
        super().__init__()
        self.running = self.interrupted = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.interrupted = True

    def finish(self):
        self.running = False
        self.finished.emit()


class QtBridge(unittest.TestCase):
    def setUp(self):
        TC.Services.setUp(self)
        self.patch_store = patch("companion_ui.C.Store", return_value=self.store)
        self.patch_store.start()
        self.clock = [0.0]
        self.pet = QWidget()
        self.pet.state, self.pet.gl, self.pet.panel = {}, Mock(), Mock()
        self.pet.bubble_win = self.pet.chat = None
        self.runtime = self.pet.performance = PerformanceRuntime(
            load_profile("hiyori"), self.pet, clock=lambda: self.clock[0], auto_start=False)
        self.runtime.send("renderer.set", value=True)
        self.pet.companion = UI.CompanionController(self.pet)
        self.pet.companion.timer.stop()
        self.chat = UI.ChatWindow(self.pet, ControlledWorker)
        self.pet.chat = self.chat

    def tearDown(self):
        self.runtime.close()
        self.chat.prepare_quit()
        for widget in (self.chat, self.pet.companion.popup, self.pet.companion.counter, self.pet):
            widget.hide()
            widget.deleteLater()
        APP.processEvents()
        # processEvents alone does not deliver DeferredDelete without exec().
        # Dispose test windows here, before later tests start worker threads.
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.patch_store.stop()
        TC.Services.tearDown(self)

    def start(self):
        self.chat.input.setPlainText("合成测试回复")
        self.chat.send()
        return self.chat.worker

    def test_stream_progress_refreshes_lease_and_records_no_text(self):
        worker = self.start()
        self.assertEqual(self.runtime.engine.activity, "thinking")
        for second, kind, phase in ((119, "reasoning", "thinking"), (238, "tool", "searching"),
                                    (357, "content", "replying")):
            self.clock[0] = second
            worker.chunk.emit(kind, "PRIVATE_SENTINEL")
            self.assertEqual(self.runtime.engine.activity, phase)
            self.assertTrue(self.runtime.engine.turn_open)
        worker.finish()
        self.assertEqual(self.runtime.engine.activity, "done")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(list(self.runtime.engine.history)))
        count = len(self.store.messages())
        worker.finished.emit()
        self.assertEqual(len(self.store.messages()), count)
        self.pet.gl.set_activity.assert_not_called()

    def test_cancel_is_immediate_and_late_chunks_and_done_do_not_revive(self):
        worker = self.start()
        self.runtime.send("action.request", action="greet", source="reply", turn_id=1)
        self.chat.send_or_stop()
        self.assertTrue(worker.interrupted)
        self.assertFalse(self.runtime.engine.turn_open)
        self.assertIsNone(self.runtime.engine.active)
        self.assertEqual(self.runtime.engine.activity, "idle")
        worker.chunk.emit("content", "迟到的内容")
        worker.finish()
        self.assertEqual(self.chat.cur_reply, [])
        self.assertEqual(self.runtime.engine.activity, "idle")
        self.assertIsNone(self.runtime.engine.active)

    def test_queued_old_worker_signals_cannot_modify_a_new_reply(self):
        old = self.start()
        old.chunk.disconnect(self.chat.on_chunk)
        old.finished.disconnect(self.chat.on_done)
        old.chunk.connect(self.chat.on_chunk, Qt.QueuedConnection)
        old.finished.connect(self.chat.on_done, Qt.QueuedConnection)
        old.chunk.emit("content", "旧回复")
        old.finish()
        current = self.start()
        self.assertIsNot(old, current)
        APP.processEvents()
        self.assertEqual(self.runtime.engine.turn_id, 2)
        self.assertTrue(self.runtime.engine.turn_open)
        self.assertEqual(self.chat.cur_reply, [])
        self.assertFalse(self.chat._reply_done)
        current.chunk.emit("content", "当前回复")
        current.finish()
        self.assertEqual(self.chat.cur_reply, ["当前回复"])
        self.assertEqual(self.runtime.engine.activity, "done")

    def test_old_feedback_deadline_does_not_clear_next_reply(self):
        first = self.start()
        first.chunk.emit("content", "完成")
        first.finish()
        self.clock[0] = 1
        self.start()
        self.clock[0] = 2
        self.runtime.tick()
        self.assertEqual(self.runtime.engine.activity, "thinking")
        self.assertTrue(self.runtime.engine.turn_open)

    def test_focus_quiet_retains_work_and_recovers_without_old_action(self):
        worker = self.start()
        self.runtime.send("action.request", action="greet", source="user")
        task = self.store.create_task("合成专注", seconds=60, kind="focus")
        self.pet.companion.tick()
        self.assertEqual(self.runtime.engine.snapshot()["layer"], "quiet")
        worker.chunk.emit("content", "安静期间的进度")
        self.store.task_action(task["id"], "complete")
        self.pet.companion.tick()
        self.assertEqual(self.runtime.engine.activity, "replying")
        self.assertEqual(self.runtime.engine.snapshot()["layer"], "activity")
        self.assertIsNone(self.runtime.engine.active)

    def test_runtime_emits_changes_only_and_rejects_events_after_close(self):
        changed = Mock()
        self.runtime.changed.connect(changed)
        self.clock[0] = 1
        self.runtime.tick()
        changed.assert_not_called()
        self.runtime.send("mood.set", mood="happy")
        self.assertEqual(changed.call_count, 1)
        self.runtime.close()
        self.assertFalse(self.runtime.timer.isActive())
        self.assertFalse(self.runtime.send("action.request", action="greet", source="user")["accepted"])
        self.assertEqual(self.runtime.engine.snapshot()["layer"], "closed")

    def test_semantic_intent_publishes_one_snapshot_and_invalid_payload_is_atomic(self):
        self.start()
        changed = Mock()
        self.runtime.changed.connect(changed)
        result = self.runtime.reply_intent(1, {"mood": "happy", "action": "greet"})
        self.assertTrue(all(row["accepted"] for row in result))
        self.assertEqual(changed.call_count, 1)
        state = self.runtime.engine.snapshot()
        with self.assertRaises(ValueError):
            self.runtime.reply_intent(1, {"mood": "sad", "action": "run_python"})
        self.assertEqual(self.runtime.engine.snapshot(), state)

    def test_user_clicks_preempt_reply_actions_but_hover_does_not(self):
        import pet as P
        bare = SimpleNamespace(performance=self.runtime)
        self.start()
        self.runtime.send("action.request", action="greet", source="reply", turn_id=1)
        P.PetWindow._request_presentation_action(bare, "react", "Flick", 1)
        self.assertEqual(self.runtime.engine.active["source"], "reply")
        P.PetWindow._request_presentation_action(bare, "acknowledge", "Tap@Body", 3, source="user")
        self.assertEqual(self.runtime.engine.active["source"], "user")
