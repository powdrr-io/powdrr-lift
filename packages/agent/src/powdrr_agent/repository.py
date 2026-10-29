"""Local repository capabilities: evidence capture, authorization, and effects."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from powdrr_contracts.models import (
    ActionReceipt,
    Artifact,
    OperationRequest,
    RepositorySnapshot,
    artifact_manifest_digest,
)

SOURCE_FILE = "application/vnd.powdrr.source-file"
_FORBIDDEN_ROOTS = {".git", ".worktrees", ".venv", "venv", "node_modules"}
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class LocalActionError(ValueError):
    """A local repository capability was rejected or could not be completed."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def collect_tracked_worktree(
    repo_root: str | Path,
    *,
    request_id: str,
    operation_kind: str,
    inputs: dict[str, object],
    include_untracked: bool = False,
) -> OperationRequest:
    """Capture tracked worktree bytes without exposing its path to the service.

    Tracked file contents and executable bits come from the worktree, matching
    Structrr bootstrap behavior. Untracked files are omitted unless explicitly
    requested. The commit SHA identifies the base revision, while the artifact
    manifest records the exact bytes observed by the agent.
    """
    root = Path(repo_root).resolve()
    head_sha = _git(root, "rev-parse", "--verify", "HEAD").strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"
    command = ["ls-files", "-z"]
    if include_untracked:
        command = ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    raw_paths = _git_bytes(root, *command).split(b"\0")
    artifacts: list[Artifact] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        try:
            relative = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LocalActionError(
                "unsupported_path_encoding", "Repository contains a non-UTF-8 path."
            ) from exc
        if PurePosixPath(relative).parts[0] in {
            ".git",
            ".worktrees",
            ".venv",
            "venv",
            "node_modules",
            "vendor",
        }:
            continue
        target = _authorized_path(root, relative, require_parent=False)
        try:
            file_stat = target.lstat()
            if stat.S_ISLNK(file_stat.st_mode):
                content = os.readlink(target).encode("utf-8")
                mode = "120000"
            elif stat.S_ISREG(file_stat.st_mode):
                content = target.read_bytes()
                mode = "100755" if file_stat.st_mode & stat.S_IXUSR else "100644"
            else:
                continue
        except FileNotFoundError:
            # A tracked deletion is absent from bootstrap evidence, as in the
            # existing implementation which filters with Path.is_file().
            continue
        if mode == "120000" and not target.is_file():
            continue
        artifacts.append(
            Artifact(
                artifact_id=relative,
                media_type=SOURCE_FILE,
                data=content,
                scope="repository_snapshot",
                attributes={"git_mode": mode},
            )
        )
    frozen_artifacts = tuple(sorted(artifacts, key=lambda item: item.artifact_id))
    manifest = artifact_manifest_digest(frozen_artifacts)
    snapshot = RepositorySnapshot(
        repository_id=root.name,
        branch=branch,
        base_sha=None,
        head_sha=head_sha,
        manifest_digest=manifest,
        worktree_digest=manifest,
        source_artifact_refs=tuple(item.artifact_id for item in frozen_artifacts),
        includes_dirty_and_untracked=include_untracked,
    )
    return OperationRequest(
        request_id=request_id,
        operation_kind=operation_kind,
        inputs={
            **inputs,
            "snapshot_policy": {
                "tracked_content": "worktree",
                "dirty_tracked_included": True,
                "untracked_included": include_untracked,
            },
        },
        repository=snapshot,
        artifacts=frozen_artifacts,
        capabilities=(
            {"name": "repository.pinned_file_snapshot", "version": "1"},
            {"name": "file.materialize", "version": "1"},
            {"name": "command.execute", "version": "1"},
        ),
    )


def read_pinned_file(
    repo_root: str | Path, commit_sha: str, relative_path: str
) -> Artifact:
    """Capture one regular file from the exact Git tree named by commit_sha."""
    if not _GIT_OBJECT_ID.fullmatch(commit_sha):
        raise LocalActionError(
            "invalid_revision", "Commit SHA must be a full Git object ID."
        )
    root = Path(repo_root).resolve()
    _authorized_path(root, relative_path, require_parent=False)
    resolved_commit = _git(
        root, "rev-parse", "--verify", f"{commit_sha}^{{commit}}"
    ).strip()
    if resolved_commit != commit_sha:
        raise LocalActionError(
            "invalid_revision", "Commit SHA must identify the full commit object."
        )
    listing = _git_bytes(
        root,
        "ls-tree",
        "-z",
        commit_sha,
        "--",
        f":(literal){relative_path}",
    )
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) != 1:
        raise LocalActionError(
            "forbidden_path", "Source path is not a tracked file at the pinned SHA."
        )
    metadata, separator, entry_path = entries[0].partition(b"\t")
    fields = metadata.split()
    if not separator or entry_path.decode("utf-8", errors="strict") != relative_path:
        raise LocalActionError(
            "forbidden_path", "Git did not resolve the exact pinned source path."
        )
    if (
        len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
    ):
        raise LocalActionError(
            "forbidden_path", "Pinned source must be a regular tracked blob."
        )
    blob_sha = fields[2].decode("ascii")
    if len(blob_sha) != len(commit_sha):
        raise LocalActionError("repository_error", "Git object format is inconsistent.")
    content = _git_bytes(root, "cat-file", "blob", blob_sha)
    return Artifact(
        artifact_id=relative_path,
        media_type=SOURCE_FILE,
        data=content,
        scope="repository_snapshot",
        attributes={
            "git_blob_sha": blob_sha,
            "git_mode": fields[0].decode("ascii"),
            "source_revision": commit_sha,
        },
    )


def digest_at_path(repo_root: str | Path, relative_path: str) -> str | None:
    """Observe a safe in-repository file digest for an optimistic precondition."""
    root = Path(repo_root).resolve()
    target = _authorized_path(root, relative_path)
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_file():
        raise LocalActionError("forbidden_path", "Target is not a regular file.")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def materialize_artifact(
    repo_root: str | Path,
    artifact: Artifact,
    *,
    expected_digest: str | None,
) -> ActionReceipt:
    """Atomically write a service artifact after local path and CAS checks."""
    root = Path(repo_root).resolve()
    target = _authorized_path(root, artifact.artifact_id)
    current = digest_at_path(root, artifact.artifact_id)
    if current != expected_digest:
        raise LocalActionError(
            "stale_precondition",
            f"{artifact.artifact_id} changed after the service request was made.",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_parent(root, target.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".powdrr-tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(artifact.data)
            stream.flush()
            os.fsync(stream.fileno())
        mode = artifact.attributes.get("git_mode")
        os.chmod(temporary_name, 0o755 if mode == "100755" else 0o644)
        os.replace(temporary_name, target)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return ActionReceipt(
        action_id=str(uuid.uuid4()),
        payload_digest=artifact.sha256,
        status="succeeded",
        observed_revisions={"path": artifact.artifact_id, "previous_digest": current},
        changes=(artifact.artifact_id,),
        artifact_refs=(artifact.artifact_id,),
    )


def execute_command(
    repo_root: str | Path,
    arguments: tuple[str, ...],
    *,
    timeout_seconds: int,
) -> tuple[ActionReceipt, Artifact, Artifact]:
    """Run a bounded argv locally; never invoke a shell or accept a service cwd."""
    if not arguments or any(
        not isinstance(value, str) or "\0" in value for value in arguments
    ):
        raise LocalActionError(
            "invalid_action", "Command arguments must be non-empty strings."
        )
    if timeout_seconds < 1 or timeout_seconds > 3600:
        raise LocalActionError(
            "invalid_action", "Command timeout must be between 1 and 3600 seconds."
        )
    root = Path(repo_root).resolve()
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=root,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        status, exit_code, signal_name = "timed_out", None, None
    else:
        stdout, stderr = completed.stdout, completed.stderr
        status = "succeeded" if completed.returncode == 0 else "failed"
        exit_code = completed.returncode if completed.returncode >= 0 else None
        signal_name = (
            f"SIG{-completed.returncode}" if completed.returncode < 0 else None
        )
    stdout_artifact = Artifact(
        artifact_id=f"command-output/{uuid.uuid4()}/stdout",
        media_type="text/plain; charset=utf-8",
        data=stdout,
    )
    stderr_artifact = Artifact(
        artifact_id=f"command-output/{uuid.uuid4()}/stderr",
        media_type="text/plain; charset=utf-8",
        data=stderr,
    )
    receipt = ActionReceipt(
        action_id=str(uuid.uuid4()),
        payload_digest=hashlib.sha256("\0".join(arguments).encode()).hexdigest(),
        status=status,
        exit_code=exit_code,
        signal=signal_name,
        stdout_ref=stdout_artifact.artifact_id,
        stderr_ref=stderr_artifact.artifact_id,
    )
    return receipt, stdout_artifact, stderr_artifact


def _authorized_path(
    root: Path, relative_path: str, *, require_parent: bool = True
) -> Path:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[0] in _FORBIDDEN_ROOTS
    ):
        raise LocalActionError(
            "forbidden_path", f"Path is not authorized: {relative_path}"
        )
    target = root.joinpath(*relative.parts)
    if require_parent:
        _assert_safe_parent(root, target.parent)
    return target


def _assert_safe_parent(root: Path, parent: Path) -> None:
    try:
        parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise LocalActionError(
            "forbidden_path", "Path escapes the repository root."
        ) from exc
    cursor = root
    for part in parent.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise LocalActionError(
                "forbidden_path", "Symlinked parent paths are not writable."
            )


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise LocalActionError(
            "repository_error", result.stderr.strip() or "git command failed"
        )
    return result.stdout


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise LocalActionError(
            "repository_error", result.stderr.decode(errors="replace").strip()
        )
    return result.stdout
