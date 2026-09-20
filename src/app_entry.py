"""The stable command boundary behind ``AIPet.exe``."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

from app_paths import PATHS

ROOT = PATHS.install


def _ensure_startable() -> None:
    import update_lifecycle
    update_lifecycle.ensure_available(ROOT)


def _update(args: list[str]) -> int:
    import update

    old = sys.argv
    sys.argv = [str(ROOT / "src" / "update.py"), *args]
    try:
        update.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old
    return 0


def dispatch(args: list[str] | None = None) -> int:
    args = list(args if args is not None else sys.argv[1:])
    command = args[0].lower() if args else "desktop"
    if command in {"desktop", "gui", "start"}:
        _ensure_startable()
        # Import Qt only after maintenance commands have been separated.
        sys.argv = [str(ROOT / "src" / "pet.py"), "--show-chat", *args[1:]]
        runpy.run_path(str(ROOT / "src" / "pet.py"), run_name="__main__")
        return 0

    import maintenance

    if command == "qq":
        action = args[1].lower() if len(args) > 1 else "status"
        if action == "start":
            _ensure_startable()
            return maintenance.qq_start()
        if action == "stop":
            return maintenance.qq_stop()
        if action == "status":
            return maintenance.qq_status()
        print("用法：AIPet.exe qq start|stop|status")
        return 2
    if command == "status":
        return maintenance.status()
    if command in {"diagnose", "diagnosis"}:
        return maintenance.diagnose()
    if command == "update":
        return _update(args)
    print("用法：AIPet.exe [desktop|qq start|qq stop|status|diagnose|update]")
    return 2


def main() -> int:
    return dispatch()


if __name__ == "__main__":
    raise SystemExit(main())
