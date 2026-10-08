import json
from pathlib import Path
import tempfile
import unittest

import settings_data as S


class SettingsDataTests(unittest.TestCase):
    def test_persona_and_secret_writes_are_utf8_and_atomic(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            S.write_persona(root, "SOUL.md", "她会说：在。\n")
            self.assertEqual(S.read_persona(root, "SOUL.md"), "她会说：在。\n")
            self.assertEqual(S.read_persona(root, "BOUNDARIES.md"), "")

            S.save_secrets(root, {"deepseek_api_key": "sk-test", "qq_appid": "bot"})
            self.assertEqual(S.load_secrets(root)["qq_appid"], "bot")
            self.assertEqual(json.loads(S.secrets_path(root).read_text(encoding="utf-8"))["deepseek_api_key"],
                             "sk-test")
            self.assertFalse(list((root / "data").glob(".*secrets.json.*")))

    def test_thinking_save_preserves_the_whole_object(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            original = {"current": "daily", "auto_rules": {"thresholds": {"daily": 25}},
                        "presets": {"daily": {"params": {"model": "old"}}}}
            S.save_thinking(root, original)
            loaded = S.load_thinking(root)
            loaded["presets"]["daily"]["params"]["model"] = "new"
            S.save_thinking(root, loaded)
            self.assertEqual(S.load_thinking(root)["auto_rules"]["thresholds"]["daily"], 25)
            self.assertEqual(S.load_thinking(root)["presets"]["daily"]["params"]["model"], "new")


if __name__ == "__main__":
    unittest.main()

class SessionTests(unittest.TestCase):
    def test_noop_is_read_only_and_merge_preserves_unedited_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            S.save_secrets(root, {'api_key': 'old', 'qq_secret': 'keep', 'unknown': {'x': 1}})
            session = S.SettingsSession(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(session.save(), [])
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            session.values['secrets']['api_key'] = 'new'
            S.save_secrets(root, {'api_key': 'old', 'qq_secret': 'concurrent', 'unknown': {'x': 2}})
            self.assertEqual(session.save(), ['secrets.json'])
            self.assertEqual(S.load_secrets(root), {'api_key': 'new', 'qq_secret': 'concurrent', 'unknown': {'x': 2}})
            self.assertTrue(list((root/'data/settings_backups').glob('*/secrets.json')))

    def test_same_field_conflict_and_corrupt_json_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            S.save_config(root, {'tools': {'proxy': 'auto'}})
            session = S.SettingsSession(root)
            session.values['config']['tools']['proxy'] = 'off'
            S.save_config(root, {'tools': {'proxy': 'http://localhost:1'}})
            with self.assertRaises(ValueError): session.save()
            self.assertEqual(S.load_config(root)['tools']['proxy'], 'http://localhost:1')
            S.config_path(root).write_text('{broken', encoding='utf-8')
            with self.assertRaises(ValueError): session.save()
            self.assertEqual(S.config_path(root).read_text(encoding='utf-8'), '{broken')

    def test_failed_second_write_restores_first_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            S.save_config(root, {'x': 1}); S.save_secrets(root, {'key': 'old'})
            session = S.SettingsSession(root)
            session.values['config']['x'] = 2
            session.values['secrets']['key'] = 'new'
            original = S._write_atomic
            def fail(path, text):
                if path.name == 'secrets.json': raise OSError('simulated full disk')
                original(path, text)
            with patch.object(S, '_write_atomic', side_effect=fail), self.assertRaises(OSError): session.save()
            self.assertEqual(S.load_config(root), {'x': 1})
            self.assertEqual(S.load_secrets(root), {'key': 'old'})

    def test_persona_transaction_validates_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root/'SOUL.md', root/'PROFILE.md'
            a.write_text('a', encoding='utf-8'); b.write_text('external', encoding='utf-8')
            with self.assertRaises(ValueError): S.save_texts(root, {a: ('a', 'new'), b: ('old', 'new')})
            self.assertEqual(a.read_text(encoding='utf-8'), 'a')
