"""Tests for the shared click-versus-drag state machine."""

import sys
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gesture import DragGesture


class DragGestureTests(unittest.TestCase):
    def test_small_motion_stays_a_click(self):
        gesture = DragGesture(8)
        gesture.press(QPoint(100, 100))

        self.assertFalse(gesture.move(QPoint(105, 105)))
        self.assertFalse(gesture.release(QPoint(105, 105)))
        self.assertFalse(gesture.active)

    def test_threshold_crossing_starts_drag(self):
        gesture = DragGesture(8)
        gesture.press(QPoint(100, 100))

        self.assertTrue(gesture.move(QPoint(108, 100)))
        self.assertTrue(gesture.release(QPoint(120, 100)))
        self.assertFalse(gesture.active)

    def test_release_can_classify_a_jump_as_drag(self):
        gesture = DragGesture(8)
        gesture.press(QPoint(0, 0))

        self.assertTrue(gesture.release(QPoint(0, 8)))
        self.assertFalse(gesture.active)

    def test_new_press_clears_previous_drag_state(self):
        gesture = DragGesture(8)
        gesture.press(QPoint(0, 0))
        self.assertTrue(gesture.move(QPoint(8, 0)))

        gesture.press(QPoint(40, 40))
        self.assertFalse(gesture.dragging)
        self.assertFalse(gesture.release(QPoint(42, 42)))


if __name__ == "__main__":
    unittest.main()
