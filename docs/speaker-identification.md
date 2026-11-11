# Speaker labels and names

Settings → **Identify speakers locally** separates voices after a recording
stops, before meeting notes are generated. It requires local transcription. The
setting is saved with each new recording; enabling it does not reprocess older
recordings or change existing Drive documents.

Quill runs FluidAudio's offline speaker model on this Mac. It assigns words to
speaker turns on each audio track, then combines the timed transcript. A shared
microphone can contain several speakers, and system audio can contain several
remote participants. Microphone and system speakers have separate IDs. Quill
does not assume one microphone equals one person or merge voices across tracks.

For a Mac used by one person on remote calls, set
`transcription.remote_microphone_owner` in the native Quill configuration to
that person's display name. Quill saves it with the recording when capture
starts during a detected Google Meet or browser Zoom call. Microphone speech
uses that name, including turns that speaker separation split or left unassigned.
A different identified local speaker keeps their name, and ambiguous microphone
turns in that recording keep their source labels. A manual recording started
outside a detected call uses the usual speaker labels.

For older recordings, the local meeting view can apply the configured name when
the saved Meet or Zoom evidence identifies that person in the call and no self
badge conflicts. This updates the displayed transcript and participant list.
Original recordings, filed documents and upload receipts retain their saved contents.

New captures use `speaker_name_policy: "explicit_intro_v3"`. It accepts direct
introductions such as “My name is Jamie” and “Hi, I'm Alex,” including a bounded
set of natural lead-ins such as “Well, yeah, my name is Jamie” or “It doesn't show
my name here, but my name is Jamie.” It can join up to three adjacent, timed
fragments from the same voice. It does not search arbitrary stories for a name.
Bare “I'm Jamie,” quotations, role-play, conflicting introductions and nearby
corrections do not establish a confirmed name. V2 and earlier saved transcripts
keep their original parsing policy.

A voice without a supported name stays as **Microphone speaker 1** or **Other speaker 1**.
Overlapping speech or words without a reliable speaker match keep an unidentified
audio-source label. A name stated in the recording is not an independently verified
identity. Calendar attendance, a browser's self badge and microphone/system source
alone do not identify a voice.

## Focused review and corrections

For new diarized meetings using the configured Codex notes adapter, a separate
speaker review runs before notes generation. It must account for every separated
voice with a `confirmed`, `matched`, `suggested` or `unresolved` result, supporting references
and a short reason. It considers introductions, direct address, replies and saved
visible participant names. A strongly matched calendar invitation can supply
names and email candidates through the explicitly connected Google account.
Ambiguous invitations supply no automatic identities; an unavailable calendar
does not block the remaining evidence review.

### Live Meet speaking activity

The Chrome extension also collects visible Google Meet participant names and
their speaking-indicator changes while joined. It sends bounded samples to the
local app; it does not read captions, chat, audio, or profile photos. The app
keeps only evidence belonging to the recording's meeting/tab and writes a
`speaker-activity.json` sidecar. Its timestamps follow the audio's startup offset
and pause cuts. Gaps, reloads, conflicting names, and clock jumps break continuity
rather than extending the last speaker across missing time.

The review compares this timeline with separated speech. Automatic remote naming
requires at least three distinct turns, three seconds of supporting activity,
80% coverage of the voice's speech, and 95% support for the same participant.
Overlapping speech, duplicate display names, multiple voices on one device, or
truncated evidence prevent an automatic match. The validator repeats this check;
the model cannot claim a platform match without the saved evidence. Clear spoken
introductions and user corrections take precedence.

An explicit **You** label is retained as the local meeting identity. The Speakers
tab shows it separately from voice names. This identifies the signed-in meeting
participant, not every voice on a shared microphone. Being the calendar organizer
or owning the connected Drive account does not substitute for speaking evidence.
Self-device timing can suggest a name but does not automatically name shared-mic
voices. Remembered corrections save contact spelling, not voiceprints.

This collector supports the verified Google Meet interface. Zoom browser meetings
keep existing participant/context and introduction-based naming; live Zoom
speaking-indicator extraction is not enabled. Missing or changed interface
markers contribute no timing evidence. Capturing **You** currently requires that
Meet expose the self label in its visible People panel at least once during the
page's lifetime. Evidence coverage may also be limited by the visible layout.

Older recordings without a speaking timeline retain their existing evidence;
uploading them to Drive cannot reconstruct missing speaker timing. Invalid or
missing sidecars do not block notes or filing.

Contextual matches remain **Jamie (suggested)**. A suggestion alone cannot become
a definite action owner or participant. An invitation with only an email remains
an email-only candidate: Quill does not turn the address into a name. The Speakers
tab lets the user choose that address and confirm its association with a name.

Open a meeting's **Speakers** tab to see voice IDs, matched, suggested or confirmed names,
reasons and short speech samples. **Save correction** confirms a name and optional
email for that voice; **Clear correction** removes the manual override. The user
can explicitly assign one person to multiple voice IDs. Unseparated `unknown`
segments cannot be assigned as a single person.

Save and Clear update the reviewed local transcript and start a local notes
refresh through the configured Codex runner. The panel reports progress or offers
**Retry local notes** if the refresh fails or becomes stale. Original transcript
JSON, audio, speaker IDs, words and timestamps remain unchanged. Existing Drive
documents and sharing permissions remain unchanged too; corrections do not
republish or share them.

**Remember name and email** is optional and requires an email. It saves confirmed
spelling and aliases for future suggestions when the next meeting's evidence
matches. It does not learn a voiceprint or recognize the person by sound.
**Forget for future meetings** removes that saved identity while preserving the
current meeting's correction.

The first use downloads the model files into FluidAudio's local cache. Audio is
processed locally. The Codex review and notes runners send the selected transcript
and supplied identity evidence, including invitation candidates, to their hosted
model using the saved ChatGPT login. If local speaker detection fails, the
transcript still saves with source labels and an unavailable/partial status.
Unresolved names do not block notes or the existing folder policy.

## Cancellation and saved files

Speaker detection runs in an isolated child of Quill. Delete or cancellation
stops and reaps that child before the session's file lease is released. A child
also stops if its parent disappears or the meeting is marked for deletion. The
child does not initialize recording devices or the application interface.

New opted-in transcripts declare `speaker_labels: "diarized_v1"`, keep the audio
source on each segment, and store speaker labels and self-introduction evidence
in a `speakers` map. Names and evidence are validated again before rendering.
New focused notes carry a caller-validated `_speaker_identity` bundle containing
the evidence context, source-bound review and explicit corrections. The model
cannot supply this trusted envelope or overwrite it through legacy assignments.
Local corrections and refreshed notes live under `results/<job>/speakers/`;
the review UI uses them only while their source bindings match. Existing notes
with a legacy `speaker_review`, V2 transcripts and `audio_source` transcripts keep
their compatible rendering. A new focused review can apply the current
introduction checks as a derived mapping without changing the saved policy.
Stored filing receipts and accepted Drive payloads
are preserved for retry reconciliation.

## Verification scope

Synthetic fixtures cover the shared Swift/Python introduction policies, competing
voices, source-bound proposals, email-only candidates, manual corrections,
reversible identity memory, notes refresh, cancellation and legacy compatibility.
Timeline fixtures cover pause/startup alignment, shared devices, duplicate names,
overlap, stale observations, malformed optional payloads, queue snapshots and
bounded input handling. The current Meet marker was checked in an empty live call
while muted, speaking and silent. A full multi-person recording still needs a
listening check against the resulting transcript.
Fake model processes exercise the runner and validation boundaries without using
real meetings. These checks do not establish diarization or naming accuracy in
actual calls; that still needs a listening review against the recorded speech.

The implementation uses the pinned FluidAudio dependency. Its
[offline diarization documentation](https://github.com/FluidInference/FluidAudio/blob/19600a485baa4998812e4654b70d2bab8f2c9949/README.md#speaker-diarization)
describes the local segmentation and clustering pipeline.
