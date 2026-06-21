from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def create_change_log_template(
    branch_name: str,
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
    related_sections_by_entry = _collect_related_sections_by_entry(
        repo_root_path,
        branch_name,
        default_branch_name,
        diff_entries,
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
        "version: 2",
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
        "# Structured document files go in `structured_files` as paths only.",
        "# Use `files` for code and other non-structured file entries.",
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
                    "    # relationships, invariants, and guidance.",
                    "    # Related ids for this file entry.",
                    "    # Remove this block entirely if it does not point",
                    "    # to anything.",
                    "    related:",
                    *(
                        ["      entities: []"]
                        if not related_section.entities
                        else [
                            "      entities:",
                            *(
                                f"        - {related_entity}"
                                for related_entity in related_section.entities
                            ),
                        ]
                    ),
                    *(
                        ["      invariants: []"]
                        if not related_section.invariants
                        else [
                            "      invariants:",
                            *(
                                f"        - {related_invariant}"
                                for related_invariant in related_section.invariants
                            ),
                        ]
                    ),
                    *(
                        ["      guidance: []"]
                        if not related_section.guidance
                        else [
                            "      guidance:",
                            *(
                                f"        - {related_guidance}"
                                for related_guidance in related_section.guidance
                            ),
                        ]
                    ),
                ]
            )
    else:
        lines.append("files: []")

    lines.extend(
        [
            "entities: []",
            "entity_relationships: []",
            "invariants: []",
            "guidance: []",
            "# Track feature and proposed PR state updates when relevant.",
            "# Use `in_progress` when work is underway and `completed` when",
            "# it is done.",
            "features: []",
            "prs: []",
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
            )
        )

    return related_sections_by_entry


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
        "# - Keep `version: 2` unless the schema changes.\n"
        "# - Put `path`, `type`, `span`, `summary`, `rationale`, and optional\n"
        "#   `related` on each file entry.\n"
        "# - Review each prefilled `related` section before you fill in the rest\n"
        "#   of the changelog. Keep entries that are still relevant, add\n"
        "#   missing ones, and remove stale ones.\n"
        "# - Use the final related lists to help fill out `entities`,\n"
        "#   `entity_relationships`, `invariants`, and `guidance`.\n"
        "# - Put file-related entity ids under `related.entities`.\n"
        "# - Use `related.entities`, `related.invariants`, and `related.guidance`\n"
        "#   when this file change needs to point at supporting code areas or at\n"
        "#   the invariant or guidance entries it drives.\n"
        "# - Remove `related` entirely if it would otherwise stay empty.\n"
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


def _resolve_default_branch(repo_root: Path) -> str:
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
        return "main"

    for line in remote_output.splitlines():
        if line.startswith("  HEAD branch: "):
            return line.partition(": ")[2].strip()

    return "main"


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
    normalized_path = path.strip()
    if not normalized_path.startswith("docs/"):
        return False

    return normalized_path.endswith((".md", ".markdown", ".yaml", ".yml"))


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


def _current_branch(repo_root: Path) -> str:
    return _git_output(repo_root, "branch", "--show-current").strip()


if __name__ == "__main__":
    raise SystemExit(main())
