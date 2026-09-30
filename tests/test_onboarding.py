"""First-install and migration acceptance using synthetic local files only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
from persona_manager import PersonaManager

PROJECT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MIG = load_tool("migrate")
WIZ = load_tool("update_wizard")
FIRST = load_tool("first_run")


class Onboarding(unittest.TestCase):
    def setUp(self):
        work = PROJECT / "work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.base = Path(self.temp.name)
        self.old = self.install("old")
        self.new = self.install("new")

    def tearDown(self):
        self.temp.cleanup()

    def install(self, name):
        root = self.base / name
        (root / "src").mkdir(parents=True)
        (root / "src/pet.py").write_text("# fixture\n", encoding="utf-8")
        (root / "data").mkdir()
        for kind in bootstrap.TEMPLATES:
            shutil.copyfile(PROJECT / "data" / f"{kind}.example.json", root / "data" / f"{kind}.example.json")
        shutil.copytree(PROJECT / "persona_defaults", root / "persona_defaults")
        return root

    def populate_old(self):
        bootstrap.initialize(self.old)
        key = {"deepseek_api_key": "SYNTHETIC_NOT_A_KEY", "custom_preserved": "OLD_FIELD"}
        (self.old / "data/secrets.json").write_text(json.dumps(key), encoding="utf-8")
        cfg = bootstrap.read_object(self.old / "data/thinking.json")
        cfg["current"] = "daily"
        cfg["presets"]["daily"]["params"]["model"] = "my-old-model"
        (self.old / "data/thinking.json").write_text(json.dumps(cfg), encoding="utf-8")
        (self.old / "persona").mkdir()
        (self.old / "persona/SOUL.md").write_text("# MY_PRIVATE_PERSONA\n", encoding="utf-8")
        (self.old / "data/moods").mkdir()
        (self.old / "data/moods/reimu.json").write_text('{"current":"tea"}', encoding="utf-8")
        (self.old / "data/persona_moods.json").write_text('{"reimu":"reimu"}', encoding="utf-8")

    def test_blank_install_creates_editable_files_and_keeps_max(self):
        created = bootstrap.initialize(self.new)
        self.assertEqual(len(created), 3)
        self.assertEqual(bootstrap.validate(self.new), [])
        self.assertEqual(bootstrap.read_object(self.new / "data/thinking.json")["current"], "max")
        self.assertEqual(bootstrap.read_object(self.new / "data/secrets.json")["deepseek_api_key"], "")
        self.assertFalse((self.new / "persona").exists())

    def test_repeated_preparation_never_rewrites_user_bytes(self):
        self.populate_old()
        files = [self.old / "data" / f"{name}.json" for name in bootstrap.TEMPLATES]
        before = {p: p.read_bytes() for p in files}
        self.assertEqual(bootstrap.initialize(self.old), [])
        self.assertEqual(before, {p: p.read_bytes() for p in files})
        (self.old / "data/config.json").unlink()
        bootstrap.initialize(self.old)
        self.assertEqual(before[files[1]], files[1].read_bytes())
        self.assertEqual(before[files[2]], files[2].read_bytes())

    def test_old_partial_config_gets_defaults_without_disk_changes(self):
        bootstrap.initialize(self.old)
        path = self.old / "data/config.json"
        text = '{"tools":{"fs_root":"D:/my-files"},"private_extra":true}'
        path.write_text(text, encoding="utf-8")
        config = bootstrap.load_config(self.old)
        self.assertEqual(config["tools"]["fs_root"], "D:/my-files")
        self.assertEqual(config["paths"]["journal"], "memory/journal.jsonl")
        self.assertTrue(config["private_extra"])
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_invalid_json_reports_location_without_key_and_keeps_file(self):
        bootstrap.initialize(self.new)
        path = self.new / "data/secrets.json"
        bad = '{"deepseek_api_key":"SYNTHETIC_PRIVATE_VALUE",}'
        path.write_text(bad, encoding="utf-8")
        errors = bootstrap.validate(self.new)
        self.assertIn("第 1 行", errors[0])
        self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(errors))
        bootstrap.initialize(self.new)
        self.assertEqual(path.read_text(encoding="utf-8"), bad)

    def test_bom_and_wrong_types_are_handled(self):
        bootstrap.initialize(self.new)
        path = self.new / "data/secrets.json"
        path.write_text('{"deepseek_api_key":""}', encoding="utf-8-sig")
        self.assertEqual(bootstrap.validate(self.new), [])
        path.write_text('[]', encoding="utf-8")
        self.assertTrue(bootstrap.validate(self.new))
        path.write_text('{"deepseek_api_key":123}', encoding="utf-8")
        self.assertTrue(bootstrap.validate(self.new))
        cfg = bootstrap.read_object(self.new / "data/thinking.json")
        cfg["current"] = []
        (self.new / "data/thinking.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertTrue(bootstrap.validate(self.new))

    def test_missing_public_template_fails_clearly(self):
        (self.new / "data/thinking.example.json").unlink()
        with self.assertRaisesRegex(bootstrap.ConfigError, "thinking.example.json"):
            bootstrap.initialize(self.new)

    def test_prepared_new_install_accepts_migration_and_legacy_persona(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        originals = {p: p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        rows, _ = WIZ.inspect_migration(self.old, self.new)
        MIG.apply(rows, self.new)
        bootstrap.initialize(self.new)
        self.assertEqual(bootstrap.validate(self.new), [])
        self.assertEqual((self.new / "data/secrets.json").read_bytes(), (self.old / "data/secrets.json").read_bytes())
        self.assertEqual(bootstrap.read_object(self.new / "data/thinking.json")["current"], "daily")
        self.assertTrue((self.new / "data/moods/reimu.json").exists())
        self.assertTrue((self.new / "data/persona_moods.json").exists())
        PersonaManager(self.new)
        self.assertIn("MY_PRIVATE_PERSONA", (self.new / "persona/characters/hiyori/SOUL.md").read_text())
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_used_destination_is_rejected_before_overwrite(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        path = self.new / "data/secrets.json"
        path.write_text('{"deepseek_api_key":"DIFFERENT_SYNTHETIC_KEY"}', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "已经有"):
            WIZ.inspect_migration(self.old, self.new)
        self.assertEqual(path.read_bytes(), before)

    def test_migration_snapshots_sqlite_wal_and_checks_integrity(self):
        self.populate_old()
        db = sqlite3.connect(self.old / "data/companion.sqlite3")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE tasks (title TEXT)")
            db.execute("INSERT INTO tasks VALUES ('synthetic reminder')")
            db.commit()
            bootstrap.initialize(self.new)
            rows, _ = WIZ.inspect_migration(self.old, self.new)
            MIG.apply(rows, self.new)
            new = sqlite3.connect(self.new / "data/companion.sqlite3")
            try:
                self.assertEqual(new.execute("SELECT title FROM tasks").fetchone()[0], "synthetic reminder")
                self.assertEqual(new.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                new.close()
        finally:
            db.close()

    def test_failed_copy_rolls_back_only_this_transactions_changes(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        rows, _ = WIZ.inspect_migration(self.old, self.new)
        before = {r["dst"]: r["dst"].read_bytes() for r in rows if r["dst"].exists()}
        replace = os.replace
        calls = []
        def interrupt(source, destination):
            calls.append(destination)
            if len(calls) == 3:
                raise OSError("synthetic disk failure")
            return replace(source, destination)
        with patch.object(MIG.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "已回退"):
                MIG.apply(rows, self.new)
        for row in rows:
            path = row["dst"]
            if path in before:
                self.assertEqual(path.read_bytes(), before[path])
            else:
                self.assertFalse(path.exists())

    def test_unusable_pip_is_repaired_before_install(self):
        interpreter = self.new / ".venv/Scripts/python.exe"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        commands = []
        def fake_run(command, *args, **kwargs):
            commands.append(command)
            return 1 if len(commands) in (1, 2) else 0
        with patch.object(FIRST, "platform_error", return_value=""), \
             patch.object(FIRST, "environment_python", return_value=interpreter), \
             patch.object(FIRST.shutil, "which", return_value=None), \
             patch.object(FIRST, "_run", side_effect=fake_run):
            FIRST.prepare_environment(self.new)
        self.assertIn("ensurepip", commands[2])
        self.assertIn("install", commands[3])

    def test_ready_runtime_or_venv_does_not_reinstall(self):
        for rel in ("runtime/python.exe", ".venv/Scripts/python.exe"):
            interpreter = self.new / rel
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.touch()
            with patch.object(FIRST, "platform_error", return_value=""), \
                 patch.object(FIRST, "environment_python", return_value=interpreter), \
                 patch.object(FIRST, "_run", return_value=0) as run:
                self.assertEqual(FIRST.prepare_environment(self.new), interpreter)
                self.assertEqual(run.call_count, 1)

    def test_fresh_default_max_completes_a_synthetic_reply(self):
        import brain as B
        import memory as M
        import thinking as T
        bootstrap.initialize(self.new)
        secrets = self.new / "data/secrets.json"
        secrets.write_text('{"deepseek_api_key":"SYNTHETIC_KEY"}', encoding="utf-8")
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"hello from fixture"}}]}'
                yield b'data: [DONE]'
        config = bootstrap.load_config(self.new)
        with patch.object(M, "ROOT", self.new), patch.object(M, "CFG", config), \
             patch.object(M, "P", config["paths"]), patch.object(B, "SECRETS", secrets), \
             patch.object(T, "THINKING_FILE", self.new / "data/thinking.json"), \
             patch.object(T, "_cache", {"mtime": 0, "data": None}), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(B, "_request", return_value=Response()) as request:
            events = list(B.stream("你好"))
        self.assertIn(("content", "hello from fixture"), events)
        payload = request.call_args.args[0]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["max_tokens"], 80000)
        self.assertEqual(payload["thinking"], {"type": "enabled"})

    def test_custom_storage_path_is_not_silently_left_in_old_install(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        path = self.old / "data/config.json"
        cfg = bootstrap.read_object(path)
        cfg["paths"]["journal"] = str(self.old / "custom-journal.jsonl")
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "自定义记忆"):
            WIZ.inspect_migration(self.old, self.new)
        self.assertEqual(bootstrap.read_object(self.new / "data/secrets.json")["deepseek_api_key"], "")

    def test_disabled_custom_live2d_does_not_block_migration(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        path = self.old / "data/config.json"
        config = bootstrap.read_object(path)
        config["live2d"].update(enabled=False, model="custom/missing.model3.json")
        path.write_text(json.dumps(config), encoding="utf-8")
        original = path.read_bytes()
        rows, notes = WIZ.inspect_migration(self.old, self.new)
        self.assertTrue(any("Live2D 已关闭" in note for note in notes))
        MIG.apply(rows, self.new)
        self.assertEqual((self.new / "data/config.json").read_bytes(), original)
        config["live2d"]["enabled"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        clean = self.install("another-clean")
        bootstrap.initialize(clean)
        with self.assertRaisesRegex(ValueError, "自定义 Live2D"):
            WIZ.inspect_migration(self.old, clean)

    def test_bad_sqlite_has_actionable_error_before_target_changes(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        (self.old / "data/companion.sqlite3").write_bytes(b"SYNTHETIC_NOT_A_DATABASE")
        rows, _ = WIZ.inspect_migration(self.old, self.new)
        before = {p: p.read_bytes() for p in self.new.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValueError, "陪伴数据库.*迁移未完成"):
            MIG.apply(rows, self.new)
        self.assertEqual(before, {p: p.read_bytes() for p in self.new.rglob("*") if p.is_file()})

    def test_busy_sqlite_migration_stops_without_target_changes(self):
        self.populate_old()
        bootstrap.initialize(self.new)
        writer = sqlite3.connect(self.old / "data/companion.sqlite3")
        try:
            writer.execute("CREATE TABLE marker (text)")
            writer.commit()
            writer.execute("BEGIN EXCLUSIVE")
            rows, _ = WIZ.inspect_migration(self.old, self.new)
            before = {p: p.read_bytes() for p in self.new.rglob("*") if p.is_file()}
            with patch.object(MIG, "SQLITE_BUSY_SECONDS", 0.05):
                with self.assertRaisesRegex(ValueError, "数据库仍被占用"):
                    MIG.apply(rows, self.new)
            self.assertEqual(before, {p: p.read_bytes() for p in self.new.rglob("*") if p.is_file()})
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
