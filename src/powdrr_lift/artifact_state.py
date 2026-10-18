"""Read a commit-pinned inventory of Powdrr repository artifacts."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.core.index import ChangelogDocument
from powdrr_lift.workrr.github_issue_queue import parse_reviewed_plan_yaml


class ArtifactStateError(ValueError):
    """The requested repository revision cannot be inspected."""


def build_artifact_state(
    repo_root: str | Path,
    *,
    branch: str,
    sha: str,
    repository: str | None = None,
    include_github: bool = True,
) -> dict[str, Any]:
    """Build a read-only artifact inventory for a commit reachable from a branch."""
    root = Path(repo_root).resolve()
    commit = _git(root, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    branch_commit = _git(root, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    if not commit or not branch_commit:
        raise ArtifactStateError(f"could not resolve branch {branch!r} and sha {sha!r}")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, branch_commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ArtifactStateError(
            f"commit {commit} is not reachable from branch {branch!r}"
        )

    paths = _git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
    bootstrap_path, bootstrap, bootstrap_commit_time = _load_bootstrap(
        root, paths, commit
    )
    changelog_documents = _load_changelogs(root, paths, commit)
    entities = _materialize_entities(
        root, bootstrap, changelog_documents, bootstrap_commit_time
    )
    relationships = _materialize_relationships(
        root, bootstrap, changelog_documents, bootstrap_commit_time
    )
    workflows = _load_workflows(root, paths, commit)
    plans = _load_plans(root, paths, commit, workflows)

    repository_name = repository or _github_repository(root)
    github_data: dict[str, Any] = {
        "repository": repository_name,
        "source": "live_github_api" if include_github else "skipped",
        "observed_at": datetime.now(UTC).isoformat() if include_github else None,
        "available": False,
        "error": None,
        "pull_requests": [],
    }
    if include_github and repository_name:
        github_data["pull_requests"], github_data["available"], github_data["error"] = (
            _load_pull_requests(repository_name)
        )
        for plan in plans:
            if github_data["available"]:
                matching = _pull_requests_for_plan(github_data["pull_requests"], plan)
                plan["pull_requests"] = matching
                plan["pull_request_count"] = len(matching)

    changelogs = [
        {
            "pr_number": document.pr_number,
            "commit_sha": document.commit_sha,
            "title": document.changelog.title,
            "change_id": document.changelog.change_id,
            "path": str(document.changelog_path.relative_to(root)),
            "entities": [
                {"id": item.id, "type": item.type, "action": item.action}
                for item in document.changelog.entity_changes
            ],
            "entity_relationships": [
                {
                    "id": item.id,
                    "source": item.source,
                    "target": item.target,
                    "relationship": item.relationship,
                    "action": item.action,
                }
                for item in document.changelog.entity_relationship_changes
            ],
            "document": _read_yaml_at(
                root, commit, str(document.changelog_path.relative_to(root))
            ),
        }
        for document in changelog_documents
    ]
    return {
        "schema": "https://powdrr.io/schemas/artifact-state-v1",
        "repository": repository_name,
        "branch": branch,
        "sha": commit,
        "bootstrap": {
            "path": bootstrap_path,
            "commit_timestamp": bootstrap_commit_time,
            "present": bootstrap is not None,
            "document": dict(bootstrap) if bootstrap is not None else None,
        },
        "entities": entities,
        "relationships": relationships,
        "workflows": workflows,
        "plans": plans,
        "changelogs": changelogs,
        "github": github_data,
        "summary": {
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "workflow_count": len(workflows),
            "published_workflow_count": sum(
                1 for workflow in workflows if workflow["published"]
            ),
            "plan_count": len(plans),
            "changelog_count": len(changelogs),
        },
    }


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ArtifactStateError(result.stderr.strip() or "git command failed")
    return result.stdout


def _read_yaml_at(root: Path, commit: str, path: str) -> object:
    try:
        return yaml.safe_load(_git(root, "show", f"{commit}:{path}"))
    except (yaml.YAMLError, ArtifactStateError):
        return None


def _load_bootstrap(
    root: Path, paths: list[str], commit: str
) -> tuple[str | None, Mapping[str, Any] | None, int | None]:
    candidates = [
        path
        for path in paths
        if path.startswith("docs/structrr/current/baseline-") and path.endswith(".yaml")
    ]
    ranked: list[tuple[int, str, Mapping[str, Any]]] = []
    for path in candidates:
        document = _read_yaml_at(root, commit, path)
        if not isinstance(document, Mapping) or document.get("structrr_schema") != (
            "https://powdrr.io/schema/structrr-bootstrap-v1"
        ):
            continue
        result = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%ct", commit, "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        timestamp = (
            int(result.stdout.strip())
            if result.returncode == 0 and result.stdout.strip()
            else 0
        )
        ranked.append((timestamp, path, document))
    if not ranked:
        return None, None, None
    timestamp, path, document = max(ranked)
    return path, document, timestamp


def _load_changelogs(
    root: Path, paths: list[str], commit: str
) -> list[ChangelogDocument]:
    changelog_paths = [
        path
        for path in paths
        if re.fullmatch(r"docs/changelogs/PR-\d+-changelog\.yaml", path)
    ]
    history: dict[int, tuple[str, int, str]] = {}
    for record in _git(
        root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x1f%ct%x1f%s",
        commit,
    ).splitlines():
        parts = record.split("\x1f", maxsplit=2)
        if len(parts) != 3:
            continue
        commit_sha, timestamp_text, subject = parts
        match = re.search(r"#(\d+)", subject)
        if match is None:
            continue
        history[int(match.group(1))] = (commit_sha, int(timestamp_text), subject)

    documents: list[ChangelogDocument] = []
    for path in sorted(changelog_paths):
        match = re.fullmatch(r"docs/changelogs/PR-(\d+)-changelog\.yaml", path)
        if match is None:
            continue
        pr_number = int(match.group(1))
        commit_record = history.get(pr_number)
        if commit_record is None:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "log",
                    "-1",
                    "--format=%H%x1f%ct%x1f%s",
                    commit,
                    "--",
                    path,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            parts = result.stdout.strip().split("\x1f", maxsplit=2)
            if result.returncode == 0 and len(parts) == 3:
                commit_record = (parts[0], int(parts[1]), parts[2])
        content = _git(root, "show", f"{commit}:{path}")
        documents.append(
            ChangelogDocument(
                pr_number=pr_number,
                changelog_path=root / path,
                changelog=parse_change_log(content),
                commit_sha=commit_record[0] if commit_record else None,
                commit_timestamp=commit_record[1] if commit_record else None,
                commit_subject=commit_record[2] if commit_record else None,
            )
        )
    return documents


def _materialize_entities(
    root: Path,
    bootstrap: Mapping[str, Any] | None,
    documents: list[ChangelogDocument],
    bootstrap_timestamp: int | None,
) -> list[dict[str, Any]]:
    entities: dict[str, dict[str, Any]] = {}
    if bootstrap is not None:
        for item in bootstrap.get("entities", []):
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                entities[str(item["id"])] = {
                    **dict(item),
                    "source": {"kind": "bootstrap", "change_id": "bootstrap"},
                }
    for document in sorted(
        documents,
        key=lambda item: (item.commit_timestamp or 0, item.pr_number),
    ):
        if (
            bootstrap_timestamp is not None
            and document.commit_timestamp is not None
            and document.commit_timestamp <= bootstrap_timestamp
        ):
            continue
        for change in document.changelog.entity_changes:
            if not isinstance(change.id, str) or not change.id:
                continue
            if change.action in {"deleted", "removed"}:
                entities.pop(change.id, None)
                continue
            previous = entities.get(change.id, {})
            entities[change.id] = {
                **previous,
                "id": change.id,
                "type": change.type or previous.get("type"),
                "action": change.action,
                "source": {
                    "kind": "changelog",
                    "change_id": document.changelog.change_id,
                    "pr_number": document.pr_number,
                    "commit_sha": document.commit_sha,
                    "path": str(document.changelog_path.relative_to(root)),
                },
            }
    return [entities[key] for key in sorted(entities)]


def _materialize_relationships(
    root: Path,
    bootstrap: Mapping[str, Any] | None,
    documents: list[ChangelogDocument],
    bootstrap_timestamp: int | None,
) -> list[dict[str, Any]]:
    relationships: dict[str, dict[str, Any]] = {}
    if bootstrap is not None:
        for item in bootstrap.get("entity_relationships", []):
            if not isinstance(item, Mapping):
                continue
            source = item.get("source")
            target = item.get("target")
            relationship = item.get("relationship")
            if not all(isinstance(value, str) and value for value in (source, target)):
                continue
            key = str(item.get("id") or f"{source}|{relationship}|{target}")
            relationships[key] = {
                **dict(item),
                "source": {"kind": "bootstrap", "change_id": "bootstrap"},
            }
    for document in sorted(
        documents,
        key=lambda item: (item.commit_timestamp or 0, item.pr_number),
    ):
        if (
            bootstrap_timestamp is not None
            and document.commit_timestamp is not None
            and document.commit_timestamp <= bootstrap_timestamp
        ):
            continue
        for change in document.changelog.entity_relationship_changes:
            if not change.source or not change.target:
                continue
            key = str(
                change.id or f"{change.source}|{change.relationship}|{change.target}"
            )
            if change.action in {"removed", "deleted"}:
                relationships.pop(key, None)
                continue
            relationships[key] = {
                "id": change.id,
                "source_id": change.source,
                "target_id": change.target,
                "relationship": change.relationship,
                "description": change.description,
                "rationale": change.rationale,
                "action": change.action,
                "source": {
                    "kind": "changelog",
                    "change_id": document.changelog.change_id,
                    "pr_number": document.pr_number,
                    "commit_sha": document.commit_sha,
                    "path": str(document.changelog_path.relative_to(root)),
                },
            }
    return [relationships[key] for key in sorted(relationships)]


def _load_workflows(root: Path, paths: list[str], commit: str) -> list[dict[str, Any]]:
    workflows_by_id: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.startswith("docs/workflows/") or not path.endswith(
            (".yaml", ".yml")
        ):
            continue
        data = _read_yaml_at(root, commit, path)
        if not isinstance(data, Mapping):
            continue
        workflow_id = _first_string(data, "workflow_id", "proposed_pr_id")
        if workflow_id is None and isinstance(data.get("task_id"), str):
            workflow_id = str(data["task_id"]).split("-task-", maxsplit=1)[0]
        if workflow_id is None:
            continue
        workflow = workflows_by_id.setdefault(
            workflow_id,
            {
                "id": workflow_id,
                "published": True,
                "plan_id": _nested_string(
                    data, "inputs", "proposed_pr_context", "plan_id"
                ),
                "status": None,
                "artifacts": [],
                "tasks": [],
            },
        )
        task_id = _first_string(data, "task_id")
        status = _first_string(data, "status", "state")
        workflow["artifacts"].append(
            {
                "path": path,
                "kind": "plan_instance"
                if data.get("process_id")
                else "workflow_task"
                if task_id
                else "workflow_state",
                "content": dict(data),
            }
        )
        if task_id:
            workflow["tasks"].append({"id": task_id, "status": status, "path": path})
        elif status is not None:
            workflow["status"] = status
    workflows = list(workflows_by_id.values())
    for workflow in workflows:
        workflow["artifacts"].sort(key=lambda item: item["path"])
        workflow["tasks"].sort(key=lambda item: item["id"])
        workflow["task_count"] = len(workflow["tasks"])
    return sorted(workflows, key=lambda item: item["id"])


def _load_plans(
    root: Path, paths: list[str], commit: str, workflows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    workflow_paths = {
        artifact["path"] for workflow in workflows for artifact in workflow["artifacts"]
    }
    known_plans: set[str] = set()
    plans: list[dict[str, Any]] = []
    for path in paths:
        if not path.startswith("docs/plans/"):
            continue
        if path.endswith(".md"):
            plan_id = Path(path).stem
            known_plans.add(plan_id)
            attached_workflows = [
                workflow for workflow in workflows if workflow.get("plan_id") == plan_id
            ]
            plans.append(
                {
                    "id": plan_id,
                    "kind": "planning_document",
                    "path": path,
                    "source_document": path,
                    "published": True,
                    "document": _git(root, "show", f"{commit}:{path}"),
                    "items": [],
                    "workflow_count": len(attached_workflows),
                    "published_workflow_count": sum(
                        1 for workflow in attached_workflows if workflow["published"]
                    ),
                    "pull_requests": [],
                    "pull_request_count": None,
                }
            )
            continue
        if path.endswith(".json"):
            plan_id = Path(path).stem
            known_plans.add(plan_id)
            plans.append(
                {
                    "id": plan_id,
                    "kind": "planning_document",
                    "path": path,
                    "source_document": path,
                    "published": True,
                    "document": _git(root, "show", f"{commit}:{path}"),
                    "items": [],
                    "workflow_count": sum(
                        1
                        for workflow in workflows
                        if workflow.get("plan_id") == plan_id
                    ),
                    "published_workflow_count": sum(
                        1
                        for workflow in workflows
                        if workflow.get("plan_id") == plan_id and workflow["published"]
                    ),
                    "pull_requests": [],
                    "pull_request_count": None,
                }
            )
            continue
        if not path.endswith((".yaml", ".yml")):
            continue
        content = _git(root, "show", f"{commit}:{path}")
        try:
            plan = parse_reviewed_plan_yaml(content)
        except (ValueError, yaml.YAMLError):
            plan_id = Path(path).stem
            known_plans.add(plan_id)
            plans.append(
                {
                    "id": plan_id,
                    "kind": "planning_document",
                    "path": path,
                    "source_document": path,
                    "published": True,
                    "document": yaml.safe_load(content),
                    "items": [],
                    "workflow_count": sum(
                        1
                        for workflow in workflows
                        if workflow.get("plan_id") == plan_id
                    ),
                    "published_workflow_count": sum(
                        1
                        for workflow in workflows
                        if workflow.get("plan_id") == plan_id and workflow["published"]
                    ),
                    "pull_requests": [],
                    "pull_request_count": None,
                }
            )
            continue
        known_plans.add(plan.plan_id)
        items = []
        for item in plan.items:
            workflow_path = item.workflow_file
            if workflow_path is None and item.workflow_id:
                workflow_path = f"docs/workflows/{plan.plan_id}/{item.workflow_id}.yaml"
            attached = workflow_path in workflow_paths if workflow_path else False
            items.append(
                {
                    "id": item.item_id,
                    "title": item.title,
                    "objective": item.objective,
                    "scope": list(item.scope),
                    "acceptance_criteria": list(item.acceptance_criteria),
                    "workflow_id": item.workflow_id,
                    "workflow_path": workflow_path,
                    "workflow_attached": attached,
                    "workflow_published": attached,
                    "dependencies": list(item.dependencies),
                }
            )
        plans.append(
            {
                "id": plan.plan_id,
                "kind": "execution_plan",
                "path": path,
                "source_document": plan.source_document,
                "published": True,
                "document": yaml.safe_load(content),
                "items": items,
                "workflow_count": sum(1 for item in items if item["workflow_attached"]),
                "published_workflow_count": sum(
                    1 for item in items if item["workflow_published"]
                ),
                "pull_requests": [],
                "pull_request_count": None,
            }
        )
    for workflow in workflows:
        candidate_plan_id = workflow.get("plan_id")
        if not isinstance(candidate_plan_id, str) or candidate_plan_id in known_plans:
            continue
        plans.append(
            {
                "id": candidate_plan_id,
                "kind": "workflow_plan",
                "path": None,
                "source_document": None,
                "published": True,
                "items": [],
                "workflow_count": sum(
                    1 for item in workflows if item.get("plan_id") == candidate_plan_id
                ),
                "published_workflow_count": sum(
                    1
                    for item in workflows
                    if item.get("plan_id") == candidate_plan_id and item["published"]
                ),
                "pull_requests": [],
                "pull_request_count": None,
            }
        )
        known_plans.add(candidate_plan_id)
    return sorted(plans, key=lambda item: item["id"])


def _github_repository(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    match = re.search(
        r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", result.stdout.strip()
    )
    return match.group(1) if match else None


def _load_pull_requests(
    repository: str,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        return [], False, "repository must use owner/name form"
    endpoint = f"repos/{repository}/pulls?state=all&per_page=100"
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [], False, result.stderr.strip() or "GitHub CLI request failed"
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], False, "GitHub CLI returned malformed pull request JSON"
    values = [item for page in pages if isinstance(page, list) for item in page]
    return (
        [
            {
                "number": value.get("number"),
                "title": value.get("title"),
                "url": value.get("html_url"),
                "state": "merged" if value.get("merged_at") else value.get("state"),
                "body": value.get("body") or "",
                "head_branch": (value.get("head") or {}).get("ref"),
            }
            for value in values
            if isinstance(value, Mapping)
        ],
        True,
        None,
    )


def _pull_requests_for_plan(
    pull_requests: list[dict[str, Any]], plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    plan_id = str(plan.get("id", ""))
    workflow_ids = {
        str(item.get("workflow_id"))
        for item in plan.get("items", [])
        if isinstance(item, Mapping) and item.get("workflow_id")
    }
    matches = []
    for pull_request in pull_requests:
        searchable = " ".join(
            str(pull_request.get(key, "")) for key in ("title", "body", "head_branch")
        )
        if _contains_identifier(searchable, plan_id) or any(
            _contains_identifier(searchable, workflow_id)
            for workflow_id in workflow_ids
        ):
            matches.append(
                {key: value for key, value in pull_request.items() if key != "body"}
            )
    return matches


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    return (
        re.search(
            rf"(?<![A-Za-z0-9._/-]){re.escape(identifier)}(?![A-Za-z0-9._/-])",
            text,
            re.IGNORECASE,
        )
        is not None
    )


def _first_string(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested_string(data: Mapping[str, Any], *keys: str) -> str | None:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current.strip() if isinstance(current, str) and current.strip() else None
