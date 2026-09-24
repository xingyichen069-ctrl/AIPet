"""Shared theme behavior; no network, model, or owner's runtime data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from desktop_state import Appearance, write_json
from theme_widgets import ThemeMenu, add_appearance_menu
from ui_theme import THEME_NAMES, character_palette, theme_roles
from pet import PetWindow

APP = QApplication.instance() or QApplication([])


class AppearanceTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / 'work'
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.tmp.name)
        self.owner = QWidget()
        self.appearance = Appearance(self.root, self.owner)
        self.menu = ThemeMenu(self.appearance, self.owner, heading=True)
        self.options = add_appearance_menu(self.menu, self.appearance)

    def tearDown(self):
        self.menu.close()
        self.owner.deleteLater()
        APP.sendPostedEvents(None, 0)
        APP.processEvents()
        self.tmp.cleanup()

    def test_selecting_menu_theme_updates_existing_menus_and_survives_restart(self):
        themes = self.options.actions()[0].menu()
        for action in themes.actions():
            action.trigger()
            key = action.data()
            self.assertEqual(self.appearance.settings['theme'], key)
            self.assertEqual(sum(a.isChecked() for a in themes.actions()), 1)
            self.assertTrue(action.isChecked())
            for day in (True, False):
                with patch('theme_widgets.is_daytime', return_value=day):
                    self.menu.aboutToShow.emit()
                    themes.aboutToShow.emit()
                    colour = character_palette(key, day)['top']
                    self.assertIn(colour, self.menu.styleSheet())
                    self.assertIn(colour, themes.styleSheet())
            reopened = Appearance(self.root, self.owner)
            self.assertEqual(reopened.settings['theme'], key)
            reopened.deleteLater()

    def test_keyboard_can_select_theme(self):
        themes = self.options.actions()[0].menu()
        themes.popup(QPoint(100, 100))
        themes.setActiveAction(themes.actions()[1])
        QTest.keyClick(themes, Qt.Key_Return)
        self.assertEqual(self.appearance.settings['theme'], 'marisa')

    def test_external_edit_is_shared_and_invalid_save_keeps_last_theme(self):
        write_json(self.appearance.overrides, {'theme': 'koishi', 'font_size': 17})
        QTest.qWait(700)
        self.assertEqual(self.appearance.settings['theme'], 'koishi')
        self.assertIn('font-size:17px', self.menu.styleSheet())
        self.appearance.overrides.write_text('{broken', encoding='utf-8')
        self.appearance.reload()
        self.assertEqual(self.appearance.settings['theme'], 'koishi')
        write_json(self.appearance.overrides, {'theme': 'unknown'})
        self.appearance.reload()
        self.assertEqual(self.appearance.settings['theme'], 'koishi')

    def test_original_custom_colours_and_preferences_remain_compatible(self):
        write_json(self.appearance.overrides, {'day': {'top': '#fff0dc'}, 'font_size': 15, 'glass_opacity': .4})
        self.appearance.reload()
        self.assertEqual(self.appearance.palette(True)['top'], '#fff0dc')
        self.appearance.choose('theme', 'marisa')
        self.assertEqual(self.appearance.settings['font_size'], 15)
        self.assertEqual(self.appearance.settings['glass_opacity'], .4)
        self.assertEqual(self.appearance.palette(True)['top'], '#fff0dc')

    def test_actual_pet_menu_keeps_callbacks_and_themes_advanced_submenus(self):
        pet = self.owner
        pet.appearance = self.appearance
        pet.state, pet.gl = {}, None
        pet.companion = Mock()
        pet.companion.store.focus.return_value = False
        # ★ 这个列表要跟着 _build_menu 走：菜单里 addAction 接了哪个 self.方法，
        #   这里就得桩上哪个。漏一个，用例会以 AttributeError 挂掉，而报错信息
        #   （"'QWidget' object has no attribute ..."）看不出是漏桩，像代码坏了。
        for name in ('open_chat', '_start_company', '_open_memory_view', '_show_people',
                     '_toggle_panel', '_set_level', '_toggle_click_through', '_toggle_topmost',
                     '_toggle_focus_timer', 'probe_proxy', '_check_update',
                     '_open_config', '_open_persona_manager', 'quit_safely'):
            setattr(pet, name, Mock())
        with patch('pet.QQ_STATUS', return_value={'label': 'QQ 未连接', 'state': 'off'}), patch('pet.T.load', return_value={}):
            menu = PetWindow._build_menu(pet)
        actions = {a.text(): a for a in menu.actions()}
        actions['打开对话'].trigger()
        pet.open_chat.assert_called_once()
        actions['人格管理'].trigger()
        pet._open_persona_manager.assert_called_once()
        advanced = actions['高级'].menu()
        self.assertIsInstance(advanced, ThemeMenu)
        self.assertIsInstance(advanced.actions()[1].menu(), ThemeMenu)
        actions['退出'].trigger()
        pet.quit_safely.assert_called_once()
        menu.deleteLater()

    def test_text_and_primary_buttons_meet_normal_text_contrast(self):
        def luminance(colour):
            values = [int(colour[i:i+2], 16) / 255 for i in (1, 3, 5)]
            values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
            return sum(v * k for v, k in zip(values, (.2126, .7152, .0722)))
        for theme in THEME_NAMES:
            for day in (True, False):
                c, p = theme_roles(theme, day), character_palette(theme, day)
                for fg, bg in ((c['text'], p['top']), (c['selected'], c['hover']), (c['button_text'], c['button'])):
                    a, b = sorted((luminance(fg), luminance(bg)))
                    self.assertGreaterEqual((b + .05) / (a + .05), 4.5, (theme, day, fg, bg))
