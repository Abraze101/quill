"""Recording watchdog bridge tests; all state is synthetic and local to temp dirs."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recording_status_native_host", ROOT / "native-host.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class BrowserRecordingStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.signals = self.root / "meeting-signals"
        self.status = self.root / "recording-status.json"
        self.probe = {"type": "recording-status", "meeting": "opaque-meeting-1", "requestID": "probe-1"}
        self.message = {"source": "tab-1", "meeting": self.probe["meeting"], "provider": "google-meet",
                        "state": "joined", "requestID": "delivery-1"}

    def tearDown(self):
        self.temp.cleanup()

    def write_status(self, **changes):
        data = {"version": 1, "timestamp": 100, "phase": "recording", "deliberatelySkipped": []}
        data.update(changes)
        native.atomic_json(self.status, data)
        return data

    def state(self, meeting=None, now=100):
        return native.recording_status(self.signals, meeting or self.probe["meeting"], now)

    def assert_unavailable(self):
        self.assertEqual(self.state(), {"state": "unavailable"})

    def frames(self, messages):
        result = b""
        for message in messages:
            payload = json.dumps(message).encode()
            result += struct.pack("=I", len(payload)) + payload
        return io.BytesIO(result)

    def read_frames(self, result):
        source = io.BytesIO(result)
        messages = []
        while header := source.read(4):
            length = struct.unpack("=I", header)[0]
            payload = source.read(length)
            self.assertEqual(len(payload), length)
            messages.append(json.loads(payload))
        return messages

    def test_fresh_phases_translate_without_leaking_snapshot_fields(self):
        for phase, state in (("idle", "not-recording"), ("starting", "starting"),
                             ("recording", "recording"), ("paused", "paused"), ("busy", "not-recording")):
            with self.subTest(phase=phase):
                self.write_status(phase=phase)
                response = native.accept(self.probe, self.signals, now=100)
                self.assertEqual(response, {"accepted": True, "requestID": "probe-1",
                                            "recordingStatus": {"state": state}})
                self.assertFalse(self.signals.exists())

    def test_missing_stale_and_future_status_are_unavailable(self):
        self.assert_unavailable()
        for timestamp in (91.99, 102.01, -100, "100", True, None, [], {}):
            with self.subTest(timestamp=timestamp):
                self.write_status(timestamp=timestamp)
                self.assert_unavailable()
        for timestamp in (92, 100, 102):
            with self.subTest(boundary=timestamp):
                self.write_status(timestamp=timestamp)
                self.assertEqual(self.state(), {"state": "recording"})

    def test_corrupt_truncated_nonfinite_duplicate_and_oversized_status_fail_closed(self):
        self.write_status()
        valid = self.status.read_bytes()
        for payload in (b"", valid[:-2], b"[]", b"null", b"\xff", b"{}",
                        valid.replace(b'"timestamp": 100', b'"timestamp": NaN'),
                        valid.replace(b'"timestamp": 100', b'"timestamp": Infinity'),
                        valid.replace(b'"timestamp": 100', b'"timestamp": ' + b"9" * 500),
                        valid.replace(b'"phase": "recording"', b'"phase": "idle", "phase": "recording"'),
                        b" " * (native.STATUS_MAX_BYTES + 1)):
            with self.subTest(payload=payload[:60]):
                self.status.write_bytes(payload)
                self.assert_unavailable()

    def test_invalid_schema_cannot_suppress_warning(self):
        cases = [{"version": 2}, {"version": True}, {"version": 1.0}, {"phase": "Record"},
                 {"phase": []}, {"phase": None}, {"phase": "stopped"}, {"extra": "private"},
                 {"deliberatelySkipped": None}, {"deliberatelySkipped": "opaque-meeting-1"},
                 {"deliberatelySkipped": [True]}, {"deliberatelySkipped": ["../bad"]},
                 {"deliberatelySkipped": ["a" * 129]}, {"deliberatelySkipped": ["a", "a"]},
                 {"deliberatelySkipped": [str(index) for index in range(65)]}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.write_status(**changes)
                self.assert_unavailable()

    def test_skip_applies_only_to_exact_requested_identity_and_fresh_snapshot(self):
        self.write_status(phase="idle", deliberatelySkipped=["opaque-meeting-1"])
        self.assertEqual(self.state(), {"state": "skipped"})
        self.assertEqual(self.state(meeting="opaque-meeting-2"), {"state": "not-recording"})
        self.assertEqual(self.state(meeting="opaque-meeting"), {"state": "not-recording"})
        self.assertEqual(self.state(now=109), {"state": "unavailable"})

    def test_symlink_file_parent_and_nonregular_file_are_rejected_without_blocking(self):
        self.write_status()
        original = self.root / "real-status.json"
        self.status.rename(original)
        self.status.symlink_to(original)
        self.assert_unavailable()
        self.status.unlink()
        self.status.mkdir()
        self.assert_unavailable()
        self.status.rmdir()
        os.mkfifo(self.status, 0o600)
        self.assert_unavailable()
        self.status.unlink()
        original.rename(self.status)
        link = self.root / "linked-state"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(native.recording_status(link / "meeting-signals", "opaque-meeting-1", 100),
                         {"state": "unavailable"})

    def test_shared_or_other_owner_file_is_not_trusted(self):
        self.write_status()
        self.status.chmod(0o644)
        self.assert_unavailable()
        self.status.chmod(0o600)
        with mock.patch.object(native.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_unavailable()
        self.assertEqual(self.state(), {"state": "recording"})

    def test_health_probes_do_not_create_or_refresh_durable_meeting_events(self):
        self.write_status()
        native.accept(self.message, self.signals, now=90)
        signal = next(self.signals.glob("*.json"))
        before = signal.read_bytes(), signal.stat().st_mtime_ns
        native.accept(self.probe, self.signals, now=100)
        self.assertEqual((signal.read_bytes(), signal.stat().st_mtime_ns), before)
        self.assertEqual(list(self.signals.glob("*.json")), [signal])

    def test_invalid_health_requests_do_not_write_signals(self):
        for changes in ({"meeting": "../escape"}, {"meeting": ""}, {"meeting": None},
                        {"requestID": None}, {"requestID": "a" * 65}, {"source": "tab-1"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    native.accept(dict(self.probe, **changes), self.signals, now=100)
                self.assertFalse(self.signals.exists())

    def test_signal_ack_contains_status_only_after_atomic_write_completes(self):
        self.write_status()
        response = native.accept(self.message, self.signals, now=100)
        self.assertEqual(response, {"accepted": True, "requestID": "delivery-1",
                                    "recordingStatus": {"state": "recording"}})
        signal = json.loads(next(self.signals.glob("*.json")).read_bytes())
        self.assertEqual(signal, {"source": "tab-1", "meeting": "opaque-meeting-1", "provider": "google-meet",
                                  "state": "joined", "timestamp": 100})
        with mock.patch.object(native, "atomic_json", side_effect=OSError("Synthetic disk failure")):
            output = io.BytesIO()
            native.serve(self.frames([self.message]), output, self.signals)
        self.assertEqual(self.read_frames(output.getvalue()), [{"accepted": False}])

    def test_framed_health_and_signal_responses_remain_request_bound(self):
        self.write_status()
        left = dict(self.message, state="left", requestID="delivery-2")
        output = io.BytesIO()
        with mock.patch.object(native.time, "time", return_value=100):
            native.serve(self.frames([self.message, self.probe, left]), output, self.signals)
        self.assertEqual(self.read_frames(output.getvalue()), [
            {"accepted": True, "requestID": request, "recordingStatus": {"state": "recording"}}
            for request in ("delivery-1", "probe-1", "delivery-2")])
        self.assertEqual(json.loads(next(self.signals.glob("*.json")).read_bytes())["state"], "left")


if __name__ == "__main__":
    unittest.main()
