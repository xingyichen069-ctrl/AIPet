"""Private settings centre, extracted from all-round and adapted to main."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid
from PySide6.QtCore import QThread, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)
import settings_data as D
import provider_config as PC
from settings_fields import CHOICES, GROUPS, LABELS
from settings_style import apply_dialog_style
from ui_theme import THEME_NAMES
from configuration_windows import ConfigurationDialog, show_configuration_window


def hint(text):
    label = QLabel(text)
    label.setObjectName("settingsHint")
    label.setWordWrap(True)
    return label


def secret_edit():
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.Password)
    return edit


def secret_row(edit):
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    show = QCheckBox("显示")
    show.toggled.connect(lambda checked: edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
    row.addWidget(show)
    return row


def combo(items):
    widget = QComboBox()
    for title, value in items:
        widget.addItem(title, value)
    return widget


def merge_defaults(defaults, values):
    out = deepcopy(defaults)
    for key, value in values.items():
        out[key] = merge_defaults(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else deepcopy(value)
    return out


class ApiProbe(QThread):
    done = Signal(bool, object)

    def __init__(self, connection, operation="probe", parent=None):
        super().__init__(parent)
        self.connection, self.operation = connection, operation

    def run(self):
        try:
            result = PC.list_models(self.connection) if self.operation == "models" else PC.probe(self.connection, vision=self.operation == "vision")
            self.done.emit(True, result)
        except (ValueError, TypeError) as error:
            self.done.emit(False, str(error))
        except Exception:
            self.done.emit(False, "接口检查未完成，请稍后重试。")


class SettingsDialog(ConfigurationDialog):
    saved = Signal()

    def __init__(self, root, appearance=None, pet=None, parent=None):
        super().__init__()
        self.root, self.appearance, self.pet = Path(root), appearance, pet
        self.session = D.SettingsSession(self.root)
        self._api_probe = None
        self._proofs, self._catalogues = set(), {}
        self._profile_id = self._preset_id = None
        self.setWindowTitle("配置 · 小日和")
        self.resize(980, 760)
        self.setMinimumSize(740, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        title = QLabel("让她更适合你")
        title.setObjectName("settingsTitle")
        layout.addWidget(title)
        layout.addWidget(hint("接口、思考、记忆和外观都在这里。只保存你改过的内容；人格管理仍是独立窗口。"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("未修改配置")
        self.status.setObjectName("settingsStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.reload_button = buttons.addButton("重新读取", QDialogButtonBox.ResetRole)
        self.save_button = buttons.addButton("保存更改", QDialogButtonBox.ApplyRole)
        self.save_button.setObjectName("primaryButton")
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        self.reload_button.clicked.connect(self.reload_all)
        self.save_button.clicked.connect(self.save_all)
        layout.addWidget(buttons)
        self._build_pages()
        if appearance:
            appearance.changed.connect(self._refresh_style)
        self._refresh_style()

    def _page(self, title):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 14, 8, 14)
        layout.setSpacing(12)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return layout

    def _group(self, layout, title):
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setSpacing(12)
        layout.addWidget(box)
        return form

    def _build_pages(self):
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            page.deleteLater()
        self._fields = []
        self._profile_id = self._preset_id = None
        self._thinking_cfg = deepcopy(self.session.values["thinking"])
        secrets = self.session.values["secrets"]
        self._profiles = deepcopy(secrets.get("model_profiles") or {"legacy": PC.legacy_profile(secrets)})
        self._initial_profiles = deepcopy(self._profiles)
        self._initial_connections = set()
        for profile in self._profiles.values():
            try:
                self._initial_connections.add(PC.from_profile(profile).fingerprint())
            except ValueError:
                pass
        self._default_id = secrets.get("default_model_profile") or next(iter(self._profiles))
        self._initial_default_id = self._default_id
        self._api_page()
        self._thinking_page()
        config = merge_defaults(D._load_json(self.root / "data/config.example.json"), self.session.values["config"])
        page = self._page("记忆与隐私")
        page.addWidget(hint("档位中的记忆预算优先于检索默认值。遗忘只降低检索权重；旧记忆整理需要另行执行。"))
        for key in ("retrieval", "speaker", "scoring", "compression", "privacy"):
            if key in config:
                self._config_group(page, "config", key, config[key])
        page.addStretch()
        page = self._page("搜索与文件")
        page.addWidget(hint("文件夹决定她能读写哪些资料。普通搜索不消耗 Tavily；境外资料按需使用它，额度为本安装的调用限制。"))
        self._config_group(page, "config", "tools", config.get("tools", {}))
        self._secret_field(self._group(page, "Tavily 凭据"), "tavily_api_key", "API key")
        self._config_group(page, "config", "knowledge", config.get("knowledge", {}))
        page.addStretch()
        self._appearance_page(config)
        self._personal_page()
        page = self._page("高级")
        page.addWidget(hint("自动选档的分数与关键词按你的问题习惯调整。当前没有配置的档位不会出现在可选项中。"))
        for key in ("auto_rules", "ui"):
            if isinstance(self._thinking_cfg.get(key), dict):
                self._config_group(page, "thinking", key, self._thinking_cfg[key])
        known = {"retrieval", "speaker", "scoring", "compression", "privacy", "tools", "knowledge", "live2d", "paths"}
        for key, value in config.items():
            if not key.startswith("_") and key not in known and isinstance(value, dict):
                self._config_group(page, "config", key, value)
        paths = self._group(page, "资料位置（供查看）")
        for name, value in config.get("paths", {}).items():
            if not name.startswith("_"):
                label = QLabel(str(value))
                label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                paths.addRow(name, label)
        page.addStretch()
        self.disable_default_buttons()

    def _api_page(self):
        page = self._page("模型与接口")
        page.addWidget(hint("填写接口真正接受的模型 ID。显示名称、服务商前缀和“最新模型”都不是调用名。新接口需获取模型列表或测试一次；DeepSeek 官方 ID 已预置。"))
        form = self._group(page, "对话方案")
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._load_profile)
        row = QHBoxLayout()
        row.addWidget(self.profile_combo, 1)
        for label, fn in (("新增", self._new_profile), ("移除", self._remove_profile)):
            button = QPushButton(label)
            button.clicked.connect(fn)
            row.addWidget(button)
        form.addRow("编辑方案", row)
        self.default_combo = QComboBox()
        form.addRow("默认使用", self.default_combo)
        self.profile_name = QLineEdit()
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com 或 http://localhost:11434/v1")
        self.api_key_edit = secret_edit()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.NoInsert)
        self.model_combo.addItems(PC.DEEPSEEK_MODELS)
        self.protocol_combo = combo([("DeepSeek 思考接口", "deepseek"), ("标准兼容接口", "compatible")])
        self.tools_box = QCheckBox("此模型支持工具调用")
        self.env_box = QCheckBox("沿用 DEEPSEEK / OPENAI 环境变量的优先级")
        form.addRow("方案名称", self.profile_name)
        form.addRow("API 根地址", self.base_url_edit)
        form.addRow("API key", secret_row(self.api_key_edit))
        form.addRow("模型 ID", self.model_combo)
        form.addRow("接口格式", self.protocol_combo)
        form.addRow("模型能力", self.tools_box)
        form.addRow("环境配置", self.env_box)
        self.api_env_hint = hint("")
        form.addRow("实际生效", self.api_env_hint)
        row = QHBoxLayout()
        self.fetch_models_button = QPushButton("获取模型 ID")
        self.test_api_button = QPushButton("测试当前方案")
        self.fetch_models_button.clicked.connect(lambda: self._check_api("models"))
        self.test_api_button.clicked.connect(lambda: self._check_api("probe"))
        row.addWidget(self.fetch_models_button)
        row.addWidget(self.test_api_button)
        form.addRow(row)
        form.addRow(hint("测试只发送固定短句，不发送人格、记忆或聊天内容；最多请求 64 tokens，可能产生少量费用。标准兼容接口不发送 DeepSeek 专属思考字段。"))
        self._profile_widgets = {"name": self.profile_name, "base_url": self.base_url_edit,
            "api_key": self.api_key_edit, "model": self.model_combo, "protocol": self.protocol_combo,
            "tools": self.tools_box, "use_environment": self.env_box}
        self._populate_profiles()
        for w in (self.base_url_edit, self.api_key_edit):
            w.textChanged.connect(self._connection_hint)
        self.env_box.toggled.connect(self._connection_hint)
        self.model_combo.currentTextChanged.connect(self._connection_hint)
        form = self._group(page, "读图接口")
        form.addRow(hint("图片仍通过独立读图接口处理，再将结果交给对话模型。也可以填写同一个支持视觉的服务。三项均留空即关闭读图。"))
        self.vision_edits = {}
        for key, label in (("vision_base_url", "API 根地址"), ("vision_api_key", "API key"), ("vision_model", "模型 ID")):
            if key == "vision_model":
                w = QComboBox()
                w.setEditable(True)
                w.setInsertPolicy(QComboBox.NoInsert)
                value = str(self.session.values["secrets"].get(key) or "")
                w.setCurrentText(value)
                form.addRow(label, w)
                self._fields.append(("secrets", (key,), w, value, "str"))
                self.vision_edits[key] = w
            else:
                self.vision_edits[key] = self._secret_field(form, key, label, password=key.endswith("api_key"))
        self.test_vision_button = QPushButton("核对读图模型 ID")
        self.test_vision_button.clicked.connect(lambda: self._check_api("models", vision=True))
        row = QHBoxLayout()
        row.addWidget(self.test_vision_button)
        button = QPushButton("测试读图接口")
        button.clicked.connect(lambda: self._check_api("vision", vision=True))
        row.addWidget(button)
        form.addRow(row)
        form.addRow(hint("读图测试只发送程序生成的示例图片，最多请求 64 tokens，可能产生少量费用。"))

    @staticmethod
    def _value(widget):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QComboBox):
            return widget.currentText().strip() if widget.isEditable() else widget.currentData()
        return widget.text().strip()

    def _populate_profiles(self, selected=None):
        selected = selected or self._profile_id or self._default_id
        for widget in (self.profile_combo, self.default_combo):
            widget.blockSignals(True)
            widget.clear()
            for key, profile in self._profiles.items():
                widget.addItem(profile.get("name", key), key)
        self.default_combo.setCurrentIndex(max(0, self.default_combo.findData(self._default_id)))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(selected)))
        for widget in (self.profile_combo, self.default_combo):
            widget.blockSignals(False)
        self._profile_id = None
        self._load_profile()

    def _store_profile(self):
        if self._profile_id is None:
            return
        profile = self._profiles[self._profile_id]
        for key, widget in self._profile_widgets.items():
            value = self._value(widget)
            if value != self._profile_view[key]:
                profile[key] = value
            elif key in self._profile_base:
                profile[key] = deepcopy(self._profile_base[key])
            else:
                profile.pop(key, None)

    def _load_profile(self, *_):
        self._store_profile()
        self._profile_id = self.profile_combo.currentData()
        p = self._profiles[self._profile_id]
        self._profile_base = deepcopy(p)
        for key, widget in self._profile_widgets.items():
            value = p.get(key, True if key == "tools" else False if key == "use_environment" else "")
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                if widget.isEditable():
                    widget.setCurrentText(str(value))
                else:
                    widget.setCurrentIndex(max(0, widget.findData(value)))
            else:
                widget.setText(str(value))
        self._profile_view = {k: self._value(w) for k, w in self._profile_widgets.items()}
        self._connection_hint()

    def _editing_connection(self, placeholder=False):
        profile = dict(self._profiles[self._profile_id])
        profile.update({k: self._value(w) for k, w in self._profile_widgets.items()})
        if placeholder and not profile.get("model"):
            profile["model"] = "list-probe"
        return PC.from_profile(profile)

    def _connection_hint(self, *_):
        try:
            c = self._editing_connection(placeholder=True)
            self.api_env_hint.setText(f"{c.base}  ·  {c.model}  ·  {c.source}")
        except (ValueError, AttributeError, KeyError):
            self.api_env_hint.setText("请补全地址与模型 ID。")

    def _new_profile(self):
        self._store_profile()
        name, ok = QInputDialog.getText(self, "新增接口方案", "给这套接口起个名字：")
        if not ok or not name.strip():
            return
        key = uuid.uuid4().hex[:12]
        self._profiles[key] = {"name": name.strip(), "base_url": "", "api_key": "", "model": "",
                               "protocol": "compatible", "tools": True, "use_environment": False}
        self._default_id = self.default_combo.currentData()
        self._populate_profiles(key)
        self._refresh_preset_profiles()

    def _remove_profile(self):
        if len(self._profiles) == 1:
            self.status.setText("至少保留一个对话方案。")
            return
        key = self.profile_combo.currentData()
        if QMessageBox.question(self, "移除方案", "只移除此接口方案，不删除聊天、人格或记忆。继续吗？") != QMessageBox.Yes:
            return
        self._profiles.pop(key)
        self._profile_id = None
        self._default_id = self.default_combo.currentData()
        if self._default_id == key:
            self._default_id = next(iter(self._profiles))
        self._populate_profiles()
        self._refresh_preset_profiles()

    def _thinking_page(self):
        page = self._page("思考档位")
        page.addWidget(hint("换档同时改变推理、回复和记忆预算。DeepSeek 的“认真 / 深究”均映射到 high，仍可通过预算区分。修改后切换编辑档位不会丢失草稿。"))
        form = self._group(page, "本次编辑")
        presets = self._thinking_cfg.get("presets", {})
        self.current_level_combo = combo([("自动判断", "auto")] + [(v.get("name", k), k) for k, v in presets.items()])
        self.current_level_combo.setCurrentIndex(max(0, self.current_level_combo.findData(self._thinking_cfg.get("current", "auto"))))
        form.addRow("默认档位", self.current_level_combo)
        self.preset_combo = combo([(v.get("name", k), k) for k, v in presets.items()])
        form.addRow("编辑档位", self.preset_combo)
        self.preset_profile = QComboBox()
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("留空使用所选方案的模型")
        form.addRow("模型方案", self.preset_profile)
        form.addRow("单独指定模型 ID", self.model_edit)
        self.reasoning_combo = combo([("不思考", "none"), ("轻量", "low"), ("认真", "medium"), ("深入", "high"), ("最大", "max")])
        self.search_combo = combo([("关闭", "off"), ("需要时搜索", "on_demand"), ("优先核实", "eager"), ("每次搜索", "always")])
        form.addRow("推理投入", self.reasoning_combo)
        form.addRow("联网方式", self.search_combo)
        self._preset_widgets = {"model_profile": self.preset_profile, "model_override": self.model_edit,
                                "reasoning_effort": self.reasoning_combo, "search": self.search_combo}
        for key, label, lo, hi in (("max_tokens", "输出预算（包括思考）", 1, 1000000),
                                  ("memory_budget", "记忆预算（tokens）", 1, 1000000),
                                  ("memory_entries", "最多记忆条数", 1, 10000),
                                  ("memory_floor", "近期记忆兜底条数", 0, 10000)):
            w = QSpinBox()
            w.setRange(lo, hi)
            form.addRow(label, w)
            self._preset_widgets[key] = w
        for key, label, lo, hi in (("temperature", "随机性（DeepSeek 思考时不生效）", 0., 2.),
                                  ("frequency_penalty", "重复词惩罚", -2., 2.), ("presence_penalty", "重复话题惩罚", -2., 2.)):
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setSingleStep(.05)
            form.addRow(label, w)
            self._preset_widgets[key] = w
        w = QCheckBox("回答前复核")
        form.addRow("自检提示", w)
        self._preset_widgets["self_check"] = w
        w = QLineEdit()
        form.addRow("篇幅提示", w)
        self._preset_widgets["verbosity"] = w
        self.limit_tools = QCheckBox("只允许下列工具（关闭时沿用默认工具集）")
        form.addRow("工具权限", self.limit_tools)
        self.tools_edit = QPlainTextEdit()
        self.tools_edit.setMaximumHeight(110)
        self.tools_edit.setPlaceholderText("每行一个调用名，例如 get_time、recall、web_search")
        self.tools_edit.setToolTip("常用：get_time 时间、recall 回忆、remember 记忆、web_search 搜索、mood 心情、agreement 约定、fs_read 读文件、fs_write 写文件。勾选后留空表示禁用全部工具。")
        self.limit_tools.toggled.connect(self.tools_edit.setEnabled)
        form.addRow("允许的工具", self.tools_edit)
        self._refresh_preset_profiles()
        self.preset_combo.currentIndexChanged.connect(self._load_preset_editor)
        self._load_preset_editor()
        page.addStretch()

    def _refresh_preset_profiles(self):
        if not hasattr(self, "preset_profile"):
            return
        current = self.preset_profile.currentData()
        self.preset_profile.clear()
        self.preset_profile.addItem("使用默认方案", "")
        for key, p in self._profiles.items():
            self.preset_profile.addItem(p.get("name", key), key)
        if current and current not in self._profiles:
            self.preset_profile.addItem("已移除的方案（请重新选择）", current)
        self.preset_profile.setCurrentIndex(max(0, self.preset_profile.findData(current)))

    def _store_preset_editor(self):
        if self._preset_id is None:
            return
        params = self._thinking_cfg["presets"][self._preset_id].setdefault("params", {})
        for key, w in self._preset_widgets.items():
            value = self._value(w)
            if value != self._preset_view[key]:
                params[key] = value
            elif key in self._preset_base:
                params[key] = deepcopy(self._preset_base[key])
            else:
                params.pop(key, None)
        tool_value = [s.strip() for s in self.tools_edit.toPlainText().replace(",", "\n").splitlines() if s.strip()] if self.limit_tools.isChecked() else None
        if tool_value != self._tools_view:
            if tool_value is None:
                params.pop("tools", None)
            else:
                params["tools"] = tool_value
        elif "tools" in self._preset_base:
            params["tools"] = deepcopy(self._preset_base["tools"])
        else:
            params.pop("tools", None)

    def _load_preset_editor(self, *_):
        self._store_preset_editor()
        self._preset_id = self.preset_combo.currentData()
        p = self._thinking_cfg.get("presets", {}).get(self._preset_id, {}).get("params", {})
        self._preset_base = deepcopy(p)
        self._refresh_preset_profiles()
        for key, w in self._preset_widgets.items():
            default = False if isinstance(w, QCheckBox) else 0 if isinstance(w, (QSpinBox, QDoubleSpinBox)) else ""
            value = p.get(key, default)
            if key == "model_override" and key not in p and not self.session.original["secrets"].get("model_profiles"):
                old = PC.resolve(self.session.original["secrets"], p).model
                default = PC.resolve(self.session.original["secrets"]).model
                value = old if old != default else ""
            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setValue(value)
            elif isinstance(w, QComboBox):
                if value and w.findData(value) < 0:
                    w.addItem(str(value), value)
                w.setCurrentIndex(max(0, w.findData(value)))
            else:
                w.setText(str(value))
        self._preset_view = {k: self._value(w) for k, w in self._preset_widgets.items()}
        self._tools_view = deepcopy(p.get("tools"))
        self.limit_tools.setChecked(isinstance(self._tools_view, list))
        self.tools_edit.setEnabled(self.limit_tools.isChecked())
        self.tools_edit.setPlainText("\n".join(self._tools_view or []))

    def _secret_field(self, form, key, label, password=True):
        w = secret_edit() if password else QLineEdit()
        value = str(self.session.values["secrets"].get(key) or "")
        w.setText(value)
        form.addRow(label, secret_row(w) if password else w)
        self._fields.append(("secrets", (key,), w, value, "str"))
        return w

    def _config_group(self, layout, document, key, values):
        form = self._group(layout, GROUPS.get(key, LABELS.get(key, key)))
        self._form_fields(form, document, values, (key,))

    def _form_fields(self, form, document, values, prefix):
        for key, value in values.items():
            if key.startswith("_"):
                continue
            trail = prefix + (key,)
            title = LABELS.get(key, key)
            explanation = values.get("_" + key, "")
            if isinstance(value, dict):
                form.addRow(hint(title))
                self._form_fields(form, document, value, trail)
                continue
            kind = "str"
            choices = CHOICES.get(".".join(trail))
            if choices:
                w = combo(choices)
                if w.findData(value) < 0:
                    w.addItem(str(value), value)
                w.setCurrentIndex(w.findData(value))
            elif isinstance(value, bool):
                w = QCheckBox()
                w.setChecked(value)
            elif isinstance(value, int):
                w = QSpinBox()
                w.setRange(-1000000 if "signals" in trail else 0, 10000000)
                w.setValue(value)
            elif isinstance(value, float):
                w = QDoubleSpinBox()
                w.setDecimals(4)
                w.setRange(0, 1000000)
                w.setSingleStep(.01)
                w.setValue(value)
            elif isinstance(value, list):
                w = QPlainTextEdit()
                w.setMaximumHeight(120)
                if all(isinstance(v, str) for v in value):
                    kind = "lines"
                    w.setPlainText("\n".join(value))
                else:
                    kind = "json"
                    w.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            else:
                w = QLineEdit(str(value or ""))
            if explanation:
                w.setToolTip(str(explanation))
            if key in ("fs_root", "docs_dir", "model") and isinstance(w, QLineEdit):
                row = QHBoxLayout()
                row.addWidget(w, 1)
                browse = QPushButton("选择…")
                browse.clicked.connect(lambda _=False, edit=w, file=(key == "model"): self._browse(edit, file))
                row.addWidget(browse)
                form.addRow(title, row)
            else:
                form.addRow(title, w)
            self._fields.append((document, trail, w, self._value(w), kind))

    def _browse(self, edit, file=False):
        value = QFileDialog.getOpenFileName(self, "选择模型", str(self.root), "Live2D (*.model3.json)")[0] if file else QFileDialog.getExistingDirectory(self, "选择文件夹", str(self.root))
        if value:
            edit.setText(value)

    def _appearance_page(self, config):
        page = self._page("外观与桌宠")
        form = self._group(page, "窗口外观（保存后立即生效）")
        effective = self.appearance.settings if self.appearance else self.session.values["appearance"]
        self.theme_combo = combo(list((v, k) for k, v in THEME_NAMES.items()))
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(effective.get("theme", "touhou"))))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(11, 18)
        self.font_spin.setValue(effective.get("font_size", 13))
        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(.3, 1.)
        self.opacity_spin.setSingleStep(.05)
        self.opacity_spin.setValue(effective.get("glass_opacity", .65))
        for key, title, w in (("theme", "主题", self.theme_combo), ("font_size", "字号（px）", self.font_spin), ("glass_opacity", "玻璃不透明度", self.opacity_spin)):
            form.addRow(title, w)
            self._fields.append(("appearance", (key,), w, self._value(w), "str"))
        form.addRow(hint("配置与人格窗口使用系统界面字体；Windows 与 Codex 的系统字体一致，使用 Segoe UI 和中文字体回退。"))
        if "live2d" in config:
            self._config_group(page, "config", "live2d", config["live2d"])
        page.addStretch()

    def _personal_page(self):
        page = self._page("人格与连接")
        form = self._group(page, "独立的人格管理")
        form.addRow(hint("人格、情绪文案、行为边界和用户档案在独立窗口中编辑。这里保存接口和外观，不会重写人格文件。"))
        button = QPushButton("打开人格与个性化管理")
        button.clicked.connect(self._open_persona)
        form.addRow(button)
        form = self._group(page, "本机 QQ 凭据")
        remote = D._load_json(self.root / "data/qq_remote.json").get("enabled") is True
        if remote:
            form.addRow(hint("当前连接由已有连接模块维护。本页只查看状态，不更改连接参数，也不会启动另一个网关。"))
            button = QPushButton("查看连接状态")
            button.setEnabled(bool(self.pet and hasattr(self.pet, "_show_qq_dashboard")))
            if button.isEnabled():
                button.clicked.connect(self.pet._show_qq_dashboard)
            form.addRow(button)
        else:
            self._secret_field(form, "qq_appid", "App ID", password=False)
            self._secret_field(form, "qq_secret", "Client Secret")
            form.addRow(hint("仅用于本机 QQ 接入；保存后需自行重启本机 QQ 桥，配置窗口不会自动启停连接。"))
        form = self._group(page, "本机资料")
        for name, relative in (("打开私人配置文件夹", "data"), ("打开人格文件夹", "persona")):
            button = QPushButton(name)
            button.clicked.connect(lambda _=False, p=self.root / relative: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            form.addRow(button)
        page.addStretch()

    def _open_persona(self):
        from persona_ui import open_persona_manager
        return open_persona_manager(self, root=self.root, appearance=self.appearance,
                                    on_changed=getattr(self.pet, "_persona_changed", None))

    def _require_model(self, c):
        if c.fingerprint() in self._initial_connections or c.fingerprint() in self._proofs:
            return
        if PC.official_deepseek(c.base) and c.model in PC.DEEPSEEK_MODELS:
            return
        catalogue = self._catalogues.get((c.base, self._key_digest(c), c.protocol), ())
        if c.model in catalogue:
            return
        raise ValueError(f"模型 {c.model} 尚未核实。请从此接口获取模型 ID，或测试准确调用名后再保存。")

    @staticmethod
    def _key_digest(c):
        import hashlib
        return hashlib.sha256(c.key.encode()).hexdigest()

    def _collect(self, validate=False):
        self._store_profile()
        self._store_preset_editor()
        values = deepcopy(self.session.original)
        self._thinking_cfg["current"] = self.current_level_combo.currentData() or "auto"
        values["thinking"] = deepcopy(self._thinking_cfg)
        profiles_changed = self._profiles != self._initial_profiles or self.default_combo.currentData() != self._initial_default_id
        routes = any(p.get("params", {}).get("model_profile") or p.get("params", {}).get("model_override") for p in self._thinking_cfg.get("presets", {}).values())
        if profiles_changed or routes:
            values["secrets"]["model_profiles"] = deepcopy(self._profiles)
            values["secrets"]["default_model_profile"] = self.default_combo.currentData()
            if not self.session.original["secrets"].get("model_profiles"):
                default = PC.resolve(self.session.original["secrets"]).model
                for preset in values["thinking"].get("presets", {}).values():
                    params = preset.get("params", {})
                    old = PC.resolve(self.session.original["secrets"], params).model
                    if old != default and "model_override" not in params and not params.get("model_profile"):
                        params["model_override"] = old
        for document, trail, w, initial, kind in self._fields:
            value = self._value(w)
            if value == initial:
                continue
            if kind == "lines":
                value = [v.strip() for v in value.splitlines() if v.strip()]
            elif kind == "json":
                value = json.loads(value)
                if not isinstance(value, list):
                    raise ValueError("列表设置需要保留列表结构。")
            D._set(values[document], trail, value)
        if validate:
            if profiles_changed or routes:
                for p in self._profiles.values():
                    self._require_model(PC.from_profile(p))
                for preset in values["thinking"].get("presets", {}).values():
                    self._require_model(PC.resolve(values["secrets"], preset.get("params", {})))
            old_vision = {k: self.session.original["secrets"].get(k, "") for k in self.vision_edits}
            new_vision = {k: values["secrets"].get(k, "") for k in self.vision_edits}
            if old_vision != new_vision and any(new_vision.values()):
                if not all(new_vision.values()):
                    raise ValueError("读图接口需要同时填写地址、密钥和模型 ID，或将三项都留空关闭。")
                self._require_model(PC.vision_connection(values["secrets"]))
            proxy = values["config"].get("tools", {}).get("proxy", "auto")
            if proxy not in ("auto", "off", "") and not str(proxy).startswith(("http://", "https://", "socks5://", "socks5h://")):
                raise ValueError("代理请填 auto、off 或完整的 http / socks5 地址。")
            tools = values["config"].get("tools", {})
            if tools.get("search_backend") == "searxng" and not str(tools.get("searxng_url", "")).startswith(("http://", "https://")):
                raise ValueError("使用 SearXNG 时需要填写它的完整地址。")
        return values

    def save_all(self):
        try:
            self.session.values = self._collect(validate=True)
            changed = self.session.save()
            if not changed:
                self.status.setText("没有需要保存的更改。")
                return
            self._reload_runtime()
            self.saved.emit()
            self.status.setText("已保存更改。新请求使用新配置；桌宠形象与尺寸需重启后生效。")
            selected = self.tabs.currentIndex()
            self._build_pages()
            self.tabs.setCurrentIndex(selected)
        except (OSError, ValueError, TypeError) as error:
            self.status.setText("未保存：" + str(error))
            QMessageBox.warning(self, "配置没有保存", str(error))

    def _reload_runtime(self):
        module = sys.modules.get("memory")
        if module and Path(module.ROOT).resolve() == self.root.resolve():
            module.reload_config()
            thinking = sys.modules.get("thinking")
            if thinking:
                thinking._cache["mtime"] = 0
            proxy = sys.modules.get("proxy")
            if proxy:
                proxy._cache["value"] = "\x00"
            tools = sys.modules.get("tools")
            if tools:
                tools.TOOLS_CFG = module.CFG.get("tools", {})
            knowledge = sys.modules.get("knowledge")
            if knowledge:
                knowledge.KCFG = module.CFG.get("knowledge", {})
                knowledge.DOCS_DIR = module.ROOT / knowledge.KCFG.get("docs_dir", "knowledge")
        if self.appearance:
            self.appearance.reload()
        if self.pet and hasattr(self.pet, "panel"):
            self.pet.panel.update()

    def reload_all(self):
        if self._running():
            return
        if self._collect() != self.session.original and QMessageBox.question(self, "重新读取", "放弃尚未保存的更改，并读取文件中的设置？") != QMessageBox.Yes:
            return
        try:
            self.session.reload()
            self._build_pages()
            self.status.setText("已重新读取本机设置。")
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法读取", str(error))

    def _check_api(self, operation, vision=False):
        if self._running():
            return
        try:
            c = PC.vision_connection({k: self._value(w) for k, w in self.vision_edits.items()}, listing=operation == "models") if vision else self._editing_connection(placeholder=operation == "models")
        except ValueError as error:
            self.status.setText(str(error))
            return
        self._api_probe = ApiProbe(c, operation, self)
        self._api_probe.done.connect(lambda ok, result: self._probe_done(c, operation, vision, ok, result))
        self._api_probe.finished.connect(self._probe_finished)
        self.tabs.setEnabled(False)
        self.save_button.setEnabled(False)
        self.reload_button.setEnabled(False)
        self.status.setText("正在核对接口…")
        self._api_probe.start()

    def _probe_done(self, c, operation, vision, ok, result):
        if not ok:
            self.status.setText(str(result))
            return
        if operation == "models":
            self._catalogues[(c.base, self._key_digest(c), c.protocol)] = result
            target = self.vision_edits["vision_model"] if vision else self.model_combo
            selected = target.currentText()
            target.clear()
            target.addItems(result)
            target.setCurrentText(selected)
            suffix = "当前读图模型在列表中。" if vision and c.model in result else "请选择列表中的模型。" if not vision else "当前读图模型不在列表中，请检查调用名。"
            self.status.setText(f"已取得 {len(result)} 个准确模型 ID。" + suffix)
        else:
            self._proofs.add(c.fingerprint())
            self.status.setText(str(result))

    def _probe_finished(self):
        self.tabs.setEnabled(True)
        self.save_button.setEnabled(True)
        self.reload_button.setEnabled(True)
        worker = self._api_probe
        self._api_probe = None
        worker.deleteLater()

    def _running(self):
        # Keep the window busy until its queued finished callback is handled;
        # otherwise a fast reopen/retry can delete the next check's live thread.
        return self._api_probe is not None

    def _refresh_style(self):
        apply_dialog_style(self, self.appearance)

    def _can_close(self):
        if self._running():
            self.status.setText("正在检查接口，请等待检查结束后关闭。")
            return False
        try:
            dirty = self._collect() != self.session.original
        except (ValueError, TypeError):
            dirty = True
        return not dirty or QMessageBox.question(
            self, "尚未保存", "放弃尚未保存的更改并关闭？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


def open_settings(root, appearance=None, pet=None):
    return show_configuration_window(root, "configuration",
                                     lambda: SettingsDialog(root, appearance, pet))
