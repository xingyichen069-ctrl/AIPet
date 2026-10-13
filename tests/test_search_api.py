"""Real request shapes with fake transports; never spend search API credits."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import search_api as S


class SearchApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "data").mkdir()
        self.secret = self.root / "data/secrets.json"
        self.secret.write_text(json.dumps({info.key_field: f"fixture-{key}"
            for key, info in S.PROVIDERS.items()}), encoding="utf-8")
        env = patch.dict(os.environ, {info.env_var: "" for info in S.PROVIDERS.values()})
        env.start(); self.addCleanup(env.stop)
        self.response = Mock(status=200)
        self.response.isclosed.return_value = False
        self.connection = Mock()
        self.connection.getresponse.return_value = self.response
        factory = patch.object(S.http.client, "HTTPSConnection", return_value=self.connection)
        self.factory = factory.start(); self.addCleanup(factory.stop)

    def body(self, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.response.read1.side_effect = io.BytesIO(raw).read1

    def test_brave_web_and_news_use_get_token_and_encoded_query(self):
        item = {"title": "资料", "url": "https://example.org/doc", "description": "Summary",
                "meta_url": {"hostname": "example.org"}, "age": "1 day ago"}
        for kind, path, data in (
            ("general", "/res/v1/web/search", {"web": {"results": [item]}}),
            ("text", "/res/v1/web/search", {"web": {"results": [item]}}),
            ("news", "/res/v1/news/search", {"results": [item]}),
        ):
            with self.subTest(kind=kind):
                self.body(data)
                result = S.search(self.root, {}, "brave", "中文 & science?", 3, kind)
                args, kwargs = self.connection.request.call_args
                url = urlsplit(args[1])
                self.assertEqual((args[0], url.path), ("GET", path))
                self.assertEqual(parse_qs(url.query), {"q": ["中文 & science?"], "count": ["3"]})
                self.assertEqual(kwargs["headers"]["X-Subscription-Token"], "fixture-brave")
                self.assertIsNone(kwargs["body"])
                self.assertEqual(self.factory.call_args.args, ("api.search.brave.com",))
                self.assertEqual(result, {"answer": "", "results": [{"title": "资料",
                    "url": item["url"], "snippet": "Summary", "source": "example.org", "date": "1 day ago"}]})

    def test_serper_web_and_news_use_post_json_and_api_key(self):
        for kind, path, field in (("general", "/search", "organic"), ("news", "/news", "news")):
            with self.subTest(kind=kind):
                self.body({field: [{"title": "T", "link": "https://example.org", "snippet": "S",
                                    "source": "Source", "date": "2 hours ago"}]})
                result = S.search(self.root, {}, "serper", "query", 2, kind)
                args, kwargs = self.connection.request.call_args
                self.assertEqual(args, ("POST", path))
                self.assertEqual(json.loads(kwargs["body"]), {"q": "query", "num": 2})
                self.assertEqual(kwargs["headers"]["X-API-KEY"], "fixture-serper")
                self.assertEqual(self.factory.call_args.args, ("google.serper.dev",))
                self.assertEqual(result["results"][0]["source"], "Source")
                self.assertEqual(result["results"][0]["date"], "2 hours ago")

    def test_bounded_normalization_ignores_generated_answers_and_unsafe_links(self):
        rows = [{"link": link} for link in ("javascript:alert(1)", "file:///private", "https:///missing-host", "https://user:pass@example.org")]
        rows += [{"title": "T" * 500, "link": "https://example.org", "snippet": "S" * 3000}] * 12
        self.body({"organic": rows, "answerBox": {"answer": "not a source"}})
        result = S.search(self.root, {}, "serper", "query", 999)
        self.assertEqual(result["answer"], "")
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(len(result["results"][0]["title"]), 240)
        self.assertEqual(len(result["results"][0]["snippet"]), 900)
        self.assertEqual(json.loads(self.connection.request.call_args.kwargs["body"])["num"], 5)
        self.assertFalse((self.root / "data/tavily_usage.json").exists())

    def test_no_results_is_success_but_invalid_bodies_are_safe_errors(self):
        self.body({"organic": []})
        self.assertEqual(S.search(self.root, {}, "serper", "query"), {"answer": "", "results": []})
        for data in (b"fixture-secret invalid JSON", {"error": "fixture-secret"}, {"organic": [None]}, []):
            with self.subTest(data=data):
                self.body(data)
                with self.assertRaises(S.SearchAPIError) as error:
                    S.search(self.root, {}, "serper", "query")
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertIn("格式", str(error.exception))

    def test_failures_never_follow_redirects_retry_or_echo_http_body(self):
        for status in (301, 401, 403, 402, 429, 500):
            with self.subTest(status=status):
                self.factory.reset_mock(); self.response.reset_mock()
                self.response.status = status
                with self.assertRaises(S.SearchAPIError):
                    S.search(self.root, {}, "brave", "query")
                self.assertEqual(self.factory.call_count, 1)
                self.response.read1.assert_not_called()
                self.response.close.assert_called_once()
        self.connection.request.side_effect = OSError("fixture-secret transport error")
        with self.assertRaises(S.SearchAPIError) as error:
            S.search(self.root, {}, "serper", "query")
        self.assertNotIn("fixture-secret", str(error.exception))
        self.connection.close.assert_called()

    def test_keys_reload_and_file_precedes_environment_without_migration(self):
        with patch.dict(os.environ, {"BRAVE_API_KEY": "environment"}):
            self.assertEqual(S.load_key(self.root, {}, "brave"), "fixture-brave")
            self.secret.write_text('{"brave_search_api_key":"replacement"}', encoding="utf-8")
            self.assertEqual(S.load_key(self.root, {}, "brave"), "replacement")
            self.secret.write_text("{}", encoding="utf-8")
            self.assertEqual(S.load_key(self.root, {}, "brave"), "environment")
        with self.assertRaises(S.SearchAPIError):
            S.load_key(self.root, {}, "brave")
        self.secret.write_text('{"brave_search_api_key":"bad key"}', encoding="utf-8")
        with self.assertRaises(S.SearchAPIError) as error:
            S.load_key(self.root, {}, "brave")
        self.assertNotIn("bad key", str(error.exception))
        self.secret.write_text("x" * (128 * 1024 + 1), encoding="utf-8")
        with self.assertRaises(S.SearchAPIError):
            S.load_key(self.root, {}, "brave")
        self.factory.assert_not_called()

    def test_bad_inputs_and_exhausted_deadline_never_request(self):
        for kwargs in ({"query": ""}, {"query": "x" * 501}, {"kind": "images"},
                       {"provider": "custom"}, {"n": 0}, {"n": True}):
            args = {"provider": "brave", "query": "query"}; args.update(kwargs)
            with self.assertRaises(ValueError):
                S.search(self.root, {}, **args)
        for value in (0, 21, "12", True):
            with self.assertRaises(ValueError):
                S.search(self.root, {"search_apis": {"brave": {"timeout_seconds": value}}}, "brave", "query")
        with self.assertRaises(S.SearchAPIError):
            S.search(self.root, {}, "brave", "query", request_deadline=time.monotonic() + 10)
        self.factory.assert_not_called()

    def test_response_size_and_slow_body_obey_limits(self):
        self.body(b"x" * (S.MAX_RESPONSE + 1))
        with self.assertRaisesRegex(S.SearchAPIError, "过大"):
            S.search(self.root, {}, "brave", "query")
        elapsed = [100.]
        def trickle(size):
            elapsed[0] += 8
            return b" "
        self.response.read1.side_effect = trickle
        with patch.object(S.time, "monotonic", side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(S.SearchAPIError, "超时"):
                S.search(self.root, {}, "serper", "query")
        self.assertLessEqual(elapsed[0], 116)
        self.connection.close.assert_called()

    def test_tavily_delegates_existing_limits_and_legacy_key(self):
        config = {"tavily": {"daily_limit": 2, "monthly_limit": 3}, "tavily_key": "legacy"}
        self.secret.write_text("{}", encoding="utf-8")
        self.assertEqual(S.load_key(self.root, config, "tavily"), "legacy")
        self.body({"results": [{"title": "Source", "url": "https://example.org", "content": "Text"}]})
        result = S.search(self.root, config, "tavily", "query", 3)
        self.assertEqual(result["results"][0]["snippet"], "Text")
        self.assertEqual(self.connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer legacy")
        usage = json.loads((self.root / "data/tavily_usage.json").read_text())
        self.assertEqual(usage["day_reserved"], 1)
        with patch.object(S.T, "search", side_effect=S.T.TavilyError("Tavily 已关闭")):
            with self.assertRaisesRegex(S.SearchAPIError, "关闭"):
                S.search(self.root, config, "tavily", "query")
