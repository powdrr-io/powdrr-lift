"""Explicit-path local checkpoints for benchmark candidate trees."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass(frozen=True, slots=True)
class CandidateCheckpoint:
    submission_base: str
    previous_revision: str
    candidate_revision: str
    committed_paths: tuple[str, ...]
    cumulative_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    committed: bool

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": "candidate-checkpoint-v1",
            "submission_base": self.submission_base,
            "previous_revision": self.previous_revision,
            "candidate_revision": self.candidate_revision,
            "committed_paths": list(self.committed_paths),
            "cumulative_paths": list(self.cumulative_paths),
            "excluded_paths": list(self.excluded_paths),
            "committed": self.committed,
        }


def checkpoint_candidate_changes(
    root: str | Path,
    *,
    submission_base: str,
    allowed_paths: Sequence[str],
    artifact_exclusions: Sequence[str],
    message: str,
    runner: Runner = subprocess.run,
) -> CandidateCheckpoint:
    """Commit only scoped product changes and retain cumulative base history.

    A no-op candidate does not create an empty commit. The full index is checked
    before commit so a previously staged artifact cannot hitchhike in this
    checkpoint.
    """
    root_path = Path(root).resolve()
    if not submission_base.strip() or not message.strip():
        raise ValueError("submission_base and checkpoint message are required")
    if not allowed_paths or any(not item.strip() for item in allowed_paths):
        raise ValueError("candidate checkpoint requires explicit allowed paths")
    previous_revision = _git_text(runner, root_path, ["rev-parse", "HEAD"])
    _require_ancestor(runner, root_path, submission_base, previous_revision)
    changed_paths = _status_paths(runner, root_path)
    excluded = tuple(
        sorted(
            path for path in changed_paths if _is_excluded(path, artifact_exclusions)
        )
    )
    product_paths = tuple(
        sorted(path for path in changed_paths if path not in set(excluded))
    )
    out_of_scope = tuple(
        path for path in product_paths if not _within_allowed_paths(path, allowed_paths)
    )
    if out_of_scope:
        raise ValueError(
            "candidate checkpoint has changes outside allowed product paths: "
            f"{list(out_of_scope)}"
        )

    staged_before = _git_paths(
        runner, root_path, ["diff", "--cached", "--name-only", "-z", "--no-renames"]
    )
    staged_excluded = tuple(
        path for path in staged_before if _is_excluded(path, artifact_exclusions)
    )
    if staged_excluded:
        raise ValueError(
            "artifact paths are already staged and must be unstaged before checkpoint: "
            f"{list(staged_excluded)}"
        )
    unexpected_staged = tuple(
        path for path in staged_before if path not in product_paths
    )
    if unexpected_staged:
        raise ValueError(
            "staged paths are not part of the candidate checkpoint: "
            f"{list(unexpected_staged)}"
        )

    if product_paths:
        _git_run(runner, root_path, ["add", "-A", "--", *product_paths])
    staged_after = _git_paths(
        runner, root_path, ["diff", "--cached", "--name-only", "-z", "--no-renames"]
    )
    if set(staged_after) != set(product_paths):
        raise ValueError(
            "staged candidate paths do not match the validated product paths: "
            f"expected={list(product_paths)}, staged={list(staged_after)}"
        )

    if staged_after:
        _git_run(
            runner,
            root_path,
            [
                "-c",
                "user.name=Powdrr Automation",
                "-c",
                "user.email=powdrr-automation@users.noreply.github.com",
                "commit",
                "-m",
                message,
            ],
        )
        candidate_revision = _git_text(runner, root_path, ["rev-parse", "HEAD"])
        _require_ancestor(runner, root_path, submission_base, candidate_revision)
        committed = True
    else:
        candidate_revision = previous_revision
        committed = False

    cumulative_paths = tuple(
        sorted(
            _git_paths(
                runner,
                root_path,
                [
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    submission_base,
                    candidate_revision,
                ],
            )
        )
    )
    return CandidateCheckpoint(
        submission_base=submission_base,
        previous_revision=previous_revision,
        candidate_revision=candidate_revision,
        committed_paths=tuple(staged_after) if committed else (),
        cumulative_paths=cumulative_paths,
        excluded_paths=excluded,
        committed=committed,
    )


def _status_paths(runner: Runner, root: Path) -> tuple[str, ...]:
    output = _git_bytes(
        runner,
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
    )
    records = output.split(b"\0")
    paths: set[str] = set()
    for record in records:
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise ValueError("Git returned malformed porcelain status output")
        paths.add(record[3:].decode("utf-8", errors="surrogateescape"))
    return tuple(sorted(paths))


def _git_paths(runner: Runner, root: Path, args: Sequence[str]) -> tuple[str, ...]:
    output = _git_bytes(runner, root, list(args))
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    )


def _git_text(runner: Runner, root: Path, args: Sequence[str]) -> str:
    return _git_run(runner, root, list(args)).stdout.strip()


def _git_run(
    runner: Runner, root: Path, args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    result = runner(
        ["git", "-C", str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result


def _git_bytes(runner: Runner, root: Path, args: Sequence[str]) -> bytes:
    result = runner(
        ["git", "-C", str(root), *args],
        cwd=root,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise ValueError(f"git {' '.join(args)} failed: {str(stderr).strip()}")
    output = result.stdout
    if isinstance(output, str):
        return output.encode("utf-8")
    return output


def _require_ancestor(runner: Runner, root: Path, ancestor: str, revision: str) -> None:
    _git_run(runner, root, ["merge-base", "--is-ancestor", ancestor, revision])


def _within_allowed_paths(path: str, allowed_paths: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        scope == "."
        or normalized == scope.rstrip("/")
        or normalized.startswith(scope.rstrip("/") + "/")
        for scope in allowed_paths
    )


def _is_excluded(path: str, exclusions: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        normalized == exclusion.strip("/")
        or normalized.startswith(exclusion.strip("/") + "/")
        for exclusion in exclusions
    )


__all__ = ["CandidateCheckpoint", "checkpoint_candidate_changes"]
