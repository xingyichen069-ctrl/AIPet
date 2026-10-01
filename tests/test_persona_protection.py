"""Private persona protection, using only synthetic installations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tools')]
from persona_manager import PersonaManager
import migrate_persona_runtime as migration


def snapshot(root):
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes()
            for p in root.rglob('*')}


class PersonaImports(unittest.TestCase):
    def setUp(self):
        (PROJECT / 'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT / 'work')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'install'
        default = self.root / 'persona_defaults/hiyori/SOUL.md'
        default.parent.mkdir(parents=True)
        default.write_text('# Original\nOriginal persona\n', encoding='utf-8')
        self.manager = PersonaManager(self.root)

    def source(self, name, raw):
        path = self.base / name / 'SOUL.md'
        path.parent.mkdir()
        path.write_bytes(raw)
        return path

    def test_repeated_import_preserves_existing_persona_and_all_side_files(self):
        first_source = self.source('first', b'# Alice\nAlice original\n')
        second_source = self.source('second', b'# Bob\nBob imported\n')
        first = self.manager.import_soul(first_source)
        self.manager.set_active(first['id'])
        folder = Path(first['path'])
        for name in ('BOUNDARIES.md', 'avatar.png', 'DIALOGUE.json', 'MOODS.json'):
            (folder / name).write_bytes(('original ' + name).encode())
        before = snapshot(self.root)
        second = self.manager.import_soul(second_source)
        third = self.manager.import_soul(first_source)
        self.assertEqual(len({first['id'], second['id'], third['id']}), 3)
        for name, raw in before.items():
            self.assertEqual(snapshot(self.root)[name], raw)
        self.assertEqual(self.manager.active_id(), first['id'])
        self.assertEqual({p.name for p in Path(second['path']).iterdir()}, {'SOUL.md'})
        self.assertEqual(Path(second['soul']).read_bytes(), second_source.read_bytes())
        self.assertFalse(second['active'])

    def test_import_preserves_bom_crlf_whitespace_and_untitled_text(self):
        raw = b'\xef\xbb\xbf  untitled persona\r\n\r\n  keep these spaces  \r\n'
        source = self.source('raw', raw)
        active = self.manager.active_id()
        item = self.manager.import_soul(source)
        self.assertEqual(Path(item['soul']).read_bytes(), raw)
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(self.manager.active_id(), active)

    def test_case_normalization_files_and_incomplete_directories_are_reserved(self):
        source = self.source('source', b'# New\n')
        occupied = self.manager.characters_dir / 'Mixed'
        occupied.mkdir()
        (occupied / 'avatar.png').write_bytes(b'keep')
        (self.manager.characters_dir / 'a-b').write_bytes(b'occupied file')
        one = self.manager.import_soul(source, pid='MIXED')
        two = self.manager.import_soul(source, pid='A+B')
        three = self.manager.import_soul(source, pid='A B')
        self.assertEqual(one['id'], 'mixed-2')
        self.assertEqual(two['id'], 'a-b-2')
        self.assertEqual(three['id'], 'a-b-3')
        self.assertEqual((occupied / 'avatar.png').read_bytes(), b'keep')
        self.assertFalse((occupied / 'SOUL.md').exists())

    def test_invalid_source_does_not_reserve_or_change_a_persona(self):
        for index, raw in enumerate((b'', b'\xef\xbb\xbf \r\n', b'\xffinvalid')):
            with self.subTest(raw=raw):
                source = self.source(str(index), raw)
                before = snapshot(self.root)
                with self.assertRaises((ValueError, UnicodeError)):
                    self.manager.import_soul(source)
                self.assertEqual(snapshot(self.root), before)

    def test_failed_write_cleans_only_the_new_directory(self):
        source = self.source('source', b'# New\n')
        old = self.manager.import_soul(source)
        before = snapshot(self.root)
        original_open = Path.open

        def fail_after_partial_write(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if args and args[0] == 'xb' and path.name == 'SOUL.md':
                stream.write(b'partial')
                stream.close()
                raise OSError('synthetic write failure')
            return stream

        with patch.object(Path, 'open', fail_after_partial_write):
            with self.assertRaisesRegex(OSError, 'synthetic'):
                self.manager.import_soul(source)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(Path(old['soul']).read_bytes(), source.read_bytes())

    def test_concurrent_imports_do_not_overwrite_each_other(self):
        source = self.source('source', b'# Same\nIndependent copies\n')
        with ThreadPoolExecutor(max_workers=6) as pool:
            items = list(pool.map(lambda _: self.manager.import_soul(source), range(12)))
        self.assertEqual(len({item['id'] for item in items}), 12)
        self.assertTrue(all(Path(item['soul']).read_bytes() == source.read_bytes() for item in items))
        self.assertEqual(self.manager.active_id(), 'hiyori')

    def test_explicit_edit_still_saves_the_selected_persona(self):
        source = self.source('source', b'# First\n')
        item = self.manager.import_soul(source)
        before_count = len(self.manager.list_personas())
        self.manager.save_soul(item['id'], '# Edited\nMy deliberate change')
        self.assertEqual(self.manager.read_soul(item['id']), '# Edited\nMy deliberate change\n')
        self.assertEqual(len(self.manager.list_personas()), before_count)
        backups = list((Path(item['path']) / 'backups').glob('SOUL-*.md'))
        self.assertEqual([p.read_bytes() for p in backups], [source.read_bytes()])

    def test_legacy_initialization_keeps_an_existing_character_boundary(self):
        soul = self.manager.characters_dir / 'hiyori/SOUL.md'
        soul.unlink()
        boundary = soul.with_name('BOUNDARIES.md')
        boundary.write_bytes(b'\xef\xbb\xbfMY CHARACTER RULES\r\n')
        self.manager.legacy_soul.write_bytes(b'My legacy persona\r\n')
        self.manager.legacy_boundaries.write_bytes(b'Older shared rules\r\n')
        PersonaManager(self.root)
        self.assertEqual(soul.read_bytes(), b'My legacy persona\r\n')
        self.assertEqual(boundary.read_bytes(), b'\xef\xbb\xbfMY CHARACTER RULES\r\n')
        self.assertEqual(self.manager.legacy_boundaries.read_bytes(), b'Older shared rules\r\n')

    def test_confirmed_import_replaces_only_explicit_target_and_keeps_raw_backup(self):
        original = b'\xef\xbb\xbf# Same\r\n  old wording  \r\n'
        target = self.manager.import_soul(self.source('old', original))
        folder = Path(target['path'])
        for name in ('BOUNDARIES.md', 'avatar.png', 'DIALOGUE.json', 'MOODS.json'):
            (folder / name).write_bytes(('keep ' + name).encode())
        before = snapshot(self.root)
        replacement = b'\xef\xbb\xbf# Same\r\n  new wording  \r\n'
        source = self.source('update', replacement)
        result = self.manager.import_soul(source, target['id'], overwrite=True)
        self.assertEqual(result['id'], target['id'])
        self.assertEqual(Path(result['soul']).read_bytes(), replacement)
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        self.assertEqual(self.manager.read_soul(target['id']), '# Same\n  new wording  \n')
        after = snapshot(self.root)
        soul_relative = Path(target['soul']).relative_to(self.root).as_posix()
        for path, raw in before.items():
            if path != soul_relative:
                self.assertEqual(after[path], raw)
        self.assertEqual(self.manager.active_id(), 'hiyori')
        self.assertFalse(result['active'])
        self.assertEqual(len(self.manager.list_personas()), 2)
        self.assertEqual(source.read_bytes(), replacement)

    def test_overwrite_requires_existing_explicit_id_and_valid_text(self):
        source = self.source('source', b'# Update\n')
        for target in (None, 'missing', '../hiyori', 'HIYORI'):
            with self.subTest(target=target):
                before = snapshot(self.root)
                with self.assertRaises(ValueError):
                    self.manager.import_soul(source, target, overwrite=True)
                self.assertEqual(snapshot(self.root), before)
        for raw in (b'', b'\xef\xbb\xbf \r\n', b'\xffinvalid'):
            source.write_bytes(raw)
            before = snapshot(self.root)
            with self.assertRaises((ValueError, UnicodeError)):
                self.manager.import_soul(source, 'hiyori', overwrite=True)
            self.assertEqual(snapshot(self.root), before)

    def test_identical_import_is_noop_and_repeated_edits_keep_all_backups(self):
        soul = self.manager.characters_dir / 'hiyori/SOUL.md'
        original = soul.read_bytes()
        before = snapshot(self.root)
        result = self.manager.import_soul(soul, 'hiyori', overwrite=True)
        self.assertEqual(result['backup'], '')
        self.assertEqual(snapshot(self.root), before)
        source = self.source('source', b'# First edit\n')
        first = self.manager.import_soul(source, 'hiyori', overwrite=True)
        source.write_bytes(b'# Second edit\n')
        second = self.manager.import_soul(source, 'hiyori', overwrite=True)
        self.assertNotEqual(first['backup'], second['backup'])
        self.assertEqual(Path(first['backup']).read_bytes(), original)
        self.assertEqual(Path(second['backup']).read_bytes(), b'# First edit\n')
        self.assertEqual(self.manager.active_id(), 'hiyori')

    def test_backup_failure_blocks_overwrite_and_cleans_partial_files(self):
        source = self.source('source', b'# New\n')
        soul = self.manager.characters_dir / 'hiyori/SOUL.md'
        original = soul.read_bytes()
        with patch('persona_manager.os.fsync', side_effect=[None, OSError('backup failed')]):
            with self.assertRaisesRegex(OSError, 'backup failed'):
                self.manager.import_soul(source, 'hiyori', overwrite=True)
        self.assertEqual(soul.read_bytes(), original)
        self.assertEqual(list((soul.parent / 'backups').iterdir()), [])
        self.assertEqual(list(soul.parent.glob('.SOUL-*')), [])

    def test_publish_failure_keeps_original_and_complete_backup(self):
        source = self.source('source', b'# New\n')
        soul = self.manager.characters_dir / 'hiyori/SOUL.md'
        original = soul.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                self.manager.import_soul(source, 'hiyori', overwrite=True)
        self.assertEqual(soul.read_bytes(), original)
        self.assertEqual([p.read_bytes() for p in (soul.parent / 'backups').iterdir()], [original])
        self.assertEqual(list(soul.parent.glob('.SOUL-*')), [])

    def test_overlapping_writers_cannot_both_publish_from_the_same_old_version(self):
        first = self.source('first', b'# First edit\n')
        second = self.source('second', b'# Second edit\n')
        another_manager = PersonaManager(self.root)
        soul = self.manager.characters_dir / 'hiyori/SOUL.md'
        original = soul.read_bytes()
        publishing = threading.Event()
        proceed = threading.Event()
        original_replace = Path.replace

        def pause_publish(path, target):
            if path.name.startswith('.SOUL-'):
                publishing.set()
                if not proceed.wait(5):
                    raise RuntimeError('writer was not released')
            return original_replace(path, target)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Path, 'replace', pause_publish):
            future = pool.submit(self.manager.import_soul, first, 'hiyori', overwrite=True)
            try:
                self.assertTrue(publishing.wait(5))
                with self.assertRaisesRegex(OSError, '正在保存'):
                    another_manager.import_soul(second, 'hiyori', overwrite=True)
                self.assertEqual(soul.read_bytes(), original)
            finally:
                proceed.set()
            first_result = future.result(timeout=5)
        second_result = another_manager.import_soul(second, 'hiyori', overwrite=True)
        self.assertEqual(Path(first_result['backup']).read_bytes(), original)
        self.assertEqual(Path(second_result['backup']).read_bytes(), first.read_bytes())
        self.assertEqual(soul.read_bytes(), second.read_bytes())


class PersonaImportUI(unittest.TestCase):
    source = PersonaImports.source

    def setUp(self):
        PersonaImports.setUp(self)
        from PySide6.QtWidgets import QApplication
        import persona_ui as UI
        self.ui = UI
        self.app = QApplication.instance() or QApplication([])
        self.selected = self.manager.create('friend', '# Friend\nOriginal friend\n')
        with patch.object(UI, 'PersonaManager', return_value=self.manager):
            self.dialog = UI.PersonaDialog()
        self.addCleanup(self.dialog.close)
        self.dialog._reload(self.selected['id'])

    def test_cancel_keeps_files_selection_and_unsaved_text(self):
        source = self.source('source', b'# Friend\nReplacement\n')
        self.dialog.editor.setPlainText('unsaved persona')
        before = snapshot(self.root)
        with patch.object(self.ui.QFileDialog, 'getOpenFileName', return_value=(str(source), '')), \
                patch.object(self.ui.QMessageBox, 'question', return_value=self.ui.QMessageBox.No) as ask:
            self.dialog._import_soul()
        self.assertIn('Friend', ask.call_args.args[2])
        self.assertIn(self.selected['id'], ask.call_args.args[2])
        self.assertEqual(ask.call_args.args[-1], self.ui.QMessageBox.No)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.dialog.editor.toPlainText(), 'unsaved persona')
        self.assertEqual(self.dialog.current_id, self.selected['id'])

    def test_confirm_updates_selected_role_without_extra_entry_or_losing_mood_edits(self):
        source = self.source('source', b'# Updated friend\r\nNew wording\r\n')
        self.dialog.editor.setPlainText('unsaved persona')
        self.dialog.mood_editor.setPlainText('unsaved moods')
        with patch.object(self.ui.QFileDialog, 'getOpenFileName', return_value=(str(source), '')), \
                patch.object(self.ui.QMessageBox, 'question', return_value=self.ui.QMessageBox.Yes), \
                patch.object(self.ui.QMessageBox, 'information') as info:
            self.dialog._import_soul()
        self.assertIn('备份', info.call_args.args[2])
        self.assertEqual(Path(self.selected['soul']).read_bytes(), source.read_bytes())
        self.assertEqual(self.dialog.current_id, self.selected['id'])
        self.assertEqual(self.manager.active_id(), 'hiyori')
        self.assertEqual(self.dialog.list.count(), 2)
        self.assertEqual(self.dialog.editor.toPlainText(), '# Updated friend\nNew wording\n')
        self.assertEqual(self.dialog.mood_editor.toPlainText(), 'unsaved moods')

    def test_file_dialog_cancel_does_not_ask_or_write(self):
        before = snapshot(self.root)
        with patch.object(self.ui.QFileDialog, 'getOpenFileName', return_value=('', '')), \
                patch.object(self.ui.QMessageBox, 'question') as ask:
            self.dialog._import_soul()
        ask.assert_not_called()
        self.assertEqual(snapshot(self.root), before)


class PersonaCandidates(unittest.TestCase):
    def setUp(self):
        (PROJECT / 'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT / 'work')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'old-install'
        (self.root / 'src').mkdir(parents=True)
        self.tool_project = self.base / 'tool-project'
        self.tool_project.mkdir()
        for pid in ('hiyori', 'reimu'):
            path = self.tool_project / 'persona_defaults' / pid / 'MOODS.json'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'{"calm":{"voice":"public default","hours":2}}\n')
        patched = patch.object(migration, 'PROJECT', self.tool_project)
        patched.start()
        self.addCleanup(patched.stop)
        self.soul = self.root / 'persona/characters/reimu/SOUL.md'
        self.soul.parent.mkdir(parents=True)
        self.dialogue = self.soul.with_name('DIALOGUE.json')
        self.raw = ('\ufeff# Custom\r\n\r\n## 话到这里就够了\r\nPRIVATE_STYLE\r\n\r\n'
                    '## 几段声音\r\nPRIVATE_PROSE\r\n\r\n对方：你好\r\n你：嗯？\r\n\r\n'
                    'Another private paragraph.\r\n\r\n## Next\r\nPRIVATE_AFTER').encode('utf-8')
        self.soul.write_bytes(self.raw)

    def test_exports_review_only_and_preserves_all_other_bytes_and_settings(self):
        config = self.root / 'data/thinking.json'
        config.parent.mkdir()
        config.write_bytes(b'{"current":"serious","temperature":0.51,"custom":"KEEP"}')
        boundaries = self.soul.with_name('BOUNDARIES.md')
        boundaries.write_bytes(b'PRIVATE_BOUNDARIES')
        self.soul.with_name('MOODS.json').write_bytes(b'{"private":"moods"}')
        before = snapshot(self.root)
        output = migration.export_candidates(self.root)
        second = migration.export_candidates(self.root)
        self.assertNotEqual(output, second)
        self.assertEqual(output.parent, self.tool_project / 'work')
        self.assertEqual(snapshot(self.root), before)
        expected = self.raw.replace('对方：你好\r\n你：嗯？\r\n'.encode('utf-8'), b'')
        relative = self.soul.relative_to(self.root)
        self.assertEqual((output / 'candidates' / relative).read_bytes(), expected)
        dialogue = json.loads((output / 'candidates' / self.dialogue.relative_to(self.root)).read_bytes())
        self.assertEqual(dialogue, [[{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '嗯？'}]])
        manifest = json.loads((output / 'manifest.json').read_bytes())
        files = {item['path']: item for item in manifest['files']}
        self.assertEqual(set(files), {relative.as_posix(), self.dialogue.relative_to(self.root).as_posix()})
        self.assertEqual(files[relative.as_posix()]['original_sha256'], hashlib.sha256(self.raw).hexdigest())
        difference = (output / 'diffs' / (relative.as_posix() + '.diff')).read_text(encoding='utf-8')
        self.assertIn('-对方：你好', difference)
        self.assertNotIn('-PRIVATE_STYLE', difference)

    def test_custom_or_malformed_dialogue_prevents_automatic_splitting(self):
        for raw in (b'[[{"role":"user","content":"mine"},{"role":"assistant","content":"keep"}]]', b'{bad', b''):
            with self.subTest(raw=raw):
                self.dialogue.write_bytes(raw)
                before = snapshot(self.root)
                changes = migration.plan(self.root)
                self.assertNotIn(self.soul, changes)
                self.assertNotIn(self.dialogue, changes)
                self.assertEqual(snapshot(self.root), before)

    def test_matching_existing_dialogue_is_not_reformatted(self):
        _, exchanges = migration.split_reimu(self.raw.decode('utf-8'))
        raw = json.dumps(exchanges, ensure_ascii=False, indent=4).encode('utf-8')
        self.dialogue.write_bytes(raw)
        changes = migration.plan(self.root)
        self.assertIn(self.soul, changes)
        self.assertNotIn(self.dialogue, changes)
        self.assertEqual(self.dialogue.read_bytes(), raw)

    def test_ambiguous_or_unusable_dialogue_stays_verbatim(self):
        for body in ('对方：只有问题\n', '对方：问题\n你：回答\n私人注解\n',
                     '对方：问题\n你：\n', '```text\n\n对方：问题\n你：回答\n\n```\n',
                     '对方：' + '长' * 1501 + '\n你：回答\n',
                     '对方：问题\n你：回答\n\n' * 25):
            with self.subTest(body=body[:40]):
                text = '# Custom\n## 几段声音\n' + body
                self.assertEqual(migration.split_reimu(text), (text, None))
        text = '# Custom\n## 几段声音\n对方：你好\n你：嗯\n## 几段声音\n私人说明\n'
        self.assertEqual(migration.split_reimu(text), (text, None))
        text = '```markdown\n## 几段声音\n对方：你好\n你：嗯\n## Other\n```\n'
        self.assertEqual(migration.split_reimu(text), (text, None))

    def test_missing_moods_are_only_candidates_and_keep_legacy_content(self):
        legacy_soul = self.root / 'persona/SOUL.md'
        legacy_soul.write_bytes(b'My hiyori')
        legacy_moods = self.root / 'data/mood_catalog.json'
        legacy_moods.parent.mkdir()
        raw = b'{"private":{"voice":"KEEP EXACT","hours":1}}\r\n'
        legacy_moods.write_bytes(raw)
        before = snapshot(self.root)
        changes = migration.plan(self.root)
        target = self.root / 'persona/characters/hiyori/MOODS.json'
        self.assertEqual(changes[target], raw)
        self.assertFalse(target.exists())
        self.assertEqual(snapshot(self.root), before)

    def test_old_apply_entries_fail_clearly_without_reading_or_writing_install(self):
        before = snapshot(self.root)
        output = io.StringIO()
        with redirect_stderr(output), patch.object(migration, 'export_candidates', side_effect=AssertionError('must not export')):
            result = migration.main([str(self.root), '--apply'])
        self.assertEqual(result, 2)
        self.assertIn('--apply 已停用', output.getvalue())
        with self.assertRaisesRegex(ValueError, '已停用'):
            migration.apply(self.root, {self.soul: b'overwrite'})
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.tool_project / 'work').exists())

    def test_export_refuses_to_put_outputs_inside_the_source_install(self):
        before = snapshot(self.tool_project)
        with self.assertRaisesRegex(ValueError, '原安装'):
            migration.export_candidates(self.tool_project)
        self.assertEqual(snapshot(self.tool_project), before)

    def test_export_failure_removes_only_its_new_report_and_preserves_source(self):
        before = snapshot(self.root)
        original_write = Path.write_bytes

        def fail_report(path, data):
            if path.name == 'DIALOGUE.json' and 'candidates' in path.parts:
                raise OSError('synthetic export failure')
            return original_write(path, data)

        with patch.object(Path, 'write_bytes', fail_report):
            with self.assertRaisesRegex(OSError, 'synthetic'):
                migration.export_candidates(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(list((self.tool_project / 'work').iterdir()), [])

    def test_changed_source_is_not_exported_as_a_stale_candidate(self):
        original_review = migration._review

        def changed_after_review(root):
            result = original_review(root)
            self.soul.write_bytes(self.raw + b'\r\nNEW USER EDIT')
            return result

        with patch.object(migration, '_review', changed_after_review):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                migration.export_candidates(self.root)
        self.assertEqual(self.soul.read_bytes(), self.raw + b'\r\nNEW USER EDIT')
        self.assertEqual(list((self.tool_project / 'work').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
