---
name: quill-setup
description: Connect the Quill desktop plugin to an existing local Quill installation and check its reader. Use during plugin onboarding or when Quill's checkout cannot be found.
---

Resolve this plugin root two directories above this file's containing directory.
Run `python3 scripts/quill.py doctor` from that root. Follow explicit user
instructions over these defaults.

If `reader_compatible` is false, explain that the checkout needs the plugin's
reader update. Do not claim setup succeeded or rebuild/reinstall the native app
as a side effect. Update the selected source only when the user authorizes that
work, preserving their local changes.

If the reader finds a compatible Quill checkout, report what it verified and show two example requests,
such as "Is Quill recording?" and "Find my latest meeting." Do not load meeting
content merely to demonstrate setup.

If it cannot find the source checkout, ask where the user cloned this version of
Quill. Given that exact path, run
`python3 scripts/quill.py --repo /path/to/quill setup`, using a quoted argument
or an execution-tool argument array. This saves only the plugin's checkout
pointer. Run `doctor` and `status` again. Do not edit Quill's recording,
transcription, notes, account, or sharing preferences as part of plugin setup.

This plugin needs local computer access in ChatGPT on macOS, Python 3.9 or newer,
and the Quill checkout containing the plugin reader support. It packages skills
and a local reader, so no additional model API key or hosted service is required.
Requested text returned to a chat is available to that chat's model. Local file
storage does not make that chat processing on-device.

If Quill is not installed, explain that the recorder setup is a separate step.
Use the checkout's `docs/meeting-workflow.md` when the user asks to install it.
New installation needs that user's permissions, account selection, destination
folders, and recording preferences. Existing meeting files, private settings,
and credentials do not belong in the plugin package.
