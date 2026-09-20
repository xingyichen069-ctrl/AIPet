from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
import zipfile

from harness.core import HarnessError, auto_use, discover_plugins, install_archive, package_plugin, validate_plugin


class HarnessTests(unittest.TestCase):
    def make_plugin(self, root: Path, *, name="fixture", version="1.0.0", healthy=True) -> Path:
        plugin = root / name
        (plugin / ".aipet-plugin").mkdir(parents=True, exist_ok=True)
        body = """\
import argparse, json
parser = argparse.ArgumentParser()
parser.add_argument('--selftest', action='store_true')
parser.add_argument('--health', action='store_true')
args = parser.parse_args()
if args.health and not HEALTHY:
    raise SystemExit(3)
print(json.dumps({'status': 'passed' if args.selftest else 'ok'}))
"""
        (plugin / "entry.py").write_text(body.replace("HEALTHY", repr(healthy)), encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "name": name,
            "version": version,
            "display_name": name,
            "entrypoint": "entry.py",
            "files": [".aipet-plugin/plugin.json", "entry.py"],
            "test_command": ["{python}", "-I", "-B", "{entrypoint}", "--selftest"],
            "health_command": ["{python}", "-I", "-B", "{entrypoint}", "--health"],
        }
        (plugin / ".aipet-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return plugin

    def test_validation_requires_exact_file_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            (plugin / "unlisted.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(HarnessError):
                validate_plugin(plugin)

    def test_validation_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as raw:
            plugin = self.make_plugin(Path(raw))
            data = json.loads((plugin / ".aipet-plugin/plugin.json").read_text())
            data["entrypoint"] = "../entry.py"
            (plugin / ".aipet-plugin/plugin.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(HarnessError):
                validate_plugin(plugin)

    def test_discovery_is_sorted_and_validated(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_plugin(root, name="zeta")
            self.make_plugin(root, name="alpha")
            self.assertEqual([spec.name for spec in discover_plugins(root)], ["alpha", "zeta"])

    def test_package_has_hash_manifest_and_round_trips(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            spec = validate_plugin(plugin)
            archive = package_plugin(spec, root / "out")
            with zipfile.ZipFile(archive) as zf:
                self.assertIn("fixture-1.0.0/release-manifest.json", zf.namelist())
            self.assertTrue(archive.is_file())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(digest, hashlib.sha256(package_plugin(spec, root / "out").read_bytes()).hexdigest())

    def test_package_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            archive = package_plugin(validate_plugin(plugin), root / "out")
            changed = root / "tampered.zip"
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(changed, "w") as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename.endswith("entry.py"):
                        data += b"\n"
                    target.writestr(info, data)
            with self.assertRaises(HarnessError):
                install_archive(changed, root / "app")

    def test_auto_use_activates_only_after_test_and_health(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            result = auto_use(plugin, root / "app")
            self.assertEqual(result["installation"]["status"], "activated")
            registry = json.loads((root / "app/data/harness/registry.json").read_text())
            self.assertEqual(registry["active"]["fixture"]["version"], "1.0.0")

    def test_failed_health_keeps_registry_without_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            good = self.make_plugin(root, version="1.0.0")
            auto_use(good, root / "app")
            bad = self.make_plugin(root, version="2.0.0", healthy=False)
            with self.assertRaises(HarnessError):
                auto_use(bad, root / "app")
            registry = json.loads((root / "app/data/harness/registry.json").read_text())
            self.assertEqual(registry["active"]["fixture"]["version"], "1.0.0")

    def test_same_version_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            app = root / "app"
            first = auto_use(plugin, app)
            second = auto_use(plugin, app)
            self.assertEqual(first["installation"]["status"], "activated")
            self.assertEqual(second["installation"]["status"], "already_active")

    def test_same_version_content_change_requires_version_bump(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            app = root / "app"
            auto_use(plugin, app)
            (plugin / "entry.py").write_text(
                (plugin / "entry.py").read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            )
            with self.assertRaises(HarnessError):
                auto_use(plugin, app)

    def test_cli_fixture_entrypoint_is_isolated(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = self.make_plugin(root)
            spec = validate_plugin(plugin)
            self.assertEqual(spec.test_command[0], "{python}")


if __name__ == "__main__":
    unittest.main()
