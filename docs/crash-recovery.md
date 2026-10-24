# Recover an interrupted recording

New Mac recordings save microphone and system audio as 16-bit PCM inside the
existing `mic.caf` and `system.caf` files. Quill also keeps a private
`recording-in-progress.json` checkpoint while capture is active. A clean stop
removes that checkpoint after recording metadata and the review decision have
both been saved.

If Quill crashes or is force-quit, opening it again checks these new checkpoints.
When both audio tracks are readable, Quill rebuilds the meeting metadata and
shows the recording in its existing local review controls. Choose **Keep local**
or inspect the files before choosing **Process entire recording**. Recovery
itself never starts transcription, meeting notes, Drive filing, or sharing.
Existing configuration and folder-selection rules still apply after approval.

The recovery keeps the original meeting folder, identity, recording timezone,
saved processing choices, source offsets, pause duration, and audio warnings.
Recovered tracks are marked incomplete because a process crash may discard the
last buffered samples. Their usable duration comes from the files on disk, not
the time when Quill restarts. Reopening repeatedly does not create another
meeting or completion hook. An interrupted call remains suppressed from automatic
recording until its recovery review is resolved.

## What can be recovered

- The new PCM audio files can remain readable without a clean close. An actual
  forced-process-exit check retained the written audio, with about 37 milliseconds
  missing from one stereo buffer. This is an observed example, not a guaranteed
  maximum loss. Abrupt power loss and damaged storage remain separate risks.
- A missing or unreadable track leaves the surviving files local and the recording
  on hold. Quill does not invent a second track or send an incomplete pair through
  the existing two-track processing workflow.
- Screen video files are preserved. Recovery does not repair unfinished MP4 files
  or include them in the rebuilt meeting. The review explains that video needs
  separate inspection. Normal completed screen recordings keep their current
  saving and processing behavior.
- Historical recordings without a new checkpoint are left as they are. Cleanly
  completed older AAC audio remains supported. This change cannot reconstruct an
  old compressed recording whose packet table was never saved.

## Storage

PCM uses more disk space than the previous compressed audio. Microphone plus
system audio takes roughly **0.9–1 GB per hour**, depending on sample rates and
channel counts. Screen video is additional. Existing recordings are not converted.

## Verification

The recovery tests use temporary synthetic sessions and child processes. They
cover abrupt termination, readable media, startup ownership locks, repeated
recovery, damaged checkpoints, mismatched identities, linked files, deletion,
partial tracks, completed outputs, and persisted review decisions. App tests
exercise capture, Pause/Resume, Stop, sleep handling and checkpoint failures.
Python workflow checks confirm that recovered meetings cannot reach processing
or Drive while held, and approval retains existing duplicate-prevention rules.

This adapts the recovery approach in
[upstream proposal #71](https://github.com/humanitas-labs/quill/pull/71) to this
version's Mac capture and review workflow. It does not adopt the upstream
multi-platform layout.
