#!/usr/bin/env python3
"""Run a workflow once and report structured LLM errors from its JSONL log."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

DEFAULT_PROMPT = (
    "I want to specify a feature where all human and LLM interactions are "
    "written to a file log"
)
DEFAULT_ANSWER = (
    "Use the feature name interaction-file-log. Log every human input and LLM "
    "response as JSONL with UTC timestamps and actor type, keep the path "
    "configurable and persistent, create missing directories, handle concurrent "
    "writes safely, avoid secrets, and add unit and integration tests."
)
DEFAULT_ERROR_LOG = Path("workflow-llm-errors.jsonl")
DEFAULT_TRANSCRIPT = Path("workflow-chat-harness.log")


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt supplied to the workflow (default: the interaction-log prompt).",
    )
    parser.add_argument(
        "--workflow-command",
        default="workflow-chat",
        help="powdrr-lift subcommand to run (default: workflow-chat).",
    )
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        help="Answer a workflow follow-up question (repeatable).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root in which workflow-chat should run.",
    )
    parser.add_argument(
        "--error-log",
        type=Path,
        default=DEFAULT_ERROR_LOG,
        help="JSONL error log path, relative to --repo-root by default.",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=DEFAULT_TRANSCRIPT,
        help="Combined workflow stdout/stderr transcript path.",
    )
    parser.add_argument(
        "--keep-log",
        action="store_true",
        help="Keep existing records and report only errors added by this run.",
    )
    parser.add_argument(
        "--workflow-arg",
        action="append",
        default=[],
        help="Additional argument passed to powdrr-lift (repeatable).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Maximum workflow-chat turns.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Maximum seconds for the workflow subprocess (default: 900).",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=15.0,
        help="Seconds between heartbeat messages when the child emits no output.",
    )
    return parser.parse_args()


def _read_errors(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    errors: list[dict[str, object]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if isinstance(record, dict):
            errors.append(record)
    return errors


def _log_root(repo_root: Path) -> Path:
    """Match workflow-chat's shared log location for dedicated worktrees."""
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


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    error_log = args.error_log
    if not error_log.is_absolute():
        error_log = _log_root(repo_root) / error_log
    transcript = args.transcript
    if not transcript.is_absolute():
        transcript = _log_root(repo_root) / transcript
    transcript.parent.mkdir(parents=True, exist_ok=True)
    previous_error_count = len(_read_errors(error_log)) if args.keep_log else 0
    if not args.keep_log:
        error_log.unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "powdrr_lift.cli",
        args.workflow_command,
        "--repo-root",
        str(repo_root),
        "--max-turns",
        str(args.max_turns),
        *args.workflow_arg,
    ]
    # A workflow may ask more than one follow-up question while repairing a
    # specification. Keep the default harness input available for every turn so
    # an unexpected prompt is captured in the transcript instead of terminating
    # the child with EOFError. Explicit answers remain a finite caller contract.
    answers = args.answer or [DEFAULT_ANSWER] * args.max_turns
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        start_new_session=(os.name != "nt"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timed_out = False
    interrupted = False
    output_chunks: list[bytes] = []
    input_data = ("\n".join([args.prompt, *answers]) + "\n").encode()
    assert process.stdin is not None
    process.stdin.write(input_data)
    process.stdin.close()
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    started_at = time.monotonic()
    next_heartbeat_at = started_at + args.progress_interval
    try:
        while process.poll() is None or selector.get_map():
            now = time.monotonic()
            remaining = args.timeout - (now - started_at)
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(
                timeout=min(remaining, max(0.1, next_heartbeat_at - now))
            ):
                stream = cast(Any, key.fileobj)
                chunk = stream.read1(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output_chunks.append(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            now = time.monotonic()
            if now >= next_heartbeat_at and process.poll() is None:
                captured = sum(len(chunk) for chunk in output_chunks)
                print(
                    f"Harness progress: workflow-chat still running after "
                    f"{now - started_at:.0f}s; captured={captured} bytes.",
                    file=sys.stderr,
                    flush=True,
                )
                next_heartbeat_at = now + args.progress_interval
    except KeyboardInterrupt:
        interrupted = True
    finally:
        selector.close()
        if process.poll() is None:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        output = b"".join(output_chunks)
    output_text = _text_output(output)
    transcript.write_text(output_text, encoding="utf-8")

    return_code = process.returncode
    termination = (
        "timed out"
        if timed_out
        else "interrupted"
        if interrupted
        else f"exit code {return_code}"
    )
    print(
        f"Harness transcript: {transcript} ({termination})",
        file=sys.stderr,
    )

    errors = _read_errors(error_log)[previous_error_count:]
    if errors:
        print(f"workflow-chat recorded {len(errors)} LLM error(s):", file=sys.stderr)
        for error in errors:
            context = error.get("context", {})
            skill = context.get("skill", {}) if isinstance(context, dict) else {}
            print(
                f"- {error.get('phase', 'unknown phase')}: {error.get('error', '')}"
                f" [{skill.get('name', 'selection')} step "
                f"{skill.get('step_index', '?')}]",
                file=sys.stderr,
            )
        return 1
    if timed_out or interrupted:
        print(
            f"workflow-chat {termination}; inspect {transcript} for final output.",
            file=sys.stderr,
        )
        return 124 if timed_out else 130
    if return_code != 0:
        print(
            "workflow-chat exited non-zero without a structured LLM error.",
            file=sys.stderr,
        )
    if return_code < 0:
        signal_name = signal.Signals(-return_code).name
        print(f"workflow-chat terminated by {signal_name}.", file=sys.stderr)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
