"""Public synthetic files only; provider results are never fetched online."""
import io
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

import companion as C
import companion_ui as UI
import docx_read as D
import vision as V
import test_companion as TC
import test_desktop as TD


def docx(path, body, pictures=None):
    pictures = pictures or {'one.png': b'\x89PNG\r\n\x1a\nONE'}
    document = (f'<w:document xmlns:w="{D.W[1:-1]}" xmlns:a="{D.A[1:-1]}" '
                f'xmlns:r="{D.R[1:-1]}"><w:body>{body}</w:body></w:document>')
    rels = '<Relationships>' + ''.join(
        f'<Relationship Id="r{i}" Target="media/{name}"/>'
        for i, name in enumerate(pictures, 1)) + '</Relationships>'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', document)
        archive.writestr('word/_rels/document.xml.rels', rels)
        for name, data in pictures.items():
            archive.writestr('word/media/' + name, data)
    return path


def image(rid='r1'):
    return f'<w:r><w:drawing><a:blip r:embed="{rid}"/></w:drawing></w:r>'


def text(value):
    return f'<w:r><w:t>{value}</w:t></w:r>'


def success(value='识别内容'):
    return {'ok': True, 'text': value, 'error': '', 'cancelled': False}


class AttachmentFiles(unittest.TestCase):
    setUp = TC.Services.setUp
    tearDown = TC.Services.tearDown

    def test_desktop_reads_each_word_image_once_and_preserves_inline_order(self):
        path = docx(self.root / 'one.docx', '<w:p>' + text('Before') + image() + text('After') + '</w:p>')
        with patch.object(V, 'available', return_value=(True, '')), \
                patch.object(V, 'read_result', return_value=success('IMAGE')) as read:
            result = C.read_attachment(path)
        self.assertEqual(read.call_count, 1)
        self.assertLess(result['text'].index('Before'), result['text'].index('IMAGE'))
        self.assertLess(result['text'].index('IMAGE'), result['text'].index('After'))

    def test_table_images_and_repeated_reference_keep_their_positions(self):
        body = ('<w:p>' + text('A') + image() + '</w:p><w:tbl><w:tr><w:tc><w:p>'
                + text('B') + image('r2') + text('C')
                + '</w:p></w:tc></w:tr></w:tbl><w:p>' + image() + text('D') + '</w:p>')
        path = docx(self.root / 'table.docx', body, {'one.png': b'\x89PNGONE', 'two.png': b'\x89PNGTWO'})
        with patch.object(V, 'available', return_value=(True, '')), \
                patch.object(V, 'read_result', side_effect=[success('FIRST'), success('SECOND')]) as read:
            result = C.read_attachment(path)['text']
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result.count('FIRST'), 2)
        self.assertLess(result.index('FIRST'), result.index('SECOND'))
        self.assertLess(result.index('SECOND'), result.rindex('FIRST'))
        self.assertIn('B', result[:result.index('SECOND')])
        self.assertIn('C', result[result.index('SECOND'):])

    def test_any_failed_image_rejects_the_entire_word_attachment(self):
        path = docx(self.root / 'partial.docx', '<w:p>' + text('正文') + image() + image('r2') + '</w:p>',
                    {'one.png': b'\x89PNGONE', 'two.png': b'\x89PNGTWO'})
        failed = {'ok': False, 'text': '', 'error': '接口拒绝', 'cancelled': False}
        with patch.object(V, 'available', return_value=(True, '')), \
                patch.object(V, 'read_result', side_effect=[success(), failed]):
            with self.assertRaisesRegex(ValueError, '整份材料未添加'):
                C.read_attachment(path)

    def test_missing_vision_and_missing_relationship_fail_before_requests(self):
        path = docx(self.root / 'missing.docx', '<w:p>' + image() + '</w:p>')
        with patch.object(V, 'available', return_value=(False, '未配置')), \
                patch.object(V, 'read_result') as read:
            with self.assertRaisesRegex(ValueError, '未配置'):
                C.read_attachment(path)
            read.assert_not_called()
        path = docx(self.root / 'external.docx', '<w:p>' + image('external') + '</w:p>')
        with patch.object(V, 'read_result') as read:
            with self.assertRaisesRegex(ValueError, '缺失或外链'):
                C.read_attachment(path)
            read.assert_not_called()

    def test_image_failure_is_not_returned_as_material(self):
        path = self.root / 'synthetic.png'
        path.write_bytes(b'\x89PNG')
        with patch.object(V, 'read_result', return_value={'ok': False, 'error': '没有配置接口'}):
            with self.assertRaisesRegex(ValueError, '没有配置接口'):
                C.read_attachment(path)

    def test_concurrent_documents_use_distinct_private_temporary_directories(self):
        a = docx(self.root / 'a.docx', '<w:p>' + image() + '</w:p>', {'one.png': b'\x89PNG_A'})
        b = docx(self.root / 'b.docx', '<w:p>' + image() + '</w:p>', {'one.png': b'\x89PNG_B'})
        barrier = threading.Barrier(2)
        seen = []

        def read(path, *args, **kwargs):
            seen.append(path.parent)
            barrier.wait(timeout=3)
            return success(path.read_bytes()[-1:].decode())

        with patch.object(V, 'available', return_value=(True, '')), patch.object(V, 'read_result', side_effect=read):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(C.read_attachment, path) for path in (a, b)]
                values = [f.result(timeout=5) for f in futures]
        self.assertEqual(len(set(seen)), 2)
        self.assertTrue(all(not p.exists() for p in seen))
        self.assertTrue(values[0]['text'].endswith('A'))
        self.assertTrue(values[1]['text'].endswith('B'))
        self.assertFalse((self.root / '_docx_tmp').exists())

    def test_cancelled_word_starts_no_next_image(self):
        path = docx(self.root / 'cancel.docx', '<w:p>' + image() + image('r2') + '</w:p>',
                    {'one.png': b'\x89PNGONE', 'two.png': b'\x89PNGTWO'})
        cancel = threading.Event()

        def read(*args, **kwargs):
            cancel.set()
            return success()

        with patch.object(V, 'available', return_value=(True, '')), patch.object(V, 'read_result', side_effect=read) as call:
            with self.assertRaises(InterruptedError):
                C.read_attachment(path, cancelled=cancel.is_set)
        self.assertEqual(call.call_count, 1)

    def test_word_resource_limits_and_text_limit_fail_without_partial_attachment(self):
        path = docx(self.root / 'long.docx', '<w:p>' + text('字' * 100) + image() + '</w:p>')
        with patch.object(D, 'MAX_XML_BYTES', 32), patch.object(V, 'read_result') as call:
            self.assertIn('XML', D.read(path)['error'])
            call.assert_not_called()
        with patch.object(C, 'MAX_ATTACHMENT_CHARS', 50), patch.object(V, 'read_result') as call:
            with self.assertRaisesRegex(ValueError, '拆分'):
                C.read_attachment(path)
            call.assert_not_called()

    def test_vision_errors_empty_and_truncated_responses_are_explicit(self):
        path = self.root / 'picture.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nSYNTHETIC')
        configuration = {'key': 'synthetic-secret', 'base': 'https://example.invalid', 'model': 'synthetic'}
        for content, finish in [('', 'stop'), ('一半', 'length'), (['wrong type'], 'stop')]:
            payload = json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': finish}]}).encode()
            with patch.object(V, 'available', return_value=(True, '')), patch.object(V, 'config', return_value=configuration), \
                    patch.object(V.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
                result = V.read_result(path)
            self.assertFalse(result['ok'])
            self.assertEqual(result['text'], '')
        error = urllib.error.HTTPError('https://example.invalid', 401, 'no', {}, io.BytesIO(b'synthetic-secret'))
        with patch.object(V, 'available', return_value=(True, '')), patch.object(V, 'config', return_value=configuration), \
                patch.object(V.urllib.request, 'urlopen', side_effect=error):
            result = V.read_result(path)
        self.assertFalse(result['ok'])
        self.assertNotIn('synthetic-secret', result['error'])

    def test_malformed_vision_config_is_an_explicit_failure_without_a_request(self):
        path = self.root / 'picture.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nSYNTHETIC')
        secrets = self.root / 'synthetic-secrets.json'
        for value in ([], {'vision_base_url': 123}, {'vision_api_key': ['synthetic-secret']}):
            secrets.write_text(json.dumps(value), encoding='utf-8')
            with patch.object(V, 'SECRETS', secrets), patch.object(V.urllib.request, 'urlopen') as request:
                result = V.read_result(path)
            self.assertFalse(result['ok'])
            self.assertEqual(result['text'], '')
            self.assertNotIn('synthetic-secret', result['error'])
            request.assert_not_called()


class AttachmentUI(unittest.TestCase):
    setUp = TD.Desktop.setUp
    tearDownData = TC.Services.tearDown

    def tearDown(self):
        self.chat.prepare_quit()
        for task in list(self.chat._attachment_jobs.values()):
            task.cancel()
            task.thread.join(timeout=3)
        TD.APP.processEvents()
        TD.Desktop.tearDown(self)

    def finish_reads(self):
        for _ in range(200):
            QTest.qWait(10)
            if not self.chat._attachment_jobs:
                return
        self.fail('Attachment worker did not finish')

    def test_slow_attachment_keeps_event_loop_responsive_and_cannot_send_early(self):
        release = threading.Event()
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()

        def slow(*args, **kwargs):
            release.wait(2)
            return {'name': 'slow.png', 'text': '成功内容'}

        with patch.object(C, 'read_attachment', side_effect=slow):
            self.chat.load_attachment('slow.png')
            try:
                self.chat.input.setPlainText('解释图片')
                self.chat.send()
                self.assertEqual(self.store.messages(), [])
                QTest.qWait(120)
                self.assertGreater(len(ticks), 3)
            finally:
                release.set()
            self.finish_reads()
        timer.stop()
        self.assertEqual(self.chat.attachment['text'], '成功内容')
        self.assertTrue(self.chat.btn.isEnabled())

    def test_cancel_topic_change_replace_and_quit_discard_late_results(self):
        for action in ('remove', 'topic', 'replace', 'quit'):
            release = threading.Event()
            started = threading.Event()
            self.chat._closing_application = False

            def slow(path, **kwargs):
                started.set()
                release.wait(2)
                return {'name': str(path), 'text': 'LATE_OLD_RESULT'}

            with patch.object(C, 'read_attachment', side_effect=slow):
                self.chat.load_attachment('old.png')
                try:
                    self.assertTrue(started.wait(2))
                    if action == 'remove':
                        self.chat.remove_attachment()
                    elif action == 'topic':
                        self.chat.new_topic()
                    elif action == 'replace':
                        with patch.object(C, 'read_attachment', return_value={'name': 'new.md', 'text': 'NEW_RESULT'}):
                            self.chat.load_attachment('new.md')
                    else:
                        self.chat.prepare_quit()
                finally:
                    release.set()
                self.finish_reads()
            self.assertNotIn('LATE_OLD_RESULT', json.dumps(self.chat.desktop_state.data))
            self.assertNotEqual((self.chat.attachment or {}).get('text'), 'LATE_OLD_RESULT')
            if action == 'replace':
                self.assertEqual(self.chat.attachment['text'], 'NEW_RESULT')

    def test_failed_word_does_not_replace_draft_with_error_material(self):
        with patch.object(C, 'read_attachment', side_effect=ValueError('图片2失败；整份材料未添加')):
            self.chat.load_attachment('broken.docx')
            self.finish_reads()
        self.assertIsNone(self.chat.attachment)
        self.assertNotIn('图片2失败', json.dumps(self.chat.desktop_state.data))
        self.assertTrue(self.chat.btn.isEnabled())


if __name__ == '__main__':
    unittest.main()
