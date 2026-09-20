import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qq_bridge as QB
import memory as M
import thinking as T
import app_entry


class DeliveryBoundaries(unittest.TestCase):
    def setUp(self):
        self.bridge = QB.Bridge(reply_enabled=True)
        self.event = QB._fake(scene="c2c", oid="U1")

    def test_network_error_is_unknown_and_not_accepted(self):
        with patch.object(QB.QB, "reply", return_value={"_error": "timeout"}):
            receipt = self.bridge._send(self.event, "实际发出的文本")
        self.assertEqual(receipt.status, "unknown")
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.text, "实际发出的文本")

    def test_platform_acceptance_returns_sanitized_text(self):
        with patch.object(QB.QB, "reply", return_value={"id": "m1"}):
            receipt = self.bridge._send(self.event, "你好")
        self.assertEqual(receipt.status, "accepted")
        self.assertEqual(receipt.text, "你好")

    def test_thinking_options_do_not_mutate_process_retrieval_config(self):
        before = deepcopy(M.CFG["retrieval"])
        options = T.apply_to_memory(level="deep", query="分析这个架构")
        self.assertEqual(M.CFG["retrieval"], before)
        self.assertIn("retrieval", options)
        self.assertGreater(options["retrieval"]["token_budget"], 0)

    def test_maintenance_commands_are_dispatched_without_starting_qt(self):
        with patch("maintenance.diagnose", return_value=7) as diagnose:
            self.assertEqual(app_entry.dispatch(["diagnose"]), 7)
            diagnose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
