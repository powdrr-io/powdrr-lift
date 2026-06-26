from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class BranchDiffEntry:
    status: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class RelatedSectionPreview:
    entities: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()
    proposed_prs: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    expected_tests: tuple[str, ...] = ()
    required_test_cases: tuple[str, ...] = ()
    expected_outcomes: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanDiffSourceSpanPreview:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class PlanDiffEntryPreview:
    id: str
    kind: str
    section: str
    description: str
    plan_value: str | None = None
    changelog_value: str | None = None
    source_paths: tuple[str, ...] = ()
    source_spans: tuple[PlanDiffSourceSpanPreview, ...] = ()


def create_change_log_template(
    branch_name: str | None = None,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch_name = branch_name or _current_branch(repo_root_path)
    output_path = _resolve_output_path(repo_root_path, output_path)
    default_branch_name = default_branch or _resolve_default_branch(
        repo_root_path,
        branch_name=resolved_branch_name,
    )
    diff_entries = _collect_branch_diff_entries(
        repo_root_path,
        default_branch_name,
        resolved_branch_name,
    )
    related_sections_by_entry = _collect_related_sections_by_entry(
        repo_root_path,
        resolved_branch_name,
        default_branch_name,
        diff_entries,
    )
    template = render_change_log_template(
        branch_name=resolved_branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
        related_sections_by_entry=related_sections_by_entry,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template, encoding="utf-8")
    return output_path


def create_change_log_template_from_plan_diff(
    *,
    branch_name: str,
    plan_diff_path: str | Path,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    output_path = _resolve_output_path(repo_root_path, output_path)
    default_branch_name = default_branch or _resolve_default_branch(repo_root_path)
    diff_entries = _collect_branch_diff_entries(
        repo_root_path,
        default_branch_name,
        branch_name,
    )
    plan_diff_report = _load_plan_diff_report(plan_diff_path, repo_root_path)
    related_sections_by_entry = _collect_related_sections_by_entry(
        repo_root_path,
        branch_name,
        default_branch_name,
        diff_entries,
    )
    related_sections_by_entry = _merge_related_sections_by_plan_diff(
        diff_entries,
        related_sections_by_entry,
        plan_diff_report,
    )
    template = render_change_log_template(
        branch_name=branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
        related_sections_by_entry=related_sections_by_entry,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template, encoding="utf-8")
    return output_path


def render_change_log_template(
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[BranchDiffEntry],
    related_sections_by_entry: Sequence[RelatedSectionPreview] | None = None,
) -> str:
    header = _render_header(branch_name, default_branch_name, diff_entries)
    body = _render_template_body(diff_entries, related_sections_by_entry)
    return f"{header}\n{body}"


def _render_template_body(
    diff_entries: Sequence[BranchDiffEntry],
    related_sections_by_entry: Sequence[RelatedSectionPreview] | None = None,
) -> str:
    if related_sections_by_entry is None:
        related_sections_by_entry = [RelatedSectionPreview()] * len(diff_entries)

    structured_file_paths: list[str] = []
    file_entries: list[tuple[BranchDiffEntry, RelatedSectionPreview]] = []
    seen_structured_file_paths: set[str] = set()
    for diff_entry, related_section in zip(
        diff_entries, related_sections_by_entry, strict=False
    ):
        if _is_structured_document_path(diff_entry.path):
            if diff_entry.path in seen_structured_file_paths:
                continue

            seen_structured_file_paths.add(diff_entry.path)
            structured_file_paths.append(diff_entry.path)
            continue

        file_entries.append((diff_entry, related_section))

    lines = [
        "schema: https://powdrr.io/schema/changelog-v2",
        "# Use the PR number here.",
        "change_id: null",
        "# Brief title for the pull request.",
        "title: null",
        "intent:",
        "  # Describe the underlying problem.",
        "  problem: null",
        "  # Describe the intended outcome.",
        "  goal: null",
        "# List each decision or ADR used by this change.",
        "decisions:",
        "  -",
        "    # Decision identifier, such as an ADR number.",
        "    id: null",
        "    # Short summary of the decision.",
        "    summary: null",
        "# Structured YAML files under `docs/specs/` go in `structured_files` as",
        "# paths only.",
        "# Structured YAML files must include a `schema` key of",
        "# `https://powdrr.io/schemas/specification-v1`.",
        "# Use `files` for code, Markdown, and other non-structured file entries.",
        "# Related references are optional. Include only the populated lists, and",
        "# remove the whole related block if it would otherwise be empty.",
        "# When a file change changes an entity, put the entity id in",
        "# `related.entities` and explain the behavior change in the summary or",
        "# rationale.",
        "# When a file change changes a relationship, put the relationship's",
        "# entity ids in `related.entities` or the explicit relationship entry",
        "# in `entity_relationships`, whichever most directly describes the",
        "# change.",
        "# If this change was based on earlier proposed PRs, record those PR ids",
        "# under `related.proposed_prs`.",
        "# Use `features` to record feature ids whose state changed in this PR.",
        "# Add a feature entry when the change creates, updates, or removes a",
        "# feature. Leave `features: []` only when the PR does not touch feature",
        "# state.",
        "# Use the top-level `proposed_prs` section to track proposed PR status",
        "# with `id` and `state` values.",
        "# Use `state: completed` for basis PRs that are already merged and",
        "# `state: in_progress` for basis PRs that are still underway.",
    ]

    if structured_file_paths:
        lines.append("structured_files:")
        lines.extend(f"  - {path}" for path in structured_file_paths)
    else:
        lines.append("structured_files: []")

    if file_entries:
        lines.append("files:")
        for diff_entry, related_section in file_entries:
            lines.extend(
                [
                    "  -",
                    "    # File path and type for this hunk.",
                    f"    path: {diff_entry.path}",
                    f"    type: {_normalize_change_type(diff_entry.status)}",
                    "    span:",
                    "      # First changed line in this file entry.",
                    f"      start_line: {diff_entry.start_line}",
                    "      # Last changed line in this file entry.",
                    f"      end_line: {diff_entry.end_line}",
                    "    # Short summary for this file entry.",
                    "    summary: null",
                    "    # Why this file entry changed.",
                    "    rationale: null",
                    "    # Review the related list before filling out the rest of",
                    "    # the change. Keep entries that are still relevant, add",
                    "    # missing ones, remove stale ones, and use the final",
                    "    # list to decide what belongs in entities, entity",
                    "    # relationships, invariants, guidance, and the test case",
                    "    # evidence fields.",
                    *(
                        [
                            "    # Related ids for this file entry.",
                            "    # Remove any empty related lists rather than leaving",
                            "    # them in place.",
                            "    related:",
                        ]
                        if _has_related_values(related_section)
                        else []
                    ),
                    *(
                        _render_related_section(related_section)
                        if _has_related_values(related_section)
                        else []
                    ),
                ]
            )
    else:
        lines.append("files: []")

    lines.extend(
        [
            "entities: []",
            "# Use `entities` for every concrete entity lifecycle change.",
            "# Each entry should explain the entity id, type, and action.",
            "entity_relationships: []",
            "# Use `entity_relationships` for every relationship that changes.",
            "# Each entry should name the source, target, and relationship.",
            "invariants: []",
            "# Use `invariants` for behavior guarantees that must remain true.",
            "guidance: []",
            "# Use `guidance` for operator or contributor instructions that need",
            "# to be updated with the change.",
            "# Track feature and proposed PR state updates when relevant.",
            "# Use `in_progress` when work is underway and `completed` when",
            "# it is done.",
            "features: []",
            "proposed_prs: []",
        ]
    )

    lines.extend(
        [
            "",
        ]
    )

    return "\n".join(lines)


def _collect_related_sections_by_entry(
    repo_root: Path,
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[BranchDiffEntry],
) -> list[RelatedSectionPreview]:
    from powdrr_lift.core.code_index import CodeIndexStore

    store = CodeIndexStore(repo_root)
    source_index = store.refresh(branch_name, default_branch_name)

    related_sections_by_entry: list[RelatedSectionPreview] = []
    for diff_entry in diff_entries:
        related_entities = _dedupe_string_values(
            [
                *_collect_related_entities_from_documents(
                    source_index.documents,
                    path=diff_entry.path,
                    start_line=diff_entry.start_line,
                    end_line=diff_entry.end_line,
                ),
                *_collect_related_entities_for_span(
                    store,
                    branch_names=(default_branch_name, branch_name),
                    path=diff_entry.path,
                    start_line=diff_entry.start_line,
                    end_line=diff_entry.end_line,
                ),
            ]
        )
        related_sections_by_entry.append(
            RelatedSectionPreview(
                entities=tuple(related_entities),
                invariants=_collect_related_lifecycle_ids(
                    source_index.documents,
                    item_kind="invariant",
                    current_file_path=diff_entry.path,
                    related_entity_names=related_entities,
                ),
                guidance=_collect_related_lifecycle_ids(
                    source_index.documents,
                    item_kind="guidance",
                    current_file_path=diff_entry.path,
                    related_entity_names=related_entities,
                ),
                required_test_cases=_collect_related_test_case_ids(
                    source_index.documents,
                    current_file_path=diff_entry.path,
                ),
            )
        )

    return related_sections_by_entry


def _load_plan_diff_report(
    plan_diff_path: str | Path,
    repo_root: Path,
) -> Mapping[str, Any]:
    resolved_path = Path(plan_diff_path)
    if not resolved_path.is_absolute():
        resolved_path = repo_root / resolved_path

    loaded_yaml = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(loaded_yaml, Mapping):
        raise ValueError("Plan diff YAML must decode to a mapping.")

    return loaded_yaml


def _merge_related_sections_by_plan_diff(
    diff_entries: Sequence[BranchDiffEntry],
    related_sections_by_entry: Sequence[RelatedSectionPreview],
    plan_diff_report: Mapping[str, Any],
) -> list[RelatedSectionPreview]:
    related_by_key: dict[tuple[str, int, int], RelatedSectionPreview] = {}
    for diff_entry, related_section in zip(
        diff_entries, related_sections_by_entry, strict=False
    ):
        related_by_key[
            (diff_entry.path, diff_entry.start_line, diff_entry.end_line)
        ] = related_section

    for difference in _parse_plan_diff_differences(plan_diff_report):
        if difference.section not in {
            "entities",
            "invariants",
            "guidance",
            "acceptance_criteria",
            "expected_tests",
            "required_test_cases",
            "expected_outcomes",
            "non_goals",
            "risks",
            "proposed_prs",
        }:
            continue

        for source_span in difference.source_spans:
            key = (source_span.path, source_span.start_line, source_span.end_line)
            existing_section = related_by_key.get(key)
            if existing_section is None:
                continue

            related_by_key[key] = _merge_related_section_preview(
                existing_section,
                difference.section,
                difference.plan_value,
            )

    return [
        related_by_key.get(
            (diff_entry.path, diff_entry.start_line, diff_entry.end_line),
            related_section,
        )
        for diff_entry, related_section in zip(
            diff_entries, related_sections_by_entry, strict=False
        )
    ]


def _parse_plan_diff_differences(
    plan_diff_report: Mapping[str, Any],
) -> list[PlanDiffEntryPreview]:
    differences: list[PlanDiffEntryPreview] = []
    for raw_difference in _ensure_sequence(plan_diff_report.get("differences")):
        difference = _parse_plan_diff_difference(raw_difference)
        if difference is not None:
            differences.append(difference)

    return differences


def _parse_plan_diff_difference(raw_difference: object) -> PlanDiffEntryPreview | None:
    if not isinstance(raw_difference, Mapping):
        return None

    source_spans = []
    for raw_span in _ensure_sequence(raw_difference.get("source_spans")):
        if not isinstance(raw_span, Mapping):
            continue

        path = _optional_string(raw_span.get("path"))
        start_line = _optional_int(raw_span.get("start_line"))
        end_line = _optional_int(raw_span.get("end_line"))
        if path is None or start_line is None or end_line is None:
            continue

        source_spans.append(
            PlanDiffSourceSpanPreview(
                path=path,
                start_line=start_line,
                end_line=end_line,
            )
        )

    return PlanDiffEntryPreview(
        id=_optional_string(raw_difference.get("id")) or "",
        kind=_optional_string(raw_difference.get("kind")) or "",
        section=_optional_string(raw_difference.get("section")) or "",
        description=_optional_string(raw_difference.get("description")) or "",
        plan_value=_optional_string(raw_difference.get("plan_value")),
        changelog_value=_optional_string(raw_difference.get("changelog_value")),
        source_paths=_dedupe_string_values(
            str(raw_value)
            for raw_value in _ensure_sequence(raw_difference.get("source_paths"))
        ),
        source_spans=tuple(source_spans),
    )


def _merge_related_section_preview(
    related_section: RelatedSectionPreview,
    section_name: str,
    value: str | None,
) -> RelatedSectionPreview:
    if value is None:
        return related_section

    if section_name == "entities":
        return RelatedSectionPreview(
            entities=_dedupe_string_values([*related_section.entities, value]),
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "invariants":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=_dedupe_string_values([*related_section.invariants, value]),
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "guidance":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=_dedupe_string_values([*related_section.guidance, value]),
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "proposed_prs":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=_dedupe_string_values([*related_section.proposed_prs, value]),
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "acceptance_criteria":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=_dedupe_string_values(
                [*related_section.acceptance_criteria, value]
            ),
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "expected_tests":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=_dedupe_string_values(
                [*related_section.expected_tests, value]
            ),
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "required_test_cases":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=_dedupe_string_values(
                [*related_section.required_test_cases, value]
            ),
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "expected_outcomes":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=_dedupe_string_values(
                [*related_section.expected_outcomes, value]
            ),
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "non_goals":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=_dedupe_string_values([*related_section.non_goals, value]),
            risks=related_section.risks,
        )

    if section_name == "risks":
        return RelatedSectionPreview(
            entities=related_section.entities,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=_dedupe_string_values([*related_section.risks, value]),
        )

    return related_section


def _ensure_sequence(raw_value: object | None) -> Sequence[object]:
    if raw_value is None:
        return ()

    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value

    return ()


def _optional_string(raw_value: object | None) -> str | None:
    if raw_value is None:
        return None

    normalized_value = str(raw_value).strip()
    return normalized_value or None


def _optional_int(raw_value: object | None) -> int | None:
    if raw_value is None:
        return None

    if not isinstance(raw_value, int | str):
        return None

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _has_related_values(related_section: RelatedSectionPreview) -> bool:
    return any(
        (
            related_section.entities,
            related_section.invariants,
            related_section.guidance,
            related_section.proposed_prs,
            related_section.acceptance_criteria,
            related_section.expected_tests,
            related_section.required_test_cases,
            related_section.expected_outcomes,
            related_section.non_goals,
            related_section.risks,
        )
    )


def _render_related_section(related_section: RelatedSectionPreview) -> list[str]:
    lines: list[str] = []
    for key, values in (
        ("entities", related_section.entities),
        ("invariants", related_section.invariants),
        ("guidance", related_section.guidance),
        ("proposed_prs", related_section.proposed_prs),
        ("acceptance_criteria", related_section.acceptance_criteria),
        ("expected_tests", related_section.expected_tests),
        ("required_test_cases", related_section.required_test_cases),
        ("expected_outcomes", related_section.expected_outcomes),
        ("non_goals", related_section.non_goals),
        ("risks", related_section.risks),
    ):
        if not values:
            continue

        lines.append(f"      {key}:")
        lines.extend(f"        - {value}" for value in values)

    return lines


def _collect_related_entities_for_span(
    store: Any,
    *,
    branch_names: Sequence[str],
    path: str,
    start_line: int,
    end_line: int,
) -> list[str]:
    related_entities: list[str] = []
    seen_entities: set[str] = set()
    lookup_lines = store.lookup_lines
    for branch_name in branch_names:
        for _, provenance in lookup_lines(branch_name, path, start_line, end_line):
            if provenance is None:
                continue

            for entity_name in provenance.affects:
                if entity_name in seen_entities:
                    continue

                seen_entities.add(entity_name)
                related_entities.append(entity_name)

    return related_entities


def _collect_related_entities_from_documents(
    documents: Sequence[object],
    *,
    path: str,
    start_line: int,
    end_line: int,
) -> list[str]:
    related_entities: list[str] = []
    seen_entities: set[str] = set()
    for document in documents:
        changelog = getattr(document, "changelog", None)
        if changelog is None:
            continue

        file_changes = getattr(changelog, "file_changes", ())
        for file_change in file_changes:
            if getattr(file_change, "path", None) != path:
                continue

            file_span = getattr(file_change, "span", None)
            file_start = getattr(file_span, "start_line", None)
            file_end = getattr(file_span, "end_line", None)
            if (
                file_start is None
                or file_end is None
                or file_start > end_line
                or file_end < start_line
            ):
                continue

            related = getattr(file_change, "related", None)
            candidate_entities = (
                getattr(related, "entities", ())
                if related is not None
                else getattr(file_change, "entities", ())
            )
            for entity_name in candidate_entities:
                if entity_name in seen_entities:
                    continue

                seen_entities.add(entity_name)
                related_entities.append(entity_name)

    return related_entities


def _collect_related_lifecycle_ids(
    documents: Sequence[object],
    *,
    item_kind: str,
    current_file_path: str,
    related_entity_names: Sequence[str],
) -> tuple[str, ...]:
    related_ids: list[str] = []
    seen_ids: set[str] = set()
    related_entity_set = set(related_entity_names)
    related_file_set = {current_file_path}

    for document in documents:
        changelog = getattr(document, "changelog", None)
        if changelog is None:
            continue

        if item_kind == "invariant":
            items = getattr(changelog, "invariant_changes", ())
        else:
            items = getattr(changelog, "guidance_changes", ())

        for item in items:
            item_id = getattr(item, "id", None)
            item_id_value = str(item_id).strip() if item_id is not None else ""
            related = getattr(item, "related", None)
            if item_id_value == "" or item_id_value in seen_ids or related is None:
                continue

            if _related_section_matches(
                related,
                related_file_set=related_file_set,
                related_entity_set=related_entity_set,
            ):
                seen_ids.add(item_id_value)
                related_ids.append(item_id_value)

    return tuple(related_ids)


def _collect_related_test_case_ids(
    documents: Sequence[object],
    *,
    current_file_path: str,
) -> tuple[str, ...]:
    related_ids: list[str] = []
    seen_ids: set[str] = set()

    for document in documents:
        changelog = getattr(document, "changelog", None)
        if changelog is None:
            continue

        for file_change in getattr(changelog, "file_changes", ()):
            file_id = getattr(file_change, "path", None)
            if file_id != current_file_path:
                continue

            file_span = getattr(file_change, "span", None)
            file_start = getattr(file_span, "start_line", None)
            file_end = getattr(file_span, "end_line", None)
            if file_start is None or file_end is None:
                continue
            if file_start > file_end:
                continue

            related = getattr(file_change, "related", None)
            if related is None:
                continue

            for related_id in getattr(related, "required_test_cases", ()):
                if related_id in seen_ids:
                    continue

                seen_ids.add(str(related_id))
                related_ids.append(str(related_id))

    return tuple(related_ids)


def _related_section_matches(
    related: object,
    *,
    related_file_set: set[str],
    related_entity_set: set[str],
) -> bool:
    related_files = set(getattr(related, "files", ()))
    related_entities = set(getattr(related, "entities", ()))
    return bool(
        related_files.intersection(related_file_set)
        or related_entities.intersection(related_entity_set)
    )


def _dedupe_strings(values: Iterable[str]) -> tuple[str, ...]:
    return _dedupe_string_values(values)


def _dedupe_string_values(values: Iterable[str]) -> tuple[str, ...]:
    seen_values: set[str] = set()
    deduped_values: list[str] = []
    for raw_value in values:
        value = str(raw_value).strip()
        if not value or value in seen_values:
            continue

        seen_values.add(value)
        deduped_values.append(value)

    return tuple(deduped_values)


def _render_header(
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[BranchDiffEntry],
) -> str:
    diff_lines = (
        "\n".join(f"#   {entry.status} {entry.path}" for entry in diff_entries)
        if diff_entries
        else "#   (no changed files detected)"
    )
    return (
        f"# ChangeLog template generated from branch `{branch_name}`.\n"
        f"# Compared against default branch `{default_branch_name}`.\n"
        "#\n"
        "# Instructions for the coding agent:\n"
        "# - Keep this file valid YAML.\n"
        "# - Replace each `null` with concrete values when they are known.\n"
        "# - Leave a list empty only when the section truly does not apply.\n"
        "# - Use the prefilled `files` entries as the starting point for the file\n"
        "#   that differ from the default branch.\n"
        "# - Add or remove `files` items as needed so every meaningful file change\n"
        "#   is represented exactly once.\n"
        "# - Keep the changelog schema unless the schema changes.\n"
        "# - Put `path`, `type`, `span`, `summary`, `rationale`, and optional\n"
        "#   `related` on each file entry.\n"
        "# - Review each prefilled `related` section before you fill in the rest\n"
        "#   of the changelog. Keep entries that are still relevant, add\n"
        "#   missing ones, and remove stale ones.\n"
        "# - Use the final related lists to help fill out `entities`,\n"
        "#   `entity_relationships`, `invariants`, and `guidance`.\n"
        "# - Related references are optional. Remove empty related lists instead\n"
        "#   of leaving them in place, and remove `related` entirely if nothing\n"
        "#   needs to point at anything else.\n"
        "# - Put file-related entity ids under `related.entities`.\n"
        "# - Use `related.entities`, `related.invariants`, and `related.guidance`\n"
        "#   when this file change needs to point at supporting code areas or at\n"
        "#   the invariant or guidance entries it drives.\n"
        "# - Use `related.acceptance_criteria`, `related.expected_tests`,\n"
        "#   `related.required_test_cases`, `related.expected_outcomes`,\n"
        "#   `related.non_goals`, and `related.risks`\n"
        "#   when the change relates to a proposed-PR detail item. These\n"
        "#   references are optional.\n"
        "# - In any rationale, put current ids you want to reference in quotes;\n"
        "#   the validator accepts quoted ids from the current codebase state,\n"
        "#   existing changelogs, and proposal specs.\n"
        "# - Put entity lifecycle changes in `entities` with `action: added`,\n"
        "#   `action: deleted`, or `action: modified`.\n"
        "# - Put relationship changes in `entity_relationships`.\n"
        "# - Put invariant changes in `invariants` and guidance changes in\n"
        "#   `guidance`.\n"
        "# - Use `related` on entity, relationship, invariant, and guidance\n"
        "#   entries to point at the relevant entity, invariant, or guidance\n"
        "#   ids.\n"
        "#\n"
        "# Diff summary:\n"
        f"{diff_lines}"
    )


def _collect_branch_diff_entries(
    repo_root: Path,
    default_branch_name: str,
    branch_name: str,
) -> list[BranchDiffEntry]:
    diff_output = _git_output(
        repo_root,
        "diff",
        "--name-status",
        f"{default_branch_name}...{branch_name}",
    )
    entries: list[BranchDiffEntry] = []
    for line in diff_output.splitlines():
        if not line:
            continue

        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            path = parts[2]

        if status[0] in {"R", "C"} and status[1:] == "100":
            continue

        if _is_changelog_artifact_path(path):
            continue

        spans = _collect_file_spans(
            repo_root,
            default_branch_name,
            branch_name,
            path,
        )
        if not spans:
            continue

        for start_line, end_line in spans:
            entries.append(
                BranchDiffEntry(
                    status=status,
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                )
            )

    return entries


def _group_branch_diff_entries_by_path(
    diff_entries: Sequence[BranchDiffEntry],
) -> list[list[BranchDiffEntry]]:
    grouped_entries: list[list[BranchDiffEntry]] = []
    grouped_by_path: dict[str, list[BranchDiffEntry]] = {}
    for entry in diff_entries:
        grouped_by_path.setdefault(entry.path, []).append(entry)

    seen_paths: set[str] = set()
    for entry in diff_entries:
        if entry.path in seen_paths:
            continue

        grouped_entries.append(grouped_by_path[entry.path])
        seen_paths.add(entry.path)

    return grouped_entries


def _collect_file_spans(
    repo_root: Path,
    default_branch_name: str,
    branch_name: str,
    path: str,
) -> list[tuple[int, int]]:
    diff_output = _git_output(
        repo_root,
        "diff",
        "--unified=0",
        "--no-color",
        f"{default_branch_name}...{branch_name}",
        "--",
        path,
    )
    span_ranges: list[tuple[int, int]] = []
    hunk_header_pattern = re.compile(
        r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
        r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
    )

    for line in diff_output.splitlines():
        match = hunk_header_pattern.match(line)
        if not match:
            continue

        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_start = int(match.group("new_start"))
        new_count = int(match.group("new_count") or "1")

        if new_count > 0:
            span_ranges.append((new_start, new_start + new_count - 1))
        elif old_count > 0:
            span_ranges.append((old_start, old_start + old_count - 1))

    if not span_ranges:
        return []

    return span_ranges


def _resolve_repo_root(repo_root: str | Path | None) -> Path:
    if repo_root is not None:
        return Path(repo_root).resolve()

    root_output = _git_output(Path.cwd(), "rev-parse", "--show-toplevel").strip()
    return Path(root_output)


def _resolve_output_path(repo_root: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return repo_root / "change-log.template.yaml"

    resolved_output = Path(output_path)
    if not resolved_output.is_absolute():
        resolved_output = repo_root / resolved_output

    return resolved_output


def _resolve_default_branch(
    repo_root: Path,
    branch_name: str | None = None,
) -> str:
    try:
        head_reference = _git_output(
            repo_root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ).strip()
        if head_reference:
            return head_reference.removeprefix("origin/")
    except subprocess.CalledProcessError:
        pass

    try:
        remote_output = _git_output(repo_root, "remote", "show", "origin")
    except subprocess.CalledProcessError:
        resolved_branch = branch_name or _current_branch(repo_root)
        local_branch = _resolve_local_base_branch(repo_root, resolved_branch)
        return local_branch or resolved_branch

    for line in remote_output.splitlines():
        if line.startswith("  HEAD branch: "):
            return line.partition(": ")[2].strip()

    resolved_branch = branch_name or _current_branch(repo_root)
    local_branch = _resolve_local_base_branch(repo_root, resolved_branch)
    return local_branch or resolved_branch


def _resolve_local_base_branch(
    repo_root: Path,
    branch_name: str,
) -> str | None:
    try:
        local_branches = [
            line.strip()
            for line in _git_output(
                repo_root,
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/heads",
            ).splitlines()
            if line.strip()
        ]
    except subprocess.CalledProcessError:
        local_branches = []

    for candidate in local_branches:
        if not candidate or candidate == branch_name:
            continue
        if not _is_conventional_base_branch_name(candidate):
            continue

        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "merge-base",
                    "--is-ancestor",
                    candidate,
                    branch_name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            continue

        return candidate

    for candidate in local_branches:
        if not candidate or candidate == branch_name:
            continue

        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "merge-base",
                    "--is-ancestor",
                    candidate,
                    branch_name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            continue

        return candidate

    return None


def _is_conventional_base_branch_name(branch_name: str) -> bool:
    return branch_name in {"main", "master", "trunk", "develop"}


def _current_branch(repo_root: Path) -> str:
    return _git_output(repo_root, "branch", "--show-current").strip()


def _git_output(repo_root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout


def _is_changelog_artifact_path(path: str) -> bool:
    return path.startswith("docs/changelogs/PR-") and path.endswith("-changelog.yaml")


def _is_structured_document_path(path: str) -> bool:
    normalized_path = path.strip().replace("\\", "/")
    return normalized_path.startswith("docs/specs/") and normalized_path.endswith(
        ".yaml"
    )


def _normalize_change_type(status: str) -> str:
    if status.startswith("A"):
        return "added"
    if status.startswith("D"):
        return "deleted"
    if status.startswith("R"):
        return "renamed"
    if status.startswith("C"):
        return "copied"
    return "modified"


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser(
        description="Generate a ChangeLog template from a git branch diff.",
    )
    argument_parser.add_argument(
        "branch",
        nargs="?",
        help="Git branch to inspect. Defaults to the current branch.",
    )
    argument_parser.add_argument(
        "-o",
        "--output",
        help=(
            "Template file to write. Defaults to change-log.template.yaml in the repo "
            "root."
        ),
    )
    parsed_arguments = argument_parser.parse_args(argv)

    branch_name = parsed_arguments.branch or _current_branch(_resolve_repo_root(None))
    output_path = create_change_log_template(
        branch_name=branch_name,
        output_path=parsed_arguments.output,
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
