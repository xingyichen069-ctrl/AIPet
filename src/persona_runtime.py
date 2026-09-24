"""Read the selected persona without creating or overwriting private files."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import re

_selection = ContextVar('persona_selection', default=None)


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[\w-]+', value))


def active_id(root):
    selected = _selection.get()
    if selected and selected[0] == str(Path(root).resolve()):
        return selected[1]
    data = read_json(Path(root) / 'persona/active.json', {})
    pid = data.get('id', 'hiyori') if isinstance(data, dict) else 'hiyori'
    return pid if valid_id(pid) else 'hiyori'


@contextmanager
def bind(root, pid=None):
    pid = pid if valid_id(pid) else active_id(root)
    profile = mood_profile(root, pid)
    token = _selection.set((str(Path(root).resolve()), pid, profile))
    try:
        yield pid
    finally:
        _selection.reset(token)


def files(root, pid=None):
    root = Path(root)
    pid = pid if valid_id(pid) else active_id(root)
    local = root / 'persona/characters' / pid
    default = root / 'persona_defaults' / pid
    result = {}
    for name in ('SOUL.md', 'BOUNDARIES.md', 'DIALOGUE.json', 'MOODS.json'):
        candidates = [local / name]
        if pid == 'hiyori':
            candidates.append(root / 'persona' / name)
        # Boundaries remain common unless the character has its own file.
        if name == 'BOUNDARIES.md':
            candidates.extend([root / 'persona/BOUNDARIES.md', root / 'persona/BOUNDARY.md'])
        candidates.append(default / name)
        found = next((p for p in candidates if p.is_file()), None)
        if found:
            result[name] = found
    return result


def examples(root):
    path = files(root).get('DIALOGUE.json')
    data = read_json(path, []) if path else []
    # Each entry is a complete exchange. Never accept system/tool messages.
    result = []
    if not isinstance(data, list):
        return result
    for exchange in data[:24]:
        if not isinstance(exchange, list) or len(exchange) % 2:
            continue
        if not all(isinstance(m, dict) and m.get('role') == ('user' if i % 2 == 0 else 'assistant')
                   and isinstance(m.get('content'), str) and 0 < len(m['content']) <= 1500
                   for i, m in enumerate(exchange)):
            continue
        result.extend({'role': m['role'], 'content': m['content']} for m in exchange)
    return result


def mood_profile(root, pid=None):
    pid = pid if valid_id(pid) else active_id(root)
    selected = _selection.get()
    if selected and selected[:2] == (str(Path(root).resolve()), pid):
        return selected[2]
    bindings = read_json(Path(root) / 'data/persona_moods.json', {})
    value = bindings.get(pid, pid) if isinstance(bindings, dict) else pid
    return value if valid_id(value) else pid


def mood_catalog(root, legacy):
    profile = mood_profile(root)
    candidates = [Path(root) / 'persona/characters' / profile / 'MOODS.json',
                  Path(root) / 'persona_defaults' / profile / 'MOODS.json']
    if profile == 'hiyori':
        candidates.insert(1, Path(root) / 'data/mood_catalog.json')
    for path in candidates:
        data = read_json(path, None)
        if isinstance(data, dict):
            clean = {k: v for k, v in data.items() if isinstance(k, str)
                     and isinstance(v, dict) and isinstance(v.get('voice'), str)
                     and isinstance(v.get('hours', 2), (int, float))
                     and 0 < v.get('hours', 2) <= 24
                     and all(isinstance(v.get(f, ''), str) for f in ('feel', 'avoid'))
                     and isinstance(v.get('sample', []), list)
                     and all(isinstance(x, str) for x in v.get('sample', []))}
            if clean:
                return clean
    if profile == 'hiyori':
        return legacy
    return {k: {'hours': v.get('hours', 2), 'feel': k,
                'voice': '按你自己的性格表现这份心情，仍把对方的话听完。'}
            for k, v in legacy.items()}
