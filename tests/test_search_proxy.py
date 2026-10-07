"""Routing, detection and consumer regressions using public synthetic state only."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import memory as M
import proxy as P
import tools as T
import update as U


class ProxySelection(unittest.TestCase):
    def setUp(self):
        self.cfg = {'tools': {'proxy': 'auto'}}
        for item in (patch.object(M, 'CFG', self.cfg),
                     patch.dict(P._cache, {'value': None, 'expires': 0.0}, clear=True),
                     patch.object(P, 'from_windows_registry', return_value=None),
                     patch.object(P, 'from_port_scan', return_value=[])):
            item.start()
            self.addCleanup(item.stop)

    def test_off_and_explicit_take_precedence_over_cached_auto(self):
        P._cache.update(value='http://127.0.0.1:7890', expires=float('inf'))
        with patch.object(P, 'verify') as verify:
            for mode in ('off', 'none', 'false', '0'):
                self.cfg['tools']['proxy'] = mode
                self.assertIsNone(P.detect())
            self.cfg['tools']['proxy'] = 'http://127.0.0.1:12345'
            self.assertEqual(P.detect(), 'http://127.0.0.1:12345')
            verify.assert_not_called()

    def test_invalid_configuration_fails_instead_of_selecting_direct(self):
        for value in (False, 0, [], {}, 'bad-host:1234', 'http://host:bad',
                      'http://host:0', 'http://host:99999', 'http://host/path',
                      'http://user:secret@host?key=value', 'socks4://host:1080',
                      'http://host\n:7890'):
            with self.subTest(value=repr(value)):
                self.cfg['tools']['proxy'] = value
                with self.assertRaises(P.ProxyError) as err:
                    P.detect()
                self.assertNotIn('secret', str(err.exception))

    def test_scan_keeps_all_listening_ports(self):
        # Test the actual scanner, not the setUp substitute.
        with patch.object(P, 'COMMON_PORTS', [(1, 'http'), (2, 'socks5h'), (3, 'http')]), \
             patch.object(P, '_port_open', side_effect=lambda p: p != 2):
            urls = self.scanner()
        self.assertEqual(urls, ['http://127.0.0.1:1', 'http://127.0.0.1:3'])

    scanner = staticmethod(P.from_port_scan)

    def test_invalid_first_listener_does_not_hide_later_proxy_and_duplicates_removed(self):
        first, second = 'http://127.0.0.1:7890', 'socks5h://127.0.0.1:1080'
        with patch.object(P, 'from_windows_registry', return_value=first), \
             patch.object(P, 'from_port_scan', return_value=[first, second]), \
             patch.object(P, 'verify', side_effect=lambda url, timeout: (url == second, 'synthetic')) as v:
            self.assertEqual(P.detect(), second)
        self.assertEqual([call.args[0] for call in v.call_args_list].count(first), 1)
        self.assertEqual({call.args[0] for call in v.call_args_list}, {first, second})

    def test_registry_has_priority_over_a_faster_port_candidate(self):
        urls = ['http://127.0.0.1:1', 'http://127.0.0.1:2']
        with patch.object(P, 'from_windows_registry', return_value=urls[0]), \
             patch.object(P, 'from_port_scan', return_value=[urls[1]]), \
             patch.object(P, 'verify', return_value=(True, 'HTTP 204')):
            self.assertEqual(P.detect(), urls[0])

    def test_negative_cache_expires_so_proxy_can_be_opened_later(self):
        with patch.object(P.time, 'monotonic', return_value=100) as now, \
             patch.object(P, 'from_port_scan', return_value=[]) as scan, \
             patch.object(P, 'verify', return_value=(True, 'HTTP 204')):
            self.assertIsNone(P.detect())
            scan.return_value = ['http://127.0.0.1:7890']
            now.return_value = 109
            self.assertIsNone(P.detect())
            self.assertEqual(scan.call_count, 1)
            now.return_value = 111
            self.assertEqual(P.detect(), scan.return_value[0])

    def test_positive_cache_expires_and_force_rechecks(self):
        with patch.object(P.time, 'monotonic', return_value=100) as now, \
             patch.object(P, 'from_port_scan', return_value=['http://127.0.0.1:7890']), \
             patch.object(P, 'verify', return_value=(True, 'HTTP 204')) as v:
            self.assertIsNotNone(P.detect())
            now.return_value = 120
            self.assertIsNotNone(P.detect())
            self.assertEqual(v.call_count, 1)
            v.return_value = (False, 'HTTP 407')
            self.assertIsNone(P.detect(force=True))
            v.return_value = (True, 'HTTP 204')
            self.assertIsNotNone(P.detect(force=True))
            now.return_value = 181
            self.assertIsNotNone(P.detect())
            self.assertEqual(v.call_count, 4)

    def test_config_change_during_probe_wins(self):
        entered, finish = threading.Event(), threading.Event()
        def verify(*_):
            entered.set()
            self.assertTrue(finish.wait(3))
            return True, 'HTTP 204'
        result = []
        with patch.object(P, 'from_port_scan', return_value=['http://127.0.0.1:7890']), \
             patch.object(P, 'verify', side_effect=verify):
            worker = threading.Thread(target=lambda: result.append(P.detect()))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.cfg['tools']['proxy'] = 'off'
                finish.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            finally:
                finish.set()
                worker.join(3)
        self.assertEqual(result, [None])

    def test_explicit_failed_probe_never_changes_actual_request_route(self):
        route = 'http://user:private-password@127.0.0.1:7890'
        self.cfg['tools']['proxy'] = route
        with patch.object(P, 'verify', return_value=(False, 'HTTP 407')):
            with self.assertRaises(P.ProxyError):
                P.refresh()
            self.assertEqual(P.detect(), route)
        fake = MagicMock()
        fake.request.side_effect = OSError(route)
        with patch.object(P, 'make_client', return_value=fake) as build:
            with self.assertRaises(P.ProxyError) as err:
                P.request('https://target.invalid/')
        self.assertEqual(build.call_args.args[0], route)
        self.assertNotIn('private-password', str(err.exception))
        self.assertEqual(build.call_count, 1)

    def test_status_verbose_and_errors_hide_proxy_credentials(self):
        route = 'http://private-user:private-password@127.0.0.1:7890'
        self.cfg['tools']['proxy'] = route
        with patch.object(P, 'verify', return_value=(False, 'HTTP 407')):
            text = json.dumps(P.status(force=True), ensure_ascii=False)
        self.cfg['tools']['proxy'] = 'auto'
        stderr = io.StringIO()
        with patch.object(P, 'from_port_scan', return_value=[route]), \
             patch.object(P, 'verify', return_value=(True, 'HTTP 204')), redirect_stderr(stderr):
            P.detect(verbose=True, force=True)
        text += stderr.getvalue() + P.error_text(OSError(route))
        self.assertNotIn('private-user', text)
        self.assertNotIn('private-password', text)
        self.assertIn('127.0.0.1:7890', text)

    def test_verify_rejects_http_errors_redirects_and_login_pages(self):
        fake = MagicMock()
        for status, body, expected in ((204, b'', True), (200, b'', True),
                                       (200, b'<html>sign in</html>', False),
                                       (302, b'', False), (403, b'', False),
                                       (407, b'', False), (502, b'', False)):
            fake.get.return_value = SimpleNamespace(status_code=status, content=body)
            with self.subTest(status=status, body=body), patch.object(P, 'make_client', return_value=fake) as build:
                self.assertEqual(P.verify('socks5h://127.0.0.1:1080')[0], expected)
                self.assertFalse(build.call_args.kwargs['follow_redirects'])
        fake.get.side_effect = OSError('http://private:password@proxy.invalid:10')
        with patch.object(P, 'make_client', return_value=fake):
            self.assertNotIn('password', P.verify('http://proxy.invalid:10')[1])


class SearchConsumers(unittest.TestCase):
    def setUp(self):
        self.cfg = {'tools': {'proxy': 'off'}}
        work = Path(os.environ['AIPET_TEST_WORK'])
        work.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temp.cleanup)
        for item in (patch.object(M, 'CFG', self.cfg), patch.object(T, 'TOOLS_CFG', self.cfg['tools']),
                     patch.object(T, 'CACHE_DIR', Path(temp.name))):
            item.start()
            self.addCleanup(item.stop)

    def test_text_and_news_proxy_go_to_client_not_search_keywords(self):
        route = 'socks5h://127.0.0.1:1080'
        self.cfg['tools']['proxy'] = route
        client = MagicMock()
        client.__enter__.return_value = client
        client.text.return_value = [{'title': 'found', 'href': 'https://found.invalid', 'body': 'summary'}]
        client.news.return_value = []
        with patch.object(P, 'ddgs_client', return_value=client) as factory:
            self.assertEqual(T.search('query', use_cache=False)['results'][0]['title'], 'found')
            self.assertEqual(T.search('query', kind='news', use_cache=False)['results'][0]['title'], 'found')
        self.assertTrue(all(call.args[0] == route for call in factory.call_args_list))
        for call in client.text.call_args_list + client.news.call_args_list:
            self.assertNotIn('proxy', call.kwargs)
        self.assertEqual(client.text.call_args.kwargs['timelimit'], 'w')

    def test_invalid_proxy_is_not_swallowed_by_search_or_fetch(self):
        self.cfg['tools']['proxy'] = 'malformed'
        with patch.object(P, 'ddgs_client') as factory:
            with self.assertRaises(P.ProxyError): T.search('query', use_cache=False)
            with self.assertRaises(P.ProxyError): T.fetch('https://target.invalid', use_cache=False)
            factory.assert_not_called()

    def test_ddgs_diagnostics_do_not_assert_dns_pollution_or_leak_credentials(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.text.side_effect = OSError('connection http://secret-user:password@proxy.invalid:1080')
        self.cfg['tools']['proxy'] = 'http://proxy.invalid:8080'
        with patch.object(P, 'ddgs_client', return_value=client):
            text = T.as_prompt_block('query')
        self.assertIn('当前使用代理', text)
        self.assertNotIn('password', text)
        self.assertNotIn('secret-user', text)
        self.assertNotIn('DNS 污染', text)

    def test_search_cache_hit_does_not_trigger_probe_or_request(self):
        hit = {'backend': 'ddgs', 'answer': '', 'results': [{'title': 'cached'}]}
        with patch.object(T, '_cache_get', return_value=hit), patch.object(P, 'detect') as detect:
            self.assertTrue(T.search('query')['cached'])
            detect.assert_not_called()

    def test_local_setup_errors_keep_actionable_guidance(self):
        self.assertIn('tavily_api_key', T.as_prompt_block('query', source_scope='overseas'))
        self.cfg['tools']['search_backend'] = 'searxng'
        self.assertIn('searxng_url', T.as_prompt_block('query'))
        self.cfg['tools'].update(search_backend='ddgs', proxy='http://proxy.invalid:8080')
        with patch.object(P, 'ddgs_client', side_effect=ImportError('synthetic missing dependency')):
            self.assertIn('准备环境', T.as_prompt_block('query'))

    def test_tavily_is_explicit_while_searxng_and_update_keep_proxy_routing(self):
        self.cfg['tools'].update(tavily_key='fixture-key', searxng_url='http://127.0.0.1:8000')
        payload = {'answer': 'a', 'results': [{'title': 'found', 'url': 'https://found.invalid'}]}
        with patch.object(T.TAVILY, '_post', return_value=payload) as paid, patch.object(P, 'request') as req:
            T.search('query', source_scope='overseas', use_cache=False)
            self.assertEqual(paid.call_args.args[0]['search_depth'], 'basic')
            self.assertEqual(paid.call_args.args[1], 'fixture-key')
            req.assert_not_called()
        with patch.object(P, 'request', return_value=SimpleNamespace(text=json.dumps(payload))) as req:
            T.search('query', backend='searxng', use_cache=False)
            self.assertIn('/search?q=query', req.call_args.args[0])
            req.return_value = SimpleNamespace(text='[{"name":"v0.5.0"}]')
            self.assertEqual(U.fetch_tags(), ['v0.5.0'])
            self.assertIn('api.github.com', req.call_args.args[0])

    def test_fetch_fallback_reuses_the_same_selected_proxy(self):
        route = 'http://127.0.0.1:7890'
        self.cfg['tools']['proxy'] = route
        client = MagicMock()
        client.__enter__.return_value = client
        client.extract.side_effect = ValueError('extraction failed')
        with patch.object(P, 'ddgs_client', return_value=client), \
             patch.object(P, 'request', return_value=SimpleNamespace(text='<p>fallback text</p>')) as req:
            self.assertEqual(T.fetch('https://target.invalid', use_cache=False), 'fallback text')
        self.assertEqual(req.call_args.kwargs['proxy_url'], route)

    def test_update_handles_http_and_proxy_failure_safely(self):
        for error in (urllib.error.HTTPError('https://unused.invalid', 403, 'blocked', None, None),
                      OSError('http://secret-user:password@proxy.invalid:1080')):
            with patch.object(P, 'request', side_effect=error):
                result = U.check()
            self.assertFalse(result['ok'])
            self.assertNotIn('password', result['error'])
            self.assertNotIn('secret-user', result['error'])

    def test_actual_pinned_library_constructs_routed_subclass_and_isolates_engines(self):
        before_socket, before_env = socket.socket, dict(os.environ)
        with patch.dict(os.environ, {'DDGS_PROXY': 'http://127.0.0.1:6666'}):
            direct = P.ddgs_client(None)
            proxied = P.ddgs_client('socks5h://127.0.0.1:1080')
            self.assertEqual(type(direct).__name__, 'RoutedDDGS')
            self.assertIsNone(direct._proxy)
            # Construction and _get_engines create real pinned native clients; no search is performed.
            first = direct._get_engines('text', 'auto')
            second = proxied._get_engines('news', 'auto')
            self.assertTrue(first and second)
            self.assertTrue(all(x.http_client.client.proxy is None for x in first))
            self.assertTrue(all(x.http_client.client.proxy == 'socks5h://127.0.0.1:1080' for x in second))
            self.assertTrue({id(x) for x in first}.isdisjoint({id(x) for x in second}))
        self.assertIs(socket.socket, before_socket)
        self.assertEqual(dict(os.environ), before_env)


if __name__ == '__main__':
    unittest.main()
