"""Compile feature intent and proposed PR ownership into one durable contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FEATURE_DELIVERY_CONTRACT_SCHEMA_VERSION = "feature-delivery-contract-v1"

_COVERAGE_SECTIONS = ("requirements", "acceptance_criteria", "expected_tests")
_EFFECT_SECTIONS = (
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "features",
    "decisions",
)


@dataclass(frozen=True, slots=True)
class FeatureDeliveryAssignment:
    item_ref: str
    section: str
    item_id: str
    proposed_pr_id: str

    def to_data(self) -> dict[str, str]:
        return {
            "item_ref": self.item_ref,
            "section": self.section,
            "id": self.item_id,
            "proposed_pr_id": self.proposed_pr_id,
        }


@dataclass(frozen=True, slots=True)
class FeatureDeliveryEffect:
    effect_ref: str
    section: str
    item_id: str
    action: str
    proposed_pr_id: str

    def to_data(self) -> dict[str, str]:
        return {
            "effect_ref": self.effect_ref,
            "section": self.section,
            "id": self.item_id,
            "action": self.action,
            "proposed_pr_id": self.proposed_pr_id,
        }


@dataclass(frozen=True, slots=True)
class FeatureDeliveryPR:
    proposed_pr_id: str
    dependent_pr_ids: tuple[str, ...]
    coverage_refs: tuple[str, ...]
    effect_refs: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.proposed_pr_id,
            "dependent_pr_ids": list(self.dependent_pr_ids),
            "coverage_refs": list(self.coverage_refs),
            "effect_refs": list(self.effect_refs),
        }


@dataclass(frozen=True, slots=True)
class FeatureDeliveryContract:
    feature_id: str
    feature_fingerprint: str
    proposed_pr_fingerprint: str
    proposed_prs: tuple[FeatureDeliveryPR, ...]
    assignments: tuple[FeatureDeliveryAssignment, ...]
    effects: tuple[FeatureDeliveryEffect, ...]
    fingerprint: str
    schema_version: str = FEATURE_DELIVERY_CONTRACT_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "feature_fingerprint": self.feature_fingerprint,
            "proposed_pr_fingerprint": self.proposed_pr_fingerprint,
            "proposed_prs": [item.to_data() for item in self.proposed_prs],
            "assignments": [item.to_data() for item in self.assignments],
            "effects": [item.to_data() for item in self.effects],
            "contract_fingerprint": self.fingerprint,
        }


def build_feature_coverage_handoff(
    feature_specification: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact runtime-owned items that require proposed PR ownership."""
    feature_id = _required_string(feature_specification.get("id"), "feature.id")
    items: list[dict[str, str]] = []
    seen_refs: set[str] = set()
    for section in _COVERAGE_SECTIONS:
        values = _required_sequence(feature_specification.get(section), section)
        if not values:
            raise ValueError(f"feature.{section} must not be empty")
        for index, raw_item in enumerate(values):
            item = _required_mapping(raw_item, f"feature.{section}[{index}]")
            item_id = _required_string(item.get("id"), f"feature.{section}[{index}].id")
            item_ref = f"{section}:{item_id}"
            if item_ref in seen_refs:
                raise ValueError(f"feature.{section} duplicates id {item_id!r}")
            seen_refs.add(item_ref)
            items.append({"item_ref": item_ref, "section": section, "id": item_id})
    return {
        "feature_id": feature_id,
        "feature_fingerprint": _fingerprint(feature_specification),
        "items": items,
    }


def compile_feature_delivery_contract(
    feature_specification: Mapping[str, Any],
    proposed_pr_specification: Mapping[str, Any],
    coverage_allocation: Mapping[str, Any],
) -> FeatureDeliveryContract:
    """Compile total feature coverage after all semantic choices are explicit."""
    handoff = build_feature_coverage_handoff(feature_specification)
    feature_id = handoff["feature_id"]
    proposed_feature_id = _required_string(
        proposed_pr_specification.get("id"), "proposed_pr_specification.id"
    )
    if proposed_feature_id != feature_id:
        raise ValueError(
            "proposed PR specification id does not match feature id: "
            f"{proposed_feature_id!r} != {feature_id!r}"
        )

    proposed_prs, proposed_pr_ids = _compile_proposed_prs(proposed_pr_specification)
    _validate_feature_ids(feature_specification, proposed_pr_specification)
    assignments = _compile_coverage_assignments(
        handoff, coverage_allocation, proposed_pr_ids
    )
    effects = _compile_effects(
        feature_specification, proposed_pr_specification, proposed_pr_ids
    )

    coverage_by_pr: dict[str, list[str]] = {item: [] for item in proposed_pr_ids}
    for assignment in assignments:
        coverage_by_pr[assignment.proposed_pr_id].append(assignment.item_ref)
    effects_by_pr: dict[str, list[str]] = {item: [] for item in proposed_pr_ids}
    for effect in effects:
        effects_by_pr[effect.proposed_pr_id].append(effect.effect_ref)

    compiled_prs = tuple(
        FeatureDeliveryPR(
            item.proposed_pr_id,
            item.dependent_pr_ids,
            tuple(sorted(coverage_by_pr[item.proposed_pr_id])),
            tuple(sorted(effects_by_pr[item.proposed_pr_id])),
        )
        for item in proposed_prs
    )
    if empty_prs := [
        item.proposed_pr_id
        for item in compiled_prs
        if not item.coverage_refs and not item.effect_refs
    ]:
        raise ValueError(
            "proposed PRs have no assigned feature work: " + ", ".join(empty_prs)
        )

    feature_fingerprint = handoff["feature_fingerprint"]
    proposed_pr_fingerprint = _fingerprint(proposed_pr_specification)
    identity = {
        "schema_version": FEATURE_DELIVERY_CONTRACT_SCHEMA_VERSION,
        "feature_id": feature_id,
        "feature_fingerprint": feature_fingerprint,
        "proposed_pr_fingerprint": proposed_pr_fingerprint,
        "proposed_prs": [item.to_data() for item in compiled_prs],
        "assignments": [item.to_data() for item in assignments],
        "effects": [item.to_data() for item in effects],
    }
    return FeatureDeliveryContract(
        feature_id,
        feature_fingerprint,
        proposed_pr_fingerprint,
        compiled_prs,
        assignments,
        effects,
        _fingerprint(identity),
    )


def _compile_proposed_prs(
    specification: Mapping[str, Any],
) -> tuple[tuple[FeatureDeliveryPR, ...], tuple[str, ...]]:
    raw_prs = _required_sequence(specification.get("proposed_prs"), "proposed_prs")
    if not raw_prs:
        raise ValueError("proposed_prs must not be empty")
    records: list[FeatureDeliveryPR] = []
    ids: list[str] = []
    for index, raw_pr in enumerate(raw_prs):
        item = _required_mapping(raw_pr, f"proposed_prs[{index}]")
        proposed_pr_id = _required_string(item.get("id"), f"proposed_prs[{index}].id")
        if proposed_pr_id in ids:
            raise ValueError(f"proposed_prs duplicates id {proposed_pr_id!r}")
        dependencies = tuple(
            _required_string(value, f"proposed_prs[{index}].dependent_prs")
            for value in _required_sequence(
                item.get("dependent_prs"), f"proposed_prs[{index}].dependent_prs"
            )
        )
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"proposed PR {proposed_pr_id!r} duplicates a dependency")
        ids.append(proposed_pr_id)
        records.append(FeatureDeliveryPR(proposed_pr_id, dependencies, (), ()))
    known = set(ids)
    for record in records:
        unknown = set(record.dependent_pr_ids) - known
        if unknown:
            raise ValueError(
                f"proposed PR {record.proposed_pr_id!r} has unknown dependencies: "
                + ", ".join(sorted(unknown))
            )
        if record.proposed_pr_id in record.dependent_pr_ids:
            raise ValueError(f"proposed PR {record.proposed_pr_id!r} depends on itself")
    _validate_acyclic(records)
    return tuple(records), tuple(ids)


def _compile_coverage_assignments(
    handoff: Mapping[str, Any],
    allocation: Mapping[str, Any],
    proposed_pr_ids: tuple[str, ...],
) -> tuple[FeatureDeliveryAssignment, ...]:
    if set(allocation) != {"assignments"}:
        raise ValueError("coverage_allocation must contain exactly assignments")
    raw_assignments = _required_sequence(
        allocation.get("assignments"), "coverage_allocation.assignments"
    )
    expected = {item["item_ref"]: item for item in handoff["items"]}
    owners: dict[str, str] = {}
    for index, raw_assignment in enumerate(raw_assignments):
        item = _required_mapping(
            raw_assignment, f"coverage_allocation.assignments[{index}]"
        )
        if set(item) != {"item_ref", "proposed_pr_id"}:
            raise ValueError(
                f"coverage_allocation.assignments[{index}] must contain exactly "
                "item_ref and proposed_pr_id"
            )
        item_ref = _required_string(
            item.get("item_ref"), f"coverage_allocation.assignments[{index}].item_ref"
        )
        owner = _required_string(
            item.get("proposed_pr_id"),
            f"coverage_allocation.assignments[{index}].proposed_pr_id",
        )
        if item_ref not in expected:
            raise ValueError(
                f"coverage allocation references unknown item {item_ref!r}"
            )
        if item_ref in owners:
            raise ValueError(f"coverage allocation duplicates item {item_ref!r}")
        if owner not in proposed_pr_ids:
            raise ValueError(f"coverage allocation references unknown PR {owner!r}")
        owners[item_ref] = owner
    missing = set(expected) - set(owners)
    if missing:
        raise ValueError(
            "coverage allocation is incomplete: " + ", ".join(sorted(missing))
        )
    return tuple(
        FeatureDeliveryAssignment(
            item_ref,
            expected[item_ref]["section"],
            expected[item_ref]["id"],
            owners[item_ref],
        )
        for item_ref in sorted(expected)
    )


def _compile_effects(
    feature: Mapping[str, Any],
    proposed: Mapping[str, Any],
    proposed_pr_ids: tuple[str, ...],
) -> tuple[FeatureDeliveryEffect, ...]:
    expected: dict[str, tuple[str, str, str]] = {}
    for section in _EFFECT_SECTIONS:
        for index, raw_item in enumerate(
            _optional_sequence(feature.get(section), section)
        ):
            item = _required_mapping(raw_item, f"feature.{section}[{index}]")
            action = item.get("action")
            if action is None:
                continue
            item_id = _required_string(item.get("id"), f"feature.{section}[{index}].id")
            action_text = _required_string(action, f"feature.{section}[{index}].action")
            effect_ref = f"{section}:{item_id}:{action_text}"
            if effect_ref in expected:
                raise ValueError(f"feature duplicates effect {effect_ref!r}")
            expected[effect_ref] = (section, item_id, action_text)

    actual: dict[str, FeatureDeliveryEffect] = {}
    for section in _EFFECT_SECTIONS:
        for index, raw_item in enumerate(
            _optional_sequence(proposed.get(section), section)
        ):
            item = _required_mapping(raw_item, f"proposed.{section}[{index}]")
            item_id = _required_string(
                item.get("id"), f"proposed.{section}[{index}].id"
            )
            action = _required_string(
                item.get("action"), f"proposed.{section}[{index}].action"
            )
            owner = _required_string(
                item.get("proposed_pr_id"),
                f"proposed.{section}[{index}].proposed_pr_id",
            )
            if owner not in proposed_pr_ids:
                raise ValueError(f"effect references unknown PR {owner!r}")
            effect_ref = f"{section}:{item_id}:{action}"
            if effect_ref in actual:
                raise ValueError(
                    f"proposed PR specification duplicates effect {effect_ref!r}"
                )
            actual[effect_ref] = FeatureDeliveryEffect(
                effect_ref, section, item_id, action, owner
            )
    missing = set(expected) - set(actual)
    if missing:
        raise ValueError(
            "proposed PR effects omit feature effects: " + ", ".join(sorted(missing))
        )
    return tuple(actual[item_ref] for item_ref in sorted(actual))


def _validate_feature_ids(
    feature: Mapping[str, Any], proposed: Mapping[str, Any]
) -> None:
    expected: set[str] = set()
    for index, raw_item in enumerate(
        _optional_sequence(feature.get("features"), "features")
    ):
        item = _required_mapping(raw_item, f"feature.features[{index}]")
        if item.get("action") is not None:
            expected.add(
                _required_string(item.get("id"), f"feature.features[{index}].id")
            )
    actual = {
        _required_string(value, "proposed_pr_specification.feature_ids")
        for value in _required_sequence(
            proposed.get("feature_ids"), "proposed_pr_specification.feature_ids"
        )
    }
    if expected != actual:
        raise ValueError(
            "proposed PR feature_ids do not match feature effects: "
            f"expected {sorted(expected)!r}, got {sorted(actual)!r}"
        )


def _validate_acyclic(records: Sequence[FeatureDeliveryPR]) -> None:
    dependencies = {item.proposed_pr_id: item.dependent_pr_ids for item in records}
    complete: set[str] = set()
    active: set[str] = set()

    def visit(proposed_pr_id: str) -> None:
        if proposed_pr_id in complete:
            return
        if proposed_pr_id in active:
            raise ValueError("proposed PR dependencies contain a cycle")
        active.add(proposed_pr_id)
        for dependency in dependencies[proposed_pr_id]:
            visit(dependency)
        active.remove(proposed_pr_id)
        complete.add(proposed_pr_id)

    for proposed_pr_id in dependencies:
        visit(proposed_pr_id)


def _required_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return value


def _required_sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{path} must be an array")
    return value


def _optional_sequence(value: Any, path: str) -> Sequence[Any]:
    if value is None:
        return ()
    return _required_sequence(value, path)


def _required_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value.strip()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
