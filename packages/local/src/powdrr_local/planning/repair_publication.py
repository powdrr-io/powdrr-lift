"""Local GitHub publication effects for service-prepared plan repairs."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from powdrr_lift.workrr.git import slugify_workflow_id


class RepairPublicationError(RuntimeError):
    """A prepared plan repair could not be published safely."""


def publish_repair_pull_request(
    *,
    repo_root: Path,
    publication: Mapping[str, Any],
    runner: Any = subprocess.run,
) -> tuple[str, tuple[str, ...]]:
    """Publish a service-prepared repair without depending on service models."""
    plan_id = _required_text(publication.get("plan_id"), "plan_id")
    repair_id = _required_text(publication.get("repair_id"), "repair_id")
    summary = _required_text(publication.get("summary"), "summary")
    body = _required_text(publication.get("body"), "body")
    raw_changed = publication.get("changed")
    artifacts = publication.get("artifacts")
    if (
        not isinstance(raw_changed, list)
        or not raw_changed
        or not all(isinstance(path, str) for path in raw_changed)
        or len(set(raw_changed)) != len(raw_changed)
        or not isinstance(artifacts, Mapping)
        or set(raw_changed) != set(artifacts)
        or not all(
            isinstance(path, str) and isinstance(content, str)
            for path, content in artifacts.items()
        )
    ):
        raise RepairPublicationError("prepared repair artifact set is malformed")
    changed = tuple(raw_changed)
    for relative in changed:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RepairPublicationError(
                f"prepared repair artifact path is invalid: {relative}"
            )

    root = repo_root.resolve()
    destinations: dict[str, Path] = {}
    for relative in changed:
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise RepairPublicationError(
                f"repair artifact escapes repository: {relative}"
            ) from error
        destinations[relative] = destination

    _run(runner, root, ["git", "fetch", "origin", "main"])
    if _run(runner, root, ["git", "status", "--porcelain"]):
        raise RepairPublicationError(
            "plan repair publication requires a clean checkout"
        )
    head = _run(runner, root, ["git", "rev-parse", "HEAD"])
    base = _run(runner, root, ["git", "rev-parse", "origin/main"])
    if head != base:
        raise RepairPublicationError(
            "plan repair publication requires HEAD to match fresh origin/main"
        )

    branch = (
        f"powdrr/{slugify_workflow_id(plan_id)}-repair-"
        f"{slugify_workflow_id(repair_id)}-{uuid.uuid4().hex[:8]}"
    )
    _run(runner, root, ["git", "switch", "--detach", "origin/main"])
    _run(runner, root, ["git", "switch", "-c", branch])
    for relative in changed:
        destination = destinations[relative]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifacts[relative], encoding="utf-8")
    _run(runner, root, ["git", "add", "--", *changed])
    _run(
        runner,
        root,
        [
            "git",
            "-c",
            "user.name=Powdrr Automation",
            "-c",
            "user.email=powdrr-automation@users.noreply.github.com",
            "commit",
            "--only",
            "-m",
            f"Repair reviewed plan {plan_id}",
            "--",
            *changed,
        ],
    )
    _run(runner, root, ["git", "push", "--set-upstream", "origin", branch])
    title_summary = " ".join(summary.split())[:65]
    url = _run(
        runner,
        root,
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"Correct plan: {title_summary}",
            "--body",
            body,
        ],
    )
    url = next(
        (line.strip() for line in reversed(url.splitlines()) if "/pull/" in line), ""
    )
    if not url:
        raise RepairPublicationError("gh pr create did not return a pull request URL")
    return url, changed


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepairPublicationError(f"prepared repair {name} is invalid")
    return value


def _run(runner: Any, root: Path, arguments: list[str]) -> str:
    result = runner(arguments, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RepairPublicationError(
            f"command failed ({' '.join(arguments)}): {result.stderr.strip()}"
        )
    return result.stdout.strip()
