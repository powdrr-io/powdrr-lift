from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    PLAN_DIFF_SCHEMA_URL,
    normalize_work_item_name,
    plan_diff_specification_path,
)


@dataclass(frozen=True, slots=True)
class PlanDiffEntry:
    id: str
    kind: str
    section: str
    description: str
    plan_value: str | None = None
    changelog_value: str | None = None
    source_paths: tuple[str, ...] = ()
    source_spans: tuple[PlanDiffSourceSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanDiffSourceSpan:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class PlanDiffReport:
    feature_plan_path: str
    changelog_paths: list[str] = field(default_factory=list)
    differences: list[PlanDiffEntry] = field(default_factory=list)


def plan_diff_specification_default_output_path(
    feature_plan_specification_path: str | Path,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_plan_path = _resolve_input_path(
        feature_plan_specification_path,
        repo_root_path,
    )
    work_item_name = normalize_work_item_name(resolved_plan_path.parent.name)
    return plan_diff_specification_path(repo_root_path, work_item_name)


def create_plan_diff_specification(
    *,
    feature_plan_specification_path: str | Path,
    changelog_paths: Sequence[str | Path],
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    report = build_plan_diff_report(
        feature_plan_specification_path=feature_plan_specification_path,
        changelog_paths=changelog_paths,
        repo_root=repo_root_path,
    )
    resolved_output_path = (
        Path(output_path)
        if output_path is not None
        else plan_diff_specification_default_output_path(
            feature_plan_specification_path,
            repo_root_path,
        )
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_plan_diff_specification(report),
        encoding="utf-8",
    )
    return resolved_output_path


def build_plan_diff_report(
    *,
    feature_plan_specification_path: str | Path,
    changelog_paths: Sequence[str | Path],
    repo_root: str | Path | None = None,
) -> PlanDiffReport:
    repo_root_path = _resolve_repo_root(repo_root)
    plan_path = _resolve_input_path(feature_plan_specification_path, repo_root_path)
    plan_data = _load_yaml_mapping(plan_path.read_text(encoding="utf-8"))
    plan_intent = _collect_plan_intent(plan_data)
    plan_sections = _collect_plan_sections(plan_data)

    resolved_changelog_paths = [
        _resolve_input_path(changelog_path, repo_root_path)
        for changelog_path in changelog_paths
    ]
    changelog_docs = [
        (path, parse_change_log(path.read_text(encoding="utf-8")))
        for path in resolved_changelog_paths
    ]
    changelog_sections = _collect_changelog_sections(changelog_docs)
    related_sections = _collect_changelog_related_sections(changelog_docs)
    source_spans_by_section = _collect_changelog_source_spans_by_section(changelog_docs)

    differences = _collect_differences(
        plan_intent=plan_intent,
        plan_sections=plan_sections,
        changelog_docs=changelog_docs,
        changelog_sections=changelog_sections,
        related_sections=related_sections,
        source_spans_by_section=source_spans_by_section,
        repo_root=repo_root_path,
    )

    return PlanDiffReport(
        feature_plan_path=_render_repo_relative_path(plan_path, repo_root_path),
        changelog_paths=[
            _render_repo_relative_path(path, repo_root_path)
            for path in resolved_changelog_paths
        ],
        differences=differences,
    )


def render_plan_diff_specification(report: PlanDiffReport) -> str:
    lines = [
        "# Plan diff specification template.",
        "#",
        "# Instructions:",
        "# - Compare the feature plan specification to each changelog file.",
        "# - Examine one difference at a time.",
        "# - Use `source_spans` to point at the file hunks that should carry",
        "#   the missing changelog evidence whenever such spans exist.",
        "# - If a difference already has source spans, update those spans in the",
        "#   changelog first and then make the code change that the span implies.",
        "# - If a difference has no source spans, make the code change that",
        "#   would create one before editing the changelog.",
        "# - Make the code changes needed to remove that difference.",
        "# - Update the changelog after each change set so the changelog and",
        "#   the plan stay aligned.",
        "# - When all differences are resolved, delete the old changelog and",
        "#   this diff file.",
        "# - Regenerate the changelog template with `powdrr-lift init` and",
        "#   then regenerate this diff.",
        "# - Repeat until `differences` is empty.",
        "",
    ]
    return "\n".join(lines) + yaml.safe_dump(
        _report_to_data(report),
        sort_keys=False,
        allow_unicode=False,
    )


def _collect_differences(
    *,
    plan_intent: tuple[str | None, str | None],
    plan_sections: Mapping[str, set[str]],
    changelog_docs: Sequence[tuple[Path, Any]],
    changelog_sections: Mapping[str, set[str]],
    related_sections: Mapping[str, set[str]],
    source_spans_by_section: Mapping[str, tuple[PlanDiffSourceSpan, ...]],
    repo_root: Path,
) -> list[PlanDiffEntry]:
    differences: list[PlanDiffEntry] = []

    for changelog_path, changelog in changelog_docs:
        if _intents_differ(
            plan_intent,
            changelog.intent.problem,
            changelog.intent.goal,
        ):
            differences.append(
                PlanDiffEntry(
                    id=f"intent-{len(differences) + 1}",
                    kind="intent_mismatch",
                    section="intent",
                    description=(
                        "The changelog intent does not match the feature plan intent."
                    ),
                    plan_value=_format_intent(*plan_intent),
                    changelog_value=_format_intent(
                        changelog.intent.problem,
                        changelog.intent.goal,
                    ),
                    source_paths=(
                        _render_repo_relative_path(changelog_path, repo_root),
                    ),
                    source_spans=(
                        source_spans_by_section.get("intent")
                        or source_spans_by_section.get("all", ())
                    ),
                )
            )

    section_map = {
        "features": "feature_changes",
        "decisions": "decisions",
        "entities": "entity_changes",
        "entity_relationships": "entity_relationship_changes",
        "invariants": "invariant_changes",
        "guidance": "guidance_changes",
    }
    for section_name, changelog_section_name in section_map.items():
        differences.extend(
            _collect_section_differences(
                section_name=section_name,
                plan_ids=plan_sections[section_name],
                changelog_ids=changelog_sections[changelog_section_name],
                source_paths=tuple(
                    _render_repo_relative_path(path, repo_root)
                    for path, _ in changelog_docs
                ),
                source_spans=(
                    source_spans_by_section.get(section_name)
                    or source_spans_by_section.get("all", ())
                ),
            )
        )

    for section_name in (
        "acceptance_criteria",
        "expected_tests",
        "expected_outcomes",
        "non_goals",
        "risks",
    ):
        differences.extend(
            _collect_section_differences(
                section_name=section_name,
                plan_ids=plan_sections[section_name],
                changelog_ids=related_sections[section_name],
                source_paths=tuple(
                    _render_repo_relative_path(path, repo_root)
                    for path, _ in changelog_docs
                ),
                source_spans=(
                    source_spans_by_section.get(section_name)
                    or source_spans_by_section.get("all", ())
                ),
            )
        )

    return differences


def _collect_section_differences(
    *,
    section_name: str,
    plan_ids: set[str],
    changelog_ids: set[str],
    source_paths: tuple[str, ...],
    source_spans: tuple[PlanDiffSourceSpan, ...],
) -> list[PlanDiffEntry]:
    differences: list[PlanDiffEntry] = []

    for item_id in sorted(plan_ids - changelog_ids):
        differences.append(
            PlanDiffEntry(
                id=f"{section_name}-{item_id}-missing",
                kind="missing_from_changelog",
                section=section_name,
                description=(
                    f"The plan item {item_id!r} is not represented in the changelog."
                ),
                plan_value=item_id,
                changelog_value=None,
                source_paths=source_paths,
                source_spans=source_spans,
            )
        )

    for item_id in sorted(changelog_ids - plan_ids):
        differences.append(
            PlanDiffEntry(
                id=f"{section_name}-{item_id}-unexpected",
                kind="unexpected_in_changelog",
                section=section_name,
                description=(
                    f"The changelog item {item_id!r} is not present in the plan."
                ),
                plan_value=None,
                changelog_value=item_id,
                source_paths=source_paths,
                source_spans=source_spans,
            )
        )

    return differences


def _collect_plan_intent(
    raw_plan: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    intent = _load_mapping(raw_plan.get("intent"))
    return (
        _optional_string(intent.get("problem")),
        _optional_string(intent.get("goal")),
    )


def _collect_plan_sections(raw_plan: Mapping[str, Any]) -> dict[str, set[str]]:
    return {
        "features": _collect_plan_id_set(raw_plan, "features"),
        "decisions": _collect_plan_id_set(raw_plan, "decisions"),
        "entities": _collect_plan_id_set(raw_plan, "entities"),
        "entity_relationships": _collect_plan_id_set(
            raw_plan,
            "entity_relationships",
        ),
        "invariants": _collect_plan_id_set(raw_plan, "invariants"),
        "guidance": _collect_plan_id_set(raw_plan, "guidance"),
        "acceptance_criteria": _collect_plan_id_set(raw_plan, "acceptance_criteria"),
        "expected_tests": _collect_plan_id_set(raw_plan, "expected_tests"),
        "expected_outcomes": _collect_plan_id_set(raw_plan, "expected_outcomes"),
        "non_goals": _collect_plan_id_set(raw_plan, "non_goals"),
        "risks": _collect_plan_id_set(raw_plan, "risks"),
    }


def _collect_changelog_sections(
    changelog_docs: Sequence[tuple[Path, Any]],
) -> dict[str, set[str]]:
    sections: dict[str, set[str]] = {
        "feature_changes": set(),
        "decisions": set(),
        "entity_changes": set(),
        "entity_relationship_changes": set(),
        "invariant_changes": set(),
        "guidance_changes": set(),
    }

    for _, changelog in changelog_docs:
        sections["feature_changes"].update(
            _collect_object_ids(changelog.feature_changes)
        )
        sections["decisions"].update(_collect_object_ids(changelog.decisions))
        sections["entity_changes"].update(_collect_object_ids(changelog.entity_changes))
        sections["entity_relationship_changes"].update(
            _collect_object_ids(changelog.entity_relationship_changes)
        )
        sections["invariant_changes"].update(
            _collect_object_ids(changelog.invariant_changes)
        )
        sections["guidance_changes"].update(
            _collect_object_ids(changelog.guidance_changes)
        )

    return sections


def _collect_changelog_related_sections(
    changelog_docs: Sequence[tuple[Path, Any]],
) -> dict[str, set[str]]:
    related_sections: dict[str, set[str]] = {
        "acceptance_criteria": set(),
        "expected_tests": set(),
        "expected_outcomes": set(),
        "non_goals": set(),
        "risks": set(),
    }

    for _, changelog in changelog_docs:
        for raw_item in (
            [*changelog.file_changes]
            + [*changelog.entity_changes]
            + [*changelog.entity_relationship_changes]
            + [*changelog.invariant_changes]
            + [*changelog.guidance_changes]
        ):
            related = getattr(raw_item, "related", None)
            if related is None:
                continue

            for section_name in related_sections:
                related_sections[section_name].update(
                    _normalize_string_values(getattr(related, section_name))
                )

    return related_sections


def _collect_changelog_source_spans_by_section(
    changelog_docs: Sequence[tuple[Path, Any]],
) -> dict[str, tuple[PlanDiffSourceSpan, ...]]:
    source_spans_by_section: dict[str, list[PlanDiffSourceSpan]] = {
        "all": [],
        "intent": [],
        "features": [],
        "decisions": [],
        "entities": [],
        "entity_relationships": [],
        "invariants": [],
        "guidance": [],
        "acceptance_criteria": [],
        "expected_tests": [],
        "required_test_cases": [],
        "expected_outcomes": [],
        "non_goals": [],
        "risks": [],
    }
    seen_spans_by_section: dict[str, set[tuple[str, int, int]]] = {
        section_name: set() for section_name in source_spans_by_section
    }
    for _changelog_path, changelog in changelog_docs:
        for file_change in getattr(changelog, "file_changes", ()):
            path = getattr(file_change, "path", None)
            span = getattr(file_change, "span", None)
            start_line = getattr(span, "start_line", None)
            end_line = getattr(span, "end_line", None)
            if (
                path is None
                or start_line is None
                or end_line is None
                or not isinstance(start_line, int)
                or not isinstance(end_line, int)
            ):
                continue

            key = (str(path), start_line, end_line)
            span_preview = PlanDiffSourceSpan(
                path=str(path),
                start_line=start_line,
                end_line=end_line,
            )
            if key not in seen_spans_by_section["all"]:
                seen_spans_by_section["all"].add(key)
                source_spans_by_section["all"].append(span_preview)

            for section_name in _infer_source_span_sections(
                path=str(path),
                file_change=file_change,
            ):
                if key in seen_spans_by_section[section_name]:
                    continue

                seen_spans_by_section[section_name].add(key)
                source_spans_by_section[section_name].append(span_preview)

    return {
        section_name: tuple(source_spans)
        for section_name, source_spans in source_spans_by_section.items()
    }


def _infer_source_span_sections(
    *,
    path: str,
    file_change: Any,
) -> tuple[str, ...]:
    related = getattr(file_change, "related", None)
    related_entities = tuple(getattr(related, "entities", ()) if related else ())
    related_invariants = tuple(getattr(related, "invariants", ()) if related else ())
    related_guidance = tuple(getattr(related, "guidance", ()) if related else ())
    related_required_test_cases = tuple(
        getattr(related, "required_test_cases", ()) if related else ()
    )
    related_acceptance_criteria = tuple(
        getattr(related, "acceptance_criteria", ()) if related else ()
    )
    related_expected_tests = tuple(
        getattr(related, "expected_tests", ()) if related else ()
    )
    related_expected_outcomes = tuple(
        getattr(related, "expected_outcomes", ()) if related else ()
    )
    related_non_goals = tuple(getattr(related, "non_goals", ()) if related else ())
    related_risks = tuple(getattr(related, "risks", ()) if related else ())
    path_lower = path.lower()
    sections: list[str] = []

    if "test" in path_lower or related_required_test_cases:
        sections.append("required_test_cases")
        sections.append("expected_tests")
        sections.append("acceptance_criteria")
        sections.append("expected_outcomes")

    if related_entities:
        sections.extend(("entities", "entity_relationships"))
    if related_invariants:
        sections.append("invariants")
    if related_guidance:
        sections.append("guidance")
    if related_acceptance_criteria:
        sections.append("acceptance_criteria")
    if related_expected_tests:
        sections.append("expected_tests")
    if related_expected_outcomes:
        sections.append("expected_outcomes")
    if related_non_goals:
        sections.append("non_goals")
    if related_risks:
        sections.append("risks")
    if not sections and path.startswith("docs/"):
        sections.append("guidance")

    ordered_sections: list[str] = []
    seen_sections: set[str] = set()
    for section_name in sections:
        if section_name in seen_sections:
            continue

        seen_sections.add(section_name)
        ordered_sections.append(section_name)

    return tuple(ordered_sections)


def _collect_plan_id_set(raw_plan: Mapping[str, Any], section_name: str) -> set[str]:
    ids: set[str] = set()
    for raw_item in _ensure_sequence(raw_plan.get(section_name)):
        if isinstance(raw_item, Mapping):
            item_id = _optional_string(cast(Mapping[str, Any], raw_item).get("id"))
            if item_id is not None:
                ids.add(item_id)
    return ids


def _collect_object_ids(items: Sequence[Any]) -> set[str]:
    ids: set[str] = set()
    for item in items:
        item_id = _optional_string(getattr(item, "id", None))
        if item_id is not None:
            ids.add(item_id)
    return ids


def _normalize_string_values(values: Sequence[Any] | None) -> set[str]:
    normalized_values: set[str] = set()
    for value in values or []:
        normalized_value = _optional_string(value)
        if normalized_value is not None:
            normalized_values.add(normalized_value)
    return normalized_values


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded_yaml = yaml.safe_load(raw_yaml)
    if loaded_yaml is None:
        return {}

    if not isinstance(loaded_yaml, Mapping):
        raise ValueError("Feature plan YAML must decode to a mapping.")

    return cast(Mapping[str, Any], loaded_yaml)


def _load_mapping(raw_value: object | None) -> Mapping[str, Any]:
    if raw_value is None:
        return {}

    if not isinstance(raw_value, Mapping):
        raise ValueError("Expected a YAML mapping.")

    return cast(Mapping[str, Any], raw_value)


def _ensure_sequence(raw_value: object | None) -> Sequence[object]:
    if raw_value is None:
        return ()

    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value

    raise ValueError("Expected a YAML sequence.")


def _optional_string(raw_value: object | None) -> str | None:
    if raw_value is None:
        return None

    normalized_value = str(raw_value).strip()
    return normalized_value or None


def _intents_differ(
    plan_intent: tuple[str | None, str | None],
    changelog_problem: str | None,
    changelog_goal: str | None,
) -> bool:
    return plan_intent != (changelog_problem, changelog_goal)


def _format_intent(
    problem: str | None,
    goal: str | None = None,
) -> str:
    if goal is None:
        return f"problem={problem!r}"

    return f"problem={problem!r}, goal={goal!r}"


def _report_to_data(report: PlanDiffReport) -> dict[str, Any]:
    return {
        "schema": PLAN_DIFF_SCHEMA_URL,
        "feature_plan_path": report.feature_plan_path,
        "changelog_paths": report.changelog_paths,
        "differences": [
            {
                "id": difference.id,
                "kind": difference.kind,
                "section": difference.section,
                "description": difference.description,
                "plan_value": difference.plan_value,
                "changelog_value": difference.changelog_value,
                "source_paths": list(difference.source_paths),
                "source_spans": [
                    {
                        "path": span.path,
                        "start_line": span.start_line,
                        "end_line": span.end_line,
                    }
                    for span in difference.source_spans
                ],
            }
            for difference in report.differences
        ],
    }


def _resolve_input_path(path: str | Path, repo_root: Path) -> Path:
    resolved_path = Path(path)
    if resolved_path.is_absolute():
        return resolved_path
    return repo_root / resolved_path


def _render_repo_relative_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return path.as_posix()
