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


def _migrate(args: list[str]) -> int:
    import migrate

    old = sys.argv
    sys.argv = [str(ROOT / "src" / "migrate.py"), *args]
    try:
        migrate.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old
    return 0


def dispatch(args: list[str] | None = None) -> int:
    args = list(args if args is not None else sys.argv[1:])
    command = args[0].lower() if args else "desktop"
    if command in {"desktop", "gui", "start", "chat"}:
        _ensure_startable()
        # Import Qt only after maintenance commands have been separated.
        show_chat = command in {"desktop", "gui", "start", "chat"} or "--show-chat" in args[1:]
        sys.argv = [str(ROOT / "src" / "pet.py"), *( ["--show-chat"] if show_chat else [] ),
                    *[arg for arg in args[1:] if arg != "--show-chat"]]
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
        if action == "restart":
            return maintenance.qq_restart()
        if action == "status":
            return maintenance.qq_status()
        print("用法：AIPet.exe qq start|stop|restart|status")
        return 2
    if command == "status":
        log_lines = 10 if len(args) == 1 else 0
        if "--log" in args[1:]:
            index = args.index("--log")
            try:
                log_lines = max(1, int(args[index + 1]))
            except (IndexError, ValueError):
                log_lines = 10
        if "--watch" in args[1:]:
            interval = 5.0
            index = args.index("--watch")
            try:
                interval = float(args[index + 1])
            except (IndexError, ValueError):
                pass
            return maintenance.watch_status(interval, log_lines)
        return maintenance.status(log_lines)
    if command in {"stop", "stop-desktop"}:
        return maintenance.desktop_stop()
    if command == "memory":
        if len(args) < 2 or args[1].lower() == "show":
            return maintenance.memory_show()
        print("用法：AIPet.exe memory show")
        return 2
    if command == "prepare":
        return maintenance.prepare()
    if command == "migrate":
        return _migrate(args[1:])
    if command in {"diagnose", "diagnosis"}:
        return maintenance.diagnose()
    if command == "update":
        return _update(args)
    print("用法：AIPet.exe [desktop|chat|qq start|qq stop|status|stop|memory show|prepare|migrate|diagnose|update]")
    return 2


def main() -> int:
    return dispatch()


if __name__ == "__main__":
    raise SystemExit(main())
