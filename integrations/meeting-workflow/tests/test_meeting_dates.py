"""Date checks use synthetic timestamps and never read real meetings."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.meeting_dates import recording_timezone


class RecordingDateTests(unittest.TestCase):
    def test_old_metadata_has_no_inferred_recording_location(self):
        self.assertIsNone(recording_timezone({"started": "2026-09-28T05:12:41Z"}))

    def test_timezone_and_historical_offset_are_validated_at_start(self):
        for zone, started, offset in (
                ("Asia/Singapore", "2026-09-28T05:12:41Z", 28800),
                ("America/Los_Angeles", "2026-07-01T07:10:00Z", -25200),
                ("America/Los_Angeles", "2026-01-01T08:10:00Z", -28800),
                ("America/Los_Angeles", "2026-11-01T08:30:00Z", -25200),
                ("America/Los_Angeles", "2026-11-01T09:30:00Z", -28800)):
            with self.subTest(zone=zone, started=started):
                self.assertEqual(recording_timezone({"started": started,
                    "recording_timezone": zone, "recording_utc_offset_seconds": offset}), zone)

    def test_present_invalid_timezone_never_falls_back_to_current_location(self):
        for zone in (None, "", True, "../Asia/Singapore", "Unknown/Place"):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                recording_timezone({"started": "2026-09-28T05:12:41Z", "recording_timezone": zone})

    def test_offset_conflict_or_malformed_offset_requires_review(self):
        for offset in (True, "28800", 0, -25200):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "UTC offset"):
                recording_timezone({"started": "2026-09-28T05:12:41Z",
                    "recording_timezone": "Asia/Singapore", "recording_utc_offset_seconds": offset})

    def test_missing_or_unzoned_start_is_not_guessed(self):
        for started in (None, "", "2026-09-28T05:12:41", "not-a-date"):
            with self.subTest(started=started), self.assertRaises(ValueError):
                recording_timezone({"started": started, "recording_timezone": "Asia/Singapore"})
