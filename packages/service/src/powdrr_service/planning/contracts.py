"""Strict, provider-neutral contracts for bounded planning questions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

QUESTION_KINDS = frozenset(
    {
        "ci_command_equivalence",
        "extract_requirements",
        "classify_source_block",
        "check_requirement_fidelity",
        "compare_requirement_equivalence",
        "assess_requirement_coverage",
        "assess_setup_state",
        "describe_requirement_validations",
        "propose_work_units",
        "check_unit_cohesion",
        "describe_item",
        "resolve_prerequisite_output",
        "confirm_dependency",
        "review_dependency_graph",
        "check_validation_coverage",
        "requires_human_setup",
        "describe_human_setup",
        "compare_setup_equivalence",
        "enumerate_structural_subjects",
        "describe_structural_change",
        "locate_repair_impact",
        "planning_question_batch",
    }
)

MAX_ENUMERATION_RECORDS = 8
MAX_CONTRACT_TEXT_LENGTH = 4_000
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLACEHOLDER_PATTERN = re.compile(
    r"(?:\{\{[^}]+\}\}|\$\{[^}]+\}|<\s*(?:TODO|TBD|PLACEHOLDER)[^>]*>)",
    re.IGNORECASE,
)

__all__ = [
    "QUESTION_KINDS",
    "MAX_ENUMERATION_RECORDS",
    "MAX_CONTRACT_TEXT_LENGTH",
    "PlanningContractError",
    "VALIDATION_ERROR_JSON_SCHEMA",
    "Question",
    "QUESTION_JSON_SCHEMA",
    "EnumerationAnswer",
    "ENUMERATION_ANSWER_JSON_SCHEMA",
    "MissingCoverage",
    "CompleteCoverage",
    "PartialCoverage",
    "CoverageAnswer",
    "parse_coverage_answer",
    "COVERAGE_ANSWER_JSON_SCHEMA",
    "HumanSetupNeedAnswer",
    "HumanSetupDescriptionAnswer",
    "HUMAN_SETUP_DESCRIPTION_JSON_SCHEMA",
]


class PlanningContractError(ValueError):
    """A contract error with a JSON pointer suitable for a correction request."""

    def __init__(self, json_pointer: str, message: str) -> None:
        self.json_pointer = json_pointer or "/"
        self.message = message
        super().__init__(f"{self.json_pointer}: {message}")

    def to_data(self) -> dict[str, str]:
        return {"json_pointer": self.json_pointer, "message": self.message}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PlanningContractError:
        _exact_fields(value, {"json_pointer", "message"}, "/")
        pointer = _text(value["json_pointer"], "/json_pointer")
        if not pointer.startswith("/"):
            raise PlanningContractError("/json_pointer", "must be a JSON pointer")
        return cls(pointer, _bounded_text(value["message"], "/message"))


VALIDATION_ERROR_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Planning validation error",
    "type": "object",
    "additionalProperties": False,
    "required": ["json_pointer", "message"],
    "properties": {
        "json_pointer": {"type": "string", "pattern": "^/"},
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
    },
}


@dataclass(frozen=True, slots=True)
class Question:
    """Runtime-owned identity and input binding for one bounded model request."""

    question_id: str
    kind: str
    contract_version: int
    subject_ids: tuple[str, ...]
    input_digest: str
    context_ref_ids: tuple[str, ...]
    expected_answer_schema: str
    attempt: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Question:
        _exact_fields(
            value,
            {
                "question_id",
                "kind",
                "contract_version",
                "subject_ids",
                "input_digest",
                "context_ref_ids",
                "expected_answer_schema",
                "attempt",
            },
            "/",
        )
        question_id = _identifier(value["question_id"], "/question_id")
        kind = _text(value["kind"], "/kind")
        if kind not in QUESTION_KINDS:
            raise PlanningContractError("/kind", "unknown planning question kind")
        version = _positive_int(value["contract_version"], "/contract_version")
        subject_ids = _text_array(value["subject_ids"], "/subject_ids")
        if not subject_ids:
            raise PlanningContractError("/subject_ids", "must contain at least one id")
        _unique(subject_ids, "/subject_ids")
        input_digest = _text(value["input_digest"], "/input_digest")
        if not _DIGEST_PATTERN.fullmatch(input_digest):
            raise PlanningContractError("/input_digest", "must be a sha256 digest")
        context_ref_ids = _text_array(value["context_ref_ids"], "/context_ref_ids")
        _unique(context_ref_ids, "/context_ref_ids")
        answer_schema = _identifier(
            value["expected_answer_schema"], "/expected_answer_schema"
        )
        attempt = _positive_int(value["attempt"], "/attempt")
        if attempt > 3:
            raise PlanningContractError("/attempt", "must be at most 3")
        return cls(
            question_id=question_id,
            kind=kind,
            contract_version=version,
            subject_ids=subject_ids,
            input_digest=input_digest,
            context_ref_ids=context_ref_ids,
            expected_answer_schema=answer_schema,
            attempt=attempt,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "kind": self.kind,
            "contract_version": self.contract_version,
            "subject_ids": list(self.subject_ids),
            "input_digest": self.input_digest,
            "context_ref_ids": list(self.context_ref_ids),
            "expected_answer_schema": self.expected_answer_schema,
            "attempt": self.attempt,
        }


QUESTION_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Planning question envelope",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "question_id",
        "kind",
        "contract_version",
        "subject_ids",
        "input_digest",
        "context_ref_ids",
        "expected_answer_schema",
        "attempt",
    ],
    "properties": {
        "question_id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
        "kind": {"type": "string", "enum": sorted(QUESTION_KINDS)},
        "contract_version": {"type": "integer", "minimum": 1},
        "subject_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "input_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "context_ref_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "expected_answer_schema": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9-]{0,63}$",
        },
        "attempt": {"type": "integer", "minimum": 1, "maximum": 3},
    },
}


@dataclass(frozen=True, slots=True)
class EnumerationAnswer:
    """One bounded page from a model's finite enumeration."""

    records: tuple[Mapping[str, Any], ...]
    has_more: bool
    next_anchor: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EnumerationAnswer:
        _exact_fields(value, {"records", "has_more", "next_anchor"}, "/")
        raw_records = value["records"]
        if not isinstance(raw_records, Sequence) or isinstance(
            raw_records, (str, bytes)
        ):
            raise PlanningContractError("/records", "must be an array")
        if len(raw_records) > MAX_ENUMERATION_RECORDS:
            raise PlanningContractError(
                "/records", f"must contain at most {MAX_ENUMERATION_RECORDS} records"
            )
        if any(not isinstance(record, Mapping) for record in raw_records):
            raise PlanningContractError("/records", "each record must be an object")
        has_more = _boolean(value["has_more"], "/has_more")
        anchor_raw = value["next_anchor"]
        anchor = None if anchor_raw is None else _text(anchor_raw, "/next_anchor")
        if has_more and not anchor:
            raise PlanningContractError(
                "/next_anchor", "is required when has_more is true"
            )
        if not has_more and anchor is not None:
            raise PlanningContractError(
                "/next_anchor", "must be null on the final page"
            )
        return cls(
            records=tuple(dict(record) for record in raw_records),
            has_more=has_more,
            next_anchor=anchor,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "records": [dict(record) for record in self.records],
            "has_more": self.has_more,
            "next_anchor": self.next_anchor,
        }


ENUMERATION_ANSWER_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Bounded enumeration answer",
    "type": "object",
    "additionalProperties": False,
    "required": ["records", "has_more", "next_anchor"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": MAX_ENUMERATION_RECORDS,
            "items": {"type": "object"},
        },
        "has_more": {"type": "boolean"},
        "next_anchor": {"type": ["string", "null"], "minLength": 1},
    },
    "allOf": [
        {
            "if": {"properties": {"has_more": {"const": True}}},
            "then": {"properties": {"next_anchor": {"type": "string", "minLength": 1}}},
            "else": {"properties": {"next_anchor": {"type": "null"}}},
        }
    ],
}


@dataclass(frozen=True, slots=True)
class MissingCoverage:
    evidence_ids: tuple[str, ...]
    reason: str
    status: Literal["missing"] = "missing"


@dataclass(frozen=True, slots=True)
class CompleteCoverage:
    evidence_ids: tuple[str, ...]
    reason: str
    status: Literal["complete"] = "complete"


@dataclass(frozen=True, slots=True)
class PartialCoverage:
    evidence_ids: tuple[str, ...]
    reason: str
    remaining_statement: str
    status: Literal["partial"] = "partial"


type CoverageAnswer = MissingCoverage | CompleteCoverage | PartialCoverage


def parse_coverage_answer(value: Mapping[str, Any]) -> CoverageAnswer:
    """Parse one requirement coverage answer without cross-variant placeholders."""
    status = value.get("status")
    if status == "missing":
        _exact_fields(value, {"status", "evidence_ids", "reason"}, "/")
        evidence_ids = _text_array(value["evidence_ids"], "/evidence_ids")
        _unique(evidence_ids, "/evidence_ids")
        if evidence_ids:
            raise PlanningContractError(
                "/evidence_ids", "missing coverage cannot cite supporting evidence"
            )
        return MissingCoverage(evidence_ids, _reason(value["reason"], "/reason"))
    if status == "complete":
        _exact_fields(value, {"status", "evidence_ids", "reason"}, "/")
        evidence_ids = _text_array(value["evidence_ids"], "/evidence_ids")
        _unique(evidence_ids, "/evidence_ids")
        if not evidence_ids:
            raise PlanningContractError(
                "/evidence_ids", "complete coverage needs at least one evidence id"
            )
        return CompleteCoverage(evidence_ids, _reason(value["reason"], "/reason"))
    if status == "partial":
        _exact_fields(
            value,
            {"status", "evidence_ids", "reason", "remaining_statement"},
            "/",
        )
        evidence_ids = _text_array(value["evidence_ids"], "/evidence_ids")
        _unique(evidence_ids, "/evidence_ids")
        if not evidence_ids:
            raise PlanningContractError(
                "/evidence_ids", "partial coverage needs implementation evidence"
            )
        remainder = _bounded_text(value["remaining_statement"], "/remaining_statement")
        return PartialCoverage(
            evidence_ids,
            _reason(value["reason"], "/reason"),
            remainder,
        )
    raise PlanningContractError("/status", "must be missing, complete, or partial")


COVERAGE_ANSWER_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Requirement coverage answer",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "evidence_ids", "reason"],
            "properties": {
                "status": {"const": "missing"},
                "evidence_ids": {"type": "array", "maxItems": 0},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "evidence_ids", "reason"],
            "properties": {
                "status": {"const": "complete"},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "evidence_ids",
                "reason",
                "remaining_statement",
            ],
            "properties": {
                "status": {"const": "partial"},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                "remaining_statement": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CONTRACT_TEXT_LENGTH,
                },
            },
        },
    ],
}


@dataclass(frozen=True, slots=True)
class HumanSetupNeedAnswer:
    required: bool
    reason: str
    source_refs: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> HumanSetupNeedAnswer:
        _exact_fields(value, {"required", "reason", "source_refs", "evidence_ids"}, "/")
        source_refs = _text_array(value["source_refs"], "/source_refs")
        evidence_ids = _text_array(value["evidence_ids"], "/evidence_ids")
        _unique(source_refs, "/source_refs")
        _unique(evidence_ids, "/evidence_ids")
        return cls(
            required=_boolean(value["required"], "/required"),
            reason=_reason(value["reason"], "/reason"),
            source_refs=source_refs,
            evidence_ids=evidence_ids,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "reason": self.reason,
            "source_refs": list(self.source_refs),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class HumanSetupDescriptionAnswer:
    title: str
    instructions: tuple[str, ...]
    verification: str
    source_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> HumanSetupDescriptionAnswer:
        _exact_fields(
            value,
            {"title", "instructions", "verification", "source_refs"},
            "/",
        )
        title = _bounded_text(value["title"], "/title")
        instructions = _text_array(value["instructions"], "/instructions")
        if not instructions:
            raise PlanningContractError(
                "/instructions", "must contain at least one step"
            )
        verification = _bounded_text(value["verification"], "/verification")
        source_refs = _text_array(value["source_refs"], "/source_refs")
        _unique(source_refs, "/source_refs")
        for pointer, text in [
            ("/title", title),
            *(
                (f"/instructions/{index}", step)
                for index, step in enumerate(instructions)
            ),
            ("/verification", verification),
        ]:
            if text.strip().casefold() in {
                "none",
                "no manual prerequisite",
                "no manual prerequisite for this item",
                "n/a",
            }:
                raise PlanningContractError(pointer, "must describe a concrete setup")
        return cls(title, instructions, verification, source_refs)

    def to_data(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "instructions": list(self.instructions),
            "verification": self.verification,
            "source_refs": list(self.source_refs),
        }


HUMAN_SETUP_DESCRIPTION_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Human setup description answer",
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "instructions", "verification", "source_refs"],
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
        "instructions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CONTRACT_TEXT_LENGTH,
            },
        },
        "verification": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CONTRACT_TEXT_LENGTH,
        },
        "source_refs": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
    },
}


def _exact_fields(value: Mapping[str, Any], expected: set[str], pointer: str) -> None:
    missing = expected - value.keys()
    if missing:
        name = sorted(missing)[0]
        raise PlanningContractError(
            _child_pointer(pointer, name), "required field is missing"
        )
    unknown = value.keys() - expected
    if unknown:
        name = sorted(unknown)[0]
        raise PlanningContractError(_child_pointer(pointer, name), "unknown field")


def _identifier(value: Any, pointer: str) -> str:
    text = _text(value, pointer)
    if not _ID_PATTERN.fullmatch(text):
        raise PlanningContractError(pointer, "must be a lowercase identifier")
    return text


def _text(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningContractError(pointer, "must be a non-empty string")
    if _PLACEHOLDER_PATTERN.search(value):
        raise PlanningContractError(pointer, "must not contain a placeholder")
    return value.strip()


def _bounded_text(value: Any, pointer: str) -> str:
    text = _text(value, pointer)
    if len(text) > MAX_CONTRACT_TEXT_LENGTH:
        raise PlanningContractError(
            pointer, f"must contain at most {MAX_CONTRACT_TEXT_LENGTH} characters"
        )
    return text


def _reason(value: Any, pointer: str) -> str:
    text = _text(value, pointer)
    if len(text) > 1_000:
        raise PlanningContractError(pointer, "must contain at most 1000 characters")
    return text


def _text_array(value: Any, pointer: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PlanningContractError(pointer, "must be an array of non-empty strings")
    return tuple(_text(item, f"{pointer}/{index}") for index, item in enumerate(value))


def _unique(values: tuple[str, ...], pointer: str) -> None:
    if len(set(values)) != len(values):
        raise PlanningContractError(pointer, "must not contain duplicate values")


def _positive_int(value: Any, pointer: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanningContractError(pointer, "must be a positive integer")
    return value


def _boolean(value: Any, pointer: str) -> bool:
    if not isinstance(value, bool):
        raise PlanningContractError(pointer, "must be a boolean")
    return value


def _child_pointer(parent: str, child: str) -> str:
    return f"/{child}" if parent == "/" else f"{parent}/{child}"


__all__ = [
    "COVERAGE_ANSWER_JSON_SCHEMA",
    "ENUMERATION_ANSWER_JSON_SCHEMA",
    "HUMAN_SETUP_DESCRIPTION_JSON_SCHEMA",
    "MAX_ENUMERATION_RECORDS",
    "PlanningContractError",
    "QUESTION_JSON_SCHEMA",
    "QUESTION_KINDS",
    "VALIDATION_ERROR_JSON_SCHEMA",
    "CompleteCoverage",
    "CoverageAnswer",
    "EnumerationAnswer",
    "HumanSetupDescriptionAnswer",
    "HumanSetupNeedAnswer",
    "MissingCoverage",
    "PartialCoverage",
    "Question",
    "parse_coverage_answer",
]
