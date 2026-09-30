#!/bin/zsh
set -e
task_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_dir"
action="${1:-launch}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export UV_CACHE_DIR="$task_dir/work/uv-cache"
export UV_PYTHON_INSTALL_DIR="$task_dir/.python"
if [[ $(sw_vers -productVersion | cut -d. -f1) -lt 15 ]]; then
    echo "实验性 Mac 支持需要 macOS 15 或以上。请参阅 README.md。"
    read -r "?按回车退出"
    exit 1
fi
if [[ -x .venv/bin/python ]]; then
    .venv/bin/python tools/first_run.py "$action" || { read -r "?启动未完成，按回车退出"; exit 1; }
elif [[ -e .venv ]]; then
    echo ".venv 已存在但不完整，请改名留存后重新启动；不要删除私人资料目录。"
    read -r "?按回车退出"
    exit 1
elif command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    .venv/bin/python tools/first_run.py "$action" || { read -r "?启动未完成，按回车退出"; exit 1; }
else
    python3 tools/first_run.py "$action" || { read -r "?启动未完成，按回车退出"; exit 1; }
fi
