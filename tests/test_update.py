import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import update as U


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


if __name__ == "__main__":
    unittest.main()
