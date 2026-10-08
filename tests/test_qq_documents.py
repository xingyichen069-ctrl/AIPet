"""Official file upload boundaries, attachment delivery and Markdown writes."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]
import access_scope as A
import local_tools as L
import qq_documents as D
import qq_bridge as B


class Documents(unittest.TestCase):
    def setUp(self):
        D._cache.clear()
        self.doc = D.MarkdownDocument("笔记.md", "# 标题\n\n| 内容 |\n| --- |\n| 中文 |\n")
        self.calls = []

    def api(self, method, path, body, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/upload_prepare"):
            size = int(body["file_size"])
            return {"upload_id": "fixture-upload", "block_size": "32",
                    "parts": [{"index": index + 1,
                               "presigned_url": "https://fixture.cos.ap-guangzhou.myqcloud.com/part",
                               "block_size": str(min(32, size - index * 32))}
                              for index in range((size + 31) // 32)]}
        if path.endswith("/upload_part_finish"):
            return {}
        if path.endswith("/files"):
            return {"file_info": "fixture-file", "ttl": 300}
        raise AssertionError("Unexpected external operation: " + path)

    def test_upload_preserves_bytes_and_returned_part_ids_without_sending(self):
        with patch.object(D, "_put") as put:
            self.assertEqual(D.upload(self.doc, "group", "GROUP", self.api), "fixture-file")
        self.assertEqual(b"".join(call.args[1] for call in put.call_args_list), self.doc.data())
        prepare = self.calls[0][2]
        self.assertEqual(prepare["md5"], hashlib.md5(self.doc.data()).hexdigest())
        acknowledgements = [body for _, path, body in self.calls if path.endswith("/upload_part_finish")]
        self.assertEqual([body["part_index"] for body in acknowledgements], [1, 2])
        self.assertTrue(all(path.startswith("/v2/groups/GROUP/") for _, path, _ in self.calls))
        self.assertEqual(self.calls[-1][2]["srv_send_msg"], False)
        self.assertEqual(self.calls[-1][2]["file_type"], 4)
        self.assertFalse(any(path.endswith("/messages") for _, path, _ in self.calls))

    def test_cache_is_scoped_to_content_scene_and_destination_and_bounded(self):
        api = self.api
        with patch.object(D, "_put"):
            D.upload(self.doc, "c2c", "OWNER", api)
            count = len(self.calls)
            D.upload(self.doc, "c2c", "OWNER", api)
            self.assertEqual(len(self.calls), count)
            D.upload(self.doc, "group", "OWNER", api)
            self.assertGreater(len(self.calls), count)
            count = len(self.calls)
            D.upload(D.MarkdownDocument("笔记.md", "# 更新"), "c2c", "OWNER", api)
            self.assertGreater(len(self.calls), count)
            for index in range(D.MAX_CACHE + 1):
                D.upload(self.doc, "group", str(index), api)
            self.assertEqual(len(D._cache), D.MAX_CACHE)
            with patch.object(D.time, "monotonic", return_value=time.monotonic() + 400):
                before = len(self.calls)
                D.upload(self.doc, "group", str(D.MAX_CACHE), api)
                self.assertGreater(len(self.calls), before)

    def test_bad_file_target_or_expired_deadline_does_not_call_api(self):
        api = MagicMock()
        for doc, scene, target in [(D.MarkdownDocument("../a.md", "x"), "group", "G"),
                                   (D.MarkdownDocument("a.md", "x" * (D.MAX_BYTES + 1)), "group", "G"),
                                   (self.doc, "group", "G/other"), (self.doc, "channel", "G")]:
            with self.subTest(doc=doc.name, scene=scene), self.assertRaises(D.DocumentError):
                D.upload(doc, scene, target, api)
        with self.assertRaises(D.DocumentError):
            D.upload(self.doc, "group", "G", api, deadline=time.monotonic() - 1)
        api.assert_not_called()

    def test_malformed_upload_plan_is_rejected_before_put(self):
        good = self.api("POST", "/upload_prepare", {"file_size": str(len(self.doc.data()))})
        bad = [dict(good, block_size="0"), dict(good, parts=[]), dict(good, upload_id="")]
        duplicate = copy.deepcopy(good)
        duplicate["parts"][1]["index"] = duplicate["parts"][0]["index"]
        bad.append(duplicate)
        for prepared in bad:
            with self.subTest(prepared=prepared), patch.object(D, "_put") as put, \
                    self.assertRaises(D.DocumentError):
                D.upload(self.doc, "group", "G", lambda *args, **kwargs: prepared)
            put.assert_not_called()

    def test_upload_rejection_never_leaks_signed_url_or_provider_message(self):
        with self.assertRaises(D.DocumentError) as error:
            D.upload(self.doc, "group", "G", lambda *args, **kwargs: {
                "_http_error": 400, "code": 850019, "message": "private-signed-url"})
        self.assertIn("850019", str(error.exception))
        self.assertNotIn("private-signed-url", str(error.exception))

    def test_put_rejects_foreign_hosts_http_credentials_and_redirects(self):
        for url in ("http://x.myqcloud.com/a", "https://example.com/a",
                    "https://a.myqcloud.com.evil.test/a", "https://user@a.myqcloud.com/a"):
            with self.subTest(url=url), patch.object(D.urllib.request, "build_opener") as opener, \
                    self.assertRaises(D.DocumentError):
                D._put(url, b"public", 1)
            opener.assert_not_called()
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(D.urllib.request, "build_opener", return_value=opener) as factory:
            D._put("https://x.myqcloud.com/a", b"markdown", 1)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.method, "PUT")
        self.assertIsNone(req.get_header("Authorization"))
        self.assertTrue(any(isinstance(handler, D._NoRedirect) for handler in factory.call_args.args))
        self.assertIsNone(D._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other"))

    def test_attachment_reply_has_one_file_card_and_uses_passive_window(self):
        for scene, target in (("group", "groups/GRP1"), ("c2c", "users/AAA1")):
            with patch.object(B.QB, "_post", return_value={"id": "file-message"}) as post:
                ev = B._fake(scene=scene)
                B.QB.reply_file(ev, "file-handle")
                path, body = post.call_args.args
                self.assertEqual(path, "/v2/" + target + "/messages")
                self.assertEqual(body, {"msg_type": 7, "media": {"file_info": "file-handle"},
                                        "msg_id": "MSG1", "msg_seq": 1})
                post.reset_mock()
                self.assertIn("_skipped", B.QB.reply_file(B._fake(secs_ago=300), "file-handle"))
                post.assert_not_called()

    def test_existing_markdown_writer_preserves_format_and_permissions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(L, "_fs_root", return_value=Path(folder)):
            root = Path(folder)
            for scope in ({"source": "local"}, {"source": "qq", "scene": "c2c", "is_owner": True}):
                with A.bind(**scope):
                    result = L.call("fs_write", {"path": "notes/test.md", "content": self.doc.text})
                    self.assertIn("已写入", result)
                    self.assertEqual((root / "notes/test.md").read_text(encoding="utf-8"), self.doc.text)
                    self.assertIn("已追加", L.call("fs_write", {"path": "notes/test.md", "content": "\n追加", "append": True}))
                    self.assertIn("追加", L.call("fs_read", {"path": "notes/test.md"}))
            for scope in ({"source": "qq", "scene": "group", "is_owner": True},
                          {"source": "qq", "scene": "c2c", "is_owner": False}):
                with A.bind(**scope):
                    self.assertIn("未获", L.call("fs_write", {"path": "blocked.md", "content": "x"}))
            self.assertFalse((root / "blocked.md").exists())


if __name__ == "__main__":
    unittest.main()
