import os
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import update_lifecycle as U


class UpdateLifecycleTests(unittest.TestCase):
    def test_running_update_request_blocks_new_services(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            U.write_json(U.request_path(root), {"pid": os.getpid()})
            self.assertTrue(U.pending(root))
            with self.assertRaises(RuntimeError):
                U.ensure_available(root)

    def test_registered_process_is_visible_and_removed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with U.registered(root, "task"):
                self.assertEqual(U.active_processes(root).get(os.getpid()), "task")
            self.assertFalse((root / "data" / "processes" / f"{os.getpid()}.json").exists())


if __name__ == "__main__":
    unittest.main()
