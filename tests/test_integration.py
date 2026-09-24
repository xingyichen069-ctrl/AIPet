import json
import os
import sqlite3
import time
import unittest
import zipfile
from unittest.mock import patch
import test_companion as TC
import companion as C
import brain as B
import backup as BK
import local_tools as LT


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


class Integration(unittest.TestCase):
    setUp = TC.Services.setUp
    tearDown = TC.Services.tearDown

    def test_provider_settings_schema_is_read_by_brain(self):
        with patch.object(B, 'load_secrets', return_value={
                'auth_token': 'provider-token',
                'base_url': 'https://provider.example/v1',
                'model': 'gpt-5.6-luna'}), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(B.api_key(), 'provider-token')
            self.assertEqual(B.base_url(), 'https://provider.example/v1')
            self.assertEqual(B.provider_model(), 'gpt-5.6-luna')

    def test_model_tool_round_creates_real_agreement_then_answers(self):
        call = {'reasoning_content': 'tool decision', 'tool_calls': [{'index': 0, 'id': 't1',
                'function': {'name': 'agreement', 'arguments': json.dumps({'action':'create','title':'收衣服','minutes':40})}}]}
        payload = {'messages': [{'role':'user','content':'40分钟后提醒我收衣服'}], 'max_tokens': 100,
                   'tools': [t for t in LT.SPECS if t['function']['name'] == 'agreement']}
        with patch.object(B, 'api_key', return_value='test-key'), \
             patch.object(B, 'build_payload', return_value=(payload, {})), \
             patch.object(B, '_request', side_effect=[Response(call), Response({'content':'已记下。'})]) as request, \
             patch.object(C, 'Store', return_value=self.store):
            events = list(B.stream('40分钟后提醒我收衣服'))
        self.assertIn(('content', '已记下。'), events)
        self.assertEqual(len(self.store.tasks()), 1)
        messages = request.call_args.args[0]['messages']
        self.assertEqual(messages[-2]['reasoning_content'], 'tool decision')
        self.assertEqual(messages[-1]['role'], 'tool')
        self.assertIn('已保存', messages[-1]['content'])

    def test_unexposed_tool_cannot_execute(self):
        called = []
        call = {'tool_calls': [{'index':0, 'id':'t1', 'function':
                {'name':'fake_hidden', 'arguments':'{}'}}]}
        payload = {'messages':[{'role':'user','content':'test'}], 'max_tokens':100}
        with patch.object(B,'api_key',return_value='test-key'), \
             patch.object(B,'build_payload',return_value=(payload,{})), \
             patch.object(B,'_request',side_effect=[Response(call),Response({'content':'结束'})]), \
             patch.dict(LT.DISPATCH,{'fake_hidden':lambda args:called.append(args) or 'bad'}):
            list(B.stream('test'))
        self.assertEqual(called,[])

    def test_cancellation_prevents_request_and_tool_execution(self):
        payload = {'messages': [], 'max_tokens': 100}
        with patch.object(B, 'api_key', return_value='test-key'), \
             patch.object(B, 'build_payload', return_value=(payload, {})), \
             patch.object(B, '_request') as request:
            list(B.stream('不要继续', cancelled=lambda: True))
        request.assert_not_called()

    def test_deadline_prevents_provider_request(self):
        payload = {'messages': [], 'max_tokens': 100}
        with patch.object(B, 'api_key', return_value='test-key'), \
             patch.object(B, 'build_payload', return_value=(payload, {})), \
             patch.object(B, '_request') as request:
            events = list(B.stream('已经过期', deadline=time.monotonic() - 1))
        request.assert_not_called()
        self.assertEqual(events[0][0], 'level')

    def test_deadline_prevents_tool_dispatch(self):
        called = []
        with patch.dict(LT.DISPATCH, {'fake': lambda args: called.append(args) or 'ok'}, clear=False):
            result = LT.call('fake', {}, deadline=time.monotonic() - 1)
        self.assertIn('超过截止时间', result)
        self.assertEqual(called, [])

    def test_backup_contains_consistent_conversations_and_agreements(self):
        self.store.add_message('user', '继续方案')
        self.store.create_task('收衣服', seconds=600)
        with patch.object(BK, 'BACKUP_DIR', self.root / 'backups'):
            path = BK.create('test')
        with zipfile.ZipFile(path) as archive:
            raw = archive.read('data/companion.sqlite3')
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(raw)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
