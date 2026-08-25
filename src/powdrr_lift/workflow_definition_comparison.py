"""Deterministic baseline-versus-candidate workflow evaluation.

This module intentionally compares only replay bundles and scripted scenarios.
It never contacts a model provider, so its regression result is appropriate for
ordinary pull-request validation.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.workflow_replay import (
    WorkflowReplayError,
    load_workflow_replay_bundle,
    render_skill_replay,
)
from powdrr_lift.workflow_scenario import (
    WorkflowScenarioError,
    load_workflow_scenario,
    run_workflow_scenario,
)


class WorkflowComparisonError(ValueError):
    """Raised when a deterministic comparison cannot be assembled safely."""


@dataclass(frozen=True, slots=True)
class WorkflowEvaluationMetrics:
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    valid_replays: int = 0
    invalid_replays: int = 0
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    roundtrips: int = 0
    prompt_user_actions: int = 0
    repeated_actions: int = 0

    def to_data(self) -> dict[str, int]:
        return {
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "valid_replays": self.valid_replays,
            "invalid_replays": self.invalid_replays,
            "passed_scenarios": self.passed_scenarios,
            "failed_scenarios": self.failed_scenarios,
            "roundtrips": self.roundtrips,
            "prompt_user_actions": self.prompt_user_actions,
            "repeated_actions": self.repeated_actions,
        }


@dataclass(frozen=True, slots=True)
class WorkflowCaseEvaluation:
    case_id: str
    kind: str
    passed: bool
    details: Mapping[str, Any]
    available: bool = True

    def to_data(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "passed": self.passed,
            "available": self.available,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class WorkflowComparisonReport:
    baseline_ref: str
    baseline_metrics: WorkflowEvaluationMetrics
    candidate_metrics: WorkflowEvaluationMetrics
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    unchanged: tuple[str, ...]
    baseline_cases: tuple[WorkflowCaseEvaluation, ...]
    candidate_cases: tuple[WorkflowCaseEvaluation, ...]

    @property
    def passed(self) -> bool:
        return not self.regressions

    def to_data(self) -> dict[str, Any]:
        return {
            "baseline_ref": self.baseline_ref,
            "status": "passed" if self.passed else "regressed",
            "baseline_metrics": self.baseline_metrics.to_data(),
            "candidate_metrics": self.candidate_metrics.to_data(),
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "unchanged": list(self.unchanged),
            "baseline_cases": [case.to_data() for case in self.baseline_cases],
            "candidate_cases": [case.to_data() for case in self.candidate_cases],
        }


def compare_workflow_definitions(
    *,
    repo_root: Path,
    baseline_ref: str,
    replay_paths: Sequence[Path] = (),
    scenario_paths: Sequence[Path] = (),
    thresholds: Mapping[str, int] | None = None,
) -> WorkflowComparisonReport:
    """Evaluate identical, explicit deterministic cases on two repository states."""
    if not replay_paths and not scenario_paths:
        raise WorkflowComparisonError("Specify at least one replay or scenario.")
    candidate_root = repo_root.resolve()
    candidate_replays = _relative_paths(replay_paths, candidate_root, "replay")
    candidate_scenarios = _relative_paths(scenario_paths, candidate_root, "scenario")
    with materialize_baseline_repository(candidate_root, baseline_ref) as baseline_root:
        baseline_cases = _evaluate_cases(
            baseline_root, candidate_replays, candidate_scenarios
        )
        candidate_cases = _evaluate_cases(
            candidate_root, candidate_replays, candidate_scenarios
        )
    return _compare_case_evaluations(
        baseline_ref=baseline_ref,
        baseline_cases=baseline_cases,
        candidate_cases=candidate_cases,
        thresholds=thresholds,
    )


@contextmanager
def materialize_baseline_repository(
    repo_root: Path, baseline_ref: str
) -> Iterator[Path]:
    """Materialize a commit without adding an interfering Git worktree."""
    if not baseline_ref.strip():
        raise WorkflowComparisonError("baseline_ref must be non-empty.")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", baseline_ref],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if archive.returncode:
        detail = archive.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowComparisonError(
            f"Could not archive baseline ref {baseline_ref!r}: {detail}"
        )
    with tempfile.TemporaryDirectory(prefix="powdrr-lift-baseline-") as temporary:
        destination = Path(temporary) / "repository"
        destination.mkdir()
        _extract_archive(archive.stdout, destination)
        yield destination


def _extract_archive(contents: bytes, destination: Path) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(contents), mode="r:")
    except tarfile.TarError as exc:
        raise WorkflowComparisonError(f"Could not read Git archive: {exc}") from exc
    with archive:
        for member in archive.getmembers():
            target = destination / member.name
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise WorkflowComparisonError(
                    f"Baseline archive contains an unsafe path: {member.name}"
                ) from exc
            if (
                member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise WorkflowComparisonError(
                    f"Baseline archive contains unsupported entry: {member.name}"
                )
        archive.extractall(destination, filter="data")


def _relative_paths(
    paths: Sequence[Path], repo_root: Path, label: str
) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        resolved = path if path.is_absolute() else repo_root / path
        if not resolved.is_file():
            raise WorkflowComparisonError(f"{label} does not exist: {resolved}")
        try:
            result.append(resolved.resolve().relative_to(repo_root))
        except ValueError as exc:
            raise WorkflowComparisonError(
                f"{label} must be inside the candidate repository: {resolved}"
            ) from exc
    return tuple(result)


def _evaluate_cases(
    repo_root: Path, replay_paths: Sequence[Path], scenario_paths: Sequence[Path]
) -> tuple[WorkflowCaseEvaluation, ...]:
    evaluations: list[WorkflowCaseEvaluation] = []
    for relative_path in replay_paths:
        path = repo_root / relative_path
        if not path.is_file():
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="replay",
                    passed=False,
                    available=False,
                    details={"unavailable": f"Replay is absent: {relative_path}"},
                )
            )
            continue
        try:
            rendered = render_skill_replay(
                load_workflow_replay_bundle(path), repo_root=repo_root
            )
            validation = rendered["response_validation"]
            passed = bool(validation["valid"])
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="replay",
                    passed=passed,
                    details={"response_validation": validation},
                )
            )
        except (WorkflowReplayError, OSError) as exc:
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="replay",
                    passed=False,
                    details={"error": str(exc)},
                )
            )
    for relative_path in scenario_paths:
        path = repo_root / relative_path
        if not path.is_file():
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="scenario",
                    passed=False,
                    available=False,
                    details={"unavailable": f"Scenario is absent: {relative_path}"},
                )
            )
            continue
        try:
            result = run_workflow_scenario(
                load_workflow_scenario(path), scenario_path=path, repo_root=repo_root
            )
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="scenario",
                    passed=result.status == "passed",
                    details=result.to_data(),
                )
            )
        except (WorkflowScenarioError, OSError) as exc:
            evaluations.append(
                WorkflowCaseEvaluation(
                    case_id=str(relative_path),
                    kind="scenario",
                    passed=False,
                    details={"error": str(exc)},
                )
            )
    return tuple(evaluations)


def _compare_case_evaluations(
    *,
    baseline_ref: str,
    baseline_cases: Sequence[WorkflowCaseEvaluation],
    candidate_cases: Sequence[WorkflowCaseEvaluation],
    thresholds: Mapping[str, int] | None,
) -> WorkflowComparisonReport:
    if [(case.case_id, case.kind) for case in baseline_cases] != [
        (case.case_id, case.kind) for case in candidate_cases
    ]:
        raise WorkflowComparisonError("Baseline and candidate case sets differ.")
    baseline_metrics = _metrics(baseline_cases)
    candidate_metrics = _metrics(candidate_cases)
    comparable_pairs = [
        (before, after)
        for before, after in zip(baseline_cases, candidate_cases, strict=True)
        if before.available and after.available
    ]
    regressions: list[str] = []
    improvements: list[str] = []
    unchanged: list[str] = []
    for before, after in zip(baseline_cases, candidate_cases, strict=True):
        label = f"{after.kind} {after.case_id}"
        if not before.available:
            if after.available and after.passed:
                improvements.append(f"new {label} passed on candidate")
            elif after.available:
                regressions.append(f"new {label} failed on candidate")
            continue
        if not after.available:
            regressions.append(f"{label} is unavailable on candidate")
        elif before.passed and not after.passed:
            regressions.append(f"{label} passed on baseline but failed on candidate")
        elif not before.passed and after.passed:
            improvements.append(f"{label} failed on baseline but passed on candidate")
        else:
            unchanged.append(label)
    limits = {"roundtrips": 0, "prompt_user_actions": 0, "repeated_actions": 0}
    if thresholds is not None:
        for metric, threshold in thresholds.items():
            if metric not in limits or not isinstance(threshold, int) or threshold < 0:
                raise WorkflowComparisonError(
                    "thresholds may set non-negative roundtrips, "
                    "prompt_user_actions, or repeated_actions only."
                )
            limits[metric] = threshold
    comparable_baseline_metrics = _metrics([pair[0] for pair in comparable_pairs])
    comparable_candidate_metrics = _metrics([pair[1] for pair in comparable_pairs])
    for metric, threshold in limits.items():
        increase = getattr(comparable_candidate_metrics, metric) - getattr(
            comparable_baseline_metrics, metric
        )
        if increase > threshold:
            regressions.append(
                f"{metric} increased by {increase} (allowed increase: {threshold})"
            )
        elif increase < 0:
            improvements.append(f"{metric} decreased by {-increase}")
    return WorkflowComparisonReport(
        baseline_ref=baseline_ref,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        unchanged=tuple(unchanged),
        baseline_cases=tuple(baseline_cases),
        candidate_cases=tuple(candidate_cases),
    )


def _metrics(cases: Sequence[WorkflowCaseEvaluation]) -> WorkflowEvaluationMetrics:
    available_cases = [case for case in cases if case.available]
    values = Counter(total_cases=len(available_cases))
    for case in available_cases:
        values["passed_cases" if case.passed else "failed_cases"] += 1
        if case.kind == "replay":
            values["valid_replays" if case.passed else "invalid_replays"] += 1
            continue
        values["passed_scenarios" if case.passed else "failed_scenarios"] += 1
        details = case.details
        values["roundtrips"] += int(details.get("roundtrips", 0))
        events = details.get("execution_events", [])
        if isinstance(events, list):
            mappings = [event for event in events if isinstance(event, Mapping)]
            action_kinds = [event.get("kind") for event in mappings]
            values["prompt_user_actions"] += action_kinds.count("prompt_user")
            counts = Counter(_action_fingerprint(event) for event in mappings)
            values["repeated_actions"] += sum(
                count - 1 for count in counts.values() if count > 1
            )
    return WorkflowEvaluationMetrics(**values)


def _action_fingerprint(event: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            key: value
            for key, value in event.items()
            if key not in {"result", "decisions_and_context"}
        },
        sort_keys=True,
        default=str,
    )
