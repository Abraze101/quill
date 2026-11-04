# Quill for ChatGPT desktop

Ask ChatGPT whether Quill is recording, find a local meeting, read saved notes or
a transcript, and open the existing processing review.

This first version is a desktop plugin with two skills and a Python reader.
It uses your installed Quill workflow. It needs local computer access, macOS,
Python 3.9 or newer, and a checkout of this Quill version. No additional model API
key or hosted service is needed. Text requested through the plugin is returned
to the chat and can be processed by its model.

## Install locally

The repository includes a marketplace at `.agents/plugins/marketplace.json`.
From the Quill repository root, with a current Codex CLI, run

```sh
codex plugin marketplace add .
codex plugin add quill@quill-local
```

Alternatively, open the Plugins Directory in ChatGPT desktop, select the Quill
local source, and install Quill. A desktop app restart may be needed for local
marketplace discovery. Open a new chat with local computer access to use it.

Run setup if the plugin asks for the source checkout. For a nonstandard location,
from this plugin directory run

```sh
python3 scripts/quill.py --repo /path/to/quill setup
python3 scripts/quill.py doctor
```

Setup saves the checkout pointer in `~/.config/quill/chatgpt-plugin.json`.
`--repo` or `QUILL_REPO` can select a checkout for one invocation. Each person
uses their own Quill settings and accounts.

## Try it

- “Is Quill recording right now?”
- “Find my latest meeting.”
- “Show me the notes from the project planning call.”
- “Open Quill’s meeting processing review.”

Recording controls use Quill's native interface when computer-use tools are
available. Filing, sharing, retries, cancellation, and deletion use the existing
review screen. Status and content lookups do not start processing.

## Reader commands

Run these from the plugin directory. Read commands print JSON. The review
command prints its private local URL and runs until its 30-minute timeout.

| Command | Result |
| --- | --- |
| `python3 scripts/quill.py doctor` | Local prerequisites and checkout discovery |
| `python3 scripts/quill.py status` | Fresh recording state and saved processing snapshot |
| `python3 scripts/quill.py meetings --query planning --limit 20` | Matching library entries without notes or transcript text |
| `python3 scripts/quill.py get JOB --content notes` | Validated notes for one meeting |
| `python3 scripts/quill.py get JOB --content transcript` | Saved transcript with reviewed speaker labels |
| `python3 scripts/quill.py review` | Existing processing review, including previously approved work |

Lists and content accept `--offset` and `--limit`. Content limits count
characters. Use `next_offset` to read the next page. `--state` and `--config`
are global options for installations with custom workflow locations.
`--recording-state` selects the heartbeat directory separately and normally stays
at `~/.local/state/quill`.

The library covers meetings already queued into Quill's completion workflow.
It does not scan unrelated recordings. An unavailable heartbeat cannot prove
recording stopped. A saved Drive receipt is historical evidence, not a live
Drive query. Unverified notes and uncertain speaker names remain unverified.

## Distribution and testing

The package follows OpenAI's [portable plugin format](https://developers.openai.com/plugins/build/plugins)
and is marked [desktop only](https://developers.openai.com/plugins/deploy/desktop-only).
It also includes a matching `.codex-plugin/plugin.json` compatibility manifest
for desktop loaders that do not yet read the portable root manifest.
This local marketplace is separate from publication in OpenAI's public directory.

Tests run with the repository's Python workflow suite. They use temporary state
and synthetic meeting content. The plugin changes neither the native recording
app nor its build, signing identity, or capture loop.

The icon is copied from Quill's existing app icon. Its Lucide/Feather notices are
included in `assets/LICENSE.lucide`. The package includes the repository's MIT
license.
