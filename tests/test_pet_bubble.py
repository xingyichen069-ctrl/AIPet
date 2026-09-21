"""Exercise actual window moves without loading a model or personal data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QWidget
from pet import BubbleWindow, PetWindow

APP = QApplication.instance() or QApplication([])


class BarePet(PetWindow):
    def __init__(self):
        QWidget.__init__(self)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(160, 200)
        self.bubble_win = BubbleWindow(self)

    def paintEvent(self, event):
        pass


class BubbleFollowing(unittest.TestCase):
    def setUp(self):
        self.pet = BarePet()
        self.pet.move(220, 230)
        self.pet.show()
        self.bubble = self.pet.bubble_win
        self.bubble.show_text('在这里。', 10000)
        APP.processEvents()

    def tearDown(self):
        for window in (self.bubble, self.pet):
            window.hide()
            window.deleteLater()
        APP.processEvents()

    def test_each_move_tracks_before_drag_release(self):
        # No mouse release / drag_finished signal is sent between these moves.
        for delta in (QPoint(30, 20), QPoint(-15, 10), QPoint(10, -25)):
            old = self.bubble.pos()
            self.pet.move(self.pet.pos() + delta)
            APP.processEvents()
            self.assertEqual(self.bubble.pos(), old + delta)

    def test_hidden_bubble_stays_hidden_and_anchors_when_shown(self):
        self.bubble.hide()
        self.pet.move(300, 310)
        APP.processEvents()
        self.assertFalse(self.bubble.isVisible())
        self.bubble.show_text('跟过来了。')
        self.assertEqual(self.bubble.x(), self.pet.frameGeometry().center().x() - self.bubble.width() // 2)

    def test_bubble_uses_pet_monitor_and_stays_in_bounds(self):
        bounds = QRect(1000, 0, 900, 700)
        monitor = Mock()
        monitor.availableGeometry.return_value = bounds
        with patch.object(QApplication, 'screenAt', return_value=monitor) as screen_at:
            for position in (QPoint(1100, 230), QPoint(1000, 0), QPoint(1840, 640)):
                self.pet.move(position)
                APP.processEvents()
                self.bubble.reposition()
                screen_at.assert_called_with(self.pet.frameGeometry().center())
                self.assertTrue(bounds.contains(self.bubble.frameGeometry()))


if __name__ == '__main__':
    unittest.main()
