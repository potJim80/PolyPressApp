// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Handrail",
    platforms: [.macOS(.v14)],
    targets: [
        // The generator and the file handling live in a library so both the app
        // and the test runner can reach them. Only the app target sees SwiftUI.
        .target(name: "HandrailCore", path: "Sources/HandrailCore"),

        .executableTarget(
            name: "Handrail",
            dependencies: ["HandrailCore"],
            path: "Sources/Handrail"),

        // A plain executable rather than XCTest: XCTest ships with Xcode, and
        // this machine has only the Command Line Tools. `swift run handrail-test`
        // works with any toolchain and can shell out to R, which is what the
        // most important test here needs to do.
        // A stopwatch you can re-run. Efficiency claims that are not measured
        // stop being true within a week.
        .executableTarget(
            name: "handrail-bench",
            dependencies: ["HandrailCore"],
            path: "Sources/HandrailBench"),

        .executableTarget(
            name: "handrail-test",
            dependencies: ["HandrailCore"],
            path: "Sources/HandrailTests"),
    ]
)
