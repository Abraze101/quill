# Retry unfinished transcription

Choose **Retry unfinished transcriptions…** in the feather menu or Quill's
application menu to retry without restarting. Local transcription must be on.
Quill reports how many recordings were newly queued; an empty result means no
additional eligible recordings were found. Already running or queued recordings
are excluded, and completed transcripts are never overwritten. The menu action
is temporarily disabled while it checks for work.

The same recording eligibility rules apply at startup and on manual retry:

Each newly completed recording saves `transcription_requested` in `meta.json`,
using the local transcription setting when Quill finalizes that recording.
Quill resumes unfinished transcription only when that saved value is
`true`. Turning transcription on later leaves recordings made with it off alone.

For older recordings without this field, recovery requires an existing completion
journal entry in `waitingForTranscript`. This preserves interrupted handoffs that
were registered when recording stopped. An explicit `false` or malformed value
prevents recovery even if an old journal entry is waiting. Recordings with an
existing `transcript.json` are left alone.

Quill atomically writes the readable `transcript.md` first, then atomically writes
`transcript.json` as the completion marker. A failed Markdown write cannot mark
transcription complete. If JSON publication fails, the Markdown file may remain,
but recovery still treats the session as unfinished. Cancellation and processing
holds are checked before each write; retry still requires the saved recording
intent and processing approval.

When recorded microphone or system-audio metadata reports an interruption or
capture failure, the transcript includes bounded `capture_quality` evidence and
one readable warning. Clean reports and older metadata without capture reports
add no warning; absence of a report does not prove complete capture. Existing
transcripts are not rewritten to add this field. See [Codex notes](codex-notes.md)
for its treatment in notes and exported documents.

Recovery still requires local transcription to be on. It examines only completed
session directories directly under the configured recordings folder and rejects
linked sessions, linked metadata, invalid metadata, and paths outside that folder.
No completion hook is needed to recover a new recording with saved intent `true`.
These changes do not enable external processing or release held completion jobs.

Retrying transcription does not create a new completion hook or replay one that
was already delivered. If meeting processing previously accepted a recording
without a transcript, use its existing transcript attachment and release controls
to continue notes or Drive filing after the local transcript is ready.

The synthetic checks run without opening capture devices or loading a model:

```sh
sh integrations/meeting-workflow/tests/test-transcription-recovery.sh
sh integrations/meeting-workflow/tests/test-transcription-lease.sh
sh integrations/meeting-workflow/tests/test-audio-lifecycle.sh
```
