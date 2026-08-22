#!/usr/bin/env python3
"""Iteratively run start-implementing-feature and expose repair feedback."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
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
_FAILURE_MARKERS = (
    "action failed",
    "validation_error",
    "validation error",
    "correction_required",
    "workflow stopped",
    "repair failed",
)


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
    parser.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
    parser.add_argument("--transcript-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


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
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timed_out = False
    interrupted = False
    try:
        output, _ = process.communicate(
            input="\n".join([args.prompt, *answers]) + "\n",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        process.kill()
        output, _ = process.communicate()
        output = (exc.output or "") + (output or "")
    except KeyboardInterrupt:
        interrupted = True
        process.kill()
        output, _ = process.communicate()
    transcript.write_text(output or "", encoding="utf-8")
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
    return {
        "iteration": iteration,
        "status": status,
        "returncode": process.returncode,
        "transcript": str(transcript),
        "error_count": len(new_errors),
        "errors": new_errors,
        "corrections": corrections,
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
    log_root = _log_root(repo_root)
    error_log = _resolve_path(args.error_log, repo_root=repo_root, log_root=log_root)
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
            repo_root=repo_root,
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
                repo_root=repo_root,
                error_log=error_log,
                iteration=iteration,
                result=result,
            )
            != 0
        ):
            print("Repair command failed; stopping harness.", file=sys.stderr)
            break
    report_path.write_text(
        json.dumps(reports, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Harness did not complete cleanly; report: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
