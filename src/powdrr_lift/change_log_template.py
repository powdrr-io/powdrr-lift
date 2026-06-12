from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class BranchDiffEntry:
    status: str
    path: str


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
    output_path.write_text(template, encoding="utf-8")
    return output_path


def render_change_log_template(
    branch_name: str,
    default_branch_name: str,
    diff_entries: Sequence[BranchDiffEntry],
) -> str:
    header = _render_header(branch_name, default_branch_name, diff_entries)
    template_data: dict[str, object] = {
        "version": 1,
        "change_id": None,
        "title": None,
        "intent": {"problem": None, "goal": None},
        "decision": {"id": None, "summary": None},
        "entities": [],
        "changes": [
            {
                "id": None,
                "file": diff_entry.path,
                "span": {"start_line": None, "end_line": None},
                "summary": None,
                "affects": [],
                "rationale": None,
            }
            for diff_entry in diff_entries
        ],
        "relationship_changes": [],
    }
    body = yaml.safe_dump(template_data, sort_keys=False, default_flow_style=False)
    return f"{header}\n{body}"


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

        entries.append(BranchDiffEntry(status=status, path=path))

    return entries


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
