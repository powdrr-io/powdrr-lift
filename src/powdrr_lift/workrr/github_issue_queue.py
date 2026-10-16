"""GitHub Issues queue for execution items described by a merged plan document.

The plan document owns work meaning. GitHub Issues own mutable execution state
and contain only stable pointers back to the reviewed plan revision.
"""

from __future__ import annotations

import fcntl
import json
import posixpath
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

QUEUE_SCHEMA = "https://powdrr.io/schemas/github-issue-execution-plan-v1"
QUEUE_LABELS = {
    "ready": "powdrr:ready",
    "claimed": "powdrr:claimed",
    "awaiting_review": "powdrr:awaiting-review",
    "blocked": "powdrr:blocked",
}
_MARKER_START = "<!-- powdrr-queue-item:v1 "
_MARKER_END = " -->"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")


@dataclass(frozen=True, slots=True)
class PlannedWorkItem:
    item_id: str
    title: str
    objective: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    workflow_directory: str
    workflow_id: str


@dataclass(frozen=True, slots=True)
class ReviewedExecutionPlan:
    plan_id: str
    items: tuple[PlannedWorkItem, ...]


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
class WorkerReport:
    outcome: str
    issue_number: int | None = None
    item_id: str | None = None
    detail: str | None = None


class GitHubIssueQueueError(RuntimeError):
    """Invalid queue data or failed GitHub operation."""


class GitHubIssueClient:
    """Small ``gh api`` adapter; all mutable queue state remains on GitHub."""

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

    def list_issues(self) -> tuple[QueueIssue, ...]:
        result = self._invoke(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repository}/issues?state=all&per_page=100",
            ]
        )
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError("gh returned malformed issue JSON") from error
        issue_values = _flatten_pages(values)
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
            issues.append(
                QueueIssue(
                    number=int(value["number"]),
                    title=str(value.get("title", "")),
                    body=str(value.get("body", "")),
                    state=str(value.get("state", "OPEN")).lower(),
                    labels=labels,
                    url=str(value.get("url", "")),
                )
            )
        return tuple(issues)

    def create_issue(self, *, body: str, label: str, title: str) -> QueueIssue:
        result = self._api(
            "POST",
            f"repos/{self.repository}/issues",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
            "-f",
            f"labels[]={label}",
        )
        return _issue_from_api(result.stdout)

    def ensure_queue_labels(self) -> None:
        result = self._invoke(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repository}/labels?per_page=100",
            ]
        )
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubIssueQueueError("gh returned malformed label JSON") from error
        existing = {
            str(item.get("name"))
            for item in _flatten_pages(values)
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        for status, label in QUEUE_LABELS.items():
            if label in existing:
                continue
            created = self._invoke(
                [
                    "api",
                    f"repos/{self.repository}/labels",
                    "--method",
                    "POST",
                    "-f",
                    f"name={label}",
                    "-f",
                    f"color={_label_color(status)}",
                    "-f",
                    f"description=Powdrr queue state: {status.replace('_', ' ')}",
                ],
                check=False,
            )
            if created.returncode != 0 and "already_exists" not in created.stderr:
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
    ) -> None:
        arguments = ["PATCH", f"repos/{self.repository}/issues/{issue.number}"]
        if body is not None:
            arguments.extend(("-f", f"body={body}"))
        if labels is not None:
            for label in labels:
                arguments.extend(("-f", f"labels[]={label}"))
        self._api(*arguments)

    def add_comment(self, issue: QueueIssue, body: str) -> None:
        self._api(
            "POST",
            f"repos/{self.repository}/issues/{issue.number}/comments",
            "-f",
            f"body={body}",
        )

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
    return _parse_plan(path.read_text(encoding="utf-8"))


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
    existing = _plan_issue_index(client.list_issues())
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
        created_issue = client.create_issue(
            title=f"Powdrr work item: {plan.plan_id}/{item.item_id}",
            body=body,
            label=QUEUE_LABELS["ready"],
        )
        created.append(created_issue.number)
        existing[key] = created_issue
    return PublishReport(plan.plan_id, plan_commit, tuple(created), tuple(reused))


def run_worker_once(
    *,
    client: GitHubIssueClient,
    repo_root: str | Path,
    worker_id: str,
    run_task: Callable[[Path, str, str], int],
    plan_loader: Callable[..., ReviewedExecutionPlan] = load_reviewed_plan_at_commit,
) -> WorkerReport:
    """Claim and execute the first ready item whose dependencies are closed."""
    root = Path(repo_root).resolve()
    with _worker_lock(root):
        issues = client.list_issues()
        indexed = _plan_issue_index(issues)
        by_number = {issue.number: issue for issue in issues}
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
            unmet = [
                dependency
                for dependency in item.dependencies
                if not _dependency_is_closed(
                    indexed.get((plan.plan_id, dependency)), by_number
                )
            ]
            if unmet:
                continue
            issue = next(
                value for value in client.list_issues() if value.number == issue.number
            )
            if QUEUE_LABELS["ready"] not in issue.labels:
                continue
            client.update_issue(
                issue,
                labels=_replace_queue_status(issue.labels, "claimed"),
            )
            client.add_comment(issue, f"Powdrr worker {worker_id} claimed this item.")
            workflow_dir = (root / item.workflow_directory).resolve()
            detail: str | None = None
            if not workflow_dir.is_relative_to(root) or not workflow_dir.is_dir():
                client.update_issue(
                    issue,
                    labels=_replace_queue_status(
                        _replace_queue_status(issue.labels, "claimed"), "blocked"
                    ),
                )
                detail = f"workflow directory is missing: {item.workflow_directory}"
                client.add_comment(issue, f"Powdrr worker blocked this item: {detail}.")
                return WorkerReport("blocked", issue.number, item.item_id, detail)
            try:
                return_code = run_task(workflow_dir, item.workflow_id, worker_id)
            except Exception as error:
                return_code = 1
                detail = f"{type(error).__name__}: {error}"
            if return_code == 0:
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
    if set(data) != {"schema", "plan_id", "items"}:
        raise GitHubIssueQueueError("plan has unknown or missing top-level fields")
    plan_id = data.get("plan_id")
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
        if set(raw) != {
            "id",
            "title",
            "objective",
            "scope",
            "acceptance_criteria",
            "dependencies",
            "workflow_directory",
            "workflow_id",
        }:
            raise GitHubIssueQueueError(f"{field} has unknown or missing fields")
        item_id = raw.get("id")
        workflow_dir = raw.get("workflow_directory")
        workflow_id = raw.get("workflow_id")
        if not isinstance(item_id, str) or not _safe_id(item_id):
            raise GitHubIssueQueueError(f"{field}.id must be a stable safe identifier")
        if not isinstance(workflow_dir, str) or not workflow_dir:
            raise GitHubIssueQueueError(f"{field}.workflow_directory is required")
        if Path(workflow_dir).is_absolute() or ".." in Path(workflow_dir).parts:
            raise GitHubIssueQueueError(
                f"{field}.workflow_directory must stay in the repository"
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
            workflow_directory=workflow_dir,
            workflow_id=workflow_id,
        )
        if not item.scope or not item.acceptance_criteria:
            raise GitHubIssueQueueError(
                f"{field} must declare scope and acceptance criteria"
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
    return ReviewedExecutionPlan(plan_id, tuple(items))


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
        "blocked": "D93F0B",
    }[status]


def _flatten_pages(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubIssueQueueError("GitHub API response must be a list")
    if not value or not all(isinstance(page, list) for page in value):
        return value
    return [item for page in value for item in page]


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
