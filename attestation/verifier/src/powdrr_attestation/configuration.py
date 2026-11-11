"""Strict, network-free parsers for trusted policy and attestor YAML blobs."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import yaml
from yaml.events import AliasEvent
from yaml.tokens import AnchorToken, AliasToken, TagToken

POLICY_MAX_BYTES = 64 * 1024
REGISTRY_MAX_BYTES = 256 * 1024
MAX_STEPS = 32
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_ENV = ("DYLD_", "LD_")
_FORBIDDEN_EXACT = {
    "PATH",
    "HOME",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "TMPDIR",
}
_RESERVED_ENV = "POWDRR_ATTEST_"


class ConfigurationError(ValueError):
    """An external protocol/configuration value is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class ValidationStep:
    step_id: str
    argv: tuple[str, ...] | None
    shell: str | None
    cwd: str
    env: tuple[tuple[str, str], ...]
    timeout_seconds: int | None


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    version: int
    max_age_seconds: int
    toolchain: str
    network: str
    default_timeout_seconds: int
    steps: tuple[ValidationStep, ...]
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class Attestor:
    username: str
    github_user_id: int
    provider_id: str
    key_id: str
    algorithm: str
    public_key: bytes
    status: str
    not_before: datetime
    not_after: datetime | None


@dataclass(frozen=True, slots=True)
class AttestorsRegistry:
    version: int
    users: tuple[Attestor, ...]
    raw_sha256: str


def parse_validation_policy(raw: bytes) -> ValidationPolicy:
    """Parse policy bytes without normalizing the bytes used for their digest."""
    document = _load_yaml(raw, max_bytes=POLICY_MAX_BYTES)
    _keys(
        document, {"version", "freshness", "environment", "defaults", "steps"}, "policy"
    )
    version = _integer(document, "version", "policy")
    if version != 1:
        raise ConfigurationError("UNSUPPORTED_POLICY_VERSION")
    freshness = _object(document, "freshness", "policy")
    _keys(freshness, {"max_age_seconds"}, "freshness")
    max_age = _integer(freshness, "max_age_seconds", "freshness")
    _range(max_age, 300, 604800, "freshness.max_age_seconds")
    environment = _object(document, "environment", "policy")
    _keys(environment, {"toolchain", "network"}, "environment")
    toolchain = _string(environment, "toolchain", "environment", nonempty=True)
    network = _string(environment, "network", "environment")
    if network != "allowed":
        raise ConfigurationError("UNSUPPORTED_NETWORK_POLICY")
    defaults = _object(document, "defaults", "policy")
    _keys(defaults, {"timeout_seconds"}, "defaults")
    default_timeout = _integer(defaults, "timeout_seconds", "defaults")
    _range(default_timeout, 1, 14400, "defaults.timeout_seconds")
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > MAX_STEPS:
        raise ConfigurationError("INVALID_STEP_COUNT")
    steps: list[ValidationStep] = []
    identities: set[str] = set()
    for index, value in enumerate(raw_steps):
        where = f"steps[{index}]"
        if not isinstance(value, dict):
            raise ConfigurationError(f"{where} must be a mapping")
        _keys(value, {"id", "argv", "shell", "cwd", "env", "timeout_seconds"}, where)
        step_id = _string(value, "id", where)
        if not _ID.fullmatch(step_id) or step_id in identities:
            raise ConfigurationError(f"INVALID_STEP_ID:{step_id}")
        identities.add(step_id)
        has_argv, has_shell = "argv" in value, "shell" in value
        if has_argv == has_shell:
            raise ConfigurationError(f"INVALID_STEP_COMMAND:{step_id}")
        argv: tuple[str, ...] | None = None
        shell: str | None = None
        if has_argv:
            command = value["argv"]
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(token, str) or not token for token in command)
            ):
                raise ConfigurationError(f"INVALID_ARGV:{step_id}")
            argv = tuple(command)
        else:
            shell_value = value["shell"]
            if (
                not isinstance(shell_value, str)
                or not shell_value.strip()
                or len(shell_value) > 16384
            ):
                raise ConfigurationError(f"INVALID_SHELL:{step_id}")
            shell = shell_value
        cwd = value.get("cwd", ".")
        if (
            not isinstance(cwd, str)
            or not cwd
            or cwd.startswith("/")
            or "\\" in cwd
            or any(part in {"..", ".git"} for part in cwd.split("/"))
        ):
            raise ConfigurationError(f"INVALID_CWD:{step_id}")
        raw_env = value.get("env", {})
        if not isinstance(raw_env, dict):
            raise ConfigurationError(f"INVALID_ENV:{step_id}")
        env: list[tuple[str, str]] = []
        for key, item in raw_env.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(item, str)
            ):
                raise ConfigurationError(f"INVALID_ENV:{step_id}")
            if (
                key in _FORBIDDEN_EXACT
                or key.startswith(_FORBIDDEN_ENV)
                or key.startswith(_RESERVED_ENV)
            ):
                raise ConfigurationError(f"FORBIDDEN_ENVIRONMENT_VARIABLE:{key}")
            env.append((key, item))
        timeout = value.get("timeout_seconds")
        if timeout is not None:
            if type(timeout) is not int:
                raise ConfigurationError(f"INVALID_TIMEOUT:{step_id}")
            _range(timeout, 1, 14400, f"steps[{index}].timeout_seconds")
        steps.append(
            ValidationStep(step_id, argv, shell, cwd, tuple(sorted(env)), timeout)
        )
    return ValidationPolicy(
        version,
        max_age,
        toolchain,
        network,
        default_timeout,
        tuple(steps),
        _digest(raw),
    )


def parse_attestors_registry(raw: bytes) -> AttestorsRegistry:
    document = _load_yaml(raw, max_bytes=REGISTRY_MAX_BYTES)
    _keys(document, {"version", "users"}, "registry")
    version = _integer(document, "version", "registry")
    if version != 1:
        raise ConfigurationError("UNSUPPORTED_REGISTRY_VERSION")
    users = document.get("users")
    if not isinstance(users, dict):
        raise ConfigurationError("INVALID_USERS")
    result: list[Attestor] = []
    account_ids: set[int] = set()
    key_ids: set[str] = set()
    for username, user in users.items():
        if (
            not isinstance(username, str)
            or not username
            or len(username) > 128
            or not isinstance(user, dict)
        ):
            raise ConfigurationError("INVALID_USER_RECORD")
        _keys(user, {"github_user_id", "providers"}, f"users.{username}")
        account = _integer(user, "github_user_id", f"users.{username}")
        if account <= 0 or account in account_ids:
            raise ConfigurationError("DUPLICATE_GITHUB_USER_ID")
        account_ids.add(account)
        providers = user.get("providers")
        if not isinstance(providers, list) or not providers or len(providers) > 64:
            raise ConfigurationError(f"INVALID_PROVIDERS:{username}")
        local: set[str] = set()
        for provider in providers:
            if not isinstance(provider, dict):
                raise ConfigurationError("INVALID_PROVIDER_RECORD")
            _keys(
                provider,
                {
                    "provider_id",
                    "key_id",
                    "algorithm",
                    "public_key",
                    "status",
                    "not_before",
                    "not_after",
                },
                "provider",
            )
            provider_id = _string(provider, "provider_id", "provider")
            key_id = _string(provider, "key_id", "provider")
            if not _PROVIDER_ID.fullmatch(provider_id) or provider_id in local:
                raise ConfigurationError("DUPLICATE_PROVIDER_ID")
            local.add(provider_id)
            if not _KEY_ID.fullmatch(key_id) or key_id in key_ids:
                raise ConfigurationError("DUPLICATE_OR_INVALID_KEY_ID")
            key_ids.add(key_id)
            if _string(provider, "algorithm", "provider") != "ed25519":
                raise ConfigurationError("UNSUPPORTED_KEY_ALGORITHM")
            encoded = _string(provider, "public_key", "provider")
            try:
                public_key = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise ConfigurationError("INVALID_PUBLIC_KEY_BASE64") from error
            if (
                len(public_key) != 32
                or "sha256:" + hashlib.sha256(public_key).hexdigest() != key_id
            ):
                raise ConfigurationError("KEY_FINGERPRINT_MISMATCH")
            status = _string(provider, "status", "provider")
            if status not in {"active", "revoked"}:
                raise ConfigurationError("INVALID_PROVIDER_STATUS")
            not_before = _timestamp(provider, "not_before", required=True)
            not_after = _timestamp(provider, "not_after", required=False)
            if not_before is None or (
                not_after is not None and not_after <= not_before
            ):
                raise ConfigurationError("INVALID_PROVIDER_VALIDITY_WINDOW")
            result.append(
                Attestor(
                    username,
                    account,
                    provider_id,
                    key_id,
                    "ed25519",
                    public_key,
                    status,
                    not_before,
                    not_after,
                )
            )
    return AttestorsRegistry(version, tuple(result), _digest(raw))


def _load_yaml(raw: bytes, *, max_bytes: int) -> dict[str, Any]:
    if len(raw) > max_bytes:
        raise ConfigurationError("INPUT_TOO_LARGE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError("INVALID_UTF8") from error
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AnchorToken, AliasToken, TagToken)):
                raise ConfigurationError("YAML_FEATURE_FORBIDDEN")
        events = yaml.parse(text)
        for event in events:
            if isinstance(event, AliasEvent):
                raise ConfigurationError("YAML_ALIAS_FORBIDDEN")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except ConfigurationError:
        raise
    except yaml.YAMLError as error:
        raise ConfigurationError("INVALID_YAML") from error
    if not isinstance(value, dict):
        raise ConfigurationError("ROOT_MUST_BE_MAPPING")
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise ConfigurationError("UNHASHABLE_MAPPING_KEY") from error
            if duplicate:
                raise ConfigurationError(f"DUPLICATE_MAPPING_KEY:{key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigurationError(
            f"UNKNOWN_FIELD:{where}:{','.join(sorted(map(str, unknown)))}"
        )


def _object(value: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    child = value.get(key)
    if not isinstance(child, dict):
        raise ConfigurationError(f"INVALID_OBJECT:{where}.{key}")
    return child


def _integer(value: Mapping[str, Any], key: str, where: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise ConfigurationError(f"INVALID_INTEGER:{where}.{key}")
    return item


def _string(
    value: Mapping[str, Any], key: str, where: str, *, nonempty: bool = True
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or (nonempty and not item):
        raise ConfigurationError(f"INVALID_STRING:{where}.{key}")
    return item


def _timestamp(
    value: Mapping[str, Any], key: str, *, required: bool
) -> datetime | None:
    raw = value.get(key)
    if raw is None and not required:
        return None
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise ConfigurationError(f"INVALID_TIMESTAMP:{key}")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"INVALID_TIMESTAMP:{key}") from error
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0 or parsed.microsecond != 0:
        raise ConfigurationError(f"INVALID_TIMESTAMP:{key}")
    return parsed


def _range(value: int, minimum: int, maximum: int, where: str) -> None:
    if value < minimum or value > maximum:
        raise ConfigurationError(f"OUT_OF_RANGE:{where}")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
