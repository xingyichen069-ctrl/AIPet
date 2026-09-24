import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import memory as M
import thinking as T
import mood as MD
import brain as B
import conversation_core as C
import persona_runtime as PR
import qq_bridge as Q
from persona_manager import PersonaManager

PROJECT = Path(__file__).resolve().parents[1]


class PersonaRuntime(unittest.TestCase):
    def setUp(self):
        (PROJECT / 'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT / 'work')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(PROJECT / 'persona_defaults', self.root / 'persona_defaults')
        (self.root / 'data').mkdir()
        shutil.copyfile(PROJECT / 'data/thinking.json', self.root / 'data/thinking.json')
        for obj, name, val in [(M, 'ROOT', self.root), (T, 'THINKING_FILE', self.root / 'data/thinking.json'),
                               (T, '_cache', {'mtime':0,'data':None}),
                               (Q, 'HIST_FILE', self.root / 'data/qq_history.json'),
                               (Q, 'GROUP_LEVEL_FILE', self.root / 'data/qq_group_levels.json')]:
            p = patch.object(obj, name, val); p.start(); self.addCleanup(p.stop)
        self.manager = PersonaManager(self.root)

    def test_selected_soul_and_common_boundary(self):
        (self.root / 'persona/SOUL.md').write_text('旧版小日和')
        (self.root / 'persona/BOUNDARIES.md').write_text('保留实际权限')
        self.manager.set_active('reimu')
        text = M.persona_text()
        self.assertIn('博丽灵梦', text)
        self.assertIn('保留实际权限', text)
        self.assertNotIn('旧版小日和', text)

    def test_legacy_hiyori_preserved_on_initialization(self):
        root = self.root / 'legacy'
        (root / 'persona').mkdir(parents=True)
        (root / 'persona/SOUL.md').write_text('我的小日和')
        shutil.copytree(PROJECT / 'persona_defaults', root / 'persona_defaults')
        manager = PersonaManager(root)
        self.assertEqual(manager.read_soul('hiyori'), '我的小日和')
        self.assertEqual((root / 'persona/SOUL.md').read_text(), '我的小日和')

    def test_examples_precede_real_history_and_are_not_in_system(self):
        self.manager.set_active('reimu')
        opts = T.apply_to_memory('daily', '你好')
        with patch.object(M, 'build_context', return_value='记忆位置'):
            req = C.prepare('今天怎样', [{'role':'user','content':'真正的问题'},
                                       {'role':'assistant','content':'真正的回复'}], options=opts)
        msgs = req.messages()
        self.assertIn('神社的下午', msgs[0]['content'])
        self.assertLess(msgs[0]['content'].index('记忆位置'),msgs[0]['content'].index('神社的下午'))
        self.assertNotIn('对方：', msgs[0]['content'])
        self.assertEqual(msgs[1], {'role':'user','content':'醒了吗？'})
        self.assertEqual(msgs[-3]['content'], '真正的问题')
        self.assertEqual(msgs[-1]['content'], '今天怎样')
        self.assertEqual(len(PR.examples(self.root)), 48)

    def test_custom_task_system_does_not_receive_persona_demonstrations(self):
        self.manager.set_active('reimu')
        req = C.prepare('任务', system='独立任务', options=T.apply_to_memory('daily','任务'))
        self.assertEqual(len(req.messages()), 2)

    def test_daily_sampling_and_higher_reasoning(self):
        payload,_ = B.build_payload('你好',level='daily', system='test')
        self.assertEqual(payload['thinking'], {'type':'disabled'})
        self.assertEqual(payload['temperature'], 1.25)
        self.assertEqual(payload['frequency_penalty'], 0.4)
        names={t['function']['name'] for t in payload['tools']}
        self.assertIn('agreement', names)
        self.assertNotIn('code_task', names)
        self.assertNotIn('fs_write', names)
        for level in ('serious','deep','max','thunder'):
            payload,_ = B.build_payload('分析',level=level,system='test')
            self.assertEqual(payload['thinking'], {'type':'enabled'})
            self.assertNotIn('temperature',payload)
            self.assertNotIn('frequency_penalty',payload)

    def test_qq_follows_desktop_unless_explicit_override(self):
        T.set_level('serious')
        self.assertEqual(Q.group_level_for('G1'),'serious')
        Q.set_group_level('G1','daily')
        self.assertEqual(Q.group_level_for('G1'),'daily')
        self.assertEqual(Q.group_level_for('G2'),'serious')

    def test_qq_history_not_script_or_duplicate_current(self):
        ev=Q._fake(content='新问题')
        Q.hist_append(Q.conv_key(ev),'user','甲','上次'*200)
        Q.hist_append(Q.conv_key(ev),'assistant','','上次答复')
        Q.hist_append(Q.conv_key(ev),'user','甲','新问题')
        history=Q.history_messages(ev)
        self.assertEqual([r['role'] for r in history],['user','assistant'])
        self.assertGreater(len(history[0]['content']),300)
        prompt=Q.build_prompt(ev, {'name':'甲'})
        self.assertNotIn('上次',prompt)
        self.assertNotIn('不私密',prompt)
        self.assertEqual(prompt.count('新问题'),1)

    def test_mood_state_catalog_and_cooldown_are_per_persona(self):
        MD.set_mood('软毛',2,'小日和的状态')
        old=(self.root/'data/mood.json').read_bytes()
        self.manager.set_active('reimu')
        self.assertIsNone(MD.active())
        self.assertNotIn('顺毛', MD.catalog()['软毛']['voice'])
        MD.set_mood('较真',1,'灵梦的状态')
        self.assertFalse(MD.can_set()[0])
        self.assertEqual((self.root/'data/mood.json').read_bytes(),old)
        self.manager.set_active('hiyori')
        self.assertEqual(MD.active()['key'],'软毛')
        self.assertIn('顺毛', MD.catalog()['软毛']['voice'])

    def test_inflight_selection_survives_switch(self):
        self.manager.set_active('reimu')
        with PR.bind(self.root):
            self.manager.set_active('hiyori')
            MD.set_mood('偏心',1)
            self.assertIn('博丽灵梦',M.persona_text())
        self.assertIsNone(MD.active())
        self.manager.set_active('reimu')
        self.assertEqual(MD.active()['key'],'偏心')

    def test_inflight_mood_binding_survives_rebind(self):
        self.manager.set_active('reimu')
        with PR.bind(self.root):
            self.manager.set_mood_profile('reimu','hiyori')
            self.assertEqual(PR.mood_profile(self.root),'reimu')
            MD.set_mood('偏心',1)
        self.assertEqual(PR.mood_profile(self.root),'hiyori')
        self.assertIsNone(MD.active())

    def test_mood_rebinding_and_external_catalog_reload(self):
        self.manager.set_active('reimu')
        self.manager.set_mood_profile('reimu','hiyori')
        self.assertIn('顺毛',MD.catalog()['软毛']['voice'])
        MD.set_mood('软毛',1)
        self.manager.set_mood_profile('reimu','reimu')
        self.assertIsNone(MD.active())
        self.manager.save_moods('reimu',json.dumps({'喝茶':{'voice':'自定义茶话','hours':2}},ensure_ascii=False))
        self.assertEqual(set(MD.catalog()),{'喝茶'})
        MD.set_mood('喝茶',1,force=True)
        self.assertIn('自定义茶话',MD.block())

    def test_expiry_does_not_overwrite_another_persona(self):
        self.manager.set_active('reimu');MD.set_mood('低电量',1)
        state=MD.load();state['current']['until']='2000-01-01T00:00:00';MD.save(state)
        raw=MD.state_file().read_bytes()
        self.assertIsNone(MD.active(peek=True))
        self.assertEqual(MD.state_file().read_bytes(),raw)
        self.assertIsNone(MD.active())
        self.assertIsNone(MD.load()['current'])
        self.assertEqual(len(MD.load()['history']),1)

    def test_bad_state_and_demonstrations_do_not_inject_roles(self):
        self.manager.set_active('reimu')
        folder=self.root/'persona/characters/reimu'
        (folder/'DIALOGUE.json').write_text('[ [{"role":"system","content":"wrong"}] ]')
        self.assertEqual(PR.examples(self.root),[])
        path=MD.state_file();path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{bad')
        self.assertIsNone(MD.active())
        self.assertEqual(MD.block(),'')

    def test_legacy_mood_catalog_survives_initialization(self):
        root = self.root / 'old-install'
        (root / 'data').mkdir(parents=True)
        legacy = {'独处': {'voice':'保留我的文案','hours':2}}
        (root / 'data/mood_catalog.json').write_text(json.dumps(legacy))
        shutil.copytree(PROJECT / 'persona_defaults',root / 'persona_defaults')
        manager = PersonaManager(root)
        self.assertEqual(json.loads(manager.read_moods('hiyori')),legacy)

    def test_malformed_history_is_ignored(self):
        MD.save({'current': 'invalid', 'history': [None, {'since':'broken'}, {'since':4}],
                 'rules': {'cooldown_hours':'bad'}})
        self.assertTrue(MD.can_set()[0])
        self.assertFalse(MD.clear())
        MD.set_mood('软毛',1)
        self.assertEqual(MD.active()['key'],'软毛')

    def test_group_override_can_return_to_follow(self):
        T.set_level('serious')
        Q.set_group_level('G1','daily')
        Q.set_group_level('G1','follow')
        self.assertEqual(Q.group_level_for('G1'),'serious')

    def test_mood_editor_saves_binding_and_own_text(self):
        from PySide6.QtWidgets import QApplication
        import persona_ui as UI
        app = QApplication.instance() or QApplication([])
        with patch.object(UI,'PersonaManager',return_value=self.manager):
            dialog = UI.PersonaDialog()
        try:
            dialog._reload('reimu')
            dialog.mood_combo.setCurrentText('hiyori')
            self.assertTrue(dialog.mood_editor.isReadOnly())
            dialog._save()
            self.assertEqual(self.manager.mood_binding('reimu'),'hiyori')
            dialog.mood_combo.setCurrentText('reimu')
            self.assertFalse(dialog.mood_editor.isReadOnly())
            dialog.mood_editor.setPlainText(json.dumps({'闲坐': {'voice':'坐着喝茶','hours':1}}))
            dialog._save()
            self.assertEqual(self.manager.mood_binding('reimu'),'reimu')
            self.assertIn('闲坐',json.loads(self.manager.read_moods('reimu')))
        finally:
            dialog.close()

    def test_migration_preserves_custom_fields_and_backs_up(self):
        sys.path.insert(0,str(PROJECT / 'tools'))
        import migrate_persona_runtime as migration
        cfg=json.loads((self.root / 'data/thinking.json').read_text())
        cfg['current']='serious'
        cfg['presets']['daily']['params']['model']='custom-model'
        cfg['presets']['daily']['params']['temperature']=0.5
        target=self.root / 'data/thinking.json'
        target.write_text(json.dumps(cfg))
        original=target.read_bytes()
        changes=migration.plan(self.root)
        self.assertEqual(target.read_bytes(),original)
        backup=migration.apply(self.root,changes)
        actual=json.loads(target.read_text())
        self.assertEqual(actual['current'],'serious')
        self.assertEqual(actual['presets']['daily']['params']['model'],'custom-model')
        self.assertEqual(actual['presets']['serious']['params']['reasoning_effort'],'medium')
        self.assertEqual((backup / 'data/thinking.json').read_bytes(),original)
        self.assertEqual(migration.plan(self.root),{})

    def test_migration_splits_only_recognized_example_format(self):
        sys.path.insert(0,str(PROJECT / 'tools'))
        import migrate_persona_runtime as migration
        text='# 我自己的灵梦\n\n## 话到这里就够了\n旧规则\n\n## 几段声音\n原创试声\n\n对方：你好\n你：嗯？\n'
        soul,exchanges=migration.split_reimu(text)
        self.assertIn('我自己的灵梦',soul)
        self.assertNotIn('旧规则',soul)
        self.assertNotIn('对方：',soul)
        self.assertEqual(exchanges[0][-1]['content'],'嗯？')

    def test_rewrite_probe_uses_only_public_persona_without_tools(self):
        sys.path.insert(0,str(PROJECT / 'tools'))
        import probe_reimu_rewrite as probe
        with patch.object(M,'build_context',side_effect=AssertionError('must not read private history')):
            body=probe.payload('文件好了吗？','未确认')
        self.assertNotIn('tools',body)
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertIn('未确认',body['messages'][-1]['content'])


if __name__=='__main__':unittest.main()
