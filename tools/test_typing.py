"""Legacy typing-demo entry: check the current chat UI with offline fixtures."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("当前聊天界面离线检查：python tools/manage.py chat-check")
        return 0 if args in (["--help"], ["-h"]) else 2
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "tools/test_core.py"), "test_desktop"],
        cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
