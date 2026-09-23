"""Windows GUI entry point with a readable startup log."""
from datetime import datetime
import os
from pathlib import Path
import sys
import traceback


_DLL_HANDLES = []


def _prepare_frozen_dlls():
    """Make PySide6 and shiboken6 sibling DLLs visible to the loader.

    The wheels keep these DLLs in separate package directories.  Python adds
    both directories while running from a virtualenv, but a PyInstaller build
    has one ``_internal`` directory and otherwise misses ``shiboken6`` when
    importing ``PySide6.QtCore``.
    """
    if not getattr(sys, 'frozen', False):
        return
    bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    directories = [bundle, bundle / 'PySide6', bundle / 'shiboken6']
    existing = [str(path) for path in directories if path.is_dir()]
    if not existing:
        return
    os.environ['PATH'] = os.pathsep.join(existing + [os.environ.get('PATH', '')])
    if hasattr(os, 'add_dll_directory'):
        for path in existing:
            try:
                _DLL_HANDLES.append(os.add_dll_directory(path))
            except OSError:
                pass


def main():
    root = Path(__file__).resolve().parent.parent
    log_path = root / 'data' / 'cache' / 'windows-startup.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Command modes need a real console so ``AIPet.exe status`` and
    # ``AIPet.exe diagnose`` remain useful.  The GUI mode alone is windowed
    # and therefore gets the persistent startup log.
    if len(sys.argv) > 1:
        from app_entry import main as app_main
        try:
            return app_main()
        except Exception:
            with log_path.open('a', encoding='utf-8', buffering=1) as log:
                traceback.print_exc(file=log)
            raise
    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        log_path.replace(log_path.with_suffix('.previous.log'))
    with log_path.open('a', encoding='utf-8', buffering=1) as log:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            print('\n[启动]', datetime.now().isoformat(timespec='seconds'))
            if getattr(sys, 'frozen', False):
                # PyInstaller stores Python modules in its embedded archive.
                # Importing the real entry point keeps the frozen build from
                # depending on a loose src/pet.py file at runtime.
                _prepare_frozen_dlls()
                from app_entry import main as app_main
                return_code = app_main()
                if return_code:
                    raise SystemExit(return_code)
            else:
                from app_entry import main as app_main
                return_code = app_main()
                if return_code:
                    raise SystemExit(return_code)
        except SystemExit:
            raise
        except Exception:
            traceback.print_exc()
            log.flush()
            if sys.platform == 'win32':
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f'桌宠启动失败。请把以下日志发来排查：\n{log_path}', '小日和', 0x10)
            raise
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


if __name__ == '__main__':
    raise SystemExit(main())
