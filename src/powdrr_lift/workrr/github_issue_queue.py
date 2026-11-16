"""GitHub Issues queue for execution items described by a merged plan document.

The plan document owns work meaning. GitHub Issues own mutable execution state
and contain only stable pointers back to the reviewed plan revision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import sleep, time
from typing import Any
from urllib.parse import quote

import yaml
from powdrr_local.file_lock import Timeout, file_lock
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.planning.checkpoints import has_complete_checkpoint_archive
from powdrr_service.planning.plan_document import (
    MANUAL_PREREQUISITE_TRIGGER,  # noqa: F401 - legacy public re-export
    QUEUE_SCHEMA,  # noqa: F401 - legacy public re-export
    PlanDocumentError,
)
from powdrr_service.planning.plan_document import (
    PLAN_REPAIR_MARKER as _PLAN_REPAIR_MARKER,
)
from powdrr_service.planning.plan_document import (
    parse_manual_prerequisite as _parse_manual_prerequisite,
)
from powdrr_service.planning.plan_document import (
    parse_reviewed_plan_yaml as _parse_reviewed_plan_yaml,
)
from powdrr_service.planning.plan_document import (
    render_plan_repair_metadata as render_plan_repair_metadata,
)
from powdrr_service.planning.plan_document import (
    serialize_reviewed_plan_yaml as serialize_reviewed_plan_yaml,
)
from powdrr_service.planning.plan_models import (
    PlannedWorkItem,
    ReviewedExecutionPlan,
    source_reference_design_file,
)
from powdrr_service.structrr.python_topology import is_validation_evidence_path
from powdrr_service.structrr.validation_models import ValidationCheck

QUEUE_LABELS = {
    "ready": "powdrr:ready",
    "claimed": "powdrr:claimed",
    "awaiting_review": "powdrr:awaiting-review",
    "awaiting_human": "powdrr:awaiting-human",
    "awaiting_ci": "powdrr:awaiting-ci",
    "blocked_on_dependency": "powdrr:blocked-on-dependency",
    "blocked": "powdrr:blocked",
}
FULFILLED_WITHOUT_PR_LABEL = "powdrr:fulfilled-without-pr"
PLAN_REPAIR_PENDING_LABEL = "powdrr:plan-repair-pending"
_MARKER_START = "<!-- powdrr-queue-item:v1 "
_MARKER_END = " -->"
_TASK_CHECKPOINT_START = "<!-- powdrr-task-checkpoint:v1 -->"
_CI_REPAIR_ARTIFACT_MARKER = "<!-- powdrr-ci-repair-artifact:v1 -->"
_CI_REPAIR_POINTER_START = "<!-- powdrr-ci-repair-artifact-pointer:start -->"
_CI_REPAIR_POINTER_END = "<!-- powdrr-ci-repair-artifact-pointer:end -->"
_CI_REPAIR_ARTIFACT_CHUNK_BYTES = 20 * 1024
_CI_REPAIR_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024
_CI_REPAIR_ARTIFACT_MAX_COMMENT_PAGES = 8
_CI_REPAIR_CONTRACT_ID_RE = re.compile(r"repair-[0-9a-f]{20}")
_PLAN_REPAIR_PENDING_MARKER = "<!-- powdrr-plan-repair-pending:v1 -->"
_PLAN_REPAIR_RECONCILED_MARKER = "<!-- powdrr-plan-repair-reconciled:v1 -->"
_PLAN_REPAIR_RESOLVED_MARKER = "<!-- powdrr-plan-repair-resolved:v1 -->"
_PLAN_REPAIR_SUPERSEDED_MARKER = "<!-- powdrr-plan-repair-superseded:v1 -->"
_PLAN_REPAIR_INVALIDATION_MARKER = "<!-- powdrr-plan-repair-invalidation:v1 -->"
_FULFILLMENT_MARKER = "<!-- powdrr-issue-fulfillment:v1 -->"
_FULFILLMENT_SCHEMA = "https://powdrr.io/schemas/github-issue-fulfillment-v1"
_PLAN_ADJUSTMENT_MARKER = "<!-- powdrr-plan-adjustment:v1 -->"
_PLAN_ADJUSTMENT_SCHEMA = "https://powdrr.io/schemas/github-issue-plan-adjustment-v1"
_PLAN_ADJUSTMENT_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}
_PLAN_ADJUSTABLE_FIELDS = {
    "title",
    "objective",
    "scope",
    "acceptance_criteria",
    "implementation_steps",
    "allowed_paths",
}
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
_GITHUB_STATUS_SETTLE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class QueueIssue:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    url: str


@dataclass(frozen=True, slots=True)
class PublishReport:
    plan_id: str
    plan_commit: str
    created_issue_numbers: tuple[int, ...]
    existing_issue_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PlanIssueMatch:
    item_id: str
    issue_number: int | None
    url: str | None
    state: str | None
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerReport:
    outcome: str
    issue_number: int | None = None
    item_id: str | None = None
    detail: str | None = None
    ci_outcome: str | None = None
    failure_record: str | None = None
    readiness: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    """Serializable terminal evidence for a failed planned item."""

    plan_id: str
    item_id: str
    workflow_id: str
    issue_number: int
    worker_id: str
    stage: str
    outcome: str
    detail: str
    runner_exit_code: int | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": "https://powdrr.io/schemas/task-execution-result-v1",
            "plan_id": self.plan_id,
            "item_id": self.item_id,
            "workflow_id": self.workflow_id,
            "issue_number": self.issue_number,
            "worker_id": self.worker_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "detail": self.detail,
            "runner_exit_code": self.runner_exit_code,
            "recorded_at": datetime.now(UTC).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    """Structured, append-only record of one completed workflow task."""

    plan_id: str
    item_id: str
    workflow_id: str
    task_id: str
    output_state_type: str
    output_state: Any
    commit_sha: str | None = None
    input_contract_digest: str | None = None


TaskCheckpointCallback = Callable[[str, str, Any, str | None], None]


class GitHubIssueQueueError(RuntimeError):
    """Invalid queue data or failed GitHub operation."""


class _WorkerCoordination:
    """Short-lived local coordination lock shared by repository worktrees."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._context: Any = None
        self.held = False

    def acquire(self, *, blocking: bool = True) -> None:
        if self.held:
            return
        self._context = file_lock(self.lock_path, blocking=blocking)
        self._context.__enter__()
        self.held = True

    def release(self) -> None:
        if self.held:
            self._context.__exit__(None, None, None)
            self._context = None
            self.held = False


class _ItemClaim:
    """Crash-released local claim with durable generation and heartbeat data."""

    def __init__(
        self,
        repo_root: Path,
        issue_number: int,
        worker_id: str,
        scopes: Sequence[str],
        max_concurrency: int,
    ) -> None:
        self.repo_root = repo_root
        self.issue_number = issue_number
        self.worker_id = worker_id
        normalized_scopes = tuple(
            sorted({posixpath.normpath(value.replace("\\", "/")) for value in scopes})
        )
        self.scopes = (
            ("__repository__",)
            if not normalized_scopes or "." in normalized_scopes
            else normalized_scopes
        )
        self.max_concurrency = max_concurrency
        self.directory = _worker_store_root(repo_root) / "claims"
        self.claim_path = self.directory / f"issue-{issue_number}.json"
        self.registry_path = self.directory / "scope-claims.json"
        self._stack = ExitStack()
        self.attempt_id = hashlib.sha256(os.urandom(32)).hexdigest()[:24]
        self.generation = 0
        self._closed = False
        self._entered = False
        self._registry_registered = False
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> _ItemClaim:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._stack.enter_context(
                file_lock(
                    self.directory / f"issue-{self.issue_number}.lock",
                    blocking=False,
                )
            )
            self._register_scopes()
            slot = next(
                (
                    index
                    for index in range(self.max_concurrency)
                    if self._try_slot(index)
                ),
                None,
            )
            if slot is None:
                raise Timeout("configured worker concurrency is full")
            previous = _read_json_mapping(self.claim_path)
            if previous.get("state") == "running":
                raise GitHubIssueQueueError(
                    f"issue #{self.issue_number} has an abandoned running claim; "
                    "reconcile it before retrying"
                )
            self.generation = int(previous.get("generation", 0)) + 1
            recovery_state = (
                "reconciled"
                if previous.get("state") == "reconciliation_required"
                else "fresh"
            )
            self._write("running", recovery_state)
            self._entered = True
            self._heartbeat_thread = threading.Thread(
                target=_claim_heartbeat_loop,
                args=(
                    self.claim_path,
                    self.issue_number,
                    self.worker_id,
                    self.attempt_id,
                    self.generation,
                    self.scopes,
                    self._stop_heartbeat,
                ),
                name=f"powdrr-claim-heartbeat-{self.issue_number}",
                daemon=True,
            )
            self._heartbeat_thread.start()
            return self
        except BaseException:
            if self._registry_registered:
                self._remove_scopes()
            self._stack.close()
            raise

    def _register_scopes(self) -> None:
        with file_lock(self.directory / "scope-claims.lock"):
            registry = _read_json_mapping(self.registry_path)
            claims_value = registry.get("claims", [])
            if not isinstance(claims_value, list):
                raise GitHubIssueQueueError("scope claim registry is malformed")
            active: list[Mapping[str, Any]] = []
            for entry in claims_value:
                if not isinstance(entry, Mapping):
                    continue
                try:
                    raw_other_issue = entry.get("issue_number")
                    if not isinstance(raw_other_issue, (int, str)):
                        continue
                    other_issue = int(raw_other_issue)
                    if other_issue == self.issue_number:
                        # This worker owns the issue lock, so an earlier
                        # registry entry for this issue is abandoned.
                        continue
                    with file_lock(
                        self.directory / f"issue-{other_issue}.lock",
                        blocking=False,
                    ):
                        continue
                except (Timeout, TypeError, ValueError):
                    active.append(entry)
            for entry in active:
                other_scopes = entry.get("scopes", [])
                if isinstance(other_scopes, list) and _write_scopes_overlap(
                    self.scopes, tuple(str(scope) for scope in other_scopes)
                ):
                    raise Timeout("an active worker owns an overlapping write scope")
            active.append(
                {
                    "issue_number": self.issue_number,
                    "owner": self.worker_id,
                    "attempt_id": self.attempt_id,
                    "generation": self.generation,
                    "scopes": list(self.scopes),
                }
            )
            _atomic_write_json(self.registry_path, {"claims": active})
            self._registry_registered = True

    def _remove_scopes(self) -> None:
        with file_lock(self.directory / "scope-claims.lock"):
            registry = _read_json_mapping(self.registry_path)
            claims_value = registry.get("claims", [])
            if isinstance(claims_value, list):
                remaining = [
                    entry
                    for entry in claims_value
                    if not (
                        isinstance(entry, Mapping)
                        and entry.get("issue_number") == self.issue_number
                        and entry.get("attempt_id") == self.attempt_id
                    )
                ]
                _atomic_write_json(self.registry_path, {"claims": remaining})
        self._registry_registered = False

    def _try_slot(self, index: int) -> bool:
        try:
            self._stack.enter_context(
                file_lock(self.directory / f"worker-slot-{index}.lock", blocking=False)
            )
            return True
        except Timeout:
            return False

    def __exit__(self, *_exc: object) -> None:
        if self._closed:
            return
        if not self._entered:
            self._stack.close()
            self._closed = True
            return
        self._stop_heartbeat.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
        if self._registry_registered:
            self._remove_scopes()
        self._write("released", "completed")
        self._stack.close()
        self._closed = True

    def __del__(self) -> None:
        try:
            self.__exit__()
        except Exception:
            self._stack.close()

    def _write(self, state: str, recovery_state: str) -> None:
        document = {
            "schema": "powdrr-issue-claim-v1",
            "issue_number": self.issue_number,
            "owner": self.worker_id,
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "state": state,
            "recovery_state": recovery_state,
            "scopes": list(self.scopes),
        }
        _atomic_write_json(self.claim_path, document)

    def assert_current(self) -> None:
        record = _read_json_mapping(self.claim_path)
        if (
            record.get("owner") != self.worker_id
            or record.get("attempt_id") != self.attempt_id
            or record.get("generation") != self.generation
            or record.get("state") != "running"
        ):
            raise GitHubIssueQueueError(
                f"worker lost claim generation {self.generation} "
                f"for issue #{self.issue_number}"
            )
        self._write("running", "active")


def _worker_store_root(repo_root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return repo_root / ".powdrr" / "github-issue-worker"
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    return common_dir.resolve() / "powdrr-github-issue-worker"


def _mark_abandoned_claims_for_reconciliation(
    client: GitHubIssueClient, repo_root: Path
) -> None:
    """Fence crashed owners; a new attempt needs explicit state reconciliation."""
    directory = _worker_store_root(repo_root) / "claims"
    for issue in client.list_issues(labels=(QUEUE_LABELS["claimed"],), state="open"):
        claim_path = directory / f"issue-{issue.number}.json"
        record = _read_json_mapping(claim_path)
        if record.get("state") != "running":
            continue
        try:
            with file_lock(directory / f"issue-{issue.number}.lock", blocking=False):
                # The OS lock, not heartbeat age, distinguishes an exited owner
                # from a slow but live model invocation.
                record = dict(_read_json_mapping(claim_path))
                if record.get("state") != "running":
                    continue
                record["state"] = "reconciliation_required"
                record["recovery_state"] = "owner_exited"
                record["heartbeat_at"] = datetime.now(UTC).isoformat()
                _atomic_write_json(claim_path, record)
        except Timeout:
            continue
        refreshed = client.get_issue(issue.number)
        client.update_issue(
            refreshed,
            labels=_replace_queue_status(refreshed.labels, "blocked"),
        )
        client.add_comment(
            refreshed,
            "Powdrr found that the previous worker exited while holding this item. "
            "The item was fenced to prevent duplicate commits or publication. "
            "Reconcile its durable checkpoint, worktree, and any pull request, then "
            "move it back to `powdrr:ready` to resume.",
        )


def _read_json_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _claim_heartbeat_loop(
    path: Path,
    issue_number: int,
    owner: str,
    attempt_id: str,
    generation: int,
    scopes: tuple[str, ...],
    stop: threading.Event,
) -> None:
    while not stop.wait(15):
        _atomic_write_json(
            path,
            {
                "schema": "powdrr-issue-claim-v1",
                "issue_number": issue_number,
                "owner": owner,
                "attempt_id": attempt_id,
                "generation": generation,
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "state": "running",
                "recovery_state": "heartbeat",
                "scopes": list(scopes),
            },
        )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_scopes_overlap(first: Sequence[str], second: Sequence[str]) -> bool:
    if "__repository__" in first or "__repository__" in second:
        return True
    for left in first:
        normalized_left = posixpath.normpath(left.replace("\\", "/"))
        for right in second:
            normalized_right = posixpath.normpath(right.replace("\\", "/"))
            if (
                normalized_left == normalized_right
                or normalized_left.startswith(normalized_right.rstrip("/") + "/")
                or normalized_right.startswith(normalized_left.rstrip("/") + "/")
            ):
                return True
    return False


class GitHubIssueClient:
    """Small ``gh api`` adapter for issue status and plan pointers."""

    def __init__(
        self,
        repository: str,
        *,
        executable: str = "gh",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep_fn: Callable[[float], None] = sleep,
        api_retries: int = 6,
    ) -> None:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            raise GitHubIssueQueueError("repository must use owner/name form")
        self.repository = repository
        self.executable = executable
        self._run = run
        self._sleep = sleep_fn
        self._api_retries = max(0, api_retries)

    def list_issues(
        self, *, labels: Sequence[str] = (), state: str = "all"
    ) -> tuple[QueueIssue, ...]:
        if state not in {"open", "closed", "all"}:
            raise GitHubIssueQueueError(
                "issue state filter must be open, closed, or all"
            )
        # GitHub's issue list can omit recently relabeled open issues when the
        # status label and state=open filters are combined. Query by the sparse
        # Powdrr label across all states, then apply the requested state locally.
        endpoint_state = "all" if labels and state != "all" else state
        endpoint = f"repos/{self.repository}/issues?state={endpoint_state}&per_page=100"
        if labels:
            endpoint += "&labels=" + quote(",".join(labels), safe=",")
        result = self._invoke(
            [
                "api",
                "-H",
                "Cache-Control: no-cache",
                "--paginate",
                endpoint,
            ]
        )
        issue_values = _parse_paginated_json_arrays(result.stdout, description="issue")
        issues: list[QueueIssue] = []
        for value in issue_values:
            if not isinstance(value, Mapping):
                continue
            if "pull_request" in value:
                continue
            raw_labels = value.get("labels", [])
            labels = (
                tuple(
                    str(label["name"])
                    for label in raw_labels
                    if isinstance(label, Mapping) and isinstance(label.get("name"), str)
                )
                if isinstance(raw_labels, list)
                else ()
            )
            issue = QueueIssue(
                number=int(value["number"]),
                title=str(value.get("title", "")),
                body=str(value.get("body", "")),
                state=str(value.get("state", "OPEN")).lower(),
                labels=labels,
                url=str(value.get("url", "")),
            )
            if state != "all" and issue.state != state:
                continue
            issues.append(issue)
        return tuple(issues)

    def create_issue(
        self, *, body: str, labels: Sequence[str], title: str
    ) -> QueueIssue:
        arguments = [
            "POST",
            f"repos/{self.repository}/issues",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        ]
        for label in labels:
            arguments.extend(("-f", f"labels[]={label}"))
        result = self._api(*arguments)
        return _issue_from_api(result.stdout)

    def ensure_plan_label(self, plan_id: str) -> str:
        label = _plan_label(plan_id)
        self._ensure_label(
            label,
            color="5319e7",
            description=f"Powdrr plan index for {plan_id}",
        )
        return label

    def ensure_queue_labels(self) -> None:
        for status, label in QUEUE_LABELS.items():
            self._ensure_label(
                label,
                color=_label_color(status),
                description=f"Powdrr queue state: {status.replace('_', ' ')}",
            )
        self._ensure_label(
            PLAN_REPAIR_PENDING_LABEL,
            color="B60205",
            description="Powdrr work item is impacted by a pending plan repair",
        )

    def ensure_planning_label(self) -> None:
        """Ensure the session tracker label, separate from executable queue state."""
        self._ensure_label(
            "powdrr:planning",
            color="1D76DB",
            description="Powdrr planning session tracker; not executable work",
        )

    def ensure_fulfilled_without_pr_label(self) -> None:
        self._ensure_label(
            FULFILLED_WITHOUT_PR_LABEL,
            color="0E8A16",
            description="Powdrr work completed without an implementation pull request",
        )

    def get_issue(self, issue_number: int) -> QueueIssue:
        result = self._api("GET", f"repos/{self.repository}/issues/{issue_number}")
        return _issue_from_api(result.stdout)

    def _ensure_label(self, label: str, *, color: str, description: str) -> None:
        endpoint = f"repos/{self.repository}/labels/{quote(label, safe='')}"
        existing = self._invoke(["api", endpoint], check=False)
        if existing.returncode == 0:
            return
        if "HTTP 404" not in existing.stderr:
            raise GitHubIssueQueueError(
                f"could not check GitHub label {label!r}: {existing.stderr.strip()}"
            )

        created = self._invoke(
            [
                "api",
                f"repos/{self.repository}/labels",
                "--method",
                "POST",
                "-f",
                f"name={label}",
                "-f",
                f"color={color}",
                "-f",
                f"description={description}",
            ],
            check=False,
        )
        if created.returncode != 0:
            raise GitHubIssueQueueError(
                f"could not create GitHub label {label!r}: {created.stderr.strip()}"
            )

    def ensure_plan_commit_merged(self, plan_commit: str) -> None:
        repository = self._api("GET", f"repos/{self.repository}")
        repository_data = _json_mapping(repository.stdout, "repository")
        default_branch = repository_data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubIssueQueueError("GitHub did not return the default branch name")
        comparison = self._api(
            "GET",
            "repos/"
            f"{self.repository}/compare/{plan_commit}..."
            f"{quote(default_branch, safe='')}",
        )
        comparison_data = _json_mapping(comparison.stdout, "commit comparison")
        behind_by = comparison_data.get("behind_by")
        if not isinstance(behind_by, int) or behind_by != 0:
            raise GitHubIssueQueueError(
                "plan commit is not reachable from the repository default branch; "
                "publish only after the plan review PR merges"
            )

    def update_issue(
        self,
        issue: QueueIssue,
        *,
        body: str | None = None,
        labels: Sequence[str] | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> None:
        arguments = ["PATCH", f"repos/{self.repository}/issues/{issue.number}"]
        if body is not None:
            arguments.extend(("-f", f"body={body}"))
        if labels is not None:
            for label in labels:
                arguments.extend(("-f", f"labels[]={label}"))
        if state is not None:
            arguments.extend(("-f", f"state={state}"))
        if state_reason is not None:
            arguments.extend(("-f", f"state_reason={state_reason}"))
        self._api(*arguments)

    def add_comment(self, issue: QueueIssue, body: str) -> None:
        self.add_issue_comment(issue.number, body)

    def add_issue_comment(self, issue_number: int, body: str) -> None:
        arguments = [
            "api",
            "--include",
            f"repos/{self.repository}/issues/{issue_number}/comments",
            "--method",
            "POST",
            "-f",
            f"body={body}",
        ]
        self._invoke_comment_api(
            arguments,
            operation=(f"POST repos/{self.repository}/issues/{issue_number}/comments"),
        )

    def list_comments(self, issue_number: int) -> tuple[Mapping[str, Any], ...]:
        result = self._invoke_comment_api(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repository}/issues/{issue_number}/comments?per_page=100",
            ],
            operation=(f"GET repos/{self.repository}/issues/{issue_number}/comments"),
        )
        try:
            pages = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError(
                "gh returned malformed issue comments"
            ) from error
        return tuple(
            value for value in _flatten_pages(pages) if isinstance(value, Mapping)
        )

    def list_checkpoint_comments(
        self, issue_number: int, *, session_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """Read issue comments backward until a complete archive is found."""
        operation = f"GET repos/{self.repository}/issues/{issue_number}/comments"
        issue_result = self._invoke_comment_api(
            ["api", f"repos/{self.repository}/issues/{issue_number}"],
            operation=f"GET repos/{self.repository}/issues/{issue_number}",
        )
        try:
            issue = json.loads(issue_result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError(
                "gh returned malformed issue metadata"
            ) from error
        count = issue.get("comments") if isinstance(issue, Mapping) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GitHubIssueQueueError(
                "gh returned issue metadata without a valid comment count"
            )
        if count == 0:
            return ()

        pages: list[list[Mapping[str, Any]]] = []
        last_page = (count + 99) // 100
        for page in range(last_page, 0, -1):
            result = self._invoke_comment_api(
                [
                    "api",
                    f"repos/{self.repository}/issues/{issue_number}/comments"
                    f"?per_page=100&page={page}",
                ],
                operation=operation,
            )
            try:
                values = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise GitHubIssueQueueError(
                    "gh returned malformed issue comments"
                ) from error
            if not isinstance(values, list):
                raise GitHubIssueQueueError(
                    "gh returned a non-array issue-comment page"
                )
            pages.append([value for value in values if isinstance(value, Mapping)])
            comments = [comment for page_values in pages for comment in page_values]
            if has_complete_checkpoint_archive(comments, session_id=session_id):
                return tuple(
                    comment
                    for page_values in reversed(pages)
                    for comment in page_values
                )
        return tuple(
            comment for page_values in reversed(pages) for comment in page_values
        )

    def list_ci_repair_artifact_comments(
        self, issue_number: int, *, contract_id: str, bundle_digest: str
    ) -> tuple[Mapping[str, Any], ...]:
        """Read bounded recent comments until the pointed repair bundle is complete."""
        operation = f"GET repos/{self.repository}/issues/{issue_number}/comments"
        issue_result = self._invoke_comment_api(
            ["api", f"repos/{self.repository}/issues/{issue_number}"],
            operation=f"GET repos/{self.repository}/issues/{issue_number}",
        )
        try:
            issue = json.loads(issue_result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError(
                "gh returned malformed issue metadata"
            ) from error
        count = issue.get("comments") if isinstance(issue, Mapping) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GitHubIssueQueueError(
                "gh returned issue metadata without a valid comment count"
            )
        if count == 0:
            return ()
        pages: list[list[Mapping[str, Any]]] = []
        first_page = (count + 99) // 100
        last_page = max(1, first_page - _CI_REPAIR_ARTIFACT_MAX_COMMENT_PAGES + 1)
        for page in range(first_page, last_page - 1, -1):
            result = self._invoke_comment_api(
                [
                    "api",
                    f"repos/{self.repository}/issues/{issue_number}/comments"
                    f"?per_page=100&page={page}",
                ],
                operation=operation,
            )
            try:
                values = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise GitHubIssueQueueError(
                    "gh returned malformed issue comments"
                ) from error
            if not isinstance(values, list):
                raise GitHubIssueQueueError(
                    "gh returned a non-array issue-comment page"
                )
            pages.append([value for value in values if isinstance(value, Mapping)])
            comments = [comment for page_values in pages for comment in page_values]
            if _has_complete_ci_repair_artifact(
                comments, contract_id, bundle_digest=bundle_digest
            ):
                return tuple(
                    comment
                    for page_values in reversed(pages)
                    for comment in page_values
                )
        raise GitHubIssueQueueError(
            "pointed CI repair artifact is incomplete or outside the recent "
            "checkpoint window"
        )

    def _invoke_comment_api(
        self, arguments: Sequence[str], *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        for attempt in range(self._api_retries + 1):
            result = self._invoke(arguments, check=False)
            if result.returncode == 0:
                return result
            rate_limited = _is_github_rate_limit_response(result)
            transient_error = _is_github_transient_api_error(result)
            if not rate_limited and not transient_error:
                raise GitHubIssueQueueError(
                    f"GitHub API {operation} failed: {result.stderr.strip()}"
                )
            if attempt >= self._api_retries:
                raise GitHubIssueQueueError(
                    f"GitHub API {operation} remained unsuccessful after "
                    f"{attempt} retries: {result.stderr.strip()}"
                )
            delay = (
                _github_retry_delay(result, attempt)
                if rate_limited
                else _github_server_retry_delay(attempt)
            )
            self._sleep(delay)
        raise GitHubIssueQueueError(
            f"GitHub API {operation} failed after the retry limit"
        )

    def pull_request(self, number: int) -> Mapping[str, Any]:
        result = self._api("GET", f"repos/{self.repository}/pulls/{number}")
        return _json_mapping(result.stdout, "pull request")

    def _api(
        self, method: str, endpoint: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        result = self._invoke(
            ["api", endpoint, "--method", method, *arguments], check=False
        )
        if result.returncode != 0:
            raise GitHubIssueQueueError(
                f"GitHub API {method} {endpoint} failed: {result.stderr.strip()}"
            )
        return result

    def _invoke(
        self, arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitHubIssueQueueError(
                f"GitHub command failed: {result.stderr.strip()}"
            )
        return result


def _is_github_rate_limit_response(
    result: subprocess.CompletedProcess[str],
) -> bool:
    response = f"{result.stdout}\n{result.stderr}".lower()
    if "http 429" in response or "status 429" in response:
        return True
    return ("http 403" in response or "status 403" in response) and any(
        marker in response
        for marker in (
            "rate limit",
            "secondary rate",
            "abuse detection",
            "temporarily blocked from content creation",
        )
    )


def _is_github_transient_api_error(
    result: subprocess.CompletedProcess[str],
) -> bool:
    response = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in response
        for marker in (
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "http/2.0 500",
            "http/2.0 502",
            "http/2.0 503",
            "http/2.0 504",
            "status 500",
            "status 502",
            "status 503",
            "status 504",
            "connection reset by peer",
            "connection timed out",
            "operation timed out",
            "i/o timeout",
            "tls handshake timeout",
            "unexpected eof",
            "broken pipe",
        )
    )


def _github_server_retry_delay(attempt: int) -> float:
    return float(min(2**attempt, 30))


def _github_retry_delay(
    result: subprocess.CompletedProcess[str], attempt: int
) -> float:
    response = f"{result.stdout}\n{result.stderr}"
    retry_after = re.search(r"retry-after\s*[:=]\s*(\d+)", response, re.IGNORECASE)
    if retry_after is not None:
        return float(retry_after.group(1))
    reset = re.search(r"x-ratelimit-reset\s*:\s*(\d+)", response, re.IGNORECASE)
    if reset is not None:
        return max(0.0, float(reset.group(1)) - time())
    return float(min(60 * (2**attempt), 900))


def load_reviewed_plan(
    plan_path: str | Path, *, repo_root: str | Path
) -> ReviewedExecutionPlan:
    """Load and validate a plan document from the current checkout."""
    path = Path(plan_path)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(Path(repo_root).resolve())
        except ValueError as error:
            raise GitHubIssueQueueError(
                "plan path must be inside the repository"
            ) from error
    return parse_reviewed_plan_yaml(path.read_text(encoding="utf-8"))


def parse_reviewed_plan_yaml(text: str) -> ReviewedExecutionPlan:
    try:
        return _parse_reviewed_plan_yaml(text)
    except PlanDocumentError as error:
        raise GitHubIssueQueueError(str(error)) from error


def parse_manual_prerequisite(value: Any, field: str) -> dict[str, Any]:
    try:
        return _parse_manual_prerequisite(value, field)
    except PlanDocumentError as error:
        raise GitHubIssueQueueError(str(error)) from error


_parse_plan = parse_reviewed_plan_yaml


def load_reviewed_plan_at_commit(
    plan_path: str,
    plan_commit: str,
    *,
    repo_root: str | Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ReviewedExecutionPlan:
    """Load the immutable plan version named by a Git commit."""
    _validate_plan_reference(plan_path, plan_commit)
    result = run(
        [
            "git",
            "-C",
            str(Path(repo_root).resolve()),
            "show",
            f"{plan_commit}:{plan_path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubIssueQueueError(
            f"could not read {plan_path!r} at {plan_commit}: {result.stderr.strip()}"
        )
    return parse_reviewed_plan_yaml(result.stdout)


def source_design_items_requiring_replan_at_commit(
    plan: ReviewedExecutionPlan,
    plan_commit: str,
    *,
    repo_root: str | Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> frozenset[str]:
    """Find reviewed plan items whose pinned source design cannot execute yet."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", plan_commit):
        raise GitHubIssueQueueError("plan commit must be an exact Git object ID")
    root = Path(repo_root).resolve()
    blocked: set[str] = set()
    for item in plan.items:
        if item.source_reference_design_file is None:
            continue
        path = source_reference_design_file(item.source_reference_design_file)
        result = run(
            ["git", "-C", str(root), "show", f"{plan_commit}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitHubIssueQueueError(
                f"could not read source design {path!r} at {plan_commit}: "
                f"{result.stderr.strip()}"
            )
        try:
            document = yaml.safe_load(result.stdout)
        except yaml.YAMLError as error:
            raise GitHubIssueQueueError(
                f"source design {path!r} is not valid YAML: {error}"
            ) from error
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != "structrr-source-reference-task-v1"
            or document.get("plan_id") != plan.plan_id
            or document.get("item_id") != item.item_id
            or not isinstance(document.get("design"), Mapping)
        ):
            raise GitHubIssueQueueError(
                f"source design {path!r} does not match plan item {item.item_id!r}"
            )
        design = document["design"]
        if (
            document.get("status") == "requires_replan"
            or design.get("compilation_status") == "requires_replan"
            or not isinstance(document.get("preparation"), Mapping)
        ):
            blocked.add(item.item_id)
    return frozenset(blocked)


def publish_plan_issues(
    *,
    plan: ReviewedExecutionPlan,
    plan_path: str,
    plan_commit: str,
    client: GitHubIssueClient,
    blocked_item_ids: frozenset[str] = frozenset(),
) -> PublishReport:
    """Create one minimal pointer issue per plan item; reruns reuse issue IDs."""
    _validate_plan_reference(plan_path, plan_commit)
    unknown_blocked_ids = blocked_item_ids - {item.item_id for item in plan.items}
    if unknown_blocked_ids:
        raise GitHubIssueQueueError(
            "blocked item IDs are not present in the reviewed plan: "
            + ", ".join(sorted(unknown_blocked_ids))
        )
    client.ensure_plan_commit_merged(plan_commit)
    client.ensure_queue_labels()
    plan_label = client.ensure_plan_label(plan.plan_id)
    existing = _plan_issue_index(client.list_issues(labels=(plan_label,)))
    created: list[int] = []
    reused: list[int] = []
    for item in plan.items:
        key = (plan.plan_id, item.item_id)
        issue = existing.get(key)
        body = _issue_pointer_body(
            plan.plan_id,
            item.item_id,
            plan_path,
            plan_commit,
            repository=client.repository,
        )
        if issue is not None:
            pointer = parse_issue_pointer(issue.body)
            expected_pointer = {
                "plan_id": plan.plan_id,
                "item_id": item.item_id,
                "plan_path": plan_path,
                "plan_commit": plan_commit,
            }
            if pointer != expected_pointer:
                raise GitHubIssueQueueError(
                    f"issue #{issue.number} already represents "
                    f"{plan.plan_id}/{item.item_id} "
                    "from a different plan revision; use a new plan or item ID"
                )
            reused.append(issue.number)
            continue
        status = (
            "blocked"
            if item.item_id in blocked_item_ids
            else "ready"
            if not item.dependencies
            else "blocked_on_dependency"
        )
        created_issue = client.create_issue(
            title=f"Powdrr work item: {plan.plan_id}/{item.item_id}",
            body=body,
            labels=(QUEUE_LABELS[status], plan_label),
        )
        created.append(created_issue.number)
        existing[key] = created_issue
    by_number = {issue.number: issue for issue in existing.values()}
    for item in plan.items:
        issue = existing[(plan.plan_id, item.item_id)]
        if issue.state != "open":
            continue
        current_statuses = {
            QUEUE_LABELS["ready"],
            QUEUE_LABELS["blocked_on_dependency"],
        }
        if not current_statuses.intersection(issue.labels):
            continue
        dependencies_closed = all(
            _dependency_is_fulfilled(
                existing.get((plan.plan_id, dependency)),
                by_number,
                client=client,
                plan=plan,
                dependency_id=dependency,
            )
            for dependency in item.dependencies
        )
        status = (
            "blocked"
            if item.item_id in blocked_item_ids
            else "ready"
            if dependencies_closed
            else "blocked_on_dependency"
        )
        labels = _replace_queue_status(issue.labels, status)
        if labels != issue.labels:
            client.update_issue(issue, labels=labels)
            existing[(plan.plan_id, item.item_id)] = QueueIssue(
                issue.number,
                issue.title,
                issue.body,
                issue.state,
                labels,
                issue.url,
            )
    return PublishReport(plan.plan_id, plan_commit, tuple(created), tuple(reused))


def find_plan_issues(
    *,
    plan: ReviewedExecutionPlan,
    plan_path: str,
    plan_commit: str,
    client: GitHubIssueClient,
) -> tuple[PlanIssueMatch, ...]:
    """Resolve every plan item to its GitHub issue without mutating GitHub."""
    _validate_plan_reference(plan_path, plan_commit)
    issues_by_item = _plan_issue_index(
        client.list_issues(labels=(_plan_label(plan.plan_id),))
    )
    matches: list[PlanIssueMatch] = []
    for item in plan.items:
        issue = issues_by_item.get((plan.plan_id, item.item_id))
        if issue is None:
            matches.append(PlanIssueMatch(item.item_id, None, None, None, ()))
            continue
        expected_pointer = {
            "plan_id": plan.plan_id,
            "item_id": item.item_id,
            "plan_path": plan_path,
            "plan_commit": plan_commit,
        }
        if parse_issue_pointer(issue.body) != expected_pointer:
            raise GitHubIssueQueueError(
                f"issue #{issue.number} for {plan.plan_id}/{item.item_id} "
                "points to a different plan revision"
            )
        matches.append(
            PlanIssueMatch(
                item.item_id,
                issue.number,
                issue.url,
                issue.state,
                issue.labels,
            )
        )
    return tuple(matches)


def run_worker_once(
    *,
    client: GitHubIssueClient,
    repo_root: str | Path,
    worker_id: str,
    run_task: Callable[
        [
            Path,
            str,
            str,
            PlannedWorkItem,
            ReviewedExecutionPlan,
            tuple[str, ...],
            TaskCheckpointCallback,
        ],
        int,
    ],
    plan_loader: Callable[..., ReviewedExecutionPlan] = load_reviewed_plan_at_commit,
    max_concurrency: int = 4,
    multi_host: bool = False,
) -> WorkerReport:
    """Claim and execute the first ready item whose dependencies are closed."""
    root = Path(repo_root).resolve()
    if max_concurrency < 1:
        raise GitHubIssueQueueError("max_concurrency must be at least 1")
    if multi_host:
        raise GitHubIssueQueueError(
            "multi-host issue workers are unsupported by the local Git common-dir "
            "claim store; configure a shared atomic claim store before enabling them"
        )
    with _worker_lock(root) as coordination:
        _requeue_merged_plan_amendments(
            client=client,
            repo_root=root,
            plan_loader=plan_loader,
        )
        _mark_abandoned_claims_for_reconciliation(client, root)
        _promote_unblocked_issues(
            client=client,
            repo_root=root,
            plan_loader=plan_loader,
        )
        issues = client.list_issues(labels=(QUEUE_LABELS["ready"],), state="open")
        plan_issue_indexes: dict[str, dict[tuple[str, str], QueueIssue]] = {}
        for issue in sorted(issues, key=lambda value: value.number):
            if QUEUE_LABELS["ready"] not in issue.labels or issue.state != "open":
                continue
            pointer = parse_issue_pointer(issue.body)
            if pointer is None:
                continue
            plan = plan_loader(
                pointer["plan_path"], pointer["plan_commit"], repo_root=root
            )
            item = next(
                (
                    candidate
                    for candidate in plan.items
                    if candidate.item_id == pointer["item_id"]
                ),
                None,
            )
            if item is None or plan.plan_id != pointer["plan_id"]:
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(issue.labels, "blocked"),
                )
                client.add_comment(
                    issue,
                    "Powdrr could not find this item in its pinned plan revision. "
                    "Please repair the plan pointer before retrying.",
                )
                return WorkerReport("blocked", issue.number, pointer["item_id"])
            indexed = plan_issue_indexes.get(plan.plan_id)
            if indexed is None:
                indexed = _plan_issue_index(
                    client.list_issues(labels=(_plan_label(plan.plan_id),))
                )
                plan_issue_indexes[plan.plan_id] = indexed
            by_number = {value.number: value for value in indexed.values()}
            unmet = [
                dependency
                for dependency in item.dependencies
                if not _dependency_is_fulfilled(
                    indexed.get((plan.plan_id, dependency)),
                    by_number,
                    client=client,
                    plan=plan,
                    dependency_id=dependency,
                )
            ]
            if unmet:
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(issue.labels, "blocked_on_dependency"),
                )
                continue
            issue = next(
                value
                for value in client.list_issues(
                    labels=(QUEUE_LABELS["ready"],), state="open"
                )
                if value.number == issue.number
            )
            if QUEUE_LABELS["ready"] not in issue.labels:
                continue
            issue = client.get_issue(issue.number)
            detail: str | None = None
            planned_item = item
            try:
                item = _apply_issue_plan_adjustments(
                    item=item,
                    plan_id=plan.plan_id,
                    comments=client.list_comments(issue.number),
                )
                if item != planned_item and item.workflow_file is None:
                    raise GitHubIssueQueueError(
                        "inline adjustments require a workflow instance file"
                    )
            except GitHubIssueQueueError as error:
                detail = f"inline plan adjustment could not be applied: {error}"
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(
                        _replace_queue_status(issue.labels, "claimed"), "blocked"
                    ),
                )
                client.add_comment(issue, f"Powdrr worker blocked this item: {detail}.")
                return WorkerReport("blocked", issue.number, item.item_id, detail)
            workflow_reference = item.workflow_file or item.workflow_directory
            assert workflow_reference is not None
            workflow_path = (root / workflow_reference).resolve()
            expected_kind_exists = (
                workflow_path.is_file()
                if item.workflow_file is not None
                else workflow_path.is_dir()
            )
            if not workflow_path.is_relative_to(root) or not expected_kind_exists:
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(
                        _replace_queue_status(issue.labels, "claimed"), "blocked"
                    ),
                )
                detail = f"workflow definition is missing: {workflow_reference}"
                client.add_comment(issue, f"Powdrr worker blocked this item: {detail}.")
                return WorkerReport("blocked", issue.number, item.item_id, detail)
            if item.workflow_file is not None:
                from powdrr_lift.workrr.github_issue_plan_generation import (
                    PlanGenerationError,
                    load_plan_workflow_instance,
                )

                try:
                    workflow_instance = load_plan_workflow_instance(workflow_path)
                    workflow_ids = {
                        candidate.item_id: candidate.workflow_id
                        for candidate in plan.items
                    }
                    expected_dependencies = tuple(
                        workflow_ids[dependency] for dependency in item.dependencies
                    )
                    if (
                        workflow_instance.workflow_id != item.workflow_id
                        or workflow_instance.dependencies != expected_dependencies
                    ):
                        detail = (
                            "workflow dependencies do not match the pinned plan; "
                            f"expected {expected_dependencies!r}, found "
                            f"{workflow_instance.dependencies!r}"
                        )
                    else:
                        context = workflow_instance.inputs.get("proposed_pr_context")
                        actual_prerequisite = (
                            context.get("manual_prerequisite")
                            if isinstance(context, Mapping)
                            else None
                        )
                        expected_prerequisite = (
                            dict(item.manual_prerequisite)
                            if item.manual_prerequisite is not None
                            else None
                        )
                        detail = (
                            "workflow manual prerequisite does not match the pinned "
                            "plan item"
                            if actual_prerequisite != expected_prerequisite
                            else None
                        )
                        if (
                            isinstance(context, Mapping)
                            and context.get("source_reference_design_file")
                            != item.source_reference_design_file
                        ):
                            detail = (
                                "workflow source reference design does not match "
                                "the pinned plan item"
                            )
                except (PlanGenerationError, KeyError) as error:
                    detail = f"invalid workflow definition: {error}"
                if detail is not None:
                    client.update_issue(
                        issue,
                        labels=_replace_queue_status(
                            _replace_queue_status(issue.labels, "claimed"), "blocked"
                        ),
                    )
                    client.add_comment(
                        issue, f"Powdrr worker blocked this item: {detail}."
                    )
                    return WorkerReport("blocked", issue.number, item.item_id, detail)
            try:
                claim: _ItemClaim | None = None
                item = replace(item, github_issue_number=issue.number)
                item_by_id = {candidate.item_id: candidate for candidate in plan.items}
                fulfilled_without_pr_dependencies = tuple(
                    item_by_id[dependency].workflow_id
                    for dependency in item.dependencies
                    if (
                        (dependency_issue := indexed.get((plan.plan_id, dependency)))
                        is not None
                        and _dependency_has_verified_noop_receipt(
                            dependency_issue,
                            client=client,
                            plan=plan,
                            dependency_id=dependency,
                        )
                    )
                )
                claim = _ItemClaim(
                    root,
                    issue.number,
                    worker_id,
                    item.allowed_paths,
                    max_concurrency,
                )
                try:
                    claim.__enter__()
                except Timeout:
                    # A competing worker owns this item or an overlapping write
                    # scope. Leave it ready so a later pass can retry it.
                    continue
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(issue.labels, "claimed"),
                )
                client.add_comment(
                    issue, f"Powdrr worker {worker_id} claimed this item."
                )
                # Keep queue discovery and GitHub mutations serialized, but let
                # independent item executions proceed concurrently.
                checkpoint_callback = _task_checkpoint_callback(
                    client=client,
                    repo_root=root,
                    issue=issue,
                    plan=plan,
                    item=item,
                    plan_loader=plan_loader,
                )
                checkpoint_callback.assert_claim_owned = claim.assert_current  # type: ignore[attr-defined]
                coordination.release()
                try:
                    claim.assert_current()
                    return_code = run_task(
                        workflow_path,
                        item.workflow_id,
                        worker_id,
                        item,
                        plan,
                        fulfilled_without_pr_dependencies,
                        checkpoint_callback,
                    )
                    claim.assert_current()
                finally:
                    coordination.acquire()
                prerequisite = None
                if return_code == 0:
                    local_record = _read_local_task_checkpoint(root, issue.number)
                    if local_record is not None:
                        prerequisite = _manual_prerequisite_checkpoint_from_record(
                            local_record
                        )
                    else:
                        # Read checkpoints written by older workers during rollout.
                        prerequisite = latest_manual_prerequisite_checkpoint(
                            client.list_comments(issue.number)
                        )
            except Exception as error:
                return_code = 1
                detail = f"{type(error).__name__}: {error}"
            ci_outcome: str | None = None
            if return_code == 0:
                if prerequisite is not None:
                    status = "awaiting_human"
                    outcome = "awaiting_human"
                    note = (
                        "Powdrr staged the workflow pull request with manual "
                        "prerequisite instructions. After completing them, resume "
                        "with `powdrr-lift github-issue-worker-continue --repository "
                        f"{client.repository} --issue-number {issue.number}`."
                    )
                else:
                    comments = client.list_comments(issue.number)
                    task_record = _read_local_task_checkpoint(root, issue.number)
                    pull_request_url = _pull_request_url_from_task_record(
                        task_record
                    ) or _latest_checkpoint_pull_request_url(comments)
                    ci_response_result = _ci_response_result_from_task_record(
                        task_record
                    )
                    inventory = _read_repository_validation_inventory(root)
                    observation = _observe_required_ci_outcome(
                        client,
                        pull_request_url,
                        validation_inventory=(
                            inventory["checks"] if inventory is not None else None
                        ),
                        inventory_digest=(
                            inventory["digest"] if inventory is not None else None
                        ),
                        validation_input_paths=(
                            inventory["input_paths"] if inventory is not None else None
                        ),
                        previous_observation=_latest_ci_outcome_observation(
                            comments, pull_request_url
                        ),
                    )
                    ci_outcome = str(observation["outcome"])
                    _append_ci_outcome_checkpoint(
                        client=client,
                        issue=issue,
                        pull_request_url=pull_request_url,
                        observation=observation,
                        ci_response_result=ci_response_result,
                        human_prerequisite=(
                            _ci_human_prerequisite_from_response(ci_response_result)
                        ),
                    )
                    has_human_prerequisite = (
                        ci_response_result is not None
                        and ci_response_result.get("response_state") == "awaiting_human"
                    )
                    if (
                        has_human_prerequisite
                        and ci_response_result is not None
                        and ci_outcome != "awaiting_review"
                    ):
                        ci_outcome = "awaiting_human"
                        status = "awaiting_human"
                        note = _ci_response_human_note(
                            client.repository,
                            issue.number,
                            pull_request_url,
                            ci_response_result,
                        )
                    else:
                        status = _queue_status_for_ci_outcome(ci_outcome)
                        note = _ci_outcome_note(
                            client.repository, issue.number, observation
                        )
                    outcome = ci_outcome
            else:
                status = "blocked"
                outcome = "blocked"
                detail = detail or "Task runner returned a non-success exit code."
                failure = TaskExecutionResult(
                    plan_id=plan.plan_id,
                    item_id=item.item_id,
                    workflow_id=item.workflow_id,
                    issue_number=issue.number,
                    worker_id=worker_id,
                    stage="implementation",
                    outcome="failed",
                    detail=detail,
                    runner_exit_code=(
                        return_code if isinstance(return_code, int) else None
                    ),
                )
                failure_path = _write_task_execution_result(root, failure)
                note = (
                    "Powdrr execution failed during implementation.\n\n"
                    f"{detail}\n\n"
                    f"Durable diagnostic: `{failure_path.relative_to(root).as_posix()}`"
                )
            issue = client.get_issue(issue.number)
            client.update_issue(
                issue,
                labels=_replace_queue_status(
                    _replace_queue_status(issue.labels, "claimed"), status
                ),
            )
            _refresh_pending_repair_prior_state(client, client.get_issue(issue.number))
            client.add_comment(issue, note)
            _requeue_merged_plan_amendments(
                client=client,
                repo_root=root,
                plan_loader=plan_loader,
            )
            if claim is not None:
                claim.__exit__(None, None, None)
            return WorkerReport(
                outcome,
                issue.number,
                item.item_id,
                detail,
                ci_outcome=ci_outcome,
                failure_record=(str(failure_path) if return_code != 0 else None),
            )
    readiness = _no_eligible_work_readiness(
        client, repo_root=root, plan_loader=plan_loader
    )
    waiting = readiness["blocking_reasons"]
    detail = (
        "; ".join(
            f"{entry['item_id']}: {entry['explanation']}" for entry in waiting[:3]
        )
        or "No open planned work item is currently eligible."
    )
    return WorkerReport("no_eligible_work", detail=detail, readiness=readiness)


def _write_task_execution_result(repo_root: Path, result: TaskExecutionResult) -> Path:
    """Atomically persist the terminal failure record before reporting it."""
    directory = repo_root / ".powdrr" / "github-issue-worker" / "results"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"issue-{result.issue_number}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".issue-{result.issue_number}-", suffix=".tmp", dir=directory
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                result.to_data(), stream, ensure_ascii=False, indent=2, sort_keys=True
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def latest_manual_prerequisite_checkpoint(
    comments: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Read a legacy manual-prerequisite checkpoint from issue comments."""
    for comment in reversed(comments):
        body = comment.get("body")
        if not isinstance(body, str) or _TASK_CHECKPOINT_START not in body:
            continue
        match = re.search(r"```json\s*(.*?)\s*```", body, re.DOTALL)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        checkpoint = _manual_prerequisite_checkpoint_from_record(payload)
        if checkpoint is not None:
            return checkpoint
        if payload.get("output_state_type") == "manual-prerequisite-completed-v1":
            return None
    return None


def _manual_prerequisite_checkpoint_from_record(
    record: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if record.get("output_state_type") != "manual-prerequisite-checkpoint-v1":
        return None
    state = record.get("output_state")
    if not isinstance(state, Mapping):
        return None
    if (
        state.get("schema")
        != "https://powdrr.io/schemas/manual-prerequisite-checkpoint-v1"
        or not all(
            isinstance(state.get(field), str) and state[field].strip()
            for field in (
                "human_task_id",
                "follow_up_task_id",
                "workflow_id",
                "workflow_directory",
                "integration_branch",
                "pull_request_url",
                "commit_sha",
            )
        )
        or not isinstance(state.get("prerequisite"), Mapping)
        or not isinstance(state.get("commit_sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", state["commit_sha"]) is None
        or not isinstance(state.get("integration_branch"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", state["integration_branch"])
        is None
        or ".." in state["integration_branch"].split("/")
        or not isinstance(state.get("workflow_directory"), str)
        or Path(state["workflow_directory"]).is_absolute()
        or ".." in Path(state["workflow_directory"]).parts
    ):
        return None
    return state


def _issue_checkpoint_path(repo_root: str | Path, issue_number: int) -> Path:
    if issue_number < 1:
        raise GitHubIssueQueueError("issue number must be positive")
    return (
        Path(repo_root).resolve()
        / ".powdrr"
        / "github-issue-worker"
        / "checkpoints"
        / f"issue-{issue_number}.json"
    )


def _task_checkpoint_record(
    checkpoint: TaskCheckpoint, *, issue_number: int
) -> dict[str, Any]:
    return {
        "schema": "https://powdrr.io/schemas/github-issue-task-checkpoint-v1",
        "issue_number": issue_number,
        "plan_id": checkpoint.plan_id,
        "item_id": checkpoint.item_id,
        "workflow_id": checkpoint.workflow_id,
        "task_id": checkpoint.task_id,
        "status": (
            "awaiting_human"
            if checkpoint.output_state_type == "manual-prerequisite-checkpoint-v1"
            else "completed"
        ),
        "output_state_type": checkpoint.output_state_type,
        "output_state": checkpoint.output_state,
        "commit_sha": checkpoint.commit_sha,
        "input_contract_digest": checkpoint.input_contract_digest,
    }


def _write_local_task_checkpoint(
    *,
    repo_root: str | Path,
    issue_number: int,
    checkpoint: TaskCheckpoint,
) -> Path:
    path = _issue_checkpoint_path(repo_root, issue_number)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".issue-{issue_number}-", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                _task_checkpoint_record(checkpoint, issue_number=issue_number),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _read_local_task_checkpoint(
    repo_root: str | Path, issue_number: int
) -> Mapping[str, Any] | None:
    path = _issue_checkpoint_path(repo_root, issue_number)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise GitHubIssueQueueError(
            f"could not read local checkpoint for issue #{issue_number}: {error}"
        ) from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema")
        != "https://powdrr.io/schemas/github-issue-task-checkpoint-v1"
        or payload.get("issue_number") != issue_number
        or not isinstance(payload.get("plan_id"), str)
        or not isinstance(payload.get("item_id"), str)
        or not isinstance(payload.get("workflow_id"), str)
        or not isinstance(payload.get("task_id"), str)
        or not isinstance(payload.get("output_state_type"), str)
        or "output_state" not in payload
    ):
        raise GitHubIssueQueueError(
            f"local checkpoint for issue #{issue_number} is malformed"
        )
    return payload


def _local_manual_prerequisite_checkpoint(
    repo_root: str | Path, issue_number: int
) -> Mapping[str, Any] | None:
    record = _read_local_task_checkpoint(repo_root, issue_number)
    return (
        _manual_prerequisite_checkpoint_from_record(record)
        if record is not None
        else None
    )


def continue_manual_prerequisite_issue(
    *,
    client: GitHubIssueClient,
    issue_number: int,
    repo_root: str | Path,
    resume_workflow: Callable[[Mapping[str, Any], TaskCheckpointCallback], int],
) -> WorkerReport:
    """Retry staged PR checks, then resume the durable workflow for one issue."""
    root = Path(repo_root).resolve()
    with _worker_lock(root):
        return _continue_manual_prerequisite_issue_locked(
            client=client,
            issue_number=issue_number,
            repo_root=root,
            resume_workflow=resume_workflow,
        )


def continue_awaiting_ci_issue(
    *, client: GitHubIssueClient, issue_number: int, repo_root: str | Path
) -> WorkerReport:
    """Refresh the required-check outcome for a published implementation PR."""
    issue = client.get_issue(issue_number)
    if issue.state != "open":
        raise GitHubIssueQueueError(f"issue #{issue_number} is not open")
    comments = client.list_comments(issue_number)
    pull_request_url = _latest_checkpoint_pull_request_url(comments)
    if pull_request_url is None:
        raise GitHubIssueQueueError(
            f"issue #{issue_number} has no published PR checkpoint to observe"
        )
    previous_observation = _latest_ci_outcome_observation(comments, pull_request_url)
    human_prerequisite = (
        previous_observation.get("human_prerequisite")
        if previous_observation is not None
        and isinstance(previous_observation.get("human_prerequisite"), Mapping)
        else None
    )
    awaiting_ci = QUEUE_LABELS["awaiting_ci"] in issue.labels
    awaiting_human = (
        QUEUE_LABELS["awaiting_human"] in issue.labels
        and human_prerequisite is not None
    )
    if not (awaiting_ci or awaiting_human):
        raise GitHubIssueQueueError(
            f"issue #{issue_number} is not awaiting CI or a CI human prerequisite"
        )
    inventory = _read_repository_validation_inventory(repo_root)
    observation = _observe_required_ci_outcome(
        client,
        pull_request_url,
        validation_inventory=(inventory["checks"] if inventory is not None else None),
        inventory_digest=inventory["digest"] if inventory is not None else None,
        validation_input_paths=(
            inventory["input_paths"] if inventory is not None else None
        ),
        previous_observation=previous_observation,
    )
    ci_response_result = (
        previous_observation.get("ci_response_result")
        if previous_observation is not None
        and isinstance(previous_observation.get("ci_response_result"), Mapping)
        else None
    )
    _append_ci_outcome_checkpoint(
        client=client,
        issue=issue,
        pull_request_url=pull_request_url,
        observation=observation,
        ci_response_result=ci_response_result,
        human_prerequisite=(
            human_prerequisite
            if awaiting_human and observation.get("outcome") == "blocked"
            else None
        ),
    )
    outcome = str(observation["outcome"])
    if awaiting_human and outcome == "blocked":
        outcome = "awaiting_human"
        reasons = _normalize_ci_response_reasons(
            human_prerequisite.get("reasons")
            if human_prerequisite is not None
            else None
        )
        response_result = {
            "response_state": "awaiting_human",
            "repair_reasons": reasons,
            "response_reason": observation.get("reason", ""),
        }
        note = _ci_response_human_note(
            client.repository, issue_number, pull_request_url, response_result
        )
    else:
        note = _ci_outcome_note(client.repository, issue_number, observation)
    status = _queue_status_for_ci_outcome(outcome)
    issue = client.get_issue(issue_number)
    client.update_issue(issue, labels=_replace_queue_status(issue.labels, status))
    client.add_comment(client.get_issue(issue_number), note)
    return WorkerReport(outcome, issue_number, detail=note, ci_outcome=outcome)


def has_ci_human_checkpoint(*, client: GitHubIssueClient, issue_number: int) -> bool:
    """Return whether the latest published-PR checkpoint asks for CI human action."""
    comments = client.list_comments(issue_number)
    pull_request_url = _latest_checkpoint_pull_request_url(comments)
    if pull_request_url is None:
        return False
    observation = _latest_ci_outcome_observation(comments, pull_request_url)
    return bool(
        observation is not None
        and isinstance(observation.get("human_prerequisite"), Mapping)
    )


def _continue_manual_prerequisite_issue_locked(
    *,
    client: GitHubIssueClient,
    issue_number: int,
    repo_root: Path,
    resume_workflow: Callable[[Mapping[str, Any], TaskCheckpointCallback], int],
) -> WorkerReport:
    issue = client.get_issue(issue_number)
    if issue.state != "open" or QUEUE_LABELS["awaiting_human"] not in issue.labels:
        raise GitHubIssueQueueError(
            f"issue #{issue_number} is not open and awaiting a manual prerequisite"
        )
    local_record = _read_local_task_checkpoint(repo_root, issue_number)
    checkpoint = (
        _manual_prerequisite_checkpoint_from_record(local_record)
        if local_record is not None
        else latest_manual_prerequisite_checkpoint(client.list_comments(issue_number))
    )
    if checkpoint is None:
        raise GitHubIssueQueueError(
            f"issue #{issue_number} has no valid manual prerequisite checkpoint"
        )
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(client.repository)}/pull/[1-9][0-9]*",
        str(checkpoint["pull_request_url"]),
    ):
        raise GitHubIssueQueueError(
            "manual prerequisite checkpoint pull request does not belong to "
            f"{client.repository}"
        )
    try:
        _retry_manual_prerequisite_ci(client, checkpoint)
    except GitHubIssueQueueError as error:
        client.add_comment(
            issue,
            "Powdrr could not resume the workflow because the staged pull "
            f"request checks have not passed: {error}. The issue remains "
            "awaiting-human.",
        )
        raise
    client.add_comment(
        issue,
        "CI passed after the manual prerequisite. Powdrr is resuming the same "
        "durable workflow now.",
    )
    pointer = parse_issue_pointer(issue.body)
    if pointer is None:
        raise GitHubIssueQueueError(
            f"issue #{issue_number} has no valid Powdrr plan pointer"
        )

    def checkpoint_callback(
        task_id: str, output_state_type: str, output_state: Any, commit_sha: str | None
    ) -> None:
        active_repair = _active_plan_repair_pending(client.list_comments(issue.number))
        if active_repair is not None:
            raise GitHubIssueQueueError(
                "workflow paused at a durable checkpoint because plan repair "
                f"{active_repair.get('repair_id')} is pending review"
            )
        _mark_plan_repair_from_checkpoint(
            client=client,
            output_state_type=output_state_type,
            output_state=output_state,
            plan_id=pointer["plan_id"],
        )
        resumed_checkpoint = TaskCheckpoint(
            plan_id=pointer["plan_id"],
            item_id=pointer["item_id"],
            workflow_id=str(checkpoint["workflow_id"]),
            task_id=task_id,
            output_state_type=output_state_type,
            output_state=output_state,
            commit_sha=commit_sha,
            input_contract_digest=(
                checkpoint.get("input_contract_digest")
                if isinstance(checkpoint.get("input_contract_digest"), str)
                else None
            ),
        )
        _write_local_task_checkpoint(
            repo_root=repo_root,
            issue_number=issue_number,
            checkpoint=resumed_checkpoint,
        )
        checkpoint_comment = render_task_checkpoint_comment(resumed_checkpoint)
        if not any(
            item.get("body") == checkpoint_comment
            for item in client.list_comments(issue_number)
        ):
            client.add_comment(client.get_issue(issue_number), checkpoint_comment)
        _requeue_merged_plan_amendments(
            client=client,
            repo_root=repo_root,
            plan_loader=load_reviewed_plan_at_commit,
        )

    detail: str | None = None
    try:
        return_code = resume_workflow(checkpoint, checkpoint_callback)
    except Exception as error:
        return_code = 1
        detail = f"{type(error).__name__}: {error}"
    latest_local_checkpoint = _read_local_task_checkpoint(repo_root, issue_number)
    pending_prerequisite = (
        _manual_prerequisite_checkpoint_from_record(latest_local_checkpoint)
        if latest_local_checkpoint is not None
        else latest_manual_prerequisite_checkpoint(client.list_comments(issue_number))
    )
    comments = client.list_comments(issue_number)
    active_repair = _active_plan_repair_pending(comments)
    if active_repair is not None:
        status = "blocked"
        outcome = "blocked"
        note = (
            "Powdrr paused because plan repair "
            f"{active_repair.get('repair_id')} is pending review."
        )
    elif pending_prerequisite is not None:
        status = "awaiting_human"
        outcome = "awaiting_human"
        note = (
            "Powdrr paused for another manual prerequisite. Follow the updated "
            "instructions on the staged pull request, then run "
            "`powdrr-lift github-issue-worker-continue --repository "
            f"{client.repository} --issue-number {issue.number}`."
        )
    elif return_code == 0:
        pull_request_url = (
            _pull_request_url_from_task_record(latest_local_checkpoint)
            or _latest_checkpoint_pull_request_url(comments)
            or str(checkpoint["pull_request_url"])
        )
        inventory = _read_repository_validation_inventory(repo_root)
        observation = _observe_required_ci_outcome(
            client,
            pull_request_url,
            validation_inventory=(
                inventory["checks"] if inventory is not None else None
            ),
            inventory_digest=inventory["digest"] if inventory is not None else None,
            validation_input_paths=(
                inventory["input_paths"] if inventory is not None else None
            ),
            previous_observation=_latest_ci_outcome_observation(
                comments, pull_request_url
            ),
        )
        _append_ci_outcome_checkpoint(
            client=client,
            issue=issue,
            pull_request_url=pull_request_url,
            observation=observation,
        )
        outcome = str(observation["outcome"])
        status = _queue_status_for_ci_outcome(outcome)
        note = _ci_outcome_note(client.repository, issue_number, observation)
    else:
        status = "blocked"
        outcome = "blocked"
        note = "Powdrr execution did not finish successfully."
        if detail:
            note += f" {detail}"
    issue = client.get_issue(issue_number)
    client.update_issue(
        issue,
        labels=_replace_queue_status(issue.labels, status),
    )
    _refresh_pending_repair_prior_state(client, client.get_issue(issue.number))
    client.add_comment(issue, note)
    _requeue_merged_plan_amendments(
        client=client,
        repo_root=repo_root,
        plan_loader=load_reviewed_plan_at_commit,
    )
    return WorkerReport(
        outcome,
        issue_number,
        detail=detail,
        ci_outcome=outcome
        if return_code == 0 and pending_prerequisite is None
        else None,
    )


def _retry_manual_prerequisite_ci(
    client: GitHubIssueClient, checkpoint: Mapping[str, Any]
) -> None:
    branch = str(checkpoint["integration_branch"])
    commit_sha = str(checkpoint["commit_sha"])
    runs_result = client._invoke(
        [
            "run",
            "list",
            "--branch",
            branch,
            "--json",
            "databaseId,headSha,status,conclusion",
            "--limit",
            "100",
        ],
        check=False,
    )
    if runs_result.returncode != 0:
        raise GitHubIssueQueueError(
            "could not inspect CI runs for the staged prerequisite PR: "
            f"{runs_result.stderr.strip()}"
        )
    try:
        runs = json.loads(runs_result.stdout)
    except json.JSONDecodeError as error:
        raise GitHubIssueQueueError(
            "gh returned malformed workflow run data"
        ) from error
    matching = (
        [
            run
            for run in runs
            if isinstance(run, Mapping)
            and run.get("headSha") == commit_sha
            and isinstance(run.get("databaseId"), int)
        ]
        if isinstance(runs, list)
        else []
    )
    if not matching:
        raise GitHubIssueQueueError(
            "no GitHub Actions runs were found for the staged PR commit "
            f"{commit_sha}; the issue remains awaiting-human"
        )
    for run in matching:
        run_id = str(run["databaseId"])
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status == "completed" and conclusion not in {
            "success",
            "neutral",
            "skipped",
        }:
            rerun = client._invoke(["run", "rerun", run_id, "--failed"], check=False)
            if rerun.returncode != 0:
                raise GitHubIssueQueueError(
                    f"could not rerun failed CI run {run_id}: {rerun.stderr.strip()}"
                )
        elif status not in {
            "completed",
            "queued",
            "in_progress",
            "waiting",
            "requested",
        }:
            raise GitHubIssueQueueError(
                f"CI run {run_id} is in unsupported state {status!r}"
            )
        watched = client._invoke(["run", "watch", run_id, "--exit-status"], check=False)
        if watched.returncode != 0:
            raise GitHubIssueQueueError(
                f"CI run {run_id} did not pass: {watched.stderr.strip()}"
            )
    checks = client._invoke(
        ["pr", "checks", str(checkpoint["pull_request_url"]), "--watch", "--fail-fast"],
        check=False,
    )
    if checks.returncode != 0:
        raise GitHubIssueQueueError(
            f"the staged pull request still has failing checks: {checks.stderr.strip()}"
        )


def fulfill_issue_without_pr(
    *,
    issue_number: int,
    reason: str,
    client: GitHubIssueClient,
    repo_root: str | Path | None = None,
) -> QueueIssue:
    """Record that a planned item is already fulfilled without an implementation PR."""
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise GitHubIssueQueueError(
            "a reason is required to fulfill an issue without a PR"
        )
    issue = client.get_issue(issue_number)
    pointer = parse_issue_pointer(issue.body)
    if pointer is None:
        raise GitHubIssueQueueError(
            f"issue #{issue_number} is not a Powdrr plan work item"
        )
    contract_digest: str | None = None
    if repo_root is not None:
        pinned_plan = load_reviewed_plan_at_commit(
            pointer["plan_path"], pointer["plan_commit"], repo_root=repo_root
        )
        planned_item = next(
            (item for item in pinned_plan.items if item.item_id == pointer["item_id"]),
            None,
        )
        if pinned_plan.plan_id != pointer["plan_id"] or planned_item is None:
            raise GitHubIssueQueueError(
                f"issue #{issue_number} does not match an item in its pinned plan"
            )
        from powdrr_service.planning.repair import item_input_contract_digest

        contract_digest = item_input_contract_digest(planned_item, pinned_plan.planning)
    client.ensure_fulfilled_without_pr_label()
    labels = tuple(
        sorted(
            (set(issue.labels) - set(QUEUE_LABELS.values()))
            | {FULFILLED_WITHOUT_PR_LABEL}
        )
    )
    comments = client.list_comments(issue_number)
    if not any(
        isinstance(comment.get("body"), str)
        and _FULFILLMENT_MARKER in str(comment.get("body"))
        for comment in comments
    ):
        client.add_comment(
            issue,
            render_fulfilled_without_pr_comment(
                plan_id=pointer["plan_id"],
                item_id=pointer["item_id"],
                reason=normalized_reason,
                plan_commit=pointer["plan_commit"],
                input_contract_digest=contract_digest,
            ),
        )
    client.update_issue(
        issue,
        labels=labels,
        state="closed",
        state_reason="completed",
    )
    return QueueIssue(
        number=issue.number,
        title=issue.title,
        body=issue.body,
        state="closed",
        labels=labels,
        url=issue.url,
    )


def _apply_issue_plan_adjustments(
    *,
    item: PlannedWorkItem,
    plan_id: str,
    comments: Sequence[Mapping[str, Any]],
) -> PlannedWorkItem:
    """Apply trusted, structured issue-comment edits without changing the plan."""
    effective = item
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or _PLAN_ADJUSTMENT_MARKER not in body:
            continue
        if (
            str(comment.get("author_association", "")).upper()
            not in _PLAN_ADJUSTMENT_AUTHORS
        ):
            continue
        match = re.search(r"```json\s*(.*?)\s*```", body, re.DOTALL)
        if match is None:
            raise GitHubIssueQueueError("marked comment must contain a JSON code block")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError("comment JSON is malformed") from error
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema",
            "plan_id",
            "item_id",
            "reason",
            "changes",
        }:
            raise GitHubIssueQueueError("comment has unknown or missing fields")
        if payload.get("schema") != _PLAN_ADJUSTMENT_SCHEMA:
            raise GitHubIssueQueueError("comment has an unsupported schema")
        if payload.get("plan_id") != plan_id or payload.get("item_id") != item.item_id:
            raise GitHubIssueQueueError("comment does not match this plan item")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise GitHubIssueQueueError("reason must not be empty")
        changes = payload.get("changes")
        if (
            not isinstance(changes, Mapping)
            or not changes
            or not set(changes).issubset(_PLAN_ADJUSTABLE_FIELDS)
        ):
            raise GitHubIssueQueueError("changes must contain supported item fields")

        values: dict[str, Any] = {}
        for field, value in changes.items():
            if field in {"title", "objective"}:
                maximum_length = 120 if field == "title" else 2000
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value.strip()) > maximum_length
                ):
                    raise GitHubIssueQueueError(
                        f"{field} must contain 1 to {maximum_length} characters"
                    )
                values[field] = value.strip()
                continue
            if not isinstance(value, list) or not all(
                isinstance(entry, str) and entry.strip() for entry in value
            ):
                raise GitHubIssueQueueError(
                    f"{field} must be a list of unique non-empty strings"
                )
            normalized = tuple(entry.strip() for entry in value)
            if len(set(normalized)) != len(normalized):
                raise GitHubIssueQueueError(
                    f"{field} must be a list of unique non-empty strings"
                )
            if field == "scope" and not 1 <= len(normalized) <= 5:
                raise GitHubIssueQueueError(
                    "scope must contain between 1 and 5 entries"
                )
            if field == "acceptance_criteria" and not normalized:
                raise GitHubIssueQueueError("acceptance_criteria must not be empty")
            if field == "acceptance_criteria" and len(normalized) > 10:
                raise GitHubIssueQueueError(
                    "acceptance_criteria must contain at most 10 entries"
                )
            if field == "implementation_steps" and not 1 <= len(normalized) <= 8:
                raise GitHubIssueQueueError(
                    "implementation_steps must contain between 1 and 8 entries"
                )
            if field == "allowed_paths":
                if not normalized or any(
                    Path(path).is_absolute() or ".." in Path(path).parts
                    for path in normalized
                ):
                    raise GitHubIssueQueueError(
                        "allowed_paths must contain unique repository-relative paths"
                    )
            values[field] = normalized
        adjustment_record = {
            "reason": reason.strip(),
            "changes": {
                field: list(value) if isinstance(value, tuple) else value
                for field, value in values.items()
            },
        }
        effective = replace(
            effective,
            **values,
            inline_adjustments=(
                *effective.inline_adjustments,
                adjustment_record,
            ),
        )
    return effective


def render_fulfilled_without_pr_comment(
    *,
    plan_id: str,
    item_id: str,
    reason: str,
    plan_commit: str | None = None,
    input_contract_digest: str | None = None,
) -> str:
    payload = {
        "schema": _FULFILLMENT_SCHEMA,
        "plan_id": plan_id,
        "item_id": item_id,
        "plan_commit": plan_commit,
        "input_contract_digest": input_contract_digest,
        "fulfillment": "without_implementation_pr",
        "reason": reason,
    }
    return (
        "### Powdrr work item fulfilled without an implementation PR\n\n"
        f"{_FULFILLMENT_MARKER}\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```"
    )


def render_task_checkpoint_comment(checkpoint: TaskCheckpoint) -> str:
    """Render a readable GitHub comment with machine-readable checkpoint data."""
    payload = {
        "schema": "https://powdrr.io/schemas/github-issue-task-checkpoint-v1",
        "plan_id": checkpoint.plan_id,
        "item_id": checkpoint.item_id,
        "workflow_id": checkpoint.workflow_id,
        "task_id": checkpoint.task_id,
        "status": (
            "awaiting_human"
            if checkpoint.output_state_type == "manual-prerequisite-checkpoint-v1"
            else "completed"
        ),
        "output_state_type": checkpoint.output_state_type,
        "output_state": checkpoint.output_state,
        "commit_sha": checkpoint.commit_sha,
        "input_contract_digest": checkpoint.input_contract_digest,
    }
    return (
        "### Powdrr task checkpoint\n\n"
        f"{_TASK_CHECKPOINT_START}\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```"
    )


def _ci_repair_artifact_pointers(body: str) -> dict[str, str]:
    if _CI_REPAIR_POINTER_START not in body and _CI_REPAIR_POINTER_END not in body:
        return {}
    if (
        body.count(_CI_REPAIR_POINTER_START) != 1
        or body.count(_CI_REPAIR_POINTER_END) != 1
    ):
        raise GitHubIssueQueueError("CI repair artifact pointer is malformed")
    start = body.index(_CI_REPAIR_POINTER_START) + len(_CI_REPAIR_POINTER_START)
    end = body.index(_CI_REPAIR_POINTER_END, start)
    try:
        payload = json.loads(body[start:end].strip())
    except json.JSONDecodeError as error:
        raise GitHubIssueQueueError(
            "CI repair artifact pointer is malformed"
        ) from error
    if not isinstance(payload, Mapping):
        raise GitHubIssueQueueError("CI repair artifact pointer must be an object")
    pointers = payload.get("contracts")
    if not isinstance(pointers, Mapping) or not all(
        isinstance(key, str)
        and _CI_REPAIR_CONTRACT_ID_RE.fullmatch(key)
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
        for key, value in pointers.items()
    ):
        raise GitHubIssueQueueError("CI repair artifact pointer entries are malformed")
    return dict(pointers)


def _render_ci_repair_artifact_pointer(
    body: str, *, contract_id: str, bundle_digest: str
) -> str:
    pointers = _ci_repair_artifact_pointers(body)
    pointers[contract_id] = bundle_digest
    block = (
        f"{_CI_REPAIR_POINTER_START}\n"
        f"{json.dumps({'contracts': pointers}, sort_keys=True)}\n"
        f"{_CI_REPAIR_POINTER_END}"
    )
    if _CI_REPAIR_POINTER_START in body:
        start = body.index(_CI_REPAIR_POINTER_START)
        end = body.index(_CI_REPAIR_POINTER_END, start) + len(_CI_REPAIR_POINTER_END)
        return body[:start] + block + body[end:]
    return f"{body.rstrip()}\n\n{block}" if body.strip() else block


def _has_complete_ci_repair_artifact(
    comments: Sequence[Mapping[str, Any]],
    contract_id: str,
    *,
    bundle_digest: str,
) -> bool:
    bundles: dict[str, tuple[int, set[int]]] = {}
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or _CI_REPAIR_ARTIFACT_MARKER not in body:
            continue
        match = re.search(r"\x60{3}json\s*\n(.*?)\n\x60{3}", body, re.DOTALL)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema")
            != "https://powdrr.io/schemas/ci-repair-artifact-chunk-v1"
            or payload.get("contract_id") != contract_id
            or payload.get("bundle_digest") != bundle_digest
        ):
            continue
        count = payload.get("chunk_count")
        index = payload.get("chunk_index")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 1 <= index <= count
        ):
            continue
        digest = str(payload["bundle_digest"])
        existing_count, indexes = bundles.setdefault(digest, (count, set()))
        if existing_count == count:
            indexes.add(index)
    return any(
        indexes == set(range(1, count + 1)) for count, indexes in bundles.values()
    )


def _persist_ci_repair_artifact(
    *,
    client: GitHubIssueClient,
    issue: QueueIssue,
    contract_id: str,
    record: Mapping[str, Any],
) -> None:
    """Store a bounded, integrity-checked CI repair continuation in issue comments."""
    if _CI_REPAIR_CONTRACT_ID_RE.fullmatch(contract_id) is None:
        raise GitHubIssueQueueError("CI repair artifact contract ID is malformed")
    payload = {"schema": "ci-repair-continuation-v1", **dict(record)}
    if payload.get("contract_id") != contract_id:
        raise GitHubIssueQueueError("CI repair artifact contract ID does not match")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _CI_REPAIR_ARTIFACT_MAX_BYTES:
        raise GitHubIssueQueueError("CI repair continuation exceeds the size limit")
    compressed = zlib.compress(encoded, level=9)
    if len(compressed) > _CI_REPAIR_ARTIFACT_MAX_BYTES:
        raise GitHubIssueQueueError("CI repair continuation exceeds the size limit")
    digest = hashlib.sha256(compressed).hexdigest()
    pieces = [
        compressed[offset : offset + _CI_REPAIR_ARTIFACT_CHUNK_BYTES]
        for offset in range(0, len(compressed), _CI_REPAIR_ARTIFACT_CHUNK_BYTES)
    ] or [b""]
    for index, piece in enumerate(pieces, start=1):
        chunk_payload = {
            "schema": "https://powdrr.io/schemas/ci-repair-artifact-chunk-v1",
            "contract_id": contract_id,
            "bundle_digest": digest,
            "bundle_size": len(compressed),
            "chunk_index": index,
            "chunk_count": len(pieces),
            "chunk_sha256": hashlib.sha256(piece).hexdigest(),
            "content_base64": base64.b64encode(piece).decode("ascii"),
        }
        body = (
            "### Powdrr CI repair continuation\n\n"
            f"{_CI_REPAIR_ARTIFACT_MARKER}\n"
            "```json\n"
            f"{json.dumps(chunk_payload, indent=2, sort_keys=True)}\n"
            "```"
        )
        client.add_comment(issue, body)
    current_issue = client.get_issue(issue.number)
    updated_body = _render_ci_repair_artifact_pointer(
        current_issue.body,
        contract_id=contract_id,
        bundle_digest=digest,
    )
    if updated_body != current_issue.body:
        client.update_issue(current_issue, body=updated_body)


def _load_ci_repair_artifact(
    *,
    client: GitHubIssueClient,
    issue_number: int,
    contract_id: str,
) -> Mapping[str, Any] | None:
    """Load the newest complete CI repair continuation for one contract."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    issue = client.get_issue(issue_number)
    pointers = _ci_repair_artifact_pointers(issue.body)
    digest_pointer = pointers.get(contract_id)
    if digest_pointer is None:
        return None
    list_artifacts = getattr(client, "list_ci_repair_artifact_comments", None)
    comments = (
        list_artifacts(
            issue_number,
            contract_id=contract_id,
            bundle_digest=digest_pointer,
        )
        if callable(list_artifacts)
        else client.list_comments(issue_number)
    )
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or _CI_REPAIR_ARTIFACT_MARKER not in body:
            continue
        match = re.search(r"```json\s*\n(.*?)\n```", body, re.DOTALL)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema")
            != "https://powdrr.io/schemas/ci-repair-artifact-chunk-v1"
            or payload.get("contract_id") != contract_id
            or payload.get("bundle_digest") != digest_pointer
        ):
            continue
        digest = str(payload["bundle_digest"])
        if digest not in groups:
            groups[digest] = {"chunks": {}, "payload": dict(payload)}
            order.append(digest)
        group = groups[digest]
        index = payload.get("chunk_index")
        count = payload.get("chunk_count")
        size = payload.get("bundle_size")
        content = payload.get("content_base64")
        chunk_digest = payload.get("chunk_sha256")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= index <= count
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= _CI_REPAIR_ARTIFACT_MAX_BYTES
            or not isinstance(content, str)
            or not isinstance(chunk_digest, str)
        ):
            continue
        try:
            piece = base64.b64decode(content, validate=True)
        except ValueError:
            continue
        if hashlib.sha256(piece).hexdigest() != chunk_digest:
            continue
        group["count"] = count
        group["size"] = size
        group["chunks"][index] = piece

    for digest in reversed(order):
        group = groups[digest]
        count = group.get("count")
        chunks = group["chunks"]
        if not isinstance(count, int) or set(chunks) != set(range(1, count + 1)):
            continue
        compressed = b"".join(chunks[index] for index in range(1, count + 1))
        if (
            len(compressed) != group.get("size")
            or hashlib.sha256(compressed).hexdigest() != digest
        ):
            continue
        try:
            decompressor = zlib.decompressobj()
            raw_payload = decompressor.decompress(
                compressed, _CI_REPAIR_ARTIFACT_MAX_BYTES + 1
            )
            if (
                len(raw_payload) > _CI_REPAIR_ARTIFACT_MAX_BYTES
                or not decompressor.eof
                or decompressor.unused_data
            ):
                continue
            payload = json.loads(raw_payload)
        except (zlib.error, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("schema") == "ci-repair-continuation-v1"
            and payload.get("contract_id") == contract_id
        ):
            return payload
    return None


def _task_checkpoint_callback(
    *,
    client: GitHubIssueClient,
    issue: QueueIssue,
    plan: ReviewedExecutionPlan,
    item: PlannedWorkItem,
    repo_root: Path | None = None,
    plan_loader: Callable[..., ReviewedExecutionPlan] = load_reviewed_plan_at_commit,
) -> TaskCheckpointCallback:
    def persist(
        task_id: str,
        output_state_type: str,
        output_state: Any,
        commit_sha: str | None,
        *,
        target_issue: QueueIssue = issue,
        target_plan: ReviewedExecutionPlan = plan,
        target_item: PlannedWorkItem = item,
    ) -> None:
        active_repair = _active_plan_repair_pending(
            client.list_comments(target_issue.number)
        )
        if active_repair is not None:
            raise GitHubIssueQueueError(
                "workflow paused at a durable checkpoint because plan repair "
                f"{active_repair.get('repair_id')} is pending review"
            )
        _mark_plan_repair_from_checkpoint(
            client=client,
            output_state_type=output_state_type,
            output_state=output_state,
            plan_id=target_plan.plan_id,
        )
        from powdrr_service.planning.repair import item_input_contract_digest

        checkpoint = TaskCheckpoint(
            plan_id=target_plan.plan_id,
            item_id=target_item.item_id,
            workflow_id=target_item.workflow_id,
            task_id=task_id,
            output_state_type=output_state_type,
            output_state=output_state,
            commit_sha=commit_sha,
            input_contract_digest=item_input_contract_digest(
                target_item, target_plan.planning
            ),
        )
        if repo_root is not None:
            _write_local_task_checkpoint(
                repo_root=repo_root,
                issue_number=target_issue.number,
                checkpoint=checkpoint,
            )
        comment = render_task_checkpoint_comment(checkpoint)
        if not any(
            item.get("body") == comment
            for item in client.list_comments(target_issue.number)
        ):
            client.add_comment(target_issue, comment)
        if repo_root is not None:
            # A repair PR must block its full impact set before another durable
            # checkpoint can be reached.
            _requeue_merged_plan_amendments(
                client=client,
                repo_root=repo_root,
                plan_loader=plan_loader,
            )

    def persist_ci_repair_artifact(contract_id: str, record: Mapping[str, Any]) -> None:
        _persist_ci_repair_artifact(
            client=client,
            issue=issue,
            contract_id=contract_id,
            record=record,
        )

    def load_ci_repair_artifact(contract_id: str) -> Mapping[str, Any] | None:
        return _load_ci_repair_artifact(
            client=client,
            issue_number=issue.number,
            contract_id=contract_id,
        )

    persist.persist_ci_repair_artifact = persist_ci_repair_artifact  # type: ignore[attr-defined]
    persist.load_ci_repair_artifact = load_ci_repair_artifact  # type: ignore[attr-defined]
    return persist


def _mark_plan_repair_from_checkpoint(
    *,
    client: GitHubIssueClient,
    output_state_type: str,
    output_state: Any,
    plan_id: str,
) -> None:
    if not isinstance(output_state, Mapping) or not output_state_type.endswith(
        "result-state"
    ):
        return
    pull_request_url = output_state.get("pull_request_url")
    number = _same_repository_pull_request_number(
        pull_request_url if isinstance(pull_request_url, str) else None,
        client.repository,
    )
    if number is None:
        return
    metadata = _plan_repair_metadata_from_pr(client.pull_request(number))
    if metadata is None:
        return
    if metadata["plan_id"] != plan_id:
        raise GitHubIssueQueueError(
            "worker-created plan repair PR does not match the issue plan"
        )
    _mark_plan_repair_pending(
        client=client,
        metadata=metadata,
        pull_request_number=number,
    )


def _promote_unblocked_issues(
    *,
    client: GitHubIssueClient,
    repo_root: Path,
    plan_loader: Callable[..., ReviewedExecutionPlan],
) -> None:
    """Make dependency-blocked work ready after all prerequisite issues close."""
    blocked = client.list_issues(
        labels=(QUEUE_LABELS["blocked_on_dependency"],), state="open"
    )
    loaded_plans: dict[tuple[str, str, str], ReviewedExecutionPlan] = {}
    issue_indexes: dict[str, dict[tuple[str, str], QueueIssue]] = {}
    status_changed = False
    for issue in sorted(blocked, key=lambda value: value.number):
        if (
            QUEUE_LABELS["blocked_on_dependency"] not in issue.labels
            or issue.state != "open"
        ):
            continue
        pointer = parse_issue_pointer(issue.body)
        if pointer is None:
            continue
        plan_key = (
            pointer["plan_id"],
            pointer["plan_path"],
            pointer["plan_commit"],
        )
        plan = loaded_plans.get(plan_key)
        if plan is None:
            plan = plan_loader(
                pointer["plan_path"], pointer["plan_commit"], repo_root=repo_root
            )
            loaded_plans[plan_key] = plan
        if plan.plan_id != pointer["plan_id"]:
            continue
        item = next(
            (
                candidate
                for candidate in plan.items
                if candidate.item_id == pointer["item_id"]
            ),
            None,
        )
        if item is None:
            continue
        indexed = issue_indexes.get(plan.plan_id)
        if indexed is None:
            indexed = _plan_issue_index(
                client.list_issues(labels=(_plan_label(plan.plan_id),))
            )
            issue_indexes[plan.plan_id] = indexed
        by_number = {value.number: value for value in indexed.values()}
        if all(
            _dependency_is_fulfilled(
                indexed.get((plan.plan_id, dependency)),
                by_number,
                client=client,
                plan=plan,
                dependency_id=dependency,
            )
            for dependency in item.dependencies
        ):
            client.update_issue(
                issue,
                labels=_replace_queue_status(issue.labels, "ready"),
            )
            status_changed = True
    if status_changed:
        # The next worker step queries for ready issues. Give GitHub's issue
        # listing a moment to reflect the label update before that read.
        sleep(_GITHUB_STATUS_SETTLE_SECONDS)


def _requeue_merged_plan_amendments(
    *,
    client: GitHubIssueClient,
    repo_root: Path,
    plan_loader: Callable[..., ReviewedExecutionPlan],
) -> None:
    """Reconcile pending plan repairs over every issue in their impact manifest."""
    plan_issues = client.list_issues(labels=(PLAN_REPAIR_PENDING_LABEL,), state="all")
    repairs: dict[int, Mapping[str, Any]] = {}
    for issue in plan_issues:
        comments = client.list_comments(issue.number)
        pending = _active_plan_repair_pending(comments)
        url = None
        if pending is not None and isinstance(pending.get("pull_request_number"), int):
            url = (
                f"https://github.com/{client.repository}/pull/"
                f"{pending['pull_request_number']}"
            )
        if url is None:
            url = _latest_checkpoint_pull_request_url(comments)
        number = _same_repository_pull_request_number(url, client.repository)
        if number is not None:
            repairs[number] = {"issue": issue}

    for pull_request_number, discovery in sorted(repairs.items()):
        pull_request = client.pull_request(pull_request_number)
        metadata = _plan_repair_metadata_from_pr(pull_request)
        if metadata is None:
            continue
        origin = discovery["issue"]
        if not isinstance(origin, QueueIssue):
            continue
        origin_pointer = parse_issue_pointer(origin.body)
        if origin_pointer is None or origin_pointer["plan_id"] != metadata["plan_id"]:
            raise GitHubIssueQueueError(
                f"plan-repair PR #{pull_request_number} does not match its issue plan"
            )
        state = pull_request.get("state")
        if pull_request.get("merged") is True:
            merge_commit = pull_request.get("merge_commit_sha")
            if not isinstance(merge_commit, str) or not re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", merge_commit
            ):
                raise GitHubIssueQueueError(
                    f"merged plan-repair PR #{pull_request_number} has no merge commit"
                )
            _reconcile_merged_plan_repair(
                client=client,
                repo_root=repo_root,
                plan_loader=plan_loader,
                metadata=metadata,
                pull_request_number=pull_request_number,
                merge_commit=merge_commit,
            )
        elif state == "closed":
            _restore_closed_plan_repair(
                client=client,
                metadata=metadata,
                pull_request_number=pull_request_number,
            )
        else:
            _mark_plan_repair_pending(
                client=client,
                metadata=metadata,
                pull_request_number=pull_request_number,
            )


def _plan_repair_metadata_from_pr(
    pull_request: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    body = pull_request.get("body")
    if not isinstance(body, str) or _PLAN_REPAIR_MARKER not in body:
        return None
    payload = _json_payload_after_marker(body, _PLAN_REPAIR_MARKER)
    if (
        payload is None
        or payload.get("schema")
        != "https://powdrr.io/schemas/github-issue-plan-repair-v1"
        or not isinstance(payload.get("plan_id"), str)
        or not isinstance(payload.get("plan_path"), str)
        or not isinstance(payload.get("repair"), Mapping)
    ):
        raise GitHubIssueQueueError("plan-repair PR has malformed repair metadata")
    repair = payload["repair"]
    if (
        not isinstance(repair.get("repair_id"), str)
        or not isinstance(repair.get("affected_item_ids"), list)
        or any(not isinstance(item, str) for item in repair["affected_item_ids"])
        or not repair["affected_item_ids"]
        or not isinstance(repair.get("old_item_digests"), Mapping)
        or not isinstance(repair.get("new_item_digests"), Mapping)
        or not isinstance(repair.get("old_input_contract_digests"), Mapping)
        or not isinstance(repair.get("new_input_contract_digests"), Mapping)
        or not isinstance(repair.get("retired_items"), list)
    ):
        raise GitHubIssueQueueError("plan-repair PR has an incomplete impact manifest")
    return {
        "plan_id": payload["plan_id"],
        "plan_path": payload["plan_path"],
        "repair": dict(repair),
    }


def _json_payload_after_marker(body: str, marker: str) -> Mapping[str, Any] | None:
    try:
        content = body.split(marker, 1)[1]
        payload_text = content.split("```json\n", 1)[1].split("\n```", 1)[0]
        value = json.loads(payload_text)
    except (IndexError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _active_plan_repair_pending(
    comments: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    pending_records: list[Mapping[str, Any]] = []
    resolved_ids: set[str] = set()
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _PLAN_REPAIR_RESOLVED_MARKER in body:
            resolved = _json_payload_after_marker(body, _PLAN_REPAIR_RESOLVED_MARKER)
            if isinstance(resolved, Mapping) and isinstance(
                resolved.get("repair_id"), str
            ):
                resolved_ids.add(str(resolved["repair_id"]))
        if _PLAN_REPAIR_PENDING_MARKER in body:
            pending = _json_payload_after_marker(body, _PLAN_REPAIR_PENDING_MARKER)
            if isinstance(pending, Mapping) and isinstance(
                pending.get("repair_id"), str
            ):
                pending_records.append(pending)
    for pending in reversed(pending_records):
        if pending.get("repair_id") not in resolved_ids:
            return pending
    return None


def _plan_repair_comment(marker: str, payload: Mapping[str, Any], title: str) -> str:
    return (
        f"### {title}\n\n{marker}\n```json\n"
        f"{json.dumps(dict(payload), indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```"
    )


def _mark_plan_repair_pending(
    *,
    client: GitHubIssueClient,
    metadata: Mapping[str, Any],
    pull_request_number: int,
) -> None:
    repair = metadata["repair"]
    repair_id = str(repair["repair_id"])
    plan_id = str(metadata["plan_id"])
    plan_label = _plan_label(plan_id)
    client.ensure_queue_labels()
    issues = client.list_issues(labels=(plan_label,), state="all")
    by_item = _plan_issue_index(issues)
    pull_request_url = (
        f"https://github.com/{client.repository}/pull/{pull_request_number}"
    )
    active_by_item = {
        item_id: _active_plan_repair_pending(client.list_comments(issue.number))
        for item_id in repair["affected_item_ids"]
        if (issue := by_item.get((plan_id, item_id))) is not None
    }
    for item_id, pending in active_by_item.items():
        if pending is not None and pending.get("repair_id") != repair_id:
            issue = by_item[(plan_id, item_id)]
            raise GitHubIssueQueueError(
                f"issue #{issue.number} is already blocked by plan repair "
                f"{pending.get('repair_id')}"
            )
    for item_id in repair["affected_item_ids"]:
        issue = by_item.get((plan_id, item_id))
        if issue is None:
            continue
        comments = client.list_comments(issue.number)
        pending = _active_plan_repair_pending(comments)
        if pending is None or pending.get("repair_id") != repair_id:
            pointer = parse_issue_pointer(issue.body)
            if pointer is None:
                continue
            prior_status = next(
                (
                    status
                    for status, label in QUEUE_LABELS.items()
                    if label in issue.labels
                ),
                "blocked",
            )
            client.add_comment(
                issue,
                _plan_repair_comment(
                    _PLAN_REPAIR_PENDING_MARKER,
                    {
                        "repair_id": repair_id,
                        "item_id": item_id,
                        "pull_request_number": pull_request_number,
                        "pull_request_url": pull_request_url,
                        "prior_queue_status": prior_status,
                        "prior_issue_state": issue.state,
                        "prior_plan_commit": pointer["plan_commit"],
                    },
                    "Plan repair pending review",
                ),
            )
        if issue.state == "open" and QUEUE_LABELS["blocked"] not in issue.labels:
            client.update_issue(
                issue,
                labels=tuple(
                    sorted(
                        set(_replace_queue_status(issue.labels, "blocked"))
                        | {PLAN_REPAIR_PENDING_LABEL}
                    )
                ),
            )
        elif issue.state == "open" and PLAN_REPAIR_PENDING_LABEL not in issue.labels:
            client.update_issue(
                issue,
                labels=tuple(sorted((*issue.labels, PLAN_REPAIR_PENDING_LABEL))),
            )


def _refresh_pending_repair_prior_state(
    client: GitHubIssueClient, issue: QueueIssue
) -> None:
    """Record the queue state left by a worker that paused for an open repair."""
    pending = _active_plan_repair_pending(client.list_comments(issue.number))
    if pending is None:
        return
    current_status = next(
        (status for status, label in QUEUE_LABELS.items() if label in issue.labels),
        "blocked",
    )
    if (
        pending.get("prior_queue_status") == current_status
        and pending.get("prior_issue_state") == issue.state
    ):
        return
    updated = dict(pending)
    updated["prior_queue_status"] = current_status
    updated["prior_issue_state"] = issue.state
    pointer = parse_issue_pointer(issue.body)
    if pointer is not None:
        updated["prior_plan_commit"] = pointer["plan_commit"]
    client.add_comment(
        issue,
        _plan_repair_comment(
            _PLAN_REPAIR_PENDING_MARKER,
            updated,
            "Plan repair pending review; worker state checkpointed",
        ),
    )


def _restore_closed_plan_repair(
    *,
    client: GitHubIssueClient,
    metadata: Mapping[str, Any],
    pull_request_number: int,
) -> None:
    plan_id = str(metadata["plan_id"])
    repair_id = str(metadata["repair"]["repair_id"])
    for issue in client.list_issues(labels=(_plan_label(plan_id),), state="all"):
        pointer = parse_issue_pointer(issue.body)
        if (
            pointer is None
            or pointer["item_id"] not in metadata["repair"]["affected_item_ids"]
        ):
            continue
        comments = client.list_comments(issue.number)
        pending = _active_plan_repair_pending(comments)
        is_this_pending = (
            pending is not None
            and pending.get("repair_id") == repair_id
            and pending.get("pull_request_number") == pull_request_number
        )
        if is_this_pending and pending is not None:
            pointer = parse_issue_pointer(issue.body)
            if (
                issue.state == "open"
                and pointer is not None
                and pointer["plan_commit"] == pending.get("prior_plan_commit")
            ):
                prior_status = pending.get("prior_queue_status")
                if prior_status not in QUEUE_LABELS:
                    prior_status = "blocked"
                labels = set(_replace_queue_status(issue.labels, str(prior_status)))
                labels.discard(PLAN_REPAIR_PENDING_LABEL)
                client.update_issue(issue, labels=tuple(sorted(labels)))
            client.add_comment(
                issue,
                _plan_repair_comment(
                    _PLAN_REPAIR_RESOLVED_MARKER,
                    {"repair_id": repair_id, "disposition": "closed_unmerged"},
                    "Plan repair closed without merging",
                ),
            )
        latest_issue = client.get_issue(issue.number)
        if PLAN_REPAIR_PENDING_LABEL in latest_issue.labels:
            client.update_issue(
                latest_issue,
                labels=tuple(
                    sorted(set(latest_issue.labels) - {PLAN_REPAIR_PENDING_LABEL})
                ),
            )


def _reconcile_merged_plan_repair(
    *,
    client: GitHubIssueClient,
    repo_root: Path,
    plan_loader: Callable[..., ReviewedExecutionPlan],
    metadata: Mapping[str, Any],
    pull_request_number: int,
    merge_commit: str,
) -> None:
    plan_id = str(metadata["plan_id"])
    plan_path = str(metadata["plan_path"])
    repair = metadata["repair"]
    repair_id = str(repair["repair_id"])
    plan = plan_loader(plan_path, merge_commit, repo_root=repo_root)
    if plan.plan_id != plan_id:
        raise GitHubIssueQueueError("merged repair changed the plan ID")
    latest_repair = plan.planning.get("latest_repair") if plan.planning else None
    if not isinstance(latest_repair, Mapping) or dict(latest_repair) != dict(repair):
        raise GitHubIssueQueueError(
            "merged plan repair metadata does not match the compiled plan manifest"
        )
    client.ensure_plan_commit_merged(merge_commit)
    client.ensure_queue_labels()
    plan_label = client.ensure_plan_label(plan_id)
    active_items = {item.item_id: item for item in plan.items}
    retired = {
        str(record.get("item_id")): record
        for record in repair["retired_items"]
        if isinstance(record, Mapping) and isinstance(record.get("item_id"), str)
    }
    issues = client.list_issues(labels=(plan_label,), state="all")
    by_item = _plan_issue_index(issues)
    by_number = {issue.number: issue for issue in by_item.values()}
    for item_id in repair["affected_item_ids"]:
        issue = by_item.get((plan_id, item_id))
        item = active_items.get(item_id)
        disposition = retired.get(item_id)
        if issue is None and item is not None:
            initial = "ready" if not item.dependencies else "blocked_on_dependency"
            issue = client.create_issue(
                title=f"Powdrr work item: {plan_id}/{item_id}",
                body=_issue_pointer_body(
                    plan_id,
                    item_id,
                    plan_path,
                    merge_commit,
                    repository=client.repository,
                ),
                labels=(QUEUE_LABELS[initial], plan_label),
            )
            by_item[(plan_id, item_id)] = issue
            by_number[issue.number] = issue
        if issue is None:
            continue
        comments = client.list_comments(issue.number)
        pending = _active_plan_repair_pending(comments)
        prior_status = (
            str(pending.get("prior_queue_status"))
            if pending is not None and pending.get("repair_id") == repair_id
            else None
        )
        marker_key = (repair_id, item_id, merge_commit)
        if _has_repair_reconciliation(comments, marker_key):
            continue

        if item is not None:
            client.update_issue(
                issue,
                body=_issue_pointer_body(
                    plan_id,
                    item_id,
                    plan_path,
                    merge_commit,
                    repository=client.repository,
                ),
            )
        elif disposition is not None and disposition.get("disposition") == "superseded":
            successor_ids = disposition.get("successor_item_ids", [])
            successor_links = [
                by_item[(plan_id, successor)].url
                for successor in successor_ids
                if (plan_id, successor) in by_item
            ]
            was_open = issue.state == "open" or (
                pending is not None
                and pending.get("repair_id") == repair_id
                and pending.get("prior_issue_state") == "open"
            )
            if issue.state == "open":
                client.update_issue(
                    issue,
                    labels=tuple(
                        sorted(set(issue.labels) - set(QUEUE_LABELS.values()))
                    ),
                    state="closed",
                    state_reason="completed",
                )
            if was_open and not _has_repair_notice(
                comments, _PLAN_REPAIR_SUPERSEDED_MARKER, repair_id, item_id
            ):
                client.add_comment(
                    issue,
                    _plan_repair_comment(
                        _PLAN_REPAIR_SUPERSEDED_MARKER,
                        {
                            "repair_id": repair_id,
                            "item_id": item_id,
                            "successor_item_ids": successor_ids,
                            "successor_urls": successor_links,
                        },
                        "Work item superseded by plan repair",
                    ),
                )

        if item is not None:
            new_digest = repair["new_input_contract_digests"].get(item_id)
            if not isinstance(new_digest, str):
                from powdrr_service.planning.repair import (
                    item_input_contract_digest,
                )

                new_digest = item_input_contract_digest(item, plan.planning)
            stale_checkpoints: list[str] = []
            checkpoint_payloads = _task_checkpoint_payloads(comments)
            awaiting_human_unchanged = bool(checkpoint_payloads)
            for checkpoint in checkpoint_payloads:
                task_id = checkpoint.get("task_id")
                digest = checkpoint.get("input_contract_digest")
                if digest != new_digest:
                    awaiting_human_unchanged = False
                    if isinstance(task_id, str):
                        stale_checkpoints.append(task_id)
            keep_awaiting_human = (
                prior_status == "awaiting_human" and awaiting_human_unchanged
            )
            if issue.state == "open":
                if keep_awaiting_human:
                    status = "awaiting_human"
                else:
                    dependencies_closed = all(
                        _dependency_is_fulfilled(
                            by_item.get((plan_id, dependency)),
                            by_number,
                            client=client,
                            plan=plan,
                            dependency_id=dependency,
                        )
                        for dependency in item.dependencies
                    )
                    status = "ready" if dependencies_closed else "blocked_on_dependency"
                issue = client.get_issue(issue.number)
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(issue.labels, status),
                )
            manual_prerequisite_invalidated = (
                prior_status == "awaiting_human" and not awaiting_human_unchanged
            )
            if (stale_checkpoints or manual_prerequisite_invalidated) and not (
                _has_repair_notice(
                    comments,
                    _PLAN_REPAIR_INVALIDATION_MARKER,
                    repair_id,
                    item_id,
                )
            ):
                client.add_comment(
                    issue,
                    _plan_repair_comment(
                        _PLAN_REPAIR_INVALIDATION_MARKER,
                        {
                            "repair_id": repair_id,
                            "item_id": item_id,
                            "invalidated_task_ids": sorted(set(stale_checkpoints)),
                            "manual_prerequisite_invalidated": (
                                manual_prerequisite_invalidated
                            ),
                        },
                        "Task checkpoints invalidated by plan repair",
                    ),
                )
        client.add_comment(
            issue,
            _plan_repair_comment(
                _PLAN_REPAIR_RECONCILED_MARKER,
                {
                    "repair_id": repair_id,
                    "item_id": item_id,
                    "merge_commit": merge_commit,
                    "idempotency_key": list(marker_key),
                },
                "Plan repair reconciled",
            ),
        )

    # Clear pending state after all affected rows have their new revision and
    # labels. A retry can safely finish this loop after any individual API error.
    affected_ids = set(str(item_id) for item_id in repair["affected_item_ids"])
    for item_id in affected_ids:
        issue = by_item.get((plan_id, item_id))
        if issue is None:
            continue
        comments = client.list_comments(issue.number)
        pending = _active_plan_repair_pending(comments)
        if pending is not None and pending.get("repair_id") == repair_id:
            client.add_comment(
                issue,
                _plan_repair_comment(
                    _PLAN_REPAIR_RESOLVED_MARKER,
                    {
                        "repair_id": repair_id,
                        "disposition": "merged",
                        "merge_commit": merge_commit,
                    },
                    "Plan repair applied",
                ),
            )
        latest_issue = client.get_issue(issue.number)
        if PLAN_REPAIR_PENDING_LABEL in latest_issue.labels:
            client.update_issue(
                latest_issue,
                labels=tuple(
                    sorted(set(latest_issue.labels) - {PLAN_REPAIR_PENDING_LABEL})
                ),
            )


def _has_repair_reconciliation(
    comments: Sequence[Mapping[str, Any]], key: tuple[str, str, str]
) -> bool:
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or _PLAN_REPAIR_RECONCILED_MARKER not in body:
            continue
        payload = _json_payload_after_marker(body, _PLAN_REPAIR_RECONCILED_MARKER)
        if (
            isinstance(payload, Mapping)
            and tuple(payload.get("idempotency_key", ())) == key
        ):
            return True
    return False


def _has_repair_notice(
    comments: Sequence[Mapping[str, Any]],
    marker: str,
    repair_id: str,
    item_id: str,
) -> bool:
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        payload = _json_payload_after_marker(body, marker)
        if (
            isinstance(payload, Mapping)
            and payload.get("repair_id") == repair_id
            and payload.get("item_id") == item_id
        ):
            return True
    return False


def _task_checkpoint_payloads(
    comments: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    checkpoints: list[Mapping[str, Any]] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or _TASK_CHECKPOINT_START not in body:
            continue
        payload = _json_payload_after_marker(body, _TASK_CHECKPOINT_START)
        if isinstance(payload, Mapping):
            checkpoints.append(payload)
    return tuple(checkpoints)


def _latest_checkpoint_pull_request_url(
    comments: Sequence[Mapping[str, Any]],
) -> str | None:
    for comment in reversed(comments):
        body = comment.get("body")
        if not isinstance(body, str) or _TASK_CHECKPOINT_START not in body:
            continue
        try:
            payload_text = body.split("```json\n", 1)[1].split("\n```", 1)[0]
            payload = json.loads(payload_text)
        except (IndexError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or payload.get("status") != "completed":
            continue
        output_state = payload.get("output_state")
        if not isinstance(output_state, Mapping):
            continue
        pull_request_url = output_state.get("pull_request_url")
        if isinstance(pull_request_url, str):
            return pull_request_url
    return None


def _pull_request_url_from_task_record(
    record: Mapping[str, Any] | None,
) -> str | None:
    if record is None or record.get("status") != "completed":
        return None
    state = record.get("output_state")
    value = state.get("pull_request_url") if isinstance(state, Mapping) else None
    return value if isinstance(value, str) else None


def _ci_response_result_from_task_record(
    record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Read the structured CI response produced by the completed task workflow."""
    if (
        record is None
        or record.get("status") != "completed"
        or record.get("output_state_type")
        != "execute-proposed-pr-workflow-result-state"
    ):
        return None
    output_state = record.get("output_state")
    result = (
        output_state.get("ci_response_result")
        if isinstance(output_state, Mapping)
        else None
    )
    if not isinstance(result, Mapping):
        return None
    response_state = result.get("response_state")
    head_sha = result.get("head_sha")
    observation_id = result.get("observation_id")
    if (
        result.get("observation_schema_version") != 1
        or not isinstance(response_state, str)
        or response_state
        not in {
            "awaiting_ci",
            "awaiting_human",
            "awaiting_review",
            "failed",
            "blocked",
        }
        or not isinstance(head_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_sha) is None
        or not isinstance(observation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", observation_id) is None
    ):
        return None
    waited_seconds = result.get("waited_seconds", 0)
    if not isinstance(waited_seconds, int) or waited_seconds < 0:
        waited_seconds = 0
    required_checks_status = result.get("required_checks_status")
    if not isinstance(required_checks_status, str) or required_checks_status not in {
        "available",
        "unavailable",
    }:
        required_checks_status = "unavailable"
    normalized = {
        "schema_version": 1,
        "head_sha": head_sha,
        "observation_id": observation_id,
        "response_state": response_state,
        "response_reason": str(result.get("response_reason", ""))[:500],
        "required_checks_status": required_checks_status,
        "waited_seconds": waited_seconds,
        "wait_deadline_reached": result.get("wait_deadline_reached") is True,
    }
    dispatch_status = result.get("repair_dispatch_status")
    if isinstance(dispatch_status, str) and dispatch_status in {
        "accepted",
        "blocked",
        "human_required",
        "interrupted",
        "not_requested",
        "rejected",
    }:
        normalized["repair_dispatch_status"] = dispatch_status
    publication_status = result.get("publication_status")
    if isinstance(publication_status, str) and publication_status in {
        "blocked",
        "not_attempted",
        "published",
    }:
        normalized["publication_status"] = publication_status
    repair_reasons = _normalize_ci_response_reasons(result.get("repair_reasons"))
    publication_reasons = _normalize_ci_response_reasons(
        result.get("publication_reasons")
    )
    if repair_reasons:
        normalized["repair_reasons"] = repair_reasons
    if publication_reasons:
        normalized["publication_reasons"] = publication_reasons
    return normalized


_CI_OUTCOME_MARKER = "<!-- powdrr-ci-outcome:v1 -->"


def _read_repository_validation_inventory(
    repo_root: str | Path,
) -> dict[str, Any] | None:
    """Load typed validation obligations only from a fresh committed baseline."""
    root = Path(repo_root)
    path = root / "docs/structrr/current/baseline-bootstrap.yaml"
    try:
        raw = path.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(document, Mapping):
        return None
    inventory = document.get("validation_inventory")
    if not isinstance(inventory, list):
        return None
    context = document.get("validation_context")
    if not isinstance(context, Mapping):
        return None
    input_fingerprints = context.get("input_fingerprints")
    if not isinstance(input_fingerprints, list):
        return None
    report_path = path.with_suffix(".md")
    freshness_path = path.with_suffix(".freshness.json")
    try:
        report_digest = _sha256_file(report_path)
        baseline_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        freshness = json.loads(freshness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(freshness, Mapping):
        return None
    if (
        freshness.get("schema_version") != "structrr-baseline-freshness-v1"
        or freshness.get("baseline_digest") != baseline_digest
        or freshness.get("report_digest") != report_digest
        or freshness.get("validation_input_fingerprint")
        != content_fingerprint(
            {
                "validation_inventory": inventory,
                "validation_context": context,
            }
        )
    ):
        return None
    for fingerprint in input_fingerprints:
        if not isinstance(fingerprint, Mapping):
            return None
        input_path = fingerprint.get("path")
        input_digest = fingerprint.get("digest")
        if (
            not isinstance(input_path, str)
            or not input_path
            or Path(input_path).is_absolute()
            or ".." in Path(input_path).parts
            or not isinstance(input_digest, str)
        ):
            return None
        try:
            resolved_input = (root / input_path).resolve(strict=True)
            resolved_input.relative_to(root.resolve())
            actual_digest = _sha256_file(resolved_input)
        except (OSError, ValueError):
            return None
        if actual_digest != input_digest:
            return None
    try:
        checks = tuple(
            ValidationCheck.from_data(value).to_data()
            for value in inventory
            if isinstance(value, Mapping)
        )
    except (TypeError, ValueError):
        return None
    if len(checks) != len(inventory):
        return None
    return {
        "checks": checks,
        "digest": baseline_digest,
        "input_paths": tuple(
            str(item["path"])
            for item in input_fingerprints
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        ),
    }


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _pull_request_changed_paths(
    client: GitHubIssueClient, pull_request_number: int
) -> tuple[str, ...] | None:
    """Read every changed PR filename, retaining both sides of renames."""
    endpoint = (
        f"repos/{client.repository}/pulls/{pull_request_number}/files?per_page=100"
    )
    result = client._invoke(["api", "--paginate", endpoint], check=False)
    if result.returncode != 0:
        return None
    try:
        files = _parse_paginated_json_arrays(
            result.stdout, description="pull request file"
        )
    except GitHubIssueQueueError:
        return None
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping):
            return None
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            return None
        paths.add(filename)
        previous_filename = item.get("previous_filename")
        if isinstance(previous_filename, str) and previous_filename:
            paths.add(previous_filename)
    return tuple(sorted(paths))


def _validation_input_changes(
    changed_paths: Sequence[str],
    recorded_input_paths: Sequence[str] | None,
) -> tuple[str, ...]:
    """Return PR paths that can change validation discovery or its evidence."""
    recorded = set(recorded_input_paths or ())
    baseline_paths = {
        "docs/structrr/current/baseline-bootstrap.yaml",
        "docs/structrr/current/baseline-bootstrap.md",
        "docs/structrr/current/baseline-bootstrap.freshness.json",
    }
    return tuple(
        sorted(
            path
            for path in set(changed_paths)
            if path in recorded
            or path in baseline_paths
            or is_validation_evidence_path(path)
        )
    )


def _match_required_check_to_inventory(
    required_check: Mapping[str, Any],
    validation_inventory: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Match a remote required check to one declared workflow job identity."""
    if validation_inventory is None:
        return {
            "status": "unavailable",
            "profile_ids": [],
            "reason": "fresh repository validation inventory is unavailable",
        }
    check_name = required_check.get("name")
    workflow = required_check.get("workflow")
    if not isinstance(check_name, str) or not check_name.strip():
        return {
            "status": "unmatched",
            "profile_ids": [],
            "reason": "required check has no usable name",
        }
    check_name_key = check_name.strip().casefold()
    workflow_key = workflow.strip().casefold() if isinstance(workflow, str) else None
    candidates: dict[tuple[str, str], set[str]] = {}
    for check in validation_inventory:
        profile_id = check.get("id")
        origins = check.get("ci_origins")
        if not isinstance(profile_id, str) or not isinstance(origins, list):
            continue
        for origin in origins:
            if not isinstance(origin, Mapping):
                continue
            job_names = {
                value.strip().casefold()
                for key in ("job_name", "job_id")
                if isinstance((value := origin.get(key)), str) and value.strip()
            }
            if check_name_key not in job_names:
                continue
            workflow_path = origin.get("workflow")
            if not isinstance(workflow_path, str) or not workflow_path.strip():
                continue
            workflow_aliases = {
                workflow_path.strip().casefold(),
                Path(workflow_path).name.casefold(),
                Path(workflow_path).stem.casefold(),
            }
            workflow_name = origin.get("workflow_name")
            if isinstance(workflow_name, str) and workflow_name.strip():
                workflow_aliases.add(workflow_name.strip().casefold())
            if workflow_key is not None and workflow_key not in workflow_aliases:
                continue
            job_id = origin.get("job_id")
            job_identity = (
                workflow_path.strip(),
                job_id.strip()
                if isinstance(job_id, str) and job_id.strip()
                else check_name_key,
            )
            candidates.setdefault(job_identity, set()).add(profile_id)
    if not candidates:
        return {
            "status": "unmatched",
            "profile_ids": [],
            "reason": "required check does not match a discovered workflow job",
        }
    if len(candidates) != 1:
        return {
            "status": "ambiguous",
            "profile_ids": [],
            "reason": "required check matches multiple discovered workflow jobs",
        }
    profile_ids = sorted(next(iter(candidates.values())))
    return {
        "status": "matched",
        "profile_ids": profile_ids,
        "reason": None,
    }


def _observe_required_ci_outcome(
    client: GitHubIssueClient,
    pull_request_url: str | None,
    *,
    validation_inventory: Sequence[Mapping[str, Any]] | None,
    inventory_digest: str | None,
    validation_input_paths: Sequence[str] | None = None,
    previous_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify only current-PR required checks reported by GitHub CLI."""
    observed_at = datetime.now(UTC).isoformat()
    if pull_request_url is None:
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=(),
            head_sha=None,
            reason="the completed workflow did not checkpoint a pull request URL",
            observed_at=observed_at,
            inventory_digest=inventory_digest,
        )
    number = _same_repository_pull_request_number(pull_request_url, client.repository)
    if number is None:
        return _ci_outcome_observation(
            "blocked",
            None,
            (),
            "the checkpointed PR URL does not belong to this repository",
            observed_at,
            inventory_digest,
        )
    head_result = client._invoke(
        ["pr", "view", str(number), "--json", "headRefOid"], check=False
    )
    if head_result.returncode != 0:
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=(),
            head_sha=None,
            reason="GitHub could not report the current PR head",
            observed_at=observed_at,
            inventory_digest=inventory_digest,
        )
    try:
        head_value = json.loads(head_result.stdout)
    except json.JSONDecodeError:
        head_value = None
    head_sha = head_value.get("headRefOid") if isinstance(head_value, Mapping) else None
    if (
        not isinstance(head_sha, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None
    ):
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=(),
            head_sha=None,
            reason="GitHub returned no valid current PR head SHA",
            observed_at=observed_at,
            inventory_digest=inventory_digest,
        )
    checks_result = client._invoke(
        [
            "pr",
            "checks",
            str(number),
            "--required",
            "--json",
            "name,state,workflow",
        ],
        check=False,
    )
    if checks_result.returncode != 0:
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=(),
            head_sha=head_sha,
            reason=(
                "GitHub could not report required check states: "
                + checks_result.stderr.strip()[:500]
            ),
            observed_at=observed_at,
            inventory_digest=inventory_digest,
        )
    try:
        raw_checks = json.loads(checks_result.stdout)
    except json.JSONDecodeError:
        raw_checks = None
    if not isinstance(raw_checks, list) or not raw_checks:
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=(),
            head_sha=head_sha,
            reason="GitHub reported no required checks; review readiness is unknown",
            observed_at=observed_at,
            inventory_digest=inventory_digest,
        )
    checks = [
        {
            "name": str(value.get("name", "")),
            "state": str(value.get("state", "unknown")).casefold(),
            "status": _normalized_required_check_status(
                str(value.get("state", "unknown")).casefold()
            ),
            "workflow": (
                str(value["workflow"]) if value.get("workflow") is not None else None
            ),
            "inventory_match": _match_required_check_to_inventory(
                value, validation_inventory
            ),
        }
        for value in raw_checks
        if isinstance(value, Mapping)
    ]
    changed_paths = _pull_request_changed_paths(client, number)
    if changed_paths is None:
        return _ci_outcome_observation(
            "blocked",
            head_sha,
            checks,
            "GitHub could not report changed PR paths; validation-input freshness "
            "is unknown",
            observed_at,
            inventory_digest,
        )
    validation_input_changes = _validation_input_changes(
        changed_paths, validation_input_paths
    )
    confirmed_result = client._invoke(
        ["pr", "view", str(number), "--json", "headRefOid"], check=False
    )
    try:
        confirmed_value = json.loads(confirmed_result.stdout)
    except json.JSONDecodeError:
        confirmed_value = None
    confirmed_sha = (
        confirmed_value.get("headRefOid")
        if isinstance(confirmed_value, Mapping)
        else None
    )
    if confirmed_result.returncode != 0 or confirmed_sha != head_sha:
        return _stale_ci_outcome_observation(
            previous_observation=previous_observation,
            current_checks=checks,
            head_sha=(confirmed_sha if isinstance(confirmed_sha, str) else head_sha),
            reason="the PR head changed while required checks were being observed",
            observed_at=observed_at,
            inventory_digest=inventory_digest,
            validation_input_changes=validation_input_changes,
        )
    if validation_input_changes:
        outcome = "blocked"
        reason = (
            "the PR changes validation inputs; refresh the committed validation "
            "baseline before review"
        )
    elif validation_inventory is None:
        outcome = "blocked"
        reason = "committed validation inventory is missing, invalid, or stale"
    elif not checks or any(not item["name"] for item in checks):
        outcome = "blocked"
        reason = "GitHub returned incomplete required-check data"
    elif any(not _required_check_inventory_match_is_valid(item) for item in checks):
        outcome = "blocked"
        reason = (
            "one or more required checks could not be matched to the validation "
            "inventory"
        )
    elif any(item["status"] == "failed" for item in checks):
        outcome = "blocked"
        reason = "one or more required checks failed or were canceled"
    elif any(item["status"] == "unknown" for item in checks):
        outcome = "blocked"
        reason = "one or more required checks have an unrecognized state"
    elif all(item["status"] == "passed" for item in checks):
        outcome = "awaiting_review"
        reason = "all required checks passed for the current PR head"
    elif any(item["status"] == "pending" for item in checks):
        outcome = "awaiting_ci"
        reason = "one or more required checks are still pending for the current PR head"
    else:
        outcome = "blocked"
        reason = "GitHub returned an unrecognized required-check state"
    return _ci_outcome_observation(
        outcome,
        head_sha,
        checks,
        reason,
        observed_at,
        inventory_digest,
        validation_input_changes,
    )


def _normalized_required_check_status(state: str) -> str:
    if state in {"pass", "success"}:
        return "passed"
    if state in {"fail", "failure", "cancel", "cancelled", "timed_out"}:
        return "failed"
    if state in {"pending", "queued", "in_progress"}:
        return "pending"
    return "unknown"


def _required_check_inventory_match_is_valid(check: Mapping[str, Any]) -> bool:
    match = check.get("inventory_match")
    return isinstance(match, Mapping) and match.get("status") == "matched"


def _ci_outcome_observation(
    outcome: str,
    head_sha: str | None,
    checks: Sequence[Mapping[str, Any]],
    reason: str,
    observed_at: str,
    inventory_digest: str | None,
    validation_input_changes: Sequence[str] = (),
    *,
    refresh_succeeded: bool = True,
    stale_remote_observations: bool = False,
    last_remote_observed_at: str | None = None,
    preserve_check_timestamps: bool = False,
) -> dict[str, Any]:
    """Return a dated observation set tied to the PR head reviewed by the gate."""
    head_checks = [
        {
            **check,
            "head_sha": check.get("head_sha")
            if preserve_check_timestamps
            else head_sha,
            "observed_at": (
                check.get("observed_at") if preserve_check_timestamps else observed_at
            ),
        }
        for check in checks
    ]
    return {
        "outcome": outcome,
        "head_sha": head_sha,
        "observed_at": observed_at,
        "validation_inventory_digest": inventory_digest,
        "validation_input_changes": list(validation_input_changes),
        "checks": head_checks,
        "reason": reason,
        "refresh_succeeded": refresh_succeeded,
        "stale_remote_observations": stale_remote_observations,
        "last_remote_observed_at": last_remote_observed_at
        or (observed_at if refresh_succeeded else None),
    }


def _stale_ci_outcome_observation(
    *,
    previous_observation: Mapping[str, Any] | None,
    current_checks: Sequence[Mapping[str, Any]],
    head_sha: str | None,
    reason: str,
    observed_at: str,
    inventory_digest: str | None,
    validation_input_changes: Sequence[str] = (),
) -> dict[str, Any]:
    """Preserve last-known remote checks as explicitly stale on refresh failure."""
    prior_checks = (
        previous_observation.get("checks", [])
        if previous_observation is not None
        else []
    )
    source_checks = list(current_checks) or (
        list(prior_checks) if isinstance(prior_checks, list) else []
    )
    previous_last_observed_at = (
        previous_observation.get("last_remote_observed_at")
        if previous_observation is not None
        else None
    )
    if not isinstance(previous_last_observed_at, str):
        previous_last_observed_at = (
            previous_observation.get("observed_at")
            if previous_observation is not None
            else None
        )
    last_observed_at = observed_at if current_checks else previous_last_observed_at
    stale_checks: list[dict[str, Any]] = []
    for check in source_checks:
        stale_check = dict(check)
        stale_check["last_known_status"] = check.get(
            "last_known_status", check.get("status", "unknown")
        )
        stale_check["status"] = "unknown"
        stale_check["freshness"] = "stale"
        stale_check["stale_since"] = check.get("stale_since", observed_at)
        stale_check["stale_reason"] = reason
        if current_checks:
            stale_check["head_sha"] = head_sha
            stale_check["observed_at"] = observed_at
        stale_checks.append(stale_check)
    if head_sha is None and previous_observation is not None:
        previous_head = previous_observation.get("head_sha")
        head_sha = previous_head if isinstance(previous_head, str) else None
    observation = _ci_outcome_observation(
        "blocked",
        head_sha,
        stale_checks,
        reason,
        observed_at,
        inventory_digest,
        validation_input_changes,
        refresh_succeeded=False,
        stale_remote_observations=bool(stale_checks),
        last_remote_observed_at=last_observed_at,
        preserve_check_timestamps=True,
    )
    previous_url = (
        previous_observation.get("pull_request_url")
        if previous_observation is not None
        else None
    )
    if isinstance(previous_url, str):
        observation["pull_request_url"] = previous_url
    return observation


def _queue_status_for_ci_outcome(outcome: str) -> str:
    if outcome == "awaiting_review":
        return "awaiting_review"
    if outcome == "awaiting_ci":
        return "awaiting_ci"
    if outcome == "awaiting_human":
        return "awaiting_human"
    return "blocked"


def _render_ci_outcome_checkpoint(
    *,
    pull_request_url: str | None,
    observation: Mapping[str, Any],
    ci_response_result: Mapping[str, Any] | None = None,
    human_prerequisite: Mapping[str, Any] | None = None,
) -> str:
    checkpoint_url = pull_request_url or observation.get("pull_request_url")
    payload = {
        "schema": "https://powdrr.io/schemas/ci-outcome-checkpoint-v1",
        "pull_request_url": checkpoint_url,
        "head_sha": observation.get("head_sha"),
        "observed_at": observation.get("observed_at"),
        "validation_inventory_digest": observation.get("validation_inventory_digest"),
        "validation_input_changes": observation.get("validation_input_changes", []),
        "outcome": observation.get("outcome"),
        "required_checks": observation.get("checks", []),
        "reason": observation.get("reason", ""),
        "refresh_succeeded": observation.get("refresh_succeeded", True),
        "stale_remote_observations": observation.get(
            "stale_remote_observations", False
        ),
        "last_remote_observed_at": observation.get("last_remote_observed_at"),
    }
    if human_prerequisite is not None:
        reasons = _normalize_ci_response_reasons(human_prerequisite.get("reasons"))
        payload["human_prerequisite"] = {
            "schema_version": 1,
            "reasons": reasons,
        }
    response_result = _normalize_ci_response_for_checkpoint(
        ci_response_result, expected_head_sha=observation.get("head_sha")
    )
    if response_result is not None:
        payload["ci_response_result"] = response_result
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return (
        "### Powdrr required CI outcome\n\n"
        f"{_CI_OUTCOME_MARKER}\n"
        f"Observation `{fingerprint}` for "
        f"{checkpoint_url or 'an unlinked PR'} at head "
        f"`{observation.get('head_sha') or 'unknown'}`: "
        f"**{observation.get('outcome', 'blocked')}**.\n\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        "```"
    )


def _latest_ci_outcome_observation(
    comments: Sequence[Mapping[str, Any]], pull_request_url: str | None
) -> dict[str, Any] | None:
    """Load the most recent intact CI checkpoint, optionally for one PR."""
    for comment in reversed(comments):
        body = comment.get("body")
        if not isinstance(body, str) or _CI_OUTCOME_MARKER not in body:
            continue
        payload_match = re.search(r"```json\s*\n(.*?)\n```", body, re.DOTALL)
        fingerprint_match = re.search(r"Observation `([0-9a-f]{16})`", body)
        if payload_match is None or fingerprint_match is None:
            continue
        try:
            payload = json.loads(payload_match.group(1))
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema")
            != "https://powdrr.io/schemas/ci-outcome-checkpoint-v1"
            or not isinstance(payload.get("pull_request_url"), str)
            or (
                pull_request_url is not None
                and payload.get("pull_request_url") != pull_request_url
            )
            or not isinstance(payload.get("required_checks"), list)
            or not all(
                isinstance(check, Mapping)
                for check in payload.get("required_checks", [])
            )
        ):
            continue
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        if fingerprint != fingerprint_match.group(1):
            continue
        result = {
            "pull_request_url": payload.get("pull_request_url"),
            "outcome": payload.get("outcome"),
            "head_sha": payload.get("head_sha"),
            "observed_at": payload.get("observed_at"),
            "validation_inventory_digest": payload.get("validation_inventory_digest"),
            "validation_input_changes": payload.get("validation_input_changes", []),
            "checks": payload.get("required_checks", []),
            "reason": payload.get("reason", ""),
            "refresh_succeeded": payload.get("refresh_succeeded", True),
            "stale_remote_observations": payload.get(
                "stale_remote_observations", False
            ),
            "last_remote_observed_at": payload.get("last_remote_observed_at"),
        }
        response_result = payload.get("ci_response_result")
        if isinstance(response_result, Mapping):
            normalized_response = _normalize_ci_response_for_checkpoint(
                response_result, expected_head_sha=payload.get("head_sha")
            )
            if normalized_response is not None:
                result["ci_response_result"] = normalized_response
        human_prerequisite = payload.get("human_prerequisite")
        if (
            isinstance(human_prerequisite, Mapping)
            and human_prerequisite.get("schema_version") == 1
        ):
            result["human_prerequisite"] = {
                "schema_version": 1,
                "reasons": _normalize_ci_response_reasons(
                    human_prerequisite.get("reasons")
                ),
            }
        return result
    return None


def _append_ci_outcome_checkpoint(
    *,
    client: GitHubIssueClient,
    issue: QueueIssue,
    pull_request_url: str | None,
    observation: Mapping[str, Any],
    ci_response_result: Mapping[str, Any] | None = None,
    human_prerequisite: Mapping[str, Any] | None = None,
) -> None:
    comment = _render_ci_outcome_checkpoint(
        pull_request_url=pull_request_url,
        observation=observation,
        ci_response_result=ci_response_result,
        human_prerequisite=human_prerequisite,
    )
    fingerprint = re.search(r"Observation `([0-9a-f]{16})`", comment)
    if fingerprint is not None and any(
        isinstance(item.get("body"), str)
        and f"Observation `{fingerprint.group(1)}`" in str(item["body"])
        for item in client.list_comments(issue.number)
    ):
        return
    client.add_comment(issue, comment)


def _normalize_ci_response_for_checkpoint(
    result: Mapping[str, Any] | None, *, expected_head_sha: object
) -> dict[str, Any] | None:
    if result is None:
        return None
    head_sha = result.get("head_sha")
    observation_id = result.get("observation_id")
    response_state = result.get("response_state")
    if (
        not isinstance(head_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_sha) is None
        or not isinstance(observation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", observation_id) is None
        or not isinstance(response_state, str)
        or response_state
        not in {
            "awaiting_ci",
            "awaiting_human",
            "awaiting_review",
            "failed",
            "blocked",
        }
    ):
        return None
    checks_status = result.get("required_checks_status")
    waited_seconds = result.get("waited_seconds", 0)
    if checks_status not in {"available", "unavailable"}:
        checks_status = "unavailable"
    if not isinstance(waited_seconds, int) or waited_seconds < 0:
        waited_seconds = 0
    normalized = {
        "schema_version": 1,
        "head_sha": head_sha,
        "observation_id": observation_id,
        "response_state": response_state,
        "response_reason": str(result.get("response_reason", ""))[:500],
        "required_checks_status": checks_status,
        "waited_seconds": min(waited_seconds, 3600),
        "wait_deadline_reached": result.get("wait_deadline_reached") is True,
        "matches_queue_head": head_sha == expected_head_sha,
    }
    dispatch_status = result.get("repair_dispatch_status")
    if isinstance(dispatch_status, str) and dispatch_status in {
        "accepted",
        "blocked",
        "human_required",
        "interrupted",
        "not_requested",
        "rejected",
    }:
        normalized["repair_dispatch_status"] = dispatch_status
    publication_status = result.get("publication_status")
    if isinstance(publication_status, str) and publication_status in {
        "blocked",
        "not_attempted",
        "published",
    }:
        normalized["publication_status"] = publication_status
    repair_reasons = _normalize_ci_response_reasons(result.get("repair_reasons"))
    publication_reasons = _normalize_ci_response_reasons(
        result.get("publication_reasons")
    )
    if repair_reasons:
        normalized["repair_reasons"] = repair_reasons
    if publication_reasons:
        normalized["publication_reasons"] = publication_reasons
    return normalized


def _normalize_ci_response_reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        " ".join(reason.split())[:240]
        for reason in value[:8]
        if isinstance(reason, str) and reason.strip()
    ]


def _ci_human_prerequisite_from_response(
    result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if result is None or result.get("response_state") != "awaiting_human":
        return None
    reasons = _normalize_ci_response_reasons(result.get("repair_reasons"))
    if not reasons:
        reason = result.get("response_reason")
        reasons = _normalize_ci_response_reasons(
            [reason] if isinstance(reason, str) else []
        )
    return {"schema_version": 1, "reasons": reasons}


def _ci_outcome_note(
    repository: str, issue_number: int, observation: Mapping[str, Any]
) -> str:
    outcome = observation.get("outcome")
    reason = str(observation.get("reason", "Required CI state is unknown."))
    if outcome == "awaiting_review":
        return "All current required PR checks passed. Human review is still required."
    if outcome == "awaiting_ci":
        return (
            f"{reason}. Recheck with `powdrr-lift github-issue-worker-continue "
            f"--repository {repository} --issue-number {issue_number}`."
        )
    return (
        f"Powdrr cannot mark this item ready for review: {reason}. "
        "Inspect the CI outcome checkpoint and resolve the reported state."
    )


def _ci_response_human_note(
    repository: str,
    issue_number: int,
    pull_request_url: str | None,
    response_result: Mapping[str, Any],
) -> str:
    reasons = _normalize_ci_response_reasons(response_result.get("repair_reasons"))
    reason = "; ".join(reasons) or str(
        response_result.get("response_reason")
        or "The CI failure needs human review before a repair can be authorized."
    )
    pull_request = pull_request_url or "the published PR"
    return (
        f"Powdrr paused CI repair for {pull_request}: {reason}. After the "
        "external prerequisite or review is complete, run `powdrr-lift "
        "github-issue-worker-continue --repository "
        f"{repository} --issue-number {issue_number}` to recheck the current PR head."
    )


def _same_repository_pull_request_number(
    url: str | None, repository: str
) -> int | None:
    if url is None:
        return None
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/pull/(\d+)", url
    )
    return int(match.group(1)) if match else None


def parse_issue_pointer(body: str) -> dict[str, str] | None:
    start = body.find(_MARKER_START)
    if start < 0:
        return None
    start += len(_MARKER_START)
    end = body.find(_MARKER_END, start)
    if end < 0:
        return None
    try:
        value = json.loads(body[start:end])
    except json.JSONDecodeError:
        return None
    required = ("plan_id", "item_id", "plan_path", "plan_commit")
    if not isinstance(value, Mapping) or any(
        not isinstance(value.get(key), str) or not value[key] for key in required
    ):
        return None
    return {key: str(value[key]) for key in required}


def _issue_pointer_body(
    plan_id: str,
    item_id: str,
    plan_path: str,
    plan_commit: str,
    *,
    repository: str,
) -> str:
    pointer = {
        "plan_id": plan_id,
        "item_id": item_id,
        "plan_path": plan_path,
        "plan_commit": plan_commit,
    }
    source_url = f"https://github.com/{repository}/blob/{plan_commit}/{plan_path}"
    return (
        f"Reviewed plan: [{plan_path}]({source_url})\n\n"
        f"{_MARKER_START}{json.dumps(pointer, sort_keys=True)}{_MARKER_END}\n"
    )


def _plan_issue_index(
    issues: Sequence[QueueIssue],
) -> dict[tuple[str, str], QueueIssue]:
    indexed: dict[tuple[str, str], QueueIssue] = {}
    for issue in issues:
        pointer = parse_issue_pointer(issue.body)
        if pointer is None:
            continue
        key = pointer["plan_id"], pointer["item_id"]
        if key in indexed:
            raise GitHubIssueQueueError(
                f"duplicate GitHub queue issues exist for {key[0]}/{key[1]}"
            )
        indexed[key] = issue
    return indexed


def _replace_queue_status(labels: Sequence[str], status: str) -> tuple[str, ...]:
    target = QUEUE_LABELS[status]
    queue_labels = set(QUEUE_LABELS.values())
    return tuple(sorted((set(labels) - queue_labels) | {target}))


def _dependency_is_fulfilled(
    issue: QueueIssue | None,
    by_number: Mapping[int, QueueIssue],
    *,
    client: GitHubIssueClient,
    plan: ReviewedExecutionPlan,
    dependency_id: str,
) -> bool:
    if issue is None:
        return False
    current = by_number.get(issue.number, issue)
    if current.state != "closed":
        return False
    if _dependency_has_verified_noop_receipt(
        current, client=client, plan=plan, dependency_id=dependency_id
    ):
        return True
    from powdrr_service.planning.repair import item_input_contract_digest

    item = next((value for value in plan.items if value.item_id == dependency_id), None)
    if item is None:
        return False
    expected_digest = item_input_contract_digest(item, plan.planning)
    pointer = parse_issue_pointer(current.body)
    if pointer is None or pointer["plan_id"] != plan.plan_id:
        return False
    for checkpoint in _task_checkpoint_payloads(client.list_comments(current.number)):
        if (
            checkpoint.get("schema")
            != "https://powdrr.io/schemas/github-issue-task-checkpoint-v1"
            or checkpoint.get("plan_id") != plan.plan_id
            or checkpoint.get("item_id") != dependency_id
            or checkpoint.get("status") != "completed"
            or checkpoint.get("input_contract_digest") != expected_digest
        ):
            continue
        output_state = checkpoint.get("output_state")
        pull_request_url = (
            output_state.get("pull_request_url")
            if isinstance(output_state, Mapping)
            else None
        )
        number = _same_repository_pull_request_number(
            pull_request_url, client.repository
        )
        if number is None:
            continue
        pull_request = client.pull_request(number)
        merge_commit = pull_request.get("merge_commit_sha")
        body = pull_request.get("body")
        closes_dependency = (
            isinstance(body, str)
            and re.search(
                rf"\b(?:close[sd]?|fix(?:es)?|resolve[sd]?)\s+"
                rf"(?:{re.escape(client.repository)}#|#){current.number}\b",
                body,
                re.IGNORECASE,
            )
            is not None
        )
        if (
            pull_request.get("merged") is True
            and closes_dependency
            and isinstance(merge_commit, str)
            and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", merge_commit)
        ):
            return True
    return False


def _dependency_has_verified_noop_receipt(
    issue: QueueIssue,
    *,
    client: GitHubIssueClient,
    plan: ReviewedExecutionPlan,
    dependency_id: str,
) -> bool:
    if issue.state != "closed" or FULFILLED_WITHOUT_PR_LABEL not in issue.labels:
        return False
    pointer = parse_issue_pointer(issue.body)
    if pointer is None or pointer["plan_id"] != plan.plan_id:
        return False
    from powdrr_service.planning.repair import item_input_contract_digest

    planned_item = next(
        (item for item in plan.items if item.item_id == dependency_id), None
    )
    if planned_item is None:
        return False
    expected_digest = item_input_contract_digest(planned_item, plan.planning)
    for comment in client.list_comments(issue.number):
        body = comment.get("body")
        if not isinstance(body, str) or _FULFILLMENT_MARKER not in body:
            continue
        payload = _json_payload_after_marker(body, _FULFILLMENT_MARKER)
        if (
            isinstance(payload, Mapping)
            and payload.get("schema") == _FULFILLMENT_SCHEMA
            and payload.get("fulfillment") == "without_implementation_pr"
            and payload.get("plan_id") == plan.plan_id
            and payload.get("item_id") == dependency_id
            and (
                payload.get("input_contract_digest") == expected_digest
                or (
                    payload.get("input_contract_digest") is None
                    and payload.get("plan_commit") == pointer["plan_commit"]
                )
            )
            and isinstance(payload.get("reason"), str)
            and bool(payload.get("reason", "").strip())
        ):
            return True
    return False


def _no_eligible_work_readiness(
    client: GitHubIssueClient,
    *,
    repo_root: Path,
    plan_loader: Callable[..., ReviewedExecutionPlan],
) -> dict[str, Any]:
    blocking: list[dict[str, Any]] = []
    counts = {"open": 0, "ready": 0, "waiting_dependencies": 0, "other": 0}
    for issue in client.list_issues(state="open"):
        pointer = parse_issue_pointer(issue.body)
        if pointer is None:
            continue
        counts["open"] += 1
        if (
            QUEUE_LABELS["ready"] not in issue.labels
            and QUEUE_LABELS["blocked_on_dependency"] not in issue.labels
        ):
            counts["other"] += 1
            continue
        plan = plan_loader(
            pointer["plan_path"], pointer["plan_commit"], repo_root=repo_root
        )
        item = next(
            (
                candidate
                for candidate in plan.items
                if candidate.item_id == pointer["item_id"]
            ),
            None,
        )
        if item is None or plan.plan_id != pointer["plan_id"]:
            blocking.append(
                {
                    "issue_number": issue.number,
                    "item_id": pointer["item_id"],
                    "reason": "invalid_plan_item",
                    "explanation": "the item is missing from its pinned plan",
                }
            )
            continue
        index = _plan_issue_index(
            client.list_issues(labels=(_plan_label(plan.plan_id),))
        )
        by_number = {value.number: value for value in index.values()}
        unmet = [
            dependency
            for dependency in item.dependencies
            if not _dependency_is_fulfilled(
                index.get((plan.plan_id, dependency)),
                by_number,
                client=client,
                plan=plan,
                dependency_id=dependency,
            )
        ]
        if unmet:
            counts["waiting_dependencies"] += 1
            blocking.append(
                {
                    "issue_number": issue.number,
                    "item_id": item.item_id,
                    "reason": "dependency_not_fulfilled",
                    "dependencies": unmet,
                    "explanation": (
                        f"waiting for verified fulfillment of {', '.join(unmet)}"
                    ),
                }
            )
        else:
            counts["ready"] += 1
            blocking.append(
                {
                    "issue_number": issue.number,
                    "item_id": item.item_id,
                    "reason": "not_claimed",
                    "dependencies": [],
                    "explanation": (
                        "eligible item was not claimed during this worker pass"
                    ),
                }
            )
    return {"counts": counts, "blocking_reasons": blocking}


@contextmanager
def _worker_lock(repo_root: Path) -> Iterator[_WorkerCoordination]:
    lock_path = _worker_store_root(repo_root) / "coordination.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    coordination = _WorkerCoordination(lock_path)
    try:
        coordination.acquire(blocking=False)
        yield coordination
    except Timeout as error:
        raise GitHubIssueQueueError(
            "another issue worker is already running"
        ) from error
    finally:
        coordination.release()


def _issue_from_api(text: str) -> QueueIssue:
    try:
        value = _json_mapping(text, "issue")
    except GitHubIssueQueueError:
        raise
    labels = value.get("labels", [])
    return QueueIssue(
        number=int(value["number"]),
        title=str(value.get("title", "")),
        body=str(value.get("body", "")),
        state=str(value.get("state", "open")).lower(),
        labels=tuple(
            str(label.get("name")) for label in labels if isinstance(label, Mapping)
        )
        if isinstance(labels, list)
        else (),
        url=str(value.get("html_url", "")),
    )


def _label_color(status: str) -> str:
    return {
        "ready": "0E8A16",
        "claimed": "1D76DB",
        "awaiting_review": "FBCA04",
        "awaiting_human": "C5DEF5",
        "awaiting_ci": "D4C5F9",
        "blocked_on_dependency": "D4C5F9",
        "blocked": "D93F0B",
    }[status]


def _plan_label(plan_id: str) -> str:
    """Return a short, deterministic GitHub label for a plan ID."""
    digest = hashlib.sha256(plan_id.encode("utf-8")).hexdigest()[:32]
    return f"powdrr-plan-{digest}"


def _flatten_pages(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubIssueQueueError("GitHub API response must be a list")
    if not value or not all(isinstance(page, list) for page in value):
        return value
    return [item for page in value for item in page]


def _parse_paginated_json_arrays(text: str, *, description: str) -> list[Any]:
    """Decode the JSON arrays emitted page by page by ``gh api --paginate``."""
    decoder = json.JSONDecoder()
    pages: list[list[Any]] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        try:
            value, cursor = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError(
                f"gh returned malformed paginated {description} JSON"
            ) from error
        if not isinstance(value, list):
            raise GitHubIssueQueueError(
                f"GitHub paginated {description} response must be a list"
            )
        pages.append(value)
    return _flatten_pages(pages)


def _json_mapping(text: str, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise GitHubIssueQueueError(
            f"GitHub returned malformed {description} JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise GitHubIssueQueueError(f"GitHub {description} response must be an object")
    return value


def _safe_id(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(value)) and ".." not in value.split("/")


def _validate_plan_reference(plan_path: str, plan_commit: str) -> None:
    if (
        not plan_path
        or plan_path.startswith("/")
        or ".." in plan_path.split("/")
        or posixpath.normpath(plan_path) != plan_path
    ):
        raise GitHubIssueQueueError(
            "plan path must be a normalized repository-relative path"
        )
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", plan_commit):
        raise GitHubIssueQueueError("plan commit must be a full Git object ID")
