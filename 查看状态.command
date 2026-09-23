#!/bin/zsh
set -u

task_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_dir"

if [[ -x .venv/bin/python ]]; then
    py=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    py="$(command -v python3)"
else
    echo "找不到 Python 3。"
    read -r "?按回车关闭窗口…"
    exit 1
fi

"$py" src/app_entry.py status --log 20
rc=$?
echo
read -r "?按回车关闭窗口…"
exit $rc
