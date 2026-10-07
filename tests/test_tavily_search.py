"""Search routing, real request shape, private configuration and credit bounds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import local_tools as L
import memory as M
import tavily_search as T
import tools as W


class Search(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'data').mkdir()
        self.secret = self.root / 'data/secrets.json'
        self.secret.write_text(json.dumps({'tavily_api_key': 'test-only-private-key'}))
        for p in (patch.object(M, 'ROOT', self.root),
                  patch.object(W, 'CACHE_DIR', self.root / 'data/cache'),
                  patch.object(W, 'TOOLS_CFG', {'search_backend': 'ddgs'}),
                  patch.dict(os.environ, {'TAVILY_API_KEY': ''})):
            p.start(); self.addCleanup(p.stop)
        self.response = Mock(status=200)
        self.response.isclosed.return_value = False
        self.set_body({'answer': 'must not use a generated answer', 'results': [{
            'title': 'Public source', 'url': 'https://example.org/doc',
            'content': 'A public snippet.'}]})
        self.connection = Mock()
        self.connection.getresponse.return_value = self.response
        factory = patch.object(T.http.client, 'HTTPSConnection', return_value=self.connection)
        self.factory = factory.start(); self.addCleanup(factory.stop)

    def set_body(self, data):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.response.read1.side_effect = io.BytesIO(raw).read1

    def search(self, **kwargs):
        return W.search('foreign official source', source_scope='overseas', **kwargs)

    def usage(self):
        return json.loads((self.root / 'data/tavily_usage.json').read_text())

    def test_ordinary_english_search_and_failure_never_spend_tavily(self):
        with patch.object(W.PROXY, 'detect', return_value='http://fixture.invalid:80'), patch.object(W, '_search_ddgs', return_value=[]) as ddgs:
            self.assertEqual(W.search('Apple news')['backend'], 'ddgs')
            ddgs.side_effect = RuntimeError('network timeout')
            self.assertIn('超时', L.web_search('another ordinary query'))
        self.factory.assert_not_called()
        self.assertFalse((self.root / 'data/tavily_usage.json').exists())

    def test_overseas_uses_basic_once_with_bounded_source_results(self):
        self.set_body({'answer': 'discard', 'results': [{'title': 'T' * 500,
            'url': 'https://example.org', 'content': 's' * 3000}] * 20})
        with patch.object(W, '_search_ddgs') as ddgs:
            result = self.search(max_results=999)
        ddgs.assert_not_called()
        self.assertEqual(result['answer'], '')
        self.assertEqual(len(result['results']), 5)
        self.assertLessEqual(len(result['results'][0]['snippet']), 900)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ('POST', '/search'))
        body = json.loads(kwargs['body'])
        self.assertEqual((body['search_depth'], body['max_results']), ('basic', 5))
        for name in ('include_answer', 'include_raw_content', 'auto_parameters', 'include_images'):
            self.assertIs(body[name], False)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.usage()['month_reserved'], 1)

    def test_cached_normalized_query_does_not_request_or_reserve_again(self):
        self.assertFalse(self.search()['cached'])
        self.secret.unlink()  # Cached public sources need no credential.
        result = W.search(' foreign   official source ', source_scope='overseas')
        self.assertTrue(result['cached'])
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.usage()['month_reserved'], 1)
        W.clear_cache()
        self.assertEqual(self.usage()['month_reserved'], 1)

    def test_empty_success_is_cached_and_news_has_a_separate_cache(self):
        self.set_body({'results': []})
        self.search()
        self.assertTrue(self.search()['cached'])
        self.set_body({'results': []})
        self.assertFalse(self.search(kind='news')['cached'])
        body = json.loads(self.connection.request.call_args.kwargs['body'])
        self.assertEqual(body['topic'], 'news')
        self.assertEqual(self.usage()['month_reserved'], 2)

    def test_expired_cache_reserves_a_new_credit(self):
        self.search()
        cache = next((self.root / 'data/cache/search').glob('*.json'))
        data = json.loads(cache.read_text()); data['_ts'] = 0
        cache.write_text(json.dumps(data))
        self.set_body({'results': []})
        self.assertFalse(self.search()['cached'])
        self.assertEqual(self.usage()['month_reserved'], 2)

    def test_missing_key_disabled_and_invalid_query_do_not_reserve(self):
        self.secret.unlink()
        with self.assertRaisesRegex(T.TavilyError, 'tavily_api_key'):
            self.search()
        with patch.dict(W.TOOLS_CFG, {'tavily': {'enabled': False}}):
            with self.assertRaisesRegex(T.TavilyError, '关闭'):
                self.search()
        for query in ('', 'x' * 501):
            with self.assertRaises(ValueError):
                W.search(query, source_scope='overseas')
        with self.assertRaises(ValueError):
            W.search('query', source_scope='anything')
        self.factory.assert_not_called()
        self.assertFalse((self.root / 'data/tavily_usage.json').exists())

    def test_private_key_reload_and_legacy_environment_fallback(self):
        self.assertEqual(T.load_key(self.root, {}), 'test-only-private-key')
        self.secret.write_text('{"tavily_api_key":"replacement"}')
        self.assertEqual(T.load_key(self.root, {}), 'replacement')
        self.secret.unlink()
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'environment'}):
            self.assertEqual(T.load_key(self.root, {}), 'environment')
        self.assertEqual(T.load_key(self.root, {'tavily_key': 'legacy'}), 'legacy')

    def test_daily_monthly_reset_and_clock_rollback(self):
        settings = {'monthly_limit': 2, 'daily_limit': 1}
        def reserve(date):
            T.reserve(self.root, settings, now=datetime.fromisoformat(date).replace(tzinfo=timezone.utc))
        reserve('2026-10-01')
        with self.assertRaisesRegex(T.TavilyError, '每日'):
            reserve('2026-10-01')
        reserve('2026-10-02')
        with self.assertRaisesRegex(T.TavilyError, '每月'):
            reserve('2026-10-03')
        reserve('2026-11-01')
        self.assertEqual(self.usage()['month_reserved'], 1)
        with self.assertRaises(T.TavilyError):
            reserve('2026-10-01')

    def test_concurrent_threads_cannot_exceed_quota(self):
        def attempt(_):
            try:
                T.reserve(self.root, {'daily_limit': 3})
                return 1
            except T.TavilyError:
                return 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(16))), 3)
        self.assertEqual(self.usage()['month_reserved'], 3)

    def test_concurrent_processes_cannot_exceed_quota(self):
        script = '''import sys
sys.path.insert(0, sys.argv[1])
import tavily_search as t
try:
    t.reserve(sys.argv[2], {'daily_limit': 3})
    print('1')
except t.TavilyError:
    print('0')
'''
        def attempt(_):
            result = subprocess.run([sys.executable, '-c', script, str(Path(T.__file__).parent),
                                     str(self.root)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return int(result.stdout.strip())
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(6))), 3)

    def test_damaged_or_unwritable_usage_stops_before_network(self):
        path = self.root / 'data/tavily_usage.json'
        for content in ('not json', '[]', '{"schema":2}', '{"schema":1}'):
            path.write_text(content)
            with self.assertRaisesRegex(T.TavilyError, '用量记录'):
                self.search()
            self.assertEqual(path.read_text(), content)
        path.unlink()
        with patch.object(T.STORE, 'write_json', side_effect=OSError('disk full')):
            with self.assertRaises(T.TavilyError):
                self.search()
        self.factory.assert_not_called()

    def test_zero_limit_blocks_and_invalid_limit_never_spends(self):
        for settings in ({'monthly_limit': 0}, {'daily_limit': 0},
                         {'monthly_limit': 1001}, {'daily_limit': -1}):
            with patch.dict(W.TOOLS_CFG, {'tavily': settings}), self.assertRaises(T.TavilyError):
                self.search()
        self.factory.assert_not_called()

    def test_provider_errors_keep_reservation_and_never_echo_secrets(self):
        for status in (301, 401, 403, 429, 432, 433, 500):
            self.response.status = status
            self.set_body({'error': 'test-only-private-key'})
            with self.assertRaises(T.TavilyError) as caught:
                self.search(use_cache=False)
            self.assertNotIn('test-only-private-key', str(caught.exception))
        self.assertEqual(self.factory.call_count, 7)
        self.assertEqual(self.usage()['month_reserved'], 7)
        self.response.read1.assert_not_called()

    def test_network_exception_and_malformed_response_are_sanitized(self):
        self.connection.request.side_effect = TimeoutError('test-only-private-key')
        with self.assertRaises(T.TavilyError) as caught:
            self.search()
        self.assertNotIn('test-only-private-key', str(caught.exception))
        self.connection.request.side_effect = None
        self.set_body(b'test-only-private-key')
        with self.assertRaises(T.TavilyError) as caught:
            self.search()
        self.assertNotIn('test-only-private-key', str(caught.exception))
        self.assertEqual(self.usage()['month_reserved'], 2)

    def test_response_size_is_bounded_and_connection_closes(self):
        self.set_body(b'x' * (T.MAX_RESPONSE + 20))
        with self.assertRaisesRegex(T.TavilyError, '过大'):
            self.search()
        self.connection.close.assert_called_once()
        self.assertTrue(all(call.args[0] <= 32768 for call in self.response.read1.call_args_list))

    def test_connection_close_response_finishes_without_touching_closed_socket(self):
        body = b'{"results":[]}'
        wire = io.BytesIO(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                          + str(len(body)).encode() + b'\r\n\r\n' + body)
        sock = Mock()
        sock.makefile.return_value = wire
        def set_timeout(_):
            if wire.closed:
                raise OSError('socket already closed')
        sock.settimeout.side_effect = set_timeout
        response = T.http.client.HTTPResponse(sock)
        response.begin()
        self.connection.sock = sock
        self.connection.getresponse.return_value = response
        self.assertEqual(self.search()['results'], [])
        self.assertTrue(wire.closed)



    def test_trickling_response_stops_at_total_deadline(self):
        self.set_body(b'xxxxxxxx')
        with patch.object(T.time, 'monotonic', side_effect=[0, 0, 0, 13]):
            with self.assertRaisesRegex(T.TavilyError, '超时'):
                T._post({}, 'test-key', 12)
        self.connection.close.assert_called_once()

    def test_tool_dispatch_exposes_scope_and_keeps_prompt_small(self):
        self.set_body({'results': [{'title': 'source', 'url': 'https://example.org',
                                  'content': 'x' * 5000}] * 5})
        result = L.call('web_search', {'query': 'foreign official source', 'source_scope': 'overseas'})
        self.assertIn('境外检索来源摘录', result)
        self.assertLessEqual(len(result), 1800)
        spec = next(s['function'] for s in L.SPECS if s['function']['name'] == 'web_search')
        self.assertEqual(spec['parameters']['properties']['source_scope']['enum'], ['default', 'overseas'])

    def test_failed_cache_write_keeps_the_successful_result(self):
        with patch.object(W, '_cache_put', side_effect=OSError('disk full')):
            self.assertEqual(len(self.search()['results']), 1)
        self.assertEqual(self.usage()['month_reserved'], 1)


if __name__ == '__main__':
    unittest.main()
