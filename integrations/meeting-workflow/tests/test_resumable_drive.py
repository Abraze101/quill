"""Synthetic resumable protocol checks. No credentials, network, or recordings."""
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.drive_upload import UploadNeedsReview, UploadRetryable, file_identity
from quill_workflow.pipeline import NeedsConfiguration
from quill_workflow.resumable_drive import CHUNK_BYTES, HTTPResponse, ResumableDriveUploader

OWNER = 'owner@example.test'
PARENT = 'parent_folder_123'
FILE_ID = 'reserved_file_123'
SESSION = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=synthetic'


def response(status=200, value=None, headers=None):
    return HTTPResponse(status, headers or {}, json.dumps(value).encode() if value is not None else b'')


def private(identity, mime):
    return {'id': identity, 'mimeType': mime, 'trashed': False, 'shared': False,
            'capabilities': {'canShare': True}, 'permissions': [
                {'type': 'user', 'role': 'owner', 'emailAddress': OWNER}],
            'owners': [{'emailAddress': OWNER, 'me': True}]}


class FakeDrive:
    def __init__(self):
        self.calls = []
        self.parent = private(PARENT, 'application/vnd.google-apps.folder')
        self.item = None
        self.metadata = None
        self.offset = 0
        self.md5 = hashlib.md5()
        self.expired = False
        self.starts = 0
        self.before = None
        self.after_chunk = None
        self.location = SESSION
        self.operation = None
        self.saved = None
        self.ack_limit = None

    def __call__(self, method, url, *, headers, body, timeout):
        path = urlsplit(url).path
        call = (method, path, headers.get('Content-Range'), len(body or b''))
        self.calls.append(call)
        assert 0 < timeout <= 60
        assert len(body or b'') <= CHUNK_BYTES
        if self.before:
            override = self.before(method, url, headers, body)
            if override:
                return override
        if path.endswith('/generateIds'):
            return response(value={'ids': [FILE_ID]})
        if method == 'GET' and path.endswith('/' + PARENT):
            return response(value=self.parent)
        if method == 'GET' and path.endswith('/' + FILE_ID):
            return response(value=self.item) if self.item else response(404)
        if method == 'POST':
            assert self.saved['resumable']['file_id'] == FILE_ID
            assert 'id' not in self.saved
            self.metadata = json.loads(body)
            if self.item:
                return response(409)
            self.starts += 1
            self.offset = 0
            self.md5 = hashlib.md5()
            self.expired = False
            return response(headers={'Location': self.location})
        if method == 'PUT':
            assert self.saved['resumable']['session_uri'] == SESSION
            if self.expired:
                return response(404)
            if headers['Content-Range'].startswith('bytes */'):
                if self.item:
                    return response(200)
                return response(308, headers={'Range': f'bytes=0-{self.offset - 1}'} if self.offset else {})
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', headers['Content-Range'])
            start, end, total = map(int, match.groups())
            assert start == self.offset
            assert len(body) == end - start + 1
            received = min(len(body), self.ack_limit) if self.ack_limit else len(body)
            self.ack_limit = None
            self.md5.update(body[:received])
            self.offset += received
            if self.offset == total:
                self.item = {**private(FILE_ID, self.metadata['mimeType']), **self.metadata,
                             'md5Checksum': self.md5.hexdigest(), 'size': str(total)}
            if self.after_chunk:
                self.after_chunk(self)
            return response(201) if self.item else response(308, headers={'Range': f'bytes=0-{self.offset - 1}'})
        raise AssertionError(call)


class ResumableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'synthetic.mp4'
        self.source.write_bytes(b'synthetic media\n' * 40000)
        self.file = file_identity(self.source)
        self.operation = {}
        self.drive = FakeDrive()
        self.drive.operation = self.operation
        self.guard = lambda: None
        self.uploader = ResumableDriveUploader(lambda: 'fake-token', self.drive)

    def save(self):
        self.drive.saved = copy.deepcopy(self.operation)

    def upload(self, **overrides):
        kwargs = dict(source=self.source, file=self.file, parent=PARENT, name='Meeting.mp4',
                      mime='video/mp4', owner=OWNER, operation=self.operation,
                      save=self.save, guard=self.guard)
        kwargs.update(overrides)
        return self.uploader.upload(**kwargs)

    def pause_after_chunk(self):
        def stop(drive):
            drive.after_chunk = None
            raise OSError('private session URL must never escape')
        self.drive.after_chunk = stop

    def test_success_and_completed_retry_only_reads_exact_id(self):
        self.assertEqual(self.upload(), {'success': True, 'id': FILE_ID})
        self.assertTrue(self.operation['resumable']['complete'])
        self.drive.calls.clear()
        self.upload()
        self.assertTrue(all(call[0] == 'GET' for call in self.drive.calls))
        self.assertEqual(self.drive.starts, 1)

    def test_sparse_file_larger_than_connector_limit_is_bounded(self):
        with self.source.open('wb') as handle:
            handle.truncate(513 * 1024 * 1024 + 1)
        self.file = file_identity(self.source)
        self.upload()
        chunks = [call for call in self.drive.calls if call[0] == 'PUT' and call[3]]
        self.assertEqual(sum(call[3] for call in chunks), self.file['size'])
        self.assertTrue(all(call[3] == CHUNK_BYTES for call in chunks[:-1]))
        self.assertEqual(self.drive.starts, 1)

    def test_interruption_resumes_using_remote_partial_offset(self):
        self.drive.ack_limit = 43
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.assertNotIn('id', self.operation)
        self.assertEqual(self.operation['resumable']['offset'], 0)
        self.drive.calls.clear()
        self.upload()
        puts = [call for call in self.drive.calls if call[0] == 'PUT']
        self.assertEqual(puts[0][2], f'bytes */{self.file["size"]}')
        self.assertTrue(puts[1][2].startswith('bytes 43-'))
        self.assertEqual(self.drive.starts, 1)

    def test_restart_new_instance_after_final_response_loss_reads_completed_file(self):
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.assertIsNotNone(self.drive.item)
        self.operation = json.loads(json.dumps(self.drive.saved))
        self.uploader = ResumableDriveUploader(lambda: 'fake-token', self.drive)
        self.drive.calls.clear()
        self.upload()
        self.assertEqual(self.operation['id'], FILE_ID)
        self.assertTrue(all(call[0] == 'GET' for call in self.drive.calls))

    def test_expired_session_restarts_same_reserved_id(self):
        self.drive.ack_limit = 43
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.drive.expired = True
        self.upload()
        self.assertEqual(self.drive.starts, 2)
        self.assertEqual(self.operation['id'], FILE_ID)
        self.assertEqual(len([c for c in self.drive.calls if c[1].endswith('generateIds')]), 1)

    def test_lost_initiation_response_reuses_reserved_id(self):
        original = self.drive.__call__
        failed = False
        def request(method, url, **kwargs):
            nonlocal failed
            result = original(method, url, **kwargs)
            if method == 'POST' and not failed:
                failed = True
                raise OSError('synthetic')
            return result
        self.uploader.request = request
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.assertNotIn('session_uri', self.operation['resumable'])
        self.upload()
        self.assertEqual(self.drive.starts, 2)
        self.assertEqual(len([c for c in self.drive.calls if c[1].endswith('generateIds')]), 1)

    def test_foreign_or_changed_completed_file_is_not_adopted(self):
        for field, value in [('appProperties', {}), ('size', '1'), ('md5Checksum', 'bad'),
                             ('parents', ['different_parent']), ('name', 'renamed'),
                             ('shared', True), ('trashed', True), ('capabilities', None),
                             ('permissions', []), ('owners', [{'emailAddress': OWNER, 'me': False}])]:
            with self.subTest(field=field):
                self.setUp()
                self.upload()
                self.drive.item[field] = value
                self.drive.calls.clear()
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertTrue(all(c[0] == 'GET' for c in self.drive.calls))

    def test_changed_source_or_identity_does_not_send(self):
        self.drive.ack_limit = 43
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.drive.calls.clear()
        with self.assertRaises(UploadNeedsReview):
            self.upload(name='Changed.mp4')
        self.source.write_bytes(b'changed')
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(self.drive.calls, [])

    def test_symlink_source_does_not_send(self):
        original = self.source.with_suffix('.original')
        self.source.rename(original)
        self.source.symlink_to(original)
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(self.drive.calls, [])

    def test_source_mutates_between_chunks(self):
        def mutate(drive):
            self.source.write_bytes(b'x' * self.file['size'])
        self.drive.after_chunk = mutate
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertNotIn('id', self.operation)

    def test_malicious_session_address_never_gets_media(self):
        for url in ['http://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=x',
                    'https://evil.test/upload/drive/v3/files?uploadType=resumable&upload_id=x',
                    'https://www.googleapis.com.evil.test/upload/drive/v3/files?uploadType=resumable&upload_id=x',
                    'https://www.googleapis.com@evil.test/upload/drive/v3/files?uploadType=resumable&upload_id=x',
                    'https://www.googleapis.com/other?uploadType=resumable&upload_id=x', None]:
            with self.subTest(url=url):
                self.setUp()
                self.drive.location = url
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertFalse(any(c[0] == 'PUT' for c in self.drive.calls))
                self.assertNotIn('session_uri', self.operation['resumable'])

    def test_failures_keep_session_and_do_not_leak_body(self):
        for status, reason, error in [(401, 'auth', NeedsConfiguration), (429, 'rate', UploadRetryable),
                                      (500, 'backend', UploadRetryable), (503, 'backend', UploadRetryable),
                                      (403, 'rateLimitExceeded', UploadRetryable),
                                      (403, 'insufficientPermissions', NeedsConfiguration),
                                      (302, 'redirect', UploadNeedsReview)]:
            with self.subTest(status=status, reason=reason):
                self.setUp()
                self.drive.before = lambda method, url, headers, body: response(
                    status, {'secret': 'do-not-leak', 'error': {'errors': [{'reason': reason}]}}) if method == 'PUT' else None
                with self.assertRaises(error) as caught:
                    self.upload()
                self.assertNotIn('do-not-leak', str(caught.exception))
                self.assertEqual(self.operation['resumable']['session_uri'], SESSION)
                self.assertNotIn('id', self.operation)

    def test_parent_becomes_shared_mid_upload_stops_next_chunk(self):
        self.drive.ack_limit = 43
        self.drive.after_chunk = lambda drive: drive.parent.update(shared=True)
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(len([c for c in self.drive.calls if c[0] == 'PUT']), 1)

    def test_wrong_account_parent_stops_before_create(self):
        self.drive.parent['owners'][0]['me'] = False
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertFalse(any(c[0] == 'POST' for c in self.drive.calls))

    def test_hold_appearing_during_authorization_stops_the_media_request(self):
        held = False
        def token():
            nonlocal held
            held = True
            return 'synthetic-token'
        def guard():
            if held:
                raise NeedsConfiguration('Synthetic recording review hold')
        self.uploader.access_token = token
        self.guard = guard
        with self.assertRaisesRegex(NeedsConfiguration, 'recording review hold'):
            self.upload()
        self.assertEqual(self.drive.calls, [])

    def test_disable_guard_mid_upload_stops_next_request(self):
        enabled = True
        def guard():
            if not enabled:
                raise NeedsConfiguration('Uploads are off')
        def disable(drive):
            nonlocal enabled
            enabled = False
        self.guard = guard
        self.drive.ack_limit = 43
        self.drive.after_chunk = disable
        with self.assertRaises(NeedsConfiguration):
            self.upload()
        self.assertEqual(len([c for c in self.drive.calls if c[0] == 'PUT']), 1)
        self.assertNotIn('id', self.operation)

    def test_invalid_remote_range_holds_without_guessing_offset(self):
        self.drive.before = lambda method, url, headers, body: response(308, headers={'Range': 'bytes=5-10'}) if method == 'PUT' else None
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertNotIn('id', self.operation)

    def test_deadline_stops_after_chunk_and_keeps_saved_session(self):
        current = 0
        self.uploader.clock = lambda: current
        def expire(drive):
            nonlocal current
            current = 1201
        self.drive.after_chunk = expire
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.assertIn('session_uri', self.operation['resumable'])
        self.assertNotIn('id', self.operation)

    def test_failed_journal_save_prevents_create(self):
        def fail():
            if self.operation.get('resumable', {}).get('file_id'):
                raise OSError('disk full')
            self.save()
        with self.assertRaises(OSError):
            self.upload(save=fail)
        self.assertFalse(any(c[0] == 'POST' for c in self.drive.calls))

    def test_failed_session_save_prevents_media(self):
        def fail():
            if self.operation.get('resumable', {}).get('session_uri'):
                raise OSError('disk full')
            self.save()
        with self.assertRaises(OSError):
            self.upload(save=fail)
        self.assertFalse(any(c[0] == 'PUT' for c in self.drive.calls))

    def test_same_id_conflict_is_reconciled_without_another_reservation(self):
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        completed = self.drive.item
        self.drive.item = None
        self.operation['resumable'].pop('session_uri')
        def conflict(method, url, headers, body):
            if method == 'POST':
                self.drive.item = completed
                return response(409)
        self.drive.before = conflict
        self.upload()
        self.assertEqual(self.operation['id'], FILE_ID)
        self.assertEqual(len([c for c in self.drive.calls if c[1].endswith('generateIds')]), 1)

    def test_source_same_size_different_hash_stops_before_network(self):
        self.source.write_bytes(b'x' * self.file['size'])
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(self.drive.calls, [])

    def test_status_query_auth_failure_does_not_replace_session(self):
        self.drive.ack_limit = 43
        self.pause_after_chunk()
        with self.assertRaises(UploadRetryable):
            self.upload()
        self.drive.before = lambda method, url, headers, body: response(401) if method == 'PUT' else None
        with self.assertRaises(NeedsConfiguration):
            self.upload()
        self.assertEqual(self.operation['resumable']['session_uri'], SESSION)
        self.assertEqual(self.drive.starts, 1)

    def test_preflight_rejects_token_without_network(self):
        self.uploader.access_token = lambda: 'unsafe\ntoken'
        with self.assertRaises(NeedsConfiguration):
            self.uploader.preflight()
        self.assertEqual(self.drive.calls, [])


if __name__ == '__main__':
    unittest.main()
