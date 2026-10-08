"""Native panel provisioning: preserve other panels and reconcile uncertain writes."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import qq_panel as P


class FakeQQ:
    def __init__(self):
        self.records = []
        self.calls = []
        self.lose_create_response = False
        self.hide_list = False
        self.wrong_readback = False

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, deepcopy(body)))
        if method == "GET" and path.startswith("/v2/panels?"):
            return {"records": [] if self.hide_list else deepcopy(self.records), "is_end": True}
        if method == "POST":
            record = deepcopy(body)
            record["panel_id"] = "p_created"
            self.records.append(record)
            if self.lose_create_response:
                raise TimeoutError("provider secret should not reach error message")
            return {"panel_id": "p_created"}
        record = next(r for r in self.records if path.endswith("/" + r["panel_id"]))
        if method == "PUT":
            record["panel"] = deepcopy(body["panel"])
            return {"version": 2}
        result = deepcopy(record)
        if self.wrong_readback:
            result["panel"]["items"] = []
        return result


class Panels(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.manifest = P.load_manifest(ROOT / "templates/qq_group_panel.json")
        self.api = FakeQQ()

    def tearDown(self):
        self.temp.cleanup()

    def apply(self):
        return P.apply_panel(self.api, self.manifest, self.data)

    def record(self, panel_id="p_existing", **kwargs):
        return {**deepcopy(self.manifest), "panel_id": panel_id, **kwargs}

    def writes(self):
        return [c for c in self.api.calls if c[0] != "GET"]

    def test_create_readback_then_reapply_is_idempotent(self):
        self.assertEqual(self.apply()["action"], "created")
        self.assertEqual(self.apply()["action"], "unchanged")
        self.assertEqual([c[0] for c in self.writes()], ["POST"])
        saved = json.loads((self.data / "state.json").read_text())
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(len(list((self.data / "backups").glob("*.json"))), 1)

    def test_update_only_own_panel_and_preserve_other_specific_panel(self):
        current = self.record()
        current["panel"]["items"][0]["desc"] = "旧说明"
        other = self.record("p_other", target_type="specific")
        other["panel"]["remark"] = "another application"
        other["group_openids"] = ["group_kept"]
        self.api.records = [current, deepcopy(other)]
        self.assertEqual(self.apply()["action"], "updated")
        self.assertEqual(self.writes()[0][:2], ("PUT", "/v2/panels/p_existing"))
        self.assertEqual(self.api.records[1], other)
        self.assertNotIn("group_openids", self.writes()[0][2])

    def test_unknown_global_panel_is_not_replaced(self):
        other = self.record()
        other["panel"]["remark"] = "not ours"
        self.api.records = [other]
        with self.assertRaises(P.PanelError):
            self.apply()
        self.assertEqual(self.writes(), [])

    def test_duplicate_marker_or_wrong_scope_prevents_write(self):
        for records in ([self.record(), self.record("p_two")],
                        [self.record(target_type="specific")]):
            self.api.records = records
            with self.assertRaises(P.PanelError):
                self.apply()
        self.assertEqual(self.writes(), [])

    def test_lost_create_response_is_reconciled_without_second_post(self):
        self.api.lose_create_response = True
        with self.assertRaises(P.PanelError) as caught:
            self.apply()
        self.assertNotIn("provider secret", str(caught.exception))
        self.assertEqual(self.apply()["action"], "unchanged")
        self.assertEqual(len(self.writes()), 1)

    def test_unknown_creation_hidden_from_list_is_not_retried(self):
        self.api.lose_create_response = True
        with self.assertRaises(P.PanelError):
            self.apply()
        self.api.hide_list = True
        with self.assertRaisesRegex(P.PanelError, "禁止重复创建"):
            self.apply()
        self.assertEqual(len(self.writes()), 1)

    def test_known_id_survives_eventually_consistent_listing(self):
        self.apply()
        self.api.hide_list = True
        self.assertEqual(self.apply()["action"], "unchanged")
        self.assertEqual(len(self.writes()), 1)

    def test_readback_mismatch_is_not_reported_as_success(self):
        self.api.wrong_readback = True
        with self.assertRaisesRegex(P.PanelError, "回读配置"):
            self.apply()
        self.assertEqual(json.loads((self.data / "state.json").read_text())["status"], "unknown")
        self.assertEqual(len(self.writes()), 1)

    def test_api_errors_and_incomplete_pages_prevent_writes(self):
        for response in ({"_http_error": 403, "code": 11253}, {},
                         {"records": [], "is_end": False},
                         {"records": [], "is_end": True, "next_cursor": "unexpected"},
                         {"is_end": False, "next_cursor": "same"}):
            calls = []
            def api(method, *args, **kwargs):
                calls.append(method)
                return response
            with self.assertRaises(P.PanelError):
                P.apply_panel(api, self.manifest, self.data)
            self.assertTrue(all(c == "GET" for c in calls))

    def test_empty_list_can_omit_records_as_live_api_does(self):
        self.assertEqual(P.list_panels(lambda *a, **k: {"is_end": True}), [])

    def test_qq_normalizes_leading_slash_and_omits_false_defaults(self):
        current = self.record()
        for item in current["panel"]["items"]:
            item["name"] = item["name"].removeprefix("/")
            item.pop("only_admin")
        self.api.records = [current]
        self.assertEqual(self.apply()["action"], "unchanged")
        self.assertEqual(self.writes(), [])

    def test_unsafe_or_oversized_manifest_is_rejected(self):
        for key, value in [("name", "/" + "汉" * 8), ("only_admin", True),
                           ("type", "link"), ("name", "/版本\n"), ("desc", "")]:
            manifest = deepcopy(self.manifest)
            manifest["panel"]["items"][0][key] = value
            path = self.data / "manifest.json"
            path.write_text(json.dumps(manifest))
            with self.assertRaises(P.PanelError):
                P.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
