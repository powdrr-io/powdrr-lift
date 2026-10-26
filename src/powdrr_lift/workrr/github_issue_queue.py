"""GitHub Issues queue for execution items described by a merged plan document.

The plan document owns work meaning. GitHub Issues own mutable execution state
and contain only stable pointers back to the reviewed plan revision.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import quote

import yaml

QUEUE_SCHEMA = "https://powdrr.io/schemas/github-issue-execution-plan-v1"
QUEUE_LABELS = {
    "ready": "powdrr:ready",
    "claimed": "powdrr:claimed",
    "awaiting_review": "powdrr:awaiting-review",
    "awaiting_human": "powdrr:awaiting-human",
    "blocked_on_dependency": "powdrr:blocked-on-dependency",
    "blocked": "powdrr:blocked",
}
FULFILLED_WITHOUT_PR_LABEL = "powdrr:fulfilled-without-pr"
MANUAL_PREREQUISITE_TRIGGER = "after_implementation_before_validation"
_MARKER_START = "<!-- powdrr-queue-item:v1 "
_MARKER_END = " -->"
_TASK_CHECKPOINT_START = "<!-- powdrr-task-checkpoint:v1 -->"
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
_PLAN_AMENDMENT_TITLE_PREFIX = "Correct plan:"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
_GITHUB_STATUS_SETTLE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class PlannedWorkItem:
    item_id: str
    title: str
    objective: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    workflow_directory: str | None
    workflow_id: str
    workflow_file: str | None = None
    implementation_steps: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = (".",)
    manual_prerequisite: Mapping[str, Any] | None = None
    inline_adjustments: tuple[Mapping[str, Any], ...] = ()
    github_issue_number: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewedExecutionPlan:
    plan_id: str
    items: tuple[PlannedWorkItem, ...]
    source_document: str | None = None


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


TaskCheckpointCallback = Callable[[str, str, Any, str | None], None]


class GitHubIssueQueueError(RuntimeError):
    """Invalid queue data or failed GitHub operation."""


class GitHubIssueClient:
    """Small ``gh api`` adapter for issue status and plan pointers."""

    def __init__(
        self,
        repository: str,
        *,
        executable: str = "gh",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            raise GitHubIssueQueueError("repository must use owner/name form")
        self.repository = repository
        self.executable = executable
        self._run = run

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
        self._api(
            "POST",
            f"repos/{self.repository}/issues/{issue.number}/comments",
            "-f",
            f"body={body}",
        )

    def list_comments(self, issue_number: int) -> tuple[Mapping[str, Any], ...]:
        result = self._invoke(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repository}/issues/{issue_number}/comments?per_page=100",
            ]
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
    """Parse and validate a GitHub issue execution plan document."""
    return _parse_plan(text)


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
    return _parse_plan(result.stdout)


def publish_plan_issues(
    *,
    plan: ReviewedExecutionPlan,
    plan_path: str,
    plan_commit: str,
    client: GitHubIssueClient,
) -> PublishReport:
    """Create one minimal pointer issue per plan item; reruns reuse issue IDs."""
    _validate_plan_reference(plan_path, plan_commit)
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
        status = "ready" if not item.dependencies else "blocked_on_dependency"
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
            _dependency_is_closed(existing.get((plan.plan_id, dependency)), by_number)
            for dependency in item.dependencies
        )
        status = "ready" if dependencies_closed else "blocked_on_dependency"
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
) -> WorkerReport:
    """Claim and execute the first ready item whose dependencies are closed."""
    root = Path(repo_root).resolve()
    with _worker_lock(root):
        _requeue_merged_plan_amendments(
            client=client,
            repo_root=root,
            plan_loader=plan_loader,
        )
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
                if not _dependency_is_closed(
                    indexed.get((plan.plan_id, dependency)), by_number
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
            client.update_issue(
                issue,
                labels=_replace_queue_status(issue.labels, "claimed"),
            )
            client.add_comment(issue, f"Powdrr worker {worker_id} claimed this item.")
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
                item = replace(item, github_issue_number=issue.number)
                item_by_id = {candidate.item_id: candidate for candidate in plan.items}
                fulfilled_without_pr_dependencies = tuple(
                    item_by_id[dependency].workflow_id
                    for dependency in item.dependencies
                    if (
                        (dependency_issue := indexed.get((plan.plan_id, dependency)))
                        is not None
                        and dependency_issue.state == "closed"
                        and FULFILLED_WITHOUT_PR_LABEL in dependency_issue.labels
                    )
                )
                # The task runner owns its retry policy. Keep the GitHub issue
                # untouched while it retries, then publish one final status below.
                return_code = run_task(
                    workflow_path,
                    item.workflow_id,
                    worker_id,
                    item,
                    plan,
                    fulfilled_without_pr_dependencies,
                    _task_checkpoint_callback(
                        repo_root=root,
                        issue=issue,
                        plan=plan,
                        item=item,
                    ),
                )
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
                    status = "awaiting_review"
                    outcome = "awaiting_review"
                    note = "Powdrr execution finished. Human review is still required."
            else:
                status = "blocked"
                outcome = "blocked"
                note = "Powdrr execution did not finish successfully."
                if detail:
                    note += f" {detail}"
            client.update_issue(
                issue,
                labels=_replace_queue_status(
                    _replace_queue_status(issue.labels, "claimed"), status
                ),
            )
            client.add_comment(issue, note)
            return WorkerReport(outcome, issue.number, item.item_id, detail)
    return WorkerReport("no_eligible_work")


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
    with _worker_lock(Path(repo_root).resolve()):
        return _continue_manual_prerequisite_issue_locked(
            client=client,
            issue_number=issue_number,
            repo_root=Path(repo_root).resolve(),
            resume_workflow=resume_workflow,
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
        _write_local_task_checkpoint(
            repo_root=repo_root,
            issue_number=issue_number,
            checkpoint=TaskCheckpoint(
                plan_id=pointer["plan_id"],
                item_id=pointer["item_id"],
                workflow_id=str(checkpoint["workflow_id"]),
                task_id=task_id,
                output_state_type=output_state_type,
                output_state=output_state,
                commit_sha=commit_sha,
            ),
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
    if pending_prerequisite is not None:
        status = "awaiting_human"
        outcome = "awaiting_human"
        note = (
            "Powdrr paused for another manual prerequisite. Follow the updated "
            "instructions on the staged pull request, then run "
            "`powdrr-lift github-issue-worker-continue --repository "
            f"{client.repository} --issue-number {issue.number}`."
        )
    elif return_code == 0:
        status = "awaiting_review"
        outcome = "awaiting_review"
        note = "Powdrr execution finished. Human review is still required."
    else:
        status = "blocked"
        outcome = "blocked"
        note = "Powdrr execution did not finish successfully."
        if detail:
            note += f" {detail}"
    client.update_issue(
        issue,
        labels=_replace_queue_status(issue.labels, status),
    )
    client.add_comment(issue, note)
    return WorkerReport(outcome, issue_number, detail=detail)


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
                if (
                    not normalized
                    or len(normalized) > 20
                    or any(
                        Path(path).is_absolute() or ".." in Path(path).parts
                        for path in normalized
                    )
                ):
                    raise GitHubIssueQueueError(
                        "allowed_paths must contain at most 20 "
                        "repository-relative paths"
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
    *, plan_id: str, item_id: str, reason: str
) -> str:
    payload = {
        "schema": _FULFILLMENT_SCHEMA,
        "plan_id": plan_id,
        "item_id": item_id,
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


def _task_checkpoint_callback(
    *,
    repo_root: Path,
    issue: QueueIssue,
    plan: ReviewedExecutionPlan,
    item: PlannedWorkItem,
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
        _write_local_task_checkpoint(
            repo_root=repo_root,
            issue_number=target_issue.number,
            checkpoint=TaskCheckpoint(
                plan_id=target_plan.plan_id,
                item_id=target_item.item_id,
                workflow_id=target_item.workflow_id,
                task_id=task_id,
                output_state_type=output_state_type,
                output_state=output_state,
                commit_sha=commit_sha,
            ),
        )

    return persist


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
            _dependency_is_closed(indexed.get((plan.plan_id, dependency)), by_number)
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
    """Repin work items after a reviewed plan-amendment PR merges."""
    awaiting = client.list_issues(
        labels=(QUEUE_LABELS["awaiting_review"],), state="open"
    )
    for issue in sorted(awaiting, key=lambda value: value.number):
        if issue.state != "open" or QUEUE_LABELS["awaiting_review"] not in issue.labels:
            continue
        pointer = parse_issue_pointer(issue.body)
        if pointer is None:
            continue
        pull_request_url = _latest_checkpoint_pull_request_url(
            client.list_comments(issue.number)
        )
        pull_request_number = _same_repository_pull_request_number(
            pull_request_url, client.repository
        )
        if pull_request_number is None:
            continue
        pull_request = client.pull_request(pull_request_number)
        title = pull_request.get("title")
        if not isinstance(title, str) or not title.startswith(
            _PLAN_AMENDMENT_TITLE_PREFIX
        ):
            continue
        if pull_request.get("merged") is not True:
            continue
        merge_commit = pull_request.get("merge_commit_sha")
        if not isinstance(merge_commit, str) or not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", merge_commit
        ):
            raise GitHubIssueQueueError(
                f"merged plan-amendment PR #{pull_request_number} has no merge commit"
            )
        plan = plan_loader(pointer["plan_path"], merge_commit, repo_root=repo_root)
        if plan.plan_id != pointer["plan_id"]:
            raise GitHubIssueQueueError(
                f"merged plan-amendment PR #{pull_request_number} changed the plan ID"
            )
        item = next(
            (
                candidate
                for candidate in plan.items
                if candidate.item_id == pointer["item_id"]
            ),
            None,
        )
        if item is None:
            raise GitHubIssueQueueError(
                "merged plan-amendment PR "
                f"#{pull_request_number} removed the issue item"
            )
        client.ensure_plan_commit_merged(merge_commit)
        updated_body = _issue_pointer_body(
            pointer["plan_id"],
            pointer["item_id"],
            pointer["plan_path"],
            merge_commit,
            repository=client.repository,
        )
        indexed = _plan_issue_index(
            client.list_issues(labels=(_plan_label(plan.plan_id),))
        )
        by_number = {value.number: value for value in indexed.values()}
        dependencies_closed = all(
            _dependency_is_closed(indexed.get((plan.plan_id, dependency)), by_number)
            for dependency in item.dependencies
        )
        status = "ready" if dependencies_closed else "blocked_on_dependency"
        client.update_issue(
            issue,
            body=updated_body,
            labels=_replace_queue_status(issue.labels, status),
        )
        client.add_comment(
            issue,
            "Powdrr detected that plan-amendment PR "
            f"https://github.com/{client.repository}/pull/{pull_request_number} "
            f"merged. This issue now points to the amended plan at `{merge_commit}` "
            f"and was requeued as `{status.replace('_', ' ')}` for execution.",
        )


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


def _parse_plan(text: str) -> ReviewedExecutionPlan:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise GitHubIssueQueueError(f"invalid plan YAML: {error}") from error
    if not isinstance(data, Mapping) or data.get("schema") != QUEUE_SCHEMA:
        raise GitHubIssueQueueError(f"plan schema must be {QUEUE_SCHEMA!r}")
    if set(data) not in (
        {"schema", "plan_id", "items"},
        {"schema", "plan_id", "source_document", "items"},
    ):
        raise GitHubIssueQueueError("plan has unknown or missing top-level fields")
    plan_id = data.get("plan_id")
    source_document = data.get("source_document")
    if source_document is not None and (
        not isinstance(source_document, str)
        or not source_document.strip()
        or Path(source_document).is_absolute()
        or ".." in Path(source_document).parts
    ):
        raise GitHubIssueQueueError(
            "source_document must be a repository-relative path without traversal"
        )
    raw_items = data.get("items")
    if not isinstance(plan_id, str) or not _safe_id(plan_id):
        raise GitHubIssueQueueError("plan_id must be a stable safe identifier")
    if not isinstance(raw_items, list) or not raw_items:
        raise GitHubIssueQueueError("plan items must be a non-empty list")
    items: list[PlannedWorkItem] = []
    for index, raw in enumerate(raw_items):
        field = f"items[{index}]"
        if not isinstance(raw, Mapping):
            raise GitHubIssueQueueError(f"{field} must be a mapping")
        common_fields = {
            "id",
            "title",
            "objective",
            "scope",
            "acceptance_criteria",
            "dependencies",
            "workflow_id",
        }
        workflow_keys = set(raw) & {"workflow_directory", "workflow_file"}
        optional_keys = set(raw) - common_fields - workflow_keys
        if (
            not common_fields <= set(raw)
            or len(workflow_keys) != 1
            or not optional_keys
            <= {"implementation_steps", "allowed_paths", "manual_prerequisite"}
        ):
            raise GitHubIssueQueueError(f"{field} has unknown or missing fields")
        item_id = raw.get("id")
        workflow_dir = raw.get("workflow_directory")
        workflow_file = raw.get("workflow_file")
        workflow_id = raw.get("workflow_id")
        if not isinstance(item_id, str) or not _safe_id(item_id):
            raise GitHubIssueQueueError(f"{field}.id must be a stable safe identifier")
        workflow_reference = (
            workflow_file if workflow_file is not None else workflow_dir
        )
        if not isinstance(workflow_reference, str) or not workflow_reference:
            raise GitHubIssueQueueError(f"{field}.workflow_file is required")
        if (
            Path(workflow_reference).is_absolute()
            or ".." in Path(workflow_reference).parts
        ):
            raise GitHubIssueQueueError(
                f"{field}.workflow_file must stay in the repository"
            )
        if not isinstance(workflow_id, str) or not _safe_id(workflow_id):
            raise GitHubIssueQueueError(
                f"{field}.workflow_id must be a stable safe identifier"
            )
        item = PlannedWorkItem(
            item_id=item_id,
            title=_required_text(raw, "title", field),
            objective=_required_text(raw, "objective", field),
            scope=_string_list(raw, "scope", field),
            acceptance_criteria=_string_list(raw, "acceptance_criteria", field),
            dependencies=_string_list(raw, "dependencies", field, allow_empty=True),
            workflow_directory=workflow_dir if isinstance(workflow_dir, str) else None,
            workflow_id=workflow_id,
            workflow_file=workflow_file if isinstance(workflow_file, str) else None,
            implementation_steps=(
                _string_list(raw, "implementation_steps", field)
                if "implementation_steps" in raw
                else ()
            ),
            allowed_paths=(
                _repository_path_list(raw, field) if "allowed_paths" in raw else (".",)
            ),
            manual_prerequisite=(
                parse_manual_prerequisite(raw["manual_prerequisite"], field)
                if "manual_prerequisite" in raw
                else None
            ),
        )
        if not item.scope or not item.acceptance_criteria:
            raise GitHubIssueQueueError(
                f"{field} must declare scope and acceptance criteria"
            )
        if "implementation_steps" in raw and (
            len(item.scope) > 5 or not 1 <= len(item.implementation_steps) <= 8
        ):
            raise GitHubIssueQueueError(
                f"{field} exceeds the bounded scope or implementation-step limits"
            )
        items.append(item)
    ids = [item.item_id for item in items]
    if len(set(ids)) != len(ids):
        raise GitHubIssueQueueError("plan item IDs must be unique")
    known = set(ids)
    for item in items:
        if (
            len(set(item.dependencies)) != len(item.dependencies)
            or item.item_id in item.dependencies
            or not set(item.dependencies) <= known
        ):
            raise GitHubIssueQueueError(
                f"item {item.item_id!r} has a self or unknown dependency"
            )
    if _has_dependency_cycle(items):
        raise GitHubIssueQueueError("plan dependencies must be acyclic")
    return ReviewedExecutionPlan(plan_id, tuple(items), source_document)


def _required_text(raw: Mapping[str, Any], key: str, field: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GitHubIssueQueueError(f"{field}.{key} must be non-empty text")
    return value.strip()


def _string_list(
    raw: Mapping[str, Any], key: str, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    value = raw.get(key, [] if allow_empty else None)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GitHubIssueQueueError(
            f"{field}.{key} must be a list of non-empty strings"
        )
    return tuple(item.strip() for item in value)


def _repository_path_list(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    paths = _string_list(raw, "allowed_paths", field)
    if (
        not paths
        or len(set(paths)) != len(paths)
        or any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths)
    ):
        raise GitHubIssueQueueError(
            f"{field}.allowed_paths must contain unique repository-relative paths"
        )
    return paths


def parse_manual_prerequisite(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "title",
        "instructions",
        "verification",
        "trigger",
        "unblocks",
    }:
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite must contain exactly title, instructions, "
            "verification, trigger, and unblocks"
        )
    title = value.get("title")
    instructions = value.get("instructions")
    verification = value.get("verification")
    trigger = value.get("trigger")
    unblocks = value.get("unblocks")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite.title must be non-empty text of at most "
            "160 characters"
        )
    if (
        not isinstance(instructions, list)
        or not instructions
        or not all(isinstance(item, str) and item.strip() for item in instructions)
    ):
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite.instructions must be a non-empty list of "
            "non-empty strings"
        )
    if not isinstance(verification, str) or not verification.strip():
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite.verification must be non-empty text"
        )
    if trigger != MANUAL_PREREQUISITE_TRIGGER:
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite.trigger must be "
            f"{MANUAL_PREREQUISITE_TRIGGER!r}"
        )
    if not isinstance(unblocks, str) or not unblocks.strip():
        raise GitHubIssueQueueError(
            f"{field}.manual_prerequisite.unblocks must be non-empty text"
        )
    return {
        "title": title.strip(),
        "instructions": [item.strip() for item in instructions],
        "verification": verification.strip(),
        "trigger": trigger,
        "unblocks": unblocks.strip(),
    }


def _has_dependency_cycle(items: Sequence[PlannedWorkItem]) -> bool:
    dependencies = {item.item_id: set(item.dependencies) for item in items}
    active: set[str] = set()
    done: set[str] = set()

    def visit(item_id: str) -> bool:
        if item_id in active:
            return True
        if item_id in done:
            return False
        active.add(item_id)
        if any(visit(dep) for dep in dependencies[item_id]):
            return True
        active.remove(item_id)
        done.add(item_id)
        return False

    return any(visit(item_id) for item_id in dependencies)


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


def _dependency_is_closed(
    issue: QueueIssue | None, by_number: Mapping[int, QueueIssue]
) -> bool:
    if issue is None:
        return False
    current = by_number.get(issue.number, issue)
    return current.state == "closed"


@contextmanager
def _worker_lock(repo_root: Path) -> Iterator[None]:
    common_dir = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if common_dir.returncode == 0:
        common_path = Path(common_dir.stdout.strip())
        if not common_path.is_absolute():
            common_path = repo_root / common_path
        lock_path = common_path.resolve() / "powdrr-github-issue-worker.lock"
    else:
        lock_path = repo_root / ".powdrr" / "github-issue-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GitHubIssueQueueError(
                "another issue worker is already running"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
