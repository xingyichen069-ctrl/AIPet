import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import code_tasks as CT
import local_tools as LT
import qq_bot as QB


class CodeTaskTests(unittest.TestCase):
    def test_run_python_is_bound_to_task_root_and_reports_file(self):
        with tempfile.TemporaryDirectory() as d:
            with LT.bind_context(task_id="ct-test", fs_root=d):
                out = LT.run_python(
                    "from pathlib import Path\n"
                    "Path('result.txt').write_text('ok', encoding='utf-8')\n"
                    "print('ran')"
                )
            self.assertIn("退出码：0", out)
            self.assertIn("result.txt", out)
            self.assertEqual(Path(d, "result.txt").read_text(encoding="utf-8"), "ok")

    def test_run_python_refuses_without_task_context(self):
        self.assertIn("只能由后台代码任务调用", LT.run_python("print(1)"))

    def test_manager_runs_in_background_and_returns_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "tasks"
            with patch.object(CT, "TASKS_ROOT", root), \
                    patch.object(CT, "INDEX_FILE", root / "index.json"):
                def fake_ask(manager, task, instruction):
                    task.root.mkdir(parents=True, exist_ok=True)
                    (task.root / "answer.txt").write_text(instruction, encoding="utf-8")
                    return "已完成\nTASK_STATUS: DONE", {}

                event = QB.QQEvent(kind="c2c", scene="c2c", user_openid="U1")
                ctx = {
                    "is_owner": True,
                    "event": event,
                    "actor_id": "U1",
                    "actor_name": "主人",
                    "conversation_key": "c2c:U1",
                }
                with patch.object(CT.TaskManager, "_ask", fake_ask), \
                        patch.object(QB, "send_active", return_value={"id": "m"}) as send, \
                        patch.object(QB, "send_media", return_value={"id": "f"}) as media:
                    manager = CT.TaskManager()
                    ack = manager.submit(ctx, "写结果")
                    self.assertIn("ct-", ack)
                    for _ in range(100):
                        task = manager.latest_for(ctx)
                        if task and not task.worker_started:
                            break
                        time.sleep(0.01)
                    task = manager.latest_for(ctx)
                    self.assertIsNotNone(task)
                    self.assertEqual(task.state, "completed")
                    self.assertEqual(task.artifacts, ["answer.txt"])
                    self.assertEqual(
                        (task.root / "answer.txt").read_text(encoding="utf-8"), "写结果"
                    )
                    self.assertGreaterEqual(send.call_count, 2)
                    self.assertEqual(media.call_count, 1)

    def test_upload_file_uses_prepare_parts_and_rich_media(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "out.txt"
            source.write_text("abc", encoding="utf-8")
            calls = []

            def fake_api(method, path, body=None, timeout=12):
                calls.append((method, path, body))
                if path.endswith("upload_prepare"):
                    return {
                        "upload_id": "u1",
                        "block_size": 2,
                        "parts": [
                            {"part_index": 1, "upload_url": "https://put/1"},
                            {"part_index": 2, "upload_url": "https://put/2"},
                        ],
                    }
                if path.endswith("upload_part_finish"):
                    return {}
                return {"file_info": "fi"}

            event = QB.QQEvent(scene="c2c", user_openid="U1")
            with patch.object(QB, "_api", side_effect=fake_api), \
                    patch.object(QB, "_raw_put", return_value={"ok": True}) as put:
                result = QB.send_media(event, source)
            self.assertEqual(result["file_info"], "fi")
            self.assertEqual(put.call_count, 2)
            self.assertEqual(calls[-1][1], "/v2/users/U1/messages")
            self.assertEqual(calls[-1][2], {
                "msg_type": 7, "media": {"file_info": "fi"}
            })


if __name__ == "__main__":
    unittest.main()
