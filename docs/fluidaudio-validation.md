# FluidAudio 0.17.5 validation

Checked October 24, 2026. Quill pins FluidAudio exactly to **0.17.5** at
`0b1f46289fe27d95b5e66ad8be46e64f5ee02ae7`. Swift Argument Parser stays at 1.8.2.
The transcription model remains Parakeet v2, and Quill's speaker alignment,
anonymous-speaker fallback and name-evidence rules are unchanged.

## Model compatibility

A 74.93-second synthetic meeting used two macOS speech voices and 210 reference
words. It spans multiple 15-second model windows. Audio was written directly to
a file; this check used no microphone, real recording, completion hook or upload.
Separate executables linked the actual 0.15.5 and 0.17.5 libraries and exercised
Quill's production word-to-speaker alignment.

| Check | 0.15.5 | 0.17.5 |
| --- | --- | --- |
| Recognized text | Same text; one reference-word error | Identical |
| Word timings | 209 valid entries, ending at 74.64 seconds | Identical |
| Plain transcript segments | 19 | 19 |
| Anonymous speaker clusters | 2 across 3 intervals | Same |
| Aligned transcript segments | 20; one uncertain boundary left anonymous | Identical |
| ASR plus cached model loading | 12.29 seconds | 11.57 seconds |

These are single-run compatibility measurements, not a performance benchmark.
The synthetic sample did not reproduce the scrambled-word issue; it established
that the upgrade preserved this transcript and its speaker assignments.

Four direct library checks also passed: preserve token order across conflicting
chunk timestamps, retain unrelated shared-prefix words, deduplicate true acoustic
overlap, and recognize v2 punctuation IDs. The token-order fixture demonstrates
that the previous global timestamp sort would corrupt that sequence.

## First load and rollback

The new library refreshed 24 public diarization model files in FluidAudio's cache.
The first diarization run took 24.03 seconds including that download; a later
cached run took 0.56 seconds. Parakeet v2 reused its existing cache. A first run on
another Mac may therefore need network access and more time even with previously
cached models.

The old 0.15.5 library was tested again after the cache refresh and returned the
same speaker-aligned transcript. This checks rollback compatibility for the tested
model cache on this Mac; it does not guarantee compatibility with future model
revisions.

Both versions emitted the same CoreML zero-shape diagnostic after returning the
complete transcript. Both reported an ASR duration field of zero, while their
word spans covered the speech. Quill uses those word spans in this path.

## Build and packaging

The full candidate app built with `sh scripts/swift-local.sh build --product quill`.
No changes to `ParakeetEngine` were required. A relocated validation executable
passed real ASR and diarization with the dependency's build-time resource bundle
unavailable, confirming that these paths did not fall back to checkout resources.

NemoTextProcessing is statically linked; the executable links only system dynamic
libraries. FluidAudio also adds a roughly 1 MB resource bundle used by LuxTTS,
which Quill does not invoke. A future binary distribution should include dependency
resources and notices as part of its packaging review. See
[third-party notices](third-party-notices.md).

This validation does not replace the recording workflow suite or live checks with
real meetings, hardware changes, pauses, sleep and wake. No installed app or
runtime configuration was changed by these model checks.

## Optimized release build

A separate October 24 build used
`sh scripts/swift-local.sh build -c release --product quill` and passed in
100.46 seconds. The release executable was 21,906,552 bytes, compared with
33,775,520 bytes for the preceding debug validation build. These sizes describe
the executables alone, excluding app resources, downloaded model weights and
packaging; a new signature or later source change can alter them.

The same synthetic fixture was processed by an optimized harness linked against
the release FluidAudio objects and Quill's production speaker alignment. Its
recognized text, all 209 word timings and all 20 speaker-aligned segments matched
the saved debug results exactly. A relocated copy of the actual release `quill`
executable also ran `diarize-local` as a child process and produced the same two
speakers across three intervals. It used a private synthetic session and output
directory, without starting the app daemon or completion workflow.

Observed elapsed times were 12.57 seconds for ASR plus model loading, 0.78 seconds
for standalone diarization and 0.89 seconds for the native diarization child.
The existing public model cache was reused; no cache files changed during these
checks. These single measurements include load and cache effects and do not
establish a speed improvement. The checks cover optimized processing, not live
capture, UI behavior or recording hardware.
