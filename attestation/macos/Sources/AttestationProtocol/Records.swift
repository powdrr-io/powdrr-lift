import Foundation

public struct PolicyRecord: Codable, Sendable, Equatable {
    public let version: Int
    public let freshness: FreshnessRecord
    public let environment: EnvironmentRecord
    public let defaults: DefaultsRecord
    public let steps: [StepRecord]

    public init(version: Int, freshness: FreshnessRecord, environment: EnvironmentRecord, defaults: DefaultsRecord, steps: [StepRecord]) {
        self.version = version
        self.freshness = freshness
        self.environment = environment
        self.defaults = defaults
        self.steps = steps
    }
}

public struct FreshnessRecord: Codable, Sendable, Equatable {
    public let maxAgeSeconds: Int
    enum CodingKeys: String, CodingKey { case maxAgeSeconds = "max_age_seconds" }
}
public struct EnvironmentRecord: Codable, Sendable, Equatable { public let toolchain: String; public let network: String }
public struct DefaultsRecord: Codable, Sendable, Equatable {
    public let timeoutSeconds: Int
    enum CodingKeys: String, CodingKey { case timeoutSeconds = "timeout_seconds" }
}

public struct StepRecord: Codable, Sendable, Equatable {
    public let id: String
    public let argv: [String]?
    public let shell: String?
    public let cwd: String
    public let env: [String: String]
    public let timeoutSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case id, argv, shell, cwd, env
        case timeoutSeconds = "timeout_seconds"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        argv = try container.decodeIfPresent([String].self, forKey: .argv)
        shell = try container.decodeIfPresent(String.self, forKey: .shell)
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd) ?? "."
        env = try container.decodeIfPresent([String: String].self, forKey: .env) ?? [:]
        timeoutSeconds = try container.decodeIfPresent(Int.self, forKey: .timeoutSeconds)
    }
}

public struct ProviderRecord: Codable, Sendable, Equatable {
    public let providerID: String
    public let keyID: String
    public let algorithm: String
    public let publicKey: String
    public let status: String
    public let notBefore: String
    public let notAfter: String?
}

public enum ProtocolParseError: Error, Equatable {
    case inputTooLarge
    case invalidUTF8
    case yamlHazard
    case duplicateKey
    case invalidDocument
}
