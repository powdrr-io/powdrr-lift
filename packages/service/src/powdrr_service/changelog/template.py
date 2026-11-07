"""Private policy for rendering changelog templates from pinned diff evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class RelatedSectionPreview:
    entities: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
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


def _value(source: object, key: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _integer_value(source: object, key: str) -> int:
    value = _value(source, key, 1)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return 1


def render_change_log_template(
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[object],
    related_sections_by_entry: Sequence[RelatedSectionPreview] | None = None,
) -> str:
    header = _render_header(branch_name, default_branch_name, diff_entries)
    body = _render_template_body(diff_entries, related_sections_by_entry)
    return f"{header}\n{body}"


def build_change_log_template(
    *,
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[Mapping[str, Any]],
    related_sections_by_entry: Sequence[Mapping[str, Any]],
    plan_diff_report: Mapping[str, Any] | None = None,
    existing_content: str | None = None,
) -> str:
    """Render and enrich a changelog template from agent-collected evidence."""
    related_sections = [
        _related_section_from_mapping(section) for section in related_sections_by_entry
    ]
    if len(diff_entries) != len(related_sections):
        raise ValueError("diff entries and related sections must have matching lengths")
    rendered = render_change_log_template(
        branch_name=branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
        related_sections_by_entry=related_sections,
    )
    if plan_diff_report is not None:
        related_sections = merge_related_sections_by_plan_diff(
            diff_entries, related_sections, plan_diff_report
        )
        rendered = render_change_log_template(
            branch_name=branch_name,
            default_branch_name=default_branch_name,
            diff_entries=diff_entries,
            related_sections_by_entry=related_sections,
        )
    return merge_existing_template_content(rendered, existing_content)


def _related_section_from_mapping(value: Mapping[str, Any]) -> RelatedSectionPreview:
    field_names = (
        "entities",
        "decisions",
        "invariants",
        "guidance",
        "proposed_prs",
        "acceptance_criteria",
        "expected_tests",
        "required_test_cases",
        "expected_outcomes",
        "non_goals",
        "risks",
    )
    fields: dict[str, tuple[str, ...]] = {}
    for field_name in field_names:
        raw_values = value.get(field_name, [])
        if not isinstance(raw_values, list) or not all(
            isinstance(item, str) for item in raw_values
        ):
            raise ValueError(
                f"related section {field_name} must be an array of strings"
            )
        fields[field_name] = tuple(raw_values)
    return RelatedSectionPreview(**fields)


def merge_existing_template_content(
    generated_content: str,
    existing_content: str | None,
) -> str:
    """Preserve valid user-entered values when regenerating a template."""
    if existing_content is None:
        return generated_content
    try:
        generated_data = yaml.safe_load(generated_content)
        existing_data = yaml.safe_load(existing_content)
    except yaml.YAMLError:
        return generated_content
    if not isinstance(generated_data, Mapping) or not isinstance(
        existing_data, Mapping
    ):
        return generated_content
    merged_data = _merge_template_value(generated_data, existing_data)
    instruction_prefix = _instruction_prefix(generated_content)
    rendered_data = yaml.safe_dump(merged_data, sort_keys=False)
    if instruction_prefix:
        return f"{instruction_prefix}\n{rendered_data}"
    return rendered_data


def _instruction_prefix(generated_content: str) -> str:
    lines: list[str] = []
    for line in generated_content.splitlines():
        if line.strip() == "" or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        break
    return "\n".join(lines).rstrip()


def _merge_template_value(template: Any, existing: Any) -> Any:
    if isinstance(template, Mapping) and isinstance(existing, Mapping):
        merged: dict[Any, Any] = {}
        for key, template_value in template.items():
            merged[key] = (
                _merge_template_value(template_value, existing[key])
                if key in existing
                else template_value
            )
        for key, existing_value in existing.items():
            if key not in merged:
                merged[key] = existing_value
        return merged
    if isinstance(template, list) and isinstance(existing, list):
        if not template:
            return existing
        prototype = template[0]
        if isinstance(prototype, Mapping):
            return [
                _merge_template_value(prototype, item)
                if isinstance(item, Mapping)
                else item
                for item in existing
            ]
        return existing
    if isinstance(template, Sequence) and not isinstance(
        template, (str, bytes, bytearray)
    ):
        return existing
    return existing


def _render_template_body(
    diff_entries: Sequence[object],
    related_sections_by_entry: Sequence[RelatedSectionPreview] | None = None,
) -> str:
    if related_sections_by_entry is None:
        related_sections_by_entry = [RelatedSectionPreview()] * len(diff_entries)

    structured_file_paths: list[str] = []
    file_entries: list[tuple[object, RelatedSectionPreview]] = []
    seen_structured_file_paths: set[str] = set()
    for diff_entry, related_section in zip(
        diff_entries, related_sections_by_entry, strict=False
    ):
        path = str(_value(diff_entry, "path", ""))
        if is_structured_document_path(path):
            if path in seen_structured_file_paths:
                continue

            seen_structured_file_paths.add(path)
            structured_file_paths.append(path)
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
        "# Put ALL CREATIVE AND DESIGN DECISIONS MADE BY THE HUMAN OPERATOR",
        "# in `human-decisions`.",
        "# List each decision or ADR used by this change.",
        "human-decisions:",
        "  -",
        "    # Decision identifier, such as an ADR number.",
        "    id: null",
        "    # Short summary of the decision.",
        "    summary: null",
        "# Structured YAML files under `docs/proposals/` or `docs/current/` go in",
        "# `structured_files` as",
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
        "# When a file change depends on a human decision, put that decision id",
        "# in `related.decisions` and make sure any input decisions are",
        "# explicitly referenced in that block.",
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
        "# Use `verification.required_test_cases` to track acceptance-linked tests",
        "# that this PR adds or updates. Record their exact pytest selectors and",
        "# mark them passed only when the candidate run passed.",
        "# Record repository check commands and their outcomes in",
        "# `verification.validation_runs`.",
    ]

    if structured_file_paths:
        lines.append("structured_files:")
        lines.extend(f"  - {path}" for path in structured_file_paths)
    else:
        lines.append("structured_files: []")

    if file_entries:
        lines.append("files:")
        for diff_entry, related_section in file_entries:
            status = str(_value(diff_entry, "status", "M"))
            lines.extend(
                [
                    "  -",
                    "    # File path and type for this hunk.",
                    f"    path: {_value(diff_entry, 'path', '')}",
                    f"    type: {_normalize_change_type(status)}",
                    "    span:",
                    "      # First changed line in this file entry.",
                    f"      start_line: {_value(diff_entry, 'start_line', 1)}",
                    "      # Last changed line in this file entry.",
                    f"      end_line: {_value(diff_entry, 'end_line', 1)}",
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
            "verification:",
            "  required_test_cases: []",
            "  validation_runs: []",
        ]
    )

    lines.extend(
        [
            "",
        ]
    )

    return "\n".join(lines)


def merge_related_sections_by_plan_diff(
    diff_entries: Sequence[object],
    related_sections_by_entry: Sequence[RelatedSectionPreview],
    plan_diff_report: Mapping[str, Any],
) -> list[RelatedSectionPreview]:
    related_by_key: dict[tuple[str, int, int], RelatedSectionPreview] = {}
    for diff_entry, related_section in zip(
        diff_entries, related_sections_by_entry, strict=False
    ):
        related_by_key[
            (
                str(_value(diff_entry, "path", "")),
                _integer_value(diff_entry, "start_line"),
                _integer_value(diff_entry, "end_line"),
            )
        ] = related_section

    for difference in _parse_plan_diff_differences(plan_diff_report):
        if difference.section not in {
            "entities",
            "decisions",
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
            key = (
                str(_value(source_span, "path", "")),
                _integer_value(source_span, "start_line"),
                _integer_value(source_span, "end_line"),
            )
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
            (
                str(_value(diff_entry, "path", "")),
                _integer_value(diff_entry, "start_line"),
                _integer_value(diff_entry, "end_line"),
            ),
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
        source_paths=dedupe_string_values(
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
            entities=dedupe_string_values([*related_section.entities, value]),
            decisions=related_section.decisions,
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

    if section_name == "decisions":
        return RelatedSectionPreview(
            entities=related_section.entities,
            decisions=dedupe_string_values([*related_section.decisions, value]),
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

    if section_name == "decisions":
        return RelatedSectionPreview(
            entities=related_section.entities,
            decisions=dedupe_string_values([*related_section.decisions, value]),
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
            decisions=related_section.decisions,
            invariants=dedupe_string_values([*related_section.invariants, value]),
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
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=dedupe_string_values([*related_section.guidance, value]),
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
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=dedupe_string_values([*related_section.proposed_prs, value]),
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
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=dedupe_string_values(
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
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=dedupe_string_values(
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
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=dedupe_string_values(
                [*related_section.required_test_cases, value]
            ),
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "expected_outcomes":
        return RelatedSectionPreview(
            entities=related_section.entities,
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=dedupe_string_values(
                [*related_section.expected_outcomes, value]
            ),
            non_goals=related_section.non_goals,
            risks=related_section.risks,
        )

    if section_name == "non_goals":
        return RelatedSectionPreview(
            entities=related_section.entities,
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=dedupe_string_values([*related_section.non_goals, value]),
            risks=related_section.risks,
        )

    if section_name == "risks":
        return RelatedSectionPreview(
            entities=related_section.entities,
            decisions=related_section.decisions,
            invariants=related_section.invariants,
            guidance=related_section.guidance,
            proposed_prs=related_section.proposed_prs,
            acceptance_criteria=related_section.acceptance_criteria,
            expected_tests=related_section.expected_tests,
            required_test_cases=related_section.required_test_cases,
            expected_outcomes=related_section.expected_outcomes,
            non_goals=related_section.non_goals,
            risks=dedupe_string_values([*related_section.risks, value]),
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
            related_section.decisions,
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
        ("decisions", related_section.decisions),
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


def related_section_matches(
    related: object,
    *,
    related_file_set: set[str],
    related_entity_set: set[str],
) -> bool:
    raw_files = _value(related, "files", ())
    raw_entities = _value(related, "entities", ())
    related_files = (
        set(raw_files)
        if isinstance(raw_files, Sequence)
        and all(isinstance(item, str) for item in raw_files)
        else set()
    )
    related_entities = (
        set(raw_entities)
        if isinstance(raw_entities, Sequence)
        and all(isinstance(item, str) for item in raw_entities)
        else set()
    )
    return bool(
        related_files.intersection(related_file_set)
        or related_entities.intersection(related_entity_set)
    )


def dedupe_string_values(values: Iterable[str]) -> tuple[str, ...]:
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
    diff_entries: Sequence[object],
) -> str:
    diff_lines = (
        "\n".join(
            f"#   {_value(entry, 'status', 'M')} {_value(entry, 'path', '')}"
            for entry in diff_entries
        )
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
        "#   `human-decisions`, `entity_relationships`, `invariants`, and\n"
        "#   `guidance`.\n"
        "# - Related references are optional. Remove empty related lists instead\n"
        "#   of leaving them in place, and remove `related` entirely if nothing\n"
        "#   needs to point at anything else.\n"
        "# - Put file-related entity ids under `related.entities`.\n"
        "# - Use `related.decisions` when this file change needs to point at the\n"
        "#   human decision ids that informed the code block. Make sure any\n"
        "#   input decisions are explicitly referenced in this block.\n"
        "# - Use `related.entities`, `related.decisions`, `related.invariants`,\n"
        "#   and `related.guidance` when this file change needs to point at\n"
        "#   supporting code areas or at the decision, invariant, or guidance\n"
        "#   entries it drives.\n"
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
        "# - Put ALL CREATIVE AND DESIGN DECISIONS MADE BY THE HUMAN OPERATOR\n"
        "#   in `human-decisions`.\n"
        "# - Use `human-decisions` for the top-level decision records and\n"
        "#   `related.decisions` to connect file entries back to them.\n"
        "# - Use `related` on entity, relationship, invariant, and guidance\n"
        "#   entries to point at the relevant entity, invariant, or guidance\n"
        "#   ids.\n"
        "#\n"
        "# Diff summary:\n"
        f"{diff_lines}"
    )


def is_structured_document_path(path: str) -> bool:
    normalized_path = path.strip().replace("\\", "/")
    return normalized_path.startswith(
        ("docs/proposals/", "docs/current/")
    ) and normalized_path.endswith(".yaml")


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
