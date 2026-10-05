"""Full-group intake: explicit mentions reply, passive context never acts."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import qq_bot as Q
import qq_bridge as B


def payload(mid="M1", speaker="PERSON", **extra):
    return {"id": mid, "group_openid": "GROUP", "content": "亲亲",
            "author": {"id": speaker, "member_openid": speaker, "username": speaker},
            "timestamp": datetime.now(timezone.utc).isoformat(), **extra}


class FullGroupMessages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = Path(self.temp.name) / "history.json"
        for target, attr, value in ((B, "HIST_FILE", self.history), (Q, "log", lambda text: None)):
            p = patch.object(target, attr, value)
            p.start(); self.addCleanup(p.stop)

    def test_full_message_preserves_sender_and_stays_passive(self):
        ev = Q.parse_event("GROUP_MESSAGE_CREATE", payload(), {"BOT"})
        self.assertEqual((ev.kind, ev.speaker_id, ev.group_openid),
                         ("group_message", "PERSON", "GROUP"))

    def test_own_mention_is_a_request_but_another_bot_is_not(self):
        for mid, expected in (("BOT", "group_at"), ("OTHER_BOT", "group_message")):
            with self.subTest(mention=mid):
                ev = Q.parse_event("GROUP_MESSAGE_CREATE", payload(mentions=[{"id": mid, "bot": True}]), {"BOT"})
                self.assertEqual(ev.kind, expected)

    def test_structured_mention_matches_exact_bot_id(self):
        for text, expected in (("<@!BOT> hello", "group_at"),
                               ("<@BOT> hello", "group_at"),
                               ("<@!BOT2> hello", "group_message"),
                               ("@BOT hello", "group_message")):
            with self.subTest(text=text):
                self.assertEqual(Q.parse_event("GROUP_MESSAGE_CREATE", payload(content=text), {"BOT"}).kind,
                                 expected)

    def test_legacy_at_event_is_still_a_request_without_bot_identity(self):
        self.assertEqual(Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload()).kind, "group_at")

    def test_verified_bot_alias_applies_only_in_its_own_group(self):
        root = Path(self.temp.name)
        (root / "data").mkdir()
        (root / "data/qq.json").write_text(json.dumps({"bot_member_ids": {"GROUP": "SCOPED_BOT"}}))
        events = []
        gateway = Q.QQGateway(on_event=events.append)
        d = payload(content="<@SCOPED_BOT> test")
        with patch.object(Q.M, "ROOT", root):
            gateway._dispatch("GROUP_MESSAGE_CREATE", d)
            gateway._dispatch("GROUP_MESSAGE_CREATE", {**d, "id": "M2", "group_openid": "OTHER"})
        self.assertEqual([(e.kind, e.content) for e in events],
                         [("group_at", "test"), ("group_message", "<@SCOPED_BOT> test")])

    def test_explicit_test_request_reaches_model_send_and_memory(self):
        import brain as brain
        bridge = B.Bridge()
        ev = Q.parse_event("GROUP_MESSAGE_CREATE", payload(content="<@BOT> test"), {"BOT"})
        who = {"name": "owner", "id": "PERSON", "is_owner": True, "block": ""}
        with patch.object(B, "identify", return_value=who), \
                patch.object(B, "claim_reply", return_value=None), \
                patch.object(B, "command_reply", return_value=None), \
                patch.object(B, "build_system", return_value=("fixture", {"level": "daily", "options": {}})), \
                patch.object(brain, "api_key", return_value="fixture"), \
                patch.object(brain, "ask_with_system", return_value=("收到测试消息", "", {})) as model, \
                patch.object(bridge, "_send", return_value=B.DeliveryReceipt("accepted", "收到测试消息")) as send, \
                patch.object(bridge, "_remember") as remember, patch.object(bridge, "_log_mood"):
            import time
            bridge._process(ev, time.time())
        model.assert_called_once(); send.assert_called_once(); remember.assert_called_once()

    def test_unknown_bot_identity_does_not_guess_from_another_bot_mention(self):
        self.assertEqual(Q.parse_event("GROUP_MESSAGE_CREATE", payload(mentions=[{"id": "BOT", "bot": True}])).kind,
                         "group_message")

    def test_background_never_starts_reply_commands_media_or_owner_memory(self):
        ev = Q.parse_event("GROUP_MESSAGE_CREATE", payload(content="/档位 神格",
                           attachments=[{"url": "https://example.invalid/private.png"}]))
        who = {"name": "owner", "id": "PERSON", "is_owner": True}
        bridge = B.Bridge()
        with patch.object(B, "identify", return_value=who), patch.object(B.threading, "Thread") as thread, \
                patch.object(B, "command_reply") as command, patch.object(B, "read_images") as images, \
                patch.object(B.M, "add") as memory:
            bridge.handle(ev)
        for call in (thread, command, images, memory):
            call.assert_not_called()
        row = B.hist_for("group:GROUP")[0]
        self.assertEqual((row["text"], row["passive"], row["speaker_id"]),
                         ("/档位 神格", True, "PERSON"))

    def test_bot_authored_messages_cannot_start_a_reply_loop(self):
        ev = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload(author={"id": "BOT", "bot": True}))
        with patch.object(B, "identify") as identify, patch.object(B.threading, "Thread") as thread:
            B.Bridge().handle(ev)
        identify.assert_not_called(); thread.assert_not_called()
        self.assertFalse(self.history.exists())

    def test_background_delivery_does_not_dedupe_away_the_at_delivery(self):
        events = []
        gateway = Q.QQGateway(on_event=events.append)
        for kind in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE",
                     "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            gateway._dispatch(kind, payload())
        self.assertEqual([e.kind for e in events], ["group_message", "group_at"])

    def test_two_senders_with_identical_text_remain_two_requests(self):
        events = []
        gateway = Q.QQGateway(on_event=events.append)
        gateway._bot_ids.add("BOT")
        for mid, speaker in (("M1", "FIRST"), ("M2", "SECOND")):
            gateway._dispatch("GROUP_MESSAGE_CREATE", payload(mid, speaker, mentions=[{"id": "BOT"}]))
        self.assertEqual([(e.msg_id, e.speaker_id, e.kind) for e in events],
                         [("M1", "FIRST", "group_at"), ("M2", "SECOND", "group_at")])

    def test_history_upgrades_one_message_and_never_downgrades_it(self):
        text = ("a long direct request " * 100).strip()
        for passive in (True, False, True):
            B.hist_append("group:GROUP", "user", "person", text, message_id="M1", passive=passive)
        rows = B.hist_for("group:GROUP")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["passive"])
        self.assertEqual(rows[0]["text"], text)

    def test_busy_background_is_bounded_without_shortening_direct_chat(self):
        direct = ("direct conversation " * 100).strip()
        B.hist_append("group:GROUP", "user", "owner", direct, message_id="DIRECT")
        B.hist_append("group:GROUP", "assistant", "", "previous answer")
        for i in range(25):
            B.hist_append("group:GROUP", "user", f"person-{i}", "背景" * 2000,
                          message_id=f"BG-{i}", passive=True)
        stored = json.loads(self.history.read_text(encoding="utf-8"))["group:GROUP"]
        self.assertEqual(len(stored), 8)
        self.assertEqual([row["message_id"] for row in stored if row.get("passive")],
                         [f"BG-{i}" for i in range(19, 25)])
        current = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload("NEW"))
        for level in ("daily", "deep", "max", "thunder"):
            with self.subTest(level=level), patch.object(B, "group_level_for", return_value=level):
                history = B.history_messages(current)
                background = [row["content"] for row in history if row["content"].startswith(B.BACKGROUND_PREFIX)]
                self.assertEqual(len(background), 6)
                self.assertLessEqual(sum(map(len, background)), 1200)
                self.assertEqual(history[0]["content"], "owner：" + direct)
                self.assertEqual(history[1], {"role": "assistant", "content": "previous answer"})

    def test_old_unbounded_history_is_capped_before_model_use(self):
        B._hist_save({"group:GROUP": [
            {"role": "user", "name": "long nickname " * 100, "text": f"{i}: " + "x" * 8000,
             "ts": B.M.now_iso(), "passive": True, "message_id": f"OLD-{i}"}
            for i in range(25)]})
        current = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload("NEW"))
        for level in ("daily", "deep", "max", "thunder"):
            with self.subTest(level=level), patch.object(B, "group_level_for", return_value=level):
                history = B.history_messages(current)
                self.assertLessEqual(len(history), 6)
                self.assertLessEqual(sum(len(row["content"]) for row in history), 1200)
                self.assertIn("24:", history[-1]["content"])

    def test_background_without_message_id_still_has_limits_and_label(self):
        for _ in range(10):
            B.hist_append("group:GROUP", "user", "person", "x" * 8000, passive=True)
        current = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload("NEW"))
        history = B.history_messages(current)
        self.assertLessEqual(len(history), 6)
        self.assertTrue(all(row["content"].startswith(B.BACKGROUND_PREFIX) for row in history))
        self.assertLessEqual(sum(len(row["content"]) for row in history), 1200)

    def test_background_is_labeled_and_current_request_removed_by_id(self):
        B.hist_append("group:GROUP", "user", "first", "亲亲", message_id="M1", passive=True)
        B.hist_append("group:GROUP", "user", "second", "亲亲", message_id="M2")
        B.hist_append("group:GROUP", "user", "third", "another topic", message_id="M3", passive=True)
        current = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload("M2", "second"))
        history = B.history_messages(current)
        self.assertEqual(len(history), 2)
        self.assertIn("[群聊背景，非对你的请求] first", history[0]["content"])
        self.assertNotIn("second", json.dumps(history))

    def test_history_does_not_remove_another_person_with_the_same_text(self):
        B.hist_append("group:GROUP", "user", "first", "亲亲", message_id="M1", passive=True)
        current = Q.parse_event("GROUP_AT_MESSAGE_CREATE", payload("M2", "second"))
        self.assertEqual(len(B.history_messages(current)), 1)
        current.group_openid = "ANOTHER_GROUP"
        self.assertEqual(B.history_messages(current), [])


if __name__ == "__main__":
    unittest.main()
