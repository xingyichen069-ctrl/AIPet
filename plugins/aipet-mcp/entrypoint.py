"""Standalone adapter that exposes AIPet's existing MCP server to the harness."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys


def _app_root() -> Path:
    value = os.environ.get("AIPET_ROOT", "").strip()
    if not value:
        raise RuntimeError("AIPET_ROOT 未配置")
    root = Path(value).resolve()
    if not (root / "src" / "mcp_server.py").is_file():
        raise RuntimeError("AIPET_ROOT 缺少 src/mcp_server.py")
    return root


def _load_server():
    root = _app_root()
    source = root / "src" / "mcp_server.py"
    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("aipet_harness_mcp_server", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 AIPet MCP server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def health() -> int:
    module = _load_server()
    names = [item.get("name") for item in module.TOOLS]
    if not names or len(names) != len(module.DISPATCH) or set(names) != set(module.DISPATCH):
        raise RuntimeError("MCP 工具清单与分发器不一致")
    print(json.dumps({"status": "ok", "plugin": "aipet-mcp", "tools": len(names)}, ensure_ascii=False))
    return 0


def selftest() -> int:
    module = _load_server()
    checks = []
    initialized = module.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    if not initialized or "result" not in initialized:
        raise RuntimeError("MCP initialize 失败")
    checks.append("initialize")
    listed = module.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = listed.get("result", {}).get("tools", []) if isinstance(listed, dict) else []
    if len(tools) != len(module.DISPATCH):
        raise RuntimeError("MCP tools/list 数量不一致")
    checks.append("tools/list")
    unknown = module.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "__unknown__"}})
    if not unknown or "error" not in unknown:
        raise RuntimeError("未知 MCP 工具没有拒绝")
    checks.append("unknown_tool_rejected")
    notification = module.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if notification is not None:
        raise RuntimeError("通知不应产生响应")
    checks.append("notification")
    print(json.dumps({"status": "passed", "plugin": "aipet-mcp", "checks": checks}, ensure_ascii=False))
    return 0


def serve() -> int:
    module = _load_server()
    module.serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIPet MCP 插件")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--health", action="store_true")
    group.add_argument("--selftest", action="store_true")
    group.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.health:
            return health()
        if args.selftest:
            return selftest()
        return serve()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
