#!/usr/bin/env python3
"""Build the standalone Windows desktop package.

Usage::

    python tools/package_windows.py --force

The output is a PyInstaller onedir package at ``dist/AIPet``.  It keeps the
repository's public assets outside the executable so Live2D and Qt can load
their files normally.  Private ``data/`` files are never copied into it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "AIPet"
OUTPUT = ROOT / "dist" / APP_NAME
BUILD_ROOT = ROOT / "build" / "windows-package"

HIDDEN_IMPORTS = (
    "pet", "memory", "thinking", "ui_theme", "desktop_state", "theme_widgets",
    "brain", "local_tools", "live2d_widget", "update", "qq_bot", "companion",
    "people", "render", "proxy", "code_tasks",
)


def _pyinstaller_command() -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ROOT / "assets" / "character.ico"),
        "--paths", str(ROOT / "src"),
        "--add-data", f"{ROOT / 'VERSION'};.",
        "--add-data", f"{ROOT / 'assets'};assets",
        "--add-data", f"{ROOT / 'themes'};themes",
        "--add-data", f"{ROOT / 'hiyori_zh-Hans'};hiyori_zh-Hans",
        "--add-data", f"{ROOT / 'data' / 'config.example.json'};data",
        "--add-data", f"{ROOT / 'data' / 'thinking.json'};data",
    ]
    for name in HIDDEN_IMPORTS:
        command.extend(("--hidden-import", name))
    command.extend((
        "--distpath", str(BUILD_ROOT / "dist"),
        "--workpath", str(BUILD_ROOT / "work"),
        "--specpath", str(BUILD_ROOT),
        str(ROOT / "src" / "windows_launcher.py"),
    ))
    return command


def _remove_incompatible_icu(bundle: Path) -> list[str]:
    """Remove PyInstaller's accidental Windows ICU dependency copies.

    Qt6Core from the PySide6 wheel imports the Windows ICU API without a
    version suffix.  PyInstaller can copy a version-suffixed ICU build from
    its dependency scan; that DLL exports ``ucnv_open_78`` instead of the
    required ``ucnv_open`` and prevents QtCore from loading.  Windows 10/11
    already provide the compatible ``icuuc.dll`` in System32, which is what
    the normal virtualenv run uses as well.
    """
    removed = []
    for name in ("icuuc.dll", "icudt78.dll"):
        path = bundle / "_internal" / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return removed


def _relocate_runtime_data(bundle: Path) -> None:
    """Put project-relative resources beside the EXE.

    PyInstaller 6 places most ``--add-data`` entries under ``_internal``.
    AIPet deliberately resolves ``ROOT`` to the directory beside the EXE so
    that its editable themes, model files, and private data remain visible to
    the user.  Move the public resources back to that layout after COLLECT.
    """
    internal = bundle / "_internal"
    for name in ("assets", "themes", "hiyori_zh-Hans"):
        source = internal / name
        target = bundle / name
        if source.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            shutil.rmtree(source)

    source_data = internal / "data"
    target_data = bundle / "data"
    target_data.mkdir(parents=True, exist_ok=True)
    for name in ("config.example.json", "thinking.json"):
        source = source_data / name
        if source.is_file():
            shutil.copy2(source, target_data / name)
            source.unlink()
    if source_data.is_dir() and not any(source_data.iterdir()):
        source_data.rmdir()

    source_version = internal / "VERSION"
    if source_version.is_file():
        shutil.copy2(source_version, bundle / "VERSION")
        source_version.unlink()


def build(force: bool = False) -> Path:
    if OUTPUT.exists() and not force:
        raise RuntimeError(
            f"输出目录已存在：{OUTPUT}\n"
            "为避免删除其中的本地数据，请先移走它，或明确使用 --force。"
        )

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(_pyinstaller_command(), cwd=ROOT, check=True)

    built = BUILD_ROOT / "dist" / APP_NAME
    if not built.is_dir():
        raise RuntimeError(f"PyInstaller 没有生成预期目录：{built}")
    removed = _remove_incompatible_icu(built)
    _relocate_runtime_data(built)

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, OUTPUT)
    (OUTPUT / "使用说明.txt").write_text(
        "双击 AIPet.exe 启动桌宠和本地聊天窗口。\n"
        "首次运行时请在 data/secrets.json 或环境变量中配置模型密钥。\n"
        "本目录的 data、memory、persona 等运行资料请自行备份。\n",
        encoding="utf-8",
    )
    print(f"已生成：{OUTPUT}")
    if removed:
        print("已排除不兼容的 Qt ICU DLL：" + ", ".join(removed))
    return OUTPUT


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 AIPet Windows 目录版 EXE")
    parser.add_argument(
        "--force", action="store_true",
        help="替换已有 dist/AIPet；只应对打包产物使用，运行数据请先备份",
    )
    args = parser.parse_args()
    try:
        build(force=args.force)
    except subprocess.CalledProcessError as exc:
        print(f"PyInstaller 构建失败（退出码 {exc.returncode}）。", file=sys.stderr)
        return exc.returncode or 1
    except (OSError, RuntimeError) as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
