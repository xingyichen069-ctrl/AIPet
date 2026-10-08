import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch
import provider_config as P


class ProviderTests(unittest.TestCase):
    def test_legacy_precedence_and_aliases(self):
        secrets = {'deepseek_api_key': 'old', 'auth_token': 'generic',
                   'deepseek_base_url': P.DEFAULT_BASE, 'model': 'deepseek-v4-pro'}
        env = {'OPENAI_API_KEY': 'env', 'OPENAI_BASE_URL': 'https://example.invalid/v1'}
        c = P.resolve(secrets, {'model': 'deepseek::deepseek-flash'}, env)
        self.assertEqual((c.key, c.base, c.model), ('env', env['OPENAI_BASE_URL'], 'deepseek-v4-pro'))
        self.assertEqual(P.resolve({}, {'model': 'deepseek::deepseek-v4-flash'}, {}).model, 'deepseek-flash')
        self.assertEqual(P.resolve(secrets, {'model': 'custom-exact'}, {}).model, 'custom-exact')

    def test_explicit_profile_does_not_inherit_unrelated_environment(self):
        secrets = {'model_profiles': {'a': {'name': 'A', 'base_url': 'https://example.invalid/v1',
                   'api_key': 'profile-key', 'model': 'org/model:1', 'protocol': 'compatible'}}, 'default_model_profile': 'a'}
        c = P.resolve(secrets, {'model_override': 'org/model:2'}, {'DEEPSEEK_API_KEY': 'wrong'})
        self.assertEqual((c.key, c.model), ('profile-key', 'org/model:2'))
        self.assertNotIn(c.key, repr(c))
        with self.assertRaises(ValueError):
            P.resolve(secrets, {'model_profile': 'removed'}, {})

    def test_protocol_and_bound_turn(self):
        c = P.Connection('https://example.invalid', 'test', 'model', 'compatible', tools=False)
        body = {'tools': ['tool']}
        P.apply_parameters(body, c, {'reasoning_effort': 'max', 'temperature': .3})
        self.assertNotIn('tools', body)
        self.assertNotIn('thinking', body)
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(body['temperature'], .3)
        with P.bind(c):
            self.assertIs(P.current(), c)
            with P.bind(P.Connection(P.DEFAULT_BASE, 'other')):
                self.assertNotEqual(P.current(), c)
            self.assertIs(P.current(), c)
        self.assertIsNone(P.current())

    def test_exact_ids_and_addresses(self):
        for model in ('DeepSeek 最新模型', 'deepseek::deepseek-flash', 'model name', ''):
            with self.assertRaises(ValueError):
                P.validate_model(model)
        for url in ('api.example.com', 'https://user:pass@example.com', 'https://api.example.com/chat/completions',
                    'https://api.example.com?api_key=secret', 'http://remote.example.com'):
            with self.assertRaises(ValueError):
                P.validate_base(url)
        self.assertEqual(P.validate_base('http://localhost:11434/v1/'), 'http://localhost:11434/v1')
        self.assertIsNone(P.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))

    def test_catalogue_and_short_isolated_probes(self):
        c = P.Connection(P.DEFAULT_BASE, 'test', protocol='deepseek')
        with patch.object(P, 'request_json', return_value={'data': [{'id': 'org/model:1'}, {'id': 'deepseek-flash'}]}):
            self.assertEqual(P.list_models(c), ['deepseek-flash', 'org/model:1'])
        with patch.object(P, 'request_json', return_value={'choices': [{'message': {'content': 'OK'}}]}) as request:
            P.probe(c, vision=True)
            body = request.call_args.args[2]
            self.assertEqual(body['max_tokens'], 64)
            self.assertEqual(len(body['messages']), 1)
            self.assertTrue(body['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        with patch.object(P, 'request_json', return_value={'choices': []}), self.assertRaises(ValueError):
            P.probe(c)

    def test_errors_never_echo_provider_credentials(self):
        error = urllib.error.HTTPError('https://example.invalid', 401, 'private-key', {}, io.BytesIO(b'private-key'))
        with patch('urllib.request.OpenerDirector.open', side_effect=error), self.assertRaisesRegex(ValueError, 'HTTP 401') as raised:
            P.request_json(P.Connection(P.DEFAULT_BASE, 'private-key'), '/models')
        self.assertNotIn('private-key', str(raised.exception))

    def test_real_tool_loop_keeps_connection_snapshot(self):
        import brain as B
        from test_integration import Response
        before = {'deepseek_api_key': 'first', 'deepseek_base_url': P.DEFAULT_BASE}
        after = {'deepseek_api_key': 'second', 'deepseek_base_url': 'https://other.invalid/v1'}
        observed = []
        call = {'tool_calls': [{'index': 0, 'id': 'x', 'function': {'name': 'missing', 'arguments': '{}'}}]}
        def request(*args, **kwargs):
            observed.append((B.api_key(), B.base_url()))
            return Response(call if len(observed) == 1 else {'content': 'done'})
        payload = {'messages': [{'role': 'user', 'content': 'test'}], 'max_tokens': 64}
        with patch.object(B, 'load_secrets', side_effect=[before, after]), \
             patch.object(B.C, 'snapshot_options', return_value={'params': {'model': 'deepseek-flash'}}), \
             patch.object(B, 'build_payload', return_value=(payload, {})), patch.object(B, '_request', side_effect=request):
            self.assertIn(('content', 'done'), list(B.stream('test')))
        self.assertEqual(observed, [('first', P.DEFAULT_BASE)] * 2)
