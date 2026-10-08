"""Register QQ's native group command panel without starting a gateway.

The manifest uses familiar slash-command notation. QQ may remove the leading
slash in API records. This is an explicit administration command, never part
of gateway startup or model tools.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import atomic_store as STORE

MARKER = "aipet:group-command-panel:v1"
PANEL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class PanelError(RuntimeError):
    pass


def load_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("scope") != "group" or value.get("target_type") != "all":
        raise PanelError("配置必须是 group 场景的 all 面板")
    panel = value.get("panel", {})
    if panel.get("remark") != MARKER:
        raise PanelError("不要更改面板标记，否则无法识别已有配置")
    items = panel.get("items", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise PanelError("面板需要 1 至 20 项")
    names = set()
    for item in items:
        name, desc = item.get("name", ""), item.get("desc", "")
        # QQ describes these as 14/30 characters, about 7/15 Chinese glyphs.
        # Conservatively count non-ASCII characters as two units.
        width = lambda text: sum(1 if ord(c) < 128 else 2 for c in text)
        if (item.get("type") != "command" or item.get("only_admin") is not False
                or not name.startswith("/") or name != name.strip() or name in names
                or any(ord(c) < 32 for c in name + desc)
                or not desc.strip() or width(name) > 14 or width(desc) > 30
                or set(item) != {"type", "name", "desc", "only_admin"}):
            raise PanelError("指令名称、长度、描述或权限配置不符合要求")
        names.add(name)
    return {"scope": "group", "target_type": "all",
            "panel": {"items": items, "remark": MARKER}}


def request(api, method: str, path: str, body=None) -> dict:
    try:
        result = api(method, path, body, timeout=15)
    except Exception:
        raise PanelError("QQ 接口连接失败；未自动重试，写入结果可能尚未确定") from None
    if not isinstance(result, dict):
        raise PanelError("QQ 接口返回了无法识别的内容")
    if result.get("_http_error") or result.get("code") not in (None, 0) or "_raw" in result:
        # Do not log complete provider responses, credentials or destination IDs.
        code = result.get("code", result.get("_http_error", "unknown"))
        raise PanelError(f"QQ 指令面板接口拒绝请求，错误码 {code}")
    return result


def list_panels(api) -> list[dict]:
    records, cursors = [], set()
    cursor = ""
    for _ in range(5):
        query = {"scope": "group", "limit": 50}
        if cursor:
            query["cursor"] = cursor
        result = request(api, "GET", "/v2/panels?" + urlencode(query))
        page = result.get("records", [])
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise PanelError("QQ 面板列表格式异常，未写入")
        records.extend(page)
        cursor = result.get("next_cursor", "")
        if result.get("is_end") is True and cursor:
            raise PanelError("QQ 面板分页标记冲突，未写入")
        if result.get("is_end") is True or ("records" in result and not cursor
                                            and result.get("is_end") is not False):
            return records
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise PanelError("QQ 面板分页不完整，未写入")
        cursors.add(cursor)
    raise PanelError("QQ 面板分页超过上限，未写入")


def own_record(record: dict) -> bool:
    return (record.get("scope") == "group" and record.get("target_type") == "all"
            and isinstance(record.get("panel"), dict)
            and record["panel"].get("remark") == MARKER)


def panel_content(record: dict) -> dict:
    panel = record.get("panel", {})
    return {"remark": panel.get("remark"), "items": [
        {"type": item.get("type"), "name": (item.get("name", "").removeprefix("/")
                                          if item.get("type") == "command" else item.get("name")),
         "desc": item.get("desc"),
         "only_admin": item.get("only_admin", False)} for item in panel.get("items", [])]}


def apply_panel(api, manifest: dict, data_dir: Path) -> dict:
    """Update only our marked panel; save intent before any ambiguous write."""
    with STORE.state_lock(data_dir):
        state_path = data_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        records = list_panels(api)
        owned = [r for r in records if r.get("panel", {}).get("remark") == MARKER]
        if len(owned) > 1:
            raise PanelError("存在多个同标记面板，请先核对；未覆盖或删除")
        current = owned[0] if owned else None
        if any(r.get("target_type") == "all" and r not in owned for r in records):
            raise PanelError("已有其他全局群面板，请先核对；未覆盖或另建")
        if current is None and state.get("panel_id"):
            panel_id = state["panel_id"]
            if not isinstance(panel_id, str) or not PANEL_ID.fullmatch(panel_id):
                raise PanelError("本地面板编号无效")
            current = request(api, "GET", "/v2/panels/" + panel_id)
        if current is not None:
            panel_id = current.get("panel_id", "")
            if not own_record(current) or not PANEL_ID.fullmatch(panel_id):
                raise PanelError("面板身份或场景不匹配，未修改")
            # List responses can be abbreviated; verify the complete record first.
            current = request(api, "GET", "/v2/panels/" + panel_id)
            if not own_record(current) or current.get("panel_id") != panel_id:
                raise PanelError("面板详情不匹配，未修改")
            if panel_content(current) == panel_content(manifest):
                STORE.write_json(state_path, {"panel_id": panel_id, "status": "verified"})
                return {"action": "unchanged", "panel_id": panel_id,
                        "items": len(manifest["panel"]["items"]), "verified": True}
        elif state.get("status") in {"pending", "unknown"}:
            raise PanelError("上次创建结果尚未确定；先查询平台，禁止重复创建")

        operation = "updated" if current else "created"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        STORE.write_json(data_dir / "backups" / (stamp + ".json"),
                         {"before": current, "requested": manifest})
        intent = {"status": "pending", "operation": operation,
                  "panel_id": current["panel_id"] if current else ""}
        STORE.write_json(state_path, intent)
        try:
            if current:
                panel_id = current["panel_id"]
                request(api, "PUT", "/v2/panels/" + panel_id, {"panel": manifest["panel"]})
            else:
                result = request(api, "POST", "/v2/panels", manifest)
                panel_id = result.get("panel_id", "")
                if not isinstance(panel_id, str) or not PANEL_ID.fullmatch(panel_id):
                    raise PanelError("创建响应缺少面板编号，请核对平台结果后再操作")
            intent["panel_id"] = panel_id
            STORE.write_json(state_path, intent)
            actual = request(api, "GET", "/v2/panels/" + panel_id)
            if (actual.get("panel_id") != panel_id or not own_record(actual)
                    or panel_content(actual) != panel_content(manifest)):
                raise PanelError("平台回读配置尚未吻合，未重复写入")
        except Exception:
            intent["status"] = "unknown"
            STORE.write_json(state_path, intent)
            raise
        STORE.write_json(state_path, {"panel_id": panel_id, "status": "verified"})
        return {"action": operation, "panel_id": panel_id,
                "items": len(manifest["panel"]["items"]), "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "list", "apply"), default="preview", nargs="?")
    args = parser.parse_args()
    try:
        manifest = load_manifest(ROOT / "templates/qq_group_panel.json")
        if args.action == "preview":
            result = manifest
        else:
            import qq_bot
            if args.action == "list":
                result = list_panels(qq_bot._api)
            else:
                result = apply_panel(qq_bot._api, manifest, ROOT / "data/qq_panel_admin")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PanelError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
