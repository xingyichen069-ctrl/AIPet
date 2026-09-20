#!/usr/bin/env python3
"""Build the portable Windows desktop package.

Usage::

    .venv\\Scripts\\python.exe tools\\package_windows.py --force

The output is a source-mode package at ``dist/AIPet``.  ``AIPet.exe`` is a
small native launcher built with Visual Studio; it starts the bundled Python
runtime and ``src/windows_launcher.py``.  Keeping the source files outside the
launcher means the in-app updater and local code tasks keep working after the
package is installed.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "AIPet"
OUTPUT = ROOT / "dist" / APP_NAME
BUILD_ROOT = ROOT / "build" / "windows-package"
BASE_PYTHON = Path(sys.base_prefix).resolve()
VENV_SITE = Path(sys.prefix).resolve() / "Lib" / "site-packages"

# PySide6's meta package pulls in every Qt Addons module.  AIPet uses only the
# Essentials modules (Core/Gui/Widgets/Network/OpenGL), so leaving Addons out
# keeps the portable package smaller without changing runtime behavior.
SKIP_DISTRIBUTIONS = {
    "pyside6-addons", "pyinstaller", "pyinstaller-hooks-contrib",
    "setuptools", "pip", "wheel",
}

PROJECT_DIRECTORIES = ("src", "assets", "themes", "hiyori_zh-Hans")
PROJECT_FILES = (
    "VERSION", "requirements.txt", "README.md", "CHANGELOG.md",
    "Windows使用说明.md", "新增功能说明.md",
)


def _key(name: str) -> str:
    return name.lower().replace("_", "-")


def _requirements() -> list[Requirement]:
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(Requirement(line))
    return out


def _runtime_distributions() -> dict[str, metadata.Distribution]:
    """Resolve the app's direct and transitive wheels in the build venv."""
    pending = _requirements()
    selected: dict[str, metadata.Distribution] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = _key(requirement.name)
        if name in SKIP_DISTRIBUTIONS or name in selected:
            continue
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"构建环境缺少 {requirement.name}，请先运行 AIPet.exe prepare 或准备源码环境。"
            ) from exc
        selected[name] = distribution
        for dependency in distribution.requires or ():
            pending.append(Requirement(dependency))
    return selected


def _copy_base_python(runtime: Path) -> None:
    """Copy CPython itself, excluding the host's unrelated site-packages."""
    def ignore(source: str, names: list[str]) -> set[str]:
        source_path = Path(source)
        try:
            relative = source_path.relative_to(BASE_PYTHON)
        except ValueError:
            relative = Path()
        ignored = set()
        for name in names:
            rel = relative / name
            if (name == "__pycache__" or name.lower().endswith(".bat")
                    or name in {"Doc", "include", "Scripts", "share"}):
                ignored.add(name)
            elif rel == Path("Lib") / "site-packages":
                ignored.add(name)
            elif rel.parts[:2] == ("Lib", "test"):
                ignored.add(name)
        return ignored

    shutil.copytree(BASE_PYTHON, runtime, ignore=ignore, dirs_exist_ok=True)


def _copy_runtime_distributions(runtime: Path) -> list[str]:
    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    copied: set[str] = set()
    for name, distribution in sorted(_runtime_distributions().items()):
        for member in distribution.files or ():
            source = Path(distribution.locate_file(member)).resolve()
            if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            try:
                relative = source.relative_to(VENV_SITE)
            except ValueError:
                continue
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.add(str(relative))
    if not copied:
        raise RuntimeError("没有找到可复制的 Python 运行依赖。")
    return sorted(copied)


def _copy_project_files(bundle: Path) -> None:
    for name in PROJECT_DIRECTORIES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(
                source, bundle / name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
    for name in PROJECT_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, bundle / name)
    data = bundle / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("config.example.json", "thinking.json"):
        shutil.copy2(ROOT / "data" / name, data / name)


def _find_vcvars() -> Path:
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if not vswhere.exists():
        raise RuntimeError("找不到 Visual Studio Installer 的 vswhere.exe。")
    result = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    installations = json.loads(result.stdout or "[]")
    if not installations:
        raise RuntimeError("Visual Studio 没有安装 C++ x64 工具链。")
    install = Path(installations[0]["installationPath"])
    vcvars = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.exists():
        raise RuntimeError(f"找不到 Visual Studio x64 环境脚本：{vcvars}")
    return vcvars


def _build_launcher() -> Path:
    """Build the tiny native launcher with the installed MSVC toolchain."""
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    source = BUILD_ROOT / "aipet_launcher.c"
    resource = BUILD_ROOT / "aipet_launcher.rc"
    icon = BUILD_ROOT / "character.ico"
    shutil.copy2(ROOT / "tools" / "aipet_launcher.c", source)
    shutil.copy2(ROOT / "tools" / "aipet_launcher.rc", resource)
    shutil.copy2(ROOT / "assets" / "character.ico", icon)
    vcvars = _find_vcvars()
    command = (
        f'call "{vcvars}" >nul && '
        'rc /nologo /r /fo aipet_launcher.res aipet_launcher.rc && '
        'cl /nologo /O2 /MT /DUNICODE /D_UNICODE /W3 '
        '/Fe:AIPet.exe aipet_launcher.c aipet_launcher.res user32.lib'
    )
    # Passing a list makes subprocess escape the quotes in the batch-file
    # path as literal ``\\\"`` characters on Windows.  shell=True delegates
    # to cmd.exe and preserves the normal ``call \"C:\\Program Files...\"``
    # syntax; every path here came from our own repository or vswhere.
    subprocess.run(command, cwd=BUILD_ROOT, check=True, shell=True)
    launcher = BUILD_ROOT / "AIPet.exe"
    if not launcher.is_file():
        raise RuntimeError("Visual Studio 没有生成 AIPet.exe。")
    return launcher


def _smoke_runtime(bundle: Path) -> None:
    python = bundle / "runtime" / "python.exe"
    code = "import PySide6, live2d.v3, ddgs; print('runtime imports ok')"
    subprocess.run([str(python), "-c", code], cwd=bundle, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=90)


def _write_manifest(bundle: Path, copied: list[str]) -> None:
    """Record the exact portable components for update and support checks."""
    files = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file()
                       and p.name != "release-manifest.json"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": str(path.relative_to(bundle)).replace("\\", "/"),
                      "size": path.stat().st_size, "sha256": digest})
    manifest = {
        "format": 1,
        "app": APP_NAME,
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "python": sys.version.split()[0],
        "architecture": "64-bit" if sys.maxsize > 2**32 else "32-bit",
        "runtime_files_copied": len(copied),
        "files": files,
    }
    (bundle / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(force: bool = False) -> Path:
    if OUTPUT.exists() and not force:
        raise RuntimeError(
            f"输出目录已存在：{OUTPUT}\n"
            "为避免删除其中的本地数据，请先移走它，或明确使用 --force。"
        )
    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    _copy_project_files(OUTPUT)
    runtime = OUTPUT / "runtime"
    _copy_base_python(runtime)
    copied = _copy_runtime_distributions(runtime)
    launcher = _build_launcher()
    shutil.copy2(launcher, OUTPUT / "AIPet.exe")
    (OUTPUT / "使用说明.txt").write_text(
        "双击 AIPet.exe 启动桌宠和本地聊天窗口。\n"
        "首次运行时请在 data/secrets.json 或环境变量中配置模型密钥。\n"
        "本目录的 data、memory、persona 等运行资料请自行备份。\n",
        encoding="utf-8",
    )
    _smoke_runtime(OUTPUT)
    _write_manifest(OUTPUT, copied)
    print(f"已生成：{OUTPUT}")
    print(f"已复制 {len(copied)} 个运行时文件；代码任务和源码更新继续使用 bundled Python。")
    return OUTPUT


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 AIPet Windows 便携 EXE")
    parser.add_argument(
        "--force", action="store_true",
        help="替换已有 dist/AIPet；只应对打包产物使用，运行数据请先备份",
    )
    args = parser.parse_args()
    try:
        build(force=args.force)
    except subprocess.CalledProcessError as exc:
        print(f"构建失败（退出码 {exc.returncode}）。", file=sys.stderr)
        return exc.returncode or 1
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
