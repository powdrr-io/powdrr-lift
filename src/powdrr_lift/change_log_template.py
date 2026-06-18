from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BranchDiffEntry:
    status: str
    path: str
    start_line: int
    end_line: int


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
    template = render_change_log_template(
        branch_name=branch_name,
        default_branch_name=default_branch_name,
        diff_entries=diff_entries,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template, encoding="utf-8")
    return output_path


def render_change_log_template(
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[BranchDiffEntry],
) -> str:
    header = _render_header(branch_name, default_branch_name, diff_entries)
    body = _render_template_body(diff_entries)
    return f"{header}\n{body}"


def _render_template_body(diff_entries: Sequence[BranchDiffEntry]) -> str:
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
        "# Each file entry should record the files that changed and their",
        "# associated metadata.",
    ]

    if diff_entries:
        lines.append("files:")
        for diff_entry in diff_entries:
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
                    "    # Related ids for this file entry.",
                    "    # Remove this block entirely if it does not point",
                    "    # to anything.",
                    "    related:",
                    "      files: []",
                    "      entities: []",
                    "      invariants: []",
                    "      guidance: []",
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
        ]
    )

    lines.extend(
        [
            "",
        ]
    )

    return "\n".join(lines)


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
        "# - Put file-related entity ids under `related.entities`.\n"
        "# - Remove `related` entirely if it would otherwise stay empty.\n"
        "# - Put entity lifecycle changes in `entities` with `action: added`,\n"
        "#   `action: deleted`, or `action: modified`.\n"
        "# - Put relationship changes in `entity_relationships`.\n"
        "# - Put invariant changes in `invariants` and guidance changes in\n"
        "#   `guidance`.\n"
        "# - Use the `related` section on file, invariant, and guidance entries to\n"
        "#   point at the relevant file, entity, invariant, or guidance ids.\n"
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
