"""Read-only retrieval and request snapshots, using synthetic public fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import brain as B
import knowledge as K
import local_tools as LT
import mcp_server as MCP
import memory as M
import thinking as T


class Response:
    def __init__(self, delta):
        self.delta = delta

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield ('data: ' + json.dumps({'choices': [{'delta': self.delta}]})).encode()
        yield b'data: [DONE]'


class RetrievalBudget(unittest.TestCase):
    def setUp(self):
        work = Path(os.environ['AIPET_TEST_WORK'])
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.temp.name)
        (self.root / 'memory').mkdir()
        self.config = deepcopy(M.CFG)
        self.patches = [patch.object(M, 'ROOT', self.root),
                        patch.object(M, 'CFG', self.config),
                        patch.object(M, 'P', self.config['paths']),
                        patch.object(M, '_JOURNAL_CACHE', {'sig': None, 'entries': None}),
                        patch.object(M, '_TOKEN_CACHE', {}),
                        patch.object(M, 'now', return_value=datetime(2026, 10, 1, tzinfo=timezone.utc))]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temp.cleanup)
        for item in self.patches:
            self.addCleanup(item.stop)

    def entry(self, identity, text, **extra):
        return {'id': identity, 'ts': '2026-09-30T12:00:00+00:00',
                'text': text, 'importance': 3, 'tags': [], 'speaker': 'owner',
                'source': 'desktop', 'decay': 'permanent', **extra}

    def fixture(self, entries):
        path = self.root / 'memory/journal.jsonl'
        path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries), encoding='utf-8')
        return path

    def options(self, budget=350, count=3, floor=0):
        return {'token_budget': budget, 'max_entries': count, 'recency_floor': floor, 'min_score': 0.05}

    def assert_bounded(self, hits, budget, count):
        self.assertLessEqual(len(hits), count)
        rendered = M.format_memories(hits)
        if rendered:
            self.assertLessEqual(M.estimate_tokens(rendered), budget)

    def test_long_memory_is_marked_excerpt_without_changing_source_or_cache(self):
        original = self.entry('long-source', '蓝鲸项目' * 2000)
        path = self.fixture([original])
        before = path.read_bytes()
        cached = deepcopy(M.load_journal())
        hits = M.retrieve('蓝鲸项目', retrieval=self.options(count=1))
        self.assertEqual(len(hits), 1)
        self.assertLess(len(hits[0]['text']), len(original['text']))
        self.assertIn('记忆片段已截取', M.format_memories(hits))
        self.assertIn('记忆ID：long-source', M.format_memories(hits))
        self.assert_bounded(hits, 350, 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(M.load_journal(), cached)

    def test_progress_and_recent_fallbacks_share_the_same_limits(self):
        entries = [self.entry(str(i), ('阶段事实' + str(i)) * 50,
                              progress={'since': '2024-09-01', 'span_months': 12,
                                        'labels': ['初期', '中期', '后期']}) for i in range(30)]
        self.fixture(entries)
        hits = M.retrieve('毫无命中', retrieval=self.options(400, 2, 30), top_k=999)
        self.assertTrue(hits)
        self.assert_bounded(hits, 400, 2)
        self.assertIn('当前：', M.format_memories(hits))

    def test_unfittable_metadata_does_not_hide_later_short_match(self):
        self.fixture([self.entry('big', '蓝鲸项目', source='长来源' * 2000),
                      self.entry('small', '蓝鲸项目')])
        hits = M.retrieve('蓝鲸项目', retrieval=self.options(150, 1))
        self.assertEqual([e['id'] for e in hits], ['small'])
        self.assert_bounded(hits, 150, 1)

    def test_recent_only_fallback_is_bounded_and_not_duplicated(self):
        self.fixture([self.entry('older', '以前的合成事实', ts='2026-09-29T12:00:00+00:00'),
                      self.entry('latest', '最新的合成事实' * 100)])
        hits = M.retrieve('完全不相关', retrieval=self.options(240, 1, 20))
        self.assertEqual([e['id'] for e in hits], ['latest'])
        self.assert_bounded(hits, 240, 1)

    def test_zero_and_tiny_budgets_or_counts_return_no_oversized_entry(self):
        self.fixture([self.entry('one', '蓝鲸项目')])
        for budget, count in [(0, 3), (1, 3), (200, 0), (-10, 3)]:
            with self.subTest(budget=budget, count=count):
                self.assertEqual(M.retrieve('蓝鲸项目', retrieval=self.options(budget, count)), [])
        self.assertEqual(M.retrieve('蓝鲸项目', retrieval=self.options(), top_k=0), [])

    def test_small_budgets_cover_empty_results_in_actual_stream_and_recall_paths(self):
        cases = [('empty', []), ('miss', [self.entry('unrelated', 'SYNTHETIC_UNRELATED')]),
                 ('hit', [self.entry('one', '蓝鲸项目')])]
        fake_mood = types.SimpleNamespace(block=lambda: '', suggest_block=lambda query: '')
        call = {'tool_calls': [{'index': 0, 'id': 'r1', 'function': {
            'name': 'recall', 'arguments': json.dumps({'query': '蓝鲸项目'})}}]}
        for case, entries in cases:
            self.fixture(entries)
            for budget in (0, 1, 17, 35, 200):
                with self.subTest(case=case, budget=budget):
                    options = {'level': 'daily', 'name': '合成档位', 'reason': '合成测试',
                               'params': {'tools': ['recall'], 'model': 'synthetic-model'},
                               'retrieval': self.options(budget, 1)}
                    with patch.object(B, 'api_key', return_value='SYNTHETIC'), \
                         patch.object(B, '_request', side_effect=[Response(call), Response({'content': '继续回答'})]) as request, \
                         patch.object(T, 'apply_to_memory', side_effect=AssertionError('snapshot already supplied')), \
                         patch.object(K, 'as_prompt_block', return_value=''), \
                         patch.object(M, 'persona_text', return_value='合成人格'), \
                         patch.object(B.C.PR, 'examples', return_value=[]), \
                         patch.object(M, '_profile_facts', return_value=''), \
                         patch.dict('sys.modules', {'mood': fake_mood}):
                        events = list(B._stream('蓝鲸项目', options=options))
                    self.assertIn(('content', '继续回答'), events)
                    sent = request.call_args.args[0]['messages']
                    system = next(m['content'] for m in sent if m['role'] == 'system')
                    memory_block = ('## 相关回忆\n' + system.split('## 相关回忆\n', 1)[1]
                                    .split('\n\n## 关于用户的事实', 1)[0]
                                    if '## 相关回忆\n' in system else '')
                    outputs = [memory_block, next(m['content'] for m in sent if m['role'] == 'tool')]
                    for recall in (LT.recall, MCP._t_recall):
                        with patch.object(T, 'apply_to_memory', return_value=options):
                            outputs.append(recall('蓝鲸项目'))
                    for text in outputs:
                        self.assertNotIn('检索失败', text)
                        self.assertLessEqual(M.estimate_tokens(text) if text else 0, budget)
                        if budget <= 1:
                            self.assertEqual(text, '')
                        elif budget == 200:
                            self.assertIn('蓝鲸项目' if case == 'hit' else '没有可在本轮预算', text)

    def test_cli_search_keeps_excerpt_source_and_identity_markers(self):
        source = self.entry('cli-source', '蓝鲸项目' * 2000, speaker='guest', source='synthetic-import')
        path = self.fixture([source])
        before = path.read_bytes()
        self.config['retrieval'] = self.options(250, 1)
        output = io.StringIO()
        with patch('sys.argv', ['memory.py', 'search', '蓝鲸项目']), redirect_stdout(output):
            M.main()
        text = output.getvalue()
        self.assertIn('记忆片段已截取', text)
        self.assertIn('来源：synthetic-import', text)
        self.assertIn('不是主人', text)
        self.assertIn('记忆ID：cli-source', text)
        self.assertEqual(path.read_bytes(), before)

    def test_identity_source_and_rendered_wrapper_are_budgeted(self):
        self.fixture([self.entry('guest-source', '蓝鲸项目细节' * 100, speaker='qq:synthetic',
                                 speaker_name='合成群友', source='synthetic-source', tags=['项目'])])
        hits = M.retrieve('蓝鲸项目', retrieval=self.options(250, 1))
        text = M.format_memories(hits)
        self.assertIn('不是主人', text)
        self.assertIn('合成群友', text)
        self.assertIn('来源：synthetic-source', text)
        self.assertIn('2026-09-30', text)
        self.assert_bounded(hits, 250, 1)
        fake_mood = types.SimpleNamespace(block=lambda: '', suggest_block=lambda query: '')
        with patch.dict('sys.modules', {'mood': fake_mood}), patch.object(M, '_profile_facts', return_value=''):
            context = M.build_context('蓝鲸项目', retrieval=self.options(250, 1))
        self.assertIn(text, context)

    def test_local_recall_uses_bound_snapshot_and_keeps_source(self):
        self.fixture([self.entry('recall-source', '蓝鲸项目' * 500, speaker='guest', source='test-import')])
        with LT.bind_context(retrieval=self.options(250, 1)), \
             patch.object(T, 'apply_to_memory', side_effect=AssertionError('must not resolve twice')):
            result = LT.recall('蓝鲸项目', limit=999)
        self.assertIn('不是主人', result)
        self.assertIn('来源：test-import', result)
        self.assertLessEqual(M.estimate_tokens(result), 250)

    def test_existing_context_marker_preference_does_not_hide_tool_identity(self):
        self.fixture([self.entry('guest-source', '蓝鲸项目', speaker='guest')])
        self.config['speaker']['mark_in_context'] = False
        fake_mood = types.SimpleNamespace(block=lambda: '', suggest_block=lambda query: '')
        with patch.dict('sys.modules', {'mood': fake_mood}), patch.object(M, '_profile_facts', return_value=''):
            context = M.build_context('蓝鲸项目', retrieval=self.options())
        self.assertNotIn('不是主人', context)
        with LT.bind_context(retrieval=self.options()):
            self.assertIn('不是主人', LT.recall('蓝鲸项目'))

    def test_standalone_local_and_mcp_recall_resolve_query_by_keyword_once(self):
        self.fixture([self.entry('one', '蓝鲸项目' * 500)])
        for recall in (LT.recall, MCP._t_recall):
            with self.subTest(recall=recall.__name__), \
                 patch.object(T, 'apply_to_memory', return_value={'retrieval': self.options(260, 1)}) as resolve:
                result = recall('蓝鲸项目', limit=50)
            resolve.assert_called_once_with(query='蓝鲸项目')
            self.assertIn('记忆ID：one', result)
            self.assertLessEqual(M.estimate_tokens(result), 260)

    def test_provider_tool_round_keeps_snapshot_after_level_event_changes(self):
        self.fixture([self.entry('one', '蓝鲸项目' * 500)])
        payload = {'messages': [{'role': 'user', 'content': '蓝鲸项目'}], 'max_tokens': 100,
                   'tools': [s for s in LT.SPECS if s['function']['name'] == 'recall']}
        resolved = {'retrieval': self.options(240, 1)}
        call = {'tool_calls': [{'index': 0, 'id': 'r1', 'function': {
            'name': 'recall', 'arguments': json.dumps({'query': '蓝鲸项目', 'limit': 99})}}]}
        before = deepcopy(M.CFG)
        with patch.object(B, 'api_key', return_value='SYNTHETIC'), \
             patch.object(B, 'build_payload', return_value=(payload, resolved)), \
             patch.object(B, '_request', side_effect=[Response(call), Response({'content': '已找到'})]) as request, \
             patch.object(T, 'apply_to_memory', side_effect=AssertionError('must use turn snapshot')):
            turn = B._stream('蓝鲸项目')
            kind, live_options = next(turn)
            self.assertEqual(kind, 'level')
            live_options['retrieval']['token_budget'] = 10000
            events = list(turn)
        self.assertIn(('content', '已找到'), events)
        tool_text = next(m['content'] for m in request.call_args.args[0]['messages'] if m['role'] == 'tool')
        self.assertLessEqual(M.estimate_tokens(tool_text), 240)
        self.assertEqual(M.CFG, before)

    def test_parallel_tool_contexts_do_not_exchange_snapshots(self):
        barrier = threading.Barrier(2)
        observed = []
        def retrieve(query, **kwargs):
            barrier.wait(timeout=5)
            observed.append((query, kwargs['retrieval']['token_budget']))
            return []
        def run(query, budget):
            with LT.bind_context(retrieval=self.options(budget)):
                return LT.recall(query)
        with patch.object(M, 'retrieve', side_effect=retrieve), \
             patch.object(T, 'apply_to_memory', side_effect=AssertionError('snapshot already bound')):
            with ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(run, 'first', 100)
                b = executor.submit(run, 'second', 900)
                a.result(timeout=10)
                b.result(timeout=10)
        self.assertCountEqual(observed, [('first', 100), ('second', 900)])
        self.assertNotIn('retrieval', LT.tool_context())
