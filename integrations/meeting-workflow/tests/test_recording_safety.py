"""Synthetic safety journals only; no audio/model/cloud/runtime state is used."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import ConnectorRunner, Queue, atomic_json
from quill_workflow.recording_safety import FILENAME, RecordingSafetyHold, read_safety
from quill_workflow.local_naming import name_session, request_naming
from quill_workflow.drive_upload import DriveUploader


class RecordingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.session = self.root / 'recordings/synthetic-session'
        self.session.mkdir(parents=True)
        for filename in ('mic.caf', 'system.caf'):
            (self.session / filename).write_bytes(b'synthetic bytes, never captured audio')
        atomic_json(self.session / 'meta.json', {
            'started': '2026-01-01T10:00:00Z', 'ended': '2026-01-01T10:01:00Z',
            'files': {'mic': 'mic.caf', 'system': 'system.caf'}})
        atomic_json(self.session / 'transcript.json', {
            'segments': [{'start_ms': 0, 'text': 'Synthetic agenda planning.'}]})
        self.session_id = str(uuid.uuid4())
        self.config = {'recordings_root': str(self.session.parent)}
        for stage in ('notes', 'archive', 'publish'):
            self.config[stage] = {'enabled': True, 'command': ['synthetic-connector', '{input}', '{output}']}
        self.queue = Queue(self.root / 'state')
        self.runner = ConnectorRunner(self.queue.state, ROOT / 'skills/call-summary/SKILL.md')

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def journal(self, **changes):
        data = dict(schema_version=1, session_id=self.session_id, owner='manual',
                    phase='finished', review_status='approved', reasons=[],
                    started_at='2026-01-01T10:00:00Z', check_in_at='2026-01-01T12:00:00Z',
                    stop_at='2026-01-01T12:05:00Z', warning_revision=1)
        data.update(changes)
        atomic_json(self.session / FILENAME, data)
        return data

    def receipt(self, stage, job, settings):
        return {'job': job, 'stage': stage, 'idempotency_key': job + ':' + stage}

    def task(self, job, stage='archive'):
        return next(task for task in self.queue.tasks(job) if task['stage'] == stage)

    def test_active_pending_kept_local_and_invalid_stop_before_media_ingestion(self):
        for changes in ({'phase': 'active', 'review_status': 'pending'}, {'review_status': 'pending'},
                        {'review_status': 'kept_local'}, {'schema_version': True},
                        {'schema_version': 2}, {'session_id': 'unknown'}, {'reasons': 'timeout'},
                        {'owner': 'external'}, {'phase': 'stopped'}, {'review_status': 'yes'}):
            with self.subTest(changes=changes):
                self.journal(**changes)
                with patch('quill_workflow.pipeline.safe_member', side_effect=AssertionError('Media was inspected')):
                    with self.assertRaises(RecordingSafetyHold):
                        self.queue.enqueue(self.session, self.config)
                self.assertEqual(self.queue.tasks(), [])
                self.assertFalse((self.queue.state / 'recordings').exists())

    def test_deadlines_revision_and_reason_bounds_match_app_validation(self):
        invalid = [
            {'started_at': None}, {'check_in_at': None}, {'stop_at': None},
            {'started_at': 'today'}, {'started_at': 1770000000},
            {'started_at': '2026-01-01'}, {'started_at': '2026-01-01T10:00:00'},
            {'started_at': '2026-02-30T10:00:00Z'},
            {'started_at': '2026-01-01T10:00:00+01:99'},
            {'check_in_at': '2026-01-01T09:59:59Z'},
            {'stop_at': '2026-01-01T12:00:00Z'},
            {'stop_at': '2026-01-01T11:59:59Z'},
            {'warning_revision': None}, {'warning_revision': True},
            {'warning_revision': 1.0}, {'warning_revision': 0}, {'warning_revision': -1},
            {'reasons': ['']}, {'reasons': [' \n\t']}, {'reasons': [1]},
            {'reasons': ['x'] * 101}, {'reasons': ['x' * 2001]},
            {'reasons': ['é' * 1001]},
            {'phase': 'active', 'review_status': 'approved'},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.journal(**changes)
                with self.assertRaises(RecordingSafetyHold):
                    read_safety(self.session)
                with self.assertRaises(RecordingSafetyHold):
                    read_safety(self.session, require_approval=False)
        for field in ('started_at', 'check_in_at', 'stop_at', 'warning_revision'):
            data = self.journal()
            del data[field]
            atomic_json(self.session / FILENAME, data)
            with self.subTest(missing=field), self.assertRaises(RecordingSafetyHold):
                read_safety(self.session)
        # Equal start/check-in, explicit offsets, and boundary-size reasons
        # remain valid. Approval may retain the failure reason being reviewed.
        self.journal(check_in_at='2026-01-01T18:00:00+08:00',
                     stop_at='2026-01-01T18:05:00+08:00', reasons=['é' * 1000])
        self.assertIsNotNone(read_safety(self.session))
        self.journal(reasons=['valid'] * 100)
        self.assertIsNotNone(read_safety(self.session))

    def test_finalized_metadata_is_required_again_before_each_stage(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        metadata_path = self.session / 'meta.json'
        original = json.loads(metadata_path.read_bytes())
        invalid = [None, {}, dict(original, started='unreadable'),
                   dict(original, ended='2026-01-01'),
                   dict(original, files={'mic': 'other.caf', 'system': 'system.caf'}),
                   dict(original, files={'mic': 'mic.caf', 'system': 'system.caf', 'screen': None})]
        for value in invalid:
            atomic_json(metadata_path, value)
            with self.subTest(value=value), self.assertRaises(RecordingSafetyHold):
                self.queue.require_processing(job)
            self.queue.run_ready(lambda *a: self.fail('Invalid metadata reached a connector'))
            self.assertTrue(all(task['status'] == 'safety_held' for task in self.queue.tasks(job)))
        metadata_path.unlink()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)
        elsewhere = self.root / 'linked-meta.json'
        atomic_json(elsewhere, original)
        metadata_path.symlink_to(elsewhere)
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)
        metadata_path.unlink()
        atomic_json(metadata_path, dict(original, ended='2026-01-01T09:59:00Z'))
        self.assertIsNotNone(self.queue.require_processing(job), 'A backward wall-clock jump is valid')
        self.queue.run_ready(self.receipt)
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks(job)))

    def test_malformed_linked_directory_and_oversized_journals_fail_closed(self):
        marker = self.session / FILENAME
        for payload in (b'{broken', b'[]', b'null', b'\xff', b' ' * 100001):
            marker.write_bytes(payload)
            with self.assertRaises(RecordingSafetyHold):
                read_safety(self.session)
        marker.unlink()
        marker.symlink_to(self.session / 'meta.json')
        with self.assertRaises(RecordingSafetyHold):
            read_safety(self.session)
        marker.unlink()
        marker.mkdir()
        with self.assertRaises(RecordingSafetyHold):
            read_safety(self.session)

    def test_approved_ingestion_preserves_journal_and_legacy_still_works(self):
        self.assertIsNone(read_safety(self.session))
        data = self.journal(session_id=self.session_id.upper())
        original = (self.session / FILENAME).read_bytes()
        job = self.queue.enqueue(self.session, self.config)
        self.assertEqual(self.queue.require_processing(job), data)
        self.assertFalse((self.queue.state / 'recordings' / job / FILENAME).exists())
        self.assertEqual((self.session / FILENAME).read_bytes(), original)
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)

    def test_hold_added_after_enqueue_blocks_all_stages_without_attempts(self):
        self.config['route'] = {'enabled': True, 'command': ['synthetic-route'],
                               'companies': [{'id': 'acme', 'name': 'Acme', 'drive_folder_id': 'company_folder_123'}]}
        job = self.queue.enqueue(self.session, self.config)
        self.journal(review_status='pending', reasons=['unattended_limit'])
        self.assertEqual(self.queue.run_ready(lambda *a: self.fail('Processing ran')), 0)
        self.assertFalse(self.queue.has_work())
        for task in self.queue.tasks(job):
            self.assertEqual((task['status'], task['attempts']), ('safety_held', 0))
        self.assertEqual(json.loads((self.session / FILENAME).read_text())['review_status'], 'pending')

    def test_pending_ingestion_can_be_retried_only_after_explicit_approval(self):
        self.journal(review_status='pending')
        with self.assertRaises(RecordingSafetyHold):
            self.queue.enqueue(self.session, self.config)
        self.journal(review_status='approved', reasons=['unattended_limit'])
        job = self.queue.enqueue(self.session, self.config)
        self.queue.run_ready(self.receipt)
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks(job)))

    def test_recovered_system_audio_keeps_review_hold_and_legacy_pipeline_format(self):
        metadata = json.loads((self.session / 'meta.json').read_text())
        metadata['start_offset_ms'] = {'mic': 120, 'system': 0}
        metadata['system_capture'] = {
            'received_audio': True, 'coverage_complete': False,
            'interruptions': [{'reason': 'audio_device_changed', 'at_ms': 2000}],
            'interruption_count': 1, 'gap_seconds': 0.5,
        }
        atomic_json(self.session / 'meta.json', metadata)
        media_before = {name: (self.session / name).read_bytes()
                        for name in ('mic.caf', 'system.caf', 'meta.json', 'transcript.json')}
        self.journal(review_status='pending', reasons=['System audio was interrupted or incomplete.'])
        with self.assertRaises(RecordingSafetyHold):
            self.queue.enqueue(self.session, self.config)
        self.assertEqual(self.queue.tasks(), [])

        # A real user decision releases the existing gate. Additive capture
        # diagnostics must not change filenames, offsets, bytes or stage inputs.
        self.journal(review_status='approved', reasons=['System audio was interrupted or incomplete.'])
        job = self.queue.enqueue(self.session, self.config)
        snapshot = self.queue.state / 'recordings' / job
        for name, original in media_before.items():
            self.assertEqual((snapshot / name).read_bytes(), original)
        self.assertEqual(json.loads((snapshot / 'meta.json').read_text()), metadata)
        seen = []
        def record_stage(stage, job_id, settings):
            seen.append(stage)
            return self.receipt(stage, job_id, settings)
        self.queue.run_ready(record_stage)
        self.assertEqual(set(seen), {'notes', 'archive', 'publish'})
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks(job)))
        for name, original in media_before.items():
            self.assertEqual((self.session / name).read_bytes(), original)

    def test_hold_survives_worker_restart_and_preserves_retry_backoff(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        def offline(*a):
            raise OSError('synthetic offline')
        self.queue.run_ready(offline, now=100)
        self.assertEqual(self.task(job)['next_attempt'], 130)
        self.journal(review_status='pending')
        self.queue.close()
        self.queue = Queue(self.root / 'state')
        self.queue.recover(now=110)
        self.assertEqual((self.task(job)['status'], self.task(job)['attempts'], self.task(job)['next_attempt']),
                         ('safety_held', 1, 130))
        self.queue.run_ready(lambda *a: self.fail('Pending review ran'), now=500)
        self.journal()
        self.queue.run_ready(lambda *a: self.fail('Retry ran before backoff'), now=129)
        self.queue.run_ready(self.receipt, now=130)
        self.assertEqual((self.task(job)['status'], self.task(job)['attempts']), ('succeeded', 2))

    def test_interrupted_running_task_is_held_before_recovery_execution(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running',attempts=1 WHERE job=? AND stage='archive'", (job,))
        self.journal(phase='active', review_status='pending')
        self.queue.recover(now=100)
        self.queue.run_ready(lambda *a: self.fail('Restart bypassed recording gate'), now=100)
        self.assertEqual(self.task(job)['status'], 'safety_held')

    def test_deletion_or_changed_session_cannot_turn_observed_hold_into_legacy(self):
        job = self.queue.enqueue(self.session, self.config)
        self.journal(review_status='pending')
        self.queue.run_ready(lambda *a: self.fail('Pending review ran'))
        (self.session / FILENAME).unlink()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)
        self.journal(session_id=str(uuid.uuid4()))
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)
        self.journal()
        self.queue.run_ready(self.receipt)
        self.assertEqual(self.task(job)['status'], 'succeeded')

    def test_malformed_observed_marker_remains_required_after_deletion(self):
        job = self.queue.enqueue(self.session, self.config)
        (self.session / FILENAME).write_text('{broken')
        self.queue.run_ready(lambda *a: self.fail('Invalid journal ran'))
        (self.session / FILENAME).unlink()
        self.queue.run_ready(lambda *a: self.fail('Deleted invalid journal ran'))
        self.assertEqual(self.task(job)['status'], 'safety_held')

    def test_replacing_source_folder_cannot_reuse_recording_approval(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        original = self.session.with_name('original')
        self.session.rename(original)
        self.session.mkdir()
        self.journal()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)

    def test_relocated_recording_uses_current_original_journal(self):
        self.journal()
        self.config['local_naming'] = {'enabled': True}
        job = self.queue.enqueue(self.session, self.config)
        destination = self.session.with_name('Reviewed topic - 1-1-26')
        self.session.rename(destination)
        with self.queue.db:
            self.queue.db.execute("UPDATE local_names SET destination=?,status='succeeded' WHERE job=?",
                                  (str(destination), job))
        self.session = destination
        self.journal(review_status='kept_local')
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(job)

    def test_changed_named_folder_does_not_block_later_meeting(self):
        self.journal()
        self.config['local_naming'] = {'enabled': True}
        self.config['route'] = {'enabled': True, 'command': ['synthetic-route'],
                               'companies': [{'id': 'acme', 'name': 'Acme', 'drive_folder_id': 'company_folder_123'}]}
        old_job = self.queue.enqueue(self.session, self.config)
        destination = self.session.with_name('Older meeting - 1-1-26')
        self.session.rename(destination)
        with self.queue.db:
            # This is an older binding without the new persistent volume
            # identity. A later mismatch must still fail closed, never rebind.
            self.queue.db.execute("DELETE FROM recording_identities WHERE job=?", (old_job,))
            self.queue.db.execute("UPDATE local_names SET destination=?,status='succeeded',device=device+1 WHERE job=?",
                                  (str(destination), old_job))
            self.queue.db.execute("UPDATE tasks SET status='succeeded',attempts=1 WHERE job=? AND stage='notes'", (old_job,))
            self.queue.db.execute("UPDATE tasks SET status='held',attempts=1,error='Company review required' "
                                  "WHERE job=? AND stage='route'", (old_job,))
        old_notes, old_route = self.task(old_job, 'notes'), self.task(old_job, 'route')
        binding = dict(self.queue.db.execute('SELECT * FROM local_names WHERE job=?', (old_job,)).fetchone())
        later = self.session.with_name('later-meeting')
        shutil.copytree(destination, later)
        (later / 'mic.caf').write_bytes(b'distinct later synthetic recording')
        data = json.loads((later / FILENAME).read_text())
        data['session_id'] = str(uuid.uuid4())
        atomic_json(later / FILENAME, data)
        new_job = self.queue.enqueue(later, self.config)
        calls = []
        def record_call(stage, job, settings):
            calls.append((stage, job))
            return self.receipt(stage, job, settings)
        self.queue.recover()
        self.queue.run_ready(record_call)
        self.assertEqual({job for _, job in calls}, {new_job})
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks(new_job)))
        for stage in ('archive', 'publish'):
            task = self.task(old_job, stage)
            self.assertEqual((task['status'], task['attempts']), ('safety_held', 0))
            self.assertIn('Original recording location needs review', task['error'])
        self.assertEqual(self.task(old_job, 'notes'), old_notes)
        self.assertEqual(self.task(old_job, 'route'), old_route)
        self.assertEqual(dict(self.queue.db.execute('SELECT * FROM local_names WHERE job=?', (old_job,)).fetchone()), binding)
        self.assertFalse(self.queue.has_work())

    def test_unreadable_named_location_is_held_before_connector(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        with patch('quill_workflow.local_naming.session_path', side_effect=PermissionError('Synthetic inaccessible folder')):
            self.queue.recover()
            self.queue.run_ready(lambda *a: self.fail('Unreadable original reached connector'))
        self.assertTrue(all((task['status'], task['attempts']) == ('safety_held', 0)
                            for task in self.queue.tasks(job)))

    def test_safety_approval_does_not_release_other_holds_or_disabled_stages(self):
        self.config['publish']['enabled'] = False
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        self.journal(review_status='pending')
        self.queue.run_ready(self.receipt)
        with self.assertRaises(RecordingSafetyHold):
            self.queue.release(job, 'archive', self.config)
        self.queue.run_ready(self.receipt, enabled={'notes': True, 'archive': False})
        self.journal()
        self.queue.run_ready(self.receipt, enabled={'notes': True, 'archive': True, 'publish': True})
        self.assertEqual(self.task(job, 'notes')['status'], 'succeeded')
        self.assertEqual(self.task(job, 'archive')['status'], 'held')
        self.assertEqual(self.task(job, 'publish')['status'], 'held')

    def test_safety_is_rechecked_between_stages(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        calls = []
        def notes_then_hold(stage, job, settings):
            calls.append(stage)
            self.journal(review_status='pending')
            return self.receipt(stage, job, settings)
        self.queue.run_ready(notes_then_hold)
        self.assertEqual(calls, ['notes'])
        self.assertEqual(self.task(job, 'archive')['status'], 'safety_held')
        self.assertEqual(self.task(job, 'publish')['status'], 'safety_held')

    def test_direct_connector_entry_blocks_all_stages_and_cached_receipts(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        result = self.queue.state / 'results' / job
        result.mkdir(parents=True)
        atomic_json(result / 'archive-receipt.json', self.receipt('archive', job, {}))
        self.journal(review_status='pending')
        self.runner.execute = Mock(side_effect=AssertionError('Connector launched'))
        for stage in ('notes', 'route', 'archive', 'publish'):
            with self.subTest(stage=stage), self.assertRaises(RecordingSafetyHold):
                self.runner(stage, job, self.config.get(stage, {}))
        self.runner.execute.assert_not_called()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.attach_transcript(job, self.session / 'transcript.json')

    def test_rechecks_immediately_before_model_or_rclone_launch(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        settings_by_stage = {'notes': self.config['notes'],
                             'archive': {'enabled': True, 'type': 'rclone', 'remote': 'mock', 'folder': 'test'}}
        original_guard = self.runner.require_processing
        for stage, settings in settings_by_stage.items():
            with self.subTest(stage=stage):
                self.journal()
                count = 0
                def guard(job):
                    nonlocal count
                    count += 1
                    if count == 2:
                        self.journal(review_status='pending')
                    return original_guard(job)
                with patch.object(self.runner, 'require_processing', side_effect=guard), \
                        patch.object(self.runner, 'execute') as execute:
                    with self.assertRaises(RecordingSafetyHold):
                        self.runner(stage, job, settings)
                    execute.assert_not_called()

    def test_local_rename_checks_before_planning_and_again_before_move(self):
        self.journal()
        self.config['local_naming'] = {'enabled': True}
        job = self.queue.enqueue(self.session, self.config)
        self.journal(review_status='pending')
        with self.assertRaises(RecordingSafetyHold):
            request_naming(self.queue, job, self.config)
        with self.assertRaises(RecordingSafetyHold):
            name_session(self.queue, job)
        self.journal()
        with patch('quill_workflow.local_naming._title', return_value='Synthetic topic'), \
                patch('quill_workflow.local_naming._verify_files',
                      side_effect=lambda *a: self.journal(review_status='pending')), \
                patch('quill_workflow.local_naming._rename_exclusive') as move:
            with self.assertRaises(RecordingSafetyHold):
                name_session(self.queue, job)
            move.assert_not_called()
        self.assertTrue(self.session.is_dir())

    def test_drive_checks_every_invocation_including_read_only_and_retry_requests(self):
        self.journal()
        job = self.queue.enqueue(self.session, self.config)
        invoke = Mock(return_value={})
        uploader = DriveUploader(self.queue.state, invoke, 'meetings_root_123',
                                 guard=lambda: self.runner.require_processing(job))
        uploader._call('get_document', {'document_id': 'synthetic'})
        self.journal(review_status='kept_local')
        for operation in ('get_document', 'create_folder', 'upload_file', 'batch_update_document'):
            with self.subTest(operation=operation), self.assertRaises(RecordingSafetyHold):
                uploader._call(operation, {})
        self.assertEqual(invoke.call_count, 1)


if __name__ == '__main__':
    unittest.main()
