import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
import settings_data as D
import provider_config as P
from desktop_state import Appearance
from settings_ui import SettingsDialog
from persona_ui import PersonaDialog

APP = QApplication.instance() or QApplication([])
PROJECT = Path(__file__).resolve().parents[1]


class SettingsUiTests(unittest.TestCase):
    def setUp(self):
        self.warning = patch('settings_ui.QMessageBox.warning', side_effect=lambda *args: self.fail(str(args[2])))
        self.warning.start()
        self.addCleanup(self.warning.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'data').mkdir()
        for name in ('config.example.json', 'thinking.example.json'):
            shutil.copyfile(PROJECT/'data'/name, self.root/'data'/name)
        shutil.copytree(PROJECT/'persona_defaults', self.root/'persona_defaults')
        D.save_secrets(self.root, {'deepseek_api_key': 'fixture', 'qq_secret': 'preserve'})
        self.appearance = Appearance(self.root)
        self.dialog = SettingsDialog(self.root, self.appearance)

    def tearDown(self):
        with patch('settings_ui.QMessageBox.question', return_value=QMessageBox.Yes): self.dialog.close()
        self.dialog.deleteLater(); self.appearance.deleteLater(); APP.processEvents()
        self.temp.cleanup()

    def test_noop_and_masked_keys(self):
        self.assertEqual(self.dialog.tabs.count(), 7)
        self.assertEqual(self.dialog.api_key_edit.echoMode(), QLineEdit.Password)
        self.assertEqual(self.dialog._collect(), self.dialog.session.original)
        self.dialog.save_all()
        self.assertFalse((self.root/'data/settings_backups').exists())
        self.assertFalse((self.root/'persona').exists())

    def test_drafts_and_model_validation(self):
        d = self.dialog
        d.preset_combo.setCurrentIndex(0)
        key = d.preset_combo.currentData()
        d.model_edit.setText('deepseek-v4-pro')
        d.preset_combo.setCurrentIndex(1); d.preset_combo.setCurrentIndex(0)
        self.assertEqual(d.model_edit.text(), 'deepseek-v4-pro')
        values = d._collect(validate=True)
        self.assertEqual(P.resolve(values['secrets'], values['thinking']['presets'][key]['params']).model, 'deepseek-v4-pro')
        d.model_combo.setCurrentText('mistyped-model')
        with self.assertRaisesRegex(ValueError, '尚未核实'): d._collect(validate=True)
        d.model_combo.setCurrentText('deepseek-flash')
        d.api_key_edit.setText('replacement')
        d.save_all()
        self.assertEqual(D.load_secrets(self.root)['qq_secret'], 'preserve')
        self.assertEqual(D.load_secrets(self.root)['model_profiles']['legacy']['api_key'], 'replacement')

    def test_legacy_different_models_survive_migration(self):
        d = self.dialog
        original = d.session.original['thinking']['presets']
        keys = list(original)
        original[keys[1]]['params']['model'] = 'deepseek::deepseek-v4-pro'
        d.session.values['thinking'] = json.loads(json.dumps(d.session.original['thinking']))
        d._build_pages()
        d.api_key_edit.setText('replacement')
        values = d._collect(validate=True)
        self.assertEqual(P.resolve(values['secrets'], values['thinking']['presets'][keys[1]]['params']).model, 'deepseek-v4-pro')

    def test_list_can_fill_empty_vision_model_and_custom_id_requires_proof(self):
        d = self.dialog
        d.vision_edits['vision_base_url'].setText('https://example.invalid/v1')
        d.vision_edits['vision_api_key'].setText('fixture')
        c = P.vision_connection({k: d._value(w) for k,w in d.vision_edits.items()}, listing=True)
        d._probe_done(c, 'models', True, True, ['exact-vision-id'])
        self.assertEqual(d.vision_edits['vision_model'].findText('exact-vision-id'), 0)
        d.vision_edits['vision_model'].setCurrentText('exact-vision-id')
        d._collect(validate=True)
        d.vision_edits['vision_model'].setCurrentText('wrong')
        with self.assertRaises(ValueError): d._collect(validate=True)

    def test_connection_mode_does_not_expose_or_mutate_connection_configuration(self):
        path = self.root/'data/qq_remote.json'
        path.write_text('{"enabled": true, "private": "preserve"}', encoding='utf-8')
        raw = path.read_bytes()
        self.dialog._build_pages()
        trails = [trail for doc,trail,*_ in self.dialog._fields if doc == 'secrets']
        self.assertNotIn(('qq_secret',), trails)
        self.dialog.font_spin.setValue(15)
        self.dialog.save_all()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(D.load_secrets(self.root)['qq_secret'], 'preserve')

    def test_persona_and_mood_drafts_survive_switching_and_save_is_noop(self):
        d = PersonaDialog(root=self.root, appearance=self.appearance)
        try:
            before = {p: p.read_bytes() for p in (self.root/'persona').rglob('*') if p.is_file()}
            d._save()
            self.assertEqual(before, {p: p.read_bytes() for p in (self.root/'persona').rglob('*') if p.is_file()})
            pid = d.current_id
            original = d.editor.toPlainText()
            d.editor.setPlainText(original + '\nAdded fixture preference.\n')
            d.mood_combo.setCurrentText(pid)
            moods = json.loads(d.mood_editor.toPlainText())
            first = next(iter(moods)); moods[first]['voice'] = 'fixture edit'
            d.mood_editor.setPlainText(json.dumps(moods, ensure_ascii=False))
            other = next(x for x in d.manager.mood_profiles() if x != pid)
            d.mood_combo.setCurrentText(other); d.mood_combo.setCurrentText(pid)
            self.assertEqual(json.loads(d.mood_editor.toPlainText())[first]['voice'], 'fixture edit')
            row = d.list.currentRow()
            d.list.setCurrentRow((row+1) % d.list.count()); d.list.setCurrentRow(row)
            self.assertIn('Added fixture preference.', d.editor.toPlainText())
            with patch('persona_ui.QMessageBox.information') as info: d._save()
            info.assert_not_called()
            self.assertIn('Added fixture preference.', d.manager.read_soul(pid))
            self.assertEqual(json.loads(d.manager.read_moods(pid))[first]['voice'], 'fixture edit')
        finally:
            with patch('persona_ui.QMessageBox.question', return_value=QMessageBox.Yes): d.close()
            d.deleteLater(); APP.processEvents()
