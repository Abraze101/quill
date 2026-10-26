# Quill meeting workflow

A macOS version of [Quill](https://github.com/humanitas-labs/quill),
originally published as `digimata/quill`. It records meetings locally and adds
optional transcription, reviewed notes, and Google Drive filing.

This is a source repository for a custom workflow. It is not an official upstream
release or a notarized app download. The original MIT license is preserved;
[third-party notices](docs/third-party-notices.md) cover the icon and notes adapter.

## What it does

- Detects actual Google Meet and Zoom web joins/leaves in Chrome, with automatic
  recording, manual Start/Stop, Pause/Resume, and Skip controls.
- Records microphone and system audio separately, with optional native window or
  display video. Floating recording controls remain accessible when the menu bar
  is crowded.
- Warns when a joined meeting is not recording. The browser extension can also
  warn when the app or its connection is unavailable.
- Transcribes on-device and optionally separates speakers. Names require clear
  spoken introductions; uncertain voices keep anonymous labels.
- Retries unfinished, previously opted-in transcription from either Quill menu,
  without restarting or rewriting completed transcripts.
- Uses a separate completion worker for optional Codex notes, destination
  suggestions, separate Google Drive controls for documents and recordings,
  and retries that reuse reserved file IDs.
- Presents a local meeting library for reviewing notes, recordings, folder
  decisions, retries, and deletion of an entire local meeting to Trash.
- Suggests sharing recipients from calendars. Sharing requires review and an
  explicit approval for a notes-only copy; recordings stay private.
- Holds interrupted or degraded recordings for review before processing, with
  recovery checkpoints for app and source updates.

Recording automation, transcription, notes, filing, and sharing need their own
configuration. A fresh clone does not inherit another Mac's settings or access.
Each person supplies their own account connections, destination folders, and Mac
permissions. Discord and other unsupported apps use manual recording.

## Requirements

- macOS 15 or later; Apple Silicon is recommended for local transcription.
- Xcode 26 or newer, or matching Command Line Tools, with Swift 6 and the macOS
  26 SDK. The app still targets macOS 15; older SDKs lack concurrency annotations
  used by the screen-capture code. CI selects Xcode 26.3 explicitly.
- Python 3.9 or later for the completion workflow; Node.js 22 or later for tests.
- Chrome for automatic meeting detection.
- A local code-signing certificate for an installed app with a stable identity.
- Optional: Codex CLI with a saved login and configured Drive/calendar connections.
  Large media uploads can use a separately authorized Google Drive OAuth client.

The Swift dependency revisions are pinned in `Package.resolved`. Model downloads
are separate from source builds; no model weights are committed here.

## Build from source

Clone this version, then build from its repository root:

```sh
git clone https://github.com/Abraze101/quill.git
cd quill
swift build -c release --product quill
.build/release/quill doctor
```

If the local Swift installation reports mismatched SwiftPM module interfaces,
use the project-local wrapper:

```sh
sh scripts/swift-local.sh build -c release --product quill
```

The wrapper copies SwiftPM modules into `.build/`; it does not change system
developer tools. A source build does not start recording or enable login startup.
Omit `-c release` for an unoptimized development build in `.build/debug/`.

To package the optimized local app, configure an existing certificate fingerprint
in `~/.config/quill/signing.json`, then run:

```sh
sh scripts/build-preview-app.sh
```

The script retains its existing name but always builds and packages the release
configuration. **About Quill…** in the feather menu shows the custom app's version
and build number from `Sources/quill/Info.plist`.

See the [setup guide](docs/meeting-workflow.md) for signing, installation,
permissions, and the Chrome extension. The installer rejects ad hoc signatures
and unreviewed identity changes. Keep the existing bundle ID and signing identity
when updating an installed copy.

## Configure recording and completion

Quill reads `~/.config/quill/config.json`. A minimal opt-in starting point is:

```json
{
  "recordings_dir": "~/Recordings",
  "automation": { "enabled": false },
  "transcription": { "enabled": false, "speaker_identification": false },
  "screen_recording": { "enabled": false, "scope": "window" }
}
```

Settings and the feather menu control recording options. Screen video requires
an explicit window or display selection. For window video, keep the meeting in
its own Chrome window; the selected window's visible content is what is captured.

Configure postprocessing separately using
[`workflow.example.json`](integrations/meeting-workflow/workflow.example.json).
Keep actual settings, folder IDs, templates, credentials, and recordings outside
this repository. The `on_stop` hook queues only the completed session passed to it.
It does not import old recordings automatically.

| Guide | Scope |
| --- | --- |
| [Meeting workflow](docs/meeting-workflow.md) | App setup, Chrome detection, controls, and completion wiring |
| [Recording safeguards](docs/recording-visibility-safety.md) | Reminders, duration checks, pause, sleep, and review holds |
| [Crash recovery](docs/crash-recovery.md) | Recovery of interrupted audio into local review and storage limits |
| [Local transcription recovery](docs/transcription-recovery.md) | Restart behavior and saved processing intent |
| [Speaker identification](docs/speaker-identification.md) | Voice separation, name evidence, and limits |
| [Codex notes](docs/codex-notes.md) | Notes runner and the pinned Call Summary adaptation |
| [Company routing](docs/company-routing.md) | Configured destinations and reviewed uncertain matches |
| [Drive filing](docs/codex-drive.md) | Documents, recordings, upload recovery, and optional OAuth |
| [Notes sharing](docs/notes-sharing.md) | Calendar suggestions and explicit sharing approval |
| [Updates and rollback](docs/updating-local-source.md) | Checkpoints, source replay, and safe updates |

## Data flow

Capture and transcription run locally. System audio includes anything played by
the Mac, including unrelated playback. Optional Codex notes send transcript text
to the model service used by the configured Codex login. Optional Drive filing
uploads approved outputs. Local inference does not make those completion stages
local; enable only the connections you intend to use.

A meeting normally begins under `~/Recordings/<yyyy.MM.dd-HHmm>/`. It contains
separate `mic.caf` and `system.caf` tracks, optional `screen.mp4`, session metadata,
and any generated transcript. Optional local naming changes the folder to
`Topic - Participants - M-D-YY`. Native Drive documents combine notes and the
transcript; recordings are separate files.

## Development and verification

Run the automated suite from the repository root:

```sh
sh scripts/test-workflow.sh
```

It runs Python and browser tests, Swift state-machine and native control checks,
and a release build. Tests use temporary files, fakes, and synthetic media; they
do not authorize real recording, transcription, uploads, or sharing. CI runs the
same checks with read-only repository permissions and no deployment step, except
for the two synthetic MP4 assembly cases. The hosted ARM VM crashes in Apple's
video scaler even with software encoding, so CI explicitly skips only that
part of the pause test. The default local suite runs both cases; they have passed
on the maintainer's Mac. CI still checks pause timing, audio cuts and transition
races. See [the workflow](.github/workflows/checks.yml) for the explicit setting.

The working installation has passed controlled capture, window-video, reminder,
private filing, fullscreen, Spaces, and Stage Manager checks. These results do
not verify every device or installation. Remaining live checks include physical
pause/resume, sleep/wake, output-device and monitor changes, remote-call A/V sync,
fresh login, a matching automatic calendar lookup, and speaker accuracy in a real
multi-person meeting.

New recordings use 16-bit PCM audio so an abrupt app exit does not require a
final compressed-audio packet table. On the next launch, Quill recovers readable
audio into local review; processing waits for approval. See
[crash recovery](docs/crash-recovery.md) for storage costs and limits.

## Relationship to upstream

This version starts from upstream commit
`855869e00b18bc9e7e6d171211780b439ce6ccd7`. System-audio recovery ideas were adapted
selectively while preserving this workflow's metadata and processing rules.
The October 23 review found upstream at
[`aad60f9`](https://github.com/humanitas-labs/quill/commit/aad60f98e680720dd24a30115580f226ae36c12c),
including a multi-platform directory reorganization. That reorganization has not
been merged here; this version remains macOS-only.

Updates are reviewed in a separate candidate and tested before installation.
There is no automatic merge, app update, or GitHub deployment. This source
release preserves upstream ancestry and omits private operational history.
