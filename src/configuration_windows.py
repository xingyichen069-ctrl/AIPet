"""Independent, reusable configuration windows with one guarded close path."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPushButton


_windows = {}


class ConfigurationDialog(QDialog):
    def __init__(self):
        # A Windows owned dialog inherits its owner's topmost state even without
        # WindowStaysOnTopHint. Keep the pet as context, never as a native owner.
        super().__init__(None, Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self._confirming_close = False

    def _can_close(self):
        return True

    def done(self, result):
        # Qt's default closeEvent calls reject/done and accepts the event only
        # when the dialog really closes. Escape, X and programmatic closes agree.
        if self._confirming_close:
            return
        self._confirming_close = True
        try:
            allowed = self._can_close()
        finally:
            self._confirming_close = False
        if allowed:
            super().done(result)

    def disable_default_buttons(self):
        # Enter in an API/model field must not invoke an unrelated first button.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)


def _present(dialog):
    if dialog.isMinimized():
        dialog.setWindowState(dialog.windowState() & ~Qt.WindowMinimized)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def show_configuration_window(root, kind, factory):
    key = (Path(root).resolve(), kind)
    dialog = _windows.get(key)
    if dialog is None:
        dialog = factory()
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        _windows[key] = dialog

        def forget(*_):
            if _windows.get(key) is dialog:
                _windows.pop(key)

        dialog.finished.connect(forget)
        dialog.destroyed.connect(forget)
    _present(dialog)
    return dialog


def close_configuration_windows(root):
    """Veto application exit if a draft or a running API check keeps a window open."""
    root = Path(root).resolve()
    for (folder, _), dialog in list(_windows.items()):
        if folder == root and not dialog.close():
            _present(dialog)
            return False
    return True
