"""Deterministic, operation-scoped context for coding-agent work orders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

INTENT_PACKET_SCHEMA_VERSION = "intent-packet-v1"


@dataclass(frozen=True, slots=True)
class IntentPacket:
    """The smallest explicit intent closure needed for one execution unit."""

    operation_id: str
    required_operations: tuple[Mapping[str, Any], ...] = ()
    must_preserve: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    selection_explanations: tuple[str, ...] = ()
    schema_version: str = INTENT_PACKET_SCHEMA_VERSION

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "required_operations": [
                _canonical(dict(operation)) for operation in self.required_operations
            ],
            "must_preserve": list(self.must_preserve),
            "non_goals": list(self.non_goals),
            "source_refs": list(self.source_refs),
            "selection_explanations": list(self.selection_explanations),
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_data(include_fingerprint=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> IntentPacket:
        if data.get("schema_version") != INTENT_PACKET_SCHEMA_VERSION:
            raise ValueError("unsupported intent packet schema version")
        raw_operations = data.get("required_operations", ())
        if not isinstance(raw_operations, Sequence) or isinstance(
            raw_operations, (str, bytes)
        ):
            raise ValueError("intent packet required_operations must be a list")
        if not all(isinstance(item, Mapping) for item in raw_operations):
            raise ValueError("intent packet operations must be mappings")
        packet = cls(
            operation_id=_required_text(data, "operation_id"),
            required_operations=tuple(raw_operations),
            must_preserve=_text_items(data.get("must_preserve", ())),
            non_goals=_text_items(data.get("non_goals", ())),
            source_refs=_text_items(data.get("source_refs", ())),
            selection_explanations=_text_items(data.get("selection_explanations", ())),
        )
        if data.get("fingerprint") != packet.fingerprint:
            raise ValueError("intent packet fingerprint does not match content")
        return packet

    def render(self) -> str:
        """Render stable, model-facing instructions for this packet."""
        required = _render_mappings(self.required_operations)
        preserve = _render_text(self.must_preserve)
        non_goals = _render_text(self.non_goals)
        sources = _render_text(self.source_refs)
        explanations = _render_text(self.selection_explanations)
        return (
            f"Operation-scoped intent packet: {self.operation_id}\n"
            f"Packet fingerprint: {self.fingerprint}\n"
            "Required operations (implement these and no other product changes):\n"
            f"{required}\n"
            "Must preserve (do not weaken or remove):\n"
            f"{preserve}\n"
            "Explicit non-goals (do not implement):\n"
            f"{non_goals}\n"
            "Relevant source references:\n"
            f"{sources}\n"
            "Why this context is included:\n"
            f"{explanations}"
        )


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def _required_text(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"intent packet field {name!r} must be non-empty text")
    return value


def _text_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("intent packet text collections must be lists")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("intent packet text collections must contain non-empty text")
    return tuple(item.strip() for item in value)


def _render_mappings(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "- None declared. Do not invent additional product changes."
    return "\n".join(
        f"- {json.dumps(_canonical(dict(item)), sort_keys=True, ensure_ascii=False)}"
        for item in items
    )


def _render_text(items: Sequence[str]) -> str:
    if not items:
        return "- None declared."
    return "\n".join(f"- {item}" for item in items)


__all__ = ["INTENT_PACKET_SCHEMA_VERSION", "IntentPacket"]
