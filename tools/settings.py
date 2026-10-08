"""Open only the configuration window; never start a pet or a QQ connection."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from desktop_state import Appearance
    from settings_ui import SettingsDialog
    app = QApplication.instance() or QApplication(sys.argv)
    appearance = Appearance(ROOT)
    try:
        dialog = SettingsDialog(ROOT, appearance)
    except (OSError, ValueError, TypeError) as error:
        QMessageBox.warning(None, "无法读取设置", str(error))
        return 1
    dialog.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
