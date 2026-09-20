import json
import hashlib
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import update as U
import portable_update as PU


class UpdateTests(unittest.TestCase):
    def test_archive_url_and_ref_validation(self):
        self.assertIn("/tags/v0.5.0.zip", U.archive_url("v0.5.0"))
        self.assertIn("/heads/all-round.zip", U.archive_url("all-round", "branch"))
        with self.assertRaises(ValueError):
            U.archive_url("../main", "branch")

    def test_install_archive_preserves_private_files_and_backups_public_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "AIPet"
            (target / "src").mkdir(parents=True)
            (target / "data").mkdir()
            (target / "src" / "app.py").write_text("old", encoding="utf-8")
            (target / "data" / "secrets.json").write_text("keep", encoding="utf-8")
            archive = root / "update.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("AIPet-main/VERSION", "0.6.0")
                zf.writestr("AIPet-main/src/app.py", "new")
                zf.writestr("AIPet-main/src/added.py", "added")
                zf.writestr("AIPet-main/data/secrets.json", "replace-me")

            result = U.install_archive(archive, target)
            self.assertTrue(result["ok"])
            self.assertEqual((target / "src" / "app.py").read_text(encoding="utf-8"), "new")
            self.assertEqual((target / "src" / "added.py").read_text(encoding="utf-8"), "added")
            self.assertEqual((target / "data" / "secrets.json").read_text(encoding="utf-8"), "keep")
            backup = Path(result["backup"])
            self.assertEqual((backup / "src" / "app.py").read_text(encoding="utf-8"), "old")
            self.assertEqual(json.loads(json.dumps(result))["version"], "0.6.0")

    def test_install_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "bad.zip"
            target = Path(d) / "AIPet"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("AIPet-main/../outside.txt", "bad")
            with self.assertRaises(ValueError):
                U.install_archive(archive, target)

    def test_update_installs_explicit_branch_without_git(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "AIPet"
            archive = root / "update.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("AIPet-all-round/VERSION", "0.5.1")
                zf.writestr("AIPet-all-round/src/new.py", "branch")
            with patch.object(U, "download_archive", return_value=(archive, "https://example/update.zip")):
                result = U.update("all-round", "branch", target)
            self.assertTrue(result["ok"])
            self.assertTrue(result["updated"])
            self.assertEqual(result["ref"], "all-round")
            self.assertEqual((target / "src" / "new.py").read_text(encoding="utf-8"), "branch")

    def _package_tree(self, root: Path, version: str = "0.6.0") -> dict:
        files = {
            "AIPet.exe": b"launcher",
            "VERSION": version.encode("utf-8"),
            "runtime/python.exe": b"python",
            "runtime/pythonw.exe": b"pythonw",
            "src/app_entry.py": b"print('entry')\n",
            "src/windows_launcher.py": b"print('launcher')\n",
            "src/portable_update.py": b"print('updater')\n",
            "src/update_lifecycle.py": b"print('lifecycle')\n",
            "src/app_paths.py": b"print('paths')\n",
            "data/config.example.json": b"{}\n",
            "data/thinking.json": b"{}\n",
            "themes/custom.qss": b"default\n",
        }
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = {
            "format": 1,
            "app": "AIPet",
            "version": version,
            "python": platform.python_version(),
            "architecture": "64-bit",
            "runtime_files_copied": 2,
            "files": [
                {"path": name, "size": len(content),
                 "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in sorted(files.items())
            ],
        }
        (root / PU.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_complete_package_is_verified_and_keeps_custom_files(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            staged = base / "staged"
            target = base / "AIPet"
            self._package_tree(staged)
            (target / "data").mkdir(parents=True)
            (target / "data" / "config.example.json").write_text("user-template", encoding="utf-8")
            (target / "themes").mkdir()
            (target / "themes" / "custom.qss").write_text("my-theme", encoding="utf-8")
            with patch.object(PU, "health_check") as health:
                result = PU.install_staged(staged, target)
            self.assertTrue(result["ok"])
            health.assert_any_call(staged, result and PU.read_manifest(staged))
            self.assertEqual((target / "AIPet.exe").read_bytes(), b"launcher")
            self.assertEqual((target / "data" / "config.example.json").read_text(encoding="utf-8"),
                             "user-template")
            self.assertEqual((target / "themes" / "custom.qss").read_text(encoding="utf-8"), "my-theme")
            self.assertTrue((target / PU.MANIFEST).is_file())
            self.assertIn("themes/custom.qss", result["skipped"])

    def test_package_install_rolls_back_after_post_copy_health_failure(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            staged = base / "staged"
            target = base / "AIPet"
            self._package_tree(staged)
            (target / "src").mkdir(parents=True)
            (target / "src" / "app_entry.py").write_text("old", encoding="utf-8")
            with patch.object(PU, "health_check", side_effect=[None, RuntimeError("bad runtime")]):
                with self.assertRaises(RuntimeError):
                    PU.install_staged(staged, target)
            self.assertEqual((target / "src" / "app_entry.py").read_text(encoding="utf-8"), "old")
            self.assertFalse((target / PU.MANIFEST).exists())
            journals = list((target / "backups").glob("package-update-*/transaction.json"))
            self.assertTrue(journals)
            self.assertEqual(json.loads(journals[0].read_text(encoding="utf-8"))["state"], "rolled_back")

    def test_package_manifest_rejects_private_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            manifest = self._package_tree(root)
            item = {"path": "data/secrets.json", "size": 1, "sha256": "0" * 64}
            manifest["files"].append(item)
            (root / PU.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                PU.read_manifest(root)

    def test_source_update_refuses_to_touch_portable_install(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "AIPet"
            target.mkdir()
            (target / PU.MANIFEST).write_text("{}", encoding="utf-8")
            result = U.update("all-round", "branch", target)
            self.assertFalse(result["ok"])
            self.assertIn("完整发布包", result["error"])

    def test_portable_worker_starts_in_isolated_python(self):
        worker = Path(U.__file__).with_name("portable_update.py")
        result = subprocess.run(
            [sys.executable, "-I", str(worker), "--help"],
            capture_output=True,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("AIPet", output)


if __name__ == "__main__":
    unittest.main()
