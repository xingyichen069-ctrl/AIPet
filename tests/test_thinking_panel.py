"""Exercise panel dragging without changing the user's settings."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
import pet as P

APP = QApplication.instance() or QApplication([])


class PanelDragging(unittest.TestCase):
    def setUp(self):
        work = Path(os.environ.get('AIPET_TEST_WORK', Path(__file__).resolve().parents[1] / 'work'))
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.state_patch = patch.object(P, 'STATE_FILE', Path(self.temp.name) / 'state.json')
        self.state_patch.start()
        self.character = QWidget()
        self.character.state = P.load_state()
        self.character.on_level_changed = Mock()
        self.panel = P.ThinkingPanel(self.character)
        self.panel.move(100, 100)
        self.panel.setWindowOpacity(1)
        self.panel.show()
        APP.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.character.deleteLater()
        APP.processEvents()
        self.state_patch.stop()
        self.temp.cleanup()

    def drag(self, local, delta):
        start = self.panel.mapToGlobal(local)
        for kind, global_pos, button, buttons in (
            (QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, start + delta, Qt.NoButton, Qt.LeftButton),
            (QEvent.MouseButtonRelease, start + delta, Qt.LeftButton, Qt.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(self.panel.mapFromGlobal(global_pos)),
                                QPointF(global_pos), button, buttons, Qt.NoModifier)
            QApplication.sendEvent(self.panel, event)

    def test_drag_remains_independent_across_reopen_and_restart(self):
        old_character_position = self.character.pos()
        expected = self.panel.pos() + QPoint(100, 80)
        self.drag(QPoint(20, 20), QPoint(100, 80))
        self.assertEqual(self.panel.pos(), expected)
        self.assertEqual(self.character.pos(), old_character_position)
        self.character.move(400, 400)
        self.panel.reposition()
        self.assertEqual(self.panel.pos(), expected)
        self.panel.hide()
        self.panel.fade_in()
        self.assertEqual(self.panel.pos(), expected)
        self.character.state = P.load_state()
        reopened = P.ThinkingPanel(self.character)
        try:
            reopened.reposition()
            self.assertEqual(reopened.pos(), expected)
        finally:
            reopened.deleteLater()

    def test_button_click_still_selects_level_without_dragging(self):
        key, rect = self.panel._row_rects()[1]
        original = self.panel.pos()
        with patch.object(P.T, 'set_level') as select:
            self.drag(rect.center().toPoint(), QPoint(0, 0))
        select.assert_called_once_with(key)
        self.character.on_level_changed.assert_called_once_with(key)
        self.assertEqual(self.panel.pos(), original)
        self.assertNotIn('thinking_panel_position', self.character.state)

    def test_dragging_cannot_leave_panel_offscreen(self):
        self.drag(QPoint(20, 20), QPoint(-10000, -10000))
        area = APP.primaryScreen().availableGeometry()
        self.assertTrue(area.contains(self.panel.frameGeometry()))


if __name__ == '__main__':
    unittest.main()
