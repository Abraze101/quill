# Google Drive filing through Codex

The completion worker can file a completed recording and its notes under an approved company folder using the installed Codex tools and the configured Google Drive connections. Folder selection, file selection, retries, and receipts run in `quill_workflow/drive_upload.py`. Metadata reads, folder searches/listings, folder creation, and native document copying/editing use direct app-server requests. With the separate direct connection configured, new audio/video uploads of every size use reserved IDs and resumable transfers. Without it, files up to 512 MiB use one scoped Codex tool call so its runtime can transfer the local file. Quill's recording process stays separate.

Drive filing is optional and starts disabled. Configure a private Meetings root,
its category folders, and a notes format before enabling it. Each job saves its
settings and approved destination; later configuration changes do not rewrite
existing decisions.

## Native meeting-notes template

The user-selected Google Doc template supplies the fonts, spacing, heading styles, lists and section order. A separate reusable native copy has placeholders for the meeting date, title, identified participants, attachment description, Summary, Decisions, Next steps, Details and the full Transcript. Past-meeting facts, attendee chips, screenshots and Gemini feedback links are removed from that reusable copy. The original reference is unchanged.

For new jobs, set `publish.text_format` to `"native_template_google_doc_v1"`, `publish.template_document_id` to the prepared private template's ID, and `publish.template_sha256` to the value returned by `drive_upload.native_template_sha256` for its full native document read. Use the same text format on `archive` for media-only uploads. The template ID and hash are saved with each job. Changing a template requires an explicit new template revision; existing jobs retain their saved settings.

Publication copies the native template into the meeting's Notes folder and fills its styled paragraphs. It adds a native date chip and preserves list formatting. Notes omit repeated evidence annotations; the full transcript retains timestamps. Document titles follow `Topic - Participants - M/D/YY`. Local evidence JSON remains available for review. Plain participant names are used when the transcript establishes names without verified email addresses. The current adapter does not extract screenshots from recordings.

The worker records the returned copy ID before editing. An uncertain copy without an ID holds for review. An uncertain edit is reconciled against the saved copy and its expected text, date chip, paragraph styles and lists. Changed content or a changed template holds for review rather than replacing the document or creating another copy. Native template publication does not require a model or file-upload turn; notes generation and media transfer remain separate.

## Existing combined-document format

This supported format imports a rendered DOCX. To use it instead of the native
template above, add these settings to `~/.config/quill/workflow.json`, replacing the placeholder
with the raw ID of the private Meetings folder in My Drive. The example leaves
uploads disabled:

```json
{
  "archive": {
    "enabled": false,
    "type": "codex_drive",
    "text_format": "combined_google_doc_v1",
    "document_timezone": "America/Los_Angeles",
    "meetings_root_id": "REPLACE_WITH_REAL_MEETINGS_FOLDER_ID",
    "codex": "/Applications/Codex.app/Contents/Resources/codex",
    "timeout_seconds": 180
  },
  "publish": {
    "enabled": false,
    "type": "codex_drive",
    "text_format": "combined_google_doc_v1",
    "notes_layout": "clean",
    "document_timezone": "America/Los_Angeles",
    "meetings_root_id": "REPLACE_WITH_REAL_MEETINGS_FOLDER_ID",
    "codex": "/Applications/Codex.app/Contents/Resources/codex",
    "timeout_seconds": 180
  }
}
```

With `text_format: "combined_google_doc_v1"` on both stages, `archive` uploads audio and optional video. `publish` imports one native Google Doc containing the reviewed notes followed by the full transcript. JSON, Markdown, recording metadata, and separate transcript files stay local. Neither stage sends messages or grants access to other people. Both require a saved, enabled, succeeded `route` stage with an approval matching the current transcript and frozen company map. Configure [company routing](company-routing.md) and [Codex notes](codex-notes.md) too. A legacy job without a routing stage cannot use this adapter.

Set `publish.notes_layout` to `"clean"` to omit repeated source lines and timestamp citations from the notes. The full transcript keeps its timestamps, and local JSON retains the validated evidence. Omitted values or `"sources"` preserve the original annotated layout.

The format, notes layout, and date settings are saved with each job. Recordings made by the October 1 build or later save `recording_timezone` and the UTC offset at recording start. The queue freezes that timezone as `meeting_timezone` for local naming and both Drive stages. It controls the folder date, document title and native date chip, even when filing happens after travel. A conflicting timezone or offset stops processing for review.

For older recordings without this metadata, `document_timezone` retains its previous role as the document's IANA timezone; omitted values use UTC. Older recordings do not gain a guessed recording timezone. They retain their prior naming behavior, saved settings and journaled destinations. Jobs without a format retain their original JSON/Markdown inventory, including partially uploaded jobs. Changing configuration or releasing a held job does not migrate its format, notes layout or date settings. Correcting an already filed meeting requires a separate, explicit migration; it does not rerun a completed upload stage.

`codex` is optional and defaults to the bundled executable shown above. The optional `model` field selects a supported model for the file-upload call; omission uses the installed Codex default. Metadata, search, and folder creation do not start model turns. `timeout_seconds` bounds each individual connector call, with a default of 180 seconds, rather than the entire multi-file upload. The default connector path reuses the existing Codex authentication and Drive connection. The optional direct uploader for large media has its own Google OAuth credentials. Notes and company classification use their separately configured model runners.

Enabling configuration affects new jobs. Previously held stages stay held until explicitly released. Approving a company resumes its routing stage only; it does not release old recording or notes uploads. Changing a saved job's transcript, company map, upload files, or destination requires review or an explicit revision.

Each meeting uses this structure (the older combined format retains its original document title):

```text
Meetings/
  [approved company or General]/
    Meeting topic - Participant names - M/D/YY/
      Recordings/
        [microphone file named in meta.json]
        [system-audio file named in meta.json]
        [screen-video file named in meta.json, when present]
      Notes/
        Meeting topic - Participant names - M/D/YY  [native Google Doc]
```

New notes supply a short meeting topic and names of participants established by the transcript. Their evidence is validated before filing; a person mentioned in conversation is not automatically a participant. Unknown participant names are omitted. New recordings use the calendar date at recording start in their saved timezone. A meeting spanning midnight keeps its start date. New folder names put the topic first, then participants and the date, with no time: `Portal planning - Alex, Morgan - 9/14/26`. Titles are bounded to 160 UTF-8 bytes and folder names to 200 bytes, with control characters and path separators removed or replaced in the topic and participant names. The date retains its slashes in Drive; local folders use hyphens. A new recording with no validated title uses `Meeting - M/D/YY - JOB_ID`. Historical jobs without a saved recording timezone retain the earlier naming behavior and their journaled folder names.

The worker saves the chosen folder name in the meeting's journal before any Drive write. Recording and notes uploads, later retries, and timezone or title changes reuse that name and the saved folder ID. Existing Drive folders are not renamed. A different job that would use the same name stops for review instead of adopting the first meeting's folder. The job ID remains the internal retry identity even when it is absent from the displayed name.

The selected company's folder must be a direct child of the configured Meetings root. Recordings and Notes share one meeting folder; only a stage that runs creates its subfolder. Uploads use the queue's completed recording snapshot and accepted notes. The adapter selects explicit files instead of recursively copying directories. A combined document is rendered once to a stable DOCX under the job's results, then imported with `upload_mode: "native_google_docs"`. The DOCX is a local retry source, not an additional Drive deliverable.

The Codex file-transfer bridge has a 512 MiB (536,870,912-byte) limit per file in the source revision matching the installed build. The uploader checks the selected files before creating upload state or making Drive changes. By default, a larger file holds that stage for review; it is not silently omitted or split. The optional direct uploader below handles oversized audio and video. Text and document imports retain the existing limit. See the [version-matched Codex file limit](https://github.com/openai/codex/blob/fcbdb57851be70192fd0c21faa9e529146e93ff1/codex-rs/codex-api/src/files.rs#L18).

Before each mutation, the uploader checks the saved routing decision and the known Drive folder chain. Company, meeting, and stage folders must have their expected parent, name, and type. Every folder in the chain, including the root, must report `shared: false`, allow the current user to share, and expose exactly one user permission with the same owner. Shared-drive destinations and incomplete permission metadata stop the upload for review. The initial checks also require the root and company to appear in searches excluding trashed items. These are checks at the time of each operation; they cannot make subsequent external permission changes impossible.

The worker records each planned operation, dispatch, returned ID, and verified result in private journals under `~/.local/state/quill/workflow/results/JOB_ID/`. A per-job lock coordinates recording and notes uploads. Matching files and destinations reuse saved IDs. If creating a descriptive meeting folder may have succeeded but its Drive ID was not saved, the stage stops for review. One matching name is insufficient: it could belong to another job with the same time and title. A saved ID allows verification and retry without creating another folder.

For legacy meeting names containing the full job ID, and for raw files within a known meeting folder, a lost response can be reconciled by exact parent and name. One matching item can be verified; multiple matches or an unrecognized existing item require review. Native document imports require a saved returned Drive ID. If an import may have succeeded but that ID was lost, the stage holds for review instead of adopting a same-title document or importing another copy. An unresolved write is never blindly repeated.

Folder discovery uses paginated exact-name searches, bounded at ten pages. File discovery lists the direct contents of the saved folder and matches the exact filename locally, covering JSON and media that the search connector can omit. It does not fetch file contents or recurse into subfolders. This listing has a 100-item cap and no pagination parameter, so 100 returned items require review rather than an assumption that the result is complete. Invalid listings and incomplete or repeated folder-search tokens also stop progress.

Ordinary connector failures use a maximum of six attempts, with waits of 30, 60, 120, 240, and 480 seconds between attempts. Transient media-transfer failures with a saved, reserved Drive ID keep retrying beyond that limit, with backoff capped at one hour while Quill is running. A review-required failure is held immediately. Worker termination unwinds the connector process and releases locks; the next worker recovers the interrupted task and consults its journal. Repeated worker crashes retain their separate bounded recovery policy. Completed stages retain their receipts and do not upload again merely because the worker restarts.

Each receipt records local file sizes and SHA-256 hashes alongside Drive IDs, URLs when returned, and verification time. Raw-file readback checks the filename, parent, MIME type, size, and owner-only permissions. The connector's normalized metadata omits remote checksums, so raw-file receipts use `verification: "metadata"`; local hashes do not establish remote checksum equality. Native Google Docs have no meaningful DOCX byte-size equivalence. Their verification checks the native MIME type and permissions, then reads the full document and compares its text, in order, with every rendered paragraph after whitespace normalization. Missing notes or transcript text holds the stage for review. These receipts use `verification: "metadata_and_document_text"`. A reconciled response is marked in the file receipt. Cached completion receipts describe the verification performed at that time, not a fresh permission audit.

For metadata, document readback, search, direct folder listing, and folder creation, the transport starts `codex app-server` and uses a fixed JSON-RPC sequence: `initialize`, `initialized`, an ephemeral `thread/start`, then one `mcpServer/tool/call`. It sends the validated method and arguments directly to `codex_apps`, with no model turn. Returned request IDs and structured connector results are checked before an operation is accepted.

For connector file transfers, `codex_file_upload.py` starts an ephemeral `codex exec` run with exactly one Drive method enabled: `upload_file` for media or legacy files, or `import_document` for the combined DOCX. It supplies fixed arguments and a fixed `functions.exec` program that invokes that connector once. The normal Codex runtime stages the selected local file with OpenAI and passes an authentic file reference to Google Drive; a bare local path sent through direct app-server RPC does not perform that transfer. The upload model receives the fixed path, filename or document title, MIME type or conversion mode, and approved folder ID. It is instructed to execute that call without reading the file, choosing a destination, retrying, or running other work. File content passes through OpenAI's transfer service on its way to Drive.

The upload adapter accepts only a matching native tool event and successful completed turn, with the exact supplied arguments. A model's written success message is ignored. This argument check occurs after dispatch, so it can detect a changed upload argument after that request has already reached the connector. The method restriction does not enforce an exact destination before execution. Routing remains the worker's saved decision; the upload call has no authority to choose another company. Sharing and deletion methods are disabled. Both transport paths close their child processes on completion or failure, and uncertain writes return to the uploader's reconciliation process.

Automated tests cover synthetic uploads, retries, uncertain responses, duplicate
detection, folder moves, permission changes, routing guards and worker termination.
[Notes sharing](notes-sharing.md) is a separate optional review: it prepares a
private notes-only copy, then requires confirmation of that exact copy and
individual reader recipients.

[Company routing and review](company-routing.md) · [Recording integration](meeting-workflow.md)

## Direct upload and automatic media recovery

Jobs with `archive.large_files` configured use Google's resumable upload API for
all new audio/video uploads, including small microphone and system-audio tracks.
The setting keeps its original name for compatibility. Media travels directly
from the Mac to Drive in chunks of up to 8 MiB. Folder creation, native Google Docs
and legacy non-media files continue through the Codex connection. Existing jobs
retain their saved settings; a job without the direct connection does not gain it
automatically.

### Connect the owning Google account

This uploader requires its own Desktop OAuth client and consent. It cannot reuse the Codex connector's login. In a Google Cloud project, enable the Google Drive API, configure the OAuth consent screen, then create a **Desktop app** client under Google Auth platform → Clients and download its JSON. Keep that file outside the checkout. Follow Google's [Desktop OAuth setup](https://developers.google.com/identity/protocols/oauth2/native-app) and [credential creation instructions](https://developers.google.com/workspace/guides/create-credentials).

The setup requests `https://www.googleapis.com/auth/drive`, which permits reading, creating, changing and deleting Drive files. This scope is needed by this implementation to inspect the existing folders created through the other connection. The uploader itself only reads metadata and creates the approved recording files; it contains no sharing or deletion operation. Sign in as the owner of the existing Meetings folder. Setup verifies that account before saving credentials.

Run from the checkout, substituting the client path and exact owning email:

```sh
python3 integrations/meeting-workflow/drive-auth.py \
  --client /absolute/path/to/desktop-client.json \
  --credentials "$HOME/.config/quill/drive-upload-credentials.json" \
  --account owner@example.com \
  --no-open
```

Open the printed Google URL and complete consent while the setup command is running. The callback listens only on localhost and expires after five minutes. Credentials are written atomically with owner-only file permissions. Setup refuses to replace a different account unless `--replace-account` is supplied. Do not paste credentials, callback URLs or tokens into chats, model prompts or the repository.

Choose the OAuth consent-screen settings appropriate to your installation and
supply your own app-information and privacy-policy URLs when Google requires them.
Authorization can expire or be revoked; see Google's
[refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

After successful authorization, add this property to the existing `archive` settings in `~/.config/quill/workflow.json`, preserving all other properties:

```json
"large_files": {
  "type": "google_drive_resumable",
  "credentials_file": "~/.config/quill/drive-upload-credentials.json"
}
```

The local credential check must pass before new folders are created. Live access and account ownership are checked through Google's metadata responses before media transfer. Invalid authorization holds the job. Disabling Drive filing or changing this connection holds further direct requests. New jobs save the option with their settings; existing jobs do not gain it automatically. Releasing an old held job retains its saved configuration.

### Resume and verify

Before sending media, the worker reserves a Drive file ID and saves it in the existing private operation journal. It then saves the upload session and acknowledged offset. Retries check the exact file ID and ask Google for the current offset. Expired sessions reuse the reserved ID; a lost final response is reconciled against that same file. A same-name file with a different ID requires review.

Each chunk checks current routing/settings and the immediate parent folder's ownership and privacy. The existing connector checks the full folder chain before and after transfer. The source must remain unchanged. Completion verifies the remote size and MD5 against the source, as well as ID, name, parent, MIME type, ownership and private access. Direct media files receive `verification: "metadata_and_checksum"`; archive receipts containing them use `verification: "per_file"`.

The worker saves its transport choice before dispatch. An existing resumable
operation continues through the same connection regardless of size. Old
connector uploads with saved IDs keep their verification path; an old dispatched
write without an ID keeps its reconciliation guard. Neither is silently replaced
by a fresh direct upload. A missing or changed connection never triggers fallback
to a second upload path. Only new or definitely unstarted media operations can
choose the direct path.

Connection failures, rate limits and server errors during a transfer with a saved
reserved ID keep retrying automatically, with waits increasing to one hour.
Before ID reservation, ordinary failures retain the six-attempt limit. An
individual direct attempt is limited to 20 minutes; saved progress is retained
for the next attempt. Account, source, destination and privacy mismatches hold
the job for review. Folder writes and native document copies with unknown
outcomes still require their existing reconciliation or review; this change
does not claim automatic recovery for every Drive mutation.
Metadata/listing timeouts while resuming a saved media ID and transient OAuth
refresh failures also retain automatic retries. A generic connector failure
does not reliably distinguish an outage from a setup or approval problem, so it
keeps the ordinary six-attempt limit.

Local fault-injection tests cover small recordings and a sparse 513 MiB + 1 byte
source, partial transfers, expired sessions, process restart after a lost final
response, changed source files, revoked access, legacy journals and retries past
six attempts without a second file. These are simulated failures. They do not
establish how every real network outage, account change or device interruption
will behave.

Protocol references: [resumable uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads), [reserved file IDs](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds).
