"""Generic relationship invariants for durable workflow metadata."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class WorkflowRelationshipValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRelationshipValidationReport:
    validation_successful: bool
    relationships_checked: int = 0
    issues: list[WorkflowRelationshipValidationIssue] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return {
            "validation_successful": self.validation_successful,
            "relationships_checked": self.relationships_checked,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    **({"path": issue.path} if issue.path is not None else {}),
                }
                for issue in self.issues
            ],
        }


def validate_workflow_relationships(
    workflow_state_paths: Sequence[str | Path],
    *,
    required_invariant_ids: Sequence[str] = (),
    required_invariants: Sequence[Mapping[str, Any]] = (),
) -> WorkflowRelationshipValidationReport:
    """Validate declared workflow relationship invariants deterministically.

    Workflow state is deliberately the only source of identity. This function
    never derives relationships from filenames, directory names, or file
    pointers. Relationship targets are semantic identifiers.
    """
    issues: list[WorkflowRelationshipValidationIssue] = []
    relationships: list[tuple[Path, int, Mapping[str, Any]]] = []
    invariants: list[tuple[Path, int, Mapping[str, Any]]] = []
    invariant_ids_by_path: dict[Path, set[str]] = defaultdict(set)

    if not workflow_state_paths:
        return WorkflowRelationshipValidationReport(
            validation_successful=False,
            issues=[
                WorkflowRelationshipValidationIssue(
                    "missing_workflow_state",
                    "No durable workflow state files were found in the requested "
                    "scope.",
                )
            ],
        )

    for raw_path in workflow_state_paths:
        path = Path(raw_path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "invalid_workflow_state", str(exc), str(path)
                )
            )
            continue
        if not isinstance(data, Mapping):
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "invalid_workflow_state",
                    "Workflow state must be a mapping.",
                    str(path),
                )
            )
            continue
        raw_invariants = data.get("invariants", [])
        raw_relationships = data.get("relationships", [])
        if not isinstance(raw_invariants, list):
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "invalid_invariants",
                    "invariants must be a list.",
                    f"{path}:invariants",
                )
            )
            raw_invariants = []
        if not isinstance(raw_relationships, list):
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "invalid_relationships",
                    "relationships must be a list.",
                    f"{path}:relationships",
                )
            )
            raw_relationships = []
        for index, item in enumerate(raw_invariants):
            if isinstance(item, Mapping):
                invariants.append((path, index, item))
                invariant_id = item.get("id")
                if isinstance(invariant_id, str) and invariant_id.strip():
                    invariant_ids_by_path[path].add(invariant_id.strip())
            else:
                issues.append(
                    WorkflowRelationshipValidationIssue(
                        "invalid_relationship_invariant",
                        "Each relationship invariant must be a mapping.",
                        f"{path}:invariants[{index}]",
                    )
                )

        missing_required = set(required_invariant_ids) - invariant_ids_by_path[path]
        for invariant_id in sorted(missing_required):
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "missing_required_relationship_invariant",
                    f"Required relationship invariant is not declared: {invariant_id}",
                    f"{path}:invariants",
                )
            )
        for index, item in enumerate(raw_relationships):
            if isinstance(item, Mapping):
                relationships.append((path, index, item))
            else:
                issues.append(
                    WorkflowRelationshipValidationIssue(
                        "invalid_relationship",
                        "Each workflow relationship must be a mapping.",
                        f"{path}:relationships[{index}]",
                    )
                )

    relationships_by_invariant: dict[str, list[tuple[Path, int, Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    for path, index, relation in relationships:
        relation_id = _required_string(relation, "invariant_id", path, index, issues)
        _required_string(relation, "relationship", path, index, issues)
        _required_string(relation, "source_type", path, index, issues)
        _required_string(relation, "source_id", path, index, issues)
        _required_string(relation, "target_type", path, index, issues)
        _required_string(relation, "target_id", path, index, issues)
        if "target_path" in relation:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "file_pointer_forbidden",
                    "Workflow relationships must use target_id, not target_path.",
                    f"{path}:relationships[{index}].target_path",
                )
            )
        if relation_id is not None:
            relationships_by_invariant[relation_id].append((path, index, relation))

    seen_invariants: set[str] = set()
    expected_by_id = {
        item.get("id"): item
        for item in required_invariants
        if isinstance(item.get("id"), str) and item.get("id")
    }
    for path, index, invariant in invariants:
        invariant_id = _required_string(invariant, "id", path, index, issues)
        relationship = _required_string(invariant, "relationship", path, index, issues)
        cardinality = invariant.get("cardinality", "exactly_one")
        if cardinality not in {"exactly_one", "at_least_one", "at_most_one"}:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "invalid_cardinality",
                    "cardinality must be exactly_one, at_least_one, or at_most_one.",
                    f"{path}:invariants[{index}].cardinality",
                )
            )
        if invariant_id is None or invariant_id in seen_invariants:
            if invariant_id is not None:
                issues.append(
                    WorkflowRelationshipValidationIssue(
                        "duplicate_relationship_invariant",
                        f"Relationship invariant id is duplicated: {invariant_id}",
                        f"{path}:invariants[{index}].id",
                    )
                )
            continue
        seen_invariants.add(invariant_id)
        expected = expected_by_id.get(invariant_id)
        if expected is not None:
            for key in ("relationship", "source_type", "target_type", "cardinality"):
                expected_value = expected.get(key)
                actual_value = invariant.get(key)
                if (
                    isinstance(expected_value, str)
                    and not _is_placeholder(expected_value)
                    and actual_value != expected_value
                ):
                    issues.append(
                        WorkflowRelationshipValidationIssue(
                            "relationship_invariant_mismatch",
                            f"Invariant {invariant_id!r} has {key}={actual_value!r}; "
                            f"the template requires {expected_value!r}.",
                            f"{path}:invariants[{index}].{key}",
                        )
                    )
        matching = relationships_by_invariant.get(invariant_id, [])
        if relationship is not None:
            matching = [
                item for item in matching if item[2].get("relationship") == relationship
            ]
        count = len(matching)
        if cardinality == "exactly_one" and count != 1:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "relationship_cardinality_failed",
                    f"Invariant {invariant_id!r} requires exactly one relationship; "
                    f"found {count}.",
                    f"{path}:invariants[{index}]",
                )
            )
        elif cardinality == "at_least_one" and count < 1:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "relationship_cardinality_failed",
                    f"Invariant {invariant_id!r} requires at least one relationship; "
                    "found none.",
                    f"{path}:invariants[{index}]",
                )
            )
        elif cardinality == "at_most_one" and count > 1:
            issues.append(
                WorkflowRelationshipValidationIssue(
                    "relationship_cardinality_failed",
                    f"Invariant {invariant_id!r} allows at most one relationship; "
                    f"found {count}.",
                    f"{path}:invariants[{index}]",
                )
            )

    return WorkflowRelationshipValidationReport(
        validation_successful=not issues,
        relationships_checked=len(relationships),
        issues=issues,
    )


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _required_string(
    data: Mapping[str, Any],
    key: str,
    path: Path,
    index: int,
    issues: list[WorkflowRelationshipValidationIssue],
) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        issues.append(
            WorkflowRelationshipValidationIssue(
                "missing_relationship_field",
                f"Relationship field {key!r} must be a non-empty string.",
                f"{path}:relationships[{index}].{key}",
            )
        )
        return None
    return value.strip()
