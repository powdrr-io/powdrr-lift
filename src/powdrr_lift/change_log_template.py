from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.changelog.template import (
    RelatedSectionPreview,
    dedupe_string_values,
    merge_related_sections_by_plan_diff,
    related_section_matches,
)
from powdrr_service.changelog.template import (
    render_change_log_template as _render_change_log_template,
)


@dataclass(frozen=True, slots=True)
class BranchDiffEntry:
    status: str
    path: str
    start_line: int
    end_line: int


def create_change_log_template(
    branch_name: str | None = None,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> Path:
    from powdrr_lift.core.template_generation import merge_existing_template_content

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
    template = _render_change_log_template(
        branch_name=resolved_branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
        related_sections_by_entry=related_sections_by_entry,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = (
        output_path.read_text(encoding="utf-8") if output_path.is_file() else None
    )
    output_path.write_text(
        merge_existing_template_content(template, existing_content),
        encoding="utf-8",
    )
    return output_path


def create_change_log_template_from_plan_diff(
    *,
    branch_name: str,
    plan_diff_path: str | Path,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> Path:
    from powdrr_lift.core.template_generation import merge_existing_template_content

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
    related_sections_by_entry = merge_related_sections_by_plan_diff(
        diff_entries,
        related_sections_by_entry,
        plan_diff_report,
    )
    template = _render_change_log_template(
        branch_name=branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
        related_sections_by_entry=related_sections_by_entry,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = (
        output_path.read_text(encoding="utf-8") if output_path.is_file() else None
    )
    output_path.write_text(
        merge_existing_template_content(template, existing_content),
        encoding="utf-8",
    )
    return output_path


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
        related_entities = dedupe_string_values(
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
                decisions=(),
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

            if related_section_matches(
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
