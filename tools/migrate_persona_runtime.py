"""Preview/apply non-hardware configuration updates to an existing installation.

Does not install code. Back up each changed file before applying. Custom persona
prose and credentials are never replaced by public templates.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil

PROJECT = Path(__file__).resolve().parents[1]


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def split_reimu(text):
    """Recognize only the documented original-example format, otherwise leave it."""
    match = re.search(r'^## 几段声音\s*\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    exchanges = []
    if match:
        for block in re.split(r'\n\s*\n', match.group(1)):
            lines = block.strip().splitlines()
            if not lines or not lines[0].startswith('对方：'):
                continue
            messages = []
            for i, line in enumerate(lines):
                prefix = '对方：' if i % 2 == 0 else '你：'
                if not line.startswith(prefix):
                    return text, None
                messages.append({'role': 'user' if i % 2 == 0 else 'assistant', 'content': line[len(prefix):]})
            if len(messages) % 2:
                return text, None
            exchanges.append(messages)
        if exchanges:
            text = text[:match.start()] + text[match.end():]
    default = (PROJECT / 'persona_defaults/reimu/SOUL.md').read_text(encoding='utf-8')
    style = re.search(r'^## 话到这里就够了\n.*?(?=^## |\Z)', default, re.M | re.S)
    text = re.sub(r'^## 话到这里就够了\n.*?(?=^## |\Z)', lambda _: style.group(), text, flags=re.M | re.S)
    return text.rstrip() + '\n', exchanges or None


def plan(root):
    root = Path(root)
    changes = {}
    thinking = root / 'data/thinking.json'
    if thinking.exists():
        cfg = json.loads(thinking.read_text(encoding='utf-8'))
        defaults = json.loads((PROJECT / 'data/thinking.json').read_text(encoding='utf-8'))
        for level, keys in [('daily', ('reasoning_effort','temperature','frequency_penalty','verbosity','tools')),
                            ('serious', ('verbosity',))]:
            params = cfg.setdefault('presets', {}).setdefault(level, {}).setdefault('params', {})
            for key in keys:
                params[key] = defaults['presets'][level]['params'][key]
        changes[thinking] = encoded(cfg)
    folder = root / 'persona/characters/reimu'
    soul, dialogue = folder / 'SOUL.md', folder / 'DIALOGUE.json'
    if soul.exists():
        text, exchanges = split_reimu(soul.read_text(encoding='utf-8'))
        existing = json.loads(dialogue.read_text(encoding='utf-8')) if dialogue.exists() else None
        if existing is None or exchanges is None or existing == exchanges:
            changes[soul] = text.encode('utf-8')
            if exchanges and existing is None:
                changes[dialogue] = encoded(exchanges)
    for pid in ('hiyori', 'reimu'):
        target = root / 'persona/characters' / pid / 'MOODS.json'
        if not target.exists():
            legacy = root / 'data/mood_catalog.json'
            source = legacy if pid == 'hiyori' and legacy.exists() else PROJECT / 'persona_defaults' / pid / 'MOODS.json'
            changes[target] = source.read_bytes()
    return {p: raw for p, raw in changes.items() if not p.exists() or p.read_bytes() != raw}


def apply(root, changes):
    backup = root / 'backups' / ('persona-runtime-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    manifest = []
    for path in changes:
        relative = path.relative_to(root)
        manifest.append({'path': relative.as_posix(), 'existed': path.exists()})
        if path.exists():
            dest = backup / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    (backup / 'manifest.json').write_bytes(encoded(manifest))
    for path, raw in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.migration.tmp')
        tmp.write_bytes(raw)
        tmp.replace(path)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / 'src').is_dir():
        parser.error('目标必须是 AIPet 安装目录（包含 src）。')
    changes = plan(root)
    print('待修改：' if not args.apply else '将备份并修改：')
    for path in changes:
        print(path.relative_to(root))
    if args.apply and changes:
        print('备份：', apply(root, changes))
    print('私人 BOUNDARIES.md 未自动改写；需阅读实际内容后整理。')


if __name__ == '__main__':
    main()
