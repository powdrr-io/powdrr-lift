// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PowdrrAttestationProtocol",
    platforms: [.macOS(.v14)],
    products: [.library(name: "AttestationProtocol", targets: ["AttestationProtocol"])],
    dependencies: [.package(url: "https://github.com/jpsim/Yams.git", exact: "5.4.0")],
    targets: [
        .target(name: "AttestationProtocol", dependencies: [.product(name: "Yams", package: "Yams")]),
        .testTarget(name: "AttestationProtocolTests", dependencies: ["AttestationProtocol"])
    ],
    swiftLanguageModes: [.v6]
)
