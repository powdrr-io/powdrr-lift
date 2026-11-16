"""Guarded publication of an already accepted CI repair candidate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_contracts.ci import ProtectedArtifact, RepairContract
from powdrr_service.ci_response.acceptance import (
    CIRepairValidation,
    assess_ci_repair_candidate,
)

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttemptStore,
    CodingAgentStatus,
    _working_paths,
    _worktree_fingerprint,
)

GitRunner = Callable[..., subprocess.CompletedProcess[str]]
_HEAD_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def publish_ci_repair_candidate(
    contract: RepairContract,
    observation: Mapping[str, Any],
    candidate_result: Mapping[str, Any],
    *,
    worktree_root: Path,
    attempt_store: CodingAgentAttemptStore,
    git_runner: GitRunner = subprocess.run,
) -> dict[str, Any]:
    """Revalidate one candidate, then fast-forward its existing same-repo PR branch."""
    contract = RepairContract.from_dict(contract.to_dict())
    blocked = _validate_observation(contract, observation)
    if blocked is not None:
        return blocked

    if candidate_result.get("dispatch_status") != "accepted":
        return _blocked("candidate dispatch was not accepted")
    acceptance = candidate_result.get("acceptance")
    if (
        not isinstance(acceptance, Mapping)
        or acceptance.get("status") != "accepted"
        or acceptance.get("contract_id") != contract.contract_id
    ):
        return _blocked("candidate acceptance does not match the repair contract")
    attempt_id = candidate_result.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return _blocked("candidate attempt ID is missing")

    try:
        attempt = attempt_store.load_attempt(attempt_id)
        manifest = json.loads(
            (attempt_store.root / "candidate.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        return _blocked(f"candidate evidence is unavailable: {error}")
    if not isinstance(manifest, Mapping):
        return _blocked("candidate manifest must be an object")
    if manifest.get("schema_version") != "ci-repair-candidate-v1":
        return _blocked("candidate manifest schema is unsupported")
    if (
        attempt.attempt_id != attempt_id
        or attempt.status is not CodingAgentStatus.COMPLETED
        or manifest.get("attempt_id") != attempt_id
        or manifest.get("contract_id") != contract.contract_id
        or manifest.get("base_sha") != contract.head_sha
    ):
        return _blocked("candidate attempt or manifest does not match the contract")

    changed_paths = manifest.get("changed_paths")
    if not isinstance(changed_paths, list) or not all(
        isinstance(path, str) for path in changed_paths
    ):
        return _blocked("candidate manifest changed paths are malformed")
    changed_paths = tuple(changed_paths)
    current_paths = tuple(
        sorted(set(attempt.changed_paths) | _working_paths(worktree_root))
    )
    if tuple(sorted(changed_paths)) != current_paths:
        return _blocked("candidate paths changed after acceptance")
    diff_fingerprint = manifest.get("diff_fingerprint")
    current_fingerprint = _worktree_fingerprint(worktree_root, current_paths)
    if diff_fingerprint != current_fingerprint:
        return _blocked("candidate changed after its validation receipts were recorded")

    raw_protected = manifest.get("protected_artifacts")
    if not isinstance(raw_protected, list):
        return _blocked("candidate protected artifact evidence is malformed")
    protected_artifacts = _protected_artifacts(worktree_root, contract)
    observed_protected = [item.to_dict() for item in protected_artifacts]
    if observed_protected != raw_protected:
        return _blocked("protected artifacts changed after candidate acceptance")

    raw_validations = manifest.get("validations")
    if not isinstance(raw_validations, list):
        return _blocked("candidate validation evidence is malformed")
    validations: list[CIRepairValidation] = []
    try:
        for item in raw_validations:
            if not isinstance(item, Mapping):
                return _blocked("candidate validation receipt is malformed")
            command = item.get("command")
            status = item.get("status")
            receipt_fingerprint = item.get("diff_fingerprint")
            if (
                not isinstance(command, list)
                or not all(isinstance(part, str) and part for part in command)
                or status not in {"passed", "failed", "incomplete"}
                or not isinstance(receipt_fingerprint, str)
            ):
                return _blocked("candidate validation receipt is malformed")
            validations.append(
                CIRepairValidation(
                    command=tuple(command),
                    status=status,
                    diff_fingerprint=receipt_fingerprint,
                )
            )
    except ValueError as error:
        return _blocked(f"candidate validation receipt is invalid: {error}")

    assessed = assess_ci_repair_candidate(
        contract,
        base_sha=contract.head_sha,
        worker_status=attempt.status.value,
        changed_paths=current_paths,
        protected_artifacts=protected_artifacts,
        diff_fingerprint=current_fingerprint,
        validations=validations,
    )
    if assessed.status != "accepted":
        return {
            "passed": False,
            "publication_status": "blocked",
            "reasons": list(assessed.reasons),
            "acceptance": assessed.to_dict(),
        }

    metadata = observation.get("pull_request_metadata")
    if not isinstance(metadata, Mapping):
        return _blocked("observation does not identify the pull request head branch")
    head_repository = metadata.get("head_repository")
    head_ref = metadata.get("head_ref")
    if head_repository != contract.repository:
        return _blocked("publishing CI repairs to fork branches is not supported")
    if (
        not isinstance(head_ref, str)
        or not _HEAD_REF_RE.fullmatch(head_ref)
        or ".." in head_ref.split("/")
        or not _git_ok(
            git_runner,
            worktree_root,
            ["check-ref-format", "--branch", head_ref],
        )
    ):
        return _blocked("pull request head branch name is invalid")

    remote_url = _git_output(git_runner, worktree_root, ["remote", "get-url", "origin"])
    if remote_url not in _github_remotes(contract.repository):
        return _blocked(
            "candidate worktree origin does not match the contract repository"
        )

    if not _git_ok(
        git_runner,
        worktree_root,
        ["merge-base", "--is-ancestor", contract.head_sha, "HEAD"],
    ):
        return _blocked("candidate commit is not based on the contract head")

    remote_head = _remote_branch_head(git_runner, worktree_root, head_ref)
    local_head = _git_output(git_runner, worktree_root, ["rev-parse", "HEAD"])
    if remote_head not in {contract.head_sha, local_head}:
        return _blocked("remote PR branch moved after CI observation")

    if current_paths:
        add = _run_git(git_runner, worktree_root, ["add", "--", *current_paths])
        if add.returncode != 0:
            return _blocked(_command_error("could not stage accepted candidate", add))
    staged = _git_output(git_runner, worktree_root, ["diff", "--cached", "--name-only"])
    staged_paths = tuple(path for path in staged.splitlines() if path)
    if not set(staged_paths).issubset(current_paths):
        return _blocked("staged candidate contains paths outside the accepted diff")
    if staged_paths:
        check = _run_git(git_runner, worktree_root, ["diff", "--cached", "--check"])
        if check.returncode != 0:
            return _blocked(_command_error("candidate whitespace check failed", check))
        identity = _run_git(git_runner, worktree_root, ["var", "GIT_AUTHOR_IDENT"])
        if identity.returncode != 0:
            return _blocked("Git author identity is not configured")
        commit = _run_git(
            git_runner,
            worktree_root,
            ["commit", "-m", f"Repair CI failure {contract.failure_id[:64]}"],
        )
        if commit.returncode != 0:
            return _blocked(
                _command_error("could not commit accepted candidate", commit)
            )

    committed_paths = _git_output(
        git_runner,
        worktree_root,
        ["diff", "--name-only", "--no-renames", f"{contract.head_sha}...HEAD"],
    )
    if tuple(sorted(path for path in committed_paths.splitlines() if path)) != tuple(
        sorted(current_paths)
    ):
        return _blocked("published commit paths differ from the accepted candidate")
    dirty = _run_git(git_runner, worktree_root, ["status", "--porcelain"])
    if dirty.returncode != 0 or dirty.stdout.strip():
        return _blocked("candidate worktree is not clean after commit")

    if remote_head == _git_output(git_runner, worktree_root, ["rev-parse", "HEAD"]):
        return _published(contract, head_ref, remote_head, already_published=True)

    pushed = _run_git(
        git_runner,
        worktree_root,
        ["push", "--porcelain", "origin", f"HEAD:refs/heads/{head_ref}"],
    )
    if pushed.returncode != 0:
        return _blocked(
            "normal fast-forward push failed: "
            + (pushed.stderr.strip() or pushed.stdout.strip())[-1000:]
        )
    published_head = _git_output(git_runner, worktree_root, ["rev-parse", "HEAD"])
    return _published(contract, head_ref, published_head, already_published=False)


def _validate_observation(
    contract: RepairContract, observation: Mapping[str, Any]
) -> dict[str, Any] | None:
    if observation.get("response_state") != "failed":
        return _blocked("publication requires the failed observation used for dispatch")
    if (
        observation.get("repository") != contract.repository
        or observation.get("pull_request") != contract.pull_request_number
        or observation.get("head_sha") != contract.head_sha
        or observation.get("observation_id") != contract.observation_id
    ):
        return _blocked("publication observation does not match the repair contract")
    return None


def _protected_artifacts(
    root: Path, contract: RepairContract
) -> list[ProtectedArtifact]:
    artifacts: list[ProtectedArtifact] = []
    for artifact in contract.protected_artifacts:
        path = root / artifact.path
        if not path.is_file() or path.is_symlink():
            continue
        artifacts.append(
            ProtectedArtifact(
                path=artifact.path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return artifacts


def _github_remotes(repository: str) -> set[str]:
    return {
        f"https://github.com/{repository}.git",
        f"https://github.com/{repository}",
        f"git@github.com:{repository}.git",
        f"git@github.com:{repository}",
    }


def _remote_branch_head(runner: GitRunner, root: Path, branch: str) -> str | None:
    result = _run_git(
        runner, root, ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
    )
    if result.returncode != 0:
        raise RuntimeError(_command_error("could not inspect remote PR branch", result))
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f"refs/heads/{branch}":
        raise RuntimeError("remote PR branch lookup returned malformed output")
    return rows[0][0]


def _run_git(
    runner: GitRunner, root: Path, arguments: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(runner: GitRunner, root: Path, arguments: Sequence[str]) -> bool:
    return _run_git(runner, root, arguments).returncode == 0


def _git_output(runner: GitRunner, root: Path, arguments: Sequence[str]) -> str:
    result = _run_git(runner, root, arguments)
    if result.returncode != 0:
        raise RuntimeError(_command_error("Git command failed", result))
    return result.stdout.strip()


def _command_error(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
    return f"{prefix}: {(result.stderr.strip() or result.stdout.strip())[-1000:]}"


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "passed": False,
        "publication_status": "blocked",
        "reasons": [reason],
    }


def _published(
    contract: RepairContract,
    branch: str,
    head_sha: str,
    *,
    already_published: bool,
) -> dict[str, Any]:
    return {
        "passed": True,
        "publication_status": "published",
        "repository": contract.repository,
        "pull_request_number": contract.pull_request_number,
        "pull_request_url": (
            f"https://github.com/{contract.repository}/pull/"
            f"{contract.pull_request_number}"
        ),
        "branch": branch,
        "head_sha": head_sha,
        "already_published": already_published,
    }


__all__ = ["publish_ci_repair_candidate"]
