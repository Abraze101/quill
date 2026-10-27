# Recording visibility and unattended-session safety

Quill's floating controls show whether capture is starting, recording, paused,
saving or needs attention. They display elapsed recorded time, microphone/system
status, optional video status and **Stop & save**. The compact pill keeps recording
visible without opening the menu.

macOS can hide menu-bar items when space runs out. Quill's floating control gives
access independently of the feather; **Show Quill in Dock** is an optional Settings
preference. There is no reserved menu-bar slot. See [Apple's status-item documentation](https://developer.apple.com/documentation/appkit/nsstatusitem/isvisible).

## Recent warnings

Open **Recent warnings…** from the feather menu or expanded recording controls
to read native capture warnings, recording check-ins, missed-meeting reminders,
and processing problems. Each entry shows when a warning appeared, changed, or
stopped being shown. Ending a session is recorded separately; it is not evidence
that an audio problem recovered.

The last 200 changes are saved privately on this Mac in
`~/.local/state/quill/warning-history/history.json`. Repeated timer updates do not add
entries. History survives an app restart, but old entries are not presented as
current capture status. It starts with this version and cannot reconstruct
earlier flashes. Browser-only banners and macOS notifications are not included.
History is diagnostic only: it does not control recording, clear review holds,
or upload anything. If saving history fails, entries remain visible for the
current run and the history window explains the storage problem.

During steady recording, the floating timer updates only its elapsed text.
Unchanged controls, symbols, tooltips, and layout are retained; an unchanged
hidden reminder does not repeatedly issue window-hide operations. Actual
warnings still appear immediately. These changes remove redundant redraw work;
the original intermittent flash still needs confirmation during ordinary use.

## Meeting not recording reminder

Fresh joined-call evidence without recording shows a floating **Meeting is not
recording** alert after ten seconds. It offers **Start recording** and **Skip this
meeting**. Starting or saving gets thirty seconds before a reminder; when capture
is busy or awaits review, the action opens the existing recording controls.
A notification accompanies the first alert for that uncovered call. The panel
works without notification permission or visible menu-bar space.

Recording, a deliberate pause, Skip and manual Stop silence the reminder. Failed
automatic starts remain eligible. Actions belong to the displayed call; a delayed
click cannot start or skip a different meeting. Starting while automatic recording
is off makes a manual recording without changing that preference.

The Chrome extension independently checks the app's private, short-lived status
heartbeat. After fifteen seconds without confirmation, it shows a banner inside
the call; startup gets a bounded thirty-second grace period:

- **Quill is not recording** means the app reports no active capture.
- **Quill cannot confirm recording** means the app, bridge or confirmation is
  unavailable.
- **Dismiss reminder** hides that call's browser warning only. It does not start,
  stop or skip recording.

The browser reminder recognizes in-call controls independently of the stricter
recording URL parser. It can warn on a new Meet URL path without authorizing that
path to start automatic recording. Status requests cannot write join signals or
control capture. They carry opaque identities and return a status value, with no
meeting title, attendees, transcript or media. App status expires after eight
seconds; a stale confirmation cannot remain proof of recording.

Coverage requires the extension to be enabled in the Chrome profile used for the
call and recognizable in-call controls. Meet and Zoom's current `/wc/` web client
are supported. An open app, calendar event, waiting room or Discord call alone
cannot trigger these reminders. A disabled or missing extension cannot display
the browser fallback. Capture-quality alerts remain separate from these checks.

## Pause and resume

Pause and Resume are available in the floating controls, compact pill, menu and
Settings. A paused session shows **Paused**, freezes its recorded-duration timer
and keeps **Stop & save** available. Repeated or stale actions cannot change a
newer recording.

Pausing keeps the same meeting folder. Audio writers discard paused samples,
while microphone/system transports remain open for recovery and Resume. The
macOS microphone indicator can remain visible while paused. Screen capture stops
its native stream and starts a new segment for the same target on Resume. Final
Stop trims segments to the shared audio boundaries and joins `screen.mp4`.
Joining can take additional time; failures preserve the segments and hold the
meeting locally for review. Pause does not start transcription or filing.

The unattended-session deadline continues while paused. **Continue 1 hour**
extends the deadline without resuming capture. Automatic leave, system sleep and
Stop supersede a pending Resume.

## Recording visibility and unattended sessions

Settings offers a one-, two- or four-hour check-in; changes apply to the next
recording. The default is two hours. At the deadline, a persistent five-minute
countdown offers **Stop & save** and **Continue 1 hour**. No response stops and saves
locally for review. Each extension creates a new deadline. This policy covers
manual and automatic Quill recordings, including manually started app calls.

Meeting-leave detection controls only the exact automatically owned session.
Loss of detection shows a warning and leaves capture running; missing signals
are not proof of leaving. Silence, inactivity, window changes, display sleep and
locking alone are not meeting-end evidence. Quill never stops an unrelated manual
recording or controls another recorder.

Elapsed time and media status come from the capture lifecycle, not enabled
preferences or a file's existence. Requested screen video must produce a native
writer-start callback and a complete frame before startup is confirmed. A failed
requested video start cancels startup and shows the error. A later video failure
reports that audio continues and saves the failure for review.

Quill's floating controls use a nonactivating panel, with text accompanying color.
They remain available during saving or uncertain capture shutdown. The capture
filter excludes this Quill process's windows from its whole-display recordings;
this does not promise exclusion from another application's screen sharing.

## Review before processing

`recording-safety.json` is written before capture starts. It records session
identity, ownership, deadlines, review state and failure reasons. An unanswered
check-in, interrupted capture, incomplete microphone/system coverage or video
failure holds the meeting before transcription, notes or uploads. Corrupt and
unfinished journals also require review.

**Keep local** preserves the files without processing. **Process entire recording**
requires finalized media and releases that capture review; folder selection and
sharing approval remain separate. There is no automatic trimming at the suspected
meeting end. Existing recordings without a safety journal retain their previous
processing policy.

The completion worker rechecks the original recording's review state before
ingestion, retries, stage execution, local naming and Drive requests. It resumes a
resolved safety hold without releasing an unrelated configuration/company hold.
An external request already sent cannot be recalled. Restarts never silently
resume an interrupted capture or turn a pending hold into an ordinary completion.

For newly recorded PCM audio, [crash recovery](crash-recovery.md) can rebuild
readable interrupted sessions into this review flow. Recovered recordings remain
on hold until a decision; recovery does not resume recording or external work.

## System sleep handling

Quill observes actual system sleep and wake. Before the sleep callback returns,
it blocks new starts, persists the local review hold, cancels pending startup and
stops the owned microphone. The first stop timestamp is frozen so time asleep
cannot inflate the session duration. System-audio and screen cleanup remain
asynchronous; unfinished cleanup reconciles after wake. Saving or unconfirmed
capture stays visible until shutdown completes.

Sleep during an existing Stop or unfinished cleanup also holds that session.
Duplicate notifications cannot reopen capture or replace its stop time. The
interrupted automatic meeting stays suppressed until it is left. Wake shows the
saved review or unfinished cleanup; it never restarts capture, transcribes or
uploads that interrupted session. Display sleep and locking alone do not invoke
this stop policy.

The safety clock uses elapsed deadlines rather than counting UI timer callbacks.
It includes sleep and is independent of wall-clock changes. Stop, Pause/Resume
and Continue actions carry the relevant session/revision, so older controls cannot
affect a newer session.

## System-audio recovery

The system track monitors successful buffer writes and audio-route changes.
Silence in a valid buffer counts as progress. A device change does not interrupt
capture while buffers continue. Missing buffers or a write failure trigger up to
three reconnect attempts; sustained fresh audio resets that allowance. The
controls show Reconnecting or Unavailable, with Stop still available.

Recovery preserves `system.caf`, its original track start and fixed output format.
Changed input formats are converted, and silence covers short missing intervals
to retain later timing. Long gaps and exhausted retries remain incomplete.
Recovered interruptions still hold the recording locally for review. Stop and
system sleep invalidate callbacks and pending reconnects before awaiting cleanup.

This behavior selectively adapts [upstream system-audio recovery](https://github.com/humanitas-labs/quill/commit/8c46659a2a67638ccbf36e1d4d1e0036c81976f4).
The separate microphone track and completion format remain intact. Optional
`screen.mp4` uses its own ScreenCaptureKit audio path; recovery of `system.caf`
does not establish the video's audio quality or synchronization.

## Current verification

Automated checks cover duration boundaries, delayed callbacks, clock changes,
failed starts, stale actions, capture ownership, paused sample exclusion, video
segment joining, sleep notifications, reconnect limits, review holds and worker
retries. Native controls have been rendered and inspected. These tests use
synthetic media or isolated capture boundaries rather than real calls.

Controlled live checks have verified:

- An accelerated check-in, Continue, unanswered stop, local review and ordinary
  application quit. Production defaults remained two hours/five minutes/one hour;
  the test did not wait two hours.
- Counting controls over fullscreen Chrome and during an awake Spaces/Stage
  Manager test. Sampled Quill recordings excluded its panel.
- An empty Meet with automatic recording off: no prejoin warning, a browser
  warning while Quill was closed, native/browser warnings while idle, and exact-call
  Skip clearing both. That check created no recording.
- Microphone switching with recovered speech and an explicitly recorded gap.
  Controlled video playback and separate-window isolation also passed.

Still pending are physical lid-close/wake, output-device changes, external-monitor
changes/removal, a fresh macOS login, and real-device audio/video Pause/Resume.
Remote-participant speech and measured call audio/video synchronization need a
controlled call. Native-player timing checks and synthetic media do not establish
that result. Dock Quit/reopening while capture is active, VoiceOver behavior and
broader capture-permission/target-loss edge cases also need live acceptance.

Run a disposable recording with processing disabled for a new hardware check.
Confirm the intended source, visible controls, saved track coverage and decoded
media before restoring everyday preferences. Keep the test's evidence separate
from real meetings and do not report an untested platform as verified.
