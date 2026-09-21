from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from harness.core import HarnessError, validate_plugin
from harness.editor import GuidedEditor, OpenAICompatClient, _parse_json


ENTRYPOINT = """import json
import sys

def health():
    return {"status": "ok", "plugin": "guided-fixture", "version": "1.0.0"}

def main():
    result = health()
    if "--selftest" in sys.argv:
        assert set(result) == {"status", "plugin", "version"}
        result["selftest"] = "passed"
    elif "--health" not in sys.argv:
        raise SystemExit(2)
    print(json.dumps(result))

if __name__ == "__main__":
    main()
"""


def response(*, status="ready", files=None):
    return json.dumps({
        "status": status,
        "reply": "已生成草案。",
        "plugin": {
            "name": "guided-fixture",
            "version": "1.0.0",
            "display_name": "Guided Fixture",
            "entrypoint": "entrypoint.py",
            "files": files if files is not None else [{"path": "entrypoint.py", "content": ENTRYPOINT}],
        } if status == "ready" else None,
    })


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages):
        self.calls.append(list(messages))
        return self.answers.pop(0)


class EditorTests(unittest.TestCase):
    def test_parse_json_accepts_code_fence(self):
        self.assertEqual(_parse_json("```json\n{\"status\": \"needs_clarification\"}\n```"),
                         {"status": "needs_clarification"})

    def test_conversation_keeps_previous_turn(self):
        client = FakeClient(response(status="needs_clarification"), response())
        editor = GuidedEditor(client)
        first = editor.turn("做一个插件")
        second = editor.turn("补充：离线自检")
        self.assertEqual(first.status, "needs_clarification")
        self.assertTrue(second.ready)
        self.assertEqual(len(client.calls[1]), 4)
        self.assertEqual(client.calls[1][1]["content"], "做一个插件")

    def test_materialize_and_apply_use_normal_lifecycle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            editor = GuidedEditor(FakeClient(response()))
            draft = editor.turn("生成一个离线 hello 插件")
            source = root / "draft"
            app = root / "app"
            result = editor.apply(draft, source, app)
            self.assertEqual(result["installation"]["status"], "activated")
            self.assertEqual(validate_plugin(source).name, "guided-fixture")
            registry = json.loads((app / "data/harness/registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["active"]["guided-fixture"]["version"], "1.0.0")

    def test_materialize_rejects_traversal_from_model(self):
        bad = response(files=[{"path": "../escape.py", "content": "print(1)"}])
        editor = GuidedEditor(FakeClient(bad))
        with self.assertRaises(HarnessError):
            editor.turn("写一个插件")

    def test_draft_rejects_credential_shaped_text(self):
        bad = response(files=[{"path": "entrypoint.py", "content": ENTRYPOINT + "\n# sk-example_1234567890\n"}])
        editor = GuidedEditor(FakeClient(bad))
        with self.assertRaises(HarnessError):
            editor.turn("写一个插件")

    def test_client_normalizes_endpoint_and_requires_key(self):
        with self.assertRaises(HarnessError):
            OpenAICompatClient("https://example.invalid/v1", "")
        client = OpenAICompatClient("https://example.invalid/api/v1/", "test-key", "qwen")
        self.assertEqual(client.endpoint, "https://example.invalid/api/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
