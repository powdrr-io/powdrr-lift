"""Source-backed semantic interpretation, separate from repository binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SOURCE_INTERPRETATION_SCHEMA_VERSION = "source-interpretation-v1"
BEHAVIOR_FORMS = frozenset(
    {
        "interface",
        "transformation",
        "state_transition",
        "invariant",
        "rejection",
        "compatibility",
        "guidance",
        "unclear",
    }
)


class SourceInterpretationError(ValueError):
    """Raised when an interpretation is unsupported or incomplete."""


@dataclass(frozen=True, slots=True)
class SourceInterpretation:
    source_refs: tuple[str, ...]
    subject: str | None
    operation: str | None
    affected_value: str | None
    rule: str | None
    contrast: str | None
    behavior_form: str
    result_presence: str
    event_scope: str
    contrast_presence: str
    conditions: tuple[str, ...]
    exceptions: tuple[str, ...]
    unresolved_fields: tuple[tuple[str, str], ...]
    field_evidence: tuple[tuple[str, str], ...]
    decision_fingerprints: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.source_refs or any(not item.strip() for item in self.source_refs):
            raise SourceInterpretationError("source interpretation needs source refs")
        if self.behavior_form not in BEHAVIOR_FORMS:
            raise SourceInterpretationError("behavior_form is invalid")
        if self.result_presence not in {"explicit", "entailed", "unspecified"}:
            raise SourceInterpretationError("result_presence is invalid")
        if self.event_scope not in {
            "single_event",
            "event_sequence",
            "continuous",
            "unspecified",
        }:
            raise SourceInterpretationError("event_scope is invalid")
        if self.contrast_presence not in {"explicit", "absent"}:
            raise SourceInterpretationError("contrast_presence is invalid")
        if self.contrast_presence == "absent" and self.contrast is not None:
            raise SourceInterpretationError("absent contrast must be null")
        unresolved = dict(self.unresolved_fields)
        if len(unresolved) != len(self.unresolved_fields):
            raise SourceInterpretationError("unresolved fields contain duplicates")
        for field in ("subject", "operation", "affected_value", "rule", "contrast"):
            value = getattr(self, field)
            if value is not None and not value.strip():
                raise SourceInterpretationError(f"{field} must be null or non-empty")
            if (
                value is None
                and not (field == "contrast" and self.contrast_presence == "absent")
                and field not in unresolved
            ):
                raise SourceInterpretationError(
                    f"null {field} must include an unresolved reason"
                )
        if self.behavior_form == "unclear" and "behavior_form" not in unresolved:
            raise SourceInterpretationError(
                "unclear behavior_form must include an unresolved reason"
            )
        if self.contrast_presence == "explicit" and self.contrast is None:
            raise SourceInterpretationError(
                "explicit contrast needs a contrast interpretation"
            )
        if any(not reason.strip() for _, reason in self.unresolved_fields):
            raise SourceInterpretationError("unresolved-field reason is empty")
        if any(not quote.strip() for _, quote in self.field_evidence):
            raise SourceInterpretationError("field evidence is empty")
        if any(
            not name.strip() or not value.strip()
            for name, value in self.decision_fingerprints
        ):
            raise SourceInterpretationError("decision fingerprint is invalid")

    @property
    def meaning_status(self) -> str:
        core_fields = {"subject", "operation", "rule", "behavior_form"}
        return (
            "unresolved"
            if core_fields.intersection(dict(self.unresolved_fields))
            else "interpreted"
        )

    def validate_source(self, source_ref: str, source_text: str) -> None:
        if self.source_refs != (source_ref,):
            raise SourceInterpretationError("source interpretation refs are stale")
        evidence_fields: set[str] = set()
        for field, quote in self.field_evidence:
            if field not in {
                "subject",
                "operation",
                "affected_value",
                "rule",
                "contrast",
            }:
                raise SourceInterpretationError("field evidence names an unknown field")
            if quote not in source_text:
                raise SourceInterpretationError(
                    f"{field} evidence is not present in the source"
                )
            evidence_fields.add(field)
        for field in ("subject", "operation", "affected_value", "rule", "contrast"):
            if getattr(self, field) is not None and field not in evidence_fields:
                raise SourceInterpretationError(f"{field} has no source evidence")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_INTERPRETATION_SCHEMA_VERSION,
            "source_refs": list(self.source_refs),
            "subject": self.subject,
            "operation": self.operation,
            "affected_value": self.affected_value,
            "rule": self.rule,
            "contrast": self.contrast,
            "behavior_form": self.behavior_form,
            "result_presence": self.result_presence,
            "event_scope": self.event_scope,
            "contrast_presence": self.contrast_presence,
            "conditions": list(self.conditions),
            "exceptions": list(self.exceptions),
            "unresolved_fields": [
                f"{field}|{reason}" for field, reason in self.unresolved_fields
            ],
            "field_evidence": [
                f"{field}|{quote}" for field, quote in self.field_evidence
            ],
            "decision_fingerprints": dict(self.decision_fingerprints),
            "meaning_status": self.meaning_status,
        }

    @classmethod
    def bind(
        cls,
        raw: Mapping[str, Any],
        *,
        source_ref: str,
        source_text: str,
        conditions: Sequence[str],
        exceptions: Sequence[str],
        decision_fingerprints: Mapping[str, str],
    ) -> SourceInterpretation:
        allowed = {
            "subject",
            "operation",
            "affected_value",
            "rule",
            "contrast",
            "behavior_form",
            "result_presence",
            "event_scope",
            "contrast_presence",
            "unresolved_fields",
            "field_evidence",
        }
        if set(raw) != allowed:
            raise SourceInterpretationError(
                "source interpretation response has missing or extra fields"
            )
        unresolved = _parse_pairs(raw["unresolved_fields"], "unresolved_fields")
        evidence = _parse_pairs(raw["field_evidence"], "field_evidence")
        values: dict[str, str | None] = {}
        for name in ("subject", "operation", "affected_value", "rule", "contrast"):
            value = raw.get(name)
            if value is not None and not isinstance(value, str):
                raise SourceInterpretationError(f"{name} must be a string or null")
            values[name] = value
        fields = cls(
            source_refs=(source_ref,),
            subject=values["subject"],
            operation=values["operation"],
            affected_value=values["affected_value"],
            rule=values["rule"],
            contrast=values["contrast"],
            behavior_form=_required_string(raw, "behavior_form"),
            result_presence=_required_string(raw, "result_presence"),
            event_scope=_required_string(raw, "event_scope"),
            contrast_presence=_required_string(raw, "contrast_presence"),
            conditions=tuple(conditions),
            exceptions=tuple(exceptions),
            unresolved_fields=unresolved,
            field_evidence=evidence,
            decision_fingerprints=tuple(sorted(decision_fingerprints.items())),
        )
        fields.validate_source(source_ref, source_text)
        return fields

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceInterpretation:
        if raw.get("schema_version") != SOURCE_INTERPRETATION_SCHEMA_VERSION:
            raise SourceInterpretationError("unsupported source interpretation schema")
        refs = raw.get("source_refs")
        conditions = raw.get("conditions")
        exceptions = raw.get("exceptions")
        decision_fingerprints = raw.get("decision_fingerprints")
        if (
            not isinstance(refs, list)
            or not all(isinstance(item, str) for item in refs)
            or not isinstance(conditions, list)
            or not all(isinstance(item, str) for item in conditions)
            or not isinstance(exceptions, list)
            or not all(isinstance(item, str) for item in exceptions)
            or not isinstance(decision_fingerprints, Mapping)
        ):
            raise SourceInterpretationError(
                "source interpretation provenance is invalid"
            )
        bound = cls(
            source_refs=tuple(refs),
            subject=_optional_string(raw, "subject"),
            operation=_optional_string(raw, "operation"),
            affected_value=_optional_string(raw, "affected_value"),
            rule=_optional_string(raw, "rule"),
            contrast=_optional_string(raw, "contrast"),
            behavior_form=_required_string(raw, "behavior_form"),
            result_presence=_required_string(raw, "result_presence"),
            event_scope=_required_string(raw, "event_scope"),
            contrast_presence=_required_string(raw, "contrast_presence"),
            conditions=tuple(conditions),
            exceptions=tuple(exceptions),
            unresolved_fields=_parse_pairs(
                raw.get("unresolved_fields"), "unresolved_fields"
            ),
            field_evidence=_parse_pairs(raw.get("field_evidence"), "field_evidence"),
            decision_fingerprints=tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in decision_fingerprints.items()
                )
            ),
        )
        if raw.get("meaning_status") != bound.meaning_status:
            raise SourceInterpretationError("source interpretation status is stale")
        return bound


def _parse_pairs(raw: Any, name: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise SourceInterpretationError(f"{name} must be a list")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, str) or "|" not in item:
            raise SourceInterpretationError(f"{name} entry is malformed")
        key, value = item.split("|", 1)
        if not key.strip() or not value.strip():
            raise SourceInterpretationError(f"{name} entry is empty")
        pairs.append((key.strip(), value.strip()))
    if len(dict(pairs)) != len(pairs):
        raise SourceInterpretationError(f"{name} contains duplicate fields")
    return tuple(pairs)


def _required_string(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SourceInterpretationError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(raw: Mapping[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is not None and not isinstance(value, str):
        raise SourceInterpretationError(f"{name} must be a string or null")
    return value


__all__ = ["SourceInterpretation", "SourceInterpretationError"]
