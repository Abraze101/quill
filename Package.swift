// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "quill",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.3.0"),
        .package(url: "https://github.com/FluidInference/FluidAudio.git", exact: "0.17.5"),
    ],
    targets: [
        .target(name: "MeetingWorkflow"),
        // The Command Line Tools installation lacks XCTest; these executable
        // checks run the same state transitions without a full Xcode install.
        .executableTarget(name: "meeting-workflow-checks", dependencies: ["MeetingWorkflow"], path: "Tests/MeetingWorkflowTests"),
        .executableTarget(
            name: "quill",
            dependencies: [
                "MeetingWorkflow",
                .product(name: "ArgumentParser", package: "swift-argument-parser"),
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            exclude: ["Info.plist"],
            linkerSettings: [
                // Embed Info.plist into the binary so TCC can attribute the
                // system-audio-capture permission to quill itself when it
                // runs as a LaunchAgent (no .app bundle to carry a plist).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/quill/Info.plist",
                ]),
            ]
        ),
    ]
)
