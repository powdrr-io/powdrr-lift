from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import yaml
from powdrr_service.specifications.proposed_pr_coverage import (
    ProposedPRSourceDocument,
)
from powdrr_service.specifications.proposed_pr_validation import (
    build_proposed_pr_dependency_graph as _build_service_pr_dependency_graph,
)
from powdrr_service.specifications.proposed_pr_validation import (
    build_proposed_pr_specification_validation_report as _service_validate_pr_spec,
)

from powdrr_lift.core.codebase_state import build_codebase_state_report
from powdrr_lift.core.repo import resolve_repo_root as _resolve_repo_root
from powdrr_lift.core.spec_paths import CURRENT_ROOT, PROPOSALS_ROOT
from powdrr_lift.core.template_generation import merge_existing_template_content
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

_DEFAULT_OUTPUT_PATH = PROPOSALS_ROOT
_IMPLEMENTATION_SPECIFICATION_DIRS = (
    CURRENT_ROOT,
    PROPOSALS_ROOT,
)
_EFFECT_SECTIONS = (
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "features",
    "decisions",
)
_EFFECT_SOURCE_FILES = (
    "architecture-specification.yaml",
    "implementation-specification.yaml",
)
_PROPOSED_PR_SCHEMA = "https://powdrr.io/schemas/proposed-pr-specification-v1"


@dataclass(frozen=True, slots=True)
class PRSpecificationValidationIssue(ValidationError):
    pass


@dataclass(frozen=True, slots=True)
class PRSpecificationValidationReport:
    validation_successful: bool
    proposed_pr_id: str | None
    available_feature_ids: list[str] = field(default_factory=list)
    known_pr_ids: list[str] = field(default_factory=list)
    issues: list[PRSpecificationValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _FeatureCatalogEntry:
    feature_id: str
    source_path: str
    entity_type: str | None


@dataclass(frozen=True, slots=True)
class ProposedPRSearchResult:
    pr_number: int | None
    proposed_pr_id: str | None
    path: Path
    score: float
    matched_fields: tuple[str, ...] = field(default_factory=tuple)
    feature_ids: tuple[str, ...] = field(default_factory=tuple)
    intent_goal: str | None = None
    intent_reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedPRSearchReport:
    query: str
    results: list[ProposedPRSearchResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ProposedPRDocument:
    pr_number: int | None
    path: Path
    data: Mapping[str, Any]
    proposed_pr_id: str | None
    feature_ids: tuple[str, ...]
    intent_goal: str | None
    intent_reasoning: str | None


def pr_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return (
        repo_root_path
        / _DEFAULT_OUTPUT_PATH
        / work_item_name
        / "proposed-pr-specification.yaml"
    )


def render_pr_specification_template(
    *,
    repo_root: str | Path | None = None,
    work_item_name: str | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    feature_catalog = _load_feature_catalog(repo_root_path)
    if not feature_catalog:
        raise ValueError(
            "No current feature ids were found in the codebase state. "
            "Generate the codebase state first and ensure it contains current "
            "entities before generating a PR specification."
        )

    feature_lines = [
        (
            f"# - {entry.feature_id} "
            f"({entry.entity_type or 'entity'}, {entry.source_path})"
        )
        for entry in feature_catalog
    ]

    authoritative_effects = _load_authoritative_effects(
        repo_root_path,
        work_item_name=work_item_name,
    )
    lines = [
        "# PR specification template.",
        "#",
        "# Instructions:",
        "# - Keep every proposed PR for this feature in this one file.",
        "# - Set the top-level `id` to the feature/work-item id.",
        "# - Add one ordered entry under `proposed_prs` for each proposed PR.",
        "# - Set each proposed PR `id` to a globally unique proposed PR id.",
        "# - List prerequisite proposed PR ids under each entry's `dependent_prs`.",
        "# - Dependency ids must identify another entry in this same file; cycles",
        "#   are not allowed.",
        "# - Reference one or more current feature ids from the codebase state",
        "#   listed below.",
        "# - Fill in each proposed PR's intent, justification, and dependencies.",
        "# - List the authoritative requirement, acceptance-criterion, and test ids",
        "#   delivered or proven by each proposed PR under its coverage sections.",
        "# - This document has one flat top-level mapping. Do not create a",
        "#   `specification_v1` wrapper or nest these sections under another key.",
        "#   The only top-level sections are `schema`, `id`, `feature_ids`,",
        "#   `proposed_prs`, `entities`, `modules`, `tools`,",
        "#   `entity_relationships`, `features`, and `decisions`.",
        "# - In the feature-wide specification-v1 sections below",
        "#   (`entities`, `modules`, `tools`, `entity_relationships`, `features`,",
        "#   and `decisions`), record every action-bearing id once and label each",
        "#   item with the proposed_pr_id that will deliver it.",
        "# - Preserve related_modules on each item to carry module scope through",
        "#   planning and implementation. One item may reference many modules.",
        "#   These IDs and actions are copied from the authoritative v1 proposal",
        "#   specs and must not be changed, omitted, or duplicated.",
        "# - Delete these instructions and replace them with this comment at the top:",
        '#   "# This file is read-only and should never be edited by a tool or agent."',
        "#",
        "# Current feature ids:",
        *feature_lines,
        "schema: https://powdrr.io/schemas/proposed-pr-specification-v1",
        "id: null",
        "feature_ids:",
        "  - null",
        "proposed_prs:",
        "  - id: null",
        "    intent: null",
        "    justification: null",
        "    dependent_prs: []",
        "    requirements: []",
        "    acceptance_criteria: []",
        "    expected_tests: []",
        "    required_test_cases:",
        "      - id: null",
        "        description: null",
        "        intent_refs: []",
        "        provider: null",
        "        selector: null",
        "        profile: null",
        "        expectation: pass",
        "        applicability:",
        "          mode: affected_closure",
        "        protected_inputs: []",
        "        status: active",
        *[
            line
            for section in _EFFECT_SECTIONS
            for line in _render_authoritative_effect_section(
                section,
                authoritative_effects.get(section, ()),
            )
        ],
        "",
    ]
    return "\n".join(lines)


def build_authoritative_effect_handoff(
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return ordered authoritative effects with stable, opaque references."""
    repo_root_path = _resolve_repo_root(repo_root)
    authoritative_effects = _load_authoritative_effects(
        repo_root_path,
        work_item_name=work_item_name,
    )
    effects: list[dict[str, Any]] = []
    referenced_effects: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
    for section in _EFFECT_SECTIONS:
        for item_id, action, related_modules in authoritative_effects[section]:
            effect = (section, item_id, action, related_modules)
            digest_input = json.dumps(
                effect, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            effect_ref = "effect-" + hashlib.sha256(digest_input).hexdigest()[:20]
            collision = referenced_effects.get(effect_ref)
            if collision is not None and collision != effect:
                raise ValueError(
                    f"Stable effect reference collision for {effect_ref!r}: "
                    f"{collision!r} and {effect!r}."
                )
            referenced_effects[effect_ref] = effect
            effects.append(
                {
                    "effect_ref": effect_ref,
                    "section": section,
                    "id": item_id,
                    "action": action,
                    **(
                        {"related_modules": list(related_modules)}
                        if related_modules
                        else {}
                    ),
                }
            )
    return {"effects": effects}


def compile_split_pr_specification(
    proposed_pr_plan: Mapping[str, Any],
    effect_allocation: Mapping[str, Any],
    authoritative_effect_handoff: Mapping[str, Any],
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compile separately validated planning and allocation decisions."""
    repo_root_path = _resolve_repo_root(repo_root)
    _require_exact_keys(
        proposed_pr_plan,
        expected={"proposed_prs"},
        path="proposed_pr_plan",
    )
    raw_prs = _require_semantic_sequence(
        proposed_pr_plan["proposed_prs"],
        path="proposed_pr_plan.proposed_prs",
    )
    if not raw_prs:
        raise ValueError("proposed_pr_plan.proposed_prs must not be empty.")

    proposed_prs: list[dict[str, Any]] = []
    proposed_pr_ids: set[str] = set()
    normalized_pr_ids: set[str] = set()
    for index, raw_pr in enumerate(raw_prs):
        path = f"proposed_pr_plan.proposed_prs[{index}]"
        pr = _require_semantic_mapping(raw_pr, path=path)
        _require_exact_keys(
            pr,
            expected={"id", "intent", "justification", "dependent_pr_ids"},
            path=path,
        )
        proposed_pr_id = _require_semantic_string(pr["id"], path=f"{path}.id")
        normalized_pr_id = _normalize_identifier(proposed_pr_id)
        if normalized_pr_id in normalized_pr_ids:
            raise ValueError(f"{path}.id duplicates proposed PR id {proposed_pr_id!r}.")
        proposed_pr_ids.add(proposed_pr_id)
        normalized_pr_ids.add(normalized_pr_id)
        dependencies = [
            _require_semantic_string(
                value, path=f"{path}.dependent_pr_ids[{dep_index}]"
            )
            for dep_index, value in enumerate(
                _require_semantic_sequence(
                    pr["dependent_pr_ids"],
                    path=f"{path}.dependent_pr_ids",
                )
            )
        ]
        proposed_prs.append(
            {
                "id": proposed_pr_id,
                "intent": _require_semantic_string(pr["intent"], path=f"{path}.intent"),
                "justification": _require_semantic_string(
                    pr["justification"], path=f"{path}.justification"
                ),
                "dependent_prs": dependencies,
            }
        )

    known_existing_ids = {
        _normalize_identifier(value)
        for value in _load_existing_pr_ids(
            repo_root_path,
            excluded_file_path=file_path,
        )
    }
    duplicate_existing_ids = sorted(normalized_pr_ids & known_existing_ids)
    if duplicate_existing_ids:
        raise ValueError(
            "semantic_specification proposes ids that already exist: "
            + ", ".join(duplicate_existing_ids)
        )

    for index, proposed_pr in enumerate(proposed_prs):
        for dependency in proposed_pr["dependent_prs"]:
            if dependency not in proposed_pr_ids:
                raise ValueError(
                    "proposed_pr_plan.proposed_prs"
                    f"[{index}].dependent_pr_ids references unknown proposed PR "
                    f"id {dependency!r}."
                )

    expected_handoff = build_authoritative_effect_handoff(
        work_item_name=work_item_name,
        repo_root=repo_root_path,
    )
    _require_exact_keys(
        authoritative_effect_handoff,
        expected={"effects"},
        path="authoritative_effect_handoff",
    )
    raw_effects = _require_semantic_sequence(
        authoritative_effect_handoff["effects"],
        path="authoritative_effect_handoff.effects",
    )
    effects: list[dict[str, str]] = []
    for index, raw_effect in enumerate(raw_effects):
        path = f"authoritative_effect_handoff.effects[{index}]"
        effect = _require_semantic_mapping(raw_effect, path=path)
        required_effect_fields = {"effect_ref", "section", "id", "action"}
        if not required_effect_fields <= set(effect) or set(effect) - (
            required_effect_fields | {"related_modules"}
        ):
            raise ValueError(f"{path} has invalid keys.")
        parsed_effect: dict[str, Any] = {
            field: _require_semantic_string(effect[field], path=f"{path}.{field}")
            for field in ("effect_ref", "section", "id", "action")
        }
        if "related_modules" in effect:
            parsed_effect["related_modules"] = _require_semantic_string_list(
                effect["related_modules"], path=f"{path}.related_modules"
            )
        effects.append(parsed_effect)
    if authoritative_effect_handoff != expected_handoff:
        raise ValueError(
            "authoritative_effect_handoff does not exactly match the current "
            "ordered authoritative effects."
        )

    _require_exact_keys(
        effect_allocation,
        expected={"assignments"},
        path="effect_allocation",
    )
    raw_assignments = _require_semantic_sequence(
        effect_allocation["assignments"],
        path="effect_allocation.assignments",
    )
    expected_refs = {effect["effect_ref"] for effect in effects}
    assignments: dict[str, str] = {}
    for index, raw_assignment in enumerate(raw_assignments):
        path = f"effect_allocation.assignments[{index}]"
        assignment = _require_semantic_mapping(raw_assignment, path=path)
        _require_exact_keys(
            assignment,
            expected={"effect_ref", "proposed_pr_id"},
            path=path,
        )
        effect_ref = _require_semantic_string(
            assignment["effect_ref"], path=f"{path}.effect_ref"
        )
        if effect_ref not in expected_refs:
            raise ValueError(f"{path} references unknown effect_ref {effect_ref!r}.")
        if effect_ref in assignments:
            raise ValueError(f"{path} duplicates effect_ref {effect_ref!r}.")
        proposed_pr_id = _require_semantic_string(
            assignment["proposed_pr_id"], path=f"{path}.proposed_pr_id"
        )
        if proposed_pr_id not in proposed_pr_ids:
            raise ValueError(
                f"{path}.proposed_pr_id references unknown proposed PR id "
                f"{proposed_pr_id!r}."
            )
        assignments[effect_ref] = proposed_pr_id

    missing_refs = sorted(expected_refs - set(assignments))
    if missing_refs:
        raise ValueError(
            "effect_allocation.assignments is missing authoritative effect_refs: "
            + ", ".join(repr(effect_ref) for effect_ref in missing_refs)
        )

    feature_ids = [
        effect["id"] for effect in effects if effect["section"] == "features"
    ]
    if not feature_ids:
        raise ValueError(
            "Cannot derive feature_ids because the authoritative implementation "
            "specification has no feature effects."
        )
    compiled: dict[str, Any] = {
        "schema": _PROPOSED_PR_SCHEMA,
        "id": work_item_name,
        "feature_ids": feature_ids,
        "proposed_prs": proposed_prs,
    }
    for section in _EFFECT_SECTIONS:
        compiled[section] = [
            {
                "id": effect["id"],
                "action": effect["action"],
                **(
                    {"related_modules": effect["related_modules"]}
                    if effect.get("related_modules")
                    else {}
                ),
                "proposed_pr_id": assignments[effect["effect_ref"]],
            }
            for effect in effects
            if effect["section"] == section
        ]

    compiled_yaml = yaml.safe_dump(compiled, sort_keys=False)
    report = build_pr_specification_validation_report(
        compiled_yaml,
        work_item_name=work_item_name,
        repo_root=repo_root_path,
        file_path=file_path,
    )
    if not report.validation_successful:
        details = "; ".join(
            f"{issue.path or '<document>'}: {issue.message}" for issue in report.issues
        )
        raise ValueError(f"Compiled proposed PR specification is invalid: {details}")
    return compiled


def compile_semantic_pr_specification(
    semantic_specification: Mapping[str, Any],
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility adapter for the superseded combined semantic contract."""
    _require_exact_keys(
        semantic_specification,
        expected={"proposed_prs", "effect_assignments"},
        path="semantic_specification",
    )
    handoff = build_authoritative_effect_handoff(
        work_item_name=work_item_name,
        repo_root=repo_root,
    )
    refs_by_effect = {
        (effect["section"], effect["id"], effect["action"]): effect["effect_ref"]
        for effect in handoff["effects"]
    }
    assignments = []
    assigned_effects: set[tuple[str, str, str]] = set()
    for index, raw_assignment in enumerate(
        _require_semantic_sequence(
            semantic_specification["effect_assignments"],
            path="semantic_specification.effect_assignments",
        )
    ):
        path = f"semantic_specification.effect_assignments[{index}]"
        assignment = _require_semantic_mapping(raw_assignment, path=path)
        _require_exact_keys(
            assignment,
            expected={"section", "id", "action", "proposed_pr_id"},
            path=path,
        )
        effect = (
            _require_semantic_string(assignment["section"], path=f"{path}.section"),
            _require_semantic_string(assignment["id"], path=f"{path}.id"),
            _require_semantic_string(assignment["action"], path=f"{path}.action"),
        )
        effect_ref = refs_by_effect.get(effect)
        if effect_ref is None:
            raise ValueError(f"{path} references unknown effect {effect!r}.")
        if effect in assigned_effects:
            raise ValueError(f"{path} duplicates effect assignment {effect!r}.")
        assigned_effects.add(effect)
        assignments.append(
            {
                "effect_ref": effect_ref,
                "proposed_pr_id": assignment["proposed_pr_id"],
            }
        )
    missing_effects = set(refs_by_effect) - assigned_effects
    if missing_effects:
        raise ValueError(
            "semantic_specification.effect_assignments is missing authoritative "
            "effects: " + ", ".join(repr(effect) for effect in sorted(missing_effects))
        )
    return compile_split_pr_specification(
        {"proposed_prs": semantic_specification["proposed_prs"]},
        {"assignments": assignments},
        handoff,
        work_item_name=work_item_name,
        repo_root=repo_root,
        file_path=file_path,
    )


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    expected: set[str],
    path: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(f"{path} has invalid keys ({'; '.join(details)}).")


def _require_semantic_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object.")
    return value


def _require_semantic_sequence(value: Any, *, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{path} must be an array.")
    return value


def _require_semantic_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string.")
    return value.strip()


def _require_semantic_string_list(value: Any, *, path: str) -> list[str]:
    values = _require_semantic_sequence(value, path=path)
    if not values:
        raise ValueError(f"{path} must not be empty.")
    return [
        _require_semantic_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(values)
    ]


def _load_authoritative_effects(
    repo_root: Path,
    *,
    work_item_name: str | None,
) -> dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]]:
    effects: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
        section: [] for section in _EFFECT_SECTIONS
    }
    if work_item_name is None:
        return {section: tuple(items) for section, items in effects.items()}
    proposal_dir = repo_root / PROPOSALS_ROOT / work_item_name
    seen: dict[str, dict[tuple[str, str], int]] = {
        section: {} for section in _EFFECT_SECTIONS
    }
    for filename in _EFFECT_SOURCE_FILES:
        path = proposal_dir / filename
        if not path.is_file():
            continue
        try:
            data = _load_yaml_mapping(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for section in _EFFECT_SECTIONS:
            raw_items = data.get(section, [])
            if not isinstance(raw_items, Sequence) or isinstance(
                raw_items, (str, bytes)
            ):
                continue
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                item_id = _optional_string(item.get("id"))
                action = _optional_string(item.get("action"))
                if item_id is None or action is None:
                    continue
                effect = (item_id, action)
                related_modules = tuple(
                    dict.fromkeys(_string_values(item.get("related_modules")))
                )
                effect_index = seen[section].get(effect)
                if effect_index is None:
                    seen[section][effect] = len(effects[section])
                    effects[section].append((*effect, related_modules))
                elif related_modules:
                    existing = effects[section][effect_index]
                    if existing[2]:
                        effects[section][effect_index] = (
                            existing[0],
                            existing[1],
                            tuple(dict.fromkeys((*existing[2], *related_modules))),
                        )
    return {section: tuple(items) for section, items in effects.items()}


def _render_authoritative_effect_section(
    section: str,
    effects: Sequence[tuple[str, str, tuple[str, ...]]],
) -> list[str]:
    lines = [f"{section}:"]
    if not effects:
        lines[-1] = f"{section}: []"
        return lines
    for item_id, action, related_modules in effects:
        lines.extend(
            [
                f"  - id: {item_id}",
                f"    action: {action}",
                *(
                    [f"    related_modules: {json.dumps(related_modules)}"]
                    if related_modules
                    else []
                ),
                "    proposed_pr_id: null",
            ]
        )
    return lines


def create_pr_specification_template(
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(
        repo_root_path,
        work_item_name,
        output_path,
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = (
        resolved_output_path.read_text(encoding="utf-8")
        if resolved_output_path.is_file()
        else None
    )
    resolved_output_path.write_text(
        merge_existing_template_content(
            render_pr_specification_template(
                repo_root=repo_root_path,
                work_item_name=work_item_name,
            ),
            existing_content,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def proposed_pr_specification_path(
    pr_number: int,
    *,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    for specification_path in _iter_proposed_pr_specification_paths(repo_root_path):
        if _parse_proposed_pr_number(specification_path) == pr_number:
            return specification_path

    proposal_path = (
        repo_root_path
        / PROPOSALS_ROOT
        / f"PR-{pr_number}"
        / "proposed-pr-specification.yaml"
    )
    return proposal_path


def show_proposed_pr_specification(
    pr_number: int,
    *,
    repo_root: str | Path | None = None,
) -> str:
    specification_path = proposed_pr_specification_path(pr_number, repo_root=repo_root)
    if not specification_path.exists():
        raise FileNotFoundError(
            f"Proposed PR specification not found: {specification_path}"
        )
    return specification_path.read_text(encoding="utf-8")


def search_proposed_pr_specifications(
    query: str,
    *,
    repo_root: str | Path | None = None,
    limit: int = 10,
) -> ProposedPRSearchReport:
    repo_root_path = _resolve_repo_root(repo_root)
    normalized_query = query.strip()
    if normalized_query == "":
        raise ValueError("Query must not be empty.")

    documents = _load_proposed_pr_documents(repo_root_path)
    results = sorted(
        (
            _score_proposed_pr_document(normalized_query, document)
            for document in documents
        ),
        key=lambda result: (
            -result.score,
            result.pr_number if result.pr_number is not None else float("inf"),
            result.path.name,
        ),
    )
    filtered_results = [result for result in results if result.score > 0.0][:limit]
    return ProposedPRSearchReport(query=normalized_query, results=filtered_results)


def render_proposed_pr_search_report(report: ProposedPRSearchReport) -> str:
    return yaml.safe_dump(
        _proposed_pr_search_report_to_data(report),
        sort_keys=False,
        allow_unicode=False,
    )


def validate_pr_specification_yaml(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> str:
    report = build_pr_specification_validation_report(
        proposed_pr_specification_yaml,
        work_item_name=work_item_name,
        repo_root=repo_root,
        file_path=file_path,
    )
    return yaml.safe_dump(_report_to_data(report, file_path=file_path), sort_keys=False)


def build_pr_specification_validation_report(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> PRSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    feature_catalog = _load_feature_catalog(repo_root_path)
    available_feature_ids = [entry.feature_id for entry in feature_catalog]
    document_evidence = _load_proposed_pr_validation_documents(repo_root_path)
    excluded_path = _resolved_local_file_path(repo_root_path, file_path)
    existing_documents = tuple(
        document
        for document in document_evidence
        if excluded_path is None or Path(document.path).resolve() != excluded_path
    )
    service_report = _service_validate_pr_spec(
        proposed_pr_specification_yaml,
        available_feature_ids=available_feature_ids,
        existing_pr_specifications=existing_documents,
        dependency_pr_specifications=document_evidence,
        feature_coverage_source=_load_feature_coverage_source(
            repo_root=repo_root_path,
            work_item_name=work_item_name,
        ),
        effect_sources=_load_effect_sources(
            repo_root=repo_root_path,
            work_item_name=work_item_name,
            file_path=file_path,
        ),
    )
    return PRSpecificationValidationReport(
        validation_successful=service_report.validation_successful,
        proposed_pr_id=service_report.proposed_pr_id,
        available_feature_ids=service_report.available_feature_ids,
        known_pr_ids=service_report.known_pr_ids,
        issues=[
            PRSpecificationValidationIssue(
                code=issue.code,
                message=issue.message,
                path=issue.path,
            )
            for issue in service_report.issues
        ],
    )


def _load_feature_coverage_source(
    *, repo_root: Path, work_item_name: str
) -> ProposedPRSourceDocument | None:
    source_path = (
        repo_root / PROPOSALS_ROOT / work_item_name / "feature-pr-specification.yaml"
    )
    if not source_path.is_file():
        return None
    try:
        return ProposedPRSourceDocument(
            path=str(source_path),
            yaml_content=source_path.read_text(encoding="utf-8"),
        )
    except Exception as exc:  # noqa: BLE001
        return ProposedPRSourceDocument(path=str(source_path), read_error=str(exc))


def _load_effect_sources(
    *,
    repo_root: Path,
    work_item_name: str,
    file_path: str | Path | None,
) -> tuple[ProposedPRSourceDocument, ...]:
    proposal_dir = (
        Path(file_path).resolve().parent
        if file_path is not None
        else repo_root / PROPOSALS_ROOT / work_item_name
    )
    sources: list[ProposedPRSourceDocument] = []
    for filename in _EFFECT_SOURCE_FILES:
        source_path = proposal_dir / filename
        if not source_path.is_file():
            continue
        try:
            sources.append(
                ProposedPRSourceDocument(
                    path=str(source_path),
                    yaml_content=source_path.read_text(encoding="utf-8"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            sources.append(
                ProposedPRSourceDocument(path=str(source_path), read_error=str(exc))
            )
    return tuple(sources)


def _load_proposed_pr_validation_documents(
    repo_root: Path,
) -> tuple[ProposedPRSourceDocument, ...]:
    documents: list[ProposedPRSourceDocument] = []
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        try:
            documents.append(
                ProposedPRSourceDocument(
                    path=str(specification_path),
                    yaml_content=specification_path.read_text(encoding="utf-8"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            documents.append(
                ProposedPRSourceDocument(
                    path=str(specification_path), read_error=str(exc)
                )
            )
    return tuple(documents)


def _resolved_local_file_path(
    repo_root: Path, file_path: str | Path | None
) -> Path | None:
    if file_path is None:
        return None
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def load_proposed_pr_dependency_graph(
    repo_root: str | Path,
) -> dict[str, tuple[str, ...]]:
    """Load the dependency graph from repository documents through service policy."""
    return _build_service_pr_dependency_graph(
        _load_proposed_pr_validation_documents(_resolve_repo_root(repo_root))
    )


def _load_feature_catalog(repo_root: Path) -> list[_FeatureCatalogEntry]:
    try:
        codebase_state_report = build_codebase_state_report(repo_root=repo_root)
    except Exception:  # noqa: BLE001
        codebase_state_report = None

    catalog: list[_FeatureCatalogEntry] = []
    seen_feature_ids: set[str] = set()
    if codebase_state_report is not None and codebase_state_report.entities:
        for entity in codebase_state_report.entities:
            if entity.id in seen_feature_ids:
                continue
            seen_feature_ids.add(entity.id)
            source_path = entity.source.changelog_path or "current codebase state"
            catalog.append(
                _FeatureCatalogEntry(
                    feature_id=entity.id,
                    source_path=source_path,
                    entity_type=entity.type,
                )
            )

    # The checked-in codebase state is authoritative for repository-wide
    # entities, but a workflow may be validating a specification that exists
    # only in the current worktree. Include those local implementation
    # features as well instead of falling back to them only when the codebase
    # state is empty.
    implementation_paths = [
        specification_path
        for implementation_dir in _IMPLEMENTATION_SPECIFICATION_DIRS
        if (repo_root / implementation_dir).exists()
        for specification_path in (repo_root / implementation_dir).rglob(
            "implementation-specification.yaml"
        )
    ]
    for specification_path in sorted(implementation_paths):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        features = _coerce_sequence(
            raw_spec.get("features"),
            path=f"{specification_path}#features",
            issues=[],
            issue_code="invalid_feature_registry",
            issue_message="",
        )
        for feature in features:
            if not isinstance(feature, Mapping):
                continue
            feature_id = _optional_string(feature.get("id"))
            if feature_id is None or feature_id in seen_feature_ids:
                continue
            seen_feature_ids.add(feature_id)
            catalog.append(
                _FeatureCatalogEntry(
                    feature_id=feature_id,
                    source_path=str(specification_path.relative_to(repo_root)),
                    entity_type="feature",
                )
            )

    return catalog


def _load_existing_pr_ids(
    repo_root: Path,
    *,
    excluded_file_path: str | Path | None = None,
) -> list[str]:
    pr_ids: list[str] = []
    seen_ids: set[str] = set()
    excluded_path = (
        (
            (repo_root / Path(excluded_file_path))
            if not Path(excluded_file_path).expanduser().is_absolute()
            else Path(excluded_file_path).expanduser()
        ).resolve()
        if excluded_file_path is not None
        else None
    )
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        if excluded_path is not None and specification_path.resolve() == excluded_path:
            continue
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        proposed_pr_id = _optional_string(raw_spec.get("id"))
        nested_prs = raw_spec.get("proposed_prs")
        candidate_ids = (
            [
                _optional_string(item.get("id"))
                for item in nested_prs
                if isinstance(item, Mapping)
            ]
            if isinstance(nested_prs, Sequence)
            and not isinstance(nested_prs, (str, bytes))
            else [proposed_pr_id]
        )
        for candidate_id in candidate_ids:
            if candidate_id is None:
                continue
            normalized_proposed_pr_id = _normalize_identifier(candidate_id)
            if normalized_proposed_pr_id in seen_ids:
                continue
            seen_ids.add(normalized_proposed_pr_id)
            pr_ids.append(candidate_id)

    return pr_ids


def _load_proposed_pr_documents(repo_root: Path) -> list[_ProposedPRDocument]:
    documents: list[_ProposedPRDocument] = []
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        pr_number = _parse_proposed_pr_number(specification_path)

        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        nested_prs = raw_spec.get("proposed_prs")
        records = (
            [item for item in nested_prs if isinstance(item, Mapping)]
            if isinstance(nested_prs, Sequence)
            and not isinstance(nested_prs, (str, bytes))
            else [raw_spec]
        )
        top_level_feature_ids = raw_spec.get("feature_ids")
        for record in records:
            proposed_pr_id = _optional_string(record.get("id"))
            feature_values = record.get("feature_ids", top_level_feature_ids)
            feature_ids = tuple(
                feature_id
                for feature_id in (
                    _optional_string(raw_feature_id)
                    for raw_feature_id in _coerce_sequence(
                        feature_values,
                        path=f"{specification_path}#feature_ids",
                        issues=[],
                        issue_code="invalid_feature_ids_section",
                        issue_message="",
                    )
                )
                if feature_id is not None
            )
            intent = _coerce_mapping(
                record.get("intent"),
                path=f"{specification_path}#intent",
                issues=[],
                issue_code="invalid_intent_section",
                issue_message="",
            )
            documents.append(
                _ProposedPRDocument(
                    pr_number=pr_number,
                    path=specification_path,
                    data=record,
                    proposed_pr_id=proposed_pr_id,
                    feature_ids=feature_ids,
                    intent_goal=_optional_string(intent.get("goal"))
                    if intent
                    else None,
                    intent_reasoning=(
                        _optional_string(intent.get("reasoning")) if intent else None
                    ),
                )
            )

    return documents


def _iter_proposed_pr_specification_paths(repo_root: Path) -> list[Path]:
    specification_paths = [
        specification_path
        for proposal_root in (repo_root / PROPOSALS_ROOT,)
        if proposal_root.exists()
        for specification_path in proposal_root.rglob("*.yaml")
        if specification_path.is_file()
        and (
            specification_path.name == "proposed-pr-specification.yaml"
            or specification_path.name.endswith("-proposed-pr-specification.yaml")
        )
    ]

    seen_paths: set[Path] = set()
    ordered_paths: list[Path] = []
    for specification_path in sorted(specification_paths):
        if specification_path in seen_paths:
            continue

        seen_paths.add(specification_path)
        ordered_paths.append(specification_path)

    return ordered_paths


def _parse_proposed_pr_number(specification_path: Path) -> int | None:
    parent_name = specification_path.parent.name
    if parent_name.startswith("PR-"):
        number_text = parent_name.removeprefix("PR-")
        if number_text.isdigit():
            return int(number_text)

    filename = specification_path.name
    if filename.startswith("PR-") and filename.endswith(
        "-proposed-pr-specification.yaml"
    ):
        number_text = filename.removeprefix("PR-").removesuffix(
            "-proposed-pr-specification.yaml"
        )
        if number_text.isdigit():
            return int(number_text)

    return None


def _score_proposed_pr_document(
    query: str,
    document: _ProposedPRDocument,
) -> ProposedPRSearchResult:
    field_texts = {
        "id": document.proposed_pr_id or "",
        "feature_ids": " ".join(document.feature_ids),
        "intent.goal": document.intent_goal or "",
        "intent.reasoning": document.intent_reasoning or "",
        "acceptance_criteria": _collect_detail_text(
            document.data.get("acceptance_criteria")
        ),
        "expected_tests": _collect_detail_text(document.data.get("expected_tests")),
        "required_test_cases": _collect_detail_text(
            document.data.get("required_test_cases")
        ),
        "expected_outcomes": _collect_detail_text(
            document.data.get("expected_outcomes")
        ),
        "non_goals": _collect_detail_text(document.data.get("non_goals")),
        "risks": _collect_detail_text(document.data.get("risks")),
    }

    matched_fields: list[str] = []
    best_score = 0.0
    for field_name, field_text in field_texts.items():
        score = _score_text(query, field_text)
        if score > best_score:
            best_score = score
        if score > 0.25:
            matched_fields.append(field_name)

    if document.proposed_pr_id and document.proposed_pr_id == query:
        best_score = 1.0
        if "id" not in matched_fields:
            matched_fields.append("id")

    return ProposedPRSearchResult(
        pr_number=document.pr_number,
        proposed_pr_id=document.proposed_pr_id,
        path=document.path,
        score=round(best_score, 4),
        matched_fields=tuple(dict.fromkeys(matched_fields)),
        feature_ids=document.feature_ids,
        intent_goal=document.intent_goal,
        intent_reasoning=document.intent_reasoning,
    )


def _collect_detail_text(raw_value: object | None) -> str:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        return ""

    collected: list[str] = []
    for item in raw_value:
        if not isinstance(item, Mapping):
            continue
        item_id = _optional_string(item.get("id"))
        description = _optional_string(item.get("description"))
        if item_id is not None:
            collected.append(item_id)
        if description is not None:
            collected.append(description)
    return " ".join(collected)


def _score_text(query: str, text: str) -> float:
    normalized_text = text.strip().lower()
    if normalized_text == "":
        return 0.0

    normalized_query = query.strip().lower()
    if normalized_query == "":
        return 0.0

    if normalized_query in normalized_text:
        return 1.0

    query_tokens = [token for token in _tokenize(normalized_query) if len(token) > 1]
    if query_tokens:
        overlap = sum(1 for token in query_tokens if token in normalized_text)
        token_score = overlap / len(query_tokens)
    else:
        token_score = 0.0

    return max(
        SequenceMatcher(None, normalized_query, normalized_text).ratio(), token_score
    )


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isalnum():
            current.append(character)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _normalize_identifier(value: str) -> str:
    return value.strip().casefold()


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("Top-level PR specification must be a mapping.")
    return cast(Mapping[str, Any], loaded)


def _coerce_sequence(
    raw_value: object,
    *,
    path: str,
    issues: list[PRSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(
        PRSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return ()


def _coerce_mapping(
    raw_value: object,
    *,
    path: str,
    issues: list[PRSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Mapping[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return cast(Mapping[str, Any], raw_value)

    issues.append(
        PRSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return None


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None

    value = str(raw_value).strip()
    return value or None


def _string_values(raw_value: object) -> list[str]:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        return []
    return [
        item.strip() for item in raw_value if isinstance(item, str) and item.strip()
    ]


def _resolve_output_path(
    repo_root: Path,
    work_item_name: str,
    output_path: str | Path | None,
) -> Path:
    if output_path is None:
        return pr_specification_default_output_path(work_item_name, repo_root)

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path
    return resolved_output_path


def _report_to_data(
    report: PRSpecificationValidationReport,
    *,
    file_path: str | Path | None = None,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "issues": [
            validation_error_to_data(issue, file_path=str(file_path))
            for issue in report.issues
        ],
        "proposed_pr_id": report.proposed_pr_id,
        "available_feature_ids": report.available_feature_ids,
        "known_pr_ids": report.known_pr_ids,
    }


def _proposed_pr_search_report_to_data(
    report: ProposedPRSearchReport,
) -> Mapping[str, Any]:
    return {
        "query": report.query,
        "results": [
            {
                "pr_number": result.pr_number,
                "proposed_pr_id": result.proposed_pr_id,
                "path": str(result.path),
                "score": result.score,
                "matched_fields": list(result.matched_fields),
                "feature_ids": list(result.feature_ids),
                "intent_goal": result.intent_goal,
                "intent_reasoning": result.intent_reasoning,
            }
            for result in report.results
        ],
    }
