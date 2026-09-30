"""New-directory update wizard. Copies data only; never stops any process."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import bootstrap
import migrate


def inspect_migration(source: Path, target: Path):
    source, target = source.resolve(), target.resolve()
    for path in (source, target):
        reason = migrate._looks_like_install(path)
        if reason:
            raise ValueError(reason + "。请选择包含 src 文件夹的 AIPet 目录。")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("新旧目录必须分开，且不能互相包含。请把新版解压到旧版旁边的新文件夹。")
    dirty = migrate.footprint(target)
    if dirty:
        raise ValueError("新目录已经有填写过的配置或私人资料，不能用迁移覆盖：\n"
                         + "\n".join(dirty[:8])
                         + "\n请另建新目录、准备环境后再迁移。现有内容保持不动。")
    rows, missing = migrate.plan(source, target)
    meaningful = [r for r in rows if not bootstrap.is_unedited_template(source, r["rel"].as_posix())]
    if not meaningful:
        raise ValueError("所选目录只有公开模板，没有找到私人资料。请重新选择实际使用过的旧目录。")
    notes = []
    config_file = source / "data/config.json"
    if config_file.exists():
        config = bootstrap.read_object(config_file)
        defaults = bootstrap.read_object(target / "data/config.example.json")
        paths = config.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError("旧 config.json 的 paths 格式不对。请先按旧目录中的模板修正。")
        custom = [key for key, value in paths.items() if not key.startswith("_")
                  and value != defaults.get("paths", {}).get(key)]
        if custom:
            raise ValueError("旧安装使用了自定义记忆/档案位置：" + "、".join(custom)
                             + "。本双击入口暂不迁移这种布局，请先人工确认这些文件的新位置。旧目录未改动。")
        for section in ("knowledge", "live2d"):
            values = config.get(section, {})
            if not isinstance(values, dict):
                raise ValueError(f"旧 config.json 的 {section} 格式不对。")
            key = "docs_dir" if section == "knowledge" else "model"
            value = values.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"旧 config.json 的 {section}.{key} 应是文字路径。")
            if value and value != defaults.get(section, {}).get(key):
                if section == "knowledge":
                    notes.append("自定义知识库目录不另行复制，请保留原目录并核对 config.json 中的 knowledge.docs_dir。")
                elif values.get("enabled", defaults.get("live2d", {}).get("enabled", False)) is False:
                    notes.append("Live2D 已关闭，未检查或复制未使用的自定义模型；日后启用前请另行复制模型。")
                elif not (target / value).is_file():
                    raise ValueError("旧配置使用了自定义 Live2D 模型，新目录找不到该模型。"
                                     "请先按 config.json 中 live2d.model 的位置复制对应模型文件夹，再重新迁移。")
        fs_root = config.get("tools", {}).get("fs_root") if isinstance(config.get("tools", {}), dict) else None
        if fs_root:
            notes.append("文件沙箱仍使用旧配置里的 fs_root；沙箱中的外部资料不会另行复制，请核对该文件夹仍可访问。")
    # Validate source JSON before copying, but never print its contents.
    for row in rows:
        if row["rel"].as_posix() in ("data/config.json", "data/thinking.json", "data/secrets.json"):
            bootstrap.read_object(row["src"])
    return rows, notes


def main() -> int:
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv[:1])
    source_text = sys.argv[1] if len(sys.argv) > 1 else QFileDialog.getExistingDirectory(
        None, "选择旧版 AIPet 目录（包含 src、data 的那一层）", str(ROOT.parent))
    if not source_text:
        return 0
    source = Path(source_text)
    try:
        rows, notes = inspect_migration(source, ROOT)
        kinds = list(dict.fromkeys(row["kind"] for row in rows))
        message = (f"从：{source.resolve()}\n到：{ROOT}\n\n"
                   f"将复制 {len(rows)} 个文件：{'、'.join(kinds)}。\n"
                   "旧目录和旧文件会保留。新目录的空模板会先备份。\n\n"
                   "请先从菜单退出旧桌宠；若有自行启用的后台组件，也请先退出。"
                   "这个入口不会结束任何进程。\n"
                   "迁移期间不要同时使用两份安装。\n\n"
                   + ("\n".join(notes) + "\n\n" if notes else "")
                   + "确认已经退出旧程序，开始复制？")
        choice = QMessageBox.question(None, "迁移私人内容", message,
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if choice != QMessageBox.Yes:
            return 0
        print("正在暂存、校验并复制资料；文件较多时请等待完成提示，不要关闭窗口。", flush=True)
        copied, _, backup = migrate.apply(rows, ROOT)
        bootstrap.initialize(ROOT)
        errors = bootstrap.validate(ROOT)
        if errors:
            QMessageBox.warning(None, "已复制，但配置还需检查",
                                "\n".join(errors) + f"\n\n旧目录仍保留；迁移记录：{backup}"
                                "\n修正后双击检查配置，不要删除旧目录。")
            return 1
        QMessageBox.information(None, "复制和本地检查完成",
                                f"已复制并校验 {copied} 个文件。\n迁移记录与覆盖前备份：{backup}\n\n"
                                "接下来：\n1. 双击启动桌宠，确认人格、历史话题和约定。\n"
                                "2. 发一句话确认 API 能回复。\n3. 从新目录重新创建桌面快捷方式。\n"
                                "旧目录继续保留，遇到问题可退出新版后使用旧版。"
                                "\n运行环境、自定义主题与立绘、外部沙箱、目录型历史备份未复制。")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        QMessageBox.critical(None, "迁移未完成", str(exc) + "\n\n请保留旧目录，解决上述问题后再试。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
