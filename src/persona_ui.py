"""Small editor for the local persona library."""
from __future__ import annotations

from pathlib import Path
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QTabWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QMessageBox, QPushButton, QPlainTextEdit, QVBoxLayout,
)

from persona_manager import PersonaManager
from settings_style import apply_dialog_style
from configuration_windows import ConfigurationDialog, show_configuration_window
import settings_data as SETTINGS


class PersonaDialog(ConfigurationDialog):
    changed = Signal()

    def __init__(self, parent=None, root=None, appearance=None):
        super().__init__()
        self.manager = PersonaManager(root or Path(__file__).resolve().parent.parent)
        self.appearance = appearance or getattr(parent, 'appearance', None)
        self._drafts = {}
        self._originals = {}
        self._loading = False
        self._file_originals = {}
        self._mood_drafts = {}
        self._mood_originals = {}
        self._shown_mood = None
        self.current_id = None
        self.setWindowTitle("人格管理")
        self.resize(920, 700)
        self.setMinimumSize(700, 500)
        root = QHBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(20)
        left = QVBoxLayout()
        title = QLabel("人格库")
        title.setObjectName('settingsTitle')
        left.addWidget(title)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        left.addWidget(self.list, 1)
        new_btn = QPushButton("新建人格")
        new_btn.clicked.connect(self._new)
        left.addWidget(new_btn)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        self.avatar = QLabel("暂无头像")
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setFixedSize(92, 92)
        self.avatar.setObjectName('personaAvatar')
        right.addWidget(self.avatar, 0, Qt.AlignLeft)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("在这里编辑 SOUL.md…")
        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "人格")
        self.mood_editor = QPlainTextEdit()
        self.mood_editor.setPlaceholderText('情绪方案 JSON；每个状态包含 voice 和 hours。')
        self.tabs.addTab(self.mood_editor, "情绪文案")
        self.boundary_editor = QPlainTextEdit()
        self.boundary_editor.setPlaceholderText('当前角色实际使用的行为边界；仅按保存按钮后修改。')
        self.tabs.addTab(self.boundary_editor, '行为边界')
        self.profile_editor = QPlainTextEdit()
        self.profile_editor.setPlaceholderText('关于你的稳定事实与偏好。所有人格共用这份用户档案。')
        self.tabs.addTab(self.profile_editor, '用户档案')
        self._extra_paths = {}
        self._extra_original = {}
        self._extra_drafts = {}
        right.addWidget(self.tabs, 1)
        right.addWidget(QLabel("使用的情绪方案（每个人格单独保存心情）"))
        self.mood_combo = QComboBox()
        self.mood_combo.currentTextChanged.connect(self._mood_selected)
        right.addWidget(self.mood_combo)
        buttons = QHBoxLayout()
        for label, callback in (("导入 SOUL", self._import_soul), ("导入头像", self._import_avatar),
                                ("保存全部修改", self._save), ("设为当前", self._activate)):
            b = QPushButton(label)
            b.clicked.connect(callback)
            buttons.addWidget(b)
        right.addLayout(buttons)
        root.addLayout(right, 3)
        self._reload()
        if self.appearance:
            self.appearance.changed.connect(self._refresh_style)
        self._refresh_style()
        self.disable_default_buttons()

    def _refresh_style(self):
        apply_dialog_style(self, self.appearance)

    def _capture_draft(self):
        if self.current_id and not self._loading:
            self._stash_mood()
            self._drafts[self.current_id] = (self.editor.toPlainText(), self.mood_combo.currentText(),
                                            self.mood_editor.toPlainText())
            for name, editor in (('boundary', self.boundary_editor), ('profile', self.profile_editor)):
                if name in self._extra_paths:
                    self._extra_drafts[self._extra_paths[name]] = editor.toPlainText()

    def _load_extra(self):
        import persona_runtime as PR
        base = self.manager.root
        found = PR.files(base, self.current_id).get('BOUNDARIES.md')
        boundary = found if found and base / 'persona' in found.parents else base / 'persona/BOUNDARIES.md'
        config = SETTINGS.load_config(base)
        self._extra_paths = {'boundary': boundary,
            'profile': base / config.get('paths', {}).get('profile', 'persona/PROFILE.md')}
        for name, editor in (('boundary', self.boundary_editor), ('profile', self.profile_editor)):
            path = self._extra_paths[name]
            if path not in self._extra_original:
                text = self._track(path) or ''
                self._extra_original[path] = text
            editor.setPlainText(self._extra_drafts.get(path, self._extra_original[path]))

    def _reload(self, select_id=None):
        self._capture_draft()
        self.items = self.manager.list_personas()
        self.list.blockSignals(True)
        self.list.clear()
        row = 0
        seen_names = {}
        for i, item in enumerate(self.items):
            name = item["name"]
            seen_names[name] = seen_names.get(name, 0) + 1
            label = name + (f"（同名 {seen_names[name]}）" if seen_names[name] > 1 else "") + ("  · 当前" if item["active"] else "")
            self.list.addItem(label)
            if select_id == item["id"] or (select_id is None and item["active"]):
                row = i
        if self.items:
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        if self.items:
            self._select(row)

    def _select(self, row):
        if not (0 <= row < len(self.items)):
            return
        self._capture_draft()
        self._loading = True
        self.current_id = self.items[row]["id"]
        self._shown_mood = None
        folder = self.manager.characters_dir / self.current_id
        self._track(folder / "SOUL.md")
        self._track(folder / "MOODS.json")
        self._track(self.manager.root / "data/persona_moods.json")
        self._mood_originals.setdefault(self.current_id, self.manager.read_moods(self.current_id))
        self.editor.setPlainText(self.manager.read_soul(self.current_id))
        self.mood_combo.blockSignals(True)
        self.mood_combo.clear()
        self.mood_combo.addItems(self.manager.mood_profiles())
        self.mood_combo.setCurrentText(self.manager.mood_binding(self.current_id))
        self.mood_combo.blockSignals(False)
        self._mood_selected(self.mood_combo.currentText())
        initial = (self.editor.toPlainText(), self.mood_combo.currentText(), self.mood_editor.toPlainText())
        self._originals.setdefault(self.current_id, initial)
        if self.current_id in self._drafts:
            soul, profile, moods = self._drafts[self.current_id]
            self.editor.setPlainText(soul)
            self.mood_combo.setCurrentText(profile)
            self.mood_editor.setPlainText(moods)
        self._load_extra()
        self._loading = False
        avatar = self.items[row].get("avatar")
        if avatar and Path(avatar).exists():
            pm = QPixmap(avatar).scaled(self.avatar.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.avatar.setPixmap(pm)
            self.avatar.setText("")
        else:
            self.avatar.clear()
            self.avatar.setText("暂无头像")

    def _track(self, path):
        if path not in self._file_originals:
            self._file_originals[path] = path.read_text(encoding='utf-8') if path.exists() else None
        return self._file_originals[path]

    def _stash_mood(self):
        if self._shown_mood == self.current_id and not self._loading:
            self._mood_drafts[self.current_id] = self.mood_editor.toPlainText()

    def _mood_selected(self, profile):
        self._stash_mood()
        self._shown_mood = profile
        text = self._mood_drafts.get(self.current_id) if profile == self.current_id else None
        self.mood_editor.setPlainText(text if text is not None else self.manager.read_moods(profile))
        self.mood_editor.setReadOnly(profile != self.current_id)

    def _new(self):
        name, ok = QInputDialog.getText(self, "新建人格", "名称：")
        if not ok or not name.strip():
            return
        try:
            item = self.manager.create(name)
            self._reload(item["id"])
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", str(e))

    def _save(self):
        if not self.current_id:
            return
        try:
            self._capture_draft()
            changes = {}
            bindings_path = self.manager.root / 'data/persona_moods.json'
            bindings = json.loads(self._file_originals.get(bindings_path) or '{}')
            bindings_changed = False
            for pid, current in self._drafts.items():
                original = self._originals[pid]
                folder = self.manager.characters_dir / pid
                if current[0] != original[0]:
                    if not current[0].strip():
                        raise ValueError('人格内容不能为空。')
                    path = folder / 'SOUL.md'
                    changes[path] = (self._file_originals[path], current[0].strip() + '\n')
                if current[1] != original[1]:
                    if current[1] not in self.manager.mood_profiles():
                        raise ValueError('找不到所选的情绪方案。')
                    bindings[pid] = current[1]
                    bindings_changed = True
            if bindings_changed:
                changes[bindings_path] = (self._file_originals[bindings_path], json.dumps(bindings, ensure_ascii=False, indent=2) + '\n')
            for pid, text in self._mood_drafts.items():
                if text != self._mood_originals[pid]:
                    data = self.manager.validate_moods(text)
                    path = self.manager.characters_dir / pid / 'MOODS.json'
                    changes[path] = (self._file_originals[path], json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            for path, text in self._extra_drafts.items():
                if text != self._extra_original[path]:
                    changes[path] = (self._file_originals[path], text)
            SETTINGS.save_texts(self.manager.root, changes)
            selected = self.current_id
            self.current_id = None
            for cache in (self._drafts, self._originals, self._file_originals, self._mood_drafts,
                          self._mood_originals, self._extra_original, self._extra_drafts):
                cache.clear()
            self._reload(selected)
            self.changed.emit()
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", str(e))

    def _activate(self):
        if not self.current_id:
            return
        try:
            self.manager.set_active(self.current_id)
            self._reload(self.current_id)
            self.changed.emit()
            QMessageBox.information(self, "人格管理", "已经切换。之后的新对话会使用这个人格。")
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", str(e))

    def _import_soul(self):
        if not self.current_id:
            QMessageBox.information(self, "人格管理", "请先选择一个角色；导入新角色时先点击“新建人格”。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入 SOUL.md", "", "Markdown (*.md);;所有文件 (*)")
        if not path:
            return
        selected = next(item for item in self.items if item["id"] == self.current_id)
        answer = QMessageBox.question(
            self, "确认导入人格",
            f"用所选文件覆盖“{selected['name']}”（{self.current_id}）的 SOUL？\n"
            "旧 SOUL 会先备份；当前人格文本框中未保存的修改会被替换。\n"
            "角色、头像、边界、情绪设置和当前使用的角色保持不变。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            item = self.manager.import_soul(path, self.current_id, overwrite=True)
            # Refresh only the imported SOUL and its label; keep unsaved mood edits.
            row = self.list.currentRow()
            self.items[row] = item
            seen_names = {}
            for index, entry in enumerate(self.items):
                name = entry["name"]
                seen_names[name] = seen_names.get(name, 0) + 1
                label = name + (f"（同名 {seen_names[name]}）" if seen_names[name] > 1 else "")
                self.list.item(index).setText(label + ("  · 当前" if entry["active"] else ""))
            text = self.manager.read_soul(item["id"])
            self.editor.setPlainText(text)
            path = self.manager.characters_dir / self.current_id / 'SOUL.md'
            self._file_originals[path] = path.read_text(encoding='utf-8')
            old = self._originals[self.current_id]
            self._originals[self.current_id] = (text, old[1], old[2])
            self._capture_draft()
            self.changed.emit()
            message = "已更新所选角色的 SOUL。"
            if item["backup"]:
                message += f"\n旧文件备份：{item['backup']}"
            else:
                message += "文件内容相同，无需更换或新增备份。"
            QMessageBox.information(self, "人格管理", message)
        except (ValueError, OSError, UnicodeError) as e:
            QMessageBox.information(self, "人格管理", f"导入失败：{e}")

    def _can_close(self):
        self._capture_draft()
        dirty = any(v != self._originals.get(k) for k, v in self._drafts.items()) or any(
            v != self._extra_original.get(k) for k, v in self._extra_drafts.items()) or any(
            v != self._mood_originals.get(k) for k, v in self._mood_drafts.items())
        return not dirty or QMessageBox.question(
            self, '尚未保存', '放弃尚未保存的人格或档案更改并关闭？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _import_avatar(self):
        if not self.current_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入人格头像", "", "图片 (*.png *.jpg *.jpeg *.webp);;所有文件 (*)")
        if not path:
            return
        try:
            self.manager.import_avatar(path, self.current_id)
            self._reload(self.current_id)
            self.changed.emit()
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", f"导入失败：{e}")


def open_persona_manager(parent=None, root=None, appearance=None, on_changed=None):
    root = Path(root or Path(__file__).resolve().parent.parent)

    def create():
        dialog = PersonaDialog(parent, root=root, appearance=appearance)
        if on_changed is not None:
            dialog.changed.connect(on_changed)
        return dialog

    return show_configuration_window(root, 'persona', create)
