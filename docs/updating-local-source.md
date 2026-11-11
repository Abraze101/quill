# Preserve local changes across Quill updates

This derivative changes the native app, a Chrome extension and a separate
completion workflow. Keep reviewed source in Git, and save the working software
before an update. A source commit alone does not preserve the signed installed
app, browser host or local configuration.

## Install the update shown in Quill

In the Quill menu or Settings, choose **Check for Updates…**. Starting with
0.1.18, **Update and Restart** downloads the release executable for your Mac
and checks its digest, architecture, and embedded version. Quill signs the app
with the certificate used by your existing installation and updates the workflow
files in their original checkout. The previous signed app is retained.

Finish any recording and active processing before restarting. If a meeting
starts while the update is being prepared, Quill leaves the recording running.
The installer preserves your local work and reports when it cannot update a checkout.

### First update from 0.1.17 or earlier

Those versions use the manual update procedure. For a release that includes
Mac executables, download its source ZIP from [Quill releases](https://github.com/Abraze101/quill/releases)
and extract it to a temporary folder. After recording and processing finish,
quit Quill, open Terminal in the extracted folder, and run:

```sh
/usr/bin/python3 -I -B scripts/app-update.py bootstrap
```

The helper verifies the release executable, uses the original installation's
checkout and signing certificate, then reopens Quill. Future updates use the
button in the app.

### Updating from source

Use this path for a source-only update or a checkout with local changes.

1. Finish any recording and processing, quit Quill, and close the Processing
   Center. Open Terminal in the original Quill source folder. The installed app,
   completion worker and Chrome extension use that folder. You can unpack the
   ZIP elsewhere to inspect it.
2. Run `git status --short --branch` and [save a recovery checkpoint](#save-the-working-installation-first).
   If you have local edits or commits of your own, [review the changes in a separate checkout](#prepare-an-update-without-changing-the-working-copy).
3. For a clean checkout of this fork on `main`, copy the 40-character commit ID
   from the ZIP name or from the guide’s address after `/blob/`. Run the
   commands below, replacing `COMMIT_ID` with that value. If Git reports a
   conflict or cannot fast-forward, use the separate-checkout procedure above.

   ```sh
   quill_revision=COMMIT_ID
   git fetch https://github.com/Abraze101/quill.git "$quill_revision" &&
     git merge --ff-only "$quill_revision"
   ```

4. From the same folder, build and install using your existing signing
   certificate. The installer retains the previous signed app. Open **About
   Quill…** to confirm the version and build.

   ```sh
   sh scripts/build-preview-app.sh &&
     sh scripts/install-app.sh &&
     open /Applications/Quill.app
   ```

If the update changes Chrome or the completion workflow, see the
[component-specific steps](#prepare-an-update-without-changing-the-working-copy).

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
browser acceptance. This procedure is also suitable for custom forks.

Before promoting a candidate, save a fresh checkpoint and finish recording and
processing. Keep the existing certificate, bundle identifier and installed app
path. The [packaging and installation guide](meeting-workflow.md#opening-and-updating-the-installed-app)
explains the guarded app replacement.

Before publishing an update that should appear in **Check for Updates…**,
increment `CFBundleVersion` in `Sources/quill/Info.plist`. This also applies
when the only changes are in the workflow or Chrome extension. Commits with
the same version and build produce the same update result. Use
`CFBundleShortVersionString` for the derivative's
feature or maintenance version; the first explicitly versioned release build is
**0.1.0 (2)**. Packaging requires a numeric `major.minor.patch` version and a
positive build number, and always compiles with `-c release`. Check the installed
version through **About Quill…** after reopening. Version numbers do not replace
signature, source-hash or runtime-compatibility checks.

### Publishing a build for Update and Restart

After the commit passes the macOS checks on `main`, push a version tag
matching `CFBundleShortVersionString` (for example, `v0.1.18`) at that commit.
The **Publish Quill update** workflow builds the Apple Silicon and Intel
executables on GitHub, verifies their embedded versions, then uploads both
to a draft release. It compares the uploaded SHA-256 digests with the builds
before publishing the release. Leave the draft unpublished while it runs.
A new binary release needs its own version tag.

The components update independently:

| Component | What an update needs |
|---|---|
| Native Swift app | Choose **Update and Restart** for a compatible prebuilt release. For source-only changes, build and sign from the original checkout. |
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
