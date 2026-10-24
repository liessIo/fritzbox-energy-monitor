// swift-tools-version: 6.0
import PackageDescription

// Zero third-party dependencies, by design.
let package = Package(
    name: "EnergyMonitor",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "EnergyMonitor", path: "Sources/EnergyMonitor")
    ]
)
