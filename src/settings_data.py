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
from copy import deepcopy
from datetime import datetime
import uuid

from atomic_store import state_lock


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
    if not Path(path).exists():
        return deepcopy(default or {})
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError(f"{Path(path).name} 无法读取，请先修复文件；原内容没有覆盖。") from None
    if not isinstance(value, dict):
        raise ValueError(f"{Path(path).name} 必须是 JSON 对象。")
    return value


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


_MISSING = object()


def save_texts(root, changes):
    """Validate every old value first, then back up and replace as one edit.

    changes maps a local path to (original text or None, replacement text).
    A failed replacement restores the previous bytes, including line endings.
    """
    root = Path(root).resolve()
    changes = {Path(p): (old, new) for p, (old, new) in changes.items() if old != new}
    if not changes:
        return
    with state_lock(root):
        pending = []
        for path, (old, new) in changes.items():
            relative = path.resolve().relative_to(root)
            raw = path.read_bytes() if path.exists() else None
            actual = path.read_text(encoding="utf-8") if raw is not None else None
            if actual != old:
                raise ValueError(f"{relative.name} 已在其他地方改变，请重新打开后再编辑。")
            pending.append((path, relative, raw, new))
        backup = root / "data/settings_backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        for _, relative, raw, _ in pending:
            if raw is not None:
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        replaced = []
        try:
            for path, _, raw, new in pending:
                _write_atomic(path, new)
                replaced.append((path, raw))
        except OSError:
            for path, raw in reversed(replaced):
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_atomic(path, raw.decode("utf-8"))
            raise


def _changes(before, after, prefix=()):
    for key in before.keys() | after.keys():
        old, new = before.get(key, _MISSING), after.get(key, _MISSING)
        if isinstance(old, dict) and isinstance(new, dict):
            yield from _changes(old, new, prefix + (key,))
        elif old != new:
            yield prefix + (key,), old, new


def _at(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _set(value, path, item):
    for key in path[:-1]:
        value = value.setdefault(key, {})
    if item is _MISSING:
        value.pop(path[-1], None)
    else:
        value[path[-1]] = deepcopy(item)


class SettingsSession:
    """Only changed fields are saved; concurrent untouched data survives.

    Opening/closing a settings window does not write any private file. Validate
    everything before entering this transaction. Failed writes restore all
    files already replaced; private recovery copies remain outside Git.
    """

    FILES = {"config": "data/config.json", "secrets": "data/secrets.json",
             "thinking": "data/thinking.json", "appearance": "data/appearance.json"}

    def __init__(self, root):
        self.root = Path(root)
        self.reload()

    def reload(self):
        self.original = {}
        for key, relative in self.FILES.items():
            path = self.root / relative
            fallback = {}
            if key in ("config", "thinking") and not path.exists():
                fallback = _load_json(path.with_name(key + ".example.json"))
            self.original[key] = _load_json(path, fallback)
        self.values = deepcopy(self.original)

    def dirty(self):
        return self.values != self.original

    def save(self):
        if not self.dirty():
            return []
        pending = []
        with state_lock(self.root):
            for key, relative in self.FILES.items():
                edits = list(_changes(self.original[key], self.values[key]))
                if not edits:
                    continue
                path = self.root / relative
                old_bytes = path.read_bytes() if path.exists() else None
                latest = _load_json(path, self.original[key])
                for trail, old, new in edits:
                    actual = _at(latest, trail)
                    if actual != old and actual != new:
                        raise ValueError(f"{path.name} 的 {'.'.join(trail)} 已在其他地方改变。请重新读取后再修改。")
                    _set(latest, trail, new)
                encoded = (json.dumps(latest, ensure_ascii=False, indent=2) + "\n").encode()
                pending.append((path, old_bytes, encoded))
            if not pending:
                return []
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            backup = self.root / "data/settings_backups" / stamp
            backup.mkdir(parents=True, mode=0o700)
            for path, raw, _ in pending:
                if raw is not None:
                    dest = backup / path.name
                    dest.write_bytes(raw)
                    os.chmod(dest, 0o600)
            replaced = []
            try:
                for path, raw, new in pending:
                    _write_atomic(path, new.decode())
                    replaced.append((path, raw))
                    if path.name == "secrets.json":
                        os.chmod(path, 0o600)
            except OSError:
                for path, raw in reversed(replaced):
                    if raw is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_atomic(path, raw.decode("utf-8"))
                raise
        changed = [p.name for p, _, _ in pending]
        self.reload()
        return changed
