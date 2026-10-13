"""Refresh the running desktop after either configuration entry saves a file."""
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QFileSystemWatcher, QTimer, Signal

from settings_data import _load_json


def reload_runtime(root):
    root = Path(root).resolve()
    memory = sys.modules.get("memory")
    if memory is None or Path(memory.ROOT).resolve() != root:
        return
    # Read and validate before replacing the working configuration. An external
    # editor may briefly leave a missing, partial or invalid document behind.
    config_path = root / "data/config.json"
    if not config_path.is_file():
        raise ValueError("应用配置尚未保存完整。")
    config = _load_json(config_path)
    if not isinstance(config.get("paths"), dict):
        raise ValueError("应用配置缺少有效的资料位置。")
    for key in ("tools", "knowledge", "retrieval", "speaker", "scoring", "compression", "privacy"):
        if key in config and not isinstance(config[key], dict):
            raise ValueError(f"应用配置的 {key} 必须是对象。")
    if any(not isinstance(value, str) for value in config["paths"].values()):
        raise ValueError("资料位置必须是文本路径。")
    required = ("journal", "state", "profile", "summaries", "archive", "view")
    if any(not config["paths"].get(key, "").strip() for key in required):
        raise ValueError("资料位置缺少必要路径，请补齐后再保存。")
    docs_dir = config.get("knowledge", {}).get("docs_dir", "knowledge")
    if not isinstance(docs_dir, str):
        raise ValueError("知识库位置必须是文本路径。")
    thinking = sys.modules.get("thinking")
    if thinking is not None:
        thinking.load(force=True)
    memory.reload_config(config)
    proxy = sys.modules.get("proxy")
    if proxy is not None:
        proxy._cache["value"] = "\x00"
    tools = sys.modules.get("tools")
    if tools is not None and hasattr(tools, "TOOLS_CFG"):
        tools.TOOLS_CFG = config.get("tools", {})
    knowledge = sys.modules.get("knowledge")
    if knowledge is not None:
        knowledge.KCFG = config.get("knowledge", {})
        knowledge.DOCS_DIR = root / docs_dir


class ConfigurationWatcher(QObject):
    changed = Signal()

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.paths = [self.root / "data" / name for name in ("config.json", "thinking.json")]
        self.error = ""
        self._last = self._signature()
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self.check)
        # Also handle missed Windows replace notifications and newly created files.
        self.timer = QTimer(self)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        self._watch()

    def _signature(self):
        result = []
        for path in self.paths:
            try:
                stat = path.stat()
                result.append((stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                result.append(None)
        return tuple(result)

    def _watch(self):
        # Watching the containing directory avoids retaining file handles while
        # Windows atomically replaces config.json. The timer covers in-place edits.
        directory = self.root / "data"
        if directory.is_dir() and str(directory) not in self.watcher.directories():
            self.watcher.addPath(str(directory))

    def check(self, *_):
        self._watch()
        signature = self._signature()
        if signature == self._last:
            return
        try:
            reload_runtime(self.root)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.error = str(error)
            return
        self._last = signature
        self.error = ""
        self.changed.emit()
