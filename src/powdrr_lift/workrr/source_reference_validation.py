"""Trusted Git/worktree acquisition and existing-report adapter for references."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import ReferenceCatalog
from powdrr_service.structrr.reference_validation import (
    ReferenceReviewClient,
    ReferenceValidationReport,
    validate_source_references,
)
from powdrr_service.structrr.source_reader import SourceReader
from powdrr_service.structrr.source_reference_handoff import (
    CompiledSourceReferenceHandoff,
)

from powdrr_lift.workrr.coding_agent import _committed_paths, _working_paths


def _git(root: Path, *args: str, missing_ok: bool = False) -> bytes:
    environment = os.environ | {
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    result = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode and not missing_ok:
        detail = result.stderr.decode(errors="replace")[:1000]
        raise ValueError(f"Git source acquisition failed: {detail}")
    return result.stdout if result.returncode == 0 else b""


def _paths_output(root: Path, *args: str) -> str:
    if args[0] == "diff":
        args = (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "-z",
            *args[1:],
        )
    elif args[0] == "ls-files":
        args = ("ls-files", "-z", *args[1:])
    paths = _git(root, *args).decode("utf-8").split("\0")
    if any(any(character in path for character in "\n\r\x00") for path in paths):
        raise ValueError(
            "Control characters in Git paths prevent complete source acquisition."
        )
    return "\n".join(path for path in paths if path)


def _path(path: str) -> str:
    parsed = PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or ".." in parsed.parts
        or "\\" in path
        or PureWindowsPath(path).drive
        or parsed.suffix != ".py"
    ):
        raise ValueError("Unsafe or unsupported validation source path.")
    return path


def _baseline(root: Path, base: str, path: str) -> bytes | None:
    entry = _git(root, "ls-tree", "-z", base, "--", path)
    if not entry:
        return None
    if not entry.startswith((b"100644 blob ", b"100755 blob ")):
        raise ValueError("Baseline Python source must be a regular Git blob.")
    spec = f"{base}:{path}"
    size = int(_git(root, "cat-file", "-s", spec))
    if size > 1024 * 1024:
        raise ValueError("Baseline source exceeds one MiB.")
    data = _git(root, "cat-file", "blob", spec)
    if len(data) != size:
        raise ValueError("Baseline source size changed during acquisition.")
    return data


class SourceReferenceValidationContext:
    """Frozen private inputs; each check reacquires authoritative source bytes."""

    def __init__(
        self,
        handoff: CompiledSourceReferenceHandoff,
        catalog: ReferenceCatalog,
        *,
        recorded_inputs: dict[str, Any],
        review_client: ReferenceReviewClient | None = None,
        test_collection_evidence: dict[str, Any] | None = None,
    ) -> None:
        self.handoff = CompiledSourceReferenceHandoff.from_data(handoff.to_data())
        self.catalog = ReferenceCatalog.from_data(catalog.to_data())
        self._inputs_json = json.dumps(recorded_inputs, sort_keys=True, allow_nan=False)
        self.review_client = review_client
        self._test_evidence_json = json.dumps(
            test_collection_evidence, sort_keys=True, allow_nan=False
        )

    def _collect(
        self, workspace: Path
    ) -> tuple[
        dict[str, bytes | None],
        dict[str, bytes | None],
        tuple[str, ...],
        tuple[dict[str, Any], ...],
    ]:
        before: dict[str, bytes | None] = {}
        after: dict[str, bytes | None] = {}
        errors: list[dict[str, Any]] = []
        changed: set[str] = set()
        try:
            root = workspace.resolve(strict=True)
            if (
                Path(
                    _git(root, "rev-parse", "--show-toplevel").decode().strip()
                ).resolve()
                != root
            ):
                raise ValueError(
                    "Source validation requires the actual repository workspace root."
                )
            base = self.catalog.base_commit
            head = _git(root, "rev-parse", "HEAD").decode().strip()
            _git(root, "merge-base", "--is-ancestor", base, head)
            changed = _committed_paths(
                root, base, head, output=_paths_output
            ) | _working_paths(root, output=_paths_output)
            paths = {path for path in changed if path.endswith(".py")}
            contract = self.handoff.worker_prompt.to_data()["contract"]
            semantic_roots: list[str] = []
            for record in [*contract["obligations"], *contract["preservation"]]:
                for selector in (record["owner"], record.get("destination")):
                    if selector is None:
                        continue
                    if selector["binding"] == "exact":
                        paths.add(selector["path"])
                    else:
                        semantic_roots.extend(selector["allowed_paths"])
            if semantic_roots:
                inventory = (
                    _git(
                        root,
                        "ls-files",
                        "-z",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                    )
                    .decode("utf-8")
                    .split("\0")
                )
                paths.update(
                    path
                    for path in inventory
                    if path.endswith(".py")
                    and any(
                        scope == "." or path == scope or path.startswith(scope + "/")
                        for scope in semantic_roots
                    )
                )
            if len(paths) > 1000:
                raise ValueError(
                    "Source validation exceeds its 1000-file acquisition bound."
                )
            reader = SourceReader(root, self.catalog)
            total = 0
            for path in sorted(paths):
                before[path], after[path] = None, None
                try:
                    _path(path)
                    before[path] = _baseline(root, base, path)
                    target = root / path
                    if target.exists() or target.is_symlink():
                        after[path] = reader.snapshot_bytes(path)
                    total += sum(
                        len(data)
                        for data in (before[path], after[path])
                        if data is not None
                    )
                    if total > 16 * 1024 * 1024:
                        raise ValueError(
                            "Source validation exceeds its 16 MiB acquisition bound."
                        )
                except (
                    ValueError,
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    errors.append({"path": path, "message": str(error)})
                    if total > 16 * 1024 * 1024:
                        break
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append({"message": str(error)})
        return (
            before,
            after,
            tuple(sorted(path for path in changed if path.endswith(".py"))),
            tuple(errors),
        )

    def check(
        self, workspace: Path, *, claims: tuple[dict[str, Any], ...] = ()
    ) -> ReferenceValidationReport:
        before, after, changed, errors = self._collect(workspace)
        report = validate_source_references(
            self.handoff,
            self.catalog,
            baseline=before,
            candidate=after,
            changed_paths=changed,
            recorded_inputs=json.loads(self._inputs_json),
            acquisition_findings=errors,
            review_client=self.review_client,
            claims=claims,
            test_collection_evidence=json.loads(self._test_evidence_json),
        )
        fresh = self._collect(workspace)
        if _snapshot_identity((before, after, changed, errors)) != _snapshot_identity(
            fresh
        ):
            report = validate_source_references(
                self.handoff,
                self.catalog,
                baseline=before,
                candidate=after,
                changed_paths=changed,
                recorded_inputs=json.loads(self._inputs_json),
                acquisition_findings=errors
                + (
                    {
                        "message": (
                            "Workspace changed during reference validation; "
                            "acquire fresh evidence."
                        )
                    },
                ),
                claims=claims,
            )
        return report


def _snapshot_identity(
    snapshot: tuple[
        dict[str, bytes | None],
        dict[str, bytes | None],
        tuple[str, ...],
        tuple[dict[str, Any], ...],
    ],
) -> str:
    before, after, changed, errors = snapshot
    return content_fingerprint(
        {
            "baseline": {
                p: hashlib.sha256(b).hexdigest() if b is not None else None
                for p, b in before.items()
            },
            "candidate": {
                p: hashlib.sha256(b).hexdigest() if b is not None else None
                for p, b in after.items()
            },
            "changed": changed,
            "errors": errors,
        }
    )


def main_check(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="Check source references using trusted local configuration."
    ).parse_args(argv)
    try:
        handoff = CompiledSourceReferenceHandoff.from_data(
            json.loads(Path(os.environ["POWDRR_STRUCTRR_HANDOFF"]).read_text())
        )
        catalog = ReferenceCatalog.from_data(
            json.loads(Path(os.environ["POWDRR_STRUCTRR_CATALOG"]).read_text())
        )
        recorded = json.loads(
            Path(os.environ["POWDRR_STRUCTRR_VALIDATION_INPUTS"]).read_text()
        )
        context = SourceReferenceValidationContext(
            handoff, catalog, recorded_inputs=recorded
        )
        report = context.check(Path(os.environ["POWDRR_STRUCTRR_WORKSPACE"]))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "structrr-reference-check-error-v1",
                    "status": "error",
                    "message": str(error),
                }
            )
        )
        return 2
    print(json.dumps(report.to_data(), ensure_ascii=False))
    return 0 if report.status == "passed" else 1
