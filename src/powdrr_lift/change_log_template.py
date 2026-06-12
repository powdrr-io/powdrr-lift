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
        "version: 1",
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
        "# List the repository entities affected by this change.",
        "entities: []",
        "# Each change entry should map one changed file.",
    ]

    if diff_entries:
        lines.append("changes:")
        for diff_entry in diff_entries:
            lines.extend(
                [
                    "  -",
                    "    # File changed on branch; keep this path aligned with",
                    "    # the diff.",
                    f"    file: {diff_entry.path}",
                    "    span:",
                    "      # First changed line in this file.",
                    f"      start_line: {diff_entry.start_line}",
                    "      # Last changed line in this file.",
                    f"      end_line: {diff_entry.end_line}",
                    "    # Short description of the file-level change.",
                    "    summary: null",
                    "    # List of entity ids affected by this change.",
                    "    affects: []",
                    "    # Why the change was made.",
                    "    rationale: null",
                ]
            )
    else:
        lines.append("changes: []")

    lines.extend(
        [
            "# Relationship changes are optional and can remain empty.",
            "relationship_changes: []",
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
        "# - Use the prefilled `changes` entries as the starting point for the files\n"
        "#   that differ from the default branch.\n"
        "# - Add or remove `changes` items as needed so every meaningful change is\n"
        "#   represented exactly once.\n"
        "# - Keep `version: 1` unless the schema changes.\n"
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

        start_line, end_line = _collect_file_span(
            repo_root,
            default_branch_name,
            branch_name,
            path,
        )
        entries.append(
            BranchDiffEntry(
                status=status,
                path=path,
                start_line=start_line,
                end_line=end_line,
            )
        )

    return entries


def _collect_file_span(
    repo_root: Path,
    default_branch_name: str,
    branch_name: str,
    path: str,
) -> tuple[int, int]:
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
        return 1, 1

    start_line = min(span[0] for span in span_ranges)
    end_line = max(span[1] for span in span_ranges)
    return start_line, end_line


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
