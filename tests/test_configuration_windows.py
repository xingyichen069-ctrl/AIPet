"""Window lifetimes and close guards, using disposable public configuration."""
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget
from shiboken6 import isValid

from configuration_windows import close_configuration_windows
from desktop_state import Appearance
from persona_ui import open_persona_manager
from settings_ui import open_settings
import settings_data as D


APP = QApplication.instance() or QApplication([])
PROJECT = Path(__file__).resolve().parents[1]


class ConfigurationWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        for name in ('config.example.json', 'thinking.example.json'):
            shutil.copyfile(PROJECT / 'data' / name, self.root / 'data' / name)
        shutil.copytree(PROJECT / 'persona_defaults', self.root / 'persona_defaults')
        D.save_secrets(self.root, {'deepseek_api_key': 'fixture'})
        self.appearance = Appearance(self.root)
        self.pet = QWidget(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.pet.appearance = self.appearance
        self.pet._persona_changed = Mock()
        self.pet.show()

    def tearDown(self):
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.assertTrue(close_configuration_windows(self.root))
        self.pet.close()
        self.pet.deleteLater()
        self.appearance.deleteLater()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temp.cleanup()

    def open(self):
        return open_settings(self.root, self.appearance, self.pet)

    def test_configuration_is_independent_and_nonmodal(self):
        d = self.open()
        self.assertEqual(d.windowTitle(), '配置 · 小日和')
        self.assertIsNone(d.parentWidget())
        self.assertFalse(d.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(d.isModal())
        self.assertIsNone(APP.activeModalWidget())
        self.assertTrue(self.pet.isEnabled())

    def test_reopening_restores_minimized_window_and_keeps_draft(self):
        d = self.open()
        d.api_key_edit.setText('unsaved fixture')
        d.showMinimized()
        self.assertIs(self.open(), d)
        self.assertFalse(d.isMinimized())
        self.assertEqual(d.api_key_edit.text(), 'unsaved fixture')
        self.assertEqual(D.load_secrets(self.root)['deepseek_api_key'], 'fixture')

    def test_close_deletes_old_window_and_reopen_reads_current_files(self):
        d = self.open()
        self.assertTrue(d.close())
        D.save_secrets(self.root, {'deepseek_api_key': 'external fixture'})
        new = self.open()  # Reopen before the old deleteLater event arrives.
        self.assertIsNot(new, d)
        self.assertEqual(new.api_key_edit.text(), 'external fixture')
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertFalse(isValid(d))
        self.assertIs(self.open(), new)
        self.appearance.changed.emit()  # No callbacks to the deleted UI.

    def test_titlebar_close_can_be_cancelled_and_does_not_write(self):
        d = self.open()
        original = (self.root / 'data/secrets.json').read_bytes()
        d.api_key_edit.setText('unsaved fixture')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question:
            self.assertFalse(d.close())
        self.assertTrue(d.isVisible())
        self.assertEqual(question.call_args.args[-1], QMessageBox.No)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.assertTrue(d.close())
        self.assertEqual((self.root / 'data/secrets.json').read_bytes(), original)

    def test_escape_and_accept_use_same_draft_guard(self):
        d = self.open()
        d.api_key_edit.setText('unsaved fixture')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question:
            QTest.keyClick(d, Qt.Key_Escape)
            self.assertTrue(d.isVisible())
            d.accept()
            self.assertTrue(d.isVisible())
        self.assertEqual(question.call_count, 2)

    def test_enter_in_field_does_not_trigger_reload_or_api_actions(self):
        d = self.open()
        d.reload_all()
        clicked = Mock()
        for button in d.findChildren(QPushButton):
            button.clicked.connect(clicked)
        d.api_key_edit.setFocus()
        QTest.keyClick(d.api_key_edit, Qt.Key_Return)
        clicked.assert_not_called()
        self.assertTrue(d.isVisible())

    def test_persona_entry_points_share_one_independent_window(self):
        d = self.open()
        p = d._open_persona()
        p.editor.appendPlainText('Unsaved fixture draft')
        p.showMinimized()
        same = open_persona_manager(self.pet, root=self.root, on_changed=self.pet._persona_changed)
        self.assertIs(same, p)
        self.assertFalse(p.isMinimized())
        self.assertIsNone(p.parentWidget())
        self.assertFalse(p.isModal())
        self.assertIn('Unsaved fixture draft', p.editor.toPlainText())
        self.assertTrue(d.close())
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(p.isVisible())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.assertFalse(p.close())
        p._save()
        self.pet._persona_changed.assert_called_once()
        self.assertTrue(p.close())

    def test_application_exit_respects_unsaved_persona(self):
        p = open_persona_manager(self.pet, root=self.root)
        p.editor.appendPlainText('Unsaved fixture draft')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.assertFalse(close_configuration_windows(self.root))
        self.assertTrue(p.isVisible())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.assertTrue(close_configuration_windows(self.root))

    def test_probe_close_and_quick_retry_wait_for_queued_completion(self):
        d = self.open()
        release = threading.Event()

        def models(_connection):
            release.wait(3)
            return ['deepseek-flash']

        with patch('settings_ui.PC.list_models', side_effect=models) as request:
            d._check_api('models')
            worker = d._api_probe
            try:
                self.assertFalse(d.close())
                self.assertFalse(close_configuration_windows(self.root))
                self.assertTrue(d.isVisible())
                release.set()
                self.assertTrue(worker.wait(3000))
                self.assertTrue(d._running())  # Completion is still queued.
                d._check_api('models')
                self.assertIs(d._api_probe, worker)
                QTest.qWait(50)
                self.assertFalse(d._running())
                self.assertTrue(d.tabs.isEnabled())
                self.assertTrue(d.close())
                request.assert_called_once()
            finally:
                release.set()
                if isValid(worker):
                    worker.wait(3000)
                APP.processEvents()

    def test_pet_entry_points_and_exit_use_window_guards(self):
        from pet import PetWindow
        self.pet.chat = self.pet.prober = self.pet.updater = None
        self.pet._stop_live2d = Mock()
        with patch('pet.ROOT', self.root), patch('pet.QApplication.quit') as quit_app:
            d = PetWindow._open_settings(self.pet)
            self.assertIs(PetWindow._open_settings(self.pet), d)
            p = PetWindow._open_persona_manager(self.pet)
            self.assertIs(d._open_persona(), p)
            d.api_key_edit.setText('unsaved fixture')
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
                PetWindow.quit_safely(self.pet)
            quit_app.assert_not_called()
            self.assertFalse(getattr(self.pet, '_quitting', False))
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
                PetWindow.quit_safely(self.pet)
            quit_app.assert_called_once()
            self.assertFalse(d.isVisible())
            self.assertFalse(p.isVisible())
