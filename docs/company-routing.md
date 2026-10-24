# Meeting folders and review

Configure categories for the companies or projects you want to file under, with an
optional General category. Notes generation, category suggestions and Drive filing
start disabled until configured. Folder review is the default. The optional
explicit-company policy below lets a new meeting file automatically when its
transcript directly establishes the destination. [Notes sharing](notes-sharing.md)
always requires separate approval after private filing.

The completion worker can ask the authenticated Codex CLI to suggest a filing category for a completed meeting. Categories can represent companies or General. The model receives the transcript and configured category names, descriptions, and aliases. It does not receive Drive folder IDs. The worker resolves a category against a fixed folder list and saves a durable review. Quill only displays the pending count and opens the review page. Internal configuration and CLI fields retain their existing company names for compatibility.

New jobs with a `route` configuration run notes, then routing, then recording/notes uploads. The older three-stage jobs retain their original stages and holds. Declaring or enabling routing never migrates or releases those jobs.

For a new installation, add a `route` object to `~/.config/quill/workflow.json` after supplying real folder IDs. This example leaves routing disabled:

```json
{
  "route": {
    "enabled": false,
    "review_required": true,
    "auto_file_explicit_companies": false,
    "companies": [
      {
        "id": "example-company",
        "name": "Example company",
        "drive_folder_id": "REPLACE_WITH_REAL_FOLDER_ID",
        "description": "Describe the work owned by this company.",
        "aliases": []
      }
    ],
    "command": [
      "/usr/bin/python3",
      "/absolute/path/to/quill/integrations/meeting-workflow/quill_workflow/codex_routing.py",
      "--input", "{input}", "--companies", "{companies}",
      "--output", "{output}",
      "--codex", "/Applications/Codex.app/Contents/Resources/codex",
      "--timeout", "900"
    ],
    "timeout_seconds": 960
  }
}
```

Both notes and route must be enabled for new jobs to reach a routing decision. Start with `review_required: true`. A company mentioned as a customer, competitor, or example is insufficient ownership evidence. Missing descriptions require explicit company ownership in the transcript. Unclear and multi-company meetings wait for a choice even if automatic approval is later enabled.

To file clear company meetings automatically, set `auto_file_explicit_companies: true` and keep `review_required: true`. New Codex suggestions include a verbatim transcript segment identifying ownership or an explicit filing request. The worker requires high confidence, one allowed company, an exact full-segment quote, and affirmative wording such as “This meeting is for Example company” or “Please file this meeting under Example company.” Configured aliases resolve only to their existing category. The model never chooses a Drive folder ID.

Each company can have an optional `aliases` list for spelling variants found in
transcripts. For example, a fictional company named `Acme Studio` might configure
`"aliases": ["Ackme Studio"]`. There are no built-in company aliases. Supply at most
20 names per company, each one to 200 characters before and after case folding,
with no surrounding whitespace or control characters. Matching ignores case.
Repeated aliases and aliases equal to the company's name are normalized away.
An omitted or empty list preserves the
existing map format. If an alias matches more than one category, the meeting stays
in review.

The worker checks the surrounding segments and competing ownership statements. Negations, hypothetical or quoted speech, customer/vendor references, model-directed instructions, ambiguous names, and unsupported phrasing stay in review. General also stays in review. A high-confidence contextual guess alone cannot bypass review in this mode. The saved decision retains the exact supporting quote and marks its source as `automatic_explicit`. This policy is intentionally conservative: a clear meeting may still need a folder choice when its transcript lacks a supported direct statement.

The setting is frozen when a meeting enters the queue. Enabling it affects future jobs; it does not release existing reviews, reclassify older meetings, or change a previous choice. Schema-version-1 suggestions remain valid for review. The older `review_required: false` broad high-confidence policy remains compatible when explicit-company mode is off; enabling explicit-company mode always applies the stricter evidence check. Automatic private filing never approves attendee sharing.

An optional entry with `id: "general"` and `name: "General"` supports personal, learning, networking and broader planning meetings that do not belong to one company. Give it its own Drive folder ID and a description of that scope. Codex requires transcript evidence for the broader purpose. It must still abstain when context is missing or ownership is unclear; General is not an automatic fallback. A company that clearly owns the work takes precedence. General uses the same meeting subfolders, notes/transcript document, recording archive and approval flow as the company folders.

The company list and review policy are frozen per job. A decision is tied to the exact transcript bytes, folder map, and displayed revision. Adding a category or alias to the live configuration is compatible with older jobs when every original ID, name, destination, description, and alias remains unchanged. Older jobs retain their original map, choices, review revisions and receipts; new categories and aliases apply only to newly queued meetings. Removing or replacing a frozen alias, changing the transcript, or editing/removing an original category requires an explicit new revision and review. There is no automatic migration command. Upload adapters receive only the validated `{route}` file and `{drive_folder_id}` value after routing succeeds. They must still verify Drive access and their completed uploads.

Add the optional menu command to Quill's `config.json`:

```json
{
  "meeting_review": [
    "/usr/bin/python3",
    "/absolute/path/to/quill/integrations/meeting-workflow/workflow.py",
    "review"
  ]
}
```

The worker uses `~/.local/state/quill/workflow` by default. Custom state/config paths must also be passed to the review command. Quill can read alternate locations through `completion_status_file` and `workflow_config_file`. `status.json` includes `pending_reviews`; it contains counts rather than meeting text.

**Review meetings…** opens a temporary local browser page. Select a meeting from the list to read its formatted notes in the center and see its current status and available actions in the right pane. Folder reviews offer the suggested folder, **Use this folder**, and **Keep local**. The same screen includes processing retries and a separate [notes-only sharing review](notes-sharing.md) for completed native Google Docs.

**Delete local meeting…** opens a separate confirmation for the entire local meeting folder: audio, optional video, transcript and notes. It also works for accidental or empty recordings before notes are ready, including pending or failed processing. Confirming stops further work for that meeting and moves its folder and queued copies to macOS Trash. If processing is running, **Stopping processing…** stays visible until its files are closed; removal then finishes automatically. Other meetings continue normally. Active recording must be stopped first. Older recordings without Quill’s processing lease marker wait for a requested transcript; unsupported files show a reason instead of offering deletion.

A durable deletion record stops further processing, retries and sharing for every queue job tied to that recording. Existing Drive files, shared copies and permissions stay unchanged. Original task receipts, upload journals and removal receipts remain available for audit, including any cloud operation whose result was uncertain when stopped. Restoring files from Trash does not re-enable processing. If a Trash move stops partway through, the meeting shows **Removal needs attention** and offers **Retry local removal…**; the files remain recoverable and processing stays stopped. Recovery wrappers in Trash are named `Quill - Recording - …` or `Quill - Processing copies - …` and contain the original named folders.

Closing the page preserves pending reviews. The page expires after 30 minutes and can be opened again. It binds only to loopback, uses an unpredictable URL, escapes meeting content, and checks form origin and review revision. Folder approval does not grant anyone access. Sharing requires approval of the exact copy, recipients and view-only access.

The page uses `Referrer-Policy: strict-origin` so native form submissions retain their local `Origin` while excluding the token path from referrers. The former `no-referrer` policy made Chrome send `Origin: null`, so legitimate choices were rejected. A Chrome test reproduced that rejection and verified saving after the fix. Exact Host/Origin checks still reject missing, null, foreign, or duplicate origins.

An approved choice resumes only its routing stage when routing is enabled. It never releases previously held archive, notes, or publish stages. Approval and worker-start retries are idempotent, including approval arriving while a worker is saving the review hold. Disabling a stage preserves its hold until an explicit release. A worker waiting on review exits instead of polling indefinitely.

An approved meeting with waiting work can show a separate **Resume waiting work**
button. It lists the eligible stages for that meeting. Clicking it releases only
the displayed held/failed stages that are still enabled and configured. Settings
changes can narrow the action; enabling another stage after the page loads cannot
expand it. The worker rechecks the exact company approval, frozen company map,
transcript, and saved notes/source consistency before resuming. Other meetings and
their holds are unchanged. Repeating the action after a worker-launch failure
wakes already-pending work without resetting its saved settings, attempts or backoff.
Ordinary folder approval and the Settings toggles do not perform this explicit release.

If a selected job was queued without its transcript, the explicit repair is
`workflow.py attach-transcript JOB /original/job/session/transcript.json`, followed
by `workflow.py release JOB notes` when notes are configured and enabled. Attachment
accepts only that job's original session transcript after verifying the recording
hashes; it does not release a stage. The second command releases only that job's
notes. A routed job can then use folder review and Resume for its remaining held
stages. This does not add routing to older three-stage jobs or release other jobs.

The CLI also provides `reviews`, `choose-company JOB COMPANY --revision REVISION`, and `keep-local JOB --revision REVISION`. The `review --no-open --print-url` mode prints the private local address for a controlled browser handoff. Do not publish that temporary address.

## Disposable UI preview

From the repository root, run:

```sh
python3 scripts/preview-review-ui.py
```

Open the printed local URL, or add `--open` to open it in your browser. The preview contains fictional meetings with folder reviews, processing states, calendar suggestions, sharing reviews and local deletion confirmation. Every action is simulated; it starts no worker, invokes no Codex process, makes no cloud requests and sends no invitations. Use only `example.com` recipient addresses.

Press Ctrl+C to stop the preview and remove its temporary files. It also closes after 30 minutes; `--timeout SECONDS` changes that limit. Restarting resets all sample meetings.

## Verification scope

Automated tests cover routing and pipeline behavior, saved policies, aliases,
conflicting companies, negation and quoted speech. Synthetic transcripts through
the Codex runner have exercised both a direct ownership statement eligible for
automatic filing and mixed ownership that remains pending. These checks do not
establish how the model will classify every real transcript; review remains the
default.

[Call Summary with Codex](codex-notes.md) · [Google Drive filing](codex-drive.md) · [Notes sharing](notes-sharing.md) · [Recording integration](meeting-workflow.md)
