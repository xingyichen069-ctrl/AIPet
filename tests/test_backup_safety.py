"""Real ZIP/SQLite round trips and failure injection in public fixture trees."""
from contextlib import closing, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import backup as BK
import companion as C

PROJECT = Path(__file__).resolve().parents[1]


class BackupSafety(unittest.TestCase):
    def setUp(self):
        work = Path(os.environ.get("AIPET_TEST_WORK", PROJECT / "work"))
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.base = Path(self.temp.name)
        self.root = self.base / "install"
        self.root.mkdir()
        self.patcher = patch.object(BK, "BACKUP_DIR", self.root / "backups")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def archive(self, members, name="fixture.zip"):
        target = self.root / "backups" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as z:
            for arc, content in members:
                z.writestr(arc, content)
        return target

    def restore(self, path, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            result = BK.restore(path.name, **kwargs)
        return result, output.getvalue()

    def personas(self):
        files = {
            "persona/characters/alpha/SOUL.md": "# Alpha\n私人正文\n",
            "persona/characters/alpha/BOUNDARIES.md": "边界原文\n",
            "persona/characters/alpha/DIALOGUE.json": "[]\n",
            "persona/characters/alpha/MOODS.json": '{"tea":{"voice":"tea","hours":2}}',
            "persona/characters/alpha/avatar.png": b"SYNTHETIC_PNG_ALPHA",
            "persona/characters/alpha/notes/context.md": "nested note\n",
            "persona/characters/beta/SOUL.md": "# Beta\n",
            "persona/characters/beta/avatar.png": b"SYNTHETIC_PNG_BETA",
            "persona/active.json": '{"id":"alpha"}',
            "data/persona_moods.json": '{"alpha":"beta"}',
            "data/moods/beta.json": '{"current":"tea","history":[],"rules":{}}',
            "data/mood_catalog.json": '{"old":{"voice":"old"}}',
            "data/mood_log.jsonl": '{"state":"tea"}\n',
            "data/mood.json": '{"current":"quiet"}',
        }
        for name, value in files.items():
            self.put(name, value)
        return {name: (self.root / name).read_bytes() for name in files}

    def test_complete_persona_snapshot_and_default_export_leave_live_data_unchanged(self):
        expected = self.personas()
        self.put("data/secrets.json", '{"key":"SYNTHETIC_PRIVATE_KEY"}')
        self.put("data/qq_token.json", '{"token":"SYNTHETIC_TOKEN"}')
        self.put("data/cache/private.txt", "cache")
        self.put("persona/characters/alpha/cache/derived.json", "{}")
        archive = BK.create("personas")
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(set(z.namelist()), set(expected) | {"_manifest.json"})
            for name, data in expected.items():
                self.assertEqual(z.read(name), data)
        current = self.put("persona/characters/alpha/SOUL.md", "# CURRENT_PERSONA\n")
        result, output = self.restore(archive)
        self.assertEqual(result, 0, output)
        self.assertEqual(current.read_text(), "# CURRENT_PERSONA\n")
        exports = list((self.root / "backups").glob("restored-*"))
        self.assertEqual(len(exports), 1)
        for name, data in expected.items():
            self.assertEqual((exports[0] / name).read_bytes(), data)
        self.assertIn("当前安装未修改", output)
        self.assertFalse(list((self.root / "backups").glob("*prerestore*.zip")))

    def test_in_place_keeps_complete_safety_snapshot_and_unmentioned_persona(self):
        expected = self.personas()
        archive = BK.create()
        self.put("persona/characters/alpha/SOUL.md", "# CURRENT\n")
        self.put("persona/characters/gamma/SOUL.md", "# EXTRA\n")
        result, output = self.restore(archive, in_place=True)
        self.assertEqual(result, 0, output)
        for name, data in expected.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual((self.root / "persona/characters/gamma/SOUL.md").read_text(), "# EXTRA\n")
        safety = next((self.root / "backups").glob("*prerestore*.zip"))
        with zipfile.ZipFile(safety) as z:
            self.assertEqual(z.read("persona/characters/alpha/SOUL.md"), b"# CURRENT\n")
            self.assertEqual(z.read("persona/characters/gamma/SOUL.md"), b"# EXTRA\n")

    def test_same_timestamp_backups_never_replace_earlier_archive(self):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 1, 12, 0, 0, tzinfo=tz)
        self.put("persona/PROFILE.md", "first")
        with patch.object(BK, "datetime", Frozen):
            first = BK.create("same")
            original = first.read_bytes()
            self.put("persona/PROFILE.md", "second")
            second = BK.create("same")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        with zipfile.ZipFile(second) as z:
            self.assertEqual(z.read("persona/PROFILE.md"), b"second")

    def test_backup_failure_leaves_no_finished_or_partial_archive(self):
        self.put("persona/PROFILE.md", "profile")
        with patch.object(BK, "_snapshot", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                BK.create()
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_unreadable_archive_does_not_hide_other_backups(self):
        good = self.archive([("memory/journal.jsonl", '{}\n')], "good.zip")
        broken = self.root / "backups/broken.zip"
        with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("memory/journal.jsonl", '{}\n' * 100)
        with zipfile.ZipFile(broken) as z:
            info = z.getinfo("memory/journal.jsonl")
        raw = bytearray(broken.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
        offset = info.header_offset + 30 + name_len + extra_len
        raw[offset] = (raw[offset] & 0xF8) | 0x07  # forbidden DEFLATE block type
        broken.write_bytes(raw)
        rows = {row["name"]: row for row in BK.list_backups()}
        self.assertEqual(rows[good.name]["journal_entries"], 1)
        self.assertEqual(rows[broken.name]["journal_entries"], -1)

    def test_crc_is_checked_before_any_live_write(self):
        self.put("persona/PROFILE.md", "current profile")
        self.put("persona/BOUNDARIES.md", "current boundary")
        archive = self.archive([("persona/PROFILE.md", "old profile"), ("persona/BOUNDARIES.md", "old boundary")])
        with zipfile.ZipFile(archive) as z:
            info = z.getinfo("persona/BOUNDARIES.md")
        raw = bytearray(archive.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
        raw[info.header_offset + 30 + name_len + extra_len] ^= 1
        archive.write_bytes(raw)
        result, _ = self.restore(archive, in_place=True)
        self.assertEqual(result, 1)
        self.assertEqual((self.root / "persona/PROFILE.md").read_text(), "current profile")
        self.assertEqual((self.root / "persona/BOUNDARIES.md").read_text(), "current boundary")
        self.assertFalse(list((self.root / "backups").glob("*prerestore*.zip")))

    def test_disallowed_paths_links_and_case_collisions_are_rejected(self):
        marker = self.put("persona/PROFILE.md", "unchanged")
        bad_names = ["../outside.txt", str(self.base / "absolute.txt"), "C:relative.txt", "..\\outside.txt",
                     "data/secrets.json", "src/pet.py", "persona/characters/a/NUL.txt",
                     "persona/characters/a/SOUL.md:stream", "persona/characters/a/SOUL.md."]
        for index, name in enumerate(bad_names):
            with self.subTest(name=name):
                archive = self.archive([("persona/PROFILE.md", "changed"), (name, "bad")], f"bad-{index}.zip")
                result, _ = self.restore(archive, in_place=True)
                self.assertEqual(result, 1)
                self.assertEqual(marker.read_text(), "unchanged")
        info = zipfile.ZipInfo("persona/characters/a/SOUL.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.archive([(info, "../../outside")], "link.zip")
        self.assertEqual(self.restore(archive)[0], 1)
        archive = self.archive([("persona/PROFILE.md", "a"), ("persona/profile.md", "b")], "case.zip")
        self.assertEqual(self.restore(archive)[0], 1)

    def test_invalid_json_manifest_and_sqlite_schema_are_rejected(self):
        marker = self.put("persona/PROFILE.md", "current")
        broken = [("data/config.json", "[]"), ("data/thinking.json", '{"current":[]}'),
                  (BK.DATABASE, b"not a database"), ("persona/active.json", '{"id":"missing"}')]
        for index, member in enumerate(broken):
            archive = self.archive([("persona/PROFILE.md", "old"), member], f"schema-{index}.zip")
            self.assertEqual(self.restore(archive, in_place=True)[0], 1)
            self.assertEqual(marker.read_text(), "current")
        archive = self.archive([("persona/PROFILE.md", "old"), ("_manifest.json", '{"files":[]}')], "manifest.zip")
        self.assertEqual(self.restore(archive, in_place=True)[0], 1)
        db_path = self.base / "unrelated.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE unrelated (x)")
            db.commit()
        archive = self.archive([(BK.DATABASE, db_path.read_bytes())], "db-schema.zip")
        self.assertEqual(self.restore(archive)[0], 1)

    def test_restore_failure_rolls_back_existing_and_new_files(self):
        current = self.put("persona/PROFILE.md", "current")
        archive = self.archive([("persona/PROFILE.md", "archived"), ("persona/new.md", "new"),
                                ("data/appearance.json", "{}")])
        operation = BK._write_staged
        count = 0
        def fail_third(*args):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic disk failure")
            return operation(*args)
        with patch.object(BK, "_write_staged", side_effect=fail_third):
            result, output = self.restore(archive, in_place=True)
        self.assertEqual(result, 1)
        self.assertIn("本次写入已回退", output)
        self.assertEqual(current.read_text(), "current")
        self.assertFalse((self.root / "persona/new.md").exists())
        self.assertFalse((self.root / "data/appearance.json").exists())
        manifest = next((self.root / "backups").glob("restore-before-*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text())["status"], "rolled-back")

    def test_incomplete_rollback_is_reported_and_originals_remain_available(self):
        self.put("persona/PROFILE.md", "current")
        archive = self.archive([("persona/PROFILE.md", "archived"), ("data/appearance.json", "{}")])
        operation, replace = BK._write_staged, os.replace
        def fail_second(source, target, name, existed):
            if name == "data/appearance.json":
                raise OSError("synthetic write failure")
            return operation(source, target, name, existed)
        def fail_rollback(source, target):
            if "rollback" in Path(source).parts:
                raise OSError("synthetic rollback failure")
            return replace(source, target)
        with patch.object(BK, "_write_staged", side_effect=fail_second), patch.object(BK.os, "replace", side_effect=fail_rollback):
            result, output = self.restore(archive, in_place=True)
        self.assertEqual(result, 1)
        self.assertIn("部分回退失败", output)
        original = next((self.root / "backups").glob("restore-before-*/persona/PROFILE.md"))
        self.assertEqual(original.read_text(), "current")

    def test_legacy_root_soul_exports_but_cannot_shadow_existing_character(self):
        self.put("persona/characters/hiyori/SOUL.md", "# Current\n")
        archive = self.archive([("persona/SOUL.md", "# Legacy\n")])
        result, output = self.restore(archive, in_place=True)
        self.assertEqual(result, 1)
        self.assertIn("旧备份", output)
        self.assertFalse((self.root / "persona/SOUL.md").exists())
        result, output = self.restore(archive)
        self.assertEqual(result, 0, output)
        self.assertIn("没有完整角色库", output)

    def test_legacy_archive_without_manifest_restores_into_empty_install(self):
        archive = self.archive([("persona/SOUL.md", "# Legacy\n"), ("persona/BOUNDARIES.md", "BOUNDARY")])
        self.assertEqual(self.restore(archive, in_place=True)[0], 0)
        self.assertEqual((self.root / "persona/SOUL.md").read_text(), "# Legacy\n")

    def test_symlink_source_and_target_do_not_escape_fixture_install(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "SOUL.md").write_text("outside")
        (self.root / "persona/characters").mkdir(parents=True)
        link = self.root / "persona/characters/linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not grant fixture symlink creation")
        with self.assertRaises(BK.BackupError):
            BK.create()
        archive = self.archive([("persona/characters/linked/SOUL.md", "bad")])
        self.assertEqual(self.restore(archive, in_place=True)[0], 1)
        self.assertEqual((outside / "SOUL.md").read_text(), "outside")

    def test_sqlite_wal_restores_through_sqlite_without_old_wal_overlay(self):
        store = C.Store(self.root)
        store.add_message("user", "archived")
        keeper = sqlite3.connect(store.path)
        try:
            keeper.execute("PRAGMA journal_mode=WAL")
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            archive = BK.create()
            keeper.execute("UPDATE messages SET text='current extra'")
            keeper.commit()
            self.assertTrue(Path(str(store.path) + "-wal").exists())
            result, output = self.restore(archive, in_place=True)
            self.assertEqual(result, 0, output)
            self.assertEqual([r[0] for r in keeper.execute("SELECT text FROM messages ORDER BY created")], ["archived"])
            self.assertEqual(keeper.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            keeper.close()

    def test_sqlite_sidecar_symlink_is_rejected_before_opening_database(self):
        store = C.Store(self.root)
        outside = self.base / "outside-wal"
        outside.write_bytes(b"SYNTHETIC_OUTSIDE")
        try:
            Path(str(store.path) + "-wal").symlink_to(outside)
        except OSError:
            self.skipTest("OS does not grant fixture symlink creation")
        with self.assertRaisesRegex(BK.BackupError, "日志经过符号链接"):
            BK.create()
        self.assertEqual(outside.read_bytes(), b"SYNTHETIC_OUTSIDE")

    def test_later_failure_rolls_sqlite_back_with_wal_intact(self):
        store = C.Store(self.root)
        store.add_message("user", "archived")
        self.put("persona/PROFILE.md", "archived profile")
        archive = BK.create()
        store.add_message("user", "current extra")
        keeper = sqlite3.connect(store.path)
        keeper.execute("PRAGMA journal_mode=WAL")
        operation = BK._write_staged
        def fail_after_database(source, target, name, existed):
            if name != BK.DATABASE:
                raise OSError("synthetic later failure")
            return operation(source, target, name, existed)
        try:
            with patch.object(BK, "_write_staged", side_effect=fail_after_database):
                result, output = self.restore(archive, in_place=True)
            self.assertEqual(result, 1)
            self.assertIn("本次写入已回退", output)
            self.assertEqual([r[0] for r in keeper.execute("SELECT text FROM messages ORDER BY created")], ["archived", "current extra"])
            self.assertEqual(keeper.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            keeper.close()

    def test_busy_sqlite_fails_with_bounded_wait_and_keeps_live_rows(self):
        store = C.Store(self.root)
        store.add_message("user", "archived")
        archive = BK.create()
        store.add_message("user", "current")
        writer = sqlite3.connect(store.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE messages SET text='uncommitted'")
            with patch.object(BK, "SQLITE_BUSY_SECONDS", 0.05):
                result, output = self.restore(archive, in_place=True)
            self.assertEqual(result, 1)
            self.assertIn("本次写入已回退", output)
            self.assertIn("数据库仍被占用", output)
            writer.rollback()
            self.assertEqual([r[0] for r in writer.execute("SELECT text FROM messages ORDER BY created")], ["archived", "current"])
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
