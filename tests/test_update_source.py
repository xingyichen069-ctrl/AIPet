"""Branch-pinned updates; all network responses and local versions are fixtures."""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

import update as U


COMMIT = "a" * 40


def response(data):
    return SimpleNamespace(text=json.dumps(data))


def source(version="0.5.0-beta.2", branch="main", commit=COMMIT):
    return [response({"ref": f"refs/heads/{branch}",
                      "object": {"type": "commit", "sha": commit}}),
            response({"type": "file", "path": "VERSION", "encoding": "base64",
                      "content": base64.b64encode((version + "\n").encode()).decode()})]


class UpdateSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.version = Path(self.tmp.name) / "VERSION"
        self.version.write_text("0.5.0-beta.1\n", encoding="utf-8")
        self.cfg = {}
        for target, name, value in ((U, "VERSION_FILE", self.version), (U.M, "CFG", self.cfg)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_main_uses_pinned_version_and_never_selects_other_branch_tags(self):
        with patch.object(U.PX, "request", side_effect=source()) as request, \
                patch.object(U, "fetch_tags", side_effect=AssertionError("must not consult tags")):
            result = U.check()
        self.assertTrue(result["ok"])
        self.assertTrue(result["newer"])
        self.assertEqual(result["comparison"], "remote_newer")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["commit"], COMMIT)
        self.assertEqual(result["latest_clean"], "0.5.0-beta.2")
        urls = [call.args[0] for call in request.call_args_list]
        self.assertEqual(urls, [f"https://api.github.com/repos/{U.DEFAULT_REPO}/git/ref/heads/main",
                               f"https://api.github.com/repos/{U.DEFAULT_REPO}/contents/VERSION?ref={COMMIT}"])
        self.assertTrue(result["url"].endswith("/tree/" + COMMIT))
        self.assertTrue(result["download_url"].endswith("/archive/" + COMMIT + ".zip"))

    def test_explicit_fork_and_slash_branch_remain_identifiable(self):
        self.cfg["update"] = {"repo": "example/fork", "branch": "design/chibi-pet-concepts"}
        with patch.object(U.PX, "request", side_effect=source(branch="design/chibi-pet-concepts")) as request:
            result = U.check()
        self.assertTrue(result["ok"])
        self.assertIn("/repos/example/fork/git/ref/heads/design/chibi-pet-concepts", request.call_args_list[0].args[0])
        self.assertIn("example/fork / design/chibi-pet-concepts", U.describe(result))
        self.assertNotIn("http", U.describe(result))  # QQ text stays link-free.

    def test_same_version_does_not_claim_identical_or_latest_code(self):
        with patch.object(U.PX, "request", side_effect=source("0.5.0-beta.1")):
            result = U.check()
        self.assertFalse(result["newer"])
        self.assertEqual(result["comparison"], "same_version")
        self.assertIn("未比较本机文件", U.describe(result))
        self.assertNotIn("已经是最新", U.describe(result))

    def test_local_version_ahead_is_not_treated_as_main_compatibility(self):
        self.version.write_text("0.9.0", encoding="utf-8")
        with patch.object(U.PX, "request", side_effect=source()):
            result = U.check()
        self.assertFalse(result["newer"])
        self.assertEqual(result["comparison"], "local_ahead")
        self.assertIn("不能确认", U.describe(result))

    def test_unreadable_or_invalid_local_version_is_unknown(self):
        for text in (None, "not-a-version", "0.5", ""):
            with self.subTest(text=text):
                if text is None:
                    self.version.unlink(missing_ok=True)
                else:
                    self.version.write_text(text, encoding="utf-8")
                with patch.object(U.PX, "request", side_effect=source()):
                    result = U.check()
                self.assertTrue(result["ok"])
                self.assertEqual(result["comparison"], "local_unknown")
                self.assertFalse(result["newer"])

    def test_unknown_source_never_offers_a_download(self):
        bad_refs = [{}, {"ref": "refs/tags/v9.0.0", "object": {"type": "commit", "sha": COMMIT}},
                    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": COMMIT}},
                    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "main"}}]
        for ref in bad_refs:
            with self.subTest(ref=ref), patch.object(U.PX, "request", return_value=response(ref)) as request:
                result = U.check()
                self.assertFalse(result["ok"])
                self.assertFalse(result["newer"])
                self.assertEqual(result["url"], "")
                self.assertEqual(request.call_count, 1)

    def test_invalid_remote_version_and_nonregular_file_are_rejected(self):
        for text in ("", "main", "v99", "0.5.0\n0.6.0", "0.5.0-beta.01", "x" * 300):
            with self.subTest(text=text), patch.object(U.PX, "request", side_effect=source(text)):
                result = U.check()
                self.assertFalse(result["ok"])
                self.assertEqual(result["download_url"], "")
        for file in ([{"name": "VERSION"}], {"type": "symlink", "target": "somewhere"},
                     {"type": "file", "path": "VERSION", "encoding": "base64", "content": "!bad!"}):
            with self.subTest(file=file), patch.object(U.PX, "request", side_effect=[source()[0], response(file)]):
                self.assertFalse(U.check()["ok"])

    def test_invalid_config_does_not_start_network_io(self):
        for setting in (True, "main", {"repo": "https://example.invalid"},
                        {"branch": "../main"}, {"branch": "main\n"}, {"branch": ""}):
            with self.subTest(setting=setting), patch.object(U.PX, "request") as request:
                self.cfg["update"] = setting
                self.assertFalse(U.check()["ok"])
                request.assert_not_called()

    def test_http_errors_and_malformed_payloads_are_not_reported_as_current(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status), patch.object(U.PX, "request", side_effect=
                    urllib.error.HTTPError("https://api.github.com", status, "fixture", None, None)):
                result = U.check()
                self.assertFalse(result["ok"])
                self.assertIn(str(status), result["error"])
        with patch.object(U.PX, "request", return_value=SimpleNamespace(text="not JSON")):
            self.assertFalse(U.check()["ok"])

    def test_second_request_uses_remaining_timeout_and_no_moving_ref(self):
        with patch.object(U.time, "monotonic", side_effect=[100., 102., 109.]), \
                patch.object(U.PX, "request", side_effect=source()) as request:
            result = U.check(timeout=12)
        self.assertTrue(result["ok"])
        self.assertEqual([c.kwargs["timeout"] for c in request.call_args_list], [10., 3.])
        with patch.object(U.time, "monotonic", side_effect=[100., 101., 113.]), \
                patch.object(U.PX, "request", side_effect=source()) as request:
            result = U.check(timeout=12)
        self.assertFalse(result["ok"])
        self.assertEqual(request.call_count, 1)


class VersionOrdering(unittest.TestCase):
    def test_semver_prerelease_and_metadata_order(self):
        ordered = ["0.5.0-alpha", "0.5.0-alpha.1", "0.5.0-alpha.beta", "0.5.0-beta",
                   "0.5.0-beta.2", "0.5.0-beta.10", "0.5.0-rc.1", "0.5.0", "0.5.1"]
        self.assertEqual(sorted(reversed(ordered), key=U.parse), ordered)
        self.assertEqual(U.parse("v0.5.0+build.7"), U.parse("0.5.0+other"))
        self.assertLess(U.parse("0.5.0-1"), U.parse("0.5.0-a"))

    def test_invalid_versions_are_not_silently_converted_to_zero(self):
        for value in ("main", "1", "1.2", "1.2.3.4", "01.2.3", "1.2.3-beta.01", "vv1.2.3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                U.parse(value)
