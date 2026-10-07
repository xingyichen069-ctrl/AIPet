"""Usage queries must not spend search credits; domestic reads stay direct."""
import base64
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import local_tools as L
import memory as M
import qq_bridge as B
import tavily_search as T
import tools as W
import web_direct as D


class Usage(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);(self.root/'data').mkdir()
        (self.root/'data/secrets.json').write_text(json.dumps({'tavily_api_key':'test-only-private-key'}))
        self.counts={'key':{'usage':3,'limit':None,'search_usage':3,'private':'must not return'},
                     'account':{'plan_usage':7,'plan_limit':1000,'email':'must not return'}}
        self.cfg={'tools':{'search_backend':'ddgs','proxy':'off'}}
        for item in (patch.object(M,'ROOT',self.root),patch.object(M,'CFG',self.cfg),
                     patch.object(W,'TOOLS_CFG',self.cfg['tools']),patch.object(W,'CACHE_DIR',self.root/'data/cache')):
            item.start();self.addCleanup(item.stop)

    def test_usage_reads_official_totals_and_never_reserves_or_searches(self):
        T.reserve(self.root,{})
        path=self.root/'data/tavily_usage.json';before=path.read_bytes()
        with patch.object(T,'_request',return_value=self.counts) as request,patch.object(T,'reserve') as reserve:
            result=T.usage(self.root,{})
            self.assertEqual(result['provider']['account_used'],7)
            self.assertEqual(result['local']['month_reserved'],1)
            self.assertEqual(request.call_args.args[:3],('GET','/usage',None))
            reserve.assert_not_called()
            report=L.tavily_usage()
            self.assertIn('993',report);self.assertIn('一分钟内缓存',report)
            self.assertEqual(request.call_count,1)
        self.assertEqual(path.read_bytes(),before)
        cached=(self.root/'data/cache/tavily_usage.json').read_text()
        self.assertNotIn('test-only-private-key',cached);self.assertNotIn('must not return',cached)

    def test_count_query_rolls_calendar_in_memory_without_resetting_file(self):
        T.reserve(self.root,{},now=datetime(2020,1,1,tzinfo=timezone.utc))
        path=self.root/'data/tavily_usage.json';before=path.read_bytes()
        with patch.object(T,'_request',return_value=self.counts):
            self.assertEqual(T.usage(self.root,{})['local']['month_reserved'],0)
        self.assertEqual(path.read_bytes(),before)

    def test_key_rotation_and_new_reservation_invalidate_usage_cache(self):
        with patch.object(T,'_request',return_value=self.counts) as request:
            T.usage(self.root,{})
            T.reserve(self.root,{})
            T.usage(self.root,{})
            (self.root/'data/secrets.json').write_text('{"tavily_api_key":"new-test-only-key"}')
            T.usage(self.root,{})
            self.assertEqual(request.call_count,3)

    def test_provider_failure_preserves_local_counts_and_hides_secret(self):
        T.reserve(self.root,{})
        with patch.object(T,'_request',side_effect=OSError('test-only-private-key')):
            report=T.usage_text(self.root,{})
        self.assertIn('1/20',report);self.assertIn('查询失败',report)
        self.assertNotIn('test-only-private-key',report)

    def test_corrupt_counter_is_never_silently_cleared(self):
        path=self.root/'data/tavily_usage.json';path.write_text('{broken')
        with patch.object(T,'_request') as request:
            self.assertIn('未清零',T.usage_text(self.root,{}));request.assert_not_called()
        self.assertEqual(path.read_text(),'{broken')

    def test_qq_usage_command_requires_owner_and_does_not_invoke_search(self):
        with patch.object(L,'tavily_usage',return_value='usage counts') as usage:
            for text in ('/tavily','Tavily用量','查询tavily次数','tavily还剩多少次？'):
                self.assertEqual(B.command_reply(B._fake(content=text),{'is_owner':True}),'usage counts')
            self.assertIn('仅主人',B.command_reply(B._fake(content='/tavily'),{'is_owner':False}))
            self.assertEqual(usage.call_count,4)
        with L.bind_context(source='qq',is_owner=False),patch.object(T,'usage_text') as query:
            self.assertIn('仅主人',L.tavily_usage());query.assert_not_called()
        self.assertIn('tavily_usage',L.DISPATCH)
        self.assertIn('tavily_usage',{spec['function']['name'] for spec in L.SPECS})

    def test_ordinary_queries_ignore_global_tavily_and_never_use_key(self):
        with patch.object(W.PROXY,'detect',return_value=None), \
                patch.object(W.DIRECT,'search',return_value=[{'title':'fixture','url':'https://www.gov.cn/','snippet':'public'}]) as direct, \
                patch.object(T,'search') as tavily:
            self.cfg['tools']['search_backend']='tavily'
            for query in ('国内天气','Python 国内镜像','an ordinary English query'):
                result=W.search(query,use_cache=False)
                self.assertEqual(result['backend'],'bing_direct')
            self.assertEqual(direct.call_count,3);tavily.assert_not_called()
        self.assertFalse((self.root/'data/tavily_usage.json').exists())

    def test_ordinary_failure_cannot_fall_back_to_tavily(self):
        with patch.object(W.PROXY,'detect',return_value=None), \
                patch.object(W.DIRECT,'search',side_effect=D.WebError('connection failed')), \
                patch.object(T,'search') as tavily:
            with self.assertRaises(D.WebError):W.search('国内新闻',use_cache=False)
            tavily.assert_not_called()

    def test_proxy_route_retains_existing_unmetered_backend(self):
        with patch.object(W.PROXY,'detect',return_value='http://proxy.invalid:80'), \
                patch.object(W,'_search_ddgs',return_value=[]) as regular,patch.object(T,'search') as tavily:
            self.assertEqual(W.search('public query',use_cache=False)['backend'],'ddgs')
            regular.assert_called_once();tavily.assert_not_called()


class DirectWeb(unittest.TestCase):
    def test_search_extracts_results_not_advertising_or_navigation(self):
        target='https://www.sjtu.edu.cn/'
        wrapped='https://www.bing.com/ck/a?u=a1'+base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
        fixture=f'<h2><a href="https://ad.invalid/">ad</a></h2><li class="b_algo"><h2><a href="{wrapped}">上海<strong>交通大学</strong></a></h2><div><p>学校 &amp; 新闻</p></div></li>'
        with patch.object(D,'read',return_value=fixture) as read:
            result=D.search('上海交通大学',3,'text')
        self.assertEqual(result[0]['url'],target);self.assertEqual(result[0]['title'],'上海交通大学')
        self.assertEqual(result[0]['snippet'],'学校 & 新闻');self.assertEqual(len(result),1)
        self.assertIn('https://cn.bing.com/search?',read.call_args.args[0])

    def test_captcha_is_an_error_instead_of_a_fabricated_result(self):
        with patch.object(D,'read',return_value='<title>验证</title>'):
            with self.assertRaisesRegex(D.WebError,'未调用 Tavily'):D.search('query',3,'text')

    def test_direct_http_bounds_response_and_closes_connection(self):
        response=Mock(status=200);response.isclosed.return_value=False
        response.read1.return_value=b'x'*11
        connection=Mock();connection.getresponse.return_value=response
        with patch.object(D.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaisesRegex(D.WebError,'超过读取上限'):D.read('https://www.gov.cn/',limit=10)
        response.close.assert_called_once();connection.close.assert_called_once()
