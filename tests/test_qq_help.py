"""Help is a document attachment, with no model cost or permission widening."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import qq_bridge as B
import qq_text as T
import qq_documents as D


class Help(unittest.TestCase):
    def setUp(self):
        self.document = D.help_document(ROOT)

    def test_help_is_public_and_does_not_query_state_or_paid_services(self):
        with patch.object(B.SCHEDULE, "command") as schedule, \
                patch.object(B.LT, "tavily_usage") as usage, \
                patch.object(B.QB, "_api") as api:
            for scene in ("group", "c2c"):
                for owner in (True, False):
                    for text in ("/help", " /HELP ", "／help", "/帮助", "help"):
                        reply = B.command_reply(B._fake(scene=scene, content=text), {"is_owner": owner})
                        self.assertEqual(reply, self.document)
            schedule.assert_not_called()
            usage.assert_not_called()
            api.assert_not_called()

    def test_help_does_not_capture_normal_conversation_or_bare_slash(self):
        for text in ("/", "help me with this", "解释一下 /help"):
            self.assertIsNone(B.command_reply(B._fake(content=text), {"is_owner": False}))

    def test_malformed_slash_commands_return_static_usage(self):
        import thinking
        with patch.object(thinking, "load", return_value={}), \
                patch.object(B.LT, "tavily_usage") as usage:
            for text in ("/档位 不存在", "/档位 日常 多余参数", "/tavily 什么额度", "/版本 现在安装", "/helpful"):
                reply = B.command_reply(B._fake(content=text), {"is_owner": True})
                self.assertTrue(reply)
                self.assertIn("/", reply)
                clean, notes = T.sanitize(reply)
                self.assertEqual((clean, notes), (reply, []))
            self.assertEqual(B.command_reply(B._fake(content="/help 档位"), {}), self.document)
            self.assertIsNone(B.command_reply(B._fake(content="档位是什么意思"), {"is_owner": True}))
            usage.assert_not_called()

    def test_document_uses_attachment_without_text_sanitizer_or_preamble(self):
        with patch.object(D, "upload", return_value="fixture-file") as upload, \
                patch.object(B.QB, "reply_file", return_value={"id": "receipt"}) as send, \
                patch.object(B.QB, "reply") as text_send, \
                patch.object(T, "sanitize", side_effect=AssertionError("must preserve Markdown")), \
                patch.object(B, "log"):
            ev = B._fake(content="/help")
            receipt = B.Bridge()._send_platform(ev, self.document)
            self.assertTrue(receipt.accepted)
            upload.assert_called_once()
            send.assert_called_once_with(ev, "fixture-file")
            text_send.assert_not_called()

    def test_upload_failure_has_short_static_fallback_but_uncertain_send_is_not_retried(self):
        ev = B._fake(content="/help")
        with patch.object(D, "upload", side_effect=D.DocumentError("fixture")), \
                patch.object(B.QB, "reply", return_value={"id": "short"}) as send, \
                patch.object(B.QB, "reply_file") as file_send, patch.object(B, "log"):
            self.assertTrue(B.Bridge()._send_platform(ev, self.document).accepted)
            send.assert_called_once_with(ev, B.HELP_UNAVAILABLE)
            self.assertLess(len(B.HELP_UNAVAILABLE), 60)
            file_send.assert_not_called()
        with patch.object(D, "upload", return_value="file"), \
                patch.object(B.QB, "reply") as text_send, \
                patch.object(B.QB, "reply_file", return_value={"_error": "timeout"}) as file_send, \
                patch.object(B, "log"):
            self.assertEqual(B.Bridge()._send_platform(ev, self.document).status, "unknown")
            file_send.assert_called_once()
            text_send.assert_not_called()

    def test_expired_help_does_not_start_upload(self):
        with patch.object(D, "upload") as upload:
            receipt = B.Bridge()._send_platform(B._fake(secs_ago=240), self.document)
            self.assertEqual(receipt.status, "skipped")
            upload.assert_not_called()

    def test_missing_document_returns_only_short_fallback(self):
        with patch.object(D, "help_document", side_effect=FileNotFoundError), patch.object(B, "log"):
            self.assertEqual(B.command_reply(B._fake(content="/help"), {}), B.HELP_UNAVAILABLE)

    def test_menu_entries_and_available_levels_are_documented(self):
        panel = json.loads((ROOT / "templates/qq_group_panel.json").read_text(encoding="utf-8"))
        for item in panel["panel"]["items"]:
            self.assertIn(item["name"], self.document.text)
        for name in ("自动", "省电", "日常", "认真", "深究", "极限", "雷霆"):
            self.assertIn(name, self.document.text)
            self.assertIn(name, B.LEVEL_ALIAS)

    def test_guest_still_cannot_execute_management_commands(self):
        with patch.object(B.LT, "tavily_usage") as usage:
            for text in ("/符卡 开启", "/定时任务 列表", "/档位 日常", "/tavily", "/版本"):
                reply = B.command_reply(B._fake(content=text), {"is_owner": False})
                self.assertTrue("仅主人" in reply or "只有他" in reply)
            usage.assert_not_called()

    def test_missing_thinking_preset_cannot_report_a_successful_switch(self):
        import thinking
        cfg = {"current": "deep", "presets": {"deep": {"name": "深究"}}}
        with patch.object(thinking, "load", return_value=cfg), \
                patch.object(thinking, "set_level") as local_write, \
                patch.object(B, "set_group_level") as group_write:
            for scene in ("group", "c2c"):
                reply = B.command_reply(B._fake(scene=scene, content="/档位 雷霆"), {"is_owner": True})
                self.assertIn("未配置", reply)
                self.assertIn("未修改设置", reply)
            group_write.assert_not_called()
            local_write.assert_not_called()

    def test_thinking_query_lists_only_configured_choices(self):
        import thinking
        with patch.object(thinking, "load", return_value={"current": "deep", "presets": {"deep": {"name": "深究"}}}):
            reply = B.command_reply(B._fake(scene="c2c", content="/档位"), {"is_owner": True})
            self.assertIn("深究", reply)
            self.assertNotIn("雷霆", reply)

    def test_commands_skip_attachments_model_and_conversation_history(self):
        import brain
        bridge = B.Bridge()
        who = {"is_owner": False, "name": "fixture", "id": "fixture"}
        with patch.object(B, "hist_append") as history, patch.object(B, "log"), \
                patch.object(B, "read_images") as images, patch.object(B, "read_documents") as documents, \
                patch.object(bridge, "_send", return_value=B.DeliveryReceipt("accepted", "document", {})) as send, \
                patch.object(bridge, "_remember") as remember, \
                patch.object(brain, "ask_with_system") as ask, patch.object(brain, "api_key") as key:
            for content in ("/help", "/档位 不存在", "/版本 多余参数", "/不存在的指令"):
                ev = B._fake(content=content)
                ev.attachments = [{"url": "https://example.invalid/fixture.png"}]
                bridge._process_scoped(ev, time.time(), who)
            self.assertEqual(send.call_count, 4)
            self.assertEqual(send.call_args_list[0].args[1], self.document)
            history.assert_not_called()
            images.assert_not_called()
            documents.assert_not_called()
            ask.assert_not_called()
            key.assert_not_called()
            remember.assert_not_called()

    def test_receive_only_mode_does_not_send_help(self):
        bridge = B.Bridge(reply_enabled=False)
        with patch.object(bridge, "_send") as send, patch.object(B, "log"):
            bridge._process_scoped(B._fake(content="/help"), time.time(), {"name": "fixture", "is_owner": False})
            send.assert_not_called()

    def test_successful_state_query_does_not_become_model_history(self):
        import brain
        bridge = B.Bridge()
        with patch.object(B.LT, "tavily_usage", return_value="可用额度：fixture") as usage, \
                patch.object(B, "hist_append") as history, patch.object(B, "log"), \
                patch.object(bridge, "_send") as send, patch.object(brain, "ask_with_system") as ask:
            ev = B._fake(content="/tavily")
            bridge._process_scoped(ev, time.time(), {"name": "fixture", "is_owner": True})
            usage.assert_called_once()
            send.assert_called_once_with(ev, "可用额度：fixture")
            history.assert_not_called()
            ask.assert_not_called()

    def test_unmentioned_group_help_stays_background(self):
        ev = B._fake(content="/help")
        ev.kind = "group_message"
        with patch.object(B, "identify", return_value={"name": "fixture"}), \
                patch.object(B, "hist_append"), patch.object(B, "command_reply") as command, \
                patch.object(B.QB, "reply") as reply:
            B.Bridge().handle(ev)
            command.assert_not_called()
            reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
