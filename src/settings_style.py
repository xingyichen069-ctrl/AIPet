"""Shared settings/persona appearance, using the desktop's active palette."""
import sys
from pathlib import Path
from PySide6.QtGui import QFont, QFontDatabase
from ui_theme import is_daytime, theme_roles, touhou_palette


def apply_dialog_style(dialog, appearance=None):
    # Codex 26.930 Windows uses the system UI stack ending in Segoe UI.
    # Use the installed system font; do not redistribute app-bundled fonts.
    font = QFont("Segoe UI") if sys.platform == "win32" else QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setFamilies([font.family(), "Microsoft YaHei UI", "Noto Sans CJK SC"])
    font.setPointSize(10)
    dialog.setFont(font)
    day = is_daytime()
    c = appearance.roles(day) if appearance else theme_roles("touhou", day)
    p = appearance.palette(day) if appearance else touhou_palette(day)
    size = appearance.settings.get("font_size", 13) if appearance else 13
    checkmark = (Path(__file__).resolve().parent.parent / "themes/checkmark.svg").as_posix()
    dialog.setStyleSheet(f"""
QDialog, QWidget#settingsPage {{ background:{p['top']}; color:{c['text']}; }}
QWidget {{ color:{c['text']}; font-size:{size}px; }}
QLabel#settingsTitle {{ font-size:{size+10}px; font-weight:600; color:{c['text']}; }}
QLabel#settingsHint {{ color:{c['muted']}; line-height:1.5; }}
QLabel#settingsStatus {{ color:{c['blue']}; padding:8px; }}
QGroupBox {{ border:1px solid {c['border']}; border-radius:12px; margin-top:20px; padding:18px 16px 12px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:16px; padding:0 6px; font-weight:600; color:{p['vermilion']}; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget, QTreeWidget {{
 background:{c['bubble']}; color:{c['text']}; border:1px solid {c['border']}; border-radius:7px; padding:7px;
 selection-background-color:{c['hover']}; selection-color:{c['selected']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color:{p['vermilion']}; }}
QComboBox QAbstractItemView {{ background:{c['bubble']}; color:{c['text']}; selection-background-color:{c['hover']}; selection-color:{c['selected']}; }}
QPushButton {{ background:{c['user']}; color:{c['text']}; border:1px solid {c['border']}; border-radius:8px; padding:8px 14px; min-height:18px; }}
QPushButton:hover {{ background:{c['hover']}; border-color:{p['gold']}; }}
QPushButton:pressed {{ background:{c['border']}; }}
QPushButton:disabled {{ color:{c['muted']}; background:{c['surface']}; }}
QPushButton#primaryButton {{ background:{c['button']}; color:{c['button_text']}; border-color:{c['button']}; }}
QTabWidget::pane {{ border:0; border-top:1px solid {c['border']}; }}
QTabBar::tab {{ padding:12px 14px; margin-right:4px; color:{c['muted']}; border-bottom:3px solid transparent; }}
QTabBar::tab:selected {{ color:{p['vermilion']}; border-bottom-color:{p['vermilion']}; font-weight:600; }}
QTabBar::tab:hover {{ background:{c['hover']}; }}
QCheckBox {{ spacing:9px; padding:4px 0; }}
QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {c['border']}; border-radius:4px; background:{c['bubble']}; }}
QCheckBox::indicator:checked {{ background:{p['vermilion']}; border-color:{p['vermilion']}; image:url("{checkmark}"); }}
QListWidget::item {{ padding:10px; border-radius:6px; }}
QListWidget::item:selected {{ background:{c['hover']}; color:{c['selected']}; }}
QScrollArea {{ border:0; background:transparent; }}
QScrollBar:vertical {{ background:transparent; width:9px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{c['border']}; border-radius:4px; min-height:24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
""" + (appearance.css if appearance else ""))
