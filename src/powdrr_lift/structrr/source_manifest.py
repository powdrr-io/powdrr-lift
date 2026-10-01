"""Immutable file manifests for comparable Structrr source snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.core.spec_paths import is_specification_path

SOURCE_MANIFEST_SCHEMA_VERSION = "structrr-source-manifest-v1"
SOURCE_EXTRACTOR_VERSION = "structrr-source-observations-v1"
SourceEvidenceKind = Literal["source_observation", "declaration"]


@dataclass(frozen=True, slots=True)
class SourceFileObservation:
    path: str
    content_digest: str
    size_bytes: int
    executable: bool
    evidence_kind: SourceEvidenceKind

    def to_data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "executable": self.executable,
            "evidence_kind": self.evidence_kind,
        }


@dataclass(frozen=True, slots=True)
class SourceManifest:
    submission_base: str
    source_revision: str
    extractor_version: str
    taxonomy_fingerprint: str
    excluded_paths: tuple[str, ...]
    files: tuple[SourceFileObservation, ...]
    coverage_complete: bool
    extraction_errors: tuple[str, ...]
    product_digest: str

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
            "submission_base": self.submission_base,
            "source_revision": self.source_revision,
            "extractor_version": self.extractor_version,
            "taxonomy_fingerprint": self.taxonomy_fingerprint,
            "excluded_paths": list(self.excluded_paths),
            "files": [item.to_data() for item in self.files],
            "coverage_complete": self.coverage_complete,
            "extraction_errors": list(self.extraction_errors),
            "product_digest": self.product_digest,
        }
        if include_fingerprint:
            data["fingerprint"] = content_fingerprint(data)
        return data

    @property
    def fingerprint(self) -> str:
        return str(self.to_data()["fingerprint"])

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceManifest:
        if raw.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported source manifest schema version")
        files_raw = raw.get("files")
        if not isinstance(files_raw, list) or not all(
            isinstance(item, Mapping) for item in files_raw
        ):
            raise ValueError("source manifest files must be a list of objects")
        files = tuple(_file_from_data(item) for item in files_raw)
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("source manifest contains duplicate paths")
        excluded_paths = _string_tuple(raw, "excluded_paths")
        errors = _string_tuple(raw, "extraction_errors")
        complete = raw.get("coverage_complete")
        if not isinstance(complete, bool):
            raise ValueError("source manifest coverage_complete must be a boolean")
        if complete == bool(errors):
            raise ValueError("source manifest coverage status conflicts with errors")
        manifest = cls(
            submission_base=_required_text(raw, "submission_base"),
            source_revision=_required_text(raw, "source_revision"),
            extractor_version=_required_text(raw, "extractor_version"),
            taxonomy_fingerprint=_required_text(raw, "taxonomy_fingerprint"),
            excluded_paths=excluded_paths,
            files=files,
            coverage_complete=complete,
            extraction_errors=errors,
            product_digest=_required_text(raw, "product_digest"),
        )
        if raw.get("fingerprint") != manifest.fingerprint:
            raise ValueError("source manifest fingerprint does not match content")
        if manifest.product_digest != _product_digest(files):
            raise ValueError("source manifest product digest does not match files")
        return manifest


def compile_source_manifest(
    root: str | Path,
    evidence_files: Sequence[str],
    *,
    submission_base: str,
    source_revision: str,
    taxonomy_fingerprint: str,
    excluded_paths: Sequence[str] = (),
    extractor_version: str = SOURCE_EXTRACTOR_VERSION,
) -> SourceManifest:
    """Hash the exact file inventory used for one bootstrap observation.

    File-read failures remain visible in the manifest and make its coverage
    incomplete. Commit identity is stored separately from the content-only
    product digest.
    """
    root_path = Path(root).resolve()
    for name, value in (
        ("submission_base", submission_base),
        ("source_revision", source_revision),
        ("taxonomy_fingerprint", taxonomy_fingerprint),
        ("extractor_version", extractor_version),
    ):
        if not value.strip():
            raise ValueError(f"source manifest {name} is required")
    excluded = tuple(sorted(set(_normalize_path(item) for item in excluded_paths)))
    excluded_set = set(excluded)
    files: list[SourceFileObservation] = []
    errors: list[str] = []
    for raw_path in sorted(set(evidence_files)):
        relative = _normalize_path(raw_path)
        if any(
            relative == excluded_path
            or relative.startswith(excluded_path.rstrip("/") + "/")
            for excluded_path in excluded_set
        ):
            continue
        target = root_path / relative
        try:
            content = target.read_bytes()
            executable = bool(target.stat().st_mode & 0o111)
        except OSError as error:
            errors.append(f"{relative}: {type(error).__name__}: {error}")
            continue
        files.append(
            SourceFileObservation(
                path=relative,
                content_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                size_bytes=len(content),
                executable=executable,
                evidence_kind=(
                    "declaration"
                    if is_specification_path(relative)
                    else "source_observation"
                ),
            )
        )
    frozen_files = tuple(files)
    return SourceManifest(
        submission_base=submission_base,
        source_revision=source_revision,
        extractor_version=extractor_version,
        taxonomy_fingerprint=taxonomy_fingerprint,
        excluded_paths=excluded,
        files=frozen_files,
        coverage_complete=not errors,
        extraction_errors=tuple(errors),
        product_digest=_product_digest(frozen_files),
    )


def compare_source_manifests(
    baseline: SourceManifest, candidate: SourceManifest
) -> dict[str, Any]:
    """Compare file content independently of checkpoint commit metadata."""
    compatible = (
        baseline.extractor_version == candidate.extractor_version
        and baseline.taxonomy_fingerprint == candidate.taxonomy_fingerprint
        and baseline.excluded_paths == candidate.excluded_paths
        and baseline.submission_base == candidate.submission_base
    )
    before = {item.path: item for item in baseline.files}
    after = {item.path: item for item in candidate.files}
    changes: list[dict[str, Any]] = []
    for path in sorted(before.keys() - after.keys()):
        item = before[path]
        changes.append(
            {
                "path": path,
                "status": "deleted",
                "evidence_kind": item.evidence_kind,
                "before_digest": item.content_digest,
                "after_digest": None,
            }
        )
    for path in sorted(after.keys() - before.keys()):
        item = after[path]
        changes.append(
            {
                "path": path,
                "status": "added",
                "evidence_kind": item.evidence_kind,
                "before_digest": None,
                "after_digest": item.content_digest,
            }
        )
    for path in sorted(before.keys() & after.keys()):
        old, new = before[path], after[path]
        if old == new:
            continue
        changes.append(
            {
                "path": path,
                "status": "modified",
                "evidence_kind": new.evidence_kind,
                "before_digest": old.content_digest,
                "after_digest": new.content_digest,
            }
        )
    passed = compatible and baseline.coverage_complete and candidate.coverage_complete
    report: dict[str, Any] = {
        "schema_version": "structrr-source-manifest-diff-v1",
        "baseline_manifest_fingerprint": baseline.fingerprint,
        "candidate_manifest_fingerprint": candidate.fingerprint,
        "baseline_revision": baseline.source_revision,
        "candidate_revision": candidate.source_revision,
        "baseline_product_digest": baseline.product_digest,
        "candidate_product_digest": candidate.product_digest,
        "compatible": compatible,
        "coverage_complete": (
            baseline.coverage_complete and candidate.coverage_complete
        ),
        "baseline_errors": list(baseline.extraction_errors),
        "candidate_errors": list(candidate.extraction_errors),
        "changes": changes,
        "passed": passed,
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


def _product_digest(files: Sequence[SourceFileObservation]) -> str:
    return content_fingerprint(
        [
            {
                "path": item.path,
                "content_digest": item.content_digest,
                "executable": item.executable,
            }
            for item in sorted(files, key=lambda entry: entry.path)
        ]
    )


def _file_from_data(raw: Mapping[str, Any]) -> SourceFileObservation:
    path = _required_text(raw, "path")
    if _normalize_path(path) != path:
        raise ValueError("source manifest paths must be normalized relative paths")
    evidence_kind = raw.get("evidence_kind")
    if evidence_kind not in {"declaration", "source_observation"}:
        raise ValueError("source manifest evidence_kind is unsupported")
    size_bytes = raw.get("size_bytes")
    executable = raw.get("executable")
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("source manifest size_bytes must be a non-negative integer")
    if not isinstance(executable, bool):
        raise ValueError("source manifest executable must be a boolean")
    return SourceFileObservation(
        path=path,
        content_digest=_required_text(raw, "content_digest"),
        size_bytes=size_bytes,
        executable=executable,
        evidence_kind=evidence_kind,
    )


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized or normalized == "." or ".." in normalized.split("/"):
        raise ValueError(f"invalid relative source path: {value!r}")
    return normalized


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value)


__all__ = [
    "SOURCE_EXTRACTOR_VERSION",
    "SOURCE_MANIFEST_SCHEMA_VERSION",
    "SourceFileObservation",
    "SourceManifest",
    "compare_source_manifests",
    "compile_source_manifest",
]
