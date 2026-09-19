"""Private desktop state and live appearance settings."""
import json
import os
from pathlib import Path
import re
import tempfile

from PySide6.QtCore import QObject, QFileSystemWatcher, QTimer, Signal
from ui_theme import touhou_palette, character_palette, theme_roles, THEME_NAMES


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DesktopState:
    def __init__(self, root):
        self.path = Path(root) / 'data' / 'desktop_ui.json'
        self.data = read_json(self.path)

    def save(self):
        write_json(self.path, self.data)


class Appearance(QObject):
    changed = Signal()

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.defaults = self.root / 'themes' / 'appearance.json'
        self.overrides = self.root / 'data' / 'appearance.json'
        self.extra_css = self.root / 'themes' / 'custom.qss'
        self.settings = {}
        self.css = ''
        self.watcher = QFileSystemWatcher(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.reload)
        self.watcher.fileChanged.connect(lambda *_: self.timer.start())
        self.watcher.directoryChanged.connect(lambda *_: self.timer.start())
        # Some filesystems coalesce or miss atomic-replace notifications.
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.reload)
        self.poll_timer.start()
        self.reload()

    def reload(self):
        candidate = {'theme': 'touhou', 'font_size': 13, 'glass_opacity': .65,
                     'day': dict(touhou_palette(True)), 'night': dict(touhou_palette(False))}
        for path in (self.defaults, self.overrides):
            if not path.exists():
                continue
            try:
                layer = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(layer, dict):
                    raise ValueError('Appearance must be an object')
                if 'theme' in layer:
                    theme = layer['theme']
                    if theme not in THEME_NAMES:
                        raise ValueError('Unknown theme')
                    candidate['theme'] = theme
                    candidate['day'] = character_palette(theme, True)
                    candidate['night'] = character_palette(theme, False)
                for key in ('day', 'night'):
                    for token, colour in layer.get(key, {}).items():
                        if token in candidate[key] and re.fullmatch(r'#[0-9a-fA-F]{6}', str(colour)):
                            candidate[key][token] = colour
                if 'font_size' in layer:
                    candidate['font_size'] = max(11, min(18, int(layer['font_size'])))
                if 'glass_opacity' in layer:
                    candidate['glass_opacity'] = max(.3, min(1., float(layer['glass_opacity'])))
            except (OSError, ValueError, TypeError, AttributeError):
                self._watch_paths()
                return  # An editor's incomplete save must not replace a working theme.
        try:
            css = self.extra_css.read_text(encoding='utf-8') if self.extra_css.exists() else ''
        except OSError:
            css = self.css
        changed = candidate != self.settings or css != self.css
        self.settings, self.css = candidate, css
        self._watch_paths()
        if changed:
            self.changed.emit()

    def _watch_paths(self):
        candidates = (self.defaults.parent, self.overrides.parent,
                      self.defaults, self.overrides, self.extra_css)
        watched = set(self.watcher.files() + self.watcher.directories())
        paths = [str(p) for p in candidates if p.exists() and str(p) not in watched]
        if paths:
            self.watcher.addPaths(paths)

    def choose(self, key, value):
        if key not in ('theme', 'font_size', 'glass_opacity'):
            raise ValueError('Unknown appearance preference')
        if key == 'theme' and value not in THEME_NAMES:
            raise ValueError('Unknown theme')
        saved = read_json(self.overrides)
        saved[key] = value
        write_json(self.overrides, saved)
        self.reload()

    def palette(self, day):
        return self.settings.get('day' if day else 'night', touhou_palette(day))

    def roles(self, day):
        return theme_roles(self.settings.get('theme', 'touhou'), day)

    def alpha(self, value):
        return min(255, round(value * self.settings.get('glass_opacity', .65) / .65))

    def stylesheet(self, base, day):
        source, target = touhou_palette(day), self.palette(day)
        mapping = {source[key].lower(): target[key] for key in source}
        base = re.sub(r'#[0-9a-fA-F]{6}', lambda m: mapping.get(m[0].lower(), m[0]), base)
        base = re.sub(r'rgba\((\d+,\s*\d+,\s*\d+),\s*(\d+)\)',
                      lambda m: f'rgba({m[1]},{self.alpha(int(m[2]))})', base)
        if self.settings.get('theme') != 'touhou':
            base += self.character_stylesheet(day)
        base += self.menu_stylesheet(day)
        size = self.settings.get('font_size', 13)
        return base + f'\nQWidget {{font-size:{size}px;}} QLabel#chatTitle {{font-size:{size+6}px;}}\n' + self.css


    def character_stylesheet(self, day):
        c, p = self.roles(day), self.palette(day)
        def glass(key, alpha=210):
            colour = c[key].lstrip('#')
            rgb = ','.join(str(int(colour[i:i+2], 16)) for i in (0, 2, 4))
            return f'rgba({rgb},{self.alpha(alpha)})'
        return f"""
QWidget {{ color:{c['text']}; }}
QDialog {{ background:{p['top']}; }}
QLabel#chatTitle {{ color:{c['text']}; }}
QLabel#chatSubtitle, QLabel#composerHint, QLabel#sysBubble {{ color:{c['muted']}; }}
QLabel#userBubble {{ background:{glass('user')}; color:{c['text']}; border-color:{c['border']}; }}
QLabel#petBubble {{ background:{glass('bubble')}; color:{c['text']}; border-color:{c['border']}; border-left-color:{p['vermilion']}; }}
QPlainTextEdit, QListWidget {{ background:{glass('bubble', 205)}; color:{c['text']}; border:1px solid {c['border']}; selection-background-color:{c['hover']}; selection-color:{c['selected']}; }}
QPlainTextEdit:focus {{ border-color:{c['blue']}; }}
QPushButton {{ background:{glass('user', 190)}; color:{c['text']}; border-color:{c['border']}; }}
QPushButton:hover {{ background:{c['hover']}; border-color:{p['gold']}; }}
QPushButton:pressed {{ background:{c['border']}; }}
QPushButton:disabled {{ background:{c['surface']}; color:{c['muted']}; border-color:{c['border']}; }}
QPushButton#sendButton {{ background:{c['button']}; color:{c['button_text']}; border-color:{c['button']}; }}
QPushButton#sendButton:hover {{ background:{c['blue']}; color:{p['top']}; border-color:{p['gold']}; }}
QPushButton#sendButton:pressed {{ background:{c['selected']}; color:{p['top']}; }}
QPushButton#sendButton:disabled {{ background:{c['border']}; color:{c['muted']}; border-color:{c['border']}; }}
QPushButton#attachmentTag {{ background:{glass('user', 205)}; color:{c['blue']}; border-color:{p['gold']}; }}
QPushButton#moreButton {{ background:transparent; color:{c['blue']}; border-color:{c['border']}; }}
QScrollBar::handle:vertical {{ background:{c['border']}; }}
QScrollBar::handle:vertical:hover {{ background:{p['vermilion']}; }}
"""

    def menu_stylesheet(self, day):
        c, p = self.roles(day), self.palette(day)
        size = self.settings.get('font_size', 13)
        # Opaque popup surfaces keep menu labels readable over any desktop.
        return f"""
QMenu {{ background:{p['top']}; color:{c['text']}; border:1px solid {p['gold']}; border-top:3px solid {p['vermilion']}; padding:7px; font-size:{size}px; }}
QMenu::item {{ padding:8px 32px 8px 27px; margin:2px 0; border-radius:6px; }}
QMenu::item:selected {{ background:{c['hover']}; color:{c['selected']}; }}
QMenu::item:disabled {{ color:{c['muted']}; }}
QMenu::separator {{ height:1px; background:{c['border']}; margin:6px 12px; }}
QMenu::indicator {{ width:10px; height:10px; margin-left:7px; border:1px solid {c['border']}; border-radius:5px; }}
QMenu::indicator:checked {{ background:{p['vermilion']}; border-color:{p['vermilion']}; }}
QMenu::indicator:unchecked {{ background:transparent; }}
""" + self.css
