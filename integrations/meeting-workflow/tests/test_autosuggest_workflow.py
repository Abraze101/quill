"""Synthetic completion-to-calendar dispatch; never connects or shares."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import (Queue, WorkflowError, atomic_json,
                                     automatic_calendar_suggestions_since, load_config)
from quill_workflow.recording_safety import RecordingSafetyHold
from quill_workflow.sharing import SharingReviews as RealSharingReviews
from quill_workflow.worker_supervision import Supervisor

spec = importlib.util.spec_from_file_location('autosuggest_workflow', ROOT / 'workflow.py')
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class AutomaticSuggestionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config_path = self.root / 'config.json'
        self.config = {'sharing': {'automatic_calendar_suggestions': True,
                                   'automatic_calendar_suggestions_since': 1000.5}}
        atomic_json(self.config_path, self.config)
        self.queue = Queue(self.root / 'state')
        self.original = self.root / 'recording'
        self.original.mkdir()

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def add_job(self, job='new-job', created=1001, status='succeeded', stage='publish'):
        settings = json.dumps({'enabled': True, 'command': ['fixture']})
        with self.queue.db:
            self.queue.db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?)', (job, str(self.original), created))
            self.queue.db.execute('INSERT OR REPLACE INTO tasks (job,stage,status,settings) VALUES (?,?,?,?)',
                                  (job, stage, status, settings))
        return job

    def pending_review(self, job='new-job'):
        return {'job': job, 'status': 'available', 'suggestion_status': 'searching', 'revision': 'revision-1',
                'approval': None, 'automatic_suggestion': {'state': 'pending', 'since': 1000.5,
                                                          'dispatch_attempts': 1, 'next_dispatch_at': 1015}}

    def test_policy_defaults_off_and_does_not_infer_a_boundary(self):
        self.assertIsNone(automatic_calendar_suggestions_since({}))
        self.assertIsNone(automatic_calendar_suggestions_since({'sharing': {'automatic_calendar_suggestions': False}}))
        self.assertEqual(automatic_calendar_suggestions_since(self.config), 1000.5)
        for sharing in ({'automatic_calendar_suggestions': True}, {'automatic_calendar_suggestions': 1},
                        {'automatic_calendar_suggestions': 'yes'}, [], None):
            atomic_json(self.config_path, {'sharing': sharing})
            with self.subTest(sharing=sharing), self.assertRaises(WorkflowError):
                load_config(self.config_path)

    def test_watermark_rejects_boolean_nonfinite_string_and_nonpositive_values(self):
        for value in (True, False, '1000', 0, -1, float('inf'), float('nan')):
            bad = {'sharing': {'automatic_calendar_suggestions': True,
                               'automatic_calendar_suggestions_since': value}}
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                automatic_calendar_suggestions_since(bad)

    def test_disabled_policy_never_opens_reviews_or_launches(self):
        atomic_json(self.config_path, {'sharing': {'automatic_calendar_suggestions': False}})
        with patch('quill_workflow.sharing.SharingReviews') as reviews, patch.object(workflow.subprocess, 'Popen') as launch:
            self.assertFalse(workflow.auto_suggest('new-job', self.config_path, self.queue.state))
            self.assertEqual(workflow.reconcile_automatic_suggestions(self.queue, self.config_path), 0)
            self.assertFalse(workflow.pending_automatic_suggestions(self.config_path, self.queue.state))
        reviews.assert_not_called()
        launch.assert_not_called()

    def test_dispatch_only_starts_calendar_worker_after_durable_claim(self):
        review = Mock()
        review.request_automatic.return_value = self.pending_review()
        def launched(argv, **kwargs):
            review.request_automatic.assert_called_once_with('new-job', 1000.5)
            self.assertIn('suggestion-work', argv)
            self.assertNotIn('sharing-work', argv)
            self.assertEqual(argv[-3:], ['new-job', '--revision', 'revision-1'])
            self.assertIs(kwargs['start_new_session'], True)
            return Mock()
        with patch('quill_workflow.sharing.SharingReviews', return_value=review), \
                patch.object(workflow.subprocess, 'Popen', side_effect=launched) as launch:
            self.assertTrue(workflow.auto_suggest('new-job', self.config_path, self.queue.state))
        launch.assert_called_once()
        review.close.assert_called_once()
        review.decide.assert_not_called()
        review.run.assert_not_called()

    def test_existing_or_ineligible_review_never_dispatches(self):
        review = Mock()
        review.request_automatic.return_value = None
        with patch('quill_workflow.sharing.SharingReviews', return_value=review), \
                patch.object(workflow.subprocess, 'Popen') as launch:
            self.assertFalse(workflow.auto_suggest('existing-job', self.config_path, self.queue.state))
        launch.assert_not_called()
        review.close.assert_called_once()

    def test_repeat_scan_uses_store_claim_and_does_not_duplicate_lookup(self):
        review = Mock()
        review.request_automatic.side_effect = [self.pending_review(), None]
        with patch('quill_workflow.sharing.SharingReviews', return_value=review), \
                patch.object(workflow.subprocess, 'Popen') as launch:
            self.assertTrue(workflow.auto_suggest('new-job', self.config_path, self.queue.state))
            self.assertFalse(workflow.auto_suggest('new-job', self.config_path, self.queue.state))
        launch.assert_called_once()

    def test_launch_failure_preserves_automatic_pending_recovery(self):
        review = Mock()
        review.request_automatic.return_value = self.pending_review()
        with patch('quill_workflow.sharing.SharingReviews', return_value=review), \
                patch.object(workflow.subprocess, 'Popen', side_effect=OSError('Fixture launch failure')):
            self.assertFalse(workflow.auto_suggest('new-job', self.config_path, self.queue.state))
        review.automatic_launch_failed.assert_called_once_with('new-job', 'revision-1')
        review.launch_failed.assert_not_called()
        review.close.assert_called_once()

    def test_bad_or_approved_claim_is_never_dispatched(self):
        for changes in ({'job': 'other'}, {'suggestion_status': 'ready'}, {'approval': {'approved': True}}):
            review = Mock()
            review.request_automatic.return_value = dict(self.pending_review(), **changes)
            with self.subTest(changes=changes), patch('quill_workflow.sharing.SharingReviews', return_value=review), \
                    patch.object(workflow.subprocess, 'Popen') as launch, self.assertRaises(WorkflowError):
                workflow.auto_suggest('new-job', self.config_path, self.queue.state)
            launch.assert_not_called()

    def test_recovery_scans_only_published_jobs_since_explicit_activation(self):
        self.add_job('historical', created=999)
        self.add_job('at-boundary', created=1000.5)
        self.add_job('new-job', created=1001)
        self.add_job('pending', created=1002, status='pending')
        self.add_job('held', created=1003, status='held')
        self.add_job('notes-only', created=1004, stage='notes')
        before = copy.deepcopy(self.queue.tasks())
        with patch.object(workflow, 'auto_suggest', return_value=True) as dispatch:
            self.assertEqual(workflow.reconcile_automatic_suggestions(self.queue, self.config_path), 2)
        self.assertEqual([call.args[0] for call in dispatch.call_args_list], ['at-boundary', 'new-job'])
        self.assertEqual(self.queue.tasks(), before)

    def test_changed_source_in_one_job_does_not_block_other_new_jobs(self):
        self.add_job('changed', created=1001)
        self.add_job('eligible', created=1002)
        with patch.object(workflow, 'auto_suggest', side_effect=[ValueError('Changed source'), True]) as dispatch:
            self.assertEqual(workflow.reconcile_automatic_suggestions(self.queue, self.config_path), 1)
        self.assertEqual([call.args[0] for call in dispatch.call_args_list], ['changed', 'eligible'])
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks()))

    def test_live_policy_is_rechecked_after_completion(self):
        atomic_json(self.config_path, {'sharing': {'automatic_calendar_suggestions': False}})
        with patch('quill_workflow.sharing.SharingReviews') as reviews:
            self.assertFalse(workflow.completion_autosuggest('publish', 'new-job', {}, self.config_path, self.queue.state))
        reviews.assert_not_called()

    def test_completion_dispatches_only_publish(self):
        with patch.object(workflow, 'auto_suggest', return_value=True) as dispatch:
            for stage in ('notes', 'route', 'archive'):
                self.assertFalse(workflow.completion_autosuggest(stage, 'new-job', {}, self.config_path, self.queue.state))
            self.assertTrue(workflow.completion_autosuggest('publish', 'new-job', {}, self.config_path, self.queue.state))
        dispatch.assert_called_once_with('new-job', self.config_path, self.queue.state)

    def test_publish_completion_saves_unapproved_calendar_suggestions_end_to_end(self):
        self.add_job('new-job', stage='notes')
        self.add_job('new-job', stage='route')
        self.add_job('new-job', status='pending')
        metadata_path = self.queue.state / 'recordings' / 'new-job' / 'meta.json'
        notes_path = self.queue.state / 'results' / 'new-job' / 'notes' / 'notes.json'
        atomic_json(metadata_path, {'started': '2026-10-09T11:00:37Z', 'ended': '2026-10-09T11:30:00Z',
                                    'recording_timezone': 'UTC'})
        atomic_json(notes_path, {'meeting_title': {'text': 'Partner planning',
                                                   'participants': [{'name': 'Casey'}]}})
        snapshot = {'title': 'Partner planning - Casey - 10/9/26 - Shared notes',
                    'preview': 'Summary\nDiscuss the next step.', 'owner': 'owner@example.test',
                    'binding': {'metadata_sha256': hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
                                'notes_sha256': hashlib.sha256(notes_path.read_bytes()).hexdigest()}}

        def review_store(state, config_path):
            return RealSharingReviews(state, config_path, snapshot=lambda job: copy.deepcopy(snapshot))

        event = {'id': 'synthetic-event', 'summary': 'Partner planning with Casey', 'status': 'confirmed',
                 'start': '2026-10-09T11:00:00Z', 'end': '2026-10-09T11:30:00Z',
                 'url': 'https://www.google.com/calendar/event?eid=synthetic-event',
                 'attendees': [{'email': 'owner@example.test', 'is_self': True, 'response_status': 'accepted'},
                               {'email': 'casey@example.test', 'display_name': 'Casey', 'response_status': 'accepted'}]}
        calendar_calls = []

        def calendar_read(operation, arguments):
            calendar_calls.append((operation, copy.deepcopy(arguments)))
            # The real store must durably mark lookup started before reading
            # any calendar, so later completion scans cannot replay it.
            reviews = review_store(self.queue.state, self.config_path)
            try:
                started = reviews.get('new-job')
            finally:
                reviews.close()
            self.assertEqual(started['automatic_suggestion']['state'], 'started')
            self.assertFalse(started.get('approval'))
            if operation == 'list_calendars':
                self.assertLessEqual(arguments['max_results'], 20)
                return {'calendars': [{'id': 'owner@example.test', 'summary': 'Main calendar',
                                      'access_role': 'owner', 'primary': True}]}
            if operation == 'search_events':
                self.assertEqual(arguments['time_min'], '2026-10-09T09:30:37Z')
                self.assertEqual(arguments['time_max'], '2026-10-09T12:30:37Z')
                self.assertNotIn('query', arguments)
                return {'events': [copy.deepcopy(event)]}
            if operation == 'read_event':
                self.assertEqual(arguments['event_id'], event['id'])
                return copy.deepcopy(event)
            self.fail('Unexpected non-calendar operation: ' + operation)

        def launch_suggestion_worker(argv, **kwargs):
            self.assertEqual(argv[-4], 'suggestion-work')
            reviews = review_store(self.queue.state, self.config_path)
            try:
                reviews.run_suggestions(argv[-3], argv[-1], invoke=calendar_read)
            finally:
                reviews.close()
            return Mock()

        receipt = {'job': 'new-job', 'stage': 'publish', 'destination': 'synthetic-private-document',
                   'document_format': 'native_template_google_doc_v1'}
        committed = []

        def completed(stage, job, value):
            task = next(item for item in self.queue.tasks(job) if item['stage'] == 'publish')
            self.assertEqual(task['status'], 'succeeded')
            committed.append(task['receipt'])
            self.assertTrue(workflow.completion_autosuggest(stage, job, value, self.config_path, self.queue.state))

        with patch('quill_workflow.sharing.SharingReviews', side_effect=review_store), \
                patch.object(workflow.subprocess, 'Popen', side_effect=launch_suggestion_worker) as launch:
            self.assertEqual(self.queue.run_ready(lambda *args: copy.deepcopy(receipt), on_completed=completed), 1)
            self.assertEqual(self.queue.run_ready(lambda *args: self.fail('Repeated completed publish'),
                                                   on_completed=completed), 0)
            self.assertEqual(workflow.reconcile_automatic_suggestions(self.queue, self.config_path), 0)
        launch.assert_called_once()
        self.assertEqual([operation for operation, _ in calendar_calls], ['list_calendars', 'search_events', 'read_event'])
        reviews = review_store(self.queue.state, self.config_path)
        try:
            record = reviews.get('new-job')
            self.assertFalse(reviews.pending_automatic(1000.5))
        finally:
            reviews.close()
        self.assertEqual(record['status'], 'available')
        self.assertEqual(record['suggestion_status'], 'ready')
        self.assertEqual(record['recipients'], [])
        self.assertEqual(record['journal'], {})
        for field in ('approval', 'draft', 'receipt', 'recipient_draft'):
            self.assertNotIn(field, record)
        option = record['suggested_events'][0]
        self.assertFalse(option['selected'])
        self.assertEqual([item['email'] for item in option['recipients']], ['casey@example.test'])
        self.assertTrue(all(item['selected'] is False and item['source'] == 'Calendar invite'
                            for item in option['recipients']))
        task = next(item for item in self.queue.tasks('new-job') if item['stage'] == 'publish')
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['attempts'], 1)
        self.assertEqual(committed, [task['receipt']])
        self.assertEqual(json.loads(task['receipt']), receipt)

    def test_queue_observer_sees_committed_receipt_and_is_not_repeated(self):
        self.add_job('new-job', status='pending', stage='archive')
        completed = []
        def runner(stage, job, settings):
            return {'job': job, 'stage': stage, 'destination': 'fixture-destination'}
        def observe(stage, job, receipt):
            connection = sqlite3.connect(self.queue.state / 'jobs.sqlite3')
            try:
                status, saved = connection.execute('SELECT status,receipt FROM tasks WHERE job=? AND stage=?',
                                                    (job, stage)).fetchone()
            finally:
                connection.close()
            self.assertEqual(status, 'succeeded')
            self.assertEqual(json.loads(saved), receipt)
            completed.append((stage, job))
        self.assertEqual(self.queue.run_ready(runner, on_completed=observe), 1)
        self.assertEqual(self.queue.run_ready(runner, on_completed=observe), 0)
        self.assertEqual(completed, [('archive', 'new-job')])

    def test_observer_exception_does_not_undo_success_or_create_retry(self):
        self.add_job('new-job', status='pending', stage='archive')
        receipt = {'job': 'new-job', 'stage': 'archive', 'destination': 'fixture'}
        for error in (RuntimeError('Dispatch failed'), RecordingSafetyHold('Withdrawn after commit')):
            with self.queue.db:
                self.queue.db.execute("UPDATE tasks SET status='pending',attempts=0,receipt=NULL WHERE job='new-job'")
            observer = Mock(side_effect=error)
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.queue.run_ready(lambda *args: receipt, on_completed=observer), 1)
                task = self.queue.tasks('new-job')[0]
                self.assertEqual(task['status'], 'succeeded')
                self.assertEqual(task['attempts'], 1)
                self.assertIsNone(task['error'])
                self.assertEqual(json.loads(task['receipt']), receipt)

    def test_failed_connector_does_not_notify_completion(self):
        self.add_job('new-job', status='pending', stage='archive')
        observer = Mock()
        self.queue.run_ready(Mock(side_effect=OSError('Fixture offline')), on_completed=observer)
        observer.assert_not_called()
        self.assertEqual(self.queue.tasks('new-job')[0]['status'], 'retry')

    def test_idle_worker_pass_reconciles_before_and_after_processing(self):
        with patch.object(workflow, 'reconcile_automatic_suggestions', return_value=0) as reconcile, \
                patch.object(workflow, 'ConnectorRunner') as runner:
            self.assertEqual(workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state),
                                           'work', '--once']), 0)
        self.assertEqual(reconcile.call_count, 2)
        runner.return_value.assert_not_called()

    def test_supervisor_startup_recovers_missed_dispatch_without_recording_work(self):
        with patch.object(workflow, 'reconcile_automatic_suggestions', return_value=0) as reconcile, \
                patch.object(workflow, 'supervise') as supervise, patch.object(workflow.os, 'getppid', return_value=123):
            self.assertEqual(workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state),
                                           'supervise', '--parent-pid', '123']), 0)
        reconcile.assert_called_once()
        supervise.assert_called_once()

    def test_pending_helper_respects_store_activation_boundary(self):
        reviews = Mock()
        reviews.pending_automatic.return_value = True
        with patch('quill_workflow.sharing.SharingReviews', return_value=reviews):
            self.assertTrue(workflow.pending_automatic_suggestions(self.config_path, self.queue.state))
        reviews.pending_automatic.assert_called_once_with(1000.5)
        reviews.close.assert_called_once()

    def test_worker_remains_awake_for_bounded_pending_dispatch_after_filing(self):
        with patch.object(workflow, 'reconcile_automatic_suggestions', return_value=0) as reconcile, \
                patch.object(workflow, 'ConnectorRunner'), \
                patch.object(workflow, 'pending_automatic_suggestions', side_effect=[True, False]) as pending, \
                patch.object(workflow.time, 'sleep') as sleep:
            self.assertEqual(workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state), 'work']), 0)
        self.assertEqual(reconcile.call_count, 4)
        self.assertEqual(pending.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_once_worker_never_waits_for_pending_automatic_dispatch(self):
        with patch.object(workflow, 'reconcile_automatic_suggestions', return_value=0), \
                patch.object(workflow, 'ConnectorRunner'), \
                patch.object(workflow, 'pending_automatic_suggestions', return_value=True) as pending, \
                patch.object(workflow.time, 'sleep') as sleep:
            self.assertEqual(workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state), 'work', '--once']), 0)
        pending.assert_not_called()
        sleep.assert_not_called()

    def test_startup_launch_failure_gets_one_supervised_worker_without_queue_work(self):
        self.add_job('new-job')
        reviews = Mock()
        reviews.request_automatic.return_value = self.pending_review()
        reviews.pending_automatic.return_value = True
        child = Mock(pid=43210)
        child.poll.return_value = None
        def run_supervisor(queue, config_path, launch, parent_pid):
            self.assertFalse(queue.queue.has_work())
            monitor = Supervisor(queue, config_path, launch, clock=lambda: 1002)
            monitor.tick()
            self.assertIs(monitor.child, child)
            # The active child guard prevents a duplicate worker even though
            # the durable automatic request is still pending at this instant.
            monitor.tick()
        with patch('quill_workflow.sharing.SharingReviews', return_value=reviews), \
                patch.object(workflow.subprocess, 'Popen', side_effect=OSError('Calendar process launch failed')), \
                patch.object(workflow, 'start_worker', return_value=child) as start, \
                patch.object(workflow, 'supervise', side_effect=run_supervisor), \
                patch.object(workflow.os, 'getppid', return_value=123):
            self.assertEqual(workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state),
                                           'supervise', '--parent-pid', '123']), 0)
        reviews.automatic_launch_failed.assert_called_once_with('new-job', 'revision-1')
        start.assert_called_once_with(str(self.config_path), self.queue.state, supervised=True)
        self.assertTrue(all(task['status'] == 'succeeded' for task in self.queue.tasks()))

    def test_pending_dispatch_does_not_bypass_live_worker_lock(self):
        launch = Mock()
        wrapped = workflow._SupervisedCompletionQueue(self.queue, self.config_path)
        monitor = Supervisor(wrapped, self.config_path, launch, clock=lambda: 1002)
        with patch('quill_workflow.worker_supervision.lock_held', return_value=True), \
                patch.object(workflow, 'pending_automatic_suggestions', return_value=True) as pending:
            monitor.tick()
        launch.assert_not_called()
        pending.assert_not_called()

    def test_supervisor_parent_binding_remains_enforced(self):
        with patch.object(workflow, 'start_worker') as start, \
                patch.object(workflow, 'reconcile_automatic_suggestions') as reconcile, \
                self.assertRaisesRegex(ValueError, 'running Quill app'):
            workflow.main(['--config', str(self.config_path), '--state', str(self.queue.state),
                           'supervise', '--parent-pid', '1'])
        start.assert_not_called()
        reconcile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
