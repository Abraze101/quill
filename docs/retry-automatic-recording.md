# Retry a failed automatic recording

If automatic recording could not start, correct the problem in Settings, then
choose **Retry this meeting** in Quill's menu. For example, select the meeting's
new Chrome window after the previous window was closed. The retry remains an
automatic recording and stops when you leave the meeting.

The action appears when automatic recording is enabled, Quill is idle, and
exactly one failed meeting has fresh, stable joined evidence. It retries that
meeting only. It cannot override **Skip this meeting**, a cancelled start, or
another recording. A repeated failure waits for another explicit retry.

Failed-start information survives Quit & Reopen, including permission-related
restarts. Existing saved skips remain skipped; they are never inferred to be
failed starts. The local `meeting-skips.json` reader accepts the earlier array
format and saves new state with separate `skipped` and `failedStarts` fields.

Quill still requires an explicitly selected meeting window for screen video.
Correcting that selection alone does not start recording. The ordinary **Start
recording** action remains manual and requires a manual stop.
