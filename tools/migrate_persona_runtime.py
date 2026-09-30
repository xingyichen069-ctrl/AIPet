"""Export review candidates for an old persona layout; never modify that install.

Candidates and readable diffs stay under this tool's own project/work directory.
Existing persona prose, boundaries, dialogue and thinking parameters are preserved.
The former --apply operation is disabled, including the Python apply() entry.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def split_reimu(text):
    """Suggest moving complete dialogue paragraphs, preserving every other byte."""
    headings = list(re.finditer(r'^## 几段声音[ \t]*\r?$', text, re.M))
    if len(headings) != 1:
        return text, None
    start = headings[0].end()
    following = re.search(r'^#{1,2}[ \t]+', text[start:], re.M)
    end = start + following.start() if following else len(text)
    body = text[start:end]
    # With code fences, even the heading may be an example. Leave it for review.
    if re.search(r'^[ \t]*(?:`{3,}|~{3,})', text, re.M):
        return text, None
    blocks, offset, block_start = [], start, None
    for line in body.splitlines(keepends=True):
        if line.strip():
            if block_start is None:
                block_start = offset
        elif block_start is not None:
            blocks.append((block_start, offset))
            block_start = None
        offset += len(line)
    if block_start is not None:
        blocks.append((block_start, end))
    spans, exchanges = [], []
    for first, last in blocks:
        lines = text[first:last].splitlines()
        if not lines or len(lines) % 2:
            continue
        messages = []
        for index, line in enumerate(lines):
            prefix = '对方：' if index % 2 == 0 else '你：'
            if not line.startswith(prefix):
                break
            content = line[len(prefix):]
            if not content.strip() or len(content) > 1500:
                break
            messages.append({'role': 'user' if index % 2 == 0 else 'assistant',
                             'content': content})
        if len(messages) == len(lines):
            exchanges.append(messages)
            spans.append((first, last))
    # The runtime reads at most 24 exchanges. Do not move text it would discard.
    if not exchanges or len(exchanges) > 24:
        return text, None
    for first, last in reversed(spans):
        text = text[:first] + text[last:]
    return text, exchanges


@dataclass(frozen=True)
class Candidate:
    relative: Path
    before: bytes | None
    after: bytes
    reason: str


def _read(root: Path, relative: Path) -> bytes | None:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'文件指向所选安装之外，未读取：{relative.as_posix()}')
    return path.read_bytes() if path.exists() else None


def _review(root: Path) -> tuple[list[Candidate], list[str]]:
    candidates = []
    notes = ['已有 SOUL、BOUNDARIES、DIALOGUE、MOODS 和档位参数不会被这个工具覆盖。',
             '风格规则及 daily/serious 参数不再套用公开默认值。']
    soul = Path('persona/characters/reimu/SOUL.md')
    dialogue = soul.with_name('DIALOGUE.json')
    raw = _read(root, soul)
    if raw is not None:
        proposed, exchanges = split_reimu(raw.decode('utf-8'))
        existing = _read(root, dialogue)
        matches = False
        if existing is not None:
            try:
                matches = json.loads(existing) == exchanges
            except (ValueError, UnicodeError):
                pass
        if exchanges and (existing is None or matches):
            candidates.append(Candidate(soul, raw, proposed.encode('utf-8'),
                                        '仅建议移出完整识别的示例段落；其他原文逐字保留。'))
            if existing is None:
                candidates.append(Candidate(dialogue, None, encoded(exchanges),
                                            '保存从旧 SOUL 精确识别的示例；需人工审阅。'))
        elif exchanges:
            notes.append('已有 DIALOGUE 与识别结果不同或格式无法确认；保留两份原文，不建议自动拆分。')
        else:
            notes.append('SOUL 中没有可完整识别且符合运行时限制的旧示例；不生成正文替换候选。')
    for pid in ('hiyori', 'reimu'):
        relative = Path('persona/characters') / pid / 'MOODS.json'
        if _read(root, relative) is not None:
            continue
        # Do not propose creating characters which this installation does not have.
        has_soul = _read(root, relative.with_name('SOUL.md')) is not None
        if pid == 'hiyori':
            has_soul = has_soul or _read(root, Path('persona/SOUL.md')) is not None
        if not has_soul:
            continue
        legacy = _read(root, Path('data/mood_catalog.json')) if pid == 'hiyori' else None
        source = PROJECT / 'persona_defaults' / pid / 'MOODS.json'
        if legacy is not None:
            proposed = legacy
        elif source.is_file():
            proposed = source.read_bytes()
        else:
            notes.append(f'{pid} 缺少公开情绪模板，未生成候选。')
            continue
        candidates.append(Candidate(relative, None, proposed,
                                    '仅为缺失文件提供情绪文案候选，不写入原安装。'))
    return candidates, notes


def plan(root):
    """Return proposed bytes for inspection only; this function never writes."""
    root = Path(root).resolve()
    candidates, _ = _review(root)
    return {root / item.relative: item.after for item in candidates}


def export_candidates(root: Path) -> Path:
    root = Path(root).resolve()
    project = PROJECT.resolve()
    work = project / 'work'
    if not work.resolve().is_relative_to(project):
        raise ValueError('工具的 work 指向项目之外，未导出。')
    if work.resolve().is_relative_to(root):
        raise ValueError('输出位置会落入所选原安装。请从与原安装分开的新版代码目录运行此工具。')
    candidates, notes = _review(root)
    work.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='persona-runtime-candidates-', dir=work))
    try:
        manifest = {'mode': 'review-only', 'source': str(root), 'files': [], 'notes': notes}
        for item in candidates:
            if _read(root, item.relative) != item.before:
                raise ValueError(f'原文件在检查后发生变化，请重新导出：{item.relative.as_posix()}')
            candidate = output / 'candidates' / item.relative
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(item.after)
            difference = output / 'diffs' / (item.relative.as_posix() + '.diff')
            difference.parent.mkdir(parents=True, exist_ok=True)
            before = (item.before or b'').decode('utf-8-sig').splitlines()
            after = item.after.decode('utf-8-sig').splitlines()
            lines = difflib.unified_diff(before, after,
                                        fromfile='original/' + item.relative.as_posix(),
                                        tofile='candidate/' + item.relative.as_posix(), lineterm='')
            difference.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            manifest['files'].append({
                'path': item.relative.as_posix(), 'reason': item.reason,
                'original_sha256': hashlib.sha256(item.before).hexdigest() if item.before is not None else None,
                'candidate_sha256': hashlib.sha256(item.after).hexdigest(),
            })
        (output / 'manifest.json').write_bytes(encoded(manifest))
        (output / 'README.txt').write_text(
            '这是供人工审阅的候选，不是已应用的更新。原安装没有被修改。\n'
            'candidates 保存候选文件；diffs 保存可读差异；manifest.json 记录来源与校验值。\n'
            '不要把整个候选目录直接覆盖到安装中；请逐项阅读。常规更新使用 README 的目录迁移流程。\n'
            '这些文件可能包含私人文案，请留在本机，不要公开上传。\n\n' + '\n'.join(notes) + '\n',
            encoding='utf-8')
    except BaseException:
        shutil.rmtree(output)
        raise
    return output


def apply(root, changes):
    """Fail explicitly for callers of the retired mutating API."""
    raise ValueError('直接应用已停用；请使用 export_candidates 导出差异和候选，原安装不会被修改。')


def main(argv=None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--apply', action='store_true', help='已停用；不会修改原安装')
    args = parser.parse_args(argv)
    if args.apply:
        print('--apply 已停用，未修改也未导出任何文件。去掉 --apply 可仅导出差异和候选。', file=sys.stderr)
        return 2
    root = args.root.resolve()
    if not (root / 'src').is_dir():
        parser.error('目标必须是 AIPet 安装目录（包含 src）。')
    try:
        output = export_candidates(root)
    except (OSError, ValueError, UnicodeError) as exc:
        print(f'未导出：{exc}', file=sys.stderr)
        return 1
    print('原安装未修改。候选与差异已导出到：', output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
