"""Run a feature from specification through workflow verification.

This module intentionally orchestrates the existing agents.  It does not contain
feature implementation logic: the live Powdrr agent is responsible for creating
the specification, implementation plan, tests, and product edits.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_FEATURE_REQUEST = (
    "I want to specify a feature where all human and LLM interactions are written "
    "to a file log\n\n"
    "interaction-file-logging, capture all human and llm interaction inputs and "
    "outputs. format should be json, use a hidden directory like .powdrr and file "
    'name "interaction-log.json". Interactions are what was the input and output '
    "of every interaction with the user or with the LLM."
)
DEFAULT_FEATURE_NAME = "interaction-file-log"


@dataclass(frozen=True)
class AgentFeatureRunConfig:
    repo_root: Path
    feature_request: str = DEFAULT_FEATURE_REQUEST
    feature_name: str = DEFAULT_FEATURE_NAME
    answers: tuple[str, ...] = ()
    max_turns: int = 40
    workflow_roundtrips: int = 128
    start_iterations: int = 10
    provider: str = "deepinfra-cheap"
    report_path: Path = Path(".powdrr/agent-feature-run/report.json")
    transcript_dir: Path = Path(".powdrr/agent-feature-run/transcripts")
    phase_timeout: float | None = None


@dataclass(frozen=True)
class AgentFeatureRunResult:
    status: str
    phases: tuple[dict[str, Any], ...]
    report_path: Path

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "phases": list(self.phases),
            "report_path": str(self.report_path),
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _rooted(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _workflow_directories(repo_root: Path, feature_name: str) -> list[tuple[Path, str]]:
    root = repo_root / "docs" / "workflows" / feature_name
    if not root.is_dir():
        return []
    task_files = sorted(root.glob("*-task-*.yaml"))
    workflow_ids = sorted({path.name.split("-task-", 1)[0] for path in task_files})
    return [(root, workflow_id) for workflow_id in workflow_ids]


def _task_files(repo_root: Path, feature_name: str) -> list[Path]:
    root = repo_root / "docs" / "workflows" / feature_name
    return sorted(root.glob("*-task-*.yaml")) if root.is_dir() else []


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "powdrr_lift.cli", *args]


def _skill_subset(repo_root: Path, root_skill: str, destination: Path) -> Path:
    """Copy the selected skill and its nested-skill dependency closure."""
    source_dir = repo_root / "skill-definitions"
    destination.mkdir(parents=True, exist_ok=True)
    pending = [root_skill]
    copied: set[str] = set()
    while pending:
        skill_name = pending.pop()
        if skill_name in copied:
            continue
        source = source_dir / f"{skill_name}.yaml"
        if not source.is_file():
            raise FileNotFoundError(f"Missing skill dependency: {source}")
        copied.add(skill_name)
        shutil.copy2(source, destination / source.name)
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            for step in document.get("steps", []):
                if isinstance(step, dict):
                    deterministic = step.get("uses_skill")
                    if isinstance(deterministic, dict):
                        name = deterministic.get("skill")
                        if isinstance(name, str):
                            pending.append(name)
    return destination


def _run_phase(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    input_text: str = "",
    transcript: Path,
    runner: Runner,
    timeout: float | None,
) -> dict[str, Any]:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    try:
        if runner is subprocess.run:
            completed = _run_with_inactivity_timeout(
                command, cwd=cwd, input_text=input_text, timeout=timeout
            )
        else:
            completed = runner(
                list(command),
                cwd=cwd,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        returncode = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        timed_out_output = exc.stdout or ""
        timed_out_error = exc.stderr or ""
        if isinstance(timed_out_output, bytes):
            timed_out_output = timed_out_output.decode("utf-8", errors="replace")
        if isinstance(timed_out_error, bytes):
            timed_out_error = timed_out_error.decode("utf-8", errors="replace")
        output = (
            f"Phase exceeded timeout of {timeout:g} seconds.\n"
            f"{timed_out_output}{timed_out_error}"
        )
    transcript.write_text(output, encoding="utf-8")
    return {
        "name": name,
        "command": list(command),
        "returncode": returncode,
        "transcript": str(transcript),
        "output_tail": output.splitlines()[-40:],
    }


def _run_with_inactivity_timeout(
    command: Sequence[str], *, cwd: Path, input_text: str, timeout: float | None
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(input_text)
    process.stdin.close()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    pending = ""
    timeout_seconds = timeout if timeout is not None else 0.0
    deadline = time.monotonic() + timeout_seconds if timeout is not None else None
    try:
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(
                    command, timeout_seconds, output="".join(output)
                )
            events = selector.select(remaining)
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                output.append(text)
                pending += text
                lines = pending.splitlines(keepends=True)
                pending = (
                    lines.pop()
                    if lines and not lines[-1].endswith(("\n", "\r"))
                    else ""
                )
                if deadline is not None and any(
                    _is_semantic_progress(line) for line in lines
                ):
                    deadline = time.monotonic() + timeout_seconds
                continue
            if process.poll() is not None:
                break
        return subprocess.CompletedProcess(
            list(command), process.returncode, "".join(output), ""
        )
    finally:
        selector.close()


def _is_semantic_progress(line: str) -> bool:
    """Ignore per-token telemetry; reset only on workflow-level progress."""
    stripped = line.strip()
    if not stripped or stripped.startswith("received streamed LLM data"):
        return False
    if stripped.startswith(("waiting for ", "Status: waiting")):
        return False
    return any(
        marker in stripped
        for marker in (
            "[workflow] roundtrip",
            "[powdrr-file-added]",
            "Workflow progress:",
            "Workflow gate",
            "Attempting file edit",
            "✓",
            "completed",
            "passed",
        )
    )


def run_agent_feature_e2e(
    config: AgentFeatureRunConfig,
    *,
    runner: Runner = subprocess.run,
) -> AgentFeatureRunResult:
    """Execute and record the agent-driven feature lifecycle.

    The runner fails closed: later phases never run when an earlier agent phase
    fails, and a report is written even for a failed run.
    """

    root = config.repo_root.resolve()
    report_path = _rooted(config.report_path, root)
    transcript_dir = _rooted(config.transcript_dir, root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    phases: list[dict[str, Any]] = []
    default_answer = (
        "Use the requested interaction-file-logging behavior, with sensible defaults "
        "for unspecified details and no additional feature scope. If a workflow "
        "contract is contradictory, report the contradiction instead of repeating "
        "an invalid action."
    )
    # EOF is not an answer. Keep a bounded queue of explicit fallback responses so
    # prompt_user never receives a silent empty string when a workflow asks again.
    answers = list(config.answers)
    answers.extend([default_answer] * max(config.max_turns, 1))
    chat_input = "\n".join((config.feature_request, *answers)) + "\n"
    specify_skills = _skill_subset(
        root,
        "specify-a-feature",
        transcript_dir.parent / "skills" / "specify-a-feature",
    )
    start_skills = _skill_subset(
        root,
        "start-implementing-feature",
        transcript_dir.parent / "skills" / "start-implementing-feature",
    )

    phases.append(
        _run_phase(
            name="specify-a-feature",
            command=_command(
                "workflow-chat",
                "--repo-root",
                str(root),
                "--provider",
                config.provider,
                "--max-turns",
                str(config.max_turns),
                "--output-dir",
                "docs/workflows",
                "--skills-dir",
                str(specify_skills),
            ),
            cwd=root,
            input_text=chat_input,
            transcript=transcript_dir / "01-specify-a-feature.log",
            runner=runner,
            timeout=config.phase_timeout,
        )
    )

    if phases[-1]["returncode"] == 0:
        phases.append(
            _run_phase(
                name="start-implementing-feature",
                command=[
                    sys.executable,
                    "scripts/start-implementing-feature-harness.py",
                    "--repo-root",
                    str(root),
                    "--feature-name",
                    config.feature_name,
                    "--no-seed-feature",
                    "--no-isolate-run-worktree",
                    "--iterations",
                    str(config.start_iterations),
                    "--max-turns",
                    str(config.max_turns),
                    "--prompt",
                    f"Start implementing the existing {config.feature_name} feature "
                    "using the specifications just generated by specify-a-feature.",
                    "--workflow-arg=--skills-dir",
                    f"--workflow-arg={start_skills}",
                ],
                cwd=root,
                transcript=transcript_dir / "02-start-implementing-feature.log",
                runner=runner,
                timeout=config.phase_timeout,
            )
        )

    if phases and phases[-1]["returncode"] == 0:
        for workflow_dir, workflow_id in _workflow_directories(
            root, config.feature_name
        ):
            phases.append(
                _run_phase(
                    name=f"execute-workflow:{workflow_id}",
                    command=_command(
                        "process-workflow-task",
                        "--workflow-dir",
                        str(workflow_dir),
                        "--workflow-id",
                        workflow_id,
                        "--repo-root",
                        str(root),
                        "--provider",
                        config.provider,
                        "--max-roundtrips",
                        str(config.workflow_roundtrips),
                    ),
                    cwd=root,
                    transcript=transcript_dir / f"workflow-{workflow_id}.log",
                    runner=runner,
                    timeout=config.phase_timeout,
                )
            )
            if phases[-1]["returncode"] != 0:
                break

    if phases and phases[-1]["returncode"] == 0:
        proposal_dir = root / "docs" / "proposals" / config.feature_name
        for document in ("feature-pr-specification.yaml",):
            phases.append(
                _run_phase(
                    name=f"verify:{document}",
                    command=_command(
                        "evaluate",
                        str(proposal_dir / document),
                        "--repo-root",
                        str(root),
                    ),
                    cwd=root,
                    transcript=transcript_dir / f"verify-{document}.log",
                    runner=runner,
                    timeout=config.phase_timeout,
                )
            )
            if phases[-1]["returncode"] != 0:
                break

    status = (
        "passed"
        if phases and all(item["returncode"] == 0 for item in phases)
        else "failed"
    )
    report = {
        "schema_version": 1,
        "feature_name": config.feature_name,
        "feature_request": config.feature_request,
        "status": status,
        "task_files_observed": [
            str(path) for path in _task_files(root, config.feature_name)
        ],
        "phases": phases,
    }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return AgentFeatureRunResult(status, tuple(phases), report_path)
