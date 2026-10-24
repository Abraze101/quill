"""Synthetic eligibility and privacy tests for notes-sharing previews."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.drive_upload import DOCUMENT_MIME, _hash, native_template_sha256
from quill_workflow.native_template import build_payload
from quill_workflow.pipeline import Queue, atomic_json, route_settings
from quill_workflow.routing import RoutingReviews
from quill_workflow.sharing_source import SharingSourceError, sharing_snapshot
from test_native_template import template_document


def receipt_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SharingSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.job = 'synthetic-meeting'
        self.original = self.root / 'recordings/Original'
        self.original.mkdir(parents=True)
        self.recording = self.state / 'recordings' / self.job
        self.result = self.state / 'results' / self.job
        self.metadata = {'started': '2026-10-09T19:00:00+08:00', 'ended': '2026-10-09T19:01:00+08:00',
                         'recording_timezone': 'Asia/Singapore',
                         'files': {'mic': 'mic.caf', 'system': 'system.caf'}}
        atomic_json(self.original / 'meta.json', self.metadata)
        atomic_json(self.recording / 'meta.json', self.metadata)
        self.transcript = {'segments': [{'start_ms': 0, 'end_ms': 1000, 'speaker': 'me',
                                         'text': 'Hi Max. PRIVATE TRANSCRIPT SENTINEL.'}]}
        atomic_json(self.recording / 'transcript.json', self.transcript)
        self.transcript_bytes = (self.recording / 'transcript.json').read_bytes()
        self.transcript_hash = hashlib.sha256(self.transcript_bytes).hexdigest()
        self.notes = {'schema_version': 1, 'meeting_title': {'text': 'Dashboard review', 'evidence': [0],
                      'participants': [{'name': 'Max', 'evidence': [0]}]},
                      'summary': [{'text': 'Review the dashboard.', 'evidence': [0]}],
                      'topics': [{'text': 'Dashboard layout.', 'evidence': [0]}],
                      'decisions': [{'text': 'Keep the calendar view.', 'evidence': [0]}],
                      'actions': [{'text': 'Send the draft.', 'owner': 'Max', 'due': None, 'evidence': [0]}],
                      'next_steps': [{'text': 'Review on Friday.', 'evidence': [0]}],
                      'open_questions': [{'text': 'Confirm the next date.', 'evidence': [0]}],
                      'competitive_intel': [{'text': 'INTERNAL INTELLIGENCE SENTINEL.', 'evidence': [0]}],
                      'deal_implications': [{'text': 'INTERNAL ASSESSMENT SENTINEL.', 'evidence': [0]}],
                      'customer_priorities': [{'text': 'PRIVATE PRIORITIES SENTINEL.', 'evidence': [0]}],
                      'objections': [{'text': 'PRIVATE OBJECTION SENTINEL.', 'evidence': [0]}],
                      'follow_up_draft': {'subject': 'INTERNAL DRAFT SUBJECT', 'body': 'INTERNAL DRAFT BODY.', 'evidence': [0]}}
        self.template = template_document()
        self.config = {'route': route_settings({'enabled': True, 'companies': [
            {'id': 'example', 'name': 'Example', 'drive_folder_id': 'company_folder_123'}]}),
            'publish': {'type': 'codex_drive', 'enabled': True, 'text_format': 'native_template_google_doc_v1',
                        'meetings_root_id': 'meetings_root_123', 'template_document_id': 'template_document_123',
                        'template_sha256': native_template_sha256(self.template), 'meeting_timezone': 'Asia/Singapore'},
            'notes': {'enabled': True, 'command': ['synthetic']}}
        self.config_path = self.root / 'config.json'
        atomic_json(self.config_path, self.config)
        self.reviews = RoutingReviews(self.state)
        review = self.reviews.propose(self.job, self.transcript_bytes, self.config['route']['companies'],
                                     {'schema_version': 1, 'company_id': 'example', 'confidence': 'high', 'reason': 'Synthetic evidence',
                                      'ambiguous': False, 'evidence': [0]})
        self.reviews.approve(self.job, 'example', review['revision'])
        self.route = self.reviews.route(self.job, self.transcript_bytes, self.config['route']['companies'])
        self.queue = Queue(self.state)
        with self.queue.db:
            self.queue.db.execute('INSERT INTO jobs VALUES (?,?,?)', (self.job, str(self.original), 1))
        self.publish_fixture()

    def publish_fixture(self):
        payload = build_payload(self.notes, self.transcript, self.metadata, self.route, 'Asia/Singapore')
        self.identity = {'job': self.job, 'stage': 'publish', 'idempotency_key': self.job + ':publish',
                         'meetings_root_id': 'meetings_root_123', 'route': self.route,
                         'started': self.metadata['started'], 'files': [{'name': 'native-template',
                         'mime_type': DOCUMENT_MIME, 'payload_sha256': _hash(payload)}],
                         'source': str(self.result), 'meeting_timezone': 'Asia/Singapore',
                         'text_format': 'native_template_google_doc_v1',
                         'template_document_id': 'template_document_123',
                         'template_sha256': self.config['publish']['template_sha256'], 'document_title': payload['title']}
        remote = dict(self.identity['files'][0], id='private_document_123', verification='metadata_and_template_content')
        self.external_receipt = {'job': self.job, 'stage': 'publish', 'idempotency_key': self.job + ':publish',
                                'folder_id': 'notes_folder_123', 'meeting_folder_id': 'meeting_folder_123',
                                'files': [remote], 'verification': 'metadata_and_template_content'}
        self.journal = {'schema_version': 1, 'identity': self.identity, 'identity_sha256': _hash(self.identity),
                        'owner': 'owner@example.test', 'receipt': self.external_receipt, 'operations': {
                        'stage': {'state': 'verified', 'id': 'notes_folder_123', 'parent': 'meeting_folder_123'},
                        'file:native-template': {'state': 'verified', 'id': 'private_document_123',
                        'parent': 'notes_folder_123', 'name': payload['title'], 'verification': 'metadata_and_template_content',
                        'fill': {'state': 'verified', 'template_document': self.template}}}}
        atomic_json(self.result / 'drive-publish.json', self.journal)
        for stage in ('notes', 'route', 'publish'):
            receipt = {'job': self.job, 'stage': stage, 'idempotency_key': self.job + ':' + stage,
                       'input_sha256': self.transcript_hash, 'settings_sha256': receipt_hash(self.config[stage])}
            if stage == 'publish':
                receipt.update(self.external_receipt, route_sha256=receipt_hash(self.route))
            elif stage == 'route':
                receipt['route'] = self.route
            atomic_json(self.result / (stage + '-receipt.json'), receipt)
            with self.queue.db:
                self.queue.db.execute('INSERT OR REPLACE INTO tasks (job,stage,status,settings,receipt) VALUES (?,?,?,?,?)',
                    (self.job, stage, 'succeeded', json.dumps(self.config[stage]), json.dumps(receipt)))
            if stage == 'notes':
                atomic_json(self.result / 'notes/provenance.json', receipt)
        atomic_json(self.result / 'notes/notes.json', self.notes)
        (self.result / 'notes/source-transcript.json').write_bytes(self.transcript_bytes)

    def tearDown(self):
        self.queue.close()
        self.reviews.close()
        self.temp.cleanup()

    def snapshot(self):
        return sharing_snapshot(self.state, self.config_path, self.job)

    def test_notes_only_sharing_and_review_keep_quality_notice_without_private_source(self):
        from quill_workflow.capture_quality import NOTICE
        self.transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["mic", "system"]}
        atomic_json(self.recording / 'transcript.json', self.transcript)
        self.transcript_bytes = (self.recording / 'transcript.json').read_bytes()
        self.transcript_hash = hashlib.sha256(self.transcript_bytes).hexdigest()
        review = self.reviews.propose(self.job, self.transcript_bytes, self.config['route']['companies'],
                                     {'schema_version': 1, 'company_id': 'example', 'confidence': 'high', 'reason': 'Synthetic evidence',
                                      'ambiguous': False, 'evidence': [0]})
        self.reviews.approve(self.job, 'example', review['revision'])
        self.route = self.reviews.route(self.job, self.transcript_bytes, self.config['route']['companies'])
        self.publish_fixture()
        snapshot = self.snapshot()
        replacements = snapshot['notes_payload']['replacements']
        self.assertEqual(replacements['{{QUILL_TRANSCRIPT}}'], 'Not included.')
        self.assertEqual('\n'.join(replacements.values()).count(NOTICE), 1)
        self.assertEqual(snapshot['preview'].count(NOTICE), 1)
        self.assertNotIn('PRIVATE TRANSCRIPT SENTINEL', json.dumps(snapshot))
        self.assertNotIn('INTERNAL INTELLIGENCE SENTINEL', json.dumps(snapshot))
        self.assertNotIn('capture_quality', json.dumps(snapshot))
        self.assertEqual(self.snapshot(), snapshot)

    def test_success_exposes_notes_only_and_no_archive_dependency(self):
        snapshot = self.snapshot()
        text = json.dumps(snapshot)
        for hidden in ('PRIVATE TRANSCRIPT SENTINEL', 'INTERNAL INTELLIGENCE SENTINEL', 'INTERNAL ASSESSMENT SENTINEL',
                       'PRIVATE PRIORITIES SENTINEL', 'PRIVATE OBJECTION SENTINEL', 'INTERNAL DRAFT SUBJECT', 'INTERNAL DRAFT BODY'):
            self.assertNotIn(hidden, text)
        self.assertEqual(snapshot['title'], 'Dashboard review - Max - 10/9/26 - Shared notes')
        self.assertEqual(snapshot['destination_folder_id'], 'notes_folder_123')
        self.assertEqual(snapshot['private_document_id'], 'private_document_123')
        self.assertEqual(snapshot['notes_payload']['replacements']['{{QUILL_TRANSCRIPT}}'], 'Not included.')
        self.assertEqual(snapshot['notes_payload']['replacements']['{{QUILL_ATTACHMENTS}}'], 'Meeting notes only.')
        self.assertEqual(snapshot['notes_payload']['transcript_segments'], 0)
        for value in ('Review the dashboard.', 'Keep the calendar view.', '[Max] Send the draft.', 'Confirm the next date.'):
            self.assertIn(value, snapshot['preview'])
        self.assertEqual(snapshot, self.snapshot())

    def test_failed_audio_archive_does_not_block_notes(self):
        with self.queue.db:
            self.queue.db.execute('INSERT INTO tasks (job,stage,status,settings) VALUES (?,?,?,?)',
                                  (self.job, 'archive', 'failed', '{}'))
        self.assertEqual(self.snapshot()['job'], self.job)

    def test_unfinished_stage_rejected_before_transcript_read(self):
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='publish'", (self.job,))
        (self.recording / 'transcript.json').unlink()
        with self.assertRaisesRegex(SharingSourceError, 'Sharing waits'):
            self.snapshot()

    def test_updated_notes_fail_closed_even_with_valid_evidence(self):
        self.notes['summary'][0]['text'] = 'A later unfiled note.'
        atomic_json(self.result / 'notes/notes.json', self.notes)
        with self.assertRaisesRegex(SharingSourceError, 'notes changed after Drive filing'):
            self.snapshot()

    def test_hidden_invalid_evidence_fails_before_filtering(self):
        self.notes['deal_implications'][0]['evidence'] = [10]
        atomic_json(self.result / 'notes/notes.json', self.notes)
        with self.assertRaisesRegex(SharingSourceError, 'Invalid transcript segment reference'):
            self.snapshot()

    def test_source_transcript_edit_and_provenance_edit_fail_closed(self):
        path = self.result / 'notes/source-transcript.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        with self.assertRaisesRegex(SharingSourceError, 'different transcript'):
            self.snapshot()
        path.write_bytes(original)
        provenance_path = self.result / 'notes/provenance.json'
        data = json.loads(provenance_path.read_bytes())
        data['settings_sha256'] = 'b' * 64
        atomic_json(provenance_path, data)
        with self.assertRaisesRegex(SharingSourceError, 'provenance changed'):
            self.snapshot()

    def test_changed_current_company_mapping_fails(self):
        self.config['route']['companies'][0]['drive_folder_id'] = 'different_folder_123'
        atomic_json(self.config_path, self.config)
        with self.assertRaisesRegex(SharingSourceError, 'Company folders changed'):
            self.snapshot()

    def test_added_category_preserves_existing_sharing_snapshot_and_provenance(self):
        before = self.snapshot()
        tasks = self.queue.tasks(self.job)
        review = self.reviews.get(self.job)
        self.config['route']['companies'].append(
            {'id': 'general', 'name': 'General', 'drive_folder_id': 'general_folder_123'})
        atomic_json(self.config_path, self.config)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.queue.tasks(self.job), tasks)
        self.assertEqual(self.reviews.get(self.job), review)

    def test_category_added_during_sharing_snapshot_does_not_change_approved_destination(self):
        before = self.snapshot()
        changed = copy.deepcopy(self.config)
        changed['route']['companies'].append(
            {'id': 'general', 'name': 'General', 'drive_folder_id': 'general_folder_123'})
        with patch('quill_workflow.sharing_source.load_config', side_effect=[self.config, changed]):
            self.assertEqual(self.snapshot(), before)

    def test_original_category_removed_during_sharing_snapshot_is_rejected(self):
        changed = copy.deepcopy(self.config)
        changed['route']['companies'] = [
            {'id': 'general', 'name': 'General', 'drive_folder_id': 'general_folder_123'}]
        with patch('quill_workflow.sharing_source.load_config', side_effect=[self.config, changed]):
            with self.assertRaisesRegex(SharingSourceError, 'Company folders changed during review'):
                self.snapshot()

    def test_skipped_route_fails(self):
        review = self.reviews.get(self.job)
        review['status'], review['approved_route'] = 'skipped', None
        self.reviews._save(review)
        self.reviews.db.commit()
        with self.assertRaisesRegex(SharingSourceError, 'Choose the meeting'):
            self.snapshot()

    def test_kept_local_and_unfinished_safety_journals_fail(self):
        base = dict(schema_version=1, session_id=str(uuid.uuid4()), owner='manual', phase='finished',
                    review_status='kept_local', reasons=[], started_at='2026-10-09T19:00:00+08:00',
                    check_in_at='2026-10-09T20:00:00+08:00', stop_at='2026-10-09T20:05:00+08:00', warning_revision=1)
        for changes in ({}, {'phase': 'active', 'review_status': 'pending'}):
            with self.subTest(changes=changes):
                atomic_json(self.original / 'recording-safety.json', dict(base, **changes))
                with self.assertRaisesRegex(SharingSourceError, 'processing'):
                    self.snapshot()

    def test_saved_receipt_mismatch_fails(self):
        path = self.result / 'publish-receipt.json'
        receipt = json.loads(path.read_bytes())
        receipt['folder_id'] = 'different_folder_123'
        atomic_json(path, receipt)
        with self.assertRaisesRegex(SharingSourceError, 'receipt no longer matches'):
            self.snapshot()

    def test_unverified_native_copy_fails(self):
        self.journal['operations']['file:native-template']['fill']['state'] = 'dispatched'
        atomic_json(self.result / 'drive-publish.json', self.journal)
        with self.assertRaisesRegex(SharingSourceError, 'template verification'):
            self.snapshot()

    def test_template_style_change_fails(self):
        self.journal['operations']['file:native-template']['fill']['template_document']['title'] = 'Identity change ignored'
        self.journal['operations']['file:native-template']['fill']['template_document']['tabs'][0]['title'] = 'Changed style binding'
        atomic_json(self.result / 'drive-publish.json', self.journal)
        with self.assertRaisesRegex(SharingSourceError, 'template verification'):
            self.snapshot()

    def test_linked_source_and_path_traversal_fail(self):
        path = self.result / 'notes/notes.json'
        target = self.root / 'notes.json'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(SharingSourceError, 'symbolic link'):
            self.snapshot()
        for job in ('../private', '.', '..'):
            with self.subTest(job=job), self.assertRaises(SharingSourceError):
                sharing_snapshot(self.state, self.config_path, job)

    def test_source_change_during_preview_fails(self):
        from quill_workflow import sharing_source
        real = sharing_source.note_sections
        def mutate(notes):
            data = copy.deepcopy(self.notes)
            data['summary'][0]['text'] = 'Changed during review.'
            atomic_json(self.result / 'notes/notes.json', data)
            return real(notes)
        with patch.object(sharing_source, 'note_sections', side_effect=mutate):
            with self.assertRaisesRegex(SharingSourceError, 'changed during review'):
                self.snapshot()

    def test_missing_legacy_original_fails(self):
        (self.original / 'meta.json').unlink()
        self.original.rmdir()
        with self.assertRaisesRegex(SharingSourceError, 'original recording is unavailable'):
            self.snapshot()


if __name__ == '__main__':
    unittest.main()
