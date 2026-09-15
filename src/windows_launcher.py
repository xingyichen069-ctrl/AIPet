"""Windows GUI entry point with a readable startup log."""
from datetime import datetime
from pathlib import Path
import runpy
import sys
import traceback


def main():
    root = Path(__file__).resolve().parent.parent
    log_path = root / 'data' / 'cache' / 'windows-startup.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        log_path.replace(log_path.with_suffix('.previous.log'))
    with log_path.open('a', encoding='utf-8', buffering=1) as log:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            print('\n[启动]', datetime.now().isoformat(timespec='seconds'))
            sys.argv = [str(root / 'src' / 'pet.py'), '--show-chat']
            runpy.run_path(sys.argv[0], run_name='__main__')
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
    main()
