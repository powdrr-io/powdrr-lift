from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
import re

from powdrr_attestation.configuration import (
    ConfigurationError,
    parse_attestors_registry,
    parse_validation_policy,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _policy(step_block: str) -> bytes:
    policy = (
        "version: 1\nfreshness:\n  max_age_seconds: 86400\n"
        "environment:\n  toolchain: stable\n  network: allowed\n"
        "defaults:\n  timeout_seconds: 60\n"
    )
    if step_block.startswith("steps:"):
        return (policy + step_block).encode()
    return (
        policy + "steps:\n  - id: check\n    argv: [echo, ok]\n" + step_block
    ).encode()


def test_valid_policy_preserves_raw_digest_and_order() -> None:
    raw = (FIXTURES / "valid/policy.yaml").read_bytes()
    formatted = (FIXTURES / "valid/policy-formatted.yaml").read_bytes()
    parsed = parse_validation_policy(raw)
    same_meaning = parse_validation_policy(formatted)
    assert [step.step_id for step in parsed.steps] == ["setup", "tests", "quality"]
    assert parsed.raw_sha256 == sha256(raw).hexdigest()
    assert parsed.raw_sha256 != same_meaning.raw_sha256
    assert parsed.steps == same_meaning.steps


def test_valid_registry_checks_public_key_fingerprint() -> None:
    registry = parse_attestors_registry(
        (FIXTURES / "valid/attestors.yaml").read_bytes()
    )
    assert len(registry.users) == 1
    attestor = registry.users[0]
    assert attestor.github_user_id == 583231
    assert attestor.key_id == "sha256:" + sha256(attestor.public_key).hexdigest()


@pytest.mark.parametrize(
    "fixture,code",
    [
        ("duplicate-key.yaml", "DUPLICATE_MAPPING_KEY"),
        ("alias.yaml", "YAML_FEATURE_FORBIDDEN"),
        ("custom-tag.yaml", "YAML_FEATURE_FORBIDDEN"),
        ("unknown-field.yaml", "UNKNOWN_FIELD"),
        ("non-string-env.yaml", "INVALID_ENV"),
        ("float-timeout.yaml", "INVALID_TIMEOUT"),
    ],
)
def test_shared_invalid_yaml_fixtures(fixture: str, code: str) -> None:
    raw = (FIXTURES / "invalid" / fixture).read_bytes()
    with pytest.raises(ConfigurationError, match=code):
        parse_validation_policy(raw)


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"version: 1\nversion: 1\n", "DUPLICATE_MAPPING_KEY"),
        (b"x: &a 1\ny: *a\n", "YAML_FEATURE_FORBIDDEN"),
        (b"x: !custom value\n", "YAML_FEATURE_FORBIDDEN"),
        (_policy("extra: true\n"), "UNKNOWN_FIELD"),
        (
            _policy(
                "steps:\n  - id: one\n    argv: [echo, ok]\n    env: {NUMBER: 1}\n"
            ),
            "INVALID_ENV",
        ),
        (
            _policy(
                "steps:\n  - id: same\n    argv: [echo]\n  - id: same\n    argv: [echo]\n"
            ),
            "INVALID_STEP_ID",
        ),
        (
            _policy("steps:\n  - id: one\n    argv: [echo]\n    shell: echo\n"),
            "INVALID_STEP_COMMAND",
        ),
        (
            _policy(
                "steps:\n  - id: one\n    argv: [echo]\n    timeout_seconds: 1.5\n"
            ),
            "INVALID_TIMEOUT",
        ),
        (_policy("steps: []\n"), "INVALID_STEP_COUNT"),
        (
            _policy(
                "steps:\n"
                + "".join(f"  - id: s{i}\n    argv: [echo]\n" for i in range(33))
            ),
            "INVALID_STEP_COUNT",
        ),
        (
            _policy("steps:\n  - id: one\n    argv: [echo]\n    env: {PATH: unsafe}\n"),
            "FORBIDDEN_ENVIRONMENT_VARIABLE",
        ),
        (
            _policy("steps:\n  - id: one\n    argv: [echo]\n    cwd: ../outside\n"),
            "INVALID_CWD",
        ),
        (
            _policy("").replace(b"version: 1", b"version: 2", 1),
            "UNSUPPORTED_POLICY_VERSION",
        ),
    ],
)
def test_policy_rejects_invalid_fixtures(raw: bytes, code: str) -> None:
    with pytest.raises(ConfigurationError, match=code):
        parse_validation_policy(raw)


def test_oversized_policy_and_invalid_utf8_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="INPUT_TOO_LARGE"):
        parse_validation_policy(b" " * (64 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="INVALID_UTF8"):
        parse_validation_policy(b"\xff")


@pytest.mark.parametrize(
    "replacement,code",
    [
        ("not-a-date", "INVALID_TIMESTAMP"),
        ("2026-01-01T00:00:00.123Z", "INVALID_TIMESTAMP"),
        ("2026-01-01T00:00:00Z", "KEY_FINGERPRINT_MISMATCH"),
        ("", "INVALID_PROVIDER_STATUS"),
    ],
)
def test_registry_rejects_invalid_date_fingerprint_and_status(
    replacement: str, code: str
) -> None:
    raw = (FIXTURES / "valid/attestors.yaml").read_text()
    if code == "INVALID_TIMESTAMP":
        raw = raw.replace("2026-01-01T00:00:00Z", replacement)
    elif code == "KEY_FINGERPRINT_MISMATCH":
        raw = re.sub(r"key_id: sha256:[0-9a-f]{64}", "key_id: sha256:" + "0" * 64, raw)
    else:
        raw = raw.replace("status: active", "status: unknown")
    with pytest.raises(ConfigurationError, match=code):
        parse_attestors_registry(raw.encode())
