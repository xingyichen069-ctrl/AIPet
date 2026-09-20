from __future__ import annotations

import tempfile
import unittest
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tools


def _date(delta_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(name: str, days: int = 0, snippet: str = "摘要") -> dict:
    return {"title": name, "url": f"https://example.test/{name}",
            "snippet": snippet, "date": _date(days)}


class SearchTests(unittest.TestCase):
    def test_latest_intent_keeps_time_semantics(self):
        intent = tools.resolve_search_intent("OpenAI 最新消息")
        self.assertEqual(intent.kind, "news")
        self.assertEqual(intent.freshness, "day")
        self.assertTrue(intent.force_refresh)
        self.assertFalse(intent.incremental)

    def test_latest_match_and_latest_version_are_distinct(self):
        match = tools.resolve_search_intent("阿森纳 最新比赛 比分")
        self.assertEqual(match.kind, "news")
        self.assertEqual(match.freshness, "day")
        version = tools.resolve_search_intent("Python 最新版本")
        self.assertEqual(version.temporal_mode, "latest_version")
        self.assertIsNone(version.freshness)
        self.assertTrue(version.force_refresh)
        self.assertIn("latest version", version.backend_query)

    def test_cache_key_includes_temporal_intent(self):
        normal = tools.resolve_search_intent("OpenAI 消息")
        latest = tools.resolve_search_intent("OpenAI 最新消息")
        self.assertNotEqual(tools._cache_key(normal, "ddgs", 5),
                            tools._cache_key(latest, "ddgs", 5))

    def test_force_refresh_skips_cache_and_writes_new_value(self):
        calls = []
        def backend(query, n, kind, freshness):
            calls.append(query)
            return [_item(f"result-{len(calls)}")]
        with tempfile.TemporaryDirectory() as td, patch.object(tools, "CACHE_DIR", Path(td)), \
                patch.object(tools, "_search_ddgs", side_effect=backend):
            first = tools.search("普通资料", backend="ddgs")
            cached = tools.search("普通资料", backend="ddgs")
            refreshed = tools.search("普通资料", backend="ddgs", force_refresh=True)
        self.assertFalse(first["cached"])
        self.assertTrue(cached["cached"])
        self.assertFalse(refreshed["cached"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(refreshed["results"][0]["title"], "result-2")

    def test_freshness_does_not_mix_old_results(self):
        backend = lambda query, n, kind, freshness: [_item("today", 0), _item("old", -3)]
        with patch.object(tools, "_search_ddgs", side_effect=backend):
            result = tools.search("今天 OpenAI 消息", backend="ddgs", use_cache=False)
        self.assertEqual([x["title"] for x in result["results"]], ["today"])

    def test_incremental_new_results_is_not_limited_by_display_limit(self):
        payloads = [[_item("old")], [_item("new-a"), _item("new-b")]]
        def backend(query, n, kind, freshness):
            return payloads.pop(0)
        with tempfile.TemporaryDirectory() as td, patch.object(tools, "CACHE_DIR", Path(td)), \
                patch.object(tools, "_cache_get", return_value=None), \
                patch.object(tools, "_search_ddgs", side_effect=backend):
            tools.search("普通资料", max_results=1, backend="ddgs")
            second = tools.search("普通资料", max_results=1, backend="ddgs")
        self.assertEqual(second["new_results"], 2)
        self.assertEqual([x["title"] for x in second["results"]], ["new-a"])

    def test_tavily_answer_is_preserved(self):
        adapter = lambda query, n, kind, freshness: {"answer": "Tavily 摘要", "results": [_item("A")]}
        with tempfile.TemporaryDirectory() as td, patch.object(tools, "CACHE_DIR", Path(td)), \
                patch.object(tools, "_search_tavily", side_effect=adapter):
            result = tools.search("OpenAI", backend="tavily", use_cache=False)
            rendered = tools.as_prompt_block("OpenAI", backend="tavily", max_chars=500)
        self.assertEqual(result["answer"], "Tavily 摘要")
        self.assertIn("Tavily 摘要", rendered)

    def test_prompt_keeps_each_result_header(self):
        items = [_item(f"标题{i}", snippet="很长的摘要" * 100) for i in range(1, 4)]
        with patch.object(tools, "_search_ddgs", return_value=items):
            rendered = tools.as_prompt_block("普通资料", backend="ddgs", max_results=3, max_chars=220)
        for i in range(1, 4):
            self.assertIn(f"标题{i}", rendered)
        self.assertGreaterEqual(rendered.count("example.test"), 3)

    def test_failure_types_are_distinct(self):
        with patch.object(tools, "_search_ddgs", side_effect=tools.SearchTimeout("慢")):
            self.assertTrue(tools.as_prompt_block("x", backend="ddgs").startswith("（搜索超时："))
        with patch.object(tools, "_search_ddgs", side_effect=tools.SearchNoResults("空")):
            self.assertTrue(tools.as_prompt_block("x", backend="ddgs").startswith("（搜索无结果："))
        with patch.object(tools, "_search_ddgs", side_effect=tools.SearchBackendUnavailable("断网")):
            self.assertTrue(tools.as_prompt_block("x", backend="ddgs").startswith("（搜索后端不可用："))

    def test_ddgs_news_fallback_keeps_requested_time_limit(self):
        calls = []

        class FakeDDGS:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def news(self, query, **kwargs):
                calls.append(("news", kwargs))
                raise RuntimeError("news unavailable")
            def text(self, query, **kwargs):
                calls.append(("text", kwargs))
                return [{"title": "fallback", "href": "https://example.test/fallback", "body": "ok"}]

        fake_module = types.SimpleNamespace(DDGS=FakeDDGS)
        with patch.dict(sys.modules, {"ddgs": fake_module}), patch.object(tools, "_proxy_url", return_value=None):
            result = tools._search_ddgs("OpenAI", 3, "news", "day")
        self.assertEqual(result[0]["title"], "fallback")
        self.assertEqual(calls[0][0], "news")
        text_calls = [kwargs for method, kwargs in calls if method == "text"]
        self.assertTrue(text_calls)
        self.assertEqual(text_calls[0]["timelimit"], "d")

    def test_parse_result_time(self):
        for value in ("2026-09-20", "2小时前", "2 hours ago", "Sun, 20 Sep 2026 12:00:00 GMT"):
            with self.subTest(value=value):
                self.assertIsNotNone(tools.parse_result_time(value))


if __name__ == "__main__":
    unittest.main()
