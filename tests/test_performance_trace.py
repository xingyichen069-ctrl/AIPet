"""Recording integrity, replay determinism and bounded untrusted file input."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import performance_trace as T
from performance_profile import load_profile
from performance_scenarios import SCENARIOS, scenario


class Traces(unittest.TestCase):
    def test_all_scenarios_match_after_file_roundtrip_and_variable_ticks(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in SCENARIOS:
                with self.subTest(name=name):
                    data = scenario(name)
                    result = T.Recording(load_profile("simulator"))
                    for row in data["events"]:
                        for at in range(result.engine.now, row["at_ms"], 73):
                            result.advance(at)
                        result.dispatch(row["event"], row["at_ms"])
                    for at in range(result.engine.now, data["end_ms"], 91):
                        result.advance(at)
                    result.advance(data["end_ms"])
                    self.assertEqual(result.export(), data)
                    path = Path(folder) / (name + ".json")
                    result.save(path)
                    self.assertEqual(T.replay(T.read_trace(path)).export(), data)

    def test_seek_recomputes_without_future_events_and_does_not_modify_source(self):
        data = scenario("interrupt")
        original = deepcopy(data)
        expected = [(0, "mood", None), (1500, "action", "drink_tea"),
                    (2200, "drag", None), (3400, "activity", None), (7200, "mood", None)]
        for at, layer, action in expected:
            state = T.replay(data, at).engine.snapshot()
            self.assertEqual(state["at_ms"], at)
            self.assertEqual(state["layer"], layer)
            self.assertEqual(state["action"]["name"] if state["action"] else None, action)
        self.assertEqual(data, original)
        for at in (-1, True, 7201):
            with self.assertRaises(ValueError):
                T.replay(data, at)

    def test_expected_state_digest_and_numeric_types_are_checked(self):
        original = scenario("reply")
        for mutate in (lambda d: d["expected"]["state"].update(mood="sad"),
                       lambda d: d["expected"].update(audit_sha256="0" * 64),
                       lambda d: d["expected"]["state"].update(turn_id=True)):
            data = deepcopy(original)
            mutate(data)
            with self.assertRaises(ValueError):
                T.replay(data)
        del original["expected"]
        self.assertEqual(T.replay(original).engine.mood, "happy")

    def test_digest_includes_dropped_history_and_detects_same_final_state_tampering(self):
        recording = T.Recording(load_profile("simulator"))
        for at in range(600):
            recording.dispatch({"kind": "mood.set", "mood": "happy" if at % 2 == 0 else "calm"}, at)
        self.assertGreater(recording.engine.history_dropped, 0)
        data = recording.export()
        self.assertEqual(T.replay(data).engine.audit_sha256, recording.engine.audit_sha256)
        data["events"][0]["event"]["mood"] = "sad"
        with self.assertRaisesRegex(ValueError, "摘要"):
            T.replay(data)
        del data["expected"]
        result = T.replay(data)
        self.assertEqual(result.engine.snapshot(), recording.engine.snapshot())
        self.assertEqual(list(result.engine.history), list(recording.engine.history))
        self.assertNotEqual(result.engine.audit_sha256, recording.engine.audit_sha256)

    def test_invalid_versions_order_fields_and_numbers_are_rejected(self):
        original = scenario("reply")
        def huge_number(data):
            data["initial_profile"]["parameters"]["ParamAngleZ"]["default"] = 10**1000
        mutations = [lambda d: d.update(trace_version=True), lambda d: d.update(controller_version=2),
                     lambda d: d.update(end_ms=-1), lambda d: d.update(end_ms=T.MAX_TIME_MS + 1),
                     lambda d: d["events"][2].update(at_ms=0),
                     lambda d: d["events"][0]["event"].update(text="must not record chat"),
                     lambda d: d["events"][0]["event"].update(mood=[]),
                     lambda d: d["initial_profile"].update(renderer=[]), huge_number,
                     lambda d: d["expected"].update(audit_sha256="not a digest")]
        for mutate in mutations:
            data = deepcopy(original)
            mutate(data)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                T.validate_trace(data)

    def test_capacity_failure_preserves_complete_old_record_and_clock(self):
        recording = T.Recording(load_profile("simulator"))
        event = {"kind": "mood.set", "mood": "happy"}
        recording.dispatch(event, 0)
        before = recording.export()
        for limit, value in (("MAX_EVENTS", 1), ("MAX_INPUT_BYTES", recording._bytes)):
            with patch.object(T, limit, value), self.assertRaises(ValueError):
                recording.dispatch(event, 100)
            self.assertEqual(recording.export(), before)
        with self.assertRaises(ValueError):
            recording.advance(T.MAX_TIME_MS + 1)
        self.assertEqual(recording.export(), before)
        with patch.object(T, "MAX_INPUT_BYTES", recording._bytes - 1), self.assertRaises(ValueError):
            T.validate_trace(before)

    def test_untrusted_file_size_encoding_duplicates_and_nonfinite_numbers(self):
        payloads = [b'\xff', b'{"trace_version":1,"trace_version":1}', b'{"value":NaN}',
                    b'[' * 3000 + b']' * 3000, b' ' * (T.MAX_BYTES + 1)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            for payload in payloads:
                path.write_bytes(payload)
                with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                    T.read_trace(path)

    def test_save_failure_keeps_existing_file_and_removes_only_its_temp(self):
        result = T.replay(scenario("reply"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recording.json"
            path.write_bytes(b"existing file")
            with patch("os.replace", side_effect=OSError("injected replace failure")), self.assertRaises(OSError):
                result.save(path)
            self.assertEqual(path.read_bytes(), b"existing file")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_export_reserves_the_trailing_newline_within_file_limit(self):
        result = T.replay(scenario("reply"))
        length = len(T._encoded(result.export()))
        with patch.object(T, "MAX_BYTES", length), self.assertRaises(ValueError):
            result.export()
        with patch.object(T, "MAX_BYTES", length + 1):
            result.export()

    def test_headless_cli_runs_without_site_packages_and_reports_failure(self):
        tool = ROOT / "tools/performance_lab.py"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            for name, code in (("late_reply", 0), ("invalid", 1)):
                completed = subprocess.run([sys.executable, "-S", "-B", "-X", "utf8", str(tool),
                                            "--scenario", name, "--report", str(output)],
                                           capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(completed.returncode, code, completed.stderr)
                report = json.loads(completed.stdout)
                self.assertEqual(report["ok"], code == 0)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
