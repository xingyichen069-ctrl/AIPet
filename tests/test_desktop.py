import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from datetime import datetime
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QThread, Signal, QMimeData, QUrl, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

import test_companion as TC
import companion as C
import companion_ui as UI
import ui_theme
from desktop_state import DesktopState, write_json

APP = QApplication.instance() or QApplication([])


class FakeWorker(QThread):
    chunk = Signal(str, str)
    mode = 'success'
    queries = []

    def __init__(self, query, history):
        super().__init__()
        self.queries.append((query, history))

    def run(self):
        if self.mode == 'error':
            self.chunk.emit('error', '测试连接失败')
        elif self.mode == 'wait':
            while not self.isInterruptionRequested():
                self.msleep(5)
        else:
            self.chunk.emit('tool', '工具调用')
            self.chunk.emit('content', '先完成原型，')
            self.chunk.emit('content', '再验证交互。')


class Desktop(unittest.TestCase):
    setUp = TC.Services.setUp
    tearDownData = TC.Services.tearDown

    def setUp(self):
        TC.Services.setUp(self)
        self.patch_store = patch('companion_ui.C.Store', return_value=self.store)
        self.patch_store.start()
        self.pet = QWidget()
        self.pet.resize(260, 380)
        self.pet.move(500, 250)
        self.pet.state = {}
        self.pet.gl = Mock()
        self.pet.panel = Mock()
        self.pet.bubble_win = None
        self.pet.chat = None
        self.pet._check_update = Mock()
        self.pet.companion = UI.CompanionController(self.pet)
        self.chat = UI.ChatWindow(self.pet, FakeWorker)
        self.pet.chat = self.chat
        self.chat.show()
        FakeWorker.mode = 'success'
        FakeWorker.queries = []

    def test_update_button_delegates_to_desktop_controller(self):
        self.assertEqual(self.chat.update_btn.text(), '检查更新')
        self.chat.update_btn.click()
        self.pet._check_update.assert_called_once_with()

    def tearDown(self):
        if self.chat.busy():
            self.chat.worker.requestInterruption()
            self.chat.worker.wait(3000)
        self.pet.companion.timer.stop()
        for widget in (self.chat, self.pet.companion.popup, self.pet.companion.counter, self.pet):
            widget.hide()
            widget.deleteLater()
        APP.processEvents()
        self.patch_store.stop()
        self.tearDownData()

    def finish(self):
        for _ in range(200):
            QTest.qWait(10)
            if not self.chat.busy():
                APP.processEvents()
                return
        self.fail('Worker did not finish')

    def reopen_chat(self):
        self.chat.prepare_quit()
        self.chat.hide()
        self.chat.deleteLater()
        APP.processEvents()
        self.chat = UI.ChatWindow(self.pet, FakeWorker)
        self.pet.chat = self.chat
        self.chat.show()
        QTest.qWait(30)

    def test_draft_and_attachment_snapshot_survive_restart(self):
        self.chat.input.setPlainText('还没写完的草稿')
        self.chat.input.moveCursor(UI.QTextCursor.Start)
        material = self.root / '材料.md'
        material.write_text('原始材料', encoding='utf-8')
        self.chat.load_attachment(material)
        material.unlink()
        QTest.qWait(350)
        saved = DesktopState(self.root)
        self.assertEqual(saved.data['drafts'][self.store.session()]['text'], '还没写完的草稿')
        if os.name != 'nt':
            self.assertEqual(saved.path.stat().st_mode & 0o777, 0o600)
        self.reopen_chat()
        self.assertEqual(self.chat.input.toPlainText(), '还没写完的草稿')
        self.assertEqual(self.chat.input.textCursor().position(), 0)
        self.assertEqual(self.chat.attachment, {'name': '材料.md', 'text': '原始材料'})

    def test_sent_message_does_not_return_as_draft(self):
        self.chat.input.setPlainText('今天继续写方案')
        self.chat.send()
        self.finish()
        self.reopen_chat()
        self.assertEqual(self.chat.input.toPlainText(), '')
        self.assertIsNone(self.chat.attachment)

    def test_topics_keep_separate_drafts(self):
        self.chat.input.setPlainText('第一个话题的草稿')
        self.chat.new_topic()
        self.assertEqual(self.chat.input.toPlainText(), '')
        self.chat.input.setPlainText('第二个话题的草稿')
        with patch.object(UI.QInputDialog, 'getItem', side_effect=lambda *args: (args[3][-1], True)):
            self.chat.old_topics()
        self.assertEqual(self.chat.input.toPlainText(), '第一个话题的草稿')
        self.assertEqual(len(DesktopState(self.root).data['drafts']), 2)

    def test_window_layout_and_open_state_survive_quit(self):
        self.chat.resize(440, 550)
        self.chat.move(100, 110)
        QTest.qWait(300)
        position, size = self.chat.pos(), self.chat.size()
        self.assertTrue(DesktopState(self.root).data['layout']['open'])
        self.reopen_chat()
        self.assertEqual(self.chat.pos(), position)
        self.assertEqual(self.chat.size(), size)
        self.chat.close()
        self.assertFalse(DesktopState(self.root).data['layout']['open'])

    def test_live_preferences_preserve_draft_and_apply_immediately(self):
        self.chat.input.setPlainText('调整外观时保留这段草稿')
        self.chat.change_appearance('font_size', 17)
        self.chat.change_appearance('glass_opacity', .4)
        QTest.qWait(30)
        self.assertEqual(self.chat.input.font().pixelSize(), 17)
        self.assertEqual(self.chat.appearance.settings['glass_opacity'], .4)
        self.assertEqual(self.chat.input.toPlainText(), '调整外观时保留这段草稿')
        self.reopen_chat()
        self.assertEqual(self.chat.appearance.settings['font_size'], 17)
        self.assertEqual(self.chat.input.toPlainText(), '调整外观时保留这段草稿')

    def test_character_theme_switch_preserves_draft_attachment_and_history(self):
        self.store.add_message('user', '保存好的对话')
        self.store.add_message('assistant', '一起继续')
        self.chat.restore()
        self.chat.input.setPlainText('切换时保留的草稿')
        material = self.root / '主题测试.md'
        material.write_text('需要保留的附件内容', encoding='utf-8')
        self.chat.load_attachment(material)
        attachment = dict(self.chat.attachment)
        history = self.store.history()
        # 从 THEME_NAMES 取，主题列表增加时测试自动覆盖新配色。
        for theme in ui_theme.THEME_NAMES:
            self.chat.change_appearance('theme', theme)
            for day in (True, False):
                with patch.object(UI, 'is_daytime', return_value=day):
                    self.chat._refresh_theme(force=True)
                    self.chat._glass_ready = False
                    frame = self.chat.grab().toImage()
                    self.assertEqual(frame.pixelColor(10, 40).name(),
                                     ui_theme.character_palette(theme, day)['top'])
                    self.assertEqual(self.chat.emblem.theme, theme)
                    self.assertEqual(self.chat.divider.theme, theme)
            self.assertEqual(self.chat.input.toPlainText(), '切换时保留的草稿')
            self.assertEqual(self.chat.attachment, attachment)
            self.assertEqual(self.store.history(), history)
        self.chat.change_appearance('theme', 'koishi')
        self.reopen_chat()
        self.assertEqual(self.chat.appearance.settings['theme'], 'koishi')
        self.assertEqual(self.chat.attachment, attachment)
        self.assertEqual(self.chat.input.toPlainText(), '切换时保留的草稿')
        # A shared appearance object must disconnect a destroyed chat receiver.
        self.chat.change_appearance('theme', 'marisa')
        self.assertEqual(self.chat.emblem.theme, 'marisa')

    def test_external_theme_changes_reload_after_atomic_replace(self):
        with patch.object(UI, 'is_daytime', return_value=True):
            self.chat._refresh_theme(force=True)
            path = self.chat.appearance.overrides
            write_json(path, {'day': {'top': '#fff0dc'}, 'font_size': 15})
            QTest.qWait(700)
            self.assertEqual(self.chat.appearance.palette(True)['top'], '#fff0dc')
            frame = self.chat.grab().toImage()
            self.assertEqual(frame.pixelColor(10, 40).name(), '#fff0dc')
            path.write_text('{invalid', encoding='utf-8')
            QTest.qWait(700)
            self.assertEqual(self.chat.appearance.palette(True)['top'], '#fff0dc')
            write_json(path, {'day': {'top': '#fffaf1'}, 'font_size': 13})
            QTest.qWait(700)
            self.assertEqual(self.chat.appearance.palette(True)['top'], '#fffaf1')

    def test_stream_and_restart_show_both_messages(self):
        self.chat.input.setPlainText('蓝鲸方案继续')
        self.chat.send()
        self.finish()
        self.assertEqual([m['role'] for m in self.store.messages()], ['user', 'assistant'])
        self.assertEqual(self.store.history()[-1]['content'], '先完成原型，再验证交互。')
        self.chat.restore()
        self.assertEqual(self.chat.msgs.count(), 3)

    def test_waiting_rotates_then_success_pulses_once(self):
        FakeWorker.mode = 'wait'
        self.chat.input.setPlainText('继续方案')
        self.chat.send()
        QTest.qWait(100)
        emblem = self.chat.emblem
        self.assertTrue(emblem.animation_timer.isActive())
        self.assertGreater(emblem._angle, 0)
        self.chat.on_chunk('content', '已经想好了。')
        self.chat.worker.requestInterruption()
        self.finish()
        self.assertFalse(emblem._spinning)
        self.assertIsNotNone(emblem._pulse_started)
        QTest.qWait(1300)
        self.assertIsNone(emblem._pulse_started)
        self.assertFalse(emblem.animation_timer.isActive())

    def test_cancel_and_error_do_not_play_completion_pulse(self):
        for mode in ('wait', 'error'):
            FakeWorker.mode = mode
            self.chat.input.setPlainText('再试一次')
            self.chat.send()
            if mode == 'wait':
                self.chat.send_or_stop()
            self.finish()
            self.assertFalse(self.chat.emblem.animation_timer.isActive())
            self.assertIsNone(self.chat.emblem._pulse_started)

    def test_quiet_and_hidden_chat_suspend_animation(self):
        self.chat.input.setPlainText('陪我写半小时')
        self.chat.send()
        FakeWorker.mode = 'wait'
        self.chat.input.setPlainText('稍等想一想')
        self.chat.send()
        QTest.qWait(50)
        self.assertFalse(self.chat.emblem.animation_timer.isActive())
        self.pet.companion.stop_focus()
        self.assertTrue(self.chat.emblem.animation_timer.isActive())
        self.chat.hide()
        self.assertFalse(self.chat.emblem.animation_timer.isActive())
        self.chat.show()
        self.assertTrue(self.chat.emblem.animation_timer.isActive())
        self.chat.hide()
        self.chat.on_chunk('content', '完成。')
        self.chat.worker.requestInterruption()
        self.finish()
        self.chat.show()
        self.assertFalse(self.chat.emblem.animation_timer.isActive())
        self.assertIsNone(self.chat.emblem._pulse_started)

    def test_ime_preedit_hides_placeholder_before_character_commit(self):
        editor = self.chat.input
        self.assertEqual(editor.placeholderText(), '说点什么…')
        QApplication.sendEvent(editor, QInputMethodEvent('ni hao', []))
        self.assertTrue(editor.document().isEmpty())
        self.assertEqual(editor.placeholderText(), '')
        commit = QInputMethodEvent()
        commit.setCommitString('你好')
        QApplication.sendEvent(editor, commit)
        self.assertEqual(editor.toPlainText(), '你好')
        self.assertEqual(editor.placeholderText(), '')
        editor.clear()
        self.assertEqual(editor.placeholderText(), '说点什么…')

    def test_day_night_boundaries_match_desktop(self):
        for hour, minute, expected in ((6, 59, False), (7, 0, True),
                                        (17, 59, True), (18, 0, False), (0, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(ui_theme.is_daytime(datetime(2026, 9, 15, hour, minute)), expected)

    def test_theme_switch_preserves_conversation_draft_and_attachment(self):
        self.store.add_message('user', '上次那个方案，我们接着聊。')
        self.store.add_message('assistant', '先完成原型，再验证交互。')
        self.chat.restore()
        self.chat.input.setPlainText('还没写完的草稿')
        self.chat.input.moveCursor(UI.QTextCursor.Start)
        material = self.root / '材料.md'
        material.write_text('测试材料', encoding='utf-8')
        self.chat.load_attachment(material)
        rows = self.store.messages()
        shades = []
        for day in (True, False, True):
            with patch.object(UI, 'is_daytime', return_value=day):
                self.chat._theme_timer.timeout.emit()
                QTest.qWait(50)
                shot = self.chat.grab()
                ratio = shot.devicePixelRatio()
                shades.append(shot.toImage().pixelColor(int(10*ratio), int(100*ratio)).lightness())
            self.assertEqual(self.chat.input.toPlainText(), '还没写完的草稿')
            self.assertEqual(self.chat.input.textCursor().position(), 0)
            self.assertEqual(self.chat.attachment['name'], '材料.md')
            self.assertEqual(self.store.messages(), rows)
            self.assertFalse(self.chat.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertGreater(shades[0], 200)
        self.assertLess(shades[1], 60)
        self.assertEqual(shades[0], shades[2])

    def test_cancelled_ime_restores_placeholder(self):
        editor = self.chat.input
        QApplication.sendEvent(editor, QInputMethodEvent('ni', []))
        QApplication.sendEvent(editor, QInputMethodEvent())
        self.assertEqual(editor.placeholderText(), '说点什么…')
        editor.insertPlainText('hello')
        self.assertEqual(editor.placeholderText(), '')
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key_Backspace)
        self.assertEqual(editor.placeholderText(), '说点什么…')

    def test_ime_enter_does_not_submit_unfinished_composition(self):
        editor = self.chat.input
        submitted = Mock()
        editor.submitted.connect(submitted)
        QApplication.sendEvent(editor, QInputMethodEvent('ni', []))
        QTest.keyClick(editor, Qt.Key_Return)
        submitted.assert_not_called()

    def test_failure_retry_keeps_single_user_message(self):
        FakeWorker.mode = 'error'
        self.chat.input.setPlainText('继续昨天的方案')
        self.chat.send()
        self.finish()
        self.assertEqual(self.store.last_user()['status'], 'failed')
        FakeWorker.mode = 'success'
        self.chat.retry_last()
        self.finish()
        self.assertEqual(len(self.store.messages()), 2)
        self.assertEqual(self.store.last_user()['status'], 'complete')

    def fill_chat_history(self):
        for i in range(20):
            self.store.add_message('user' if i % 2 else 'assistant',
                                   f'第 {i} 条历史消息。' * 12)
        self.chat.restore()
        QTest.qWait(100)
        bar = self.chat.scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        return bar

    def test_send_from_history_jumps_to_latest_before_reply(self):
        bar = self.fill_chat_history()
        bar.setValue(0)
        FakeWorker.mode = 'wait'
        self.chat.input.setPlainText('继续聊这个方案')
        self.chat.send()
        QTest.qWait(100)
        self.assertEqual(bar.value(), bar.maximum())

    def test_stream_follows_layout_growth_but_allows_reading_history(self):
        bar = self.fill_chat_history()
        self.assertEqual(bar.value(), bar.maximum())
        for _ in range(3):
            self.chat.on_chunk('content', '新的一行回复内容。\n' * 12)
            QTest.qWait(50)
            self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(0)
        # A queued follow must not override a subsequent manual scroll.
        self.chat.on_chunk('content', '继续生成的回复。\n' * 12)
        QTest.qWait(50)
        self.assertEqual(bar.value(), 0)
        bar.setValue(bar.maximum())
        self.chat.on_chunk('content', '回到底部后继续跟随。\n' * 12)
        QTest.qWait(50)
        self.assertEqual(bar.value(), bar.maximum())

    def test_local_command_from_history_jumps_to_latest(self):
        bar = self.fill_chat_history()
        bar.setValue(0)
        self.chat.input.setPlainText('陪我写半小时')
        self.chat.send()
        QTest.qWait(100)
        self.assertEqual(bar.value(), bar.maximum())

    def test_stop_does_not_store_success(self):
        FakeWorker.mode = 'wait'
        self.chat.input.setPlainText('想一会儿')
        self.chat.send()
        QTest.qWait(20)
        self.chat.send_or_stop()
        self.finish()
        self.assertEqual(self.store.last_user()['status'], 'cancelled')
        self.assertEqual(self.store.history(), [])

    def test_attachment_paste_remove_and_send(self):
        path = self.root / '材料.md'
        path.write_text('测试材料正文', encoding='utf-8')
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        self.chat.input.insertFromMimeData(mime)
        self.assertEqual(self.chat.attachment['name'], '材料.md')
        self.chat.remove_attachment()
        self.assertIsNone(self.chat.attachment)
        self.chat.load_attachment(path)
        self.chat.input.setPlainText('解释材料')
        self.chat.send()
        self.finish()
        self.assertIn('测试材料正文', FakeWorker.queries[-1][0])
        self.assertIsNone(self.chat.attachment)

    def test_focus_changes_actual_visual_controller(self):
        self.chat.input.setPlainText('陪我写半小时')
        self.chat.send()
        self.assertIsNotNone(self.store.focus())
        self.pet.gl.set_quiet.assert_called_with(True)
        self.assertTrue(self.pet.companion.counter.isVisible())
        self.pet.companion.stop_focus()
        self.pet.gl.set_quiet.assert_called_with(False)
        self.assertFalse(self.pet.companion.counter.isVisible())

    def test_reminder_buttons_and_queue(self):
        t = self.store.create_task('收衣服', seconds=1)
        with patch('companion.time.time', return_value=t['due']+1):
            self.pet.companion.tick()
            popup = self.pet.companion.popup
            self.assertEqual(popup.task['id'], t['id'])
            self.pet.companion.tick()
            self.assertEqual(popup.task['id'], t['id'])
            popup.act('snooze')
            self.assertFalse(popup.isVisible())
        with patch('companion.time.time', return_value=t['due']+602):
            self.pet.companion.tick()
            popup.act('complete')
            self.assertEqual(self.store.tasks(), [])

    def test_privacy_filter_also_prevents_full_conversation_storage(self):
        self.chat.input.setPlainText('这条包含密码，别写入')
        self.chat.send()
        self.finish()
        self.assertEqual(self.store.messages(), [])

    def test_memory_right_click_forget_refreshes_view(self):
        self.chat.input.setPlainText('我周五提交报告')
        self.chat.send()
        self.finish()
        self.chat.memory_action(self.store.last_user()['id'], 'forget')
        self.assertEqual(self.store.history(), [])

    def test_visual_snapshot(self):
        self.store.add_message('user', '上次那个方案，我们接着聊。')
        self.store.add_message('assistant', '上次定了先做原型。今天可以从输入和提醒这两处开始。')
        self.chat.restore()
        path = self.root / '方案.md'
        path.write_text('一段用于说明的材料', encoding='utf-8')
        self.chat.load_attachment(path)
        self.chat.input.setPlainText('这份材料也一起看看')
        QTest.qWait(100)
        folder = Path(os.environ.get('AIPET_TEST_WORK', self.root))
        self.assertTrue(self.chat.grab().save(str(folder / 'chat-preview.png')))
        t = self.store.create_task('该收衣服了。', seconds=1)
        with patch('companion.time.time', return_value=t['due']+2):
            self.pet.companion.tick()
            QTest.qWait(50)
            self.assertTrue(self.pet.companion.popup.grab().save(str(folder / 'reminder-preview.png')))


if __name__ == '__main__':
    unittest.main()
