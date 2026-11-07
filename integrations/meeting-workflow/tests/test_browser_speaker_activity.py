"""Speaker telemetry must never change whether a browser signal is delivered."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("speaker_activity_native_host", ROOT / "native-host.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class BrowserSpeakerActivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.signals = Path(self.temporary.name) / 'meeting-signals'
        self.context = {'provider': 'google-meet', 'key': 'abc-defg-hij', 'coverage': 'visible_only',
                        'participants': [{'name': 'Casey Rivera', 'self': False}]}
        self.activity = {'schema_version': 1, 'source': 'google-meet-dom-v1', 'document_id': 'doc-A',
                         'participants': [{'id': 'p1', 'name': 'Casey Rivera', 'self': False}],
                         'samples': [{'at_ms': 99500, 'active': ['p1']}, {'at_ms': 100000, 'active': ['p1']}]}
        self.message = {'source': 'tab-A', 'meeting': 'call-A', 'provider': 'google-meet', 'state': 'joined',
                        'meetingContext': self.context, 'speakerActivity': self.activity}

    def tearDown(self):
        self.temporary.cleanup()

    def saved(self, message=None):
        result = native.accept(message or self.message, self.signals, now=100)
        self.assertTrue(result['accepted'])
        return json.loads((self.signals / 'tab-A-call-A.json').read_text())

    def test_valid_telemetry_preserves_self_and_simultaneous_activity(self):
        self.activity['participants'].append({'id': 'p2', 'name': 'Jamie Riley', 'self': True})
        self.activity['samples'][-1]['active'] = ['p1', 'p2']
        saved = self.saved()
        self.assertEqual(saved['speakerActivity'], self.activity)
        self.assertEqual(saved['timestamp'], 100)

    def test_invalid_telemetry_never_blocks_capture_signal(self):
        mutations = [None, {}, {'schema_version': True}, {'source': 'unverified'}, {'document_id': 'x' * 65},
                     {'participants': self.activity['participants'] * 2},
                     {'participants': [{'id': 'p1', 'name': 'Casey', 'self': 'true'}]},
                     {'participants': [{'id': 'p1', 'name': 'person@example.test', 'self': True}]},
                     {'participants': [{'id': 'p1', 'name': 'Casey\u202e', 'self': True}]},
                     {'samples': [{'at_ms': True, 'active': ['p1']}]},
                     {'samples': [{'at_ms': 100000, 'active': [True]}]},
                     {'samples': [{'at_ms': 79999, 'active': ['p1']}]},
                     {'samples': [{'at_ms': 101001, 'active': ['p1']}]},
                     {'samples': [{'at_ms': 100000, 'active': ['missing']}]},
                     {'samples': [{'at_ms': 100000, 'active': ['p1', 'p1']}]},
                     {'samples': self.activity['samples'][::-1]},
                     {'samples': self.activity['samples'] * 17}, {'extra': 'not allowed'}]
        for changes in mutations:
            value = None if changes is None else dict(copy.deepcopy(self.activity), **changes)
            if changes == {}:
                value = {}
            with self.subTest(changes=changes):
                saved = self.saved(dict(self.message, speakerActivity=value))
                self.assertNotIn('speakerActivity', saved)
                self.assertEqual(saved['state'], 'joined')
                self.assertEqual(saved['meetingContext'], self.context)

    def test_unknown_left_and_context_free_events_omit_activity(self):
        for state in ('left', 'unknown'):
            saved = self.saved(dict(self.message, state=state))
            self.assertNotIn('speakerActivity', saved)
        message = dict(self.message)
        del message['meetingContext']
        self.assertNotIn('speakerActivity', self.saved(message))

    def test_largest_bounded_activity_can_cross_old_four_kilobyte_frame_limit(self):
        self.activity['participants'] = [{'id': f'p{i}', 'name': '界' * 99 + chr(65 + i), 'self': i == 0}
                                         for i in range(20)]
        self.activity['samples'] = [{'at_ms': 84500 + i * 500, 'active': ['p1']} for i in range(32)]
        payload = json.dumps(self.message).encode()
        self.assertGreater(len(payload), 4096)
        self.assertLessEqual(len(payload), native.NATIVE_MESSAGE_MAX_BYTES)
        source = io.BytesIO(struct.pack('=I', len(payload)) + payload)
        target = io.BytesIO()
        with mock.patch.object(native.time, 'time', return_value=100):
            native.serve(source, target, self.signals)
        self.assertTrue(json.loads(target.getvalue()[4:])['accepted'])
        self.assertEqual(len(json.loads((self.signals / 'tab-A-call-A.json').read_text())['speakerActivity']['participants']), 20)

    def test_oversized_native_frame_is_rejected_before_reading_body(self):
        with self.assertRaisesRegex(ValueError, 'too large'):
            native.serve(io.BytesIO(struct.pack('=I', native.NATIVE_MESSAGE_MAX_BYTES + 1)), io.BytesIO(), self.signals)
        self.assertFalse(self.signals.exists())


if __name__ == '__main__':
    unittest.main()
