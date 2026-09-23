"""Small, atomic persistence helpers for the in-app settings centre.

The settings window deliberately talks to this module instead of reaching into
``memory`` or ``brain`` globals.  That keeps the editor usable before the
conversation stack is imported and makes it safe to point at a disposable
installation in tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


PERSONA_FILES = (
    ("SOUL.md", "人格底色", "她是谁、怎么说话，以及她会主动做什么。"),
    ("BOUNDARIES.md", "行为边界", "隐私、拒绝、情绪和安全边界，优先级高于人格底色。"),
    ("PROFILE.md", "用户档案", "关于你的当前事实；程序会合并维护，你也可以手动修正。"),
)


def _write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            pass


def _load_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default or {})
    return value if isinstance(value, dict) else dict(default or {})


def persona_path(root: Path, name: str) -> Path:
    allowed = {item[0] for item in PERSONA_FILES}
    if name not in allowed:
        raise ValueError(f"未知人格文件：{name}")
    return Path(root) / "persona" / name


def read_persona(root: Path, name: str) -> str:
    try:
        return persona_path(root, name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_persona(root: Path, name: str, text: str) -> Path:
    if not isinstance(text, str):
        raise TypeError("人格内容必须是文本")
    path = persona_path(root, name)
    _write_atomic(path, text)
    return path


def secrets_path(root: Path) -> Path:
    return Path(root) / "data" / "secrets.json"


def load_secrets(root: Path) -> dict:
    return _load_json(secrets_path(root))


def save_secrets(root: Path, values: dict) -> Path:
    if not isinstance(values, dict):
        raise TypeError("密钥配置必须是对象")
    path = secrets_path(root)
    _write_atomic(path, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def thinking_path(root: Path) -> Path:
    return Path(root) / "data" / "thinking.json"


def load_thinking(root: Path) -> dict:
    return _load_json(thinking_path(root))


def save_thinking(root: Path, values: dict) -> Path:
    if not isinstance(values, dict):
        raise TypeError("思考配置必须是对象")
    path = thinking_path(root)
    _write_atomic(path, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
    return path


def config_path(root: Path) -> Path:
    return Path(root) / "data" / "config.json"


def load_config(root: Path) -> dict:
    path = config_path(root)
    if path.exists():
        return _load_json(path)
    example = path.with_name("config.example.json")
    return _load_json(example)


def save_config(root: Path, values: dict) -> Path:
    if not isinstance(values, dict):
        raise TypeError("应用配置必须是对象")
    path = config_path(root)
    _write_atomic(path, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
    return path
