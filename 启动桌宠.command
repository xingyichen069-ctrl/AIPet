#!/bin/zsh
set -e
task_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_dir"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import PySide6, live2d.v3, ddgs' >/dev/null 2>&1; then
    exec .venv/bin/python src/pet.py
fi
if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
else
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python src/pet.py
