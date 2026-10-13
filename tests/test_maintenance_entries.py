"""Management and experiment entries stay read-only unless explicitly requested."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
import manage
import _environment
import process_state


def entry(name):
    spec = importlib.util.spec_from_file_location('maintenance_' + name, PROJECT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MaintenanceEntries(unittest.TestCase):
    def test_tone_import_and_preview_never_run_live_code(self):
        with patch('subprocess.run', side_effect=AssertionError('no process')):
            module = entry('tone_test')
            with patch.object(module, 'prepare_fixture', side_effect=AssertionError('no fixture')):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main([]), 0)
                    self.assertEqual(module.main(['fixture-mood']), 0)

    def test_tone_private_runner_refuses_original_installation(self):
        module = entry('tone_test')
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)):
            with self.assertRaises(ValueError):
                module.run_fixture()

    def test_tone_missing_key_cannot_change_even_fixture_mood(self):
        module = entry('tone_test')
        mood = SimpleNamespace(set_mood=Mock(), clear=Mock())
        brain = SimpleNamespace(api_key=lambda: '')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.tone-demo-fixture').touch()
            with patch.object(module, 'ROOT', root), patch.object(module, 'dialogue_connection', return_value=SimpleNamespace(key='')), patch.dict(sys.modules, {'brain': brain, 'mood': mood}), redirect_stdout(io.StringIO()):
                self.assertEqual(module.run_fixture('fixture'), 2)
        mood.set_mood.assert_not_called()
        mood.clear.assert_not_called()

    def test_tone_fixture_has_no_history_and_fixes_external_runtime_paths(self):
        module = entry('tone_test')
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source', Path(folder) / 'copy'
            for directory in ('data', 'memory', 'tools', 'src', 'persona'):
                (source / directory).mkdir(parents=True)
            (source / 'data/config.json').write_text(json.dumps({'paths': {'journal': 'outside'}, 'tools': {'fs_root': 'outside'}}))
            (source / 'data/secrets.json').write_text('{"key":"fixture"}')
            (source / 'data/companion.sqlite3').write_bytes(b'private-history')
            (source / 'memory/journal.jsonl').write_bytes(b'private-journal')
            (source / 'data/mood.json').write_bytes(b'current-mood')
            (source / 'persona/SOUL.md').write_bytes(b'fixture-persona')
            shutil.copyfile(PROJECT / 'tools/_workspace.py', source / 'tools/_workspace.py')
            target.mkdir()
            script = module.prepare_fixture(source, target)
            self.assertTrue(script.is_file())
            self.assertFalse((target / 'memory').exists())
            self.assertFalse((target / 'data/mood.json').exists())
            self.assertFalse((target / 'data/companion.sqlite3').exists())
            values = json.loads((target / 'data/config.json').read_text())
            self.assertEqual(values['tools']['fs_root'], str(target / 'sandbox'))
            self.assertEqual((source / 'data/mood.json').read_bytes(), b'current-mood')

    def test_tone_requests_explicitly_disable_tools(self):
        module = entry('tone_test')
        brain = SimpleNamespace(api_key=lambda: 'fixture', persona_text=lambda: 'fixture-persona',
                                ask_with_system=Mock(return_value=('answer', '', {})))
        mood = SimpleNamespace(block=lambda: '', set_mood=Mock(), clear=Mock())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.tone-demo-fixture').touch()
            with patch.object(module, 'ROOT', root), patch.object(module, 'dialogue_connection', return_value=SimpleNamespace(key='fixture')), patch.dict(sys.modules, {'brain': brain, 'mood': mood}), redirect_stdout(io.StringIO()):
                self.assertEqual(module.run_fixture(), 0)
            self.assertTrue((root / 'tone-results.json').is_file())
        for call in brain.ask_with_system.call_args_list:
            self.assertEqual(call.kwargs['allowed_tools'], set())
            self.assertEqual(call.kwargs['history'], [])
        mood.clear.assert_not_called()

    def test_old_typing_entry_uses_current_isolated_ui_suite(self):
        module = entry('test_typing')
        with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=3)) as run:
            self.assertEqual(module.main([]), 3)
        self.assertEqual(run.call_args.args[0][-1], 'test_desktop')
        self.assertIn('test_core.py', run.call_args.args[0][-2])

    def test_rewrite_probe_supports_compatible_provider_and_binds_request(self):
        module = entry('probe_reimu_rewrite')
        connection = module.B.PC.from_profile({'base_url': 'https://fixture.example/v1',
                                               'api_key': 'fixture', 'model': 'fixture-model',
                                               'protocol': 'compatible'}, environ={})
        with module.B.PC.bind(connection):
            body = module.payload('fixture question')
        self.assertNotIn('thinking', body)
        self.assertNotIn('tools', body)
        self.assertEqual(body['model'], 'fixture-model')
        def response(payload, key, timeout):
            self.assertEqual(module.B.PC.current(), connection)
            self.assertEqual(key, connection.key)
            return io.StringIO(json.dumps({'choices': [{'message': {'content': 'fixture answer'}}]}))
        with patch.object(module.B, '_request', side_effect=response):
            answer, _ = module.generate(body, connection)
        self.assertEqual(answer, 'fixture answer')

    def test_tone_process_failure_removes_temporary_private_copy(self):
        module = entry('tone_test')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def prepare(source, target):
                (target / 'private-key.json').write_text('{"key":"fixture"}')
                return target / 'fake-entry.py'
            with patch.object(module, 'ROOT', root), patch.object(module, 'prepare_fixture', side_effect=prepare), patch.object(module.subprocess, 'run', side_effect=OSError('fixture launch failure')):
                with self.assertRaises(OSError):
                    module.main(['--live'])
            self.assertFalse(list(root.rglob('private-key.json')))

    def test_management_launch_keeps_existing_initialization_and_validation(self):
        with patch.object(manage.environment, 'require_python', side_effect=AssertionError('first-run owns setup')):
            with patch.object(manage.subprocess, 'run', return_value=SimpleNamespace(returncode=2)) as run:
                self.assertEqual(manage.run_command('start', [], root=PROJECT), 2)
        self.assertEqual(Path(run.call_args.args[0][-2]), PROJECT / 'tools/first_run.py')
        self.assertEqual(run.call_args.args[0][-1], 'launch')

    def test_management_defaults_and_output_paths(self):
        self.assertEqual(manage.COMMANDS['backup'].defaults, ('list',))
        self.assertEqual(manage.COMMANDS['knowledge'].defaults, ('stats',))
        self.assertEqual(manage.COMMANDS['consolidate'].defaults, ('status',))
        self.assertIn('--live', manage.COMMANDS['tone-demo'].usage)
        values = _environment.command_env(PROJECT)
        for name in ('PIP_CACHE_DIR', 'UV_CACHE_DIR'):
            self.assertTrue(Path(values[name]).is_relative_to(PROJECT / 'work/scratch_临时输出'))

    def test_windows_process_check_matches_pid_column_not_substring(self):
        with patch.object(process_state.os, 'name', 'nt'), patch.object(process_state.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='"contains123.exe","9123","Console","1","1 K"')
            self.assertFalse(process_state.alive(123))
            run.return_value.stdout = '"pythonw.exe","123","Console","1","1 K"'
            self.assertTrue(process_state.alive(123))


if __name__ == '__main__':
    unittest.main()
