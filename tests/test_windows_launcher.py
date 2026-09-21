import sys
import unittest
from unittest.mock import patch

import test_companion as TC
import windows_launcher as W


class WindowsLauncher(unittest.TestCase):
    setUp = TC.Services.setUp
    tearDown = TC.Services.tearDown

    def test_gui_launch_logs_failures_and_restores_streams(self):
        original = sys.stdout, sys.stderr
        with patch.object(W, '__file__', str(self.root / 'src' / 'windows_launcher.py')), \
             patch.object(W.sys, 'argv', ['windows_launcher.py']), \
             patch.object(W.sys, 'platform', 'test'), \
             patch('app_entry.main', side_effect=RuntimeError('startup-test-error')):
            with self.assertRaisesRegex(RuntimeError, 'startup-test-error'):
                W.main()
        self.assertEqual((sys.stdout, sys.stderr), original)
        log = (self.root / 'data/cache/windows-startup.log').read_text(encoding='utf-8')
        self.assertIn('startup-test-error', log)


if __name__ == '__main__':
    unittest.main()
