"""Lossless, typed behavior contracts for coding-worker handoffs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

BEHAVIOR_DIMENSIONS = (
    "normal_result",
    "error_behavior",
    "continuation",
    "unsupported_behavior",
    "cancellation_cleanup",
    "compatibility",
    "negative_boundaries",
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
    capability_matrix: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = "behavior-scenario-v1"

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "subject": self.subject,
            "given": self.given,
            "when": self.when,
            "then": self.then,
            "dimensions": {name: self.dimensions[name] for name in BEHAVIOR_DIMENSIONS},
            "evidence": list(self.evidence),
            "validator": self.validator,
            "capability_matrix": [dict(item) for item in self.capability_matrix],
        }


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
                capability_matrix=capabilities,
            )
        )
    return tuple(scenarios)


def render_behavior_matrix(scenarios: Sequence[BehaviorScenario]) -> str:
    """Render the behavior contract once, without duplicate prose sections."""
    rows = []
    for item in scenarios:
        rows.append(
            "- "
            + json.dumps(
                {
                    "scenario": item.scenario_id,
                    "subject": item.subject,
                    "given": item.given,
                    "when": item.when,
                    "then": item.then,
                    **item.dimensions,
                    "validator": item.validator,
                    "evidence": list(item.evidence),
                    "capability_matrix": [
                        dict(value) for value in item.capability_matrix
                    ],
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    return (
        "Behavior contract matrix (implement and verify each row exactly once):\n"
        + "\n".join(rows)
        + "\nRun the focused required tests after implementation."
    )


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
                **({"error": error} if error is not None else {}),
            }
        )
    return tuple(result)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
    "BehaviorContractError",
    "BehaviorScenario",
    "compile_behavior_scenarios",
    "render_behavior_matrix",
    "validate_capability_matrix",
]
