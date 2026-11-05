# Codex notes runner

The completion worker can apply the existing Anthropic Call Summary instructions through a locally installed Codex CLI and its saved ChatGPT login. No separate model-provider API key or client is required. Codex still sends the supplied transcript to its hosted model; Quill's audio transcription runs on the Mac.

The adapter is `integrations/meeting-workflow/quill_workflow/codex_notes.py`. It receives one transcript, the skill file, an output path, and optional meeting-name context or a validated speaker-identity bundle. It passes their contents to Codex, validates the returned JSON and transcript references, and writes the result atomically. The surrounding completion worker renders Markdown with Summary, Decisions, Next steps and Details, following the user-selected meeting-notes template. Actions include owners and stated deadlines; discussion topics, concerns, assessments and unresolved questions remain in Details. Repeated inline citations and timestamps are omitted, with evidence kept in the local JSON. A repeat with matching input, context and settings reuses the accepted result; exact legacy, earlier clean and template Markdown renderings remain valid for review and filing.

Writing guidance starts with the meeting's outcome or focus, followed by decisions,
concrete tasks with verified owners, and compact topic details that preserve useful
numbers and reasons. Conditions and uncertainty remain explicit; advice and
forecasts stay attributed. A configured native template controls document formatting.
Changes to this guidance apply to newly generated notes. Accepted notes are not
regenerated merely because the guidance changes; saving or clearing a speaker
correction starts a separate refresh of the local reviewed notes.

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

A direct introduction can supply a name for that voice. New captures mark
`speaker_name_policy: "explicit_intro_v3"`, which adds bounded natural lead-ins
such as “Well, yeah, my name is Jamie.” The policy retains the checks for quoted
speech, conflicting names and explicit corrections, and can join up to three
adjacent timed fragments from the same voice. Bare “I'm Jamie” remains tentative.
Swift and Python use shared synthetic policy fixtures. V2 and earlier captures
keep their original parsing policy; a stated name is not a verified identity.

For new diarized meetings, the completion worker recognizes the built-in
`codex_notes.py` adapter and supplies `--speaker-context`. Before writing notes,
`speaker_review.py` makes a focused Codex call that must return one result per
separated voice, including unresolved voices. Each result has a short reason,
evidence references and status `confirmed`, `suggested` or `unresolved`. The
context contains saved visible names, candidates from a strongly matched calendar
invitation and relevant remembered corrections. Calendar lookup uses the selected
Google account. An unavailable or ambiguous invitation does not establish names.

Invitation names and emails are candidate evidence, not proof of who spoke.
Email-only attendees remain email-only choices in the Speakers tab; Quill never
derives a name from an email handle. A shared microphone can contain several
people, visible attendance can be incomplete, and two names alone cannot identify
two voices. Suggestions stay visibly **Jamie (suggested)** and cannot become
definite owners or participants without independent evidence or user confirmation.

The caller validates the focused report against the transcript and evidence
context, then attaches it to notes as `_speaker_identity`. This bundle contains
`context`, `report` and `corrections`, with transcript/context hashes binding its
contents. The notes model receives the validated bundle, must leave its legacy
`speaker_review.assignments` empty and cannot return `_speaker_identity` itself.
The bundle and confirmed names are preserved if the notes draft needs a correction
attempt. Direct or custom adapters without the new context retain their existing
contract; historical `speaker_review` mappings remain readable.

In the **Speakers** tab, **Save correction** and **Clear correction** update the
reviewed local transcript and start a separate local notes refresh. The refresh
passes the validated bundle through `--speaker-identity`, so it does not repeat
the identity review or discard explicit corrections. Results are stored under
`results/<job>/speakers/` and displayed only while their source bindings remain
current. The panel shows progress and a retry action for failed or stale notes.
Original speech, timestamps, speaker IDs and transcript JSON remain unchanged.
Existing Drive documents, filing receipts and sharing permissions also remain
unchanged; this refresh does not republish them.

**Remember name and email** optionally saves confirmed spelling and aliases for
future suggestions supported by that meeting's evidence. It requires an email and
can be reversed with **Forget for future meetings**, without clearing the current
meeting's correction. This is an identity list, not voiceprint training.

A named action may cite a later commitment from that separated speaker. Without
that validated mapping, the owner must have their name in the cited transcript
text. A suggested mapping alone cannot establish an action owner or a definite
participant. Unknown owners stay unassigned, and numbered speaker labels never
become owners. Speaker naming does not choose recipients or authorize sharing.

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
      "--context", "{context}",
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

Keep the outer connector timeout longer than the adapter timeout so the adapter can stop its child processes cleanly. Focused speaker review, notes generation and their correction attempts share one adapter timeout budget. Missing executables, rejected configuration, failed CLI runs (including authentication failures), timeouts, and missing or unreadable answer files fail without a correction call. Empty transcripts never start Codex. Unidentified owners must be null; audio-track labels such as `me` and `them` are not people.

The adapter uses a private temporary directory and an ephemeral Codex run for each attempt. Each attempt has a new answer path, and its process group is stopped before the answer is read. A missing correction cannot reuse an earlier draft. The adapter disables shell execution, MCP discovery, browsers, apps, plugins, hooks, memory, and other unnecessary integrations. Its instructions prohibit tool use and treat spoken instructions, rejected drafts, and validation diagnostics as untrusted data. The tested CLI did not provide a supported switch to disable `view_image`; do not describe the runner as having no tools whatsoever. Rejected drafts and process logs are not copied into the user's recording directory.

Run focused checks with:

```sh
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p test_codex_notes.py -v
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p test_capture_quality.py -v
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p 'test_speaker_*.py' -v
/usr/bin/python3 -m unittest discover -s integrations/meeting-workflow/tests -p test_focused_speaker_review.py -v
```

The speaker fixtures and fake model processes validate policy and integration behavior; they do not establish accuracy on real speech. Review recorded audio against the labels before treating live naming accuracy as verified.

Enabling notes affects future completed sessions. Existing held jobs require their own explicit release. Creating Drive folders does not enable uploads, classify a meeting, or grant sharing permission. Company routing and attendee review belong in separate completion-worker stages.

[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) documents saved-login reuse, structured output, and writing the final response to a file. Keep the pinned Call Summary reference separate from this runner so either can be updated independently.
