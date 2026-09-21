"""Compact, compiler-owned context packets for code-editing workers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepositoryContextPacket:
    """Stable repository facts supplied to a coding worker."""

    allowed_paths: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    existing_tests: tuple[Mapping[str, Any], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "validation_profiles": list(self.validation_profiles),
            "existing_tests": [dict(item) for item in self.existing_tests],
        }


@dataclass(frozen=True, slots=True)
class ImplementationPacket:
    """Minimal semantic handoff; structural identity stays with Powdrr."""

    objective: str
    obligations: tuple[str, ...]
    required_tests: tuple[Mapping[str, Any], ...]
    repository: RepositoryContextPacket

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "implementation-packet-v1",
            "objective": self.objective,
            "obligations": [
                {"ordinal": index, "description": description}
                for index, description in enumerate(self.obligations, start=1)
            ],
            "required_tests": [
                {
                    "ordinal": index,
                    "description": str(item.get("description", "")),
                    "provider": str(item.get("provider", "")),
                    "profile": str(item.get("profile", "")),
                    "name_hint": str(item.get("name_hint", "")),
                    "selector": str(item.get("selector", "")),
                }
                for index, item in enumerate(self.required_tests, start=1)
            ],
            "repository": self.repository.to_data(),
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ImplementationPacket:
        if raw.get("schema_version") != "implementation-packet-v1":
            raise ValueError("unsupported implementation packet schema")
        raw_obligations = raw.get("obligations")
        raw_tests = raw.get("required_tests")
        repository = raw.get("repository")
        if not isinstance(raw_obligations, list) or not isinstance(raw_tests, list):
            raise ValueError("implementation packet is incomplete")
        if not isinstance(repository, Mapping):
            raise ValueError("implementation packet repository context is missing")
        obligations = tuple(
            str(item["description"])
            for item in raw_obligations
            if isinstance(item, Mapping) and isinstance(item.get("description"), str)
        )
        tests = tuple(
            {
                key: item.get(key, "")
                for key in (
                    "description",
                    "provider",
                    "profile",
                    "name_hint",
                    "selector",
                )
            }
            for item in raw_tests
            if isinstance(item, Mapping)
        )
        existing = repository.get("existing_tests", [])
        if not isinstance(existing, list):
            raise ValueError("implementation packet existing tests are malformed")
        packet = cls(
            objective=str(raw.get("objective", "")),
            obligations=obligations,
            required_tests=tests,
            repository=RepositoryContextPacket(
                allowed_paths=tuple(
                    str(item) for item in repository.get("allowed_paths", [])
                ),
                validation_profiles=tuple(
                    str(item) for item in repository.get("validation_profiles", [])
                ),
                existing_tests=tuple(
                    item for item in existing if isinstance(item, Mapping)
                ),
            ),
        )
        if not packet.objective.strip() or not packet.obligations:
            raise ValueError("implementation packet is missing required content")
        return packet

    def render(self) -> str:
        tests = self.to_data()["required_tests"]
        test_lines = [
            f"- required new test {item['ordinal']:03d}: function name must start "
            f"with `{item['name_hint']}` "
            f"({item['provider']}/{item['profile']}) — {item['description']}"
            for item in tests
        ] or ["- none"]
        obligation_lines = [
            f"- obligation {index:03d}: {description}"
            for index, description in enumerate(self.obligations, start=1)
        ] or ["- none"]
        return "\n".join(
            (
                "Implement the requested feature using this bounded packet.",
                "Original feature description:",
                self.objective,
                "\nObligations (in compiler order):",
                *obligation_lines,
                "\nRequired new tests (create at least one matching test per "
                "obligation):",
                *test_lines,
                "\nImplement only the original description and obligations above. "
                "Create the listed new tests, using the name hints as prefixes; "
                "optional suffixes such as `_sync` and `_async` are allowed. "
                "Run the focused new tests, then the repository validation, and "
                "stop.",
            )
        )


def compile_implementation_packet(
    *,
    objective: str,
    obligations: Sequence[str],
    required_tests: Sequence[Mapping[str, Any]],
    allowed_paths: Sequence[str],
    validation_profiles: Sequence[str],
    existing_tests: Sequence[Mapping[str, Any]] = (),
) -> ImplementationPacket:
    """Normalize worker inputs and reject incomplete executable contracts."""
    if not objective.strip():
        raise ValueError("implementation packet objective must not be empty")
    normalized_obligations = tuple(
        item.strip() for item in obligations if isinstance(item, str) and item.strip()
    )
    if not normalized_obligations:
        raise ValueError("implementation packet requires obligations")
    normalized_tests: list[Mapping[str, Any]] = []
    for item in required_tests:
        if not isinstance(item, Mapping):
            raise ValueError("implementation packet test contract is malformed")
        for key in ("provider", "profile"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"implementation packet test lacks {key}")
        name_hint = item.get("name_hint")
        if not isinstance(name_hint, str) or not name_hint.strip():
            description = item.get("description", "")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("implementation packet test lacks name_hint")
            slug = re.sub(r"[^a-z0-9]+", "_", description.casefold()).strip("_")
            name_hint = f"test_{slug[:100].rstrip('_')}"
        normalized_tests.append(
            {
                key: item.get(key, "")
                for key in (
                    "description",
                    "provider",
                    "profile",
                    "selector",
                )
            }
            | {"name_hint": name_hint}
        )
    if not normalized_tests:
        raise ValueError("implementation packet requires test contracts")
    return ImplementationPacket(
        objective=objective.strip(),
        obligations=normalized_obligations,
        required_tests=tuple(normalized_tests),
        repository=RepositoryContextPacket(
            allowed_paths=tuple(dict.fromkeys(str(item) for item in allowed_paths)),
            validation_profiles=tuple(
                dict.fromkeys(str(item) for item in validation_profiles)
            ),
            existing_tests=tuple(
                {
                    key: item.get(key)
                    for key in ("provider", "profile", "selector", "fingerprint")
                }
                for item in existing_tests
                if isinstance(item, Mapping) and isinstance(item.get("selector"), str)
            ),
        ),
    )


__all__ = [
    "ImplementationPacket",
    "RepositoryContextPacket",
    "compile_implementation_packet",
]
