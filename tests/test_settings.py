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
