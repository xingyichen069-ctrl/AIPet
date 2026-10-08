"""Opt-in, clocks, durable claims, shutdown and actual QQ text formatting."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import qq_schedule as S
import qq_text


def stamp(day=8, hour=10, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=S.CST).timestamp()


class Schedules(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "templates", self.root / "templates")
        S.initialize(self.root)
        self.send = Mock(return_value={"id": "qq-accepted-fixture"})

    def tearDown(self):
        self.temp.cleanup()

    def cmd(self, text="/符卡 开启", group="GROUP_A", owner=True, now=None, scene="group"):
        return S.command(self.root, text, scene=scene, group=group, is_owner=owner,
                         now=stamp(hour=9) if now is None else now)

    def run_at(self, now=None, **kwargs):
        return S.run_due(self.root, self.send, now=stamp() if now is None else now,
                         wait=lambda _: False, **kwargs)

    def state(self, group="GROUP_A", task="spellcards"):
        return S._state(self.root)["groups"][group][task]

    def test_defaults_are_disabled_and_preview_does_not_enable_or_rotate(self):
        self.assertEqual(self.run_at(), 0)
        self.assertIn("未开启", self.cmd("/符卡 状态"))
        text = self.cmd("/符卡 预览")
        self.assertEqual(text, "叮！今日份符卡推送：\n灵\n符\n﹁\n梦\n想\n封\n印\n﹂\n持有者：博丽灵梦")
        self.assertEqual(S._state(self.root)["groups"], {})
        self.send.assert_not_called()

    def test_guest_private_and_cross_group_commands_cannot_enable(self):
        self.assertIn("仅主人", self.cmd(owner=False))
        self.assertIn("群里", self.cmd(scene="c2c"))
        self.assertIn("用法", self.cmd("/符卡 开启 OTHER_GROUP"))
        self.assertEqual(S._state(self.root)["groups"], {})
        self.assertIsNone(self.cmd("今天谈谈符卡吧"))

    def test_three_cst_slots_each_send_one_card_with_a_holder(self):
        self.cmd()
        for hour in (10, 14, 22):
            self.assertEqual(self.run_at(stamp(hour=hour)), 1)
            self.assertEqual(self.run_at(stamp(hour=hour, second=20)), 0)
        self.assertEqual(self.send.call_count, 3)
        messages = [call.args[1] for call in self.send.call_args_list]
        self.assertEqual(len(set(messages)), 3)
        self.assertTrue(all(text.count("持有者：") == 1 for text in messages))
        self.assertTrue(all(call.args[0] == "GROUP_A" for call in self.send.call_args_list))
        self.assertEqual(self.state()["cursor"], 3)
        self.assertEqual(self.state()["recent"][-1]["status"], "sent")

    def test_cst_10am_is_utc_2am(self):
        self.cmd()
        utc = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.run_at(utc), 1)
        self.assertEqual(self.run_at(datetime(2026, 10, 8, 10, tzinfo=timezone.utc).timestamp()), 0)

    def test_enabling_mid_slot_and_missed_slots_do_not_catch_up(self):
        self.cmd(now=stamp(second=1))
        self.assertEqual(self.run_at(stamp(second=15)), 0)
        self.assertEqual(self.run_at(stamp(hour=14, minute=2)), 0)
        self.assertEqual(self.run_at(stamp(hour=22, second=10)), 1)

    def test_disable_is_per_group_and_works_with_broken_templates(self):
        self.cmd(); self.cmd(group="GROUP_B")
        self.cmd("/符卡 关闭")
        self.assertEqual(self.run_at(), 1)
        self.assertEqual(self.send.call_args.args[0], "GROUP_B")
        (self.root / "data/qq_tasks/spellcards.json").write_text("broken")
        self.assertIn("已关闭", self.cmd("/符卡 关闭", group="GROUP_B"))
        self.assertFalse(self.state("GROUP_B")["enabled"])

    def test_retry_reenable_clock_rewind_and_reloaded_state_do_not_duplicate(self):
        self.cmd();self.run_at()
        self.cmd("/符卡 关闭", now=stamp(second=5))
        self.cmd(now=stamp(second=6))
        self.assertEqual(self.run_at(stamp(second=7)), 0)
        self.assertEqual(self.run_at(stamp(day=7)), 0)
        self.assertEqual(S.run_due(self.root, self.send, now=stamp(second=8), wait=lambda _:False), 0)
        self.assertEqual(self.send.call_count, 1)

    def test_crash_or_network_ambiguity_is_never_retried(self):
        self.cmd()
        def crash(group, text):
            self.assertEqual(self.state()["recent"][-1]["status"], "sending")
            raise TimeoutError("private-provider-body must not be stored")
        self.send.side_effect = crash
        self.assertEqual(self.run_at(), 1)
        self.assertEqual(self.run_at(stamp(second=20)), 0)
        self.assertEqual(self.state()["recent"][-1]["status"], "unknown")
        self.assertNotIn("private-provider-body", (self.root / "data/qq_schedules.json").read_text())

    def test_permission_error_pauses_until_owner_reenables(self):
        self.cmd(); self.send.return_value = {"_http_error": 400, "code": 40034105}
        self.run_at()
        self.assertTrue(self.state()["blocked"])
        self.assertIn("40034105", self.cmd("/符卡 状态"))
        self.assertEqual(self.run_at(stamp(hour=14)), 0)
        self.cmd(now=stamp(hour=15)); self.send.return_value = {"id": "ok"}
        self.assertEqual(self.run_at(stamp(hour=22)), 1)

    def test_parallel_workers_claim_one_slot(self):
        self.cmd()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_at(), range(2)))
        self.assertEqual(sum(results), 1)
        self.send.assert_called_once()

    def test_corrupt_state_is_preserved_and_no_message_is_sent(self):
        p = self.root / "data/qq_schedules.json";p.write_bytes(b"{broken")
        with self.assertRaises(ValueError):self.run_at()
        self.assertIn("未重置", self.cmd())
        self.assertEqual(p.read_bytes(), b"{broken")
        self.send.assert_not_called()

    def test_recent_history_and_worker_count_remain_bounded(self):
        self.cmd()
        first = datetime(2026, 10, 8, 10, tzinfo=S.CST)
        for offset in range(35):self.run_at((first + timedelta(days=offset)).timestamp())
        self.assertEqual(len(self.state()["recent"]), 8)
        worker = S.Worker(self.root, self.send)
        with patch.object(S, "gateway_ready", return_value=False):
            worker.start();thread = worker.thread;worker.start()
            self.assertIs(thread, worker.thread)
            worker.stop();worker.join(2)
            self.assertFalse(thread.is_alive())

    def test_private_edits_survive_initialization_and_templates_are_data_only(self):
        path = self.root / "data/qq_tasks/spellcards.md"
        data = path.read_bytes() + b"\nPrivate editorial note\n"
        path.write_bytes(data);S.initialize(self.root);self.assertEqual(path.read_bytes(), data)
        p = self.root / "data/qq_tasks/spellcards.json";cfg=json.loads(p.read_text())
        cfg["catalog"]="../../secrets.json";p.write_text(json.dumps(cfg))
        self.assertIn("未重置", self.cmd())
        self.send.assert_not_called()

    def test_document_cards_survive_real_sanitizer_and_two_line_intro(self):
        cfg=S.templates(self.root)["spellcards"]
        deck=S.cards(self.root,cfg);self.assertEqual(len(deck),128)
        self.assertEqual(len({(card['name'],card['holder']) for card in deck}),128)
        for card in deck:
            text=S.render(card);self.assertEqual(qq_text.sanitize(text),(text,[]))
            self.assertTrue(text.startswith("叮！今日份符卡推送：\n"))
            if '-' in card['name']:
                self.assertIn('\n︱\n',text)
        card={"name":"梦符「春」","holder":"测试持有者","intro":"一行花影\n一行月色"}
        self.assertTrue(S.render(card).startswith("一行花影\n一行月色：\n梦\n符"))

    def test_stop_and_offline_gate_prevent_sends(self):
        self.cmd()
        self.assertEqual(self.run_at(stop=lambda:True),0)
        self.assertEqual(self.run_at(ready=lambda:False),0)
        self.assertEqual(self.state()["cursor"],0)
        self.send.assert_not_called()

    def test_generic_template_can_be_enabled_without_enabling_other_groups(self):
        p=self.root/'data/qq_tasks';cfg=json.loads((p/'spellcards.json').read_text())
        cfg.update(id='extra',title='另一任务',catalog='extra.md',times=['14:00'])
        (p/'extra.json').write_text(json.dumps(cfg));shutil.copyfile(p/'spellcards.md',p/'extra.md')
        self.assertIn('extra',self.cmd('/定时任务 列表'))
        self.assertIn('已开启',self.cmd('/定时任务 开启 extra'))
        self.assertEqual(self.run_at(),0)
        self.assertEqual(self.run_at(stamp(hour=14)),1)
        self.assertNotIn('spellcards',S._state(self.root)['groups']['GROUP_A'])

    def test_failed_claim_write_prevents_network_send(self):
        self.cmd()
        with patch.object(S.STORE,'write_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.run_at()
        self.send.assert_not_called()
        self.assertEqual(self.state()['high_water'],'')

    def test_bridge_uses_real_owner_and_passive_messages_never_execute_commands(self):
        import qq_bridge as B
        event=B._fake(content='/符卡 开启',group='GROUP_A')
        with patch.object(B.M,'ROOT',self.root):
            self.assertIn('仅主人',B.command_reply(event,{'is_owner':False}))
            self.assertIn('已开启',B.command_reply(event,{'is_owner':True}))
            self.cmd('/符卡 关闭')
            event.kind='group_message'
            with patch.object(B,'identify',return_value={'name':'fixture','is_owner':True}), \
                    patch.object(B,'hist_append'),patch.object(B,'command_reply') as command:
                B.Bridge().handle(event)
            command.assert_not_called()
            self.assertFalse(self.state()['enabled'])

    def test_stale_or_other_process_online_status_does_not_enable_worker(self):
        path=self.root/'data/qq_status.json'
        value={'pid':os.getpid(),'state':'ready','online':True,
               'at':datetime.now(timezone.utc).isoformat()}
        path.write_text(json.dumps(value));self.assertTrue(S.gateway_ready(self.root))
        value['pid']+=1;path.write_text(json.dumps(value));self.assertFalse(S.gateway_ready(self.root))
        value['pid']=os.getpid();value['at']=(datetime.now(timezone.utc)-timedelta(minutes=4)).isoformat()
        path.write_text(json.dumps(value));self.assertFalse(S.gateway_ready(self.root))

    def test_shutdown_wait_is_interruptible_after_one_send(self):
        self.cmd();self.cmd(group='GROUP_B')
        stop=threading.Event()
        def wait(seconds):
            stop.set()
            return True
        self.assertEqual(S.run_due(self.root,self.send,now=stamp(),wait=wait,stop=stop.is_set),1)
        self.send.assert_called_once()

    def test_next_day_continues_rotation_and_two_line_table_is_supported(self):
        self.cmd()
        self.run_at();self.run_at(stamp(hour=14));self.run_at(stamp(hour=22))
        self.run_at(stamp(day=9))
        self.assertIn('持有者：十六夜咲夜',self.send.call_args.args[1])
        cfg=S.templates(self.root)['spellcards'];p=self.root/'data/qq_tasks/spellcards.md'
        p.write_text('| 符卡 | 持有者 | 解说 |\n|---|---|---|\n| 梦符「春」 | 测试人物 | 花影<br>月色 |\n',encoding='utf-8')
        self.assertEqual(S.cards(self.root,cfg)[0]['intro'],'花影\n月色')


if __name__ == "__main__":
    unittest.main()
