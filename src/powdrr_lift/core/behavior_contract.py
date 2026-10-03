"""Lossless, typed behavior contracts for coding-worker handoffs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    SEMANTIC_DIMENSION_DECISION_KINDS,
)

BEHAVIOR_DIMENSIONS = (
    "normal_result",
    "error_behavior",
    "continuation",
    "unsupported_behavior",
    "cancellation_cleanup",
    "compatibility",
    "negative_boundaries",
)
ASSUMPTION_BASES = frozenset(
    {
        "repository_convention",
        "industry_standard",
        "ecosystem_convention",
        "language_or_framework_default",
        "conservative_default",
    }
)
SUPPORTED_ASSUMPTION_DIMENSIONS = frozenset(
    (*BEHAVIOR_DIMENSIONS, *SEMANTIC_DIMENSION_DECISION_KINDS)
)


class BehaviorContractError(ValueError):
    """Raised when a behavior contract is incomplete or ambiguous."""


@dataclass(frozen=True, slots=True)
class BehaviorScenario:
    """One observable behavior and its executable proof obligation."""

    scenario_id: str
    subject: str
    given: Any
    when: str
    then: Any
    dimensions: Mapping[str, Any]
    evidence: tuple[str, ...]
    validator: str
    related_requirements: tuple[str, ...] = ()
    capability_matrix: tuple[Mapping[str, Any], ...] = ()
    assumptions: tuple[Mapping[str, str], ...] = ()
    source_dimensions: Mapping[str, Any] = field(default_factory=dict)
    semantic_dimension_applicability: Mapping[str, str] = field(default_factory=dict)
    unresolved_dimensions: tuple[str, ...] = ()
    faithfulness_ref: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = "behavior-scenario-v1"
    validation_group_id: str | None = None
    validation_relation: str = "independent"
    routing: str = "include"

    def to_data(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "subject": self.subject,
            "given": self.given,
            "when": self.when,
            "then": self.then,
            "dimensions": {name: self.dimensions[name] for name in BEHAVIOR_DIMENSIONS},
            "evidence": list(self.evidence),
            "validator": self.validator,
            "routing": self.routing,
            "related_requirements": list(self.related_requirements),
            "capability_matrix": [
                {**dict(item), "evidence": list(item["evidence"])}
                for item in self.capability_matrix
            ],
        }
        if self.assumptions:
            data["assumptions"] = [dict(item) for item in self.assumptions]
        if self.source_dimensions:
            data["source_dimensions"] = dict(self.source_dimensions)
        if self.semantic_dimension_applicability:
            data["semantic_dimension_applicability"] = dict(
                self.semantic_dimension_applicability
            )
        if self.unresolved_dimensions:
            data["unresolved_dimensions"] = list(self.unresolved_dimensions)
        if self.faithfulness_ref:
            data["faithfulness_ref"] = dict(self.faithfulness_ref)
        if self.validation_group_id is not None:
            data["validation_group_id"] = self.validation_group_id
            data["validation_relation"] = self.validation_relation
        return data


def compile_behavior_scenarios(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[BehaviorScenario, ...]:
    """Validate typed behavior scenarios, failing closed on omitted dimensions."""
    scenarios: list[BehaviorScenario] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        scenario_id = _text(item.get("scenario_id"), f"scenario {index} scenario_id")
        if item.get("schema_version", "behavior-scenario-v1") != "behavior-scenario-v1":
            raise BehaviorContractError(
                f"scenario {scenario_id} has unsupported schema"
            )
        if scenario_id in seen:
            raise BehaviorContractError(f"duplicate behavior scenario: {scenario_id}")
        seen.add(scenario_id)
        dimensions_raw = item.get("dimensions")
        if not isinstance(dimensions_raw, Mapping):
            raise BehaviorContractError(f"scenario {scenario_id} requires dimensions")
        missing = [name for name in BEHAVIOR_DIMENSIONS if name not in dimensions_raw]
        if missing:
            raise BehaviorContractError(
                f"scenario {scenario_id} omits dimensions: {', '.join(missing)}"
            )
        dimensions = dict(dimensions_raw)
        for name in BEHAVIOR_DIMENSIONS:
            value = dimensions[name]
            if value is None or value == "" or value == [] or value == {}:
                raise BehaviorContractError(
                    f"scenario {scenario_id} dimension {name} must be explicit; "
                    "use not_applicable"
                )
        given = item.get("given")
        then = item.get("then")
        if given is None or then is None:
            raise BehaviorContractError(
                f"scenario {scenario_id} requires given and then"
            )
        evidence = _texts(item.get("evidence"), f"scenario {scenario_id} evidence")
        validator = _text(item.get("validator"), f"scenario {scenario_id} validator")
        raw_relationships = item.get("related_requirements", [])
        if not isinstance(raw_relationships, Sequence) or isinstance(
            raw_relationships, (str, bytes)
        ):
            raise BehaviorContractError(
                f"scenario {scenario_id} related_requirements must be a list"
            )
        related_requirements = tuple(
            _text(value, f"scenario {scenario_id} related requirement")
            for value in raw_relationships
        )
        raw_capabilities = item.get("capability_matrix", [])
        if (
            not isinstance(raw_capabilities, Sequence)
            or isinstance(raw_capabilities, (str, bytes))
            or not all(isinstance(value, Mapping) for value in raw_capabilities)
        ):
            raise BehaviorContractError(
                f"scenario {scenario_id} capability_matrix must be a list of mappings"
            )
        capabilities = (
            validate_capability_matrix(raw_capabilities) if raw_capabilities else ()
        )
        assumptions = validate_normative_assumptions(item.get("assumptions", []))
        source_dimensions_raw = item.get("source_dimensions", {})
        if not isinstance(source_dimensions_raw, Mapping):
            raise BehaviorContractError(
                f"scenario {scenario_id} source_dimensions must be an object"
            )
        if set(source_dimensions_raw) - set(SEMANTIC_DIMENSION_DECISION_KINDS):
            raise BehaviorContractError(
                f"scenario {scenario_id} has an unsupported source dimension"
            )
        source_dimensions = {
            str(name): _text(value, f"scenario {scenario_id} source dimension {name}")
            for name, value in source_dimensions_raw.items()
        }
        for name, value in source_dimensions.items():
            if value not in DECISION_VALUES[name] | {"unresolved"}:
                raise BehaviorContractError(
                    f"scenario {scenario_id} source dimension {name} is not "
                    "a classifier label"
                )
        applicability_raw = item.get("semantic_dimension_applicability", {})
        if not isinstance(applicability_raw, Mapping):
            raise BehaviorContractError(
                f"scenario {scenario_id} semantic_dimension_applicability "
                "must be an object"
            )
        if set(applicability_raw) - set(SEMANTIC_DIMENSION_DECISION_KINDS):
            raise BehaviorContractError(
                f"scenario {scenario_id} has an unsupported semantic "
                "dimension applicability"
            )
        semantic_dimension_applicability = {
            str(name): _text(value, f"scenario {scenario_id} applicability {name}")
            for name, value in applicability_raw.items()
        }
        if any(
            value != "not_applicable"
            for value in semantic_dimension_applicability.values()
        ):
            raise BehaviorContractError(
                f"scenario {scenario_id} semantic dimension applicability "
                "must be not_applicable"
            )
        if set(semantic_dimension_applicability) - set(source_dimensions):
            raise BehaviorContractError(
                f"scenario {scenario_id} marks a semantic dimension "
                "inapplicable without a source decision"
            )
        unresolved_raw = item.get("unresolved_dimensions", ())
        if not isinstance(unresolved_raw, Sequence) or isinstance(
            unresolved_raw, (str, bytes)
        ):
            raise BehaviorContractError(
                f"scenario {scenario_id} unresolved_dimensions must be an array"
            )
        unresolved_dimensions = tuple(
            _text(value, f"scenario {scenario_id} unresolved dimension")
            for value in unresolved_raw
        )
        if set(unresolved_dimensions) - SUPPORTED_ASSUMPTION_DIMENSIONS:
            raise BehaviorContractError(
                f"scenario {scenario_id} has an unsupported unresolved dimension"
            )
        faithfulness_raw = item.get("faithfulness_ref", {})
        if not isinstance(faithfulness_raw, Mapping):
            raise BehaviorContractError(
                f"scenario {scenario_id} faithfulness_ref is malformed"
            )
        if faithfulness_raw and set(faithfulness_raw) != {
            "artifact_path",
            "fingerprint",
        }:
            raise BehaviorContractError(
                f"scenario {scenario_id} faithfulness_ref is malformed"
            )
        faithfulness_ref = {
            str(name): _text(value, f"scenario {scenario_id} faithfulness ref {name}")
            for name, value in faithfulness_raw.items()
        }
        assumption_dimensions = {item["dimension"] for item in assumptions}
        for name, value in source_dimensions.items():
            if (
                value not in {"unspecified", "unresolved"}
                and name in assumption_dimensions
            ):
                raise BehaviorContractError(
                    f"scenario {scenario_id} has an assumption for source-resolved "
                    f"semantic dimension {name}"
                )
            if name in semantic_dimension_applicability and value not in {
                "unspecified",
                "unresolved",
            }:
                raise BehaviorContractError(
                    f"scenario {scenario_id} marks source-resolved semantic "
                    f"dimension {name} not_applicable"
                )
            if (
                name in semantic_dimension_applicability
                and name in assumption_dimensions
            ):
                raise BehaviorContractError(
                    f"scenario {scenario_id} cannot assume a semantic dimension "
                    f"marked not_applicable: {name}"
                )
            if name in unresolved_dimensions and value not in {
                "unspecified",
                "unresolved",
            }:
                raise BehaviorContractError(
                    f"scenario {scenario_id} marks source-resolved semantic "
                    f"dimension {name} unresolved"
                )
            if name in unresolved_dimensions and (
                name in semantic_dimension_applicability
                or name in assumption_dimensions
            ):
                raise BehaviorContractError(
                    f"scenario {scenario_id} cannot both resolve and leave "
                    f"semantic dimension {name} unresolved"
                )
        validation_group_id = item.get("validation_group_id")
        validation_relation = item.get("validation_relation", "independent")
        routing = item.get("routing", "include")
        if validation_group_id is not None and not isinstance(validation_group_id, str):
            raise BehaviorContractError(
                f"scenario {scenario_id} validation_group_id must be a string"
            )
        if validation_relation not in {
            "independent",
            "all_together",
            "ordered",
            "alternatives",
            "conditional",
        }:
            raise BehaviorContractError(
                f"scenario {scenario_id} validation_relation is invalid"
            )
        if validation_relation != "independent" and not validation_group_id:
            raise BehaviorContractError(
                f"scenario {scenario_id} related validation requires a group ID"
            )
        if routing not in {
            "include",
            "include_prohibition",
            "context",
            "exclude",
            "unclear",
        }:
            raise BehaviorContractError(f"scenario {scenario_id} routing is invalid")
        scenarios.append(
            BehaviorScenario(
                scenario_id=scenario_id,
                subject=_text(item.get("subject"), f"scenario {scenario_id} subject"),
                given=given,
                when=_text(item.get("when"), f"scenario {scenario_id} when"),
                then=then,
                dimensions=dimensions,
                evidence=evidence,
                validator=validator,
                related_requirements=related_requirements,
                capability_matrix=capabilities,
                assumptions=assumptions,
                source_dimensions=source_dimensions,
                semantic_dimension_applicability=semantic_dimension_applicability,
                unresolved_dimensions=unresolved_dimensions,
                faithfulness_ref=faithfulness_ref,
                validation_group_id=validation_group_id,
                validation_relation=str(validation_relation),
                routing=str(routing),
            )
        )
    return tuple(scenarios)


def render_behavior_matrix(scenarios: Sequence[BehaviorScenario]) -> str:
    """Turn typed scenarios into concrete checks for a coding worker.

    Provenance and applicability markers remain in the serialized packet. The
    worker needs observable behavior and meaningful boundaries, in reading
    order, rather than a JSON dump of the entire contract schema.
    """
    lines = [
        "Required behavior checks:",
        "For Include and IncludeProhibition routes, implement the listed "
        "behavior. For Unclear routes, review repository evidence, then make "
        "a best-supported conservative choice and continue even if uncertainty "
        "remains. First trace the affected "
        "code paths, including synchronous and asynchronous implementations "
        "and named integrations. Add focused tests for accepted cases and "
        "applicable paths. Run the tests before reporting completion.",
        "",
    ]
    implementation_scenarios = [
        item for item in scenarios if item.routing in {"include", "include_prohibition"}
    ]
    unclear_scenarios = [item for item in scenarios if item.routing == "unclear"]
    related_requirements: list[str] = []
    seen_related_requirements: set[str] = set()
    for index, item in enumerate(implementation_scenarios, start=1):
        detail = (
            f"{index}. [{item.scenario_id}] Given {_worker_text(item.given)}, "
            f"when {item.when}, expect {_worker_text(item.then)} "
            f"(subject: {item.subject})."
        )
        capabilities = [
            f"{value['behavior']} {value['capability']}"
            + (f" with {value['error']}" if value["behavior"] == "reject" else "")
            for value in item.capability_matrix
        ]
        if capabilities:
            detail += " Capabilities: " + "; ".join(capabilities) + "."
        if item.source_dimensions:
            detail += (
                " Source semantic dimensions: "
                + "; ".join(
                    f"{name} = {value}"
                    for name, value in sorted(item.source_dimensions.items())
                )
                + "."
            )
        if item.semantic_dimension_applicability:
            detail += (
                " Semantic dimensions not applicable to this scenario: "
                + "; ".join(
                    f"{name} = {value}"
                    for name, value in sorted(
                        item.semantic_dimension_applicability.items()
                    )
                )
                + "."
            )
        if item.unresolved_dimensions:
            detail += (
                " Unresolved dimensions: " + ", ".join(item.unresolved_dimensions) + "."
            )
        lines.append(detail)
        for relationship in item.related_requirements:
            normalized = " ".join(relationship.casefold().split())
            if normalized not in seen_related_requirements:
                seen_related_requirements.add(normalized)
                related_requirements.append(relationship)
    if related_requirements:
        lines.extend(("", "Additional cross-requirement constraints:"))
        lines.extend(f"- {relationship}" for relationship in related_requirements)
    related_groups: dict[tuple[str, str], list[str]] = {}
    for item in scenarios:
        if item.validation_group_id is not None:
            related_groups.setdefault(
                (item.validation_group_id, item.validation_relation), []
            ).append(item.scenario_id)
    if related_groups:
        lines.extend(("", "Relationships between checks:"))
        for (group_id, relation), scenario_ids in related_groups.items():
            explanations = {
                "all_together": "all checks must pass in the same scenario",
                "ordered": "checks must pass in this order",
                "alternatives": "the source allows these alternative outcomes",
                "conditional": "preserve the condition for each branch",
            }
            lines.append(
                f"- [{group_id}] {explanations[relation]}: {', '.join(scenario_ids)}."
            )
    if unclear_scenarios:
        lines.extend(
            (
                "",
                "Headless route review before implementation:",
                "Inspect repository code, tests, and documentation for evidence "
                "that informs each unclear source route. Treat the extracted "
                "behavior below as a candidate. If repository evidence resolves "
                "the route, follow it. If evidence remains insufficient, choose "
                "the most conservative, backward-compatible interpretation "
                "supported by the candidate and surrounding code, record that "
                "assumption, and continue the benchmark task. Do not stop or "
                "leave the task incomplete because the route remains unclear.",
            )
        )
        for item in unclear_scenarios:
            lines.append(
                f"- [{item.scenario_id}] source route unclear; source evidence: "
                f"{', '.join(item.evidence)}. Candidate: {item.subject}; "
                f"given {_worker_text(item.given)}, when {item.when}, "
                f"expect {_worker_text(item.then)}."
            )
    assumptions = [
        (item.scenario_id, value["dimension"], value["resolution"])
        for item in scenarios
        for value in item.assumptions
    ]
    if assumptions:
        lines.extend(("", "Defaults for behavior the source leaves unspecified:"))
        for scenario_id, dimension, resolution in assumptions:
            lines.append(f"- [{scenario_id}] {dimension}: {resolution}")
    lines.append(
        "Do not treat a passing test on one execution path as proof for another."
    )
    return "\n".join(lines)


def _worker_text(value: Any) -> str:
    """Render structured outcomes as readable prose without schema syntax."""
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_worker_text(item)}" for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "; ".join(_worker_text(item) for item in value)
    return str(value)


def validate_capability_matrix(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Require executable positive and negative evidence for declared capabilities."""
    if not raw:
        raise BehaviorContractError("capability matrix must not be empty")
    result = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        name = _text(item.get("capability"), f"capability {index} name")
        behavior = item.get("behavior")
        if behavior not in {"support", "reject", "fallback"}:
            raise BehaviorContractError(f"capability {name} has invalid behavior")
        if name in seen:
            raise BehaviorContractError(f"duplicate capability: {name}")
        seen.add(name)
        error = item.get("error")
        if behavior == "reject" and not _is_text(error):
            raise BehaviorContractError(
                f"rejected capability {name} needs a defined error"
            )
        result.append(
            {
                "capability": name,
                "behavior": behavior,
                "evidence": _texts(item.get("evidence"), f"capability {name} evidence"),
                **({"error": error} if behavior == "reject" else {}),
            }
        )
    return tuple(result)


def validate_normative_assumptions(
    raw: Any, *, expected_dimensions: Sequence[str] | None = None
) -> tuple[dict[str, str], ...]:
    """Validate explicit, auditable defaults used to resolve ambiguity."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise BehaviorContractError("assumptions must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise BehaviorContractError(f"assumption {index} must be an object")
        dimension = _text(item.get("dimension"), f"assumption {index} dimension")
        if dimension not in SUPPORTED_ASSUMPTION_DIMENSIONS:
            raise BehaviorContractError(
                f"assumption {index} names unsupported dimension {dimension!r}"
            )
        if dimension in seen:
            raise BehaviorContractError(f"duplicate assumption for {dimension}")
        seen.add(dimension)
        basis = _text(item.get("basis"), f"assumption {index} basis")
        if basis not in ASSUMPTION_BASES:
            raise BehaviorContractError(f"assumption {dimension} has invalid basis")
        confidence = _text(item.get("confidence"), f"assumption {index} confidence")
        if confidence not in {"high", "medium", "low"}:
            raise BehaviorContractError(
                f"assumption {dimension} has invalid confidence"
            )
        resolution = _text(item.get("resolution"), f"assumption {index} resolution")
        if _is_not_applicable(resolution):
            raise BehaviorContractError(
                f"assumption {dimension} cannot resolve to not_applicable"
            )
        result.append(
            {
                "dimension": dimension,
                "resolution": resolution,
                "rationale": _text(
                    item.get("rationale"), f"assumption {index} rationale"
                ),
                "basis": basis,
                "basis_reference": _text(
                    item.get("basis_reference"), f"assumption {index} basis_reference"
                ),
                "confidence": confidence,
            }
        )
    if expected_dimensions is not None and seen != set(expected_dimensions):
        missing = sorted(set(expected_dimensions) - seen)
        extra = sorted(seen - set(expected_dimensions))
        raise BehaviorContractError(
            "assumptions must cover every unresolved dimension exactly once"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(extra)}" if extra else "")
        )
    return tuple(result)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_not_applicable(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized == "not_applicable" or normalized.startswith(
        ("not_applicable ", "not_applicable-", "not_applicable—", "not_applicable:")
    )


def _text(value: Any, label: str) -> str:
    if not _is_text(value):
        raise BehaviorContractError(f"{label} must be non-empty text")
    return value.strip()


def _texts(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BehaviorContractError(f"{label} must be a list")
    result = tuple(_text(item, label) for item in value)
    if not result:
        raise BehaviorContractError(f"{label} must not be empty")
    return result


__all__ = [
    "BEHAVIOR_DIMENSIONS",
    "SUPPORTED_ASSUMPTION_DIMENSIONS",
    "ASSUMPTION_BASES",
    "BehaviorContractError",
    "BehaviorScenario",
    "compile_behavior_scenarios",
    "render_behavior_matrix",
    "validate_capability_matrix",
    "validate_normative_assumptions",
]
