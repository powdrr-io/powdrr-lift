"""Typed delivery-profile contracts for the agent execution boundary.

Delivery profiles describe ownership, artifact handoffs, and review topology.
They deliberately do not contain action instructions or executable policy. The
execution kernel remains the authority for those concerns.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

DELIVERY_PROFILE_SCHEMA_VERSION = "delivery-profile-v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")


class PhaseType(StrEnum):
    """Closed delivery phases understood by the execution kernel."""

    INTAKE = "intake"
    SPECIFY = "specify"
    REVIEW_SPECIFICATIONS = "review_specifications"
    DECOMPOSE = "decompose"
    REVIEW_PROPOSED_PRS = "review_proposed_prs"
    PLAN_PR = "plan_pr"
    AWAIT_PLAN_DECISION = "await_plan_decision"
    BUILD = "build"
    VALIDATE = "validate"
    REVIEW_PR = "review_pr"
    RESOLVE_FINDINGS = "resolve_findings"
    CONFIRM_READINESS = "confirm_readiness"
    PUBLISH_PR = "publish_pr"
    COMPLETE_FEATURE = "complete_feature"


class PersonaType(StrEnum):
    """Stable responsibility types available to a delivery profile."""

    ARCHITECT = "architect"
    ENGINEERING_MANAGER = "engineering_manager"
    ENGINEER = "engineer"
    SPECIFICATION_REVIEWER = "specification_reviewer"
    CODE_REVIEWER = "code_reviewer"


SUPPORTED_ARTIFACT_TYPES = frozenset(
    {
        "request",
        "specification",
        "specification_review",
        "proposed_pr",
        "proposed_pr_review",
        "execution_plan",
        "implementation",
        "validation_evidence",
        "specification_findings",
        "code_findings",
        "readiness_report",
        "published_pr",
    }
)

_EXPECTED_PHASE_PERSONA_TYPES: dict[PhaseType, frozenset[PersonaType]] = {
    PhaseType.INTAKE: frozenset({PersonaType.ARCHITECT}),
    PhaseType.SPECIFY: frozenset({PersonaType.ARCHITECT}),
    PhaseType.REVIEW_SPECIFICATIONS: frozenset({PersonaType.SPECIFICATION_REVIEWER}),
    PhaseType.DECOMPOSE: frozenset({PersonaType.ENGINEERING_MANAGER}),
    PhaseType.REVIEW_PROPOSED_PRS: frozenset({PersonaType.SPECIFICATION_REVIEWER}),
    PhaseType.PLAN_PR: frozenset({PersonaType.ENGINEER}),
    PhaseType.AWAIT_PLAN_DECISION: frozenset({PersonaType.ENGINEERING_MANAGER}),
    PhaseType.BUILD: frozenset({PersonaType.ENGINEER}),
    PhaseType.VALIDATE: frozenset({PersonaType.ENGINEER}),
    PhaseType.REVIEW_PR: frozenset({PersonaType.CODE_REVIEWER}),
    PhaseType.RESOLVE_FINDINGS: frozenset({PersonaType.ENGINEER}),
    PhaseType.CONFIRM_READINESS: frozenset({PersonaType.CODE_REVIEWER}),
    PhaseType.PUBLISH_PR: frozenset({PersonaType.ENGINEERING_MANAGER}),
    PhaseType.COMPLETE_FEATURE: frozenset({PersonaType.ENGINEERING_MANAGER}),
}


@dataclass(frozen=True, slots=True)
class DeliveryProfileValidationIssue(ValidationError):
    """One actionable profile validation failure."""


@dataclass(frozen=True, slots=True)
class DeliveryProfileValidationReport:
    """Machine-readable validation result for a delivery profile."""

    validation_successful: bool
    profile_id: str | None = None
    issues: list[DeliveryProfileValidationIssue] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return {
            "validation_successful": self.validation_successful,
            "profile_id": self.profile_id,
            "issues": [validation_error_to_data(issue) for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class PersonaDefinition:
    persona_id: str
    persona_type: PersonaType
    model_profile: str
    prompt_catalogs: tuple[str, ...] = field(default_factory=tuple)
    skill_references: tuple[str, ...] = field(default_factory=tuple)
    step_budget: int | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "persona_id": self.persona_id,
            "persona_type": self.persona_type.value,
            "model_profile": self.model_profile,
        }
        if self.prompt_catalogs:
            data["prompt_catalogs"] = list(self.prompt_catalogs)
        if self.skill_references:
            data["skill_references"] = list(self.skill_references)
        if self.step_budget is not None:
            data["step_budget"] = self.step_budget
        return data


@dataclass(frozen=True, slots=True)
class PhaseAssignment:
    phase_type: PhaseType
    persona_id: str
    input_artifacts: tuple[str, ...] = field(default_factory=tuple)
    output_artifacts: tuple[str, ...] = field(default_factory=tuple)
    validation_profiles: tuple[str, ...] = field(default_factory=tuple)
    auto_approve_low_risk_plan: bool = True

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "phase_type": self.phase_type.value,
            "persona_id": self.persona_id,
        }
        if self.input_artifacts:
            data["input_artifacts"] = list(self.input_artifacts)
        if self.output_artifacts:
            data["output_artifacts"] = list(self.output_artifacts)
        if self.validation_profiles:
            data["validation_profiles"] = list(self.validation_profiles)
        if not self.auto_approve_low_risk_plan:
            data["auto_approve_low_risk_plan"] = False
        return data


@dataclass(frozen=True, slots=True)
class ArtifactHandoff:
    source_phase: PhaseType
    destination_phase: PhaseType
    artifact_type: str
    owner_persona_id: str
    required: bool = True

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_phase": self.source_phase.value,
            "destination_phase": self.destination_phase.value,
            "artifact_type": self.artifact_type,
            "owner_persona_id": self.owner_persona_id,
        }
        if not self.required:
            data["required"] = False
        return data


@dataclass(frozen=True, slots=True)
class ReviewAssignment:
    reviewer_persona_id: str
    artifact_types: tuple[str, ...]
    independent: bool = True
    blocking_severities: tuple[str, ...] = ("blocking",)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "reviewer_persona_id": self.reviewer_persona_id,
            "artifact_types": list(self.artifact_types),
        }
        if not self.independent:
            data["independent"] = False
        if self.blocking_severities != ("blocking",):
            data["blocking_severities"] = list(self.blocking_severities)
        return data


@dataclass(frozen=True, slots=True)
class DeliveryProfile:
    schema_version: str
    profile_id: str
    personas: tuple[PersonaDefinition, ...]
    phases: tuple[PhaseAssignment, ...]
    artifact_handoffs: tuple[ArtifactHandoff, ...] = field(default_factory=tuple)
    reviews: tuple[ReviewAssignment, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "personas": [persona.to_data() for persona in self.personas],
            "phases": [phase.to_data() for phase in self.phases],
        }
        if self.artifact_handoffs:
            data["artifact_handoffs"] = [
                handoff.to_data() for handoff in self.artifact_handoffs
            ]
        if self.reviews:
            data["reviews"] = [review.to_data() for review in self.reviews]
        return data

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_data(), sort_keys=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> DeliveryProfile:
        validation = build_delivery_profile_validation_report(
            yaml.safe_dump(dict(data), sort_keys=False)
        )
        if not validation.validation_successful:
            details = "; ".join(issue.message for issue in validation.issues)
            raise ValueError(f"Invalid delivery profile: {details}")
        personas = tuple(
            _parse_persona(item)
            for item in _required_mapping_sequence(data, "personas")
        )
        phases = tuple(
            _parse_phase(item) for item in _required_mapping_sequence(data, "phases")
        )
        handoffs = tuple(
            _parse_handoff(item)
            for item in _optional_mapping_sequence(data, "artifact_handoffs")
        )
        reviews = tuple(
            _parse_review(item) for item in _optional_mapping_sequence(data, "reviews")
        )
        return cls(
            schema_version=DELIVERY_PROFILE_SCHEMA_VERSION,
            profile_id=_required_identifier(data, "profile_id"),
            personas=personas,
            phases=phases,
            artifact_handoffs=handoffs,
            reviews=reviews,
        )

    @classmethod
    def from_yaml(cls, content: str) -> DeliveryProfile:
        loaded = yaml.safe_load(content)
        if not isinstance(loaded, Mapping):
            raise ValueError("Delivery profile YAML must decode to an object.")
        return cls.from_data(cast("Mapping[str, Any]", loaded))

    @classmethod
    def from_file(cls, path: str | Path) -> DeliveryProfile:
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))


def build_delivery_profile_validation_report(
    content: str,
    *,
    source_path: str | Path | None = None,
) -> DeliveryProfileValidationReport:
    try:
        loaded = yaml.safe_load(content)
    except Exception as exc:  # noqa: BLE001
        return _report(
            source_path,
            [
                DeliveryProfileValidationIssue(
                    code="invalid_yaml",
                    message=f"Could not parse delivery profile YAML: {exc}",
                    path=str(source_path) if source_path is not None else None,
                )
            ],
        )
    if not isinstance(loaded, Mapping):
        return _report(
            source_path,
            [
                DeliveryProfileValidationIssue(
                    code="invalid_root_type",
                    message="Delivery profile YAML must decode to an object.",
                    path=str(source_path) if source_path is not None else None,
                )
            ],
        )

    data = cast("Mapping[str, Any]", loaded)
    issues: list[DeliveryProfileValidationIssue] = []
    _validate_unknown_keys(
        data,
        {
            "schema_version",
            "profile_id",
            "personas",
            "phases",
            "artifact_handoffs",
            "reviews",
        },
        issues,
        source_path,
        "delivery profile",
    )
    schema_version = data.get("schema_version")
    if schema_version != DELIVERY_PROFILE_SCHEMA_VERSION:
        issues.append(
            _issue(
                "unsupported_schema_version",
                f"schema_version must be {DELIVERY_PROFILE_SCHEMA_VERSION!r}.",
                source_path,
                "schema_version",
            )
        )
    profile_id = _validate_identifier(
        data.get("profile_id"), "profile_id", issues, source_path, ""
    )
    raw_personas = _validate_mapping_sequence(data, "personas", issues, source_path)
    raw_phases = _validate_mapping_sequence(data, "phases", issues, source_path)
    raw_handoffs = _validate_optional_mapping_sequence(
        data, "artifact_handoffs", issues, source_path
    )
    raw_reviews = _validate_optional_mapping_sequence(
        data, "reviews", issues, source_path
    )

    personas: dict[str, PersonaDefinition] = {}
    for index, raw_persona in enumerate(raw_personas):
        parsed = _validate_persona(raw_persona, issues, source_path, index)
        if parsed is not None:
            if parsed.persona_id in personas:
                issues.append(
                    _issue(
                        "duplicate_persona_id",
                        f"Persona id {parsed.persona_id!r} must be unique.",
                        source_path,
                        f"personas[{index}].persona_id",
                    )
                )
            personas[parsed.persona_id] = parsed

    phases: dict[PhaseType, PhaseAssignment] = {}
    for index, raw_phase in enumerate(raw_phases):
        parsed_phase = _validate_phase(raw_phase, issues, source_path, index, personas)
        if parsed_phase is not None:
            if parsed_phase.phase_type in phases:
                issues.append(
                    _issue(
                        "duplicate_phase_type",
                        "Phase "
                        f"{parsed_phase.phase_type.value!r} must be assigned once.",
                        source_path,
                        f"phases[{index}].phase_type",
                    )
                )
            phases[parsed_phase.phase_type] = parsed_phase

    missing_phases = [phase.value for phase in PhaseType if phase not in phases]
    if missing_phases:
        issues.append(
            _issue(
                "missing_phase_assignments",
                "Delivery profile must assign every supported phase: "
                + ", ".join(missing_phases)
                + ".",
                source_path,
                "phases",
            )
        )

    for index, raw_handoff in enumerate(raw_handoffs):
        _validate_handoff(raw_handoff, issues, source_path, index, personas, phases)
    for index, raw_review in enumerate(raw_reviews):
        _validate_review(raw_review, issues, source_path, index, personas)

    report = DeliveryProfileValidationReport(
        validation_successful=not issues,
        profile_id=profile_id,
        issues=issues,
    )
    return report


def validate_delivery_profile_yaml(content: str) -> str:
    return (
        json.dumps(
            build_delivery_profile_validation_report(content).to_data(),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def load_delivery_profile(path: str | Path) -> DeliveryProfile:
    profile = DeliveryProfile.from_file(path)
    report = build_delivery_profile_validation_report(
        Path(path).read_text(encoding="utf-8"), source_path=path
    )
    if not report.validation_successful:
        details = "; ".join(issue.message for issue in report.issues)
        raise ValueError(f"Invalid delivery profile {path}: {details}")
    return profile


def _parse_persona(data: Mapping[str, Any]) -> PersonaDefinition:
    return PersonaDefinition(
        persona_id=_required_identifier(data, "persona_id"),
        persona_type=PersonaType(_required_identifier(data, "persona_type")),
        model_profile=_required_identifier(data, "model_profile"),
        prompt_catalogs=_required_identifier_sequence(
            data, "prompt_catalogs", optional=True
        ),
        skill_references=_required_identifier_sequence(
            data, "skill_references", optional=True
        ),
        step_budget=_optional_positive_int(data, "step_budget"),
    )


def _parse_phase(data: Mapping[str, Any]) -> PhaseAssignment:
    return PhaseAssignment(
        phase_type=PhaseType(_required_identifier(data, "phase_type")),
        persona_id=_required_identifier(data, "persona_id"),
        input_artifacts=_required_identifier_sequence(
            data, "input_artifacts", optional=True
        ),
        output_artifacts=_required_identifier_sequence(
            data, "output_artifacts", optional=True
        ),
        validation_profiles=_required_identifier_sequence(
            data, "validation_profiles", optional=True
        ),
        auto_approve_low_risk_plan=bool(data.get("auto_approve_low_risk_plan", True)),
    )


def _parse_handoff(data: Mapping[str, Any]) -> ArtifactHandoff:
    return ArtifactHandoff(
        source_phase=PhaseType(_required_identifier(data, "source_phase")),
        destination_phase=PhaseType(_required_identifier(data, "destination_phase")),
        artifact_type=_required_identifier(data, "artifact_type"),
        owner_persona_id=_required_identifier(data, "owner_persona_id"),
        required=bool(data.get("required", True)),
    )


def _parse_review(data: Mapping[str, Any]) -> ReviewAssignment:
    return ReviewAssignment(
        reviewer_persona_id=_required_identifier(data, "reviewer_persona_id"),
        artifact_types=_required_identifier_sequence(data, "artifact_types"),
        independent=bool(data.get("independent", True)),
        blocking_severities=_required_identifier_sequence(
            data, "blocking_severities", optional=True
        )
        or ("blocking",),
    )


def _validate_persona(
    data: Mapping[str, Any],
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    index: int,
) -> PersonaDefinition | None:
    path = f"personas[{index}]"
    _validate_unknown_keys(
        data,
        {
            "persona_id",
            "persona_type",
            "model_profile",
            "prompt_catalogs",
            "skill_references",
            "step_budget",
        },
        issues,
        source_path,
        "persona",
        path,
    )
    persona_id = _validate_identifier(
        data.get("persona_id"), "persona_id", issues, source_path, path
    )
    persona_type = _validate_enum(
        data.get("persona_type"), PersonaType, "persona_type", issues, source_path, path
    )
    model_profile = _validate_identifier(
        data.get("model_profile"), "model_profile", issues, source_path, path
    )
    prompt_catalogs = _validate_identifier_sequence(
        data, "prompt_catalogs", issues, source_path, path
    )
    skill_references = _validate_identifier_sequence(
        data, "skill_references", issues, source_path, path
    )
    step_budget = data.get("step_budget")
    if step_budget is not None and (
        not isinstance(step_budget, int)
        or isinstance(step_budget, bool)
        or step_budget <= 0
    ):
        issues.append(
            _issue(
                "invalid_step_budget",
                "step_budget must be a positive integer.",
                source_path,
                f"{path}.step_budget",
            )
        )
    if persona_id is None or persona_type is None or model_profile is None:
        return None
    return PersonaDefinition(
        persona_id,
        persona_type,
        model_profile,
        prompt_catalogs,
        skill_references,
        step_budget,
    )


def _validate_phase(
    data: Mapping[str, Any],
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    index: int,
    personas: Mapping[str, PersonaDefinition],
) -> PhaseAssignment | None:
    path = f"phases[{index}]"
    _validate_unknown_keys(
        data,
        {
            "phase_type",
            "persona_id",
            "input_artifacts",
            "output_artifacts",
            "validation_profiles",
            "auto_approve_low_risk_plan",
        },
        issues,
        source_path,
        "phase assignment",
        path,
    )
    phase_type = _validate_enum(
        data.get("phase_type"), PhaseType, "phase_type", issues, source_path, path
    )
    persona_id = _validate_identifier(
        data.get("persona_id"), "persona_id", issues, source_path, path
    )
    input_artifacts = _validate_artifact_sequence(
        data, "input_artifacts", issues, source_path, path
    )
    output_artifacts = _validate_artifact_sequence(
        data, "output_artifacts", issues, source_path, path
    )
    validation_profiles = _validate_identifier_sequence(
        data, "validation_profiles", issues, source_path, path
    )
    auto_approve = data.get("auto_approve_low_risk_plan", True)
    if not isinstance(auto_approve, bool):
        issues.append(
            _issue(
                "invalid_policy_value",
                "auto_approve_low_risk_plan must be a boolean.",
                source_path,
                f"{path}.auto_approve_low_risk_plan",
            )
        )
        auto_approve = True
    if persona_id is not None and persona_id not in personas:
        issues.append(
            _issue(
                "unknown_persona",
                f"Phase references unknown persona {persona_id!r}.",
                source_path,
                f"{path}.persona_id",
            )
        )
    elif phase_type is not None and persona_id is not None:
        persona_type = personas[persona_id].persona_type
        expected_types = _EXPECTED_PHASE_PERSONA_TYPES[phase_type]
        if persona_type not in expected_types:
            expected = ", ".join(sorted(item.value for item in expected_types))
            issues.append(
                _issue(
                    "invalid_phase_persona",
                    "Phase "
                    f"{phase_type.value!r} requires a persona of type {expected}.",
                    source_path,
                    f"{path}.persona_id",
                )
            )
    if phase_type is None or persona_id is None:
        return None
    return PhaseAssignment(
        phase_type,
        persona_id,
        input_artifacts,
        output_artifacts,
        validation_profiles,
        auto_approve,
    )


def _validate_handoff(
    data: Mapping[str, Any],
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    index: int,
    personas: Mapping[str, PersonaDefinition],
    phases: Mapping[PhaseType, PhaseAssignment],
) -> None:
    path = f"artifact_handoffs[{index}]"
    _validate_unknown_keys(
        data,
        {
            "source_phase",
            "destination_phase",
            "artifact_type",
            "owner_persona_id",
            "required",
        },
        issues,
        source_path,
        "artifact handoff",
        path,
    )
    source = _validate_enum(
        data.get("source_phase"), PhaseType, "source_phase", issues, source_path, path
    )
    destination = _validate_enum(
        data.get("destination_phase"),
        PhaseType,
        "destination_phase",
        issues,
        source_path,
        path,
    )
    artifact = _validate_artifact(
        data.get("artifact_type"), "artifact_type", issues, source_path, path
    )
    owner = _validate_identifier(
        data.get("owner_persona_id"), "owner_persona_id", issues, source_path, path
    )
    if owner is not None and owner not in personas:
        issues.append(
            _issue(
                "unknown_persona",
                f"Handoff references unknown persona {owner!r}.",
                source_path,
                f"{path}.owner_persona_id",
            )
        )
    if source is not None and source not in phases:
        issues.append(
            _issue(
                "unknown_phase",
                f"Handoff references unknown source phase {source.value!r}.",
                source_path,
                f"{path}.source_phase",
            )
        )
    if destination is not None and destination not in phases:
        issues.append(
            _issue(
                "unknown_phase",
                f"Handoff references unknown destination phase {destination.value!r}.",
                source_path,
                f"{path}.destination_phase",
            )
        )
    required = data.get("required", True)
    if not isinstance(required, bool):
        issues.append(
            _issue(
                "invalid_required",
                "required must be a boolean.",
                source_path,
                f"{path}.required",
            )
        )
    if source is not None and destination is not None and source == destination:
        issues.append(
            _issue(
                "self_handoff",
                "An artifact handoff must cross a phase boundary.",
                source_path,
                path,
            )
        )
    if artifact is not None and source is not None and destination is not None:
        source_assignment = phases.get(source)
        destination_assignment = phases.get(destination)
        if (
            source_assignment is not None
            and artifact not in source_assignment.output_artifacts
        ):
            issues.append(
                _issue(
                    "handoff_missing_source_output",
                    f"Artifact {artifact!r} is not declared as a source phase output.",
                    source_path,
                    path,
                )
            )
        if (
            destination_assignment is not None
            and artifact not in destination_assignment.input_artifacts
        ):
            issues.append(
                _issue(
                    "handoff_missing_destination_input",
                    "Artifact "
                    f"{artifact!r} is not declared as a destination phase input.",
                    source_path,
                    path,
                )
            )


def _validate_review(
    data: Mapping[str, Any],
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    index: int,
    personas: Mapping[str, PersonaDefinition],
) -> None:
    path = f"reviews[{index}]"
    _validate_unknown_keys(
        data,
        {"reviewer_persona_id", "artifact_types", "independent", "blocking_severities"},
        issues,
        source_path,
        "review assignment",
        path,
    )
    reviewer = _validate_identifier(
        data.get("reviewer_persona_id"),
        "reviewer_persona_id",
        issues,
        source_path,
        path,
    )
    artifacts = _validate_artifact_sequence(
        data, "artifact_types", issues, source_path, path, required=True
    )
    independent = data.get("independent", True)
    if not isinstance(independent, bool):
        issues.append(
            _issue(
                "invalid_independent",
                "independent must be a boolean.",
                source_path,
                f"{path}.independent",
            )
        )
    _validate_identifier_sequence(
        data, "blocking_severities", issues, source_path, path
    )
    if reviewer is not None and reviewer not in personas:
        issues.append(
            _issue(
                "unknown_persona",
                f"Review references unknown persona {reviewer!r}.",
                source_path,
                f"{path}.reviewer_persona_id",
            )
        )
    elif reviewer is not None and personas[reviewer].persona_type not in {
        PersonaType.SPECIFICATION_REVIEWER,
        PersonaType.CODE_REVIEWER,
    }:
        issues.append(
            _issue(
                "invalid_reviewer_persona",
                "Review assignments must use a reviewer persona.",
                source_path,
                f"{path}.reviewer_persona_id",
            )
        )
    if not artifacts:
        issues.append(
            _issue(
                "missing_review_artifacts",
                "Review assignment must name at least one artifact type.",
                source_path,
                f"{path}.artifact_types",
            )
        )


def _report(
    source_path: str | Path | None, issues: list[DeliveryProfileValidationIssue]
) -> DeliveryProfileValidationReport:
    return DeliveryProfileValidationReport(not issues, issues=issues)


def _issue(
    code: str, message: str, source_path: str | Path | None, relative_path: str
) -> DeliveryProfileValidationIssue:
    prefix = str(source_path) if source_path is not None else None
    path = f"{prefix}:{relative_path}" if prefix else relative_path
    return DeliveryProfileValidationIssue(code=code, message=message, path=path)


def _validate_unknown_keys(
    data: Mapping[str, Any],
    allowed: set[str],
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    subject: str,
    path: str = "",
) -> None:
    for key in data:
        if key not in allowed:
            issues.append(
                _issue(
                    "unknown_field",
                    f"Unknown {subject} field {key!r}.",
                    source_path,
                    f"{path}.{key}" if path else key,
                )
            )


def _validate_identifier(
    value: Any,
    field_name: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    path: str,
) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        issues.append(
            _issue(
                "invalid_identifier",
                f"{field_name} must match {_IDENTIFIER.pattern!r}.",
                source_path,
                f"{path}.{field_name}",
            )
        )
        return None
    return value


def _required_identifier(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{key} must match {_IDENTIFIER.pattern!r}.")
    return value


def _validate_enum[EnumT: StrEnum](
    value: Any,
    enum_type: type[EnumT],
    field_name: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    path: str,
) -> EnumT | None:
    if not isinstance(value, str):
        issues.append(
            _issue(
                "invalid_enum",
                f"{field_name} must be a string.",
                source_path,
                f"{path}.{field_name}",
            )
        )
        return None
    try:
        return enum_type(value)
    except ValueError:
        issues.append(
            _issue(
                "invalid_enum",
                f"{field_name} must be one of: "
                f"{', '.join(item.value for item in enum_type)}.",
                source_path,
                f"{path}.{field_name}",
            )
        )
        return None


def _validate_artifact(
    value: Any,
    field_name: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    path: str,
) -> str | None:
    result = _validate_identifier(value, field_name, issues, source_path, path)
    if result is not None and result not in SUPPORTED_ARTIFACT_TYPES:
        issues.append(
            _issue(
                "unsupported_artifact_type",
                f"{field_name} must be one of: "
                f"{', '.join(sorted(SUPPORTED_ARTIFACT_TYPES))}.",
                source_path,
                f"{path}.{field_name}",
            )
        )
        return None
    return result


def _validate_artifact_sequence(
    data: Mapping[str, Any],
    key: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    path: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    values = _validate_identifier_sequence(
        data, key, issues, source_path, path, required=required
    )
    result: list[str] = []
    for index, value in enumerate(values):
        if value not in SUPPORTED_ARTIFACT_TYPES:
            issues.append(
                _issue(
                    "unsupported_artifact_type",
                    f"{key} contains unsupported artifact type {value!r}.",
                    source_path,
                    f"{path}.{key}[{index}]",
                )
            )
        else:
            result.append(value)
    return tuple(result)


def _validate_identifier_sequence(
    data: Mapping[str, Any],
    key: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
    path: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None and not required:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        issues.append(
            _issue(
                "invalid_sequence",
                f"{key} must be an array of identifiers.",
                source_path,
                f"{path}.{key}",
            )
        )
        return ()
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            issues.append(
                _issue(
                    "invalid_identifier",
                    f"{key} must contain identifiers matching {_IDENTIFIER.pattern!r}.",
                    source_path,
                    f"{path}.{key}[{index}]",
                )
            )
        else:
            values.append(value)
    if len(values) != len(set(values)):
        issues.append(
            _issue(
                "duplicate_sequence_item",
                f"{key} must not contain duplicates.",
                source_path,
                f"{path}.{key}",
            )
        )
    return tuple(values)


def _required_identifier_sequence(
    data: Mapping[str, Any], key: str, *, optional: bool = False
) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None and optional:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(f"{key} must be an array of identifiers.")
    values = tuple(raw)
    if any(
        not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
        for value in values
    ):
        raise ValueError(
            f"{key} must contain identifiers matching {_IDENTIFIER.pattern!r}."
        )
    return cast("tuple[str, ...]", values)


def _optional_positive_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer.")
    return value


def _required_mapping_sequence(
    data: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    raw = data.get(key)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(f"{key} must be an array of objects.")
    if not all(isinstance(item, Mapping) for item in raw):
        raise ValueError(f"{key} must contain only objects.")
    return tuple(cast(Mapping[str, Any], item) for item in raw)


def _optional_mapping_sequence(
    data: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    raw = data.get(key, [])
    if raw is None:
        return ()
    return _required_mapping_sequence({key: raw}, key)


def _validate_mapping_sequence(
    data: Mapping[str, Any],
    key: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
) -> tuple[Mapping[str, Any], ...]:
    raw = data.get(key)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        issues.append(
            _issue(
                "invalid_sequence",
                f"{key} must be an array of objects.",
                source_path,
                key,
            )
        )
        return ()
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            issues.append(
                _issue(
                    "invalid_item",
                    f"{key} must contain only objects.",
                    source_path,
                    f"{key}[{index}]",
                )
            )
        else:
            result.append(cast(Mapping[str, Any], item))
    return tuple(result)


def _validate_optional_mapping_sequence(
    data: Mapping[str, Any],
    key: str,
    issues: list[DeliveryProfileValidationIssue],
    source_path: str | Path | None,
) -> tuple[Mapping[str, Any], ...]:
    if key not in data or data[key] is None:
        return ()
    return _validate_mapping_sequence(data, key, issues, source_path)
