# Preserve local changes across Quill updates

This derivative changes the native app, a Chrome extension and a separate
completion workflow. Keep reviewed source in Git, and save the working software
before an update. A source commit alone does not preserve the signed installed
app, browser host or local configuration.

## Save the working installation first

Inspect `git status --short` and recent commits. Preserve tracked edits and
untracked source additions before changing branches or reviewing upstream. Never
reset or replace the active checkout to obtain a clean upstream copy.

For an existing signed installation, create a new checkpoint outside the checkout:

```sh
mkdir -p "$HOME/Quill-recovery"
python3 scripts/recovery-checkpoint.py create \
  --output "$HOME/Quill-recovery/before-next-update"
python3 scripts/recovery-checkpoint.py verify \
  --checkpoint "$HOME/Quill-recovery/before-next-update"
```

Use an unused output name each time. The helper expects the supported installed
app and browser files; it is not a fresh-install tool. Changed files during
capture stop checkpoint creation.

A checkpoint includes the signed app, the complete allowed source inventory and
the installed native browser host/registrations. It records configuration hashes
and database schema hashes for compatibility checks. It does not copy recordings,
transcripts, credentials, signing keys, queue rows or upload receipts. Keep those
runtime files and the signing private key in their existing protected locations.

Verification checks payload hashes, modes, the app signature and actual signing
certificate, and exact source reconstruction. The manifest checksum detects
accidental damage; it does not authenticate a checkpoint received from someone
else. A checkpoint captures source and the installed app at one time, but an
older app marker may not prove which source revision built that binary.

Backup apps can appear in Spotlight. Add the backup directory to System Settings
→ Spotlight → Search Privacy, following [Apple's folder-exclusion instructions](https://support.apple.com/guide/mac-help/mchl1bb43b84/mac).
Keep `/Applications/Quill.app` searchable. Exclusion preserves backups; opening a
backup directly in Finder is still possible, so do not launch staged copies.

## Prepare an update without changing the working copy

Review upstream changes in a separate checkout or replay candidate. Compare the
source diff, run automated tests, and use a controlled meeting for capture and
browser acceptance. No automatic fetch, merge, installation or deployment is
configured by this source tree.

Before promoting a candidate, save a fresh checkpoint and finish recording and
processing. Keep the existing certificate, bundle identifier and installed app
path. The [packaging and installation guide](meeting-workflow.md#opening-and-updating-the-installed-app)
explains the guarded app replacement.

Before packaging a changed native app, increment `CFBundleVersion` in
`Sources/quill/Info.plist`. Use `CFBundleShortVersionString` for the derivative's
feature or maintenance version; the first explicitly versioned release build is
**0.1.0 (2)**. Packaging requires a numeric `major.minor.patch` version and a
positive build number, and always compiles with `-c release`. Check the installed
version through **About Quill…** after reopening. Version numbers do not replace
signature, source-hash or runtime-compatibility checks, and do not enable an
automatic updater.

The components update independently:

| Component | What an update needs |
|---|---|
| Native Swift app | Build, sign, install while idle, then reopen. |
| Python completion/review source | Start a new worker or review server after replacing reviewed source; an open server retains loaded code. |
| Chrome extension | Reload the unpacked extension, then refresh meeting tabs when safe. |
| Native browser host | Rerun its installer with the exact extension ID; changes apply on the next connection. |
| Call Summary adapter/reference | Review the pinned Anthropic source, license and local adaptation separately. |

Completion commands use absolute paths into the configured checkout. Moving the
checkout requires updating those commands and the unpacked extension location;
a repository rename is not an installation migration.

Preserve current queue rows, approvals, upload receipts and transfer journals.
An older uploader that ignores a saved reserved-ID or resumable operation must
not retry it. Restore compatible source before releasing such work. A successful
build does not establish that a downgrade can read current state safely.

## Stage a rollback before touching live files

```sh
python3 scripts/recovery-checkpoint.py stage \
  --checkpoint "$HOME/Quill-recovery/before-next-update" \
  --output "$HOME/Quill-recovery/restore-candidate"
python3 scripts/recovery-checkpoint.py check-current \
  --checkpoint "$HOME/Quill-recovery/before-next-update"
```

Staging reconstructs source, app and browser files in a new directory. It does
not restore or run them. Do not open the staged app: its configured completion
commands may still point to the live checkout.

`check-current` exits with status 2 when source, installation paths, browser
files, configuration or database DDL differ. Database version numbers alone are
insufficient. Treat a difference as a compatibility review, not a reason to copy
old runtime data over the current state.

For an app-only replacement with matching source, browser files and settings,
finish processing and quit Quill normally, then run:

```sh
python3 scripts/recovery-checkpoint.py restore-app \
  --checkpoint "$HOME/Quill-recovery/before-next-update"
```

This holds Quill's instance and worker locks, refuses active recording/processing,
checks compatibility again and installs the archived signed app. It preserves the
replaced app and does not launch Quill or stop another process. Missing activity
locks also require review.

When source or browser files differ, automatic app restoration stops. Compare the
staged files with the live checkout, save the current installation, and restore
only reviewed software files at their original paths. Quit Quill, its workers and
review servers, and Chrome normally before changing the live software they use.
Preserve the Git index and unrelated edits. Restore the host and matching
registration together; do not substitute an old `.repo-backup` registration.
Reload the extension and meeting tabs after reopening Chrome.

Keep the newest recordings, preferences, credentials, queue, approvals and upload
receipts. There is no automated in-place database rewind. Changed configuration or
schema requires a migration/compatibility review before a downgrade. After a
reviewed restore, verify idle startup, recording choices, processing status and
browser connection before a controlled capture test.

## Retained app versions

Successful installs retain the previous signed app under
`/Applications/.quill-app-backups/install-*/Quill.app`. Each private directory has
a receipt containing checkout identity, certificate and old/new fingerprints.
The installer verifies and flushes the backup before replacement and never prunes
it automatically.

`install-app.py --source-app` can install a reviewed archived app with the same
signature, checkout and idle checks. Prefer `recovery-checkpoint.py restore-app`
for recovery because it also checks matching source, browser files and runtime
compatibility.

## Source-only export and replay

To preserve source without an installed app, export to an unused directory outside
the checkout:

```sh
mkdir -p "$HOME/Quill-source-backups"
python3 scripts/source-overlay.py export \
  --output "$HOME/Quill-source-backups/source-before-update"
python3 scripts/source-overlay.py replay \
  --bundle "$HOME/Quill-source-backups/source-before-update" \
  --output "$HOME/Quill-source-backups/verified-source"
```

The bundle contains the current HEAD snapshot, a binary-capable patch and local
source additions. Its manifest records paths, hashes, sizes, modes and deletions.
Export reconstructs the exact candidate in temporary storage before publishing
the bundle. Keep all bundle files together; replay needs no original checkout or
Git history.

The allowlist covers the package files, README, license, `.gitignore`, `Sources`,
`Tests`, `docs`, `integrations`, `scripts`, `.issues` and `.github`. Runtime settings,
media, credentials, signing material, databases and caches are excluded. A tracked
file outside the allowlist stops export. When adding a new root-level document,
update the inventory and its tests. Filename rules cannot detect every secret
inside source; review the entire exported inventory before sharing it.

To replay onto an upstream revision already available in another local repository:

```sh
python3 scripts/source-overlay.py replay \
  --bundle "$HOME/Quill-source-backups/source-before-update" \
  --output "$HOME/Quill-source-backups/upstream-candidate" \
  --onto-repo /path/to/upstream-quill --onto-ref REVIEWED_REVISION
```

This reads the selected revision without fetching, checking it out or changing
its index. It produces a source directory without `.git`. Conflicts, path
collisions, unsupported files, corrupt objects and existing output directories
stop replay. A clean replay still needs review and tests; it never replaces the
working checkout automatically.

Run the candidate's `sh scripts/test-workflow.sh`, then the relevant live checks
from the [workflow guide](meeting-workflow.md#validation-and-upgrades). Preserve
media and runtime state independently; a source export intentionally contains
neither. Moving to another Mac also requires account configuration, permissions
and deliberate signing-key handling.

The optional Swift workaround keeps copied SwiftPM modules in
`.build/local-swiftpm`. After changing Xcode or Command Line Tools, regenerate
that project-local cache if mismatch errors return. Do not edit system developer
tools to repair this checkout.
