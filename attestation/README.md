# Local validation attestation protocol

This directory freezes version 1 protocol records and portable parsing rules for
the local validation attestation implementation. The implementation plan in
`docs/plans/local-validation-attestation-implementation-plan.md` is the source of
security and lifecycle requirements.

The JSON Schemas describe policy, registry, signed predicate, DSSE envelope, and
bounded IPC request shapes. Parsers must also enforce byte limits, reject unknown
fields, duplicate YAML/JSON keys, aliases and custom YAML tags, and preserve raw
policy bytes for their digest. Schemas alone do not provide those guarantees.

`examples/local-validation.yaml` is the sample validation policy.
`fixtures/valid/` contains cross-language YAML examples. `fixtures/crypto/` and
`fixtures/snapshot-vectors.json` are deterministic golden vectors. The Ed25519
private seed is deliberately checked in only as a **test fixture**; it must never
be used by a provider or production deployment.

Python parsers are an independent package under `verifier/`; they do not import
Powdrr runtime modules. The Swift protocol target is under `macos/`. Service,
signing, execution, and CI enforcement are intentionally absent from this schema
slice.
