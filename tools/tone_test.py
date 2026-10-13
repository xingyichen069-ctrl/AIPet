"""Preview tone probes; --live runs against a temporary, private installation."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from _workspace import scratch

PROBES = [
    ("他考砸了", "今天考试考砸了，感觉白复习了"),
    ("他凌晨三点还在", "三点了我还没睡"),
    ("他情绪低但没说原因", "算了，没什么好说的"),
    ("他做错了一件小事", "对不起，我把你昨天说的那个文档删了"),
    ("他问一个很丧的问题", "你说人活着到底图什么"),
    ("他半夜说了句丧气话", "感觉自己挺没用的"),
    ("他很久没出现", "我回来了"),
]


def prepare_fixture(source, destination):
    """Copy configuration/persona only; never copy journals or conversation DBs."""
    source, destination = Path(source), Path(destination)
    for name in ("src", "persona_defaults", "persona"):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name,
                            ignore=shutil.ignore_patterns("__pycache__", "backups"))
    data = destination / "data"
    data.mkdir()
    for name in ("config", "thinking", "secrets"):
        path = source / "data" / (name + ".json")
        if not path.is_file():
            path = path.with_name(name + ".example.json")
        values = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        if name == "config":
            values["paths"] = {"journal": "memory/journal.jsonl", "state": "memory/state.json",
                               "profile": "persona/PROFILE.md", "summaries": "memory/summaries",
                               "archive": "memory/archive", "view": "view/memory.html"}
            values.setdefault("tools", {})["fs_root"] = str(destination / "sandbox")
        target = data / (name + ".json")
        target.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        os.chmod(target, 0o600)
    binding = source / "data/persona_moods.json"
    if binding.is_file():
        shutil.copyfile(binding, data / binding.name)
    script = destination / "tools/tone_test.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__), script)
    shutil.copyfile(source / "tools/_workspace.py", destination / "tools/_workspace.py")
    (destination / ".tone-demo-fixture").write_text("isolated", encoding="ascii")
    return script


def dialogue_connection():
    import brain as B
    return B.PC.resolve(B.load_secrets(), B.T.resolve(level="daily")["params"])


def run_fixture(mood=None):
    if not (ROOT / ".tone-demo-fixture").is_file():
        raise ValueError("语气实验只能在临时副本中运行。")
    sys.path.insert(0, str(ROOT / "src"))
    import brain as B
    import mood as MD
    import provider_config as PC
    connection = dialogue_connection()
    if not connection.key:
        print("当前对话方案尚未配置 API key。")
        return 2
    if mood:
        result = MD.set_mood(mood, 1, "语气实验", force=True)
        if isinstance(result, dict) and result.get("error"):
            print(result["error"])
            return 2
    system = B.persona_text() + "\n\n" + MD.block()
    rows, failed = [], False
    for label, query in PROBES:
        try:
            with PC.bind(connection):
                answer, _, info = B.ask_with_system(query, system, history=[], level="daily",
                                                   allowed_tools=set(), max_tokens=1200)
            error = str(info.get("error", ""))
        except Exception:
            answer, error = "", "模型请求失败，请检查接口配置。"
        failed |= bool(error) or not bool(answer.strip())
        rows.append({"case": label, "query": query, "answer": answer, "error": error})
        print(f"【{label}】{answer or error or '未返回正文'}", flush=True)
    (ROOT / "tone-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description="预览语气探针；加 --live 才调用模型，使用临时人格副本，不读取聊天记录。")
    parser.add_argument("mood", nargs="?", help="临时副本使用的心情名称")
    parser.add_argument("--live", action="store_true", help="真实调用 API，可能产生费用")
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.fixture:
        return run_fixture(options.mood)
    if not options.live:
        for label, query in PROBES:
            print(f"{label}：{query}")
        print("以上为预览；加 --live 才使用当前接口和人格副本实测。")
        return 0
    output = scratch(ROOT, "experiments_开发实验", "tone-demo")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=output) as folder:
        fixture = Path(folder)
        script = prepare_fixture(ROOT, fixture)
        command = [sys.executable, "-X", "utf8", str(script), "--fixture"]
        if options.mood:
            command += [options.mood]
        result = subprocess.run(command, cwd=fixture, env=dict(os.environ, AIPET_HOME=str(fixture)))
        if (fixture / "tone-results.json").is_file():
            target = output / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
            shutil.copyfile(fixture / "tone-results.json", target)
            print("实验结果：" + str(target))
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
