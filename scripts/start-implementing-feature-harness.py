#!/usr/bin/env python3
"""Iteratively run start-implementing-feature and expose repair feedback."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_PROMPT = (
    "Start implementing the existing interaction-file-log feature. Use the "
    "canonical feature specification and existing workflow artifacts."
)
DEFAULT_ANSWER = (
    "Use the canonical feature named interaction-file-log and continue with the "
    "existing checked-in specifications and workflows."
)
DEFAULT_ERROR_LOG = Path("workflow-llm-errors.jsonl")
DEFAULT_TRANSCRIPT_DIR = Path("workflow-start-feature-harness")
DEFAULT_REPORT = Path("workflow-start-feature-harness-report.json")
DEFAULT_FEATURE_NAME = "interaction-file-log"
_FEATURE_DOCUMENTS = (
    "architecture-specification.yaml",
    "implementation-specification.yaml",
    "proposed-pr-specification.yaml",
    "system-specification.yaml",
)
_FAILURE_MARKERS = (
    "action failed",
    "validation_error",
    "validation error",
    "correction_required",
    "workflow stopped",
    "repair failed",
)


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--answer", action="append", default=[])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Maximum repair iterations (default: 10).",
    )
    parser.add_argument(
        "--repair-command",
        help=(
            "Command to run after a failed iteration. It receives HARNESS_* "
            "environment variables and may edit skill details or agent code."
        ),
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--workflow-arg", action="append", default=[])
    parser.add_argument(
        "--feature-name",
        default=DEFAULT_FEATURE_NAME,
        help=(
            "Feature directory to seed from repository history when it is absent "
            "(default: interaction-file-log)."
        ),
    )
    parser.add_argument(
        "--no-seed-feature",
        action="store_true",
        help="Do not restore a missing feature fixture from repository history.",
    )
    parser.add_argument(
        "--no-isolate-run-worktree",
        action="store_true",
        help="Run directly in --repo-root instead of an ephemeral harness worktree.",
    )
    parser.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
    parser.add_argument("--transcript-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def _feature_path(repo_root: Path, feature_name: str) -> Path:
    return repo_root / "docs" / "proposals" / feature_name


def _find_historical_feature_commit(repo_root: Path, feature_name: str) -> str | None:
    feature_path = f"docs/proposals/{feature_name}"
    commits = subprocess.run(
        ["git", "rev-list", "--all", "--", feature_path],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for commit in commits:
        if all(
            subprocess.run(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"{commit}:{feature_path}/{document}",
                ],
                cwd=repo_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
            for document in _FEATURE_DOCUMENTS
        ):
            return commit
    return None


def _seed_feature_from_history(repo_root: Path, feature_name: str) -> bool:
    destination = _feature_path(repo_root, feature_name)
    if destination.is_dir() and all(
        (destination / document).is_file() for document in _FEATURE_DOCUMENTS
    ):
        return False
    commit = _find_historical_feature_commit(repo_root, feature_name)
    if commit is None:
        raise SystemExit(
            f"Cannot seed {feature_name!r}: no complete historical feature "
            "specification was found. Pass --no-seed-feature to test missing "
            "context handling explicitly."
        )
    destination.mkdir(parents=True, exist_ok=True)
    for document in _FEATURE_DOCUMENTS:
        content = subprocess.run(
            [
                "git",
                "show",
                f"{commit}:docs/proposals/{feature_name}/{document}",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
        ).stdout
        (destination / document).write_bytes(content)
    workflow_prefix = f"docs/workflows/{feature_name}/"
    workflow_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit, workflow_prefix],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    # Task outputs are historical execution results, not reusable workflow
    # metadata. Seeding them makes discovery falsely report existing workflows
    # whose branches and dependency state no longer match the proposal.
    workflow_files = [
        relative_name
        for relative_name in workflow_files
        if relative_name.endswith("-workflow.yaml")
    ]
    for relative_name in workflow_files:
        workflow_destination = repo_root / relative_name
        workflow_destination.parent.mkdir(parents=True, exist_ok=True)
        workflow_destination.write_bytes(
            subprocess.run(
                ["git", "show", f"{commit}:{relative_name}"],
                cwd=repo_root,
                check=True,
                capture_output=True,
            ).stdout
        )
    print(
        f"Seeded {destination} and {len(workflow_files)} workflow artifact(s) "
        f"from historical commit {commit[:12]} for harness run.",
        file=sys.stderr,
    )
    # The workflow creates its own nested worktree from HEAD. Commit the
    # disposable fixture so that nested worktree receives the seeded files.
    seeded_paths = [f"docs/proposals/{feature_name}"]
    if workflow_files:
        seeded_paths.append(f"docs/workflows/{feature_name}")
    subprocess.run(
        ["git", "add", "--", *seeded_paths],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "Seed harness feature fixture"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return True


def _mirror_local_code_changes(source_root: Path, destination_root: Path) -> None:
    """Copy the caller's tracked code/skill edits into an isolated run tree."""
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "HEAD",
            "--",
            "skill-definitions",
            "src",
            "scripts",
        ],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for relative_name in changed:
        source = source_root / relative_name
        destination = destination_root / relative_name
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif destination.exists():
            destination.unlink()


def _create_isolated_run_worktree(repo_root: Path) -> Path:
    run_root = Path(
        tempfile.mkdtemp(
            prefix=f"{repo_root.name}-start-feature-run-", dir=repo_root.parent
        )
    )
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(run_root), "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    _mirror_local_code_changes(repo_root, run_root)
    return run_root


def _remove_isolated_run_worktree(repo_root: Path, run_root: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(run_root)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _log_root(repo_root: Path) -> Path:
    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (repo_root / common_path).resolve()
    return common_path.parent


def _resolve_path(path: Path, *, repo_root: Path, log_root: Path) -> Path:
    return path if path.is_absolute() else log_root / path


def _run_iteration(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    error_log: Path,
    transcript: Path,
    iteration: int,
) -> dict[str, Any]:
    previous_errors = len(_read_jsonl(error_log))
    command = [
        sys.executable,
        "-m",
        "powdrr_lift.cli",
        "workflow-chat",
        "--repo-root",
        str(repo_root),
        "--max-turns",
        str(args.max_turns),
        *args.workflow_arg,
    ]
    answers = args.answer or [DEFAULT_ANSWER] * args.max_turns
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript_stream = transcript.open("w", encoding="utf-8")
    child_environment = os.environ.copy()
    child_environment.pop("VIRTUAL_ENV", None)
    child_environment["UV_PROJECT_ENVIRONMENT"] = str(repo_root / ".venv")
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        text=True,
        stdin=subprocess.PIPE,
        stdout=transcript_stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        # Validation commands run inside the isolated worktree. Prevent uv
        # from inheriting the harness worktree's environment and repeatedly
        # resolving the wrong project environment.
        env=child_environment,
    )
    timed_out = False
    interrupted = False
    try:
        process.communicate(
            input="\n".join([args.prompt, *answers]) + "\n", timeout=args.timeout
        )
        output = transcript.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        output = transcript.read_text(encoding="utf-8")
    except KeyboardInterrupt:
        interrupted = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait()
        output = transcript.read_text(encoding="utf-8")
    finally:
        transcript_stream.close()
    new_errors = _read_jsonl(error_log)[previous_errors:]
    lowered_output = (output or "").lower()
    corrections = [marker for marker in _FAILURE_MARKERS if marker in lowered_output]
    status = "clean"
    if timed_out:
        status = "timeout"
    elif interrupted:
        status = "interrupted"
    elif new_errors or corrections or process.returncode != 0:
        status = "failed"
    output_lines = (output or "").splitlines()
    return {
        "iteration": iteration,
        "status": status,
        "returncode": process.returncode,
        "transcript": str(transcript),
        "error_count": len(new_errors),
        "errors": new_errors,
        "corrections": corrections,
        "last_output": output_lines[-40:],
        "last_roundtrip": next(
            (line.strip() for line in reversed(output_lines) if "roundtrip " in line),
            None,
        ),
    }


def _run_repair_command(
    command_text: str,
    *,
    repo_root: Path,
    error_log: Path,
    iteration: int,
    result: dict[str, Any],
) -> int:
    environment = {
        "HARNESS_ITERATION": str(iteration),
        "HARNESS_REPO_ROOT": str(repo_root),
        "HARNESS_TRANSCRIPT": result["transcript"],
        "HARNESS_ERROR_LOG": str(error_log),
        "HARNESS_RESULT_JSON": json.dumps(result, ensure_ascii=False),
    }
    print(
        f"Running repair command after iteration {iteration}: {command_text}",
        file=sys.stderr,
    )
    completed = subprocess.run(
        shlex.split(command_text),
        cwd=repo_root,
        env={**os.environ, **environment},
        check=False,
    )
    return completed.returncode


def main() -> int:
    args = _parse_args()
    if args.iterations < 1:
        raise SystemExit("--iterations must be positive")
    repo_root = args.repo_root.resolve()
    run_root = repo_root
    isolated = not args.no_isolate_run_worktree
    if isolated:
        run_root = _create_isolated_run_worktree(repo_root)
    try:
        if not args.no_seed_feature:
            _seed_feature_from_history(run_root, args.feature_name)
        log_root = _log_root(run_root)
        error_log = _resolve_path(
            args.error_log, repo_root=repo_root, log_root=log_root
        )
        transcript_dir = _resolve_path(
            args.transcript_dir, repo_root=repo_root, log_root=log_root
        )
        report_path = _resolve_path(args.report, repo_root=repo_root, log_root=log_root)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        reports: list[dict[str, Any]] = []
        for iteration in range(1, args.iterations + 1):
            transcript = transcript_dir / f"iteration-{iteration:02d}.log"
            result = _run_iteration(
                args=args,
                repo_root=run_root,
                error_log=error_log,
                transcript=transcript,
                iteration=iteration,
            )
            reports.append(result)
            print(
                f"Iteration {iteration}: {result['status']} "
                f"(errors={result['error_count']}, "
                f"corrections={len(result['corrections'])})",
                file=sys.stderr,
            )
            if result["status"] == "clean":
                report_path.write_text(
                    json.dumps(reports, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"Harness completed cleanly after {iteration} iteration(s).")
                return 0
            if result["status"] in {"timeout", "interrupted"}:
                break
            if not args.repair_command:
                print(
                    "Iteration failed; provide --repair-command to apply a fix and "
                    "continue. Inspect the iteration transcript and structured errors.",
                    file=sys.stderr,
                )
                break
            if (
                _run_repair_command(
                    args.repair_command,
                    repo_root=run_root,
                    error_log=error_log,
                    iteration=iteration,
                    result=result,
                )
                != 0
            ):
                print("Repair command failed; stopping harness.", file=sys.stderr)
                break
            if isolated:
                _mirror_local_code_changes(run_root, repo_root)
        report_path.write_text(
            json.dumps(reports, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(
            f"Harness did not complete cleanly; report: {report_path}",
            file=sys.stderr,
        )
        return 1
    finally:
        if isolated:
            _remove_isolated_run_worktree(repo_root, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
