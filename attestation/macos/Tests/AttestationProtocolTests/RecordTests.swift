import Foundation
import Testing
@testable import AttestationProtocol

@Test func samplePolicyDecodesToTypedRecords() throws {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appending(path: "../fixtures/valid/policy.yaml")
    let bytes = try Data(contentsOf: url)
    let policy = try StrictYAML.policy(bytes)
    #expect(policy.version == 1)
    #expect(policy.steps.map(\.id) == ["setup", "tests", "quality"])
}

@Test func invalidPolicyShapesAndUnknownFieldsAreRejected() throws {
    let base = "version: 1\nfreshness: {max_age_seconds: 86400}\nenvironment: {toolchain: stable, network: allowed}\ndefaults: {timeout_seconds: 60}\n"
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.policy(Data((base + "steps: []\n").utf8))
    }
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.policy(Data((base + "steps: [{id: one, argv: [echo], extra: true}]\n").utf8))
    }
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.policy(Data((base + "steps: [{id: one, argv: [echo], env: {NUMBER: 1}}]\n").utf8))
    }
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.policy(Data((base + "steps: [{id: one, argv: [echo], timeout_seconds: 1.5}]\n").utf8))
    }
}

@Test func duplicateKeysAndAliasesAreRejected() throws {
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.document(Data("version: 1\nversion: 1\n".utf8), limit: 1024)
    }
    #expect(throws: ProtocolParseError.self) {
        _ = try StrictYAML.document(Data("defaults: &d {timeout_seconds: 10}\ncopy: *d\n".utf8), limit: 1024)
    }
}

@Test func sharedInvalidFixturesAreRejected() throws {
    let fixtureRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appending(path: "../fixtures/invalid")
    for name in ["duplicate-key.yaml", "alias.yaml", "custom-tag.yaml", "unknown-field.yaml", "non-string-env.yaml", "float-timeout.yaml"] {
        let bytes = try Data(contentsOf: fixtureRoot.appending(path: name))
        #expect(throws: (any Error).self) { _ = try StrictYAML.policy(bytes) }
    }
}

@Test func swiftDigestsMatchSharedGoldenVectors() throws {
    let fixtureRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appending(path: "../fixtures")
    let vectorData = try Data(contentsOf: fixtureRoot.appending(path: "snapshot-vectors.json"))
    let vectors = try #require(JSONSerialization.jsonObject(with: vectorData) as? [String: Any])
    let encodedRecords = try #require(vectors["records"] as? [[String: Any]])
    let records = try encodedRecords.map { value -> SnapshotRecord in
        guard let pathHex = value["path_hex"] as? String, let path = Data(hex: pathHex),
              let kind = (value["kind"] as? NSNumber)?.uint8Value,
              let executable = value["executable"] as? Bool,
              let contentHex = value["content_hex"] as? String,
              let content = Data(hex: contentHex) else {
            throw ProtocolParseError.invalidDocument
        }
        return SnapshotRecord(
            path: path,
            kind: kind,
            executable: executable,
            content: content
        )
    }
    let expected = try #require(vectors["digest"] as? String)
    #expect(try ProtocolDigests.snapshotSHA256(records) == expected)
    #expect(try ProtocolDigests.snapshotSHA256(records.reversed().map { $0 }) == expected)

    let policy = try Data(contentsOf: fixtureRoot.appending(path: "valid/policy.yaml"))
    #expect(ProtocolDigests.rawPolicySHA256(policy) == (vectors["policy_sha256"] as? String))
    let stepDigests = try #require(vectors["step_definition_sha256"] as? [String: String])
    for (stepID, digest) in stepDigests {
        #expect(try ProtocolDigests.stepDefinitionSHA256(policyBytes: policy, stepID: stepID) == digest)
    }
}

private extension Data {
    init?(hex: String) {
        guard hex.count.isMultiple(of: 2) else { return nil }
        var output = Data()
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            output.append(byte)
            index = next
        }
        self = output
    }
}
