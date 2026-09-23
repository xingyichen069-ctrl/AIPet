from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import settings_data as D
from desktop_state import Appearance
from settings_ui import SettingsDialog


APP = QApplication.instance() or QApplication([])


class SettingsUiTests(unittest.TestCase):
    def test_settings_centre_round_trips_persona_api_and_thinking(self):
        project = Path(__file__).resolve().parents[1]
        work = project / "work"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as name:
            root = Path(name)
            (root / "data").mkdir()
            shutil.copyfile(project / "data" / "config.example.json", root / "data" / "config.example.json")
            shutil.copyfile(project / "data" / "thinking.json", root / "data" / "thinking.json")
            appearance = Appearance(root)
            dialog = SettingsDialog(root, appearance)
            self.assertEqual(dialog.tabs.count(), 5)
            self.assertEqual(dialog.persona_tabs.count(), 3)

            dialog._persona_editors["SOUL.md"].setPlainText("# 新人格\n")
            dialog.api_key_edit.setText("sk-settings-test")
            dialog.base_url_edit.setText("https://example.invalid/v1")
            dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData("daily"))
            dialog.model_edit.setText("deepseek::test-model")
            dialog.fs_root_edit.setText("D:/资料")
            dialog.proxy_edit.setText("direct")
            with patch("settings_ui.QMessageBox.information"):
                dialog.save_all()

            self.assertEqual(D.read_persona(root, "SOUL.md"), "# 新人格\n")
            secrets = D.load_secrets(root)
            self.assertEqual(secrets["deepseek_api_key"], "sk-settings-test")
            self.assertEqual(secrets["deepseek_base_url"], "https://example.invalid/v1")
            config = D.load_config(root)
            self.assertEqual(config["tools"]["fs_root"], "D:/资料")
            self.assertEqual(config["tools"]["proxy"], "direct")
            thinking = D.load_thinking(root)
            self.assertEqual(thinking["presets"]["daily"]["params"]["model"], "deepseek::test-model")
            dialog.close()
            appearance.deleteLater()


if __name__ == "__main__":
    unittest.main()
