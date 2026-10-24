# Third-party notices

Quill is based on [digimata/quill](https://github.com/digimata/quill). The upstream
MIT license and Andrew Jones copyright notice are preserved in [LICENSE](../LICENSE).
This version adds meeting detection, recording controls and a separate completion
workflow.

## Lucide and Feather icon

The menu-bar feather in `Sources/quill/UI/MenuBarController.swift` and the app icon
in `scripts/app-icon/Quill.svg` use the Lucide feather. The app icon adds a background
and changes the scale and stroke styling. Lucide lists this icon among those derived
from Feather.

The full [Lucide and Feather license notices](licenses/LICENSE.lucide) are included.
They retain the Lucide ISC notice and the Feather MIT notice for Cole Bemis. The
copy comes from [Lucide revision `20aef31db93f`](https://github.com/lucide-icons/lucide/blob/20aef31db93fe4d9f398dbb15b120d99c48eef95/LICENSE).

## Anthropic Call Summary

The completion adapter is adapted from Anthropic's Call Summary, pinned to revision
`1f517b9de47e827c80cd933ed364e16838072239`. Its
[original reference](../integrations/meeting-workflow/skills/call-summary/references/upstream-call-summary.md),
[source metadata](../integrations/meeting-workflow/skills/call-summary/references/source.json),
[Apache-2.0 license](../integrations/meeting-workflow/skills/call-summary/references/LICENSE.anthropic)
and [adaptation notice](../integrations/meeting-workflow/skills/call-summary/SKILL.md)
remain together. Links inside the unchanged upstream reference are relative to
Anthropic’s original repository; its connector catalog is not bundled here.
The adapted instructions add transcript evidence validation,
meeting-note structure and limits on inferred names and actions.

## Downloaded dependencies

`Package.resolved` pins the source dependencies used by the tested build:

| Dependency | Pinned version | License |
| --- | --- | --- |
| [FluidAudio](https://github.com/FluidInference/FluidAudio) | 0.17.5 | [Apache-2.0](https://github.com/FluidInference/FluidAudio/blob/0b1f46289fe27d95b5e66ad8be46e64f5ee02ae7/LICENSE) |
| [Swift Argument Parser](https://github.com/apple/swift-argument-parser) | 1.8.2 | [Apache-2.0 with Swift Runtime Library Exception](https://github.com/apple/swift-argument-parser/blob/6a52f3251125d74daf04fcbd5e6f08a75d074382/LICENSE.txt) |

SwiftPM downloads these dependencies during a build. Their source, model weights
and compiled app bundles are not included in this source snapshot. A binary release
needs its own packaging review for dependency and model notices.

FluidAudio 0.17.5 also declares the prebuilt **NemoTextProcessing 0.3.1** artifact
from [text-processing-rs](https://github.com/FluidInference/text-processing-rs).
Its archive checksum is pinned in FluidAudio's package manifest; the Mac library
is statically linked. FluidAudio preserves its
[NemoTextProcessing and transitive dependency notices](https://github.com/FluidInference/FluidAudio/blob/0b1f46289fe27d95b5e66ad8be46e64f5ee02ae7/ThirdPartyLicenses/NemoTextProcessing-LICENSE.md),
covering Apache-2.0 NVIDIA NeMo material and MIT/Apache-2.0 Rust components. The
upstream notice still labels the artifact 0.3.0; the package manifest used by this
build specifies 0.3.1. The archive itself is downloaded by SwiftPM and is not
committed here.
