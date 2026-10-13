"""Management adapters to the existing first-run environment implementation."""
from pathlib import Path
from first_run import command_env, environment_python as project_python


def require_python(root: Path) -> Path:
    interpreter = project_python(root)
    if not interpreter.is_file():
        raise RuntimeError('项目运行环境尚未就绪，请先运行准备环境.bat 或 python tools/manage.py prepare。')
    return interpreter
