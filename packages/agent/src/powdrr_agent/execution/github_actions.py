"""GitHub Actions evidence transport owned by the local agent."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class GitHubActionsError(RuntimeError):
    """GitHub Actions evidence could not be fetched or checkpointed."""


@dataclass(frozen=True, slots=True)
class GitHubActionsSnapshot:
    repository: str
    pull_request: Mapping[str, Any]
    runs: tuple[Mapping[str, Any], ...]
    jobs_by_run: Mapping[str, tuple[Mapping[str, Any], ...]]
    annotations_by_check_run: Mapping[str, tuple[Mapping[str, Any], ...]]
    changed_paths: tuple[str, ...] = ()
    observed_at: str | None = None


class RemoteCheckObservationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RemoteCheckObservation:
    """A current-head observation for one declared workflow job."""

    profile: str
    workflow: str
    job_name: str
    status: RemoteCheckObservationStatus
    head_sha: str | None
    observed_at: str | None
    run_id: int | None = None
    check_run_id: int | None = None
    conclusion: str | None = None
    reason: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "workflow": self.workflow,
            "job_name": self.job_name,
            "status": self.status.value,
            "head_sha": self.head_sha,
            "observed_at": self.observed_at,
            "run_id": self.run_id,
            "check_run_id": self.check_run_id,
            "conclusion": self.conclusion,
            "reason": self.reason,
        }


def observe_required_checks(
    snapshot: GitHubActionsSnapshot,
    expected_checks: Sequence[Mapping[str, str]],
) -> tuple[RemoteCheckObservation, ...]:
    """Match required checks to workflow jobs on the snapshot's current PR head."""
    pull_request_head = snapshot.pull_request.get("head")
    head_sha = (
        pull_request_head.get("sha") if isinstance(pull_request_head, Mapping) else None
    )
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = None

    observations: list[RemoteCheckObservation] = []
    for expected in expected_checks:
        profile = expected.get("profile", "")
        workflow = expected.get("workflow", "")
        job_name = expected.get("job_name", "")
        base = {
            "profile": profile,
            "workflow": workflow,
            "job_name": job_name,
            "head_sha": head_sha,
            "observed_at": snapshot.observed_at,
        }
        if not all(
            isinstance(value, str) and value.strip()
            for value in (profile, workflow, job_name)
        ):
            observations.append(
                RemoteCheckObservation(
                    **base,
                    status=RemoteCheckObservationStatus.UNKNOWN,
                    reason="required check identity is incomplete",
                )
            )
            continue
        if head_sha is None:
            observations.append(
                RemoteCheckObservation(
                    **base,
                    status=RemoteCheckObservationStatus.UNKNOWN,
                    reason="pull request head SHA is unavailable",
                )
            )
            continue

        matching_runs = [
            run
            for run in snapshot.runs
            if run.get("head_sha") == head_sha and run.get("path") == workflow
        ]
        latest_run = max(
            matching_runs,
            key=lambda run: (
                str(run.get("updated_at", run.get("created_at", ""))),
                int(run.get("id", 0)) if isinstance(run.get("id"), int) else 0,
            ),
            default=None,
        )
        run_id = latest_run.get("id") if latest_run else None
        jobs = snapshot.jobs_by_run.get(str(run_id), ()) if run_id is not None else ()
        matching_jobs = [job for job in jobs if job.get("name") == job_name]
        if len(matching_jobs) != 1:
            observations.append(
                RemoteCheckObservation(
                    **base,
                    status=RemoteCheckObservationStatus.UNKNOWN,
                    run_id=run_id if isinstance(run_id, int) else None,
                    reason=(
                        "no matching job was observed on the current head"
                        if not matching_jobs
                        else "multiple jobs matched the declared identity"
                    ),
                )
            )
            continue

        job = matching_jobs[0]
        raw_status = job.get("status")
        conclusion = job.get("conclusion")
        if raw_status != "completed":
            status = RemoteCheckObservationStatus.PENDING
            reason = f"remote job status is {raw_status or 'unknown'}"
        elif conclusion == "success":
            status = RemoteCheckObservationStatus.PASSED
            reason = None
        elif conclusion in {
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "startup_failure",
        }:
            status = RemoteCheckObservationStatus.FAILED
            reason = f"remote job concluded {conclusion}"
        else:
            status = RemoteCheckObservationStatus.UNKNOWN
            reason = (
                f"remote job conclusion {conclusion or 'is unavailable'} "
                "is not a success"
            )
        check_run_id = job.get("id")
        observations.append(
            RemoteCheckObservation(
                **base,
                status=status,
                run_id=run_id if isinstance(run_id, int) else None,
                check_run_id=check_run_id if isinstance(check_run_id, int) else None,
                conclusion=conclusion if isinstance(conclusion, str) else None,
                reason=reason,
            )
        )
    return tuple(observations)


@dataclass(frozen=True, slots=True)
class LocalCommandRun:
    """Receipt and bounded output from a command run at a pinned Git commit."""

    commit_sha: str
    command: tuple[str, ...]
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str


class GitHubActionsClient:
    """Read Actions state and append issue comments through the authenticated gh CLI."""

    def __init__(
        self,
        repository: str,
        *,
        executable: str = "gh",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not repository or repository.count("/") != 1:
            raise GitHubActionsError("repository must use owner/name form")
        self.repository = repository
        self.executable = executable
        self._run = run

    def observe_pull_request(self, pull_request_number: int) -> GitHubActionsSnapshot:
        if pull_request_number < 1:
            raise GitHubActionsError("pull request number must be positive")
        pr = self._object(
            f"repos/{self.repository}/pulls/{pull_request_number}", "pull request"
        )
        file_data = self._get(
            f"repos/{self.repository}/pulls/{pull_request_number}/files?per_page=100",
            paginated=True,
        )
        if not isinstance(file_data, list) or not all(
            isinstance(item, Mapping) and isinstance(item.get("filename"), str)
            for item in file_data
        ):
            raise GitHubActionsError("GitHub returned malformed pull request files")
        changed_paths = tuple(
            dict.fromkeys(
                item["filename"] for item in file_data if isinstance(item, Mapping)
            )
        )
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise GitHubActionsError(
                "GitHub did not return the current pull request head SHA"
            )
        run_data = self._get(
            f"repos/{self.repository}/actions/runs?head_sha={head_sha}&per_page=100",
            paginated=True,
            collection_key="workflow_runs",
        )
        raw_runs = run_data.get("workflow_runs")
        if not isinstance(raw_runs, list) or not all(
            isinstance(item, Mapping) for item in raw_runs
        ):
            raise GitHubActionsError("GitHub returned malformed workflow runs")
        runs = tuple(item for item in raw_runs if item.get("head_sha") == head_sha)
        jobs_by_run: dict[str, tuple[Mapping[str, Any], ...]] = {}
        annotations: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for workflow_run in runs:
            run_id = workflow_run.get("id")
            attempt = workflow_run.get("run_attempt", 1)
            if not isinstance(run_id, int) or not isinstance(attempt, int):
                continue
            endpoint = (
                f"repos/{self.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                "?per_page=100"
            )
            data = self._get(endpoint, paginated=True, collection_key="jobs")
            raw_jobs = data.get("jobs")
            if not isinstance(raw_jobs, list) or not all(
                isinstance(item, Mapping) for item in raw_jobs
            ):
                raise GitHubActionsError(
                    f"GitHub returned malformed jobs for workflow run {run_id}"
                )
            jobs_by_run[str(run_id)] = tuple(raw_jobs)
            for job in raw_jobs:
                check_url = job.get("check_run_url")
                if not isinstance(check_url, str):
                    continue
                check_run_id = check_url.rstrip("/").rsplit("/", 1)[-1]
                if not check_run_id.isdigit():
                    continue
                annotation_data = self._get(
                    f"repos/{self.repository}/check-runs/{check_run_id}/annotations?per_page=100",
                    paginated=True,
                )
                values = annotation_data if isinstance(annotation_data, list) else None
                if values is None or not all(
                    isinstance(item, Mapping) for item in values
                ):
                    raise GitHubActionsError(
                        "GitHub returned malformed annotations for check run "
                        f"{check_run_id}"
                    )
                annotations[check_run_id] = tuple(values)
        return GitHubActionsSnapshot(
            repository=self.repository,
            pull_request=pr,
            runs=runs,
            jobs_by_run=jobs_by_run,
            annotations_by_check_run=annotations,
            changed_paths=changed_paths,
            observed_at=datetime.now(UTC).isoformat(),
        )

    def get_issue(self, issue_number: int) -> Mapping[str, Any]:
        return self._object(f"repos/{self.repository}/issues/{issue_number}", "issue")

    def add_issue_comment(self, issue_number: int, body: str) -> None:
        if issue_number < 1 or not body.strip():
            raise GitHubActionsError("issue number and comment body must be non-empty")
        self._invoke(
            [
                "api",
                f"repos/{self.repository}/issues/{issue_number}/comments",
                "--method",
                "POST",
                "-f",
                f"body={body}",
            ]
        )

    def list_issue_comments(self, issue_number: int) -> tuple[Mapping[str, Any], ...]:
        value = self._get(
            f"repos/{self.repository}/issues/{issue_number}/comments"
            "?per_page=20&sort=created&direction=desc",
        )
        if not isinstance(value, list) or not all(
            isinstance(item, Mapping) for item in value
        ):
            raise GitHubActionsError("GitHub returned malformed issue comments")
        return tuple(value)

    def _object(self, endpoint: str, label: str) -> Mapping[str, Any]:
        data = self._get(endpoint)
        if not isinstance(data, Mapping):
            raise GitHubActionsError(f"GitHub returned malformed {label}")
        return data

    def _get(
        self,
        endpoint: str,
        *,
        paginated: bool = False,
        collection_key: str | None = None,
    ) -> Any:
        arguments = ["api"]
        if paginated:
            arguments.extend(("--paginate", "--slurp"))
        arguments.append(endpoint)
        result = self._invoke(arguments)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubActionsError("gh api returned malformed JSON") from error
        if not paginated:
            return value
        if not isinstance(value, list):
            raise GitHubActionsError("gh api pagination did not return page arrays")
        if collection_key is not None:
            if not all(
                isinstance(page, Mapping) and isinstance(page.get(collection_key), list)
                for page in value
            ):
                raise GitHubActionsError(
                    f"gh api pages are missing collection {collection_key!r}"
                )
            merged = dict(value[0]) if value else {collection_key: []}
            merged[collection_key] = [
                item for page in value for item in page[collection_key]
            ]
            return merged
        if not all(isinstance(page, list) for page in value):
            raise GitHubActionsError("gh api returned a non-array resource page")
        return [item for page in value for item in page]

    def _invoke(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = self._run(
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitHubActionsError(f"gh {' '.join(arguments[:2])} failed: {detail}")
        return result


def execute_pinned_local_command(
    repo_root: str | Path,
    commit_sha: str,
    command: Sequence[str],
    *,
    timeout_seconds: int = 600,
    output_limit_bytes: int = 32_000,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LocalCommandRun:
    """Run an explicit argv in a disposable detached worktree of an exact commit."""
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise GitHubActionsError("repository root does not exist")
    if len(commit_sha) not in (40, 64) or any(
        char not in "0123456789abcdef" for char in commit_sha
    ):
        raise GitHubActionsError("reproduction requires a full lowercase commit SHA")
    arguments = tuple(command)
    if not arguments or any(not item or "\0" in item for item in arguments):
        raise GitHubActionsError(
            "reproduction command must contain non-empty argv values"
        )
    if timeout_seconds < 1 or timeout_seconds > 3600:
        raise GitHubActionsError(
            "reproduction timeout must be between 1 and 3600 seconds"
        )
    available = run(
        ["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if available.returncode != 0:
        raise GitHubActionsError(
            f"candidate commit {commit_sha} is not available in the local repository"
        )
    with tempfile.TemporaryDirectory(prefix="powdrr-ci-reproduction-") as parent:
        worktree = Path(parent) / "checkout"
        added = run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                "--quiet",
                str(worktree),
                commit_sha,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode != 0:
            raise GitHubActionsError(
                "could not create pinned reproduction worktree: " + added.stderr.strip()
            )
        try:
            try:
                completed = run(
                    list(arguments),
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                exit_code = completed.returncode
                status = "completed"
            except subprocess.TimeoutExpired as error:
                stdout = _as_text(error.stdout)
                stderr = _as_text(error.stderr)
                exit_code = None
                status = "timed_out"
            return LocalCommandRun(
                commit_sha=commit_sha,
                command=arguments,
                status=status,
                exit_code=exit_code,
                stdout=_bounded_text(stdout, output_limit_bytes),
                stderr=_bounded_text(stderr, output_limit_bytes),
                stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            )
        finally:
            removed = run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if removed.returncode != 0:
                raise GitHubActionsError(
                    "could not remove temporary reproduction worktree: "
                    + removed.stderr.strip()
                )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


def _bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="replace") + "\n<output truncated>"


__all__ = [
    "GitHubActionsClient",
    "GitHubActionsError",
    "GitHubActionsSnapshot",
    "LocalCommandRun",
    "RemoteCheckObservation",
    "RemoteCheckObservationStatus",
    "execute_pinned_local_command",
    "observe_required_checks",
]
