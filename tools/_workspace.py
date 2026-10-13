"""Paths for disposable output; looking up a path never creates a directory."""
from pathlib import Path


def scratch(root, *parts):
    return Path(root) / "work" / "scratch_临时输出" / Path(*parts)
