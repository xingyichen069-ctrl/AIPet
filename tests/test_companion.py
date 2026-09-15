"""Behavior tests use temporary data, never the owner's journal or API."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
import companion as C
import memory as M


class Services(unittest.TestCase):
    def setUp(self):
        work = Path(os.environ.get('AIPET_TEST_WORK', PROJECT / 'work'))
        work.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        shutil.copyfile(PROJECT / 'data/config.json', self.root / 'data/config.json')
        self.old_root = M.ROOT
        M.ROOT = self.root
        self.store = C.Store(self.root)

    def tearDown(self):
        M.ROOT = self.old_root
        self.tmp.cleanup()

    def pair(self, user='我们继续写方案', assistant='下一步先做原型'):
        mid = self.store.add_message('user', user)
        self.store.add_message('assistant', assistant)
        return mid

    def test_restart_restores_both_sides(self):
        self.pair()
        restarted = C.Store(self.root)
        self.assertEqual([m['content'] for m in restarted.history()], ['我们继续写方案', '下一步先做原型'])

    def test_new_topic_does_not_delete_old_topic(self):
        old = self.store.session()
        self.pair()
        self.store.session(new=True)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(len(self.store.messages(old)), 2)

    def test_incomplete_messages_not_sent_as_success(self):
        self.store.add_message('user', '没有发完', status='pending')
        self.store.add_message('assistant', '半句', status='failed')
        self.assertEqual(self.store.history(), [])

    def test_old_matching_exchange_is_recalled(self):
        self.pair('蓝鲸项目方案', '先完成蓝鲸原型')
        for i in range(12):
            self.pair('午饭'+str(i), '面条'+str(i))
        h = self.store.history('蓝鲸方案继续')
        self.assertIn('先完成蓝鲸原型', [m['content'] for m in h])
        self.assertLessEqual(len(h), 16)

    def test_attachment_context_survives_restart(self):
        query = C.material_query('解释这个', {'name': '材料.md', 'text': '仅供分析的材料'})
        self.store.add_message('user', '解释这个〔材料.md〕', query)
        self.assertEqual(C.Store(self.root).history()[0]['content'], query)

    def test_large_attachment_context_is_not_silently_cut(self):
        query = C.material_query('提问' * 4000, {'name':'long.md', 'text':'内容' * 11000})
        self.store.add_message('user', '较长的问题与材料', query)
        self.store.add_message('assistant', '收到材料')
        self.assertEqual(self.store.history()[0]['content'], query)

    def test_due_once_and_restart_recovers_unhandled(self):
        t = self.store.create_task('收衣服', seconds=30)
        with patch('companion.time.time', return_value=t['due'] + 1):
            self.assertEqual(len(self.store.due_tasks()), 1)
            self.assertEqual(len(C.Store(self.root).due_tasks()), 1)
            self.store.task_action(t['id'], 'complete')
            self.assertEqual(self.store.due_tasks(), [])

    def test_sleep_overdue_and_snooze(self):
        t = self.store.create_task('水烧好了', seconds=3)
        with patch('companion.time.time', return_value=t['due']+3600):
            self.assertEqual(len(self.store.due_tasks()), 1)
            self.store.task_action(t['id'], 'snooze', 10)
            self.assertEqual(self.store.due_tasks(), [])
            self.assertAlmostEqual(self.store.tasks()[0]['due'], t['due']+4200)

    def test_next_visit_does_not_fire_on_background_tick(self):
        self.store.create_task('发报告', next_visit=True)
        self.assertEqual(self.store.due_tasks(), [])
        self.assertEqual(len(self.store.due_tasks(visit=True)), 1)

    def test_focus_restores_and_ends_at_deadline(self):
        t = self.store.create_task('陪我写半小时', seconds=1800, kind='focus')
        self.assertEqual(C.Store(self.root).focus()['id'], t['id'])
        with patch('companion.time.time', return_value=t['due']+1):
            self.assertIsNone(self.store.focus())
            self.assertEqual(self.store.due_tasks()[0]['kind'], 'focus')

    def test_only_one_focus(self):
        self.store.create_task('写作', seconds=10, kind='focus')
        with self.assertRaises(ValueError):
            self.store.create_task('阅读', seconds=10, kind='focus')

    def test_retry_does_not_duplicate_agreement(self):
        first = self.store.create_task('收衣服', seconds=60)
        again = self.store.create_task('收衣服', seconds=60)
        self.assertEqual(first['id'], again['id'])

    def test_invalid_deadlines(self):
        for kwargs in ({}, {'seconds': -1}, {'seconds': float('nan')}, {'seconds': float('inf')},
                       {'due_at': '2020-01-01T00:00:00+08:00'}, {'seconds': 10, 'next_visit': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.create_task('测试', **kwargs)

    def test_chinese_duration(self):
        for text, expected in [('半小时', 1800), ('四十分钟', 2400), ('一个半小时', 5400),
                               ('一小时二十五分钟', 5100), ('1.5小时', 5400), ('十秒', 10)]:
            with self.subTest(text=text):
                self.assertEqual(C.duration(text), expected)

    def test_local_requests_work_without_api(self):
        self.assertIn('记下了', C.local_command('四十分钟后提醒我收衣服', self.store))
        self.assertIn('开始了', C.local_command('陪我写半小时', self.store))
        self.assertEqual(len(self.store.tasks()), 2)

    def test_explanation_does_not_schedule(self):
        self.assertIsNone(C.local_command('解释一下“半小时后提醒我喝水”这句话', self.store))
        self.assertEqual(self.store.tasks(), [])

    def test_remember_permanent_is_idempotent(self):
        mid = self.pair('我喜欢蓝色')
        self.store.record_memory(mid)
        self.store.remember_message(mid)
        self.store.remember_message(mid)
        journal = M.load_journal()
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]['decay'], 'permanent')

    def test_forget_removes_message_reply_attachment_and_live_memory(self):
        mid = self.store.add_message('user', '私人草稿', C.material_query('私人草稿', {'name':'x.txt','text':'材料原文'}))
        self.store.add_message('assistant', '私人草稿的回答')
        self.store.record_memory(mid)
        original = M.load_journal()
        self.store.edit_message(mid)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(M.load_journal(), [])
        raw = self.store.path.read_bytes()
        self.assertNotIn('材料原文'.encode(), raw)
        # Old journal import cannot silently undo an explicit withdrawal.
        M.save_journal(original)
        self.assertEqual(M.load_journal(), [])

    def test_correct_replaces_memory_and_removes_old_answer(self):
        mid = self.pair('报告周三交', '记住周三了')
        self.store.record_memory(mid)
        self.store.edit_message(mid, '报告周五交')
        self.assertEqual([m['content'] for m in self.store.history()], ['报告周五交'])
        self.assertEqual([e['text'] for e in M.load_journal()], ['用户说：报告周五交'])

    def test_local_forget_targets_previous_message(self):
        mid = self.pair('刚刚那件事')
        self.store.record_memory(mid)
        C.local_command('刚才那句别保存', self.store)
        self.assertEqual(self.store.history(), [])

    def test_forget_also_removes_tool_paraphrases_and_retried_answers(self):
        mid = self.store.add_message('user', '周三交报告')
        self.store.add_message('assistant', '半句旧回答', status='failed')
        self.store.add_message('assistant', '重试后的完整旧回答')
        token = M.ACTIVE_MESSAGE.set(mid)
        try:
            M.add('用户的报告截止日期是星期三', source='tool')
        finally:
            M.ACTIVE_MESSAGE.reset(token)
        self.store.edit_message(mid)
        self.assertEqual(M.load_journal(), [])
        self.assertEqual(self.store.history(), [])

    def test_ephemeral_turn_blocks_automatic_tool_memories(self):
        token = M.ACTIVE_MESSAGE.set('__ephemeral__')
        try:
            self.assertIsNone(M.add('模型从本轮提取出的事实', source='tool'))
        finally:
            M.ACTIVE_MESSAGE.reset(token)
        self.assertEqual(M.load_journal(), [])

    def test_sensitive_memory_not_saved(self):
        mid = self.pair('我的密码是不能写入的')
        with self.assertRaises(ValueError):
            self.store.remember_message(mid)
        self.assertEqual(M.load_journal(), [])

    def test_attachment_validation_and_full_text(self):
        good = self.root / '材料.md'
        good.write_text('## 标题\n原样内容', encoding='utf-8')
        self.assertEqual(C.read_attachment(good)['text'], '## 标题\n原样内容')
        bad = self.root / '大文件.txt'
        bad.write_text('字' * 24001, encoding='utf-8')
        with self.assertRaises(ValueError):
            C.read_attachment(bad)
        with self.assertRaises(ValueError):
            C.read_attachment(self.root / 'x.pdf')


if __name__ == '__main__':
    unittest.main()
