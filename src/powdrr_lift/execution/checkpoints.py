"""Content-addressed checkpoints and bounded post-action diagnostics."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    workspace_root: str
    objects: Mapping[str, str]
    state_ref: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "workspace_root": self.workspace_root,
            "objects": dict(self.objects),
            **({"state_ref": self.state_ref} if self.state_ref is not None else {}),
        }


class ContentAddressedCheckpointStore:
    def __init__(self, directory: str | Path) -> None:
        self.root = Path(directory)
        self.objects = self.root / "objects"
        self.manifests = self.root / "manifests"

    def create(
        self,
        workspace_root: str | Path,
        checkpoint_id: str,
        *,
        state_json: str | None = None,
    ) -> Checkpoint:
        workspace = Path(workspace_root).resolve()
        objects: dict[str, str] = {}
        for path in _workspace_files(workspace):
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            object_path = self.objects / digest
            object_path.parent.mkdir(parents=True, exist_ok=True)
            if not object_path.exists():
                object_path.write_bytes(content)
            objects[str(path.relative_to(workspace))] = digest
        state_ref = None
        if state_json is not None:
            state_ref = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            state_path = self.objects / state_ref
            state_path.parent.mkdir(parents=True, exist_ok=True)
            if not state_path.exists():
                state_path.write_text(state_json, encoding="utf-8")
        checkpoint = Checkpoint(checkpoint_id, str(workspace), objects, state_ref)
        self.manifests.mkdir(parents=True, exist_ok=True)
        (self.manifests / f"{checkpoint_id}.json").write_text(
            json.dumps(checkpoint.to_data(), indent=2) + "\n", encoding="utf-8"
        )
        return checkpoint

    def load(self, checkpoint_id: str) -> Checkpoint:
        data = json.loads(
            (self.manifests / f"{checkpoint_id}.json").read_text(encoding="utf-8")
        )
        return Checkpoint(
            data["checkpoint_id"],
            data["workspace_root"],
            data["objects"],
            data.get("state_ref"),
        )

    def load_state_json(self, checkpoint: Checkpoint) -> str | None:
        """Return the logical execution snapshot captured with a checkpoint."""
        if checkpoint.state_ref is None:
            return None
        return (self.objects / checkpoint.state_ref).read_text(encoding="utf-8")

    def garbage_collect(
        self, referenced_checkpoint_ids: Iterable[str]
    ) -> tuple[str, ...]:
        """Remove unreferenced manifests and content objects safely."""
        referenced = set(referenced_checkpoint_ids)
        kept_objects: set[str] = set()
        removed: list[str] = []
        for manifest in self.manifests.glob("*.json"):
            checkpoint_id = manifest.stem
            if checkpoint_id in referenced:
                checkpoint = self.load(checkpoint_id)
                kept_objects.update(checkpoint.objects.values())
                if checkpoint.state_ref is not None:
                    kept_objects.add(checkpoint.state_ref)
            else:
                manifest.unlink()
                removed.append(checkpoint_id)
        for object_path in self.objects.iterdir() if self.objects.exists() else ():
            if object_path.is_file() and object_path.name not in kept_objects:
                object_path.unlink()
        return tuple(sorted(removed))

    def restore(
        self, checkpoint: Checkpoint, workspace_root: str | Path | None = None
    ) -> None:
        workspace = Path(workspace_root or checkpoint.workspace_root).resolve()
        expected = set(checkpoint.objects)
        for path in _workspace_files(workspace):
            if str(path.relative_to(workspace)) not in expected:
                path.unlink()
        for relative, digest in checkpoint.objects.items():
            target = (workspace / relative).resolve()
            target.relative_to(workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.objects / digest, target)

    def restore_with_state(
        self, checkpoint: Checkpoint, workspace_root: str | Path | None = None
    ) -> str | None:
        """Restore files and return the exact logical state captured with them.

        The caller owns deserializing and installing the typed execution state;
        returning it from the same restore operation prevents workspace and
        logical-state recovery from silently diverging.
        """
        self.restore(checkpoint, workspace_root)
        return self.load_state_json(checkpoint)


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    name: str
    successful: bool
    output: str
    truncated: bool = False


def run_diagnostics(
    workspace_root: str | Path,
    hooks: Iterable[tuple[str, Callable[[Path], str]]],
    *,
    max_output_chars: int = 8_000,
) -> tuple[DiagnosticResult, ...]:
    results: list[DiagnosticResult] = []
    for name, hook in hooks:
        try:
            output = hook(Path(workspace_root).resolve())
            truncated = len(output) > max_output_chars
            results.append(
                DiagnosticResult(name, True, output[:max_output_chars], truncated)
            )
        except Exception as error:  # diagnostics are evidence, not execution control
            results.append(DiagnosticResult(name, False, str(error)[:max_output_chars]))
    return tuple(results)


def _workspace_files(workspace: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(workspace).parts
    )
