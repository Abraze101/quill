"""Native-copy publishing lifecycle with synthetic Drive and renderer contracts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.drive_upload import (DriveUploader, UploadNeedsReview, NATIVE_TEMPLATE_FORMAT,
                                         DOCUMENT_MIME, native_template_sha256, inventory)
from quill_workflow.pipeline import atomic_json
from test_drive_upload import FakeDrive, SimulatedTransportFailure


def body_text(document):
    return document['tabs'][0]['body']['text']


def expected_text(document, payload):
    text = body_text(document)
    if text.count('{{QUILL_TITLE}}') != 1:
        raise ValueError('Template must have one title')
    return text.replace('{{QUILL_TITLE}}', payload['title'])


def plan_requests(document, payload):
    expected_text(document, payload)
    return [{'replaceAllText': {'containsText': {'text': '{{QUILL_TITLE}}', 'matchCase': True},
                                'replaceText': payload['title']}}]


def verify_document(document, payload, template):
    if body_text(document) != expected_text(template, payload):
        raise ValueError('Document text differs')
    if document['tabs'][0]['style'] != template['tabs'][0]['style']:
        raise ValueError('Document styles differ')
    return True


class NativeDrive(FakeDrive):
    TEMPLATE = 'template_document123'

    def __init__(self):
        super().__init__()
        self.add(self.TEMPLATE, 'Reusable notes template', self.ROOT, DOCUMENT_MIME)
        self.documents = {self.TEMPLATE: {'documentId': self.TEMPLATE, 'revisionId': 'template-revision',
            'title': 'Reusable notes template', 'tabs': [{'tabId': 'notes', 'style': {'font': 'Arial'},
                                                        'body': {'text': 'Meeting\n{{QUILL_TITLE}}\n'}}]}}
        self.fail_copy = False
        self.fail_update = None
        self.after_copy = None

    @property
    def native_writes(self):
        return [(tool, args) for tool, args in self.calls if tool in {'copy_file', 'batch_update_document'}]

    def __call__(self, tool, arguments):
        if tool not in {'copy_file', 'batch_update_document', 'get_document'}:
            return super().__call__(tool, arguments)
        args = copy.deepcopy(arguments)
        self.calls.append((tool, args))
        if tool == 'get_document':
            return copy.deepcopy(self.documents[args['document_id']])
        if tool == 'copy_file':
            self.counter += 1
            identity = 'copied_document_' + str(self.counter)
            self.add(identity, args['new_title'], args['parent_folder'], DOCUMENT_MIME)
            self.documents[identity] = copy.deepcopy(self.documents[self.TEMPLATE])
            self.documents[identity].update(documentId=identity, revisionId='before-fill', title=args['new_title'])
            if self.after_copy:
                self.after_copy(self.documents[identity])
            if self.fail_copy:
                raise SimulatedTransportFailure()
            return {'success': True, 'id': identity}
        document = self.documents[args['document_id']]
        assert args['write_control'] == {'requiredRevisionId': document['revisionId']}
        if self.fail_update == 'before':
            raise SimulatedTransportFailure()
        for request in args['requests']:
            change = request['replaceAllText']
            document['tabs'][0]['body']['text'] = body_text(document).replace(change['containsText']['text'], change['replaceText'])
        document['revisionId'] = 'after-fill'
        if self.fail_update == 'after':
            raise SimulatedTransportFailure()
        return {'documentId': args['document_id'], 'replies': [{}]}


class NativeTemplateDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'notes'
        self.source.mkdir()
        self.drive = NativeDrive()
        self.uploader = DriveUploader(self.root / 'state', self.drive, self.drive.ROOT)
        self.route = {'company_id': 'acme', 'company_name': 'Acme', 'drive_folder_id': self.drive.COMPANY}
        self.options = {'text_format': NATIVE_TEMPLATE_FORMAT, 'document_title': 'Planning - 9/14/26',
                        'meeting_title': 'Planning', 'template_document_id': self.drive.TEMPLATE,
                        'template_sha256': native_template_sha256(self.drive.documents[self.drive.TEMPLATE]),
                        'template_payload': {'title': 'Planning'}}
        module = types.ModuleType('quill_workflow.native_template')
        module.expected_text, module.plan_requests, module.verify_document = expected_text, plan_requests, verify_document
        self.mock_module = patch.dict(sys.modules, {module.__name__: module})
        self.mock_module.start()

    def tearDown(self):
        self.mock_module.stop()
        self.temp.cleanup()

    def publish(self, **changes):
        return self.uploader.upload('synthetic-job', 'publish', self.source, self.route,
                                    '2026-09-14T09:30:00-07:00', **dict(self.options, **changes))

    def journal(self):
        return json.loads((self.root / 'state/results/synthetic-job/drive-publish.json').read_text())

    def test_copy_fill_verify_then_repeat_reuses_single_native_document(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual([tool for tool, _ in self.drive.native_writes], ['copy_file', 'batch_update_document'])
        self.assertEqual(first['files'][0]['id'], second['files'][0]['id'])
        self.assertEqual(first['verification'], 'metadata_and_template_content')
        self.assertEqual(self.journal()['operations']['file:native-template']['fill']['state'], 'verified')
        self.assertEqual([tool for tool, _ in self.drive.writes].count('import_document'), 0)
        self.assertEqual([tool for tool, _ in self.drive.writes].count('upload_file'), 0)

    def test_lost_copy_response_holds_without_adopting_or_copying_again(self):
        self.drive.fail_copy = True
        with self.assertRaisesRegex(UploadNeedsReview, 'no saved Drive ID'):
            self.publish()
        self.drive.fail_copy = False
        with self.assertRaisesRegex(UploadNeedsReview, 'no saved Drive ID'):
            self.publish()
        self.assertEqual([tool for tool, _ in self.drive.native_writes], ['copy_file'])

    def test_lost_edit_response_after_write_readback_recovers_without_second_edit(self):
        self.drive.fail_update = 'after'
        with self.assertRaises(SimulatedTransportFailure):
            self.publish()
        operation = self.journal()['operations']['file:native-template']
        self.assertTrue(operation['id'])
        self.assertEqual(operation['fill']['state'], 'dispatched')
        self.drive.fail_update = None
        receipt = self.publish()
        self.assertEqual(receipt['files'][0]['id'], operation['id'])
        self.assertEqual([tool for tool, _ in self.drive.native_writes], ['copy_file', 'batch_update_document'])
        self.assertTrue(self.journal()['operations']['file:native-template']['fill']['reconciled'])

    def test_lost_edit_before_write_retries_only_at_unchanged_revision(self):
        self.drive.fail_update = 'before'
        with self.assertRaises(SimulatedTransportFailure):
            self.publish()
        self.drive.fail_update = None
        self.publish()
        self.assertEqual([tool for tool, _ in self.drive.native_writes],
                         ['copy_file', 'batch_update_document', 'batch_update_document'])

    def test_uncertain_edit_with_external_change_holds_saved_copy(self):
        self.drive.fail_update = 'before'
        with self.assertRaises(SimulatedTransportFailure):
            self.publish()
        identity = self.journal()['operations']['file:native-template']['id']
        self.drive.documents[identity]['revisionId'] = 'external-edit'
        self.drive.fail_update = None
        with self.assertRaisesRegex(UploadNeedsReview, 'changed during editing'):
            self.publish()
        self.assertEqual(len(self.drive.native_writes), 2)

    def test_changed_template_input_or_copied_style_is_held(self):
        self.drive.documents[self.drive.TEMPLATE]['tabs'][0]['style']['font'] = 'Different'
        with self.assertRaisesRegex(UploadNeedsReview, 'template changed'):
            self.publish()
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.drive.native_writes, [])
        self.drive.documents[self.drive.TEMPLATE]['tabs'][0]['style']['font'] = 'Arial'
        self.drive.after_copy = lambda doc: doc['tabs'][0]['style'].update(font='Unexpected')
        with self.assertRaisesRegex(UploadNeedsReview, 'differs from the frozen template'):
            self.publish()
        self.assertEqual([tool for tool, _ in self.drive.native_writes], ['copy_file'])

    def test_changed_payload_or_template_settings_does_not_reuse_old_receipt(self):
        self.publish()
        for changes in ({'template_payload': {'title': 'Changed notes'}},
                        {'template_document_id': 'different_template123'}, {'template_sha256': 'b' * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(UploadNeedsReview, 'inputs or destination changed'):
                self.publish(**changes)
        self.assertEqual(len(self.drive.native_writes), 2)

    def test_archive_in_new_format_remains_media_only(self):
        atomic_json(self.source / 'meta.json', {'started': '2026-09-14T09:30:00-07:00',
                    'ended': '2026-09-14T09:31:00-07:00', 'files': {'mic': 'mic.caf', 'system': 'system.caf'}})
        for name in ('mic.caf', 'system.caf', 'transcript.json', 'transcript.md'):
            (self.source / name).write_text('Synthetic fixture')
        self.assertEqual([item['name'] for item in inventory(self.source, 'archive', NATIVE_TEMPLATE_FORMAT)],
                         ['mic.caf', 'system.caf'])

    def test_template_hash_ignores_revision_and_identity_but_preserves_content_and_styles(self):
        before = self.drive.documents[self.drive.TEMPLATE]
        changed = copy.deepcopy(before)
        changed.update(documentId='copy123', title='Another filename', revisionId='later')
        self.assertEqual(native_template_sha256(before), native_template_sha256(changed))
        changed['tabs'][0]['style']['font'] = 'Different'
        self.assertNotEqual(native_template_sha256(before), native_template_sha256(changed))

    def test_null_dropdown_metadata_does_not_change_approved_template_hash(self):
        before = self.drive.documents[self.drive.TEMPLATE]
        changed = copy.deepcopy(before)
        changed['tabs'][0]['dropdownDefinitions'] = None
        self.assertEqual(native_template_sha256(before), native_template_sha256(changed))
        changed['tabs'][0]['dropdownDefinitions'] = {'status': {'options': ['Approved']}}
        self.assertNotEqual(native_template_sha256(before), native_template_sha256(changed))
        other = copy.deepcopy(changed)
        other['tabs'][0]['dropdownDefinitions']['status']['options'] = ['Needs review']
        self.assertNotEqual(native_template_sha256(changed), native_template_sha256(other))

    def test_real_renderer_contract_and_native_date_requests_reach_transport(self):
        self.mock_module.stop()
        try:
            from test_native_template import template_document, filled_document
            from quill_workflow.native_template import build_payload
            from quill_workflow.codex_drive_transport import validate_arguments
            template = template_document()
            template['documentId'] = self.drive.TEMPLATE
            self.drive.documents[self.drive.TEMPLATE] = template
            transcript = {'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': 'Plan the agenda.'}]}
            notes = {'schema_version': 1, 'meeting_title': {'text': 'Agenda planning', 'evidence': [0], 'participants': []},
                     'summary': [{'text': 'Plan the agenda.', 'evidence': [0]}]}
            payload = build_payload(notes, transcript, {'started': '2026-09-14T09:30:00-07:00', 'ended': '2026-09-14T09:31:00-07:00'},
                                    self.route, 'America/Los_Angeles')
            def invoke(tool, args):
                validate_arguments(tool, args)
                if tool != 'batch_update_document':
                    return self.drive(tool, args)
                self.drive.calls.append((tool, copy.deepcopy(args)))
                before = self.drive.documents[args['document_id']]
                self.assertEqual(args['write_control']['requiredRevisionId'], before['revisionId'])
                self.drive.documents[args['document_id']] = filled_document(payload, before)
                return {'documentId': args['document_id'], 'replies': [{} for _ in args['requests']]}
            self.uploader.invoke = invoke
            result = self.publish(template_payload=payload, document_title=payload['title'],
                                  template_sha256=native_template_sha256(template))
            self.assertEqual(result['verification'], 'metadata_and_template_content')
            requests = next(args['requests'] for tool, args in self.drive.native_writes if tool == 'batch_update_document')
            self.assertEqual(sum('insertDate' in request for request in requests), 1)
        finally:
            self.mock_module.start()


if __name__ == '__main__':
    unittest.main()
