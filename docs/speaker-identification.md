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

Names appear when a separated voice gives a complete, direct introduction, such
as “My name is Jamie.” or “Hi, I'm Alex.” The same name labels later speech from
that voice in the local transcript and combined Google Doc. The notes runner can
use that attribution for an action owner, with evidence retained in its local
JSON. These are names stated in the recording, not independently verified
identities.

An uncertain name stays as **Microphone speaker 1** or **Other speaker 1**.
Overlapping speech or words without a reliable speaker match keep an unidentified
audio-source label. Conflicting introductions, the same name on multiple voice
clusters, and nearby quotation, role-play or correction cues prevent automatic
naming. Calendar attendees and people merely mentioned in conversation do not
establish who spoke. There is no persistent voiceprint directory, named-caption
capture, or automatic recognition of people from previous meetings.

The first use downloads the model files into FluidAudio's local cache. Audio is
processed locally; the existing Codex notes runner receives transcript text and
validated name evidence. If speaker detection fails, the transcript still saves
with source labels and an unavailable/partial status. Unknown names do not block
notes or the existing folder policy.

## Cancellation and saved files

Speaker detection runs in an isolated child of Quill. Delete or cancellation
stops and reaps that child before the session's file lease is released. A child
also stops if its parent disappears or the meeting is marked for deletion. The
child does not initialize recording devices or the application interface.

New opted-in transcripts declare `speaker_labels: "diarized_v1"`, keep the audio
source on each segment, and store speaker labels and self-introduction evidence
in a `speakers` map. Names and evidence are validated again before notes or
document rendering. Existing `audio_source` transcripts retain their previous
behavior and rendering so queued uploads can reconcile unchanged payloads.

## Verification scope

Automated checks cover word-boundary changes, competing voices, insufficient
coverage, names and conflicting evidence, cancellation, process output checks,
legacy transcripts, and document/notes use of attributed names. Generated speech
can exercise the real model without accessing a meeting. Accuracy in an actual
multi-person meeting still needs a listening review; synthetic checks do not
establish real-world speaker or name accuracy.

The implementation uses the pinned FluidAudio dependency. Its
[offline diarization documentation](https://github.com/FluidInference/FluidAudio/blob/19600a485baa4998812e4654b70d2bab8f2c9949/README.md#speaker-diarization)
describes the local segmentation and clustering pipeline.
