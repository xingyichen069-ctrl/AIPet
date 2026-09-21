"""The single settings centre used by the desktop and chat windows."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

import maintenance
import settings_data as D
from ui_theme import THEME_NAMES, is_daytime


SETTINGS_STYLE = """
QDialog { background:#fbf7ef; }
QLabel#settingsTitle { color:#993934; font-size:20px; font-weight:600; }
QLabel#settingsHint { color:#806d63; }
QGroupBox { border:1px solid #decebd; border-radius:10px; margin-top:12px; padding:12px 10px 10px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#8c3933; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background:#fffdf9; color:#423936; border:1px solid #decebd; border-radius:7px; padding:6px;
}
QPlainTextEdit { padding:9px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color:#b75a4d; }
QPushButton { background:#f0e7da; color:#754c42; border:1px solid #e4d5c4; border-radius:8px; padding:7px 13px; }
QPushButton:hover { background:#e9d8c8; border-color:#cfac98; }
QPushButton:pressed { background:#dec6b5; }
QPushButton:disabled { color:#ac9e94; background:#f1ebe2; border-color:#e8ded1; }
QTabWidget::pane { border:1px solid #decebd; border-radius:8px; }
QTabBar::tab { padding:8px 16px; color:#806d63; }
QTabBar::tab:selected { color:#8c3933; font-weight:600; }
QCheckBox { spacing:7px; }
"""


def _model_name(value: str) -> str:
    value = str(value or "").strip()
    return value or "deepseek::deepseek-flash"


class ApiProbe(QThread):
    """Send one tiny non-streaming request without blocking the settings UI."""

    done = Signal(bool, str)

    def __init__(self, key: str, base_url: str, model: str, parent=None):
        super().__init__(parent)
        self.key, self.base_url, self.model = key.strip(), base_url.strip(), _model_name(model)

    def run(self):
        if not self.key:
            self.done.emit(False, "没有 API key。请先填写，或设置 DEEPSEEK_API_KEY 环境变量。")
            return
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model.split("::", 1)[-1],
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read(4096)
            self.done.emit(True, "连接成功，服务端接受了最小测试请求。")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:240]
            self.done.emit(False, f"HTTP {exc.code}：{detail or exc.reason}")
        except (OSError, urllib.error.URLError, ValueError) as exc:
            self.done.emit(False, f"连接失败：{type(exc).__name__}: {exc}")


class ServiceWorker(QThread):
    done = Signal(int, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                code = int(self.fn() or 0)
        except Exception as exc:  # noqa: BLE001
            code = 1
            output.write(f"{type(exc).__name__}: {exc}")
        self.done.emit(code, output.getvalue().strip())


class SettingsDialog(QDialog):
    """Edit private settings without asking the user to find JSON files."""

    saved = Signal()

    def __init__(self, root: Path, appearance=None, pet=None, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.appearance = appearance
        self.pet = pet
        self._api_probe: ApiProbe | None = None
        self._service_worker: ServiceWorker | None = None
        self._thinking_cfg: dict = {}
        self._persona_editors: dict[str, QPlainTextEdit] = {}
        self.setWindowTitle("设置 · 小日和")
        self.setModal(True)
        self.resize(820, 650)
        self._build()
        self.reload_all()
        if self.appearance is not None:
            self.appearance.changed.connect(self._refresh_style)
        self._refresh_style()

    # ------------------------------------------------------------------ build

    def _build(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 16, 18, 14)
        root_layout.setSpacing(10)

        title = QLabel("设置")
        title.setObjectName("settingsTitle")
        root_layout.addWidget(title)
        hint = QLabel("人格、API、思考档位和运行控制集中在这里；保存后新请求立即使用。")
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        root_layout.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._persona_page(), "人格与档案")
        self.tabs.addTab(self._api_page(), "大脑 / API")
        self.tabs.addTab(self._thinking_page(), "思考档位")
        self.tabs.addTab(self._appearance_page(), "外观")
        self.tabs.addTab(self._runtime_page(), "运行与 QQ")
        root_layout.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox()
        self.reload_button = buttons.addButton("重新读取", QDialogButtonBox.ResetRole)
        self.save_button = buttons.addButton("保存全部", QDialogButtonBox.AcceptRole)
        self.close_button = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        self.reload_button.clicked.connect(self.reload_all)
        self.save_button.clicked.connect(self.save_all)
        self.close_button.clicked.connect(self.reject)
        root_layout.addWidget(buttons)

    def _persona_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("这里改的是本机人格文件。注释说明不会注入对话；PROFILE 仍可由记忆整理程序合并维护。")
        note.setWordWrap(True)
        note.setObjectName("settingsHint")
        layout.addWidget(note)
        self.persona_tabs = QTabWidget()
        for name, title, description in D.PERSONA_FILES:
            editor = QPlainTextEdit()
            editor.setFont(QFont("Consolas"))
            editor.setPlaceholderText(f"{name} 尚未创建，保存后会在 persona/ 下生成。")
            editor.setTabStopDistance(28)
            self._persona_editors[name] = editor
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            label = QLabel(description)
            label.setObjectName("settingsHint")
            label.setWordWrap(True)
            tab_layout.addWidget(label)
            tab_layout.addWidget(editor, 1)
            self.persona_tabs.addTab(tab, title)
        layout.addWidget(self.persona_tabs, 1)
        return page

    def _api_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        connection = QGroupBox("OpenAI 兼容接口")
        form = QFormLayout(connection)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("留空则删除本地 key，也可使用环境变量")
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
        key_row.addWidget(self.show_key)
        form.addRow("API key", key_row)
        self.api_env_hint = QLabel()
        self.api_env_hint.setObjectName("settingsHint")
        form.addRow("当前来源", self.api_env_hint)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("例如 https://api.deepseek.com 或代理地址")
        form.addRow("Base URL", self.base_url_edit)
        layout.addWidget(connection)

        test_box = QGroupBox("连接测试")
        test_layout = QVBoxLayout(test_box)
        test_layout.addWidget(QLabel("测试会发送一条 max_tokens=1 的最小请求，不会写入聊天记录。"))
        self.test_api_button = QPushButton("测试当前接口")
        self.test_api_button.clicked.connect(self.test_api)
        test_layout.addWidget(self.test_api_button, 0, Qt.AlignLeft)
        layout.addWidget(test_box)
        layout.addStretch(1)
        return page

    def _thinking_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        current = QGroupBox("当前档位")
        current_form = QFormLayout(current)
        self.current_level_combo = QComboBox()
        current_form.addRow("默认使用", self.current_level_combo)
        layout.addWidget(current)

        preset = QGroupBox("档位参数")
        form = QFormLayout(preset)
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._load_preset_editor)
        form.addRow("编辑档位", self.preset_combo)
        self.model_edit = QLineEdit()
        form.addRow("模型", self.model_edit)
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(["none", "low", "medium", "high", "max"])
        form.addRow("推理投入", self.reasoning_combo)
        self.search_combo = QComboBox()
        self.search_combo.addItems(["off", "on_demand", "eager", "always"])
        form.addRow("联网搜索", self.search_combo)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(100, 200000)
        self.max_tokens_spin.setSingleStep(100)
        form.addRow("回复上限", self.max_tokens_spin)
        self.memory_budget_spin = QSpinBox()
        self.memory_budget_spin.setRange(100, 500000)
        self.memory_budget_spin.setSingleStep(100)
        form.addRow("记忆预算", self.memory_budget_spin)
        self.memory_entries_spin = QSpinBox()
        self.memory_entries_spin.setRange(1, 500)
        form.addRow("记忆条数", self.memory_entries_spin)
        self.self_check_box = QCheckBox("回答前做一次自检")
        form.addRow("自检", self.self_check_box)
        self.verbosity_edit = QLineEdit()
        form.addRow("篇幅提示", self.verbosity_edit)
        layout.addWidget(preset)
        explanation = QLabel("自动模式只负责按问题挑档；这里保存的是每个档位的真实请求参数。")
        explanation.setObjectName("settingsHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addStretch(1)
        return page

    def _appearance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("聊天窗口与菜单")
        form = QFormLayout(group)
        self.theme_combo = QComboBox()
        for key, label in THEME_NAMES.items():
            self.theme_combo.addItem(label, key)
        form.addRow("主题", self.theme_combo)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(11, 18)
        self.font_spin.setSuffix(" px")
        form.addRow("字号", self.font_spin)
        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.30, 1.00)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        form.addRow("玻璃透明度", self.opacity_spin)
        layout.addWidget(group)
        note = QLabel("主题和字号会立即影响设置页、聊天窗口及菜单；角色模型文件仍放在 assets/ 或 Live2D 目录。")
        note.setObjectName("settingsHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _runtime_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        qq_credentials = QGroupBox("QQ 凭据")
        qq_form = QFormLayout(qq_credentials)
        self.qq_appid_edit = QLineEdit()
        qq_form.addRow("App ID", self.qq_appid_edit)
        self.qq_secret_edit = QLineEdit()
        self.qq_secret_edit.setEchoMode(QLineEdit.Password)
        qq_secret_row = QHBoxLayout()
        qq_secret_row.addWidget(self.qq_secret_edit, 1)
        self.show_qq_secret = QCheckBox("显示")
        self.show_qq_secret.toggled.connect(
            lambda checked: self.qq_secret_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
        qq_secret_row.addWidget(self.show_qq_secret)
        qq_form.addRow("Client Secret", qq_secret_row)
        layout.addWidget(qq_credentials)

        tools_box = QGroupBox("工具与网络")
        tools_form = QFormLayout(tools_box)
        self.fs_root_edit = QLineEdit()
        self.fs_root_edit.setPlaceholderText("留空使用默认的 AIPet 沙箱目录")
        tools_form.addRow("工具沙箱根", self.fs_root_edit)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("auto、direct，或 http://127.0.0.1:7890")
        tools_form.addRow("代理", self.proxy_edit)
        layout.addWidget(tools_box)

        status_box = QGroupBox("运行状态")
        status_layout = QVBoxLayout(status_box)
        self.status_label = QLabel()
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_layout.addWidget(self.status_label)
        status_buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_runtime)
        self.start_qq_button = QPushButton("启动 QQ")
        self.start_qq_button.clicked.connect(lambda: self._run_service(maintenance.qq_start, "启动 QQ"))
        self.stop_qq_button = QPushButton("停止 QQ")
        self.stop_qq_button.clicked.connect(lambda: self._run_service(maintenance.qq_stop, "停止 QQ"))
        for button in (refresh, self.start_qq_button, self.stop_qq_button):
            status_buttons.addWidget(button)
        status_buttons.addStretch(1)
        status_layout.addLayout(status_buttons)
        layout.addWidget(status_box)

        paths = QGroupBox("本机资料")
        path_layout = QHBoxLayout(paths)
        for label, path in (("打开 data", self.root / "data"),
                            ("打开 persona", self.root / "persona"),
                            ("打开启动日志", self.root / "data" / "cache" / "windows-startup.log")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, p=path: self._open_path(p))
            path_layout.addWidget(button)
        layout.addWidget(paths)
        note = QLabel("迁移旧安装、诊断和状态查看也已经收进 AIPet.exe 命令行：AIPet.exe migrate、diagnose、status。")
        note.setWordWrap(True)
        note.setObjectName("settingsHint")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ---------------------------------------------------------------- reload/save

    def reload_all(self):
        for name, editor in self._persona_editors.items():
            editor.setPlainText(D.read_persona(self.root, name))

        secrets = D.load_secrets(self.root)
        self.api_key_edit.setText(str(secrets.get("deepseek_api_key") or secrets.get("auth_token", "")))
        self.base_url_edit.setText(str(secrets.get("deepseek_base_url") or secrets.get("base_url", "https://api.deepseek.com")))
        self.qq_appid_edit.setText(str(secrets.get("qq_appid", "")))
        self.qq_secret_edit.setText(str(secrets.get("qq_secret", "")))
        if os.environ.get("DEEPSEEK_API_KEY"):
            self.api_env_hint.setText("环境变量 DEEPSEEK_API_KEY 已设置，会优先于本地文件。")
        else:
            self.api_env_hint.setText(f"本地文件：{D.secrets_path(self.root)}")

        self._thinking_cfg = D.load_thinking(self.root)
        self._populate_thinking()
        app_config = D.load_config(self.root)
        tools_config = app_config.get("tools") if isinstance(app_config.get("tools"), dict) else {}
        self.fs_root_edit.setText(str(tools_config.get("fs_root", "")))
        self.proxy_edit.setText(str(tools_config.get("proxy", "auto")))
        self._reload_appearance()
        self.refresh_runtime()

    def _populate_thinking(self):
        cfg = self._thinking_cfg
        presets = cfg.get("presets") if isinstance(cfg.get("presets"), dict) else {}
        levels = [key for key in ("frugal", "daily", "serious", "deep", "max") if key in presets]
        if not levels:
            levels = list(presets)
        self.current_level_combo.blockSignals(True)
        self.current_level_combo.clear()
        self.current_level_combo.addItem("自动", "auto")
        for key in levels:
            self.current_level_combo.addItem(str(presets[key].get("name", key)), key)
        current = cfg.get("current", "auto")
        index = max(0, self.current_level_combo.findData(current))
        self.current_level_combo.setCurrentIndex(index)
        self.current_level_combo.blockSignals(False)

        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for key in levels:
            self.preset_combo.addItem(str(presets[key].get("name", key)), key)
        self.preset_combo.blockSignals(False)
        if levels:
            selected = current if current in levels else levels[0]
            self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(selected)))
            self._load_preset_editor()

    def _load_preset_editor(self):
        key = self.preset_combo.currentData()
        params = ((self._thinking_cfg.get("presets", {}).get(key) or {}).get("params") or {}
                  if key else {})
        model = params.get("model", "deepseek::deepseek-flash")
        provider_model = D.load_secrets(self.root).get("model")
        if provider_model and (not model or str(model).startswith("deepseek::")):
            model = provider_model
        self.model_edit.setText(str(model))
        self.reasoning_combo.setCurrentText(str(params.get("reasoning_effort", "low")))
        self.search_combo.setCurrentText(str(params.get("search", "on_demand")))
        self.max_tokens_spin.setValue(int(params.get("max_tokens", 800)))
        self.memory_budget_spin.setValue(int(params.get("memory_budget", 1200)))
        self.memory_entries_spin.setValue(int(params.get("memory_entries", 12)))
        self.self_check_box.setChecked(bool(params.get("self_check", False)))
        self.verbosity_edit.setText(str(params.get("verbosity", "")))

    def _store_preset_editor(self):
        key = self.preset_combo.currentData()
        if not key:
            return
        presets = self._thinking_cfg.setdefault("presets", {})
        preset = presets.setdefault(key, {})
        params = preset.setdefault("params", {})
        params.update({
            "model": self.model_edit.text().strip() or "deepseek::deepseek-flash",
            "reasoning_effort": self.reasoning_combo.currentText(),
            "search": self.search_combo.currentText(),
            "max_tokens": self.max_tokens_spin.value(),
            "memory_budget": self.memory_budget_spin.value(),
            "memory_entries": self.memory_entries_spin.value(),
            "self_check": self.self_check_box.isChecked(),
            "verbosity": self.verbosity_edit.text().strip(),
        })

    def _reload_appearance(self):
        if self.appearance is None:
            return
        settings = self.appearance.settings
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.get("theme", "touhou"))))
        self.font_spin.setValue(int(settings.get("font_size", 13)))
        self.opacity_spin.setValue(float(settings.get("glass_opacity", .65)))

    def save_all(self):
        try:
            for name, editor in self._persona_editors.items():
                D.write_persona(self.root, name, editor.toPlainText())

            secrets = D.load_secrets(self.root)
            key = self.api_key_edit.text().strip()
            base = self.base_url_edit.text().strip()
            if key:
                secrets["deepseek_api_key"] = key
            else:
                secrets.pop("deepseek_api_key", None)
            if base:
                secrets["deepseek_base_url"] = base
            else:
                secrets.pop("deepseek_base_url", None)
            appid = self.qq_appid_edit.text().strip()
            qq_secret = self.qq_secret_edit.text().strip()
            if appid:
                secrets["qq_appid"] = appid
            else:
                secrets.pop("qq_appid", None)
            if qq_secret:
                secrets["qq_secret"] = qq_secret
            else:
                secrets.pop("qq_secret", None)
            D.save_secrets(self.root, secrets)

            app_config = D.load_config(self.root)
            tools_config = app_config.setdefault("tools", {})
            tools_config["fs_root"] = self.fs_root_edit.text().strip()
            tools_config["proxy"] = self.proxy_edit.text().strip() or "auto"
            D.save_config(self.root, app_config)
            try:
                import memory as M
                if Path(M.ROOT).resolve() == self.root.resolve():
                    M.reload_config()
            except (ImportError, KeyError, OSError, ValueError):
                pass

            self._store_preset_editor()
            self._thinking_cfg["current"] = self.current_level_combo.currentData() or "auto"
            D.save_thinking(self.root, self._thinking_cfg)

            if self.appearance is not None:
                self.appearance.choose("theme", self.theme_combo.currentData())
                self.appearance.choose("font_size", self.font_spin.value())
                self.appearance.choose("glass_opacity", self.opacity_spin.value())

            self.saved.emit()
            self.refresh_runtime()
            QMessageBox.information(self, "设置", "已保存。新的对话会使用最新配置。")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "设置没有保存", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- actions

    def test_api(self):
        if self._api_probe and self._api_probe.isRunning():
            return
        key = self.api_key_edit.text().strip() or os.environ.get("DEEPSEEK_API_KEY", "")
        base = self.base_url_edit.text().strip() or "https://api.deepseek.com"
        params = ((self._thinking_cfg.get("presets", {}).get(self.preset_combo.currentData()) or {}).get("params") or {})
        model = self.model_edit.text().strip() or params.get("model", "deepseek::deepseek-flash")
        self.test_api_button.setEnabled(False)
        self.test_api_button.setText("测试中…")
        self._api_probe = ApiProbe(key, base, model, self)
        self._api_probe.done.connect(self._api_probe_done)
        self._api_probe.finished.connect(lambda: self.test_api_button.setEnabled(True))
        self._api_probe.finished.connect(lambda: self.test_api_button.setText("测试当前接口"))
        self._api_probe.start()

    def _api_probe_done(self, ok: bool, message: str):
        (QMessageBox.information if ok else QMessageBox.warning)(self, "接口测试", message)

    def refresh_runtime(self):
        desktop = "没在跑"
        lock = self.root / "data" / "desktop.lock"
        try:
            pid = int(lock.read_text(encoding="utf-8").splitlines()[0])
            desktop = f"在跑（PID {pid}）" if maintenance._pid_alive(pid) else "锁文件残留，进程已退出"
        except (OSError, ValueError, IndexError):
            pass
        try:
            import qq_bot as QB
            qpid, state = QB.read_pid(), QB.read_status()
            qq = f"在跑（PID {qpid}）· {state.get('state', 'unknown')}" if qpid else "没在跑"
        except Exception as exc:  # noqa: BLE001
            qq = f"读不到：{type(exc).__name__}"
        self.status_label.setText(f"桌宠：{desktop}\nQQ：{qq}\n安装目录：{self.root}")

    def _run_service(self, fn, label: str):
        if self._service_worker and self._service_worker.isRunning():
            return
        for button in (self.start_qq_button, self.stop_qq_button):
            button.setEnabled(False)
        self._service_worker = ServiceWorker(fn, self)
        self._service_worker.done.connect(lambda code, text: self._service_done(label, code, text))
        self._service_worker.start()

    def _service_done(self, label: str, code: int, text: str):
        self.start_qq_button.setEnabled(True)
        self.stop_qq_button.setEnabled(True)
        self.refresh_runtime()
        if code:
            QMessageBox.warning(self, label, text or "操作失败。")
        else:
            QMessageBox.information(self, label, text or "已完成。")

    def _open_path(self, path: Path):
        path = Path(path)
        if not path.exists():
            path = path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _refresh_style(self):
        if self.appearance is not None:
            self.setStyleSheet(self.appearance.stylesheet(SETTINGS_STYLE, is_daytime()))
        else:
            self.setStyleSheet(SETTINGS_STYLE)

    def closeEvent(self, event):
        if ((self._api_probe and self._api_probe.isRunning())
                or (self._service_worker and self._service_worker.isRunning())):
            event.ignore()
            return
        super().closeEvent(event)
