"""Regression cases from the direct-search audit; no live network or credits."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import access_scope as A
import local_tools as L
import memory as M
import search_support as S
import tools as W
import web_direct as D


def row(title='Reimu Hakurei', url='https://example.org/reimu', snippet='public source'):
    return dict(title=title, url=url, snippet=snippet, date='', source='fixture')


class SearchOptimization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for item in (patch.object(M, 'ROOT', self.root),
                     patch.object(W, 'CACHE_DIR', self.root/'data/cache'),
                     patch.object(W, 'TOOLS_CFG', {'search_backend': 'ddgs'}),
                     patch.object(W.PROXY, 'detect', return_value=None),
                     patch.object(W, '_search_tavily', side_effect=AssertionError('paid search forbidden'))):
            item.start(); self.addCleanup(item.stop)
        with S._LOCK:
            S._FAILURES.clear()

    def events(self):
        return [json.loads(line) for line in (W.CACHE_DIR/'search-events.jsonl').read_text().splitlines()]

    def test_known_alias_retries_once_free_with_shared_deadline_and_cache(self):
        empty = D.WebError('no match', 'no_results')
        with patch.object(D, 'search', side_effect=[empty, [row()]]) as direct:
            result = W.search('博丽灵梦', fallback_query='Reimu Hakurei')
            self.assertEqual(result['matched_query'], 'Reimu Hakurei')
            self.assertTrue(result['fallback_used'])
            self.assertEqual([c.args[0] for c in direct.call_args_list], ['博丽灵梦', 'Reimu Hakurei'])
            self.assertEqual(direct.call_args_list[0].kwargs['deadline'], direct.call_args_list[1].kwargs['deadline'])
            self.assertTrue(W.search('博丽灵梦', fallback_query='Reimu Hakurei')['cached'])
            self.assertEqual(direct.call_count, 2)
        self.assertEqual([e['outcome'] for e in self.events()], ['no_results', 'ok', 'ok'])
        self.assertEqual([e['cached'] for e in self.events()], [False, False, True])

    def test_original_success_skips_alternative(self):
        with patch.object(D, 'search', return_value=[row('上海交通大学', 'https://www.sjtu.edu.cn/')]) as direct:
            result = W.search('上海交通大学 官网', fallback_query='Shanghai Jiao Tong University')
            direct.assert_called_once()
            self.assertFalse(result['fallback_used'])

    def test_unrelated_results_are_not_citable_and_alias_can_recover(self):
        unrelated = [row('经济学专业介绍', snippet='如何填报志愿')]
        with patch.object(D, 'search', side_effect=[unrelated, [row()]]) as direct:
            result = W.search('灵梦', fallback_query='Reimu Hakurei')
            self.assertEqual(result['results'][0]['title'], 'Reimu Hakurei')
            self.assertEqual(direct.call_count, 2)
        self.assertEqual(self.events()[0]['outcome'], 'low_relevance')

    def test_no_alternative_does_not_invent_or_repeat_query(self):
        with patch.object(D, 'search', return_value=[row('经济学专业介绍')]) as direct:
            result = W.search('灵梦')
            self.assertEqual(result['results'], [])
            self.assertEqual(result['status'], 'low_relevance')
            direct.assert_called_once()

    def test_two_empty_attempts_are_cached_briefly_but_not_as_hour_long_success(self):
        with patch.object(D, 'search', side_effect=D.WebError('no match', 'no_results')) as direct:
            for _ in range(3):
                result = W.search('博丽灵梦', fallback_query='Reimu Hakurei')
                self.assertEqual(result['results'], [])
            self.assertEqual(direct.call_count, 2)
            self.assertTrue(result['cached'])
            self.assertEqual(list((W.CACHE_DIR/'search').glob('*.json')), [])
            with patch.object(S.time, 'monotonic', return_value=time.monotonic()+31):
                W.search('博丽灵梦', fallback_query='Reimu Hakurei')
            self.assertEqual(direct.call_count, 4)

    def test_failure_cache_cannot_prevent_new_alternative_or_new_route(self):
        with patch.object(D, 'search', side_effect=[D.WebError('empty', 'no_results'), [row()]]) as direct:
            W.search('博丽灵梦')
            self.assertTrue(W.search('博丽灵梦', fallback_query='Reimu Hakurei')['results'])
            self.assertEqual(direct.call_count, 2)
        with patch.object(W.PROXY, 'detect', return_value='http://proxy.invalid'), \
                patch.object(W, '_search_ddgs', return_value=[row('博丽灵梦')]) as proxy:
            self.assertEqual(W.search('博丽灵梦')['backend'], 'ddgs')
            proxy.assert_called_once()

    def test_timeout_verification_http_and_parser_errors_never_trigger_alias(self):
        for code in ('timeout', 'verification', 'http_denied', 'parse_error', 'network_error'):
            with self.subTest(code=code), patch.object(D, 'search', side_effect=D.WebError('fixture', code)) as direct:
                with self.assertRaises(D.WebError):
                    W.search('博丽灵梦', fallback_query='Reimu Hakurei', use_cache=False)
                direct.assert_called_once()

    def test_request_deadline_is_checked_before_any_new_request(self):
        with A.bind(deadline=time.monotonic()+5), patch.object(D, 'search') as direct:
            with self.assertRaises(D.WebError) as error:
                W.search('博丽灵梦', fallback_query='Reimu Hakurei')
            self.assertEqual(error.exception.code, 'timeout')
            direct.assert_not_called()

    def test_same_alias_and_invalid_scope_do_not_broaden_query(self):
        with patch.object(D, 'search', side_effect=D.WebError('empty', 'no_results')) as direct:
            W.search('Reimu Hakurei', fallback_query='  Reimu   Hakurei ')
            direct.assert_called_once()
            with self.assertRaises(ValueError):
                W.search('site:example.org 灵梦', fallback_query='site:other.org Reimu Hakurei')
            self.assertEqual(direct.call_count, 1)

    def test_tool_dispatch_exposes_alternative_and_explains_actual_query(self):
        with patch.object(D, 'search', side_effect=[D.WebError('empty', 'no_results'), [row()]]):
            answer = L.call('web_search', {'query':'博丽灵梦', 'fallback_query':'Reimu Hakurei'})
        self.assertIn('备用关键词：Reimu Hakurei', answer)
        self.assertIn('bing_direct', answer)
        self.assertLessEqual(len(answer), 1800)
        spec = next(x['function'] for x in L.SPECS if x['function']['name']=='web_search')
        self.assertIn('fallback_query', spec['parameters']['properties'])

    def test_explicit_overseas_still_calls_tavily_only_once_and_reuses_empty_cache(self):
        with patch.object(W, '_search_tavily', return_value={'answer':'','results':[]}) as tavily, \
                patch.object(D, 'search') as direct:
            W.search('original', source_scope='overseas', fallback_query='alternative')
            self.assertTrue(W.search('original', source_scope='overseas', fallback_query='another')['cached'])
            tavily.assert_called_once()
            direct.assert_not_called()

    def test_ddgs_receives_proxy_in_client_and_has_no_outer_engine_retry_chain(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False)
        client.text.return_value = [{'title':'Reimu Hakurei','href':'https://example.org/'}]
        with patch.object(W.PROXY, 'ddgs_client', return_value=client) as factory:
            W._search_ddgs('Reimu Hakurei', 3, 'text', proxy_url='http://proxy.invalid', deadline=time.monotonic()+10)
        self.assertEqual(factory.call_args.args[0], 'http://proxy.invalid')
        self.assertLessEqual(factory.call_args.kwargs['timeout'], 3)
        client.text.assert_called_once()
        self.assertNotIn('proxy', client.text.call_args.kwargs)

    def test_diagnostics_are_bounded_private_metadata_and_logging_failure_is_nonfatal(self):
        with A.bind(message_id='private-message-id'), patch.object(D, 'search', return_value=[row('public query')]):
            W.search('public query')
        raw = (W.CACHE_DIR/'search-events.jsonl').read_text()
        for secret in ('public query', 'example.org', 'private-message-id', 'public source'):
            self.assertNotIn(secret, raw)
        self.assertIn('request_id', self.events()[0])
        with patch.object(S, 'TRACE_BYTES', 1100):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: S.trace(self.root, W.CACHE_DIR, query=str(i), backend='bing_direct',
                    kind='text', scope='default', outcome='ok', count=1, elapsed=.2), range(35)))
        paths=list(W.CACHE_DIR.glob('search-events*'))
        self.assertEqual(len(paths), 2)
        self.assertLessEqual(sum(p.stat().st_size for p in paths), 2200)
        for p in paths:
            for line in p.read_text().splitlines():
                json.loads(line)
        with patch.object(S.os, 'open', side_effect=OSError('disk full')), patch.object(D, 'search', return_value=[row()]):
            self.assertTrue(W.search('Reimu Hakurei', use_cache=False)['results'])


class ResultClassification(unittest.TestCase):
    def test_site_limits_and_exclusions_are_retained_for_alternative(self):
        self.assertEqual(S.fallback_query('site:example.org 灵梦 -游戏', 'Reimu Hakurei'),
                         'Reimu Hakurei site:example.org -游戏')
        rows=[row(url='https://example.org.evil.invalid/doc'),row(url='https://sub.example.org/doc')]
        self.assertEqual(S.relevant_results('site:example.org Reimu Hakurei', rows), rows[1:])
        with self.assertRaises(D.WebError):
            S.relevant_results('site:example.org/docs Reimu', [row(url='https://example.org/docsfake')])
        self.assertEqual(S.relevant_results('-site:example.org Reimu', [row(url='https://other.org/')])[0]['url'], 'https://other.org/')

    def test_short_terms_and_identical_links_do_not_become_false_failures(self):
        rows=[row('R',url='https://example.org/#part1'),row('R',url='https://example.org/#part2')]
        self.assertEqual(len(S.relevant_results('R',rows)),1)

    def test_empty_page_verification_and_unknown_layout_are_distinct(self):
        for html, code in [('<p>没有与此相关的结果</p><script>challenge()</script>', 'no_results'),
                           ('<title>验证</title>', 'verification'),
                           ('<h1>One last step</h1>', 'verification'),
                           ('<script>challenge()</script><div>new layout</div>', 'parse_error'),
                           ('<title>身份验证教程 - Bing</title>', 'parse_error')]:
            with self.subTest(code=code,html=html), patch.object(D,'read',return_value=html):
                with self.assertRaises(D.WebError) as error:
                    D.search('fixture',5,'text')
                self.assertEqual(error.exception.code,code)

    def test_http_timeout_and_network_errors_close_the_connection_and_keep_category(self):
        for failure, expected in [(TimeoutError(), 'timeout'),(ConnectionError(), 'network_error')]:
            connection=Mock();connection.request.side_effect=failure
            with patch.object(D.http.client,'HTTPSConnection',return_value=connection):
                with self.assertRaises(D.WebError) as error:D.read('https://example.org/')
                self.assertEqual(error.exception.code,expected)
                connection.close.assert_called_once()
        connection=Mock();response=Mock(status=468);connection.getresponse.return_value=response
        with patch.object(D.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(D.WebError) as error:D.read('https://example.org/')
            self.assertEqual((error.exception.code,error.exception.status),('http_denied',468))
            response.close.assert_called_once();connection.close.assert_called_once()

    def test_provider_classification_redacts_raw_proxy_credentials(self):
        error=S.provider_error(TimeoutError('https://user:secret@proxy.invalid/path'))
        self.assertEqual(error.code,'timeout');self.assertNotIn('secret',str(error))
        self.assertEqual(S.provider_error(RuntimeError('No results found.')).code,'no_results')

    def test_failure_cache_is_bounded_and_does_not_retain_tracebacks(self):
        with S._LOCK:S._FAILURES.clear()
        for i in range(200):S.failure_put(str(i),D.WebError('empty','no_results'))
        self.assertEqual(len(S._FAILURES),S.FAILURE_LIMIT)
        self.assertIsNone(S.failure_get('0'))
        error=S.failure_get('199')
        self.assertTrue(error.cached);self.assertIsNone(error.__traceback__)


if __name__=='__main__':
    unittest.main()
