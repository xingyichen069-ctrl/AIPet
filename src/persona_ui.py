"""Small editor for the local persona library."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QTabWidget, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QMessageBox, QPushButton, QPlainTextEdit, QVBoxLayout,
)

from persona_manager import PersonaManager


class PersonaDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = PersonaManager(Path(__file__).resolve().parent.parent)
        self.current_id = None
        self.setWindowTitle("人格管理")
        self.resize(760, 520)
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("人格库"))
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
        self.avatar.setStyleSheet("border:1px solid #d8c9be; border-radius:10px; padding:4px;")
        right.addWidget(self.avatar, 0, Qt.AlignLeft)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("在这里编辑 SOUL.md…")
        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "人格")
        self.mood_editor = QPlainTextEdit()
        self.mood_editor.setPlaceholderText('情绪方案 JSON；每个状态包含 voice 和 hours。')
        self.tabs.addTab(self.mood_editor, "情绪文案")
        right.addWidget(self.tabs, 1)
        right.addWidget(QLabel("使用的情绪方案（每个人格单独保存心情）"))
        self.mood_combo = QComboBox()
        self.mood_combo.currentTextChanged.connect(self._mood_selected)
        right.addWidget(self.mood_combo)
        buttons = QHBoxLayout()
        for label, callback in (("导入 SOUL", self._import_soul), ("导入头像", self._import_avatar),
                                ("保存", self._save), ("设为当前", self._activate)):
            b = QPushButton(label)
            b.clicked.connect(callback)
            buttons.addWidget(b)
        right.addLayout(buttons)
        root.addLayout(right, 3)
        self._reload()

    def _reload(self, select_id=None):
        self.items = self.manager.list_personas()
        self.list.blockSignals(True)
        self.list.clear()
        row = 0
        seen_names = {}
        for i, item in enumerate(self.items):
            name = item["name"]
            seen_names[name] = seen_names.get(name, 0) + 1
            label = name + (f"（同名 {seen_names[name]}）" if seen_names[name] > 1 else "")
            label += "  · 当前" if item["active"] else ""
            self.list.addItem(label)
            if select_id == item["id"] or (select_id is None and item["active"]):
                row = i
        self.list.blockSignals(False)
        if self.items:
            self.list.setCurrentRow(row)
            self._select(row)

    def _select(self, row):
        if not (0 <= row < len(self.items)):
            return
        self.current_id = self.items[row]["id"]
        self.editor.setPlainText(self.manager.read_soul(self.current_id))
        self.mood_combo.blockSignals(True)
        self.mood_combo.clear()
        self.mood_combo.addItems(self.manager.mood_profiles())
        self.mood_combo.setCurrentText(self.manager.mood_binding(self.current_id))
        self.mood_combo.blockSignals(False)
        self._mood_selected(self.mood_combo.currentText())
        avatar = self.items[row].get("avatar")
        if avatar and Path(avatar).exists():
            pm = QPixmap(avatar).scaled(self.avatar.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.avatar.setPixmap(pm)
            self.avatar.setText("")
        else:
            self.avatar.clear()
            self.avatar.setText("暂无头像")

    def _mood_selected(self, profile):
        self.mood_editor.setPlainText(self.manager.read_moods(profile))
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
            profile = self.mood_combo.currentText()
            if profile == self.current_id and self.mood_editor.toPlainText().strip() != "{}":
                self.manager.save_moods(self.current_id, self.mood_editor.toPlainText())
            self.manager.set_mood_profile(self.current_id, profile)
            self.manager.save_soul(self.current_id, self.editor.toPlainText())
            self._reload(self.current_id)
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", str(e))

    def _activate(self):
        if not self.current_id:
            return
        try:
            self.manager.set_active(self.current_id)
            self._reload(self.current_id)
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
            self.editor.setPlainText(self.manager.read_soul(item["id"]))
            message = "已更新所选角色的 SOUL。"
            if item["backup"]:
                message += f"\n旧文件备份：{item['backup']}"
            else:
                message += "文件内容相同，无需更换或新增备份。"
            QMessageBox.information(self, "人格管理", message)
        except (ValueError, OSError, UnicodeError) as e:
            QMessageBox.information(self, "人格管理", f"导入失败：{e}")

    def _import_avatar(self):
        if not self.current_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入人格头像", "", "图片 (*.png *.jpg *.jpeg *.webp);;所有文件 (*)")
        if not path:
            return
        try:
            self.manager.import_avatar(path, self.current_id)
            self._reload(self.current_id)
        except (ValueError, OSError) as e:
            QMessageBox.information(self, "人格管理", f"导入失败：{e}")


def open_persona_manager(parent=None) -> bool:
    dialog = PersonaDialog(parent)
    dialog.exec()
    return True
