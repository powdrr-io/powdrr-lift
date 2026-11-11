import CryptoKit
import Foundation

public struct SnapshotRecord: Sendable, Equatable {
    public let path: Data
    public let kind: UInt8
    public let executable: Bool
    public let content: Data

    public init(path: Data, kind: UInt8, executable: Bool, content: Data) {
        self.path = path
        self.kind = kind
        self.executable = executable
        self.content = content
    }
}

public enum ProtocolDigests {
    private static let snapshotMagic = Data("POWDRR-ATTEST-SNAPSHOT-V1\0".utf8)

    public static func rawPolicySHA256(_ bytes: Data) -> String {
        hex(Array(SHA256.hash(data: bytes)))
    }

    public static func stepDefinitionSHA256(policyBytes: Data, stepID: String) throws -> String {
        guard !stepID.contains("\0") else { throw ProtocolParseError.invalidDocument }
        var bytes = policyBytes
        bytes.append(0)
        bytes.append(contentsOf: stepID.utf8)
        return hex(Array(SHA256.hash(data: bytes)))
    }

    public static func snapshotSHA256(_ records: [SnapshotRecord]) throws -> String {
        guard UInt64(exactly: records.count) != nil else { throw ProtocolParseError.invalidDocument }
        let ordered = records.sorted { $0.path.lexicographicallyPrecedes($1.path) }
        guard Set(ordered.map(\.path)).count == ordered.count else { throw ProtocolParseError.invalidDocument }
        var encoded = snapshotMagic
        append(UInt64(ordered.count), to: &encoded)
        for record in ordered {
            guard !record.path.isEmpty, UInt32(exactly: record.path.count) != nil,
                  record.kind == 0 || record.kind == 1,
                  !(record.kind == 1 && record.executable),
                  UInt64(exactly: record.content.count) != nil else {
                throw ProtocolParseError.invalidDocument
            }
            append(UInt32(record.path.count), to: &encoded)
            encoded.append(record.path)
            encoded.append(record.kind)
            encoded.append(record.executable ? 1 : 0)
            append(UInt64(record.content.count), to: &encoded)
            encoded.append(contentsOf: SHA256.hash(data: record.content))
        }
        return hex(Array(SHA256.hash(data: encoded)))
    }

    private static func append<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var bigEndian = value.bigEndian
        withUnsafeBytes(of: &bigEndian) { data.append(contentsOf: $0) }
    }

    private static func hex<C: Collection>(_ bytes: C) -> String where C.Element == UInt8 {
        bytes.map { String(format: "%02x", $0) }.joined()
    }
}
