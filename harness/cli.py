"""CLI for the AIPet plugin harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .core import (
    HarnessError,
    auto_use,
    discover_plugins,
    install_archive,
    package_plugin,
    run_plugin,
    run_plugin_test,
    validate_plugin,
)


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIPet 独立插件 harness")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="AIPet 根目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出可发现插件")
    for name in ("validate", "test", "package", "auto"):
        command = sub.add_parser(name)
        command.add_argument("plugin", type=Path, help="插件目录")
        if name == "package":
            command.add_argument("--output", type=Path)
        if name == "auto":
            command.add_argument("--output", type=Path)
    install = sub.add_parser("install", help="安装已打包插件")
    install.add_argument("archive", type=Path)
    run = sub.add_parser("run", help="运行已启用插件")
    run.add_argument("name")
    run.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "list":
            rows = []
            registry = root / "data" / "harness" / "registry.json"
            active = {}
            if registry.exists():
                active = json.loads(registry.read_text(encoding="utf-8")).get("active", {})
            for spec in discover_plugins(root / "plugins"):
                current = active.get(spec.name, {})
                rows.append({"name": spec.name, "version": spec.version, "display_name": spec.display_name,
                             "active": current.get("version") == spec.version})
            _json(rows)
            return 0
        if args.command == "validate":
            spec = validate_plugin(args.plugin)
            _json({"valid": True, "name": spec.name, "version": spec.version, "files": list(spec.files)})
            return 0
        if args.command == "test":
            spec = validate_plugin(args.plugin)
            result = run_plugin_test(spec, root)
            _json({"status": "passed", "name": spec.name, "version": spec.version,
                   "stdout": result["stdout"]})
            return 0
        if args.command == "package":
            spec = validate_plugin(args.plugin)
            archive = package_plugin(spec, args.output or root / "work" / "harness-packages")
            _json({"status": "packaged", "archive": str(archive)})
            return 0
        if args.command == "install":
            _json(install_archive(args.archive, root))
            return 0
        if args.command == "auto":
            _json(auto_use(args.plugin, root, args.output))
            return 0
        if args.command == "run":
            return run_plugin(root, args.name, args.args)
    except HarnessError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"harness: 操作失败（{type(exc).__name__}）", file=sys.stderr)
        return 2
    return 2
