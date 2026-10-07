"""Run regression tests in a disposable copy containing only public fixtures.

Usage: python tools/test_core.py [--loopback] [test_module ...]
Common Python socket connection paths are blocked in the child process. This is
not an OS-level network sandbox: native clients must be given local fixture
targets explicitly. --loopback permits only literal 127.0.0.1 and ::1 endpoints
for Python sockets, for example: --loopback proxy_loopback.
No runtime data, credentials,
persona or memory from the working installation are copied into the test tree.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loopback", action="store_true",
                        help="allow Python sockets only to 127.0.0.1 or ::1")
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args()
    work = PROJECT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="core-tests-", dir=work) as name:
        root = Path(name)
        for directory in ("src", "tests", "themes", "assets", "tools", "persona_defaults"):
            shutil.copytree(PROJECT / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "data").mkdir()
        for filename in ("config.example.json", "thinking.example.json", "secrets.example.json"):
            shutil.copyfile(PROJECT / "data" / filename, root / "data" / filename)
        shutil.copyfile(PROJECT / "VERSION", root / "VERSION")
        env = dict(os.environ)
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "TAVILY_API_KEY", "AIPET_HOME"):
            env.pop(key, None)
        env.update(QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   AIPET_TEST_WORK=str(root / "work"), AIPET_HOME=str(root),
                   AIPET_TEST_LOOPBACK="1" if args.loopback else "0")
        command = """
import os, socket, sys, unittest
loopback = os.environ.get('AIPET_TEST_LOOPBACK') == '1'
def offline(*args, **kwargs):
    raise RuntimeError('Network access is disabled in regression tests')
def endpoint(address):
    if not loopback:
        offline()
    if not isinstance(address, tuple) or len(address) < 2:
        raise RuntimeError('Only literal loopback endpoints are allowed')
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode('ascii', errors='replace')
    if host not in ('127.0.0.1', '::1'):
        raise RuntimeError('Only literal loopback endpoints are allowed')
def guard_method(original):
    def guarded(self, address, *args, **kwargs):
        endpoint(address)
        return original(self, address, *args, **kwargs)
    guarded._aipet_loopback_guard = loopback
    return guarded
for name in ('connect', 'connect_ex', 'bind'):
    setattr(socket.socket, name, guard_method(getattr(socket.socket, name)))
original_connection = socket.create_connection
def connection(address, *args, **kwargs):
    endpoint(address)
    return original_connection(address, *args, **kwargs)
socket.create_connection = connection
original_resolve = socket.getaddrinfo
def resolve(host, port, *args, **kwargs):
    endpoint((host, port))
    return original_resolve(host, port, *args, **kwargs)
socket.getaddrinfo = resolve
original_sendto = socket.socket.sendto
def sendto(self, data, *args):
    endpoint(args[-1])
    return original_sendto(self, data, *args)
socket.socket.sendto = sendto
sys.path[:0] = ['tests', 'src']
loader = unittest.TestLoader()
suite = (loader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:]
         else loader.discover('tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
"""
        return subprocess.call([sys.executable, "-X", "utf8", "-c", command,
                                *args.modules], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
