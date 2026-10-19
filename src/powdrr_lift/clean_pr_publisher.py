"""Publish selected implementation files from a clean branch."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.errors import PowdrrExecutionError


def verify_publication_scope(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> dict[str, Any]:
    """Check the complete proposed file list without relying on an LLM review."""
    try:
        paths = _relative_paths(parameters.get("paths"))
    except PowdrrExecutionError as exc:
        return {"confirmed": False, "findings": [str(exc)]}
    root = worktree_root.resolve()
    findings: list[str] = []
    for relative in paths:
        parts = Path(relative).parts
        if any(
            part in {".git", ".powdrr", "__pycache__", ".pytest_cache"}
            for part in parts
        ):
            findings.append(
                f"Generated or repository-internal path is not publishable: {relative}"
            )
            continue
        candidate = root / relative
        try:
            candidate.parent.resolve().relative_to(root)
        except ValueError:
            findings.append(f"Publication path escapes the worktree: {relative}")
            continue
        if not candidate.exists() and not candidate.is_symlink():
            findings.append(
                f"Publication path is missing from the worktree: {relative}"
            )
        elif candidate.is_dir() and not candidate.is_symlink():
            findings.append(f"Publication path is a directory, not a file: {relative}")
    return {"confirmed": not findings, "findings": findings, "paths": paths}


def publish_clean_pull_request(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> str:
    """Copy only approved files to a clean publication branch and open a PR."""
    files = _relative_paths(parameters.get("files_to_publish"))
    base = _branch(parameters.get("base"), field="base")
    branch = _branch(parameters.get("publication_branch"), field="publication_branch")
    if not branch.startswith("powdrr/"):
        raise PowdrrExecutionError(
            "publication_branch must be scoped under the powdrr/ namespace."
        )
    title = _required_text(parameters.get("title"), "title")
    body = _required_text(parameters.get("body"), "body")
    commit_message = _required_text(parameters.get("commit_message"), "commit_message")
    source_root = worktree_root.resolve()
    source_branch = _run(["git", "branch", "--show-current"], cwd=source_root).strip()
    if not source_branch:
        raise PowdrrExecutionError("Cannot publish from a detached source worktree.")

    with tempfile.TemporaryDirectory(prefix="powdrr-publication-") as temp_root:
        publication_root = Path(temp_root) / "worktree"
        base_ref = _resolve_base_ref(source_root, base)
        _prepare_publication_worktree(
            source_root, publication_root, base_ref=base_ref, branch=branch
        )
        try:
            _copy_publication_files(source_root, publication_root, files)
            _run(["git", "add", "--", *files], cwd=publication_root)
            staged = _run(
                ["git", "diff", "--cached", "--name-only"], cwd=publication_root
            ).splitlines()
            if not set(staged).issubset(set(files)):
                raise PowdrrExecutionError(
                    "Clean publication branch staged a different file set: "
                    f"expected {sorted(files)!r}, found {sorted(staged)!r}."
                )
            if staged:
                _run(["git", "commit", "-m", commit_message], cwd=publication_root)
            committed = set(
                _run(
                    ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
                    cwd=publication_root,
                ).splitlines()
            )
            if committed != set(files):
                raise PowdrrExecutionError(
                    "Clean publication branch contains paths outside the approved "
                    f"file set: {sorted(committed ^ set(files))!r}."
                )
            _run(
                ["git", "push", "--set-upstream", "origin", branch],
                cwd=publication_root,
            )
            url = _create_or_update_pull_request(
                publication_root,
                branch=branch,
                base=base,
                title=title,
                body=body,
            )
            return url
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(publication_root)],
                cwd=source_root,
                capture_output=True,
                text=True,
                check=False,
            )


def _prepare_publication_worktree(
    source_root: Path,
    publication_root: Path,
    *,
    base_ref: str,
    branch: str,
) -> None:
    existing = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        _run(
            ["git", "worktree", "add", str(publication_root), branch],
            cwd=source_root,
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"],
            cwd=publication_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise PowdrrExecutionError(
                f"Existing publication branch {branch!r} is not based on {base_ref!r}."
            )
        return
    _run(
        ["git", "worktree", "add", "--detach", str(publication_root), base_ref],
        cwd=source_root,
    )
    _run(["git", "switch", "-c", branch], cwd=publication_root)


def _copy_publication_files(
    source_root: Path, publication_root: Path, files: Sequence[str]
) -> None:
    for relative in files:
        source = source_root / relative
        try:
            source.parent.resolve().relative_to(source_root)
        except ValueError as exc:
            raise PowdrrExecutionError(
                f"Publication path escapes the source worktree: {relative!r}."
            ) from exc
        if not source.exists() and not source.is_symlink():
            raise PowdrrExecutionError(
                f"Publication file does not exist in the source worktree: {relative!r}."
            )
        destination = publication_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()
        if source.is_symlink():
            destination.symlink_to(source.readlink())
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise PowdrrExecutionError(
                f"Publication path is not a regular file: {relative!r}."
            )


def _create_or_update_pull_request(
    publication_root: Path,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> str:
    existing = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,state"],
        cwd=publication_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        try:
            payload = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise PowdrrExecutionError(
                "GitHub returned invalid JSON while looking up the publication PR."
            ) from exc
        if isinstance(payload, Mapping) and payload.get("state") in {"OPEN", "DRAFT"}:
            _run(
                ["gh", "pr", "edit", branch, "--title", title, "--body", body],
                cwd=publication_root,
            )
            url = payload.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
            raise PowdrrExecutionError("Existing publication PR has no valid URL.")
    output = _run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=publication_root,
    ).strip()
    if not output.startswith("https://") or "/pull/" not in output:
        raise PowdrrExecutionError(
            "GitHub did not return a pull-request URL for the clean publication branch."
        )
    return output


def _resolve_base_ref(source_root: Path, base: str) -> str:
    _run(["git", "fetch", "--no-tags", "origin", base], cwd=source_root)
    remote_ref = f"refs/remotes/origin/{base}"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return remote_ref
    local_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if local_ref.returncode == 0:
        return f"refs/heads/{base}"
    raise PowdrrExecutionError(
        f"Cannot resolve publication base branch {base!r} from origin or local refs."
    )


def _run(command: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PowdrrExecutionError(
            f"Command failed while publishing a clean PR ({command[0]}): {detail}"
        )
    return result.stdout


def _relative_paths(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PowdrrExecutionError("files_to_publish must be an array of paths.")
    if not value:
        raise PowdrrExecutionError("files_to_publish must not be empty.")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PowdrrExecutionError("files_to_publish contains an invalid path.")
        path = Path(item.strip())
        if path.is_absolute() or ".." in path.parts:
            raise PowdrrExecutionError(
                "files_to_publish paths must stay inside the repository."
            )
        paths.append(path.as_posix())
    if len(paths) != len(set(paths)):
        raise PowdrrExecutionError("files_to_publish must not contain duplicates.")
    return paths


def _branch(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"{field} must be a non-empty branch name.")
    branch = value.strip()
    if any(character in branch for character in "..~^:?*[\\ "):
        raise PowdrrExecutionError(f"{field} is not a safe branch name.")
    return branch


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"{field} must be a non-empty string.")
    return value
