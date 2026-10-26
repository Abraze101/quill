# Meeting workflow

This Quill derivative adds meeting detection, recording controls, optional screen
video and a separate completion workflow. Recording and transcription run on the
Mac. Optional Codex notes send transcript text to a hosted model; configured Drive
connections upload selected recordings and documents. Fresh configuration leaves
automatic recording, transcription and completion connections off.

The recorder retains [upstream's MIT license](../LICENSE). The optional Call
Summary adaptation keeps its separate Anthropic attribution and Apache license.

## Current status

Google Meet and Zoom's web client are supported in Chrome. Detection uses visible
in-call controls; a calendar event, waiting room or open meeting page cannot
start recording. The detector currently recognizes English controls. Other apps,
including Discord and the Zoom desktop app, use manual Start and Stop.

The native app provides automatic or manual audio recording, optional window or
display video, Pause/Resume, a floating recording timer, capture-health alerts and
local review for interrupted recordings. A separate worker handles transcription
handoff, notes, folder decisions, Drive filing, retries and sharing review.

Controlled Meet/Zoom capture and private Drive filing have passed on the
maintainer's installation. Those checks do not establish readiness on another
Mac. See [validation and upgrades](#validation-and-upgrades) for the remaining
hardware and real-call checks.

## Opening and updating the installed app

The app targets macOS 15 or later. Building requires Xcode 26 or newer (or matching
Command Line Tools), Swift 6 and the macOS 26 SDK; CI uses Xcode 26.3. Older SDKs
lack concurrency annotations used by the screen-capture code. The workflow
scripts also need Python 3; the automated browser tests need Node.js. Follow the repository
[README](../README.md) for prerequisites and the basic build.

The app packager requires an existing code-signing certificate in the user's
Keychain. Select its exact 40-character SHA-1 fingerprint through
`QUILL_SIGNING_IDENTITY`, or save `{"identity":"CERTIFICATE_FINGERPRINT"}` in
`~/.config/quill/signing.json`. The placeholder must be replaced. Keep this file
private and keep the certificate's private key in Keychain.

For an existing installation, save a [recovery checkpoint](updating-local-source.md)
first. Finish recording and processing, then quit Quill normally. From the
checkout root:

```sh
sh scripts/build-preview-app.sh
sh scripts/install-app.sh
open /Applications/Quill.app
```

The existing `build-preview-app.sh` command now builds the optimized release
configuration and signs `.build/Quill.app`. It refuses a missing or failed release
build rather than using an existing debug executable. Version and build number
come from `Sources/quill/Info.plist`; **About Quill…** in the feather menu displays
them. The signed package marker records those values, the build configuration and
the source executable hash. These are this derivative's versions, independent of
upstream releases.

Installation validates the
certificate and checkout marker before replacing a recognized idle app. Both
preserve the previous app if replacement validation fails. Neither script changes
recording preferences, enables login launch or starts capture.

The packager supports `QUILL_SIGNING_IDENTITY=-` for an ad hoc development preview,
but the installer rejects ad hoc signatures. After a stable identity is saved in
an existing app, replacement with a different identity is refused. Certificate
creation, trust and migration require separate setup. These scripts do not provide
a notarized, general-purpose installer. Keep the existing bundle identifier,
certificate and app path when updating a working installation.

Keep the source checkout at its configured path: completion and review commands
run its Python files. Reopening Quill from Applications shows Settings. The
**Launch at login** and **Show Quill in Dock** choices are explicit Settings
preferences; building or installing does not enable either. The legacy CLI login
installer refuses to add a competing agent when the app is installed.

## Configuration

Preferences live in `~/.config/quill/config.json`. Start with recording and
processing disabled, then opt in after checking permissions and a disposable
capture:

```json
{
  "recordings_dir": "~/Recordings",
  "transcription": {
    "enabled": false,
    "engine": "parakeet",
    "speaker_identification": false
  },
  "automation": {"enabled": false},
  "screen_recording": {"enabled": false, "scope": "window"}
}
```

Use Settings to grant microphone and screen/system-audio access. Permission
approval may require quitting and reopening Quill. Another recorder's permission
does not grant Quill access. Select a specific current window or display before
enabling video. A missing or ambiguous video target fails startup rather than
silently recording a different target.

### Completion setup

Use `integrations/meeting-workflow/workflow.example.json` as the starting point
for `~/.config/quill/workflow.json`. Supply a recordings root, runner paths and
real destination IDs before enabling stages. Merge configuration changes into an
existing setup; do not overwrite its settings with an example.

After configuring the worker, add these fields to Quill's `config.json`, replacing
`/path/to/quill` with the absolute checkout path:

```json
{
  "on_stop": "exec sh '/path/to/quill/scripts/quill-workflow-hook.sh'",
  "on_stop_max_attempts": 6,
  "completion_worker": [
    "/bin/sh",
    "/path/to/quill/scripts/quill-workflow-worker.sh"
  ],
  "meeting_review": [
    "/usr/bin/python3",
    "/path/to/quill/integrations/meeting-workflow/workflow.py",
    "review"
  ]
}
```

The scripts use `/usr/bin/python3`; verify that interpreter is available before
configuring background commands. The worker defaults to
`~/.local/state/quill/workflow`. Custom config/state locations must also be passed
to review and worker commands. `workflow_config_file` and
`completion_status_file` let Quill read alternate settings and status locations.

Configure the optional stages through their guides:

- [Codex notes](codex-notes.md) uses a local authenticated Codex CLI and its hosted
  model. The Call Summary adapter preserves the pinned Anthropic reference and
  Apache license in `integrations/meeting-workflow/skills/call-summary/references/`.
- [Folder routing](company-routing.md) maps suggestions to a fixed list of
  configured folders. Review is the default. Explicit-company filing, when
  enabled, requires a validated direct statement; ambiguous meetings stay held.
- [Drive filing](codex-drive.md) stores recordings separately and combines notes
  and the transcript into one formatted Google Doc. Its optional direct media
  uploader requires separate Google OAuth setup.
- [Notes sharing](notes-sharing.md) suggests calendar attendees and prepares a
  notes-only copy. Sharing requires approval of the exact copy and recipients.

**Create notes and suggest a company** requires local transcription and configured
runners. Google Drive has two independent controls: **Save notes and transcript to
Drive** enables document filing, and **Upload audio and video to Drive** enables
recording uploads. Either can be on by itself; both require notes and routing
first. Each needs its own configured adapter. When both are enabled, they must
use the same Meetings root. Changing one control leaves the other unchanged.
Existing activation choices are preserved when updating Quill.
Turning notes off also turns both Drive options off; turning notes back on leaves Drive
off. Enabling a stage affects new jobs and does not release old holds. Disabling
holds pending/retry work; an already running external operation may finish.

Each job saves its configuration, transcript identity, folder map and approvals.
Changing live settings does not reclassify earlier meetings or replace their
saved destinations. **Configuration…** reveals the workflow file; it does not
connect a new account.

## Browser setup and live acceptance

The unpacked detector requires Chromium 106 or later. In the Chrome profile used
for meetings, open `chrome://extensions`, enable Developer mode, and choose
**Load unpacked**. Select `integrations/meeting-workflow/browser`, which contains
`manifest.json`. Register the exact extension ID displayed by Chrome:

```sh
/usr/bin/python3 integrations/meeting-workflow/install-browser-host.py \
  --browser chrome --extension-id ACTUAL_EXTENSION_ID
```

The installer places the self-contained native host at
`~/Library/Application Support/Quill/BrowserBridge/native-host.py`, with access
restricted to the current user. Its Chrome registration allows only the selected
extension. A conflicting registration stops installation for review. The browser
host does not need to read code from the checkout while running.

Browser script or manifest changes require reloading the extension. Refresh an
already open meeting tab when safe, because it can retain the previous content
script. Native-host changes require rerunning the installer; the new host takes
effect on the next connection.

Detection supports Meet `/<meeting-code>` and `/meet/<meeting-code>` URLs and
Zoom's `/wc/` web client. The bridge passes opaque call identities, state,
timestamps and optional window bounds. It does not send meeting titles,
participants, captions, chat or media. The extension's `webNavigation` permission
supports checking meeting senders; stored detector state contains no URLs.

Before relying on a new installation, use an empty test meeting and verify:

1. Prejoin and waiting screens do not start capture. Joining starts audio only
   when **Record automatically** is enabled, including when Meet's mic is muted.
2. Leaving or closing the tab stops only the automatically owned session.
   **Stop** and **Skip this meeting** prevent restart until that call is left.
3. Separate microphone and system files contain the intended speech/playback.
   If video is enabled, check frames near the start and end and listen to its
   audio rather than relying on the reported MP4 duration.
4. A manual recording remains under manual control. Detector loss warns and
   keeps capture running; it does not treat missing evidence as a confirmed leave.

Muted controls in Meet do not mute Quill's separate microphone capture. Disable
Quill recording for a detection-only test. To test the missed-recording reminder,
join with automatic recording off: the native reminder appears after about ten
seconds, and the browser fallback after about fifteen. Leave before restoring
automatic recording. See [recording reminders](recording-visibility-safety.md#meeting-not-recording-reminder).

## Recording controls

The menu, Settings and floating controls share the current recording state.
**Include screen video** enables video; choosing a scope alone does not. Start
changes to Cancel during startup and Stop after capture is confirmed. Capture
choices lock during recording. Pause keeps one session and excludes paused time
from saved media; final video joining can extend the saving phase.

Window capture requires an explicitly selected native Chrome window. Automatic
starts also check that selection against the detected meeting window. Geometry
alone never selects a target. Keep the meeting in its own Chrome window so other
windows stay outside the video.

For automatic window recordings, switching tabs within that window or moving the
meeting tab stops video while audio continues. The new tab can appear briefly
before the stop. Manual window recording captures whatever that selected window
displays and has no detected-tab guard. Entire-screen capture includes other
visible windows on the chosen display. Separate system audio captures everything
the Mac plays, including unrelated playback, regardless of video scope.

Quill owns its ScreenCaptureKit stream and does not control OBS or another
recorder. The floating timer and **Stop & save** remain available when macOS hides
a crowded menu-bar item. [Recording safeguards](recording-visibility-safety.md)
describe the duration check-in, sleep behavior and local review holds.

## Using the meeting library

**Review meetings…** opens the local library. Search matches titles, participants,
dates and folder names. **Needs attention** shows decisions and recovery work;
folder filters narrow the view without moving files. Select a meeting to open
**Notes**, **Transcript** or **Recording**. **View activity** separates processing,
folder, Drive and sharing status.

If recorded capture metadata reports incomplete audio, new transcripts and notes
carry one brief warning per exported artifact, including shared notes. Clean and
legacy outputs remain unchanged; existing meetings are not reprocessed. The
warning does not change filing or sharing approvals. See [transcription
recovery](transcription-recovery.md) and [Codex notes](codex-notes.md).

**Saved in Drive** means saved receipts match the current meeting. **Partly saved
in Drive** means only some files have that evidence. An unconfirmed result does
not prove that no cloud file exists. **Refresh status** reads local state; it does
not perform a fresh Drive check. Eligible retries reuse saved operation identities
and do not repeat successful stages or approve held work.

**Share notes…** opens a separate approval. Filing privately never grants attendee
access. Calendar suggestions are tentative; they establish invitees, not who
spoke. Closing the review preserves pending decisions. The temporary page binds
to loopback and expires after thirty minutes; reopen it from Quill as needed.

### Removing local meeting files

After stopping capture, choose **More meeting actions → Delete local meeting…**.
The confirmation covers the entire local folder, including audio, video,
transcript, notes and queued copies. Finalized accidental recordings can be
removed before processing finishes. Confirming cancels only that meeting's work
and waits for its readers and writers to release the files before moving them to
macOS Trash. Other meetings continue.

Drive files and shared copies remain unchanged. Receipts and upload journals are
retained, including uncertain cloud operations. **Retry local removal…** resumes
an interrupted Trash move. Restoring the folder does not restart processing;
its deletion marker requires explicit recovery.

### Local meeting names

Enable `local_naming.enabled` to rename future completed folders after validated
notes supply a topic and participants. Names use `Topic - Participants - M-D-YY`
locally and `M/D/YY` in Drive. The date comes from the timezone saved when recording
started. Unknown participant names are omitted. Older recordings without timezone
metadata keep their saved naming plans.

Only the folder name changes; file contents and internal filenames stay stable.
Saved path mappings preserve retries, and collisions never replace another
folder. Enabling naming does not rename old sessions. To rename one completed
meeting from its existing validated notes:

```sh
python3 integrations/meeting-workflow/workflow.py name-local JOB_ID
```

`--refresh` updates a previously named local folder to the current format. Neither
form transcribes, generates notes or contacts Drive. Existing Drive names remain
fixed in their upload journals.

## Connector contract

The completion hook receives one finalized session after transcription finishes
or fails, or immediately after Stop if transcription is disabled. Its durable
handoff registers only that session. Configuring a hook does not scan old
recordings into new external jobs. Handoff success means queue acceptance, not
upload completion. Arbitrary hooks get one attempt by default; only an idempotent
hook should opt into retries.

Command stages use argument arrays. Supported placeholders are `{input}`,
`{output}`, `{skill}`, `{job}`, `{stage}` and `{idempotency_key}`. Processes must
stay in the foreground and return after their action completes. Credentials
belong in the connector's credential store, not command arguments.

- Archive receives the completed recording snapshot.
- Notes receives `transcript.json` and the adapted skill. It writes the skill's
  JSON schema to `{output}`; the worker validates evidence and renders notes.
- Publish receives validated notes after notes succeeds.
- Archive/publish return a receipt with matching job, stage, idempotency key and
  verified destination. A lost response must be reconciled before another write.

Ordinary connector failures have six attempts with backoff. Transient direct
media transfers with a reserved Drive ID can continue retrying, capped at one
hour between attempts. Successful stages are not repeated. Worker supervision
has bounded crash recovery and never releases review or configuration holds.

Inspect state before changing an existing job:

```sh
python3 integrations/meeting-workflow/workflow.py status
python3 integrations/meeting-workflow/workflow.py reviews
```

The following commands can process or release work; use them only for an approved
job and destination:

```sh
python3 integrations/meeting-workflow/workflow.py work --once
python3 integrations/meeting-workflow/workflow.py release JOB_ID STAGE
python3 integrations/meeting-workflow/workflow.py attach-transcript JOB_ID /path/to/original/session/transcript.json
```

`attach-transcript` verifies the original recording files against the saved
snapshot and leaves notes held until explicitly released. Adding a transcript to
a folder does not silently change a queued job. Existing notes and completed
uploads are not regenerated by a configuration change.

## Validation and upgrades

Run `sh scripts/test-workflow.sh` from the repository root. Automated tests use
synthetic events, temporary files, fake connectors and isolated capture boundaries.
They do not record a real meeting, run a notes model or upload meeting data.
Swift package resolution may download dependencies. Native checks use executable
harnesses so they do not require XCTest. The default local suite includes both
real synthetic MP4 assembly cases. Hosted ARM CI explicitly skips only those
cases with `QUILL_SKIP_SYNTHETIC_VIDEO_ASSEMBLY=1` because its virtual Apple video
scaler crashes; pause timing, audio cuts and transition-race checks still run.
A green CI run therefore does not establish real video-codec behavior.

Controlled checks have exercised Meet/Zoom join and leave, local capture and
transcription, optional video, microphone switching, private filing, resumable
media transfer, notes-only sharing and missed-recording alerts. Fullscreen Chrome,
Spaces and Stage Manager visibility passed an awake test on one installation.

Remaining live checks include hardware sleep/wake, physical audio/video
pause-and-resume, output-device changes, external-monitor changes, a fresh macOS
login and a remote participant's audio/video synchronization. Positive automatic
calendar attendee matching, invitation email delivery and speaker/name accuracy
in a real multi-person meeting also remain unverified. Browser UI changes and
hardware differences require new acceptance checks.

Review upstream changes in a separate candidate and preserve a
[recovery checkpoint](updating-local-source.md) before promotion. Keep native
capture, browser detection and completion stages independently testable. Compare
updates to the pinned Anthropic reference separately from the notes adapter.
