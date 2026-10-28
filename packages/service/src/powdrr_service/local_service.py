"""Local service adapter used during the staged migration."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from powdrr_contracts.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    OperationInput,
    OperationReference,
    OperationRequest,
    OperationUpdate,
    ServiceError,
)


class LocalPowdrrService:
    """Reconstruct public artifact state from data supplied by the agent."""

    def start(self, request: OperationRequest) -> OperationUpdate:
        if request.operation_kind != "inspect_artifacts":
            return _failed(
                request.request_id,
                "unsupported_operation",
                "Operation is not migrated yet.",
            )
        if request.repository is None:
            return _failed(
                request.request_id,
                "invalid_request",
                "Repository snapshot is required.",
            )
        areas = request.inputs.get("areas", ["workflows"])
        if areas != ["workflows"]:
            return _failed(
                request.request_id,
                "unsupported_projection",
                "This boundary slice supports workflow inspection.",
            )
        try:
            projection = self._inspect_workflows(request)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return _failed(request.request_id, "artifact_inspection_failed", str(exc))
        payload = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        from powdrr_contracts.models import Artifact

        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": "artifact_state_reconstructed", "area": "workflows"},),
            output_artifacts=(
                Artifact(
                    artifact_id="artifact-state/workflows.json",
                    media_type="application/vnd.powdrr.artifact-state+json",
                    data=payload,
                ),
            ),
        )

    def _inspect_workflows(self, request: OperationRequest) -> dict[str, Any]:
        repository = request.repository
        assert repository is not None
        with tempfile.TemporaryDirectory(prefix="powdrr-service-snapshot-") as temp:
            root = Path(temp)
            refs: set[str] = set()
            manifest: list[tuple[str, str]] = []
            for artifact in request.artifacts:
                if artifact.media_type != "application/vnd.powdrr.source-file":
                    continue
                relative = PurePosixPath(artifact.artifact_id)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(
                        f"unsafe repository artifact path: {artifact.artifact_id}"
                    )
                if not artifact.artifact_id.startswith("docs/workflows/"):
                    raise ValueError(
                        "workflow projection received a non-workflow artifact"
                    )
                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(artifact.data)
                refs.add(artifact.artifact_id)
                manifest.append((artifact.artifact_id, artifact.sha256))
            if refs != set(repository.source_artifact_refs):
                raise ValueError(
                    "repository snapshot references do not match supplied files"
                )
            manifest_bytes = json.dumps(
                sorted(manifest), separators=(",", ":")
            ).encode()
            if hashlib.sha256(manifest_bytes).hexdigest() != repository.manifest_digest:
                raise ValueError(
                    "repository snapshot manifest digest does not match supplied files"
                )
            _git(root, "init", "-b", "snapshot")
            _git(root, "config", "user.name", "Powdrr local service")
            _git(root, "config", "user.email", "service@localhost")
            # Git requires one commit even for an empty, valid workflow snapshot.
            if not refs:
                (root / ".powdrr-empty-snapshot").write_text("", encoding="utf-8")
            _git(root, "add", "--all")
            _git(root, "commit", "-m", "reconstructed pinned snapshot")
            commit = _git(root, "rev-parse", "HEAD").strip()
            # Importing the legacy implementation is a temporary PR 2 migration
            # edge; it receives a service-owned snapshot directory, never the
            # agent's repository path or a callback into it.
            from powdrr_lift.artifact_state import build_artifact_state

            result = build_artifact_state(
                root,
                branch="snapshot",
                sha=commit,
                repository=repository.repository_id,
                include_github=False,
                areas=["workflows"],
            )
        result["branch"] = repository.branch
        result["sha"] = repository.head_sha
        return result

    def advance(self, operation_input: OperationInput) -> OperationUpdate:
        return _failed(
            operation_input.operation_id,
            "unsupported_operation",
            "No resumable operations are migrated yet.",
        )

    def inspect(self, reference: OperationReference) -> OperationUpdate:
        return _failed(
            reference.operation_id, "not_found", "Local operations are not persisted."
        )

    def cancel(self, reference: OperationReference) -> OperationUpdate:
        return _failed(
            reference.operation_id,
            "unsupported_operation",
            "Read-only inspection cannot be cancelled.",
        )

    def infer(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        raise NotImplementedError("provider inference moves in PR 6")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            result.stderr.strip() or "service snapshot git operation failed"
        )
    return result.stdout


def _failed(operation_id: str, category: str, message: str) -> OperationUpdate:
    return OperationUpdate(
        operation_id=operation_id,
        revision=0,
        status="failed",
        failure=ServiceError(
            category=category,
            message=message,
            retryable=False,
            correlation_id=operation_id,
        ),
    )
