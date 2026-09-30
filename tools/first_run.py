"""Shared first-run, configuration and launch entry; installs into this project."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import bootstrap

DEPENDENCY_CHECK = """
import sys
import PySide6.QtWidgets, live2d.v3, OpenGL, socks, primp
from ddgs.ddgs import DDGS
from importlib.metadata import version
# The search routing adapter uses these fixed client APIs.
sys.exit(0 if version('ddgs') == '9.16.0' and version('primp') == '2.0.1' else 1)
"""


def platform_error() -> str:
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        return "当前 Python 版本不支持。请安装 Python 3.12 x64，或使用 uv 后重新准备环境。支持范围是 3.11–3.14。"
    if sys.platform == "win32":
        import sysconfig
        if sysconfig.get_platform() != "win-amd64":
            return "Windows 需要 x64 Python；32 位或原生 ARM64 Python 没有本项目所需的完整依赖。"
    elif sys.platform == "darwin":
        version = platform.mac_ver()[0].split(".")[0]
        if not version.isdigit() or int(version) < 15:
            return "当前固定的 Live2D 依赖需要 macOS 15 或以上；Mac 支持仍属实验性。"
    else:
        return "本安装入口目前支持 Windows x64，以及实验性的 macOS 15+。"
    return ""


def environment_python(root: Path) -> Path:
    if sys.platform == "win32":
        portable = root / "runtime/python.exe"
        return portable if portable.is_file() else root / ".venv/Scripts/python.exe"
    return root / ".venv/bin/python"


def _run(command: list[str], root: Path, **kwargs) -> int:
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PIP_CACHE_DIR=str(root / "work/pip-cache"),
               UV_CACHE_DIR=str(root / "work/uv-cache"),
               UV_PYTHON_INSTALL_DIR=str(root / ".python"))
    return subprocess.run(command, cwd=root, env=env, **kwargs).returncode


def prepare_environment(root: Path) -> Path:
    error = platform_error()
    if error:
        raise RuntimeError(error)
    interpreter = environment_python(root)
    if not interpreter.exists():
        if (root / ".venv").exists():
            raise RuntimeError(".venv 已存在但找不到可运行的 Python。请把 .venv 改名留作备份，"
                               "再运行准备环境；不要删除 data、persona 或 memory。")
        print("正在创建项目专用运行环境……", flush=True)
        if _run([sys.executable, "-m", "venv", str(root / ".venv")], root):
            raise RuntimeError("创建环境失败。请检查当前目录是否可写，以及 Python 是否完整安装。")
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if _run([str(interpreter), "-c", DEPENDENCY_CHECK], root, **quiet):
        print("依赖缺失或版本需要更新，正在联网准备。已有配置不会被覆盖。", flush=True)
        uv = shutil.which("uv")
        if uv:
            command = [uv, "pip", "install", "--python", str(interpreter)]
        else:
            if _run([str(interpreter), "-m", "pip", "--version"], root, **quiet):
                print("运行环境缺少 pip，正在使用 Python 自带的 ensurepip 补齐……", flush=True)
                if _run([str(interpreter), "-m", "ensurepip", "--upgrade"], root):
                    raise RuntimeError("无法补齐 pip。请安装 uv 后重试；若是残缺的 .venv，"
                                       "可将它改名留存后重新准备。便携 runtime 请使用完整包。")
            command = [str(interpreter), "-m", "pip", "install"]
        command += ["-r", str(root / "requirements.txt")]
        if _run(command + ["--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"], root):
            print("镜像安装失败，正在通过官方源重试……", flush=True)
            if _run(command + ["--index-url", "https://pypi.org/simple"], root):
                raise RuntimeError("依赖安装未完成。请检查上面的网络或版本错误，修正后重新运行准备环境。")
        if _run([str(interpreter), "-c", DEPENDENCY_CHECK], root, **quiet):
            raise RuntimeError("依赖仍无法导入或版本不符，环境尚未准备好。请保留本窗口的错误信息排查。")
    return interpreter


def initialize(root: Path) -> None:
    created = bootstrap.initialize(root)
    for name in created:
        print(f"已创建：{name}")
    print("现有配置原样保留。人格和记忆在正式启动时读取；此处不会提前生成人格副本。")


def check(root: Path) -> bool:
    problems = bootstrap.validate(root)
    if problems:
        print("配置尚未通过检查：")
        for problem in problems:
            print(f"  - {problem}")
        print("修正后再双击「检查配置.bat」。原文件未被修改，密钥不会在这里显示。")
        return False
    secrets = bootstrap.read_object(root / "data/secrets.json")
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
                   or secrets.get("deepseek_api_key") or secrets.get("auth_token"))
    cfg = bootstrap.read_object(root / "data/thinking.json")
    print(f"配置格式通过；当前档位：{cfg.get('current')}。")
    print("大脑密钥：已填写。" if has_key else "大脑密钥：尚未填写；请双击「配置API.bat」。")
    if any(os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL")):
        print("检测到 API 环境变量，它们优先于文件中的密钥或地址。此处不显示具体值。")
    print("这只是本地格式检查，不会联网，也不能确认密钥有效或账户额度。发一条对话后才能确认服务可用。")
    return True


def edit_secrets(root: Path) -> None:
    path = root / "data/secrets.json"
    print(f"填写位置：{path}\n只改英文双引号内的值，保留其他字段。保存后运行「检查配置.bat」。")
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(path)])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="首次安装、配置与启动")
    parser.add_argument("action", choices=("prepare", "configure", "check", "launch", "diagnose", "migrate"))
    parser.add_argument("source", nargs="?", help="迁移时的旧目录，可省略后从窗口选择")
    args = parser.parse_args()
    try:
        if args.action in ("configure", "check"):
            error = platform_error()
            if error:
                raise RuntimeError(error)
            interpreter = environment_python(ROOT)
        else:
            interpreter = prepare_environment(ROOT)
        initialize(ROOT)
        if args.action == "configure":
            edit_secrets(ROOT)
            return 0
        if args.action == "migrate":
            return _run([str(interpreter), str(ROOT / "tools/update_wizard.py")]
                        + ([args.source] if args.source else []), ROOT)
        if not check(ROOT):
            return 1
        if args.action in ("prepare", "check"):
            print("\n下一步：填写 API → 检查配置 → 启动桌宠。更新用户请先运行「迁移私人内容.bat」。")
            return 0
        if args.action == "diagnose":
            return _run([str(interpreter), str(ROOT / "src/pet.py"), "--show-chat"], ROOT)
        if sys.platform == "win32":
            gui_python = interpreter.with_name("pythonw.exe")
            if not gui_python.is_file():
                raise RuntimeError("运行环境缺少 pythonw.exe；请使用完整的 Python 安装。")
            subprocess.Popen([str(gui_python), str(ROOT / "src/windows_launcher.py")], cwd=ROOT)
            return 0
        return _run([str(interpreter), str(ROOT / "src/pet.py"), "--show-chat"], ROOT)
    except (bootstrap.ConfigError, OSError, RuntimeError) as exc:
        print(f"\n未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
