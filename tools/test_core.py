"""Run regression tests in a disposable copy containing only public fixtures.

Usage: python tools/test_core.py [test_module ...]
The child process cannot open network connections. No runtime data, credentials,
persona or memory from the working installation are copied into the test tree.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    work = PROJECT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="core-tests-", dir=work) as name:
        root = Path(name)
        for directory in ("src", "tests", "themes", "assets", "tools"):
            shutil.copytree(PROJECT / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "data").mkdir()
        for filename in ("config.example.json", "thinking.json"):
            shutil.copyfile(PROJECT / "data" / filename, root / "data" / filename)
        shutil.copyfile(PROJECT / "VERSION", root / "VERSION")
        env = dict(os.environ)
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "AIPET_HOME"):
            env.pop(key, None)
        env.update(QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   AIPET_TEST_WORK=str(root / "work"), AIPET_HOME=str(root))
        command = """
import socket, sys, unittest
def offline(*args, **kwargs):
    raise RuntimeError('Network access is disabled in regression tests')
socket.socket.connect = offline
sys.path[:0] = ['tests', 'src']
loader = unittest.TestLoader()
suite = (loader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:]
         else loader.discover('tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
"""
        return subprocess.call([sys.executable, "-X", "utf8", "-c", command,
                                *sys.argv[1:]], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
