"""Search configuration drafts and honest integration status, using fake keys."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox, QPushButton
import search_api as S
import settings_data as D
from settings_ui import SettingsDialog


APP = QApplication.instance() or QApplication([])
PROJECT = Path(__file__).resolve().parents[1]


class SearchSettingsUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        for name in ("config.example.json", "thinking.example.json"):
            shutil.copyfile(PROJECT / "data" / name, self.root / "data" / name)
        D.save_secrets(self.root, {"deepseek_api_key": "fixture", "tavily_api_key": "existing-tavily",
                                  "qq_secret": "keep", "unknown": {"keep": 1}})
        D.save_config(self.root, {"tools": {"search_backend": "ddgs", "tavily_key": "legacy-private-key",
            "tavily": {"enabled": True, "daily_limit": 7, "monthly_limit": 77, "timeout_seconds": 9}}})
        self.dialog = SettingsDialog(self.root)

    def tearDown(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.dialog.close()
        self.dialog.deleteLater(); APP.processEvents()
        self.temp.cleanup()

    def field(self, trail):
        return next(w for doc, path, w, *_ in self.dialog._fields if doc == "config" and path == trail)

    def test_view_switching_is_noop_keys_masked_and_legacy_key_not_exposed(self):
        d = self.dialog
        before = {p: p.read_bytes() for p in (self.root / "data").iterdir()}
        for index in range(3):
            d.search_provider_combo.setCurrentIndex(index)
            key = d.search_provider_combo.currentData()
            self.assertFalse(d._search_panels[key].isHidden())
            self.assertTrue(all(panel.isHidden() for name, panel in d._search_panels.items() if name != key))
            self.assertEqual(d.search_key_edits[key].echoMode(), QLineEdit.Password)
            self.assertEqual(d._collect(validate=True), d.session.original)
        d.save_all()
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / "data").iterdir()})
        self.assertFalse((self.root / "data/settings_backups").exists())
        self.assertNotIn("legacy-private-key", [w.text() for w in d.findChildren(QLineEdit)])

    def test_switches_preserve_drafts_save_only_edited_keys_and_keep_tavily(self):
        d = self.dialog
        original_config = (self.root / "data/config.json").read_bytes()
        before = deepcopy(d.session.original)
        d.search_provider_combo.setCurrentIndex(1)
        d.search_key_edits["brave"].setText("new-brave")
        d.search_provider_combo.setCurrentIndex(2)
        d.search_key_edits["serper"].setText("new-serper")
        d.search_provider_combo.setCurrentIndex(1)
        self.assertEqual(d.search_key_edits["brave"].text(), "new-brave")
        expected = deepcopy(before)
        expected["secrets"].update(brave_search_api_key="new-brave", serper_api_key="new-serper")
        self.assertEqual(d._collect(validate=True), expected)
        with patch("search_api.search") as search:
            d.save_all()
        search.assert_not_called()
        self.assertEqual((self.root / "data/config.json").read_bytes(), original_config)
        self.assertEqual(D.load_secrets(self.root), expected["secrets"])
        self.assertTrue(list((self.root / "data/settings_backups").glob("*/secrets.json")))
        self.assertEqual(d.search_key_edits["brave"].echoMode(), QLineEdit.Password)
        self.assertEqual(d._collect(), d.session.original)

    def test_configuration_contract_and_limits_are_validated_without_network(self):
        d = self.dialog
        timeout = self.field(("tools", "search_apis", "brave", "timeout_seconds"))
        timeout.setValue(5)
        values = d._collect(validate=True)
        self.assertEqual(values["config"]["tools"]["search_apis"], {"brave": {"timeout_seconds": 5}})
        self.assertEqual(values["config"]["tools"]["tavily"], d.session.original["config"]["tools"]["tavily"])
        timeout.setValue(0)
        with self.assertRaisesRegex(ValueError, "1–20"):
            d._collect(validate=True)
        timeout.setValue(12)
        daily = self.field(("tools", "tavily", "daily_limit"))
        daily.setValue(101)
        with self.assertRaisesRegex(ValueError, "0–100"):
            d._collect(validate=True)
        daily.setValue(7)
        d.search_key_edits["serper"].setText("pasted wrong key")
        with self.assertRaisesRegex(ValueError, "API key"):
            d._collect(validate=True)

    def test_guidance_distinguishes_network_coverage_and_pending_services(self):
        d = self.dialog
        text = "\n".join(label.text() for label in d.findChildren(QLabel))
        for expected in ("境内直连", "已有代理或自建 SearXNG", "第三方", "不需要填写模型名称"):
            self.assertIn(expected, text)
        for provider in ("brave", "serper"):
            panel = d._search_panels[provider]
            text = "\n".join(label.text() for label in panel.findChildren(QLabel))
            self.assertIn("当前对话尚不会使用此服务", text)
            self.assertIn(S.PROVIDERS[provider].endpoint, text)
            account, docs = panel.findChildren(QPushButton)
            with patch("settings_ui.QDesktopServices.openUrl") as open_url:
                account.click(); docs.click()
            self.assertEqual([call.args[0].toString() for call in open_url.call_args_list],
                             [S.PROVIDERS[provider].account_url, S.PROVIDERS[provider].docs_url])
