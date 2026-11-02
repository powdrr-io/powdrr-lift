"""Provider-neutral executable verification contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

VERIFICATION_CONTRACT_SCHEMA_VERSION = "verification-contract-v1"
VERIFICATION_CONTRACT_FIELDS = frozenset(
    {
        "intent_refs",
        "provider",
        "selector",
        "profile",
        "expectation",
        "applicability",
        "protected_inputs",
        "status",
    }
)
REQUIRED_VERIFICATION_CONTRACT_FIELDS = (
    "intent_refs",
    "provider",
    "selector",
    "profile",
    "expectation",
    "status",
)
VERIFICATION_EXPECTATIONS = frozenset({"pass", "absent"})
VERIFICATION_STATUSES = frozenset({"active", "superseded"})


@dataclass(frozen=True, slots=True)
class VerificationContract:
    """A required test case plus the information needed to execute it."""

    contract_id: str
    description: str
    intent_refs: tuple[str, ...] = ()
    provider: str | None = None
    selector: str | None = None
    profile: str | None = None
    expectation: str | None = None
    applicability: Mapping[str, Any] | None = None
    protected_inputs: tuple[str, ...] = ()
    status: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VerificationContract:
        return cls(
            contract_id=_text(value.get("id")),
            description=_text(value.get("description")),
            intent_refs=_string_sequence(value.get("intent_refs")),
            provider=_optional_text(value.get("provider")),
            selector=_optional_text(value.get("selector")),
            profile=_optional_text(value.get("profile")),
            expectation=_optional_text(value.get("expectation")),
            applicability=_optional_mapping(value.get("applicability")),
            protected_inputs=_string_sequence(value.get("protected_inputs")),
            status=_optional_text(value.get("status")),
        )

    @property
    def is_complete(self) -> bool:
        return not self.validation_errors()

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.intent_refs:
            errors.append("intent_refs must contain at least one intent id")
        if self.provider is None:
            errors.append("provider is required")
        if self.selector is None:
            errors.append("selector is required")
        if self.profile is None:
            errors.append("profile is required")
        if self.expectation is None:
            errors.append("expectation is required")
        elif self.expectation not in VERIFICATION_EXPECTATIONS:
            errors.append(
                "expectation must be one of: "
                + ", ".join(sorted(VERIFICATION_EXPECTATIONS))
            )
        if self.status is None:
            errors.append("status is required")
        elif self.status not in VERIFICATION_STATUSES:
            errors.append(
                "status must be one of: " + ", ".join(sorted(VERIFICATION_STATUSES))
            )
        if self.applicability is not None:
            mode = self.applicability.get("mode")
            if not isinstance(mode, str) or not mode.strip():
                errors.append(
                    "applicability.mode is required when applicability is set"
                )
        return tuple(errors)

    @staticmethod
    def mapping_validation_errors(value: Mapping[str, Any]) -> tuple[str, ...]:
        """Validate field shapes before normalizing a contract mapping."""
        errors: list[str] = []
        for field_name in ("intent_refs", "protected_inputs"):
            if field_name not in value:
                continue
            raw = value[field_name]
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                errors.append(f"{field_name} must be an array of strings")
            elif any(not isinstance(item, str) or not item.strip() for item in raw):
                errors.append(f"{field_name} must contain non-empty strings")
        if "applicability" in value and not isinstance(value["applicability"], Mapping):
            errors.append("applicability must be an object")
        return tuple(errors)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.contract_id,
            "description": self.description,
        }
        data.update(
            {
                "intent_refs": list(self.intent_refs),
                "provider": self.provider,
                "selector": self.selector,
                "profile": self.profile,
                "expectation": self.expectation,
                "status": self.status,
            }
        )
        if self.applicability is not None:
            data["applicability"] = dict(self.applicability)
        if self.protected_inputs:
            data["protected_inputs"] = list(self.protected_inputs)
        return data

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": VERIFICATION_CONTRACT_SCHEMA_VERSION,
            **self.to_data(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _string_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _optional_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


__all__ = [
    "REQUIRED_VERIFICATION_CONTRACT_FIELDS",
    "VERIFICATION_CONTRACT_FIELDS",
    "VERIFICATION_CONTRACT_SCHEMA_VERSION",
    "VERIFICATION_EXPECTATIONS",
    "VERIFICATION_STATUSES",
    "VerificationContract",
]
