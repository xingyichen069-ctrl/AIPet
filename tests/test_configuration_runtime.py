"""Configuration entry points share state without touching the live installation."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
import brain as B
import memory as M
import thinking as T
import tools as TOOLS
import knowledge as K
import configuration_runtime as R
import settings_data as D
import atomic_store as STORE

APP = QApplication.instance() or QApplication([])
PROJECT = Path(__file__).resolve().parents[1]


class ConfigurationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        for name in ('config', 'thinking'):
            shutil.copyfile(PROJECT / 'data' / (name + '.example.json'), self.root / 'data' / (name + '.json'))
        self.config = D.load_config(self.root)
        self.stack = ExitStack()
        for module, name, value in ((M, 'ROOT', self.root), (M, 'CFG', self.config),
                                    (M, 'P', self.config['paths']), (T, 'THINKING_FILE', self.root / 'data/thinking.json'),
                                    (T, '_cache', {'mtime': 0, 'data': None}),
                                    (B, 'SECRETS', self.root / 'data/secrets.json'),
                                    (TOOLS, 'TOOLS_CFG', self.config.get('tools', {})),
                                    (K, 'KCFG', self.config.get('knowledge', {})), (K, 'DOCS_DIR', self.root / 'knowledge')):
            self.stack.enter_context(patch.object(module, name, value))
        self.watcher = R.ConfigurationWatcher(self.root)

    def tearDown(self):
        self.watcher.timer.stop()
        self.watcher.deleteLater()
        APP.processEvents()
        self.stack.close()
        self.temp.cleanup()

    def test_other_process_save_refreshes_running_desktop(self):
        code = ('import json,os,sys;from pathlib import Path;'
                'p=Path(sys.argv[1]);d=json.loads(p.read_text(encoding="utf-8"));'
                'd["tools"]["proxy"]="off";d["knowledge"]["docs_dir"]="new-knowledge";'
                'q=p.with_suffix(".new");q.write_text(json.dumps(d),encoding="utf-8");os.replace(q,p)')
        subprocess.run([sys.executable, '-B', '-c', code, str(self.root / 'data/config.json')], check=True)
        for _ in range(30):
            QTest.qWait(50)
            if TOOLS.TOOLS_CFG.get('proxy') == 'off':
                break
        self.assertEqual(TOOLS.TOOLS_CFG['proxy'], 'off')
        self.assertEqual(K.DOCS_DIR, self.root / 'new-knowledge')
        self.assertIs(TOOLS.TOOLS_CFG, M.CFG['tools'])

    def test_invalid_then_repaired_external_file_retains_last_good_values(self):
        path = self.root / 'data/config.json'
        path.write_text('{unfinished', encoding='utf-8')
        self.watcher.check()
        self.assertIs(M.CFG, self.config)
        self.assertTrue(self.watcher.error)
        updated = deepcopy(self.config)
        updated['tools']['proxy'] = 'off'
        path.write_text(json.dumps(updated), encoding='utf-8')
        self.watcher.check()
        self.assertEqual(M.CFG['tools']['proxy'], 'off')
        self.assertEqual(self.watcher.error, '')

    def test_temporarily_missing_file_is_not_recreated_by_watcher(self):
        path = self.root / 'data/config.json'
        path.unlink()
        self.watcher.check()
        self.assertFalse(path.exists())
        self.assertIs(M.CFG, self.config)

    def test_invalid_section_cannot_partially_replace_runtime_globals(self):
        for section, value in (('tools', []), ('knowledge', {'docs_dir': None})):
            candidate = deepcopy(self.config)
            candidate[section] = value
            D.save_config(self.root, candidate)
            self.watcher.check()
            self.assertIs(M.CFG, self.config)
            self.assertTrue(self.watcher.error)

    def test_invalid_thinking_keeps_cached_configuration_and_recovers(self):
        before = deepcopy(T.load(force=True))
        T.THINKING_FILE.write_text('[]', encoding='utf-8')
        self.watcher.check()
        self.assertTrue(self.watcher.error)
        self.assertEqual(T._cache['data'], before)
        self.assertEqual(T.current_level(), before['current'])
        self.assertEqual(T.resolve(level='daily')['params'], before['presets']['daily']['params'])
        with self.assertRaises(ValueError):
            T.set_level('daily')
        self.assertEqual(T.THINKING_FILE.read_text(encoding='utf-8'), '[]')
        D.save_thinking(self.root, before)
        self.watcher.check()
        self.assertFalse(self.watcher.error)

    def test_missing_thinking_keeps_last_working_profile_without_recreating_file(self):
        before = deepcopy(T.load(force=True))
        T.THINKING_FILE.unlink()
        self.assertEqual(T.current_level(), before['current'])
        with self.assertRaises(OSError):
            T.set_level('daily')
        self.assertFalse(T.THINKING_FILE.exists())
        D.save_thinking(self.root, before)
        self.watcher.check()
        self.assertEqual(T.load(), before)

    def test_incomplete_paths_do_not_replace_the_working_memory_locations(self):
        for key in ('journal', 'state', 'profile', 'summaries', 'archive', 'view'):
            candidate = deepcopy(self.config)
            candidate['paths'].pop(key)
            D.save_config(self.root, candidate)
            self.watcher.check()
            self.assertIs(M.CFG, self.config)
            self.assertTrue(self.watcher.error)

    def test_old_key_command_updates_default_profile_preserving_other_fields(self):
        original = {'model_profiles': {'main': {'api_key': 'old', 'model': 'fixture-model'},
                                       'other': {'api_key': 'keep'}},
                    'default_model_profile': 'main', 'qq_secret': 'private-fixture',
                    'deepseek_api_key': 'legacy'}
        D.save_secrets(self.root, original)
        B.save_key('new-fixture')
        actual = D.load_secrets(self.root)
        self.assertEqual(actual['model_profiles']['main']['api_key'], 'new-fixture')
        self.assertEqual(actual['model_profiles']['other'], original['model_profiles']['other'])
        self.assertEqual(actual['qq_secret'], 'private-fixture')
        self.assertEqual(actual['deepseek_api_key'], 'legacy')
        self.assertTrue(list((self.root / 'data/settings_backups').glob('*/secrets.json')))

    def test_old_key_command_keeps_legacy_and_corrupt_file_compatible(self):
        D.save_secrets(self.root, {'deepseek_api_key': 'old', 'qq_secret': 'keep'})
        B.save_key('new-fixture')
        self.assertEqual(D.load_secrets(self.root), {'deepseek_api_key': 'new-fixture', 'qq_secret': 'keep'})
        B.SECRETS.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            B.save_key('another')
        self.assertEqual(B.SECRETS.read_text(encoding='utf-8'), '{broken')

    def test_switch_level_keeps_external_preset_changes_even_with_old_timestamp(self):
        T.load(force=True)
        stat = T.THINKING_FILE.stat()
        config = D.load_thinking(self.root)
        config['presets']['daily']['params']['temperature'] = 0.123
        D.save_thinking(self.root, config)
        os.utime(T.THINKING_FILE, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        T.set_level('daily')
        self.assertEqual(D.load_thinking(self.root)['presets']['daily']['params']['temperature'], 0.123)

    def test_rapid_repeated_saves_with_native_watcher_keep_latest_value(self):
        for value in range(25):
            candidate = deepcopy(self.config)
            candidate['tools']['audit_fixture'] = value
            D.save_config(self.root, candidate)
            self.watcher.check()
        self.assertEqual(M.CFG['tools']['audit_fixture'], 24)

    def test_transient_windows_sharing_error_retries_without_partial_contents(self):
        error = PermissionError('fixture sharing violation')
        error.winerror = 32
        path = self.root / 'data/retry.json'
        path.write_bytes(b'old')
        replace = STORE.os.replace
        attempts = []
        def sharing_then_ready(source, destination):
            attempts.append(source)
            if len(attempts) < 3:
                self.assertEqual(path.read_bytes(), b'old')
                raise error
            replace(source, destination)
        with patch.object(STORE.os, 'replace', side_effect=sharing_then_ready), patch.object(STORE.time, 'sleep'):
            STORE.write_bytes(path, b'new')
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(len(attempts), 3)

    def test_persistent_windows_sharing_error_is_bounded_and_preserves_original(self):
        error = PermissionError('fixture sharing violation')
        error.winerror = 32
        path = self.root / 'data/retry.json'
        path.write_bytes(b'old')
        with patch.object(STORE.os, 'replace', side_effect=error) as replace, patch.object(STORE.time, 'sleep'):
            with self.assertRaises(PermissionError):
                STORE.write_bytes(path, b'new')
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(path.read_bytes(), b'old')
        self.assertFalse(list(path.parent.glob('retry.json.*.tmp')))

    @unittest.skipUnless(os.name == 'nt', 'native Windows read-only file attribute')
    def test_read_only_destination_stays_untouched_and_temporary_file_is_removed(self):
        path = self.root / 'data/readonly.json'
        path.write_bytes(b'old')
        os.chmod(path, 0o444)
        try:
            with patch.object(STORE.time, 'sleep'), self.assertRaises(PermissionError):
                STORE.write_bytes(path, b'new')
            self.assertEqual(path.read_bytes(), b'old')
            self.assertFalse(path.stat().st_mode & 0o200)
            self.assertFalse(list(path.parent.glob('readonly.json.*.tmp')))
        finally:
            os.chmod(path, 0o600)


if __name__ == '__main__':
    unittest.main()
