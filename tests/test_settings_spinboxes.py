"""Exercise arrow hit targets, not just programmatic value changes."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDialog, QDoubleSpinBox, QFormLayout, QSpinBox,
    QStyle, QStyleFactory, QStyleOptionSpinBox,
)

from settings_style import apply_dialog_style
from ui_theme import THEME_NAMES, character_palette, theme_roles


APP = QApplication.instance() or QApplication([])
STYLES = QStyleFactory.keys()


class SettingsSpinBoxTests(unittest.TestCase):
    @contextmanager
    def field(self, style, cls=QSpinBox, size=13, theme='touhou', day=True):
        original_style = APP.style().objectName()
        APP.setStyle(style)
        dialog = QDialog()
        # Native Windows runs must not steal focus or show over the user's pet.
        dialog.setAttribute(Qt.WA_DontShowOnScreen)
        form = QFormLayout(dialog)
        box = cls()
        box.setRange(0, 100)
        box.setSingleStep(.05 if cls is QDoubleSpinBox else 1)
        box.setValue(50)
        form.addRow('Value', box)
        appearance = SimpleNamespace(
            settings={'font_size': size}, css='',
            roles=lambda _: theme_roles(theme, day),
            palette=lambda _: character_palette(theme, day),
        )
        with patch('settings_style.is_daytime', return_value=day):
            apply_dialog_style(dialog, appearance)
        dialog.resize(400, 120)
        dialog.show()
        APP.processEvents()
        try:
            yield box
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.setStyle(original_style)

    @staticmethod
    def arrow_rect(box, control):
        option = QStyleOptionSpinBox()
        box.initStyleOption(option)
        return box.style().subControlRect(QStyle.CC_SpinBox, option, control, box)

    def press(self, box, point, expected):
        # Sending directly to box would bypass the QLineEdit that used to cover
        # the Windows 11 increment button, hiding the regression from this test.
        target = box.childAt(point) or box
        local = target.mapFrom(box, point)
        QTest.mousePress(target, Qt.LeftButton, Qt.NoModifier, local, 0)
        try:
            self.assertAlmostEqual(box.value(), expected)
        finally:
            QTest.mouseRelease(target, Qt.LeftButton, Qt.NoModifier, local, 0)
        self.assertAlmostEqual(box.value(), expected)

    def test_arrow_area_and_immediate_steps_across_native_styles_and_font_sizes(self):
        for style in STYLES:
            for cls in (QSpinBox, QDoubleSpinBox):
                for size in (11, 18):
                    with self.subTest(style=style, kind=cls.__name__, font=size):
                        with self.field(style, cls, size) as box:
                            up = self.arrow_rect(box, QStyle.SC_SpinBoxUp)
                            down = self.arrow_rect(box, QStyle.SC_SpinBoxDown)
                            self.assertFalse(up.intersects(down))
                            for rect, direction in ((up, 1), (down, -1)):
                                self.assertFalse(rect.intersects(box.lineEdit().geometry()))
                                self.assertGreaterEqual(rect.width(), 28)
                                self.assertGreaterEqual(rect.height(), 18)
                                for point in (rect.center(), rect.topLeft() + QPoint(3, 3),
                                              rect.bottomRight() - QPoint(3, 3)):
                                    self.press(box, point, box.value() + direction * box.singleStep())

    def test_limits_disabled_state_and_keyboard_remain_usable(self):
        for style in STYLES:
            for cls in (QSpinBox, QDoubleSpinBox):
                with self.subTest(style=style, kind=cls.__name__), self.field(style, cls) as box:
                    up = self.arrow_rect(box, QStyle.SC_SpinBoxUp).center()
                    down = self.arrow_rect(box, QStyle.SC_SpinBoxDown).center()
                    box.setValue(box.maximum())
                    self.press(box, up, box.maximum())
                    self.press(box, down, box.maximum() - box.singleStep())
                    box.setValue(box.minimum())
                    self.press(box, down, box.minimum())
                    self.press(box, up, box.minimum() + box.singleStep())
                    box.setFocus()
                    QTest.keyClick(box, Qt.Key_Up)
                    self.assertAlmostEqual(box.value(), box.minimum() + 2 * box.singleStep())
                    QTest.keyClick(box, Qt.Key_Down)
                    self.assertAlmostEqual(box.value(), box.minimum() + box.singleStep())
                    box.lineEdit().selectAll()
                    QTest.keyClicks(box.lineEdit(), '12')
                    QTest.keyClick(box, Qt.Key_Return)
                    self.assertAlmostEqual(box.value(), 12)
                    box.setEnabled(False)
                    self.press(box, up, 12)

    def test_theme_refresh_and_new_fields_keep_arrows_clickable(self):
        style = next((s for s in STYLES if s.lower() == 'windows11'), 'Fusion')
        with self.field(style) as box:
            dialog = box.window()
            box.setValue(27)
            apply_dialog_style(dialog)
            fresh = QDoubleSpinBox()
            fresh.setValue(5)
            dialog.layout().addRow('New field after save', fresh)
            APP.processEvents()
            self.press(box, self.arrow_rect(box, QStyle.SC_SpinBoxUp).center(), 28)
            self.press(fresh, self.arrow_rect(fresh, QStyle.SC_SpinBoxUp).center(), 6)

    @staticmethod
    def arrow_image(box, control, state):
        option = QStyleOptionSpinBox()
        box.initStyleOption(option)
        option.state &= ~(QStyle.State_MouseOver | QStyle.State_Sunken | QStyle.State_HasFocus)
        option.activeSubControls = QStyle.SC_None
        if state != 'normal':
            option.state |= QStyle.State_MouseOver
            option.activeSubControls = control
        if state == 'pressed':
            option.state |= QStyle.State_Sunken
        canvas = QPixmap(box.size())
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        box.style().drawComplexControl(QStyle.CC_SpinBox, option, painter, box)
        painter.end()
        return canvas.toImage().copy(SettingsSpinBoxTests.arrow_rect(box, control))

    def test_hover_and_press_are_visible_in_every_day_and_night_theme(self):
        style = next((s for s in STYLES if s.lower() == 'windows11'), 'Fusion')
        for theme in THEME_NAMES:
            for day in (True, False):
                with self.subTest(theme=theme, day=day), self.field(style, theme=theme, day=day) as box:
                    for control in (QStyle.SC_SpinBoxUp, QStyle.SC_SpinBoxDown):
                        normal = self.arrow_image(box, control, 'normal')
                        hover = self.arrow_image(box, control, 'hover')
                        pressed = self.arrow_image(box, control, 'pressed')
                        self.assertNotEqual(normal, hover)
                        self.assertNotEqual(hover, pressed)
                        # A coloured button without an arrow is still unusable.
                        x, y = normal.width() // 2, normal.height() // 2
                        pixels = {normal.pixel(i, j) for i in range(x - 5, x + 5)
                                  for j in range(y - 3, y + 4)}
                        self.assertGreater(len(pixels), 1)
                        if control == QStyle.SC_SpinBoxUp:
                            box.setMaximum(box.value())
                        else:
                            box.setMinimum(box.value())
                        off = self.arrow_image(box, control, 'normal')
                        self.assertNotEqual(normal, off)
                        self.assertEqual(off, self.arrow_image(box, control, 'hover'))


if __name__ == '__main__':
    unittest.main()
