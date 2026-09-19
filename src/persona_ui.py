"""Small editor for the local persona library."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
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
        right.addWidget(self.editor, 1)
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
        for i, item in enumerate(self.items):
            label = item["name"] + ("  · 当前" if item["active"] else "")
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
        avatar = self.items[row].get("avatar")
        if avatar and Path(avatar).exists():
            pm = QPixmap(avatar).scaled(self.avatar.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.avatar.setPixmap(pm)
            self.avatar.setText("")
        else:
            self.avatar.clear()
            self.avatar.setText("暂无头像")

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
        path, _ = QFileDialog.getOpenFileName(self, "导入 SOUL.md", "", "Markdown (*.md);;所有文件 (*)")
        if not path:
            return
        try:
            item = self.manager.import_soul(path)
            self._reload(item["id"])
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
