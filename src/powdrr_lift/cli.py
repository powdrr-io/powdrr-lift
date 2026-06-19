from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from powdrr_lift.blame_ui import serve as serve_blame_ui
from powdrr_lift.core import (
    codebase_state_default_output_path,
    create_change_log_template,
    create_codebase_state,
    lookup_edit_context,
    lookup_entity_decisions,
    lookup_entity_references,
    lookup_entity_relationships,
    parse_line_ranges,
    parse_validation_report,
    render_edit_context_report,
    render_entity_decision_report,
    render_entity_reference_report,
    render_entity_relationship_report,
    resolve_repo_root,
    validate_change_log_yaml,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powdrr-lift")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="Generate a ChangeLog template for the current branch.",
    )
    init_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to compare against the default branch.",
    )
    init_parser.add_argument(
        "--output",
        type=Path,
        help="Write the template to this path instead of the default file.",
    )
    init_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "Write the template to docs/changelogs/PR-<num>-changelog.yaml and "
            "print the next workflow step."
        ),
    )
    init_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    init_parser.add_argument(
        "--default-branch",
        help="Override the default branch name.",
    )
    init_parser.set_defaults(func=_run_init)

    evaluate_parser = subparsers.add_parser(
        "evaluate-pr-against-changelog",
        aliases=["evaluate_pr_against_changelog"],
        help="Validate a proposed ChangeLog against the branch diff.",
    )
    evaluate_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to compare against the default branch.",
    )
    evaluate_parser.add_argument(
        "--input",
        type=Path,
        help="Read the proposed ChangeLog YAML from this file instead of stdin.",
    )
    evaluate_parser.add_argument(
        "--pr-number",
        type=int,
        help=(
            "Read docs/changelogs/PR-<num>-changelog.yaml and print the final "
            "workflow step."
        ),
    )
    evaluate_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    evaluate_parser.add_argument(
        "--default-branch",
        help="Override the default branch name.",
    )
    evaluate_parser.set_defaults(func=_run_evaluate)

    edit_context_parser = subparsers.add_parser(
        "edit-context",
        aliases=["edit_context"],
        help="Report changelog-backed context for a file and line ranges.",
    )
    edit_context_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    edit_context_parser.add_argument(
        "--file",
        required=True,
        help="Repository-relative file path to inspect.",
    )
    edit_context_parser.add_argument(
        "--range",
        dest="line_ranges",
        action="append",
        required=True,
        metavar="START:END",
        help="Line range to inspect. May be repeated.",
    )
    edit_context_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    edit_context_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    edit_context_parser.set_defaults(func=_run_edit_context)

    entity_decisions_parser = subparsers.add_parser(
        "entity-decisions",
        aliases=["entity_decisions"],
        help="Report changelog decisions for PRs that mention an entity.",
    )
    entity_decisions_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_decisions_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_decisions_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_decisions_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_decisions_parser.set_defaults(func=_run_entity_decisions)

    entity_references_parser = subparsers.add_parser(
        "entity-references",
        aliases=["entity_references"],
        help="Report changelog-backed references for a named entity.",
    )
    entity_references_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_references_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_references_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_references_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_references_parser.set_defaults(func=_run_entity_references)

    entity_relationships_parser = subparsers.add_parser(
        "entity-relationships",
        aliases=["entity_relationships"],
        help="Report graph relationships for a named entity.",
    )
    entity_relationships_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    entity_relationships_parser.add_argument(
        "--entity",
        required=True,
        help="Canonical entity name to inspect.",
    )
    entity_relationships_parser.add_argument(
        "--parent-branch",
        required=True,
        help="Reference parent branch used to build the index.",
    )
    entity_relationships_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    entity_relationships_parser.set_defaults(func=_run_entity_relationships)

    codebase_state_parser = subparsers.add_parser(
        "codebase-state",
        aliases=["codebase_state"],
        help="Generate a changelog-derived snapshot of the current codebase.",
    )
    codebase_state_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch name to inspect. Defaults to the current branch.",
    )
    codebase_state_parser.add_argument(
        "--parent-branch",
        help="Reference parent branch used to build the index.",
    )
    codebase_state_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the snapshot to this path instead of "
            ".powdrr-lift/state/codebase-state.yaml."
        ),
    )
    codebase_state_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    codebase_state_parser.set_defaults(func=_run_codebase_state)

    blame_ui_parser = subparsers.add_parser(
        "blame-ui",
        aliases=["blame_ui"],
        help="Start a local GitHub-style blame viewer powered by the index.",
    )
    blame_ui_parser.add_argument(
        "branch_name",
        nargs="?",
        help="Branch to inspect. Defaults to the current branch.",
    )
    blame_ui_parser.add_argument(
        "--branch-name",
        dest="branch_name_flag",
        help="Branch to inspect. Defaults to the current branch.",
    )
    blame_ui_parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when running git commands.",
    )
    blame_ui_parser.add_argument(
        "--parent-branch",
        help="Reference parent branch used to build the index.",
    )
    blame_ui_parser.add_argument(
        "--file",
        dest="selected_file",
        help="Initial file to show in the blame UI.",
    )
    blame_ui_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the UI server to.",
    )
    blame_ui_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the UI server to.",
    )
    blame_ui_parser.set_defaults(func=_run_blame_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _run_init(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    output_path = _resolve_template_output_path(repo_root, args.output, args.pr_number)
    output_path = create_change_log_template(
        branch_name=branch_name,
        output_path=output_path,
        repo_root=repo_root,
        default_branch=args.default_branch,
    )
    print(output_path)
    if args.pr_number is not None:
        print("Next: fill out the template according to the instructions in the file.")
        print(
            "Then validate it with: "
            f"powdrr-lift evaluate-pr-against-changelog --pr-number {args.pr_number}"
        )
        print(
            "When it passes, include it in the PR as "
            f"docs/changelogs/PR-{args.pr_number}-changelog.yaml"
        )
    return 0


def _run_evaluate(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    input_path = _resolve_template_input_path(repo_root, args.input, args.pr_number)
    proposed_yaml = _read_input(input_path)
    report_yaml = validate_change_log_yaml(
        proposed_yaml,
        branch_name=branch_name,
        repo_root=repo_root,
        default_branch=args.default_branch,
    )
    report = parse_validation_report(report_yaml)
    sys.stdout.write(report_yaml)
    if not report_yaml.endswith("\n"):
        sys.stdout.write("\n")
    if args.pr_number is not None:
        if report.validation_successful:
            print(
                "Next: include docs/changelogs/PR-"
                f"{args.pr_number}-changelog.yaml in the PR.",
                file=sys.stderr,
            )
        else:
            print(
                "Next: fix docs/changelogs/PR-"
                f"{args.pr_number}-changelog.yaml and rerun the validate command.",
                file=sys.stderr,
            )
    return 0 if report.validation_successful else 1


def _run_edit_context(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_edit_context(
        args.file,
        parse_line_ranges(args.line_ranges),
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_edit_context_report(report))
    return 0


def _run_entity_references(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_references(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_reference_report(report))
    return 0


def _run_entity_relationships(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_relationships(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_relationship_report(report))
    return 0


def _run_codebase_state(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    output_path = create_codebase_state(
        branch_name=args.branch_name,
        output_path=args.output,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    if args.output is None:
        default_output = codebase_state_default_output_path(repo_root)
        print(f"Wrote codebase state to {default_output}")
    else:
        print(f"Wrote codebase state to {output_path}")

    return 0


def _run_entity_decisions(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = args.branch_name or _current_branch(repo_root)
    report = lookup_entity_decisions(
        args.entity,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        repo_root=repo_root,
    )
    sys.stdout.write(render_entity_decision_report(report))
    return 0


def _run_blame_ui(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    branch_name = (
        args.branch_name_flag or args.branch_name or _current_branch(repo_root)
    )
    serve_blame_ui(
        repo_root=repo_root,
        branch_name=branch_name,
        parent_branch=args.parent_branch,
        selected_file=args.selected_file,
        host=args.host,
        port=args.port,
    )
    return 0


def _current_branch(repo_root: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    branch_name = result.stdout.strip()
    if not branch_name:
        raise ValueError("Could not determine the current branch name.")

    return branch_name


def _read_input(input_path: Path | None) -> str:
    if input_path is None:
        return sys.stdin.read()

    return input_path.read_text(encoding="utf-8")


def _resolve_template_output_path(
    repo_root: Path,
    explicit_output_path: Path | None,
    pr_number: int | None,
) -> Path | None:
    if explicit_output_path is not None:
        return explicit_output_path

    if pr_number is None:
        return None

    return repo_root / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"


def _resolve_template_input_path(
    repo_root: Path,
    explicit_input_path: Path | None,
    pr_number: int | None,
) -> Path | None:
    if explicit_input_path is not None:
        return explicit_input_path

    if pr_number is None:
        return None

    return repo_root / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"


if __name__ == "__main__":
    raise SystemExit(main())
