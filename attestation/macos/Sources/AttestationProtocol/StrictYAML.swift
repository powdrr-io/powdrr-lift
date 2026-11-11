import Foundation
import Yams

/// Parse event preflight rejects YAML features whose meaning can differ across
/// implementations before decoding typed records. The size bound is applied to
/// original bytes, before UTF-8 decoding or parser allocation.
public enum StrictYAML {
    public static func document(_ bytes: Data, limit: Int) throws -> Node {
        guard bytes.count <= limit else { throw ProtocolParseError.inputTooLarge }
        guard let text = String(data: bytes, encoding: .utf8) else { throw ProtocolParseError.invalidUTF8 }
        do {
            var documents = try compose_all(yaml: text)
            guard let node = documents.next(), documents.next() == nil, documents.error == nil else {
                throw ProtocolParseError.invalidDocument
            }
            try rejectHazards(node)
            return node
        } catch let error as ProtocolParseError {
            throw error
        } catch {
            throw ProtocolParseError.invalidDocument
        }
    }

    public static func policy(_ bytes: Data) throws -> PolicyRecord {
        let root = try document(bytes, limit: 64 * 1024)
        try validatePolicy(root)
        do {
            return try YAMLDecoder().decode(PolicyRecord.self, from: root)
        } catch {
            throw ProtocolParseError.invalidDocument
        }
    }

    private static func validatePolicy(_ root: Node) throws {
        let policy = try fields(root, allowed: ["version", "freshness", "environment", "defaults", "steps"])
        let freshness = try fields(required(policy, "freshness"), allowed: ["max_age_seconds"])
        let environment = try fields(required(policy, "environment"), allowed: ["toolchain", "network"])
        let defaults = try fields(required(policy, "defaults"), allowed: ["timeout_seconds"])
        guard required(policy, "version").int == 1,
              let age = required(freshness, "max_age_seconds").int, (300...604800).contains(age),
              let toolchain = required(environment, "toolchain").string, !toolchain.isEmpty,
              required(environment, "network").string == "allowed",
              let timeout = required(defaults, "timeout_seconds").int, (1...14400).contains(timeout),
              let steps = required(policy, "steps").sequence,
              !steps.isEmpty, steps.count <= 32 else {
            throw ProtocolParseError.invalidDocument
        }
        var identities = Set<String>()
        for step in steps {
            let values = try fields(step, allowed: ["id", "argv", "shell", "cwd", "env", "timeout_seconds"])
            guard let id = required(values, "id").string,
                  id.range(of: "^[a-z][a-z0-9_-]{0,63}$", options: .regularExpression) != nil,
                  identities.insert(id).inserted,
                  (values["argv"] != nil) != (values["shell"] != nil) else {
                throw ProtocolParseError.invalidDocument
            }
            if let argv = values["argv"]?.sequence {
                guard !argv.isEmpty, argv.allSatisfy({ $0.string.map { !$0.isEmpty } == true }) else {
                    throw ProtocolParseError.invalidDocument
                }
            }
            if let command = values["shell"]?.string, command.isEmpty || command.count > 16_384 {
                throw ProtocolParseError.invalidDocument
            }
            if let cwd = values["cwd"]?.string,
               cwd.isEmpty || cwd.hasPrefix("/") || cwd.contains("\\") || cwd.split(separator: "/").contains(where: { $0 == ".." || $0 == ".git" }) {
                throw ProtocolParseError.invalidDocument
            }
            if let env = values["env"] {
                guard let entries = env.mapping else { throw ProtocolParseError.invalidDocument }
                for (key, value) in entries {
                    guard let name = key.string,
                          value.tag.description == "tag:yaml.org,2002:str",
                          !["PATH", "HOME", "VIRTUAL_ENV", "PYTHONPATH", "TMPDIR"].contains(name),
                          !name.hasPrefix("DYLD_"), !name.hasPrefix("LD_"), !name.hasPrefix("POWDRR_ATTEST_") else {
                        throw ProtocolParseError.invalidDocument
                    }
                }
            }
            if let value = values["timeout_seconds"],
               value.int.map({ (1...14400).contains($0) }) != true {
                throw ProtocolParseError.invalidDocument
            }
        }
    }

    private static func fields(_ node: Node, allowed: Set<String>) throws -> [String: Node] {
        guard let mapping = node.mapping else { throw ProtocolParseError.invalidDocument }
        var values: [String: Node] = [:]
        for (key, value) in mapping {
            guard let name = key.string, allowed.contains(name) else { throw ProtocolParseError.invalidDocument }
            values[name] = value
        }
        return values
    }

    private static func required(_ values: [String: Node], _ key: String) -> Node {
        values[key] ?? Node("<missing>")
    }

    private static func rejectHazards(_ node: Node) throws {
        guard node.anchor == nil else { throw ProtocolParseError.yamlHazard }
        switch node {
        case .scalar(let scalar):
            if isCustomTag(scalar.tag.description) { throw ProtocolParseError.yamlHazard }
        case .sequence(let sequence):
            if isCustomTag(sequence.tag.description) { throw ProtocolParseError.yamlHazard }
            for child in sequence { try rejectHazards(child) }
        case .mapping(let mapping):
            if isCustomTag(mapping.tag.description) { throw ProtocolParseError.yamlHazard }
            var keys = Set<String>()
            for (key, value) in mapping {
                guard case .scalar(let scalar) = key else { throw ProtocolParseError.invalidDocument }
                if !keys.insert(scalar.string).inserted { throw ProtocolParseError.duplicateKey }
                try rejectHazards(key)
                try rejectHazards(value)
            }
        case .alias:
            throw ProtocolParseError.yamlHazard
        }
    }

    private static func isCustomTag(_ tag: String) -> Bool {
        !tag.isEmpty && !tag.hasPrefix("tag:yaml.org,2002:")
    }
}
