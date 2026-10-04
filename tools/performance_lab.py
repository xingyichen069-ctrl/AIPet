"""Offline performance lab. --check TRACE and --scenario NAME need no Qt."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--check", type=Path, help="recompute and verify a saved JSON trace")
    source.add_argument("--scenario", help="verify a built-in scenario without a GUI")
    parser.add_argument("--report", type=Path, help="write the verification result as JSON")
    args = parser.parse_args()
    if args.check or args.scenario:
        from performance_trace import read_trace, replay
        from performance_scenarios import scenario
        try:
            data = read_trace(args.check) if args.check else scenario(args.scenario)
            result = replay(data)
            report = {"ok": True, "events": len(result.events), "end_ms": result.engine.now,
                      "audit_sha256": result.engine.audit_sha256,
                      "final_state": result.engine.snapshot(), "expected_verified": "expected" in data}
        except (OSError, ValueError, TypeError) as exc:
            report = {"ok": False, "error": str(exc)}
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        return 0 if report["ok"] else 1
    if args.report:
        parser.error("--report requires --check or --scenario")
    from performance_lab import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
