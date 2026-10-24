# Codex notes runner

The completion worker can apply the existing Anthropic Call Summary instructions through a locally installed Codex CLI and its saved ChatGPT login. No separate model-provider API key or client is required. Codex still sends the supplied transcript to its hosted model; Quill's audio transcription runs on the Mac.

The adapter is `integrations/meeting-workflow/quill_workflow/codex_notes.py`. It receives one transcript, the skill file, and an output path. It passes their contents to Codex, validates the returned JSON and transcript references, and writes the result atomically. The surrounding completion worker renders Markdown with Summary, Decisions, Next steps and Details, following the user-selected meeting-notes template. Actions include owners and stated deadlines; discussion topics, concerns, assessments and unresolved questions remain in Details. Repeated inline citations and timestamps are omitted, with evidence kept in the local JSON. A repeat with matching input and settings reuses the accepted result; exact legacy, earlier clean and template Markdown renderings remain valid for review and filing.

Writing guidance starts with the meeting's outcome or focus, followed by decisions,
concrete tasks with verified owners, and compact topic details that preserve useful
numbers and reasons. Conditions and uncertainty remain explicit; advice and
forecasts stay attributed. A configured native template controls document formatting.
Changes to this guidance apply to newly generated notes; accepted notes are not
regenerated automatically.

New transcripts can include `capture_quality` with status `incomplete` and the
affected sources, `mic` or `system`, based on recorded capture metadata. The
adapter validates those fields before calling Codex and instructs the model to
use only supplied speech, without inventing missing content or estimating gaps.
The exporter adds this fixed notice once per artifact:

> Some audio was interrupted or could not be captured. The transcript and notes may be incomplete.

The notice appears in local notes, combined DOCX and native Google Docs, and
notes-only sharing previews and copies. Combined documents carry one notice for
both the notes and transcript. Raw device errors and interruption timestamps
stay out of the notice, and model-authored notes cannot repeat the fixed text.
Clean and legacy transcripts without `capture_quality` retain their existing
renderings. This change does not reprocess old meetings, alter accepted files,
release filing holds or grant sharing approval.

New notes include a short meeting topic and participant names established in the transcript. The notes heading combines the topic with those names, such as `Project planning - Alex`. New Drive folder names append the recording's local date, with no time: `Project planning - Alex - 10/1/26`. Local folders use the same order with hyphens in the date. The model must distinguish people on the call from people merely mentioned; `me`, `them`, speaker numbers, and guessed surnames are excluded. If no attendee name is established, the title uses the topic alone.

With local speaker identification enabled, new transcripts use
`speaker_labels: "diarized_v1"`. Each separated voice has a source-scoped ID,
such as `mic_speaker_1` or `system_speaker_1`. Unnamed voices appear as
**Microphone speaker 1** or **Other speaker 1**. Speech that cannot be attributed
keeps **Microphone (unidentified)** or **System audio (unidentified)**; it is
never assigned to a named voice by proximity.

A clear self-introduction can supply a name for that voice. The Python runner
rechecks the exact introduction text, its speaker and source, and its segment
indices before using the name. Conflicting introductions, the same name on
different voices, and nearby quotation, roleplay or correction cues prevent
attribution. A stated name is not an independently verified identity. The notes
model receives only the names that pass these checks, and both document formats
reuse them for later segments from the same speaker ID.

A named action may cite a later commitment from that separated speaker. Without
that validated mapping, the owner must have their name in the cited transcript
text. Unknown owners stay unassigned, and numbered speaker labels never become
owners. Calendar attendees and browser meeting controls do not establish who
spoke.

Older transcripts and recordings with only audio-source labels retain their
existing **Microphone** and **System audio** labels. A shared source may contain
several people or unrelated playback. Historical document payloads remain
unchanged so pending Drive writes can still be reconciled; existing recordings
are not reprocessed automatically.

The optional-on-read `meeting_title` object contains `text`, topic `evidence`, and `participants`, each with `name` and `evidence`. New Codex output requires this object; historical notes remain valid without it. Names must occur in their cited transcript segments, and anonymous labels, invalid references, and duplicate names are rejected. These checks validate spelling and references; they do not independently establish attendance, which still needs review. The shared title helper removes path separators and control characters, limits the result to 160 UTF-8 bytes, and keeps complete participant names. Long attendee lists are shortened in the title; the notes retain all listed names, with evidence kept in the local JSON. Existing Drive folders keep their saved names across retries.

If a completed Codex run returns invalid JSON, schema fields, names, or evidence, the adapter asks for one correction. It supplies the same transcript and skill, the rejected draft, and a bounded validation error. The instructions require supported content to be preserved and unsupported names to be omitted; they prohibit changing the transcript or inventing evidence. The corrected result must pass the same validation. A valid first draft is saved unchanged. A second invalid result fails with its validation reason, without publishing notes.

The following setup example leaves notes disabled until the runner is configured:

```json
{
  "notes": {
    "enabled": false,
    "command": [
      "/usr/bin/python3",
      "/absolute/path/to/quill/integrations/meeting-workflow/quill_workflow/codex_notes.py",
      "--input", "{input}",
      "--output", "{output}",
      "--skill", "{skill}",
      "--codex", "/Applications/Codex.app/Contents/Resources/codex",
      "--timeout", "900"
    ],
    "timeout_seconds": 960
  }
}
```

Use an executable path that works with a background app's minimal PATH. The adapter
accepts an explicit `--model` when the installed CLI supports the desired model.
It ignores unrelated user configuration while retaining the existing authentication
location. Verify the installed CLI's supported flags before changing versions.

Keep the outer connector timeout longer than the adapter timeout so the adapter can stop its child processes cleanly. Generation and its optional correction share one timeout budget. Missing executables, rejected configuration, failed CLI runs (including authentication failures), timeouts, and missing or unreadable answer files fail without a correction call. Empty transcripts never start Codex. Unidentified owners must be null; audio-track labels such as `me` and `them` are not people.

The adapter uses a private temporary directory and an ephemeral Codex run for each attempt. Each attempt has a new answer path, and its process group is stopped before the answer is read. A missing correction cannot reuse an earlier draft. The adapter disables shell execution, MCP discovery, browsers, apps, plugins, hooks, memory, and other unnecessary integrations. Its instructions prohibit tool use and treat spoken instructions, rejected drafts, and validation diagnostics as untrusted data. The tested CLI did not provide a supported switch to disable `view_image`; do not describe the runner as having no tools whatsoever. Rejected drafts and process logs are not copied into the user's recording directory.

Run focused checks with:

```sh
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p test_codex_notes.py -v
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p test_capture_quality.py -v
```

Enabling notes affects future completed sessions. Existing held jobs require their own explicit release. Creating Drive folders does not enable uploads, classify a meeting, or grant sharing permission. Company routing and attendee review belong in separate completion-worker stages.

[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) documents saved-login reuse, structured output, and writing the final response to a file. Keep the pinned Call Summary reference separate from this runner so either can be updated independently.
