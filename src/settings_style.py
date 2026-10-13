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
    arrow_dir = Path(__file__).resolve().parent.parent / "themes"
    arrow_mode = "day" if day else "night"
    up_arrow = (arrow_dir / f"spin-up-{arrow_mode}.svg").as_posix()
    down_arrow = (arrow_dir / f"spin-down-{arrow_mode}.svg").as_posix()
    up_disabled = (arrow_dir / "spin-up-disabled.svg").as_posix()
    down_disabled = (arrow_dir / "spin-down-disabled.svg").as_posix()
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
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color:{p['vermilion']}; }}
/* Explicit step-button geometry keeps the Windows 11 native horizontal layout
   from placing the editor over the increment button. Reserve both hit areas. */
QSpinBox, QDoubleSpinBox {{ padding-right:8px; min-height:24px; }}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit {{ border:0; padding:0; background:transparent; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
 subcontrol-origin:border; subcontrol-position:top right; width:30px;
 border-left:1px solid {c['border']}; border-bottom:1px solid {c['border']};
 border-top-right-radius:6px; background:{c['user']}; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
 subcontrol-origin:border; subcontrol-position:bottom right; width:30px;
 border-left:1px solid {c['border']}; border-bottom-right-radius:6px; background:{c['user']}; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background:{c['hover']}; }}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{ background:{c['border']}; }}
QSpinBox::up-button:off, QDoubleSpinBox::up-button:off,
QSpinBox::down-button:off, QDoubleSpinBox::down-button:off,
QSpinBox::up-button:disabled, QDoubleSpinBox::up-button:disabled,
QSpinBox::down-button:disabled, QDoubleSpinBox::down-button:disabled {{ background:{c['surface']}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url("{up_arrow}"); width:10px; height:7px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url("{down_arrow}"); width:10px; height:7px; }}
QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off,
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image:url("{up_disabled}"); }}
QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image:url("{down_disabled}"); }}
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
