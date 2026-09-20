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
from .editor import GuidedEditor, client_from_environment, default_target, summarize


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _configure_stdio() -> None:
    # Windows consoles may still expose a legacy code page.  Plugin replies
    # are UTF-8 data, so replace only an unprintable console character instead
    # of turning an otherwise successful edit into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


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
    guide = sub.add_parser("guide", help="用 OpenAI 兼容模型对话生成插件草案")
    guide.add_argument("request", nargs="?", help="本轮插件需求；省略时从标准输入读取")
    guide.add_argument("--base-url", help="模型 API base URL")
    guide.add_argument("--model", help="模型名称，默认 qwen")
    guide.add_argument("--target", type=Path, help="草案目录；默认写入 work/harness-guided")
    guide.add_argument("--apply", action="store_true", help="测试和健康检查通过后自动启用")
    guide.add_argument("--interactive", action="store_true", help="持续读取多轮对话")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "list":
            rows = []
            registry = root / "data" / "harness" / "registry.json"
            active = {}
            if registry.exists():
                active = json.loads(registry.read_text(encoding="utf-8")).get("active", {})
            seen = set()
            for spec in discover_plugins(root / "plugins"):
                current = active.get(spec.name, {})
                rows.append({"name": spec.name, "version": spec.version, "display_name": spec.display_name,
                             "active": current.get("version") == spec.version})
                seen.add(spec.name)
            # Generated or externally installed plugins do not have to remain
            # in the source tree.  Keep them visible in status output through
            # the registry, while leaving their actual source under data/.
            for name, current in sorted(active.items()):
                if name in seen or not isinstance(current, dict):
                    continue
                rows.append({"name": name, "version": current.get("version", ""),
                             "display_name": name, "active": bool(current.get("enabled")),
                             "installed": True})
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
        if args.command == "guide":
            client = client_from_environment(base_url=args.base_url, model=args.model)
            editor = GuidedEditor(client)
            if args.interactive:
                return _interactive_guide(editor, root, args)
            request = args.request or sys.stdin.read().strip()
            if not request:
                raise HarnessError("guide 需要插件需求，或使用 --interactive")
            result = editor.turn(request)
            output: dict = summarize(result)
            if result.ready:
                target = args.target or default_target(root, result.plugin["name"])
                if args.apply:
                    applied = editor.apply(result, target, root)
                    output["source"] = applied["source"]
                    output["installation"] = applied["installation"]
                else:
                    output["source"] = str(editor.materialize(result, target))
            _json(output)
            return 0
    except HarnessError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"harness: 操作失败（{type(exc).__name__}）", file=sys.stderr)
        return 2
    return 2


def _interactive_guide(editor: GuidedEditor, root: Path, args) -> int:
    """Simple stdin conversation; /apply activates the latest validated draft."""
    latest = None
    print("AIPet 插件编辑对话已开始；输入 /apply 启用，/quit 退出。", file=sys.stderr)
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not line:
            continue
        if line.lower() in {"/quit", "/exit"}:
            return 0
        if line.lower() == "/apply":
            if latest is None or not latest.ready:
                print("当前没有可启用的完整插件草案。", file=sys.stderr)
                continue
            target = args.target or default_target(root, latest.plugin["name"])
            applied = editor.apply(latest, target, root)
            _json({"source": applied["source"], "installation": applied["installation"]})
            return 0
        latest = editor.turn(line)
        _json(summarize(latest))
        if latest.reply:
            print(f"AIPet> {latest.reply}", file=sys.stderr)
        if latest.ready and args.apply:
            target = args.target or default_target(root, latest.plugin["name"])
            applied = editor.apply(latest, target, root)
            _json({"source": applied["source"], "installation": applied["installation"]})
            return 0
