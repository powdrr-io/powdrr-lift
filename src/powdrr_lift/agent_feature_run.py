"""Run a feature from specification through workflow verification.

This module intentionally orchestrates the existing agents.  It does not contain
feature implementation logic: the live Powdrr agent is responsible for creating
the specification, implementation plan, tests, and product edits.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    provider: str = "auto"
    report_path: Path = Path(".powdrr/agent-feature-run/report.json")
    transcript_dir: Path = Path(".powdrr/agent-feature-run/transcripts")


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


def _run_phase(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    input_text: str = "",
    transcript: Path,
    runner: Runner,
) -> dict[str, Any]:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    completed = runner(
        list(command),
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    transcript.write_text(output, encoding="utf-8")
    return {
        "name": name,
        "command": list(command),
        "returncode": completed.returncode,
        "transcript": str(transcript),
        "output_tail": output.splitlines()[-40:],
    }


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
    answers = config.answers or (
        "Use the requested interaction-file-logging behavior, with sensible defaults "
        "for unspecified details and no additional feature scope.",
    )
    chat_input = "\n".join((config.feature_request, *answers)) + "\n"

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
            ),
            cwd=root,
            input_text=chat_input,
            transcript=transcript_dir / "01-specify-a-feature.log",
            runner=runner,
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
                ],
                cwd=root,
                transcript=transcript_dir / "02-start-implementing-feature.log",
                runner=runner,
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
                )
            )
            if phases[-1]["returncode"] != 0:
                break

    if phases and phases[-1]["returncode"] == 0:
        proposal_dir = root / "docs" / "proposals" / config.feature_name
        for document in (
            "system-specification.yaml",
            "architecture-specification.yaml",
            "implementation-specification.yaml",
            "proposed-pr-specification.yaml",
        ):
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
