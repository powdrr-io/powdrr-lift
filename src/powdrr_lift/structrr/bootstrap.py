"""Deterministically bootstrap a validated Structrr snapshot from a repository."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.core.entity_taxonomy import EntityTaxonomy, load_entity_taxonomy
from powdrr_lift.core.spec_paths import is_specification_path

_SCHEMA = "https://powdrr.io/schema/changelog-v2"
_DEFAULT_OUTPUT = Path("docs/structrr/bootstrap-changelog.yaml")
_IGNORED_PREFIXES = (".git/", ".worktrees/", "node_modules/", "vendor/")
_IGNORED_FILES = {".DS_Store"}


@dataclass(frozen=True, slots=True)
class BootstrapIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapValidationReport:
    successful: bool
    issues: tuple[BootstrapIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    output_path: Path
    document: dict[str, Any]
    validation: BootstrapValidationReport
    evidence_files: tuple[str, ...] = field(default_factory=tuple)


def bootstrap_structrr(
    repo_root: str | Path,
    *,
    output_path: str | Path | None = None,
    change_id: str = "bootstrap",
    title: str | None = None,
    taxonomy_path: str | Path = "software_development_entity_taxonomy.md",
) -> BootstrapResult:
    """Build and validate a Structrr snapshot from tracked repository evidence.

    The operation is intentionally read/analyze/write-only: it never stages,
    commits, or mutates source files. Existing specification documents are
    treated as authoritative semantic evidence; source files provide the
    repository structure and file-level provenance anchors.
    """
    root = Path(repo_root).resolve()
    taxonomy = load_entity_taxonomy(root, taxonomy_path)
    tracked_files = _tracked_files(root)
    spec_paths = tuple(path for path in tracked_files if is_specification_path(path))
    spec_documents = _load_spec_documents(root, spec_paths)
    document = _build_document(
        root=root,
        taxonomy=taxonomy,
        tracked_files=tracked_files,
        spec_paths=spec_paths,
        spec_documents=spec_documents,
        change_id=change_id,
        title=title or f"Bootstrap Structrr for {root.name}",
    )
    validation = validate_bootstrap_document(document, root=root, taxonomy=taxonomy)
    resolved_output = (
        root / (output_path or _DEFAULT_OUTPUT)
        if not Path(output_path or _DEFAULT_OUTPUT).is_absolute()
        else Path(output_path or _DEFAULT_OUTPUT)
    )
    if validation.successful:
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        resolved_output.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
    return BootstrapResult(
        output_path=resolved_output,
        document=document,
        validation=validation,
        evidence_files=tracked_files,
    )


def validate_bootstrap_document(
    document: Mapping[str, Any],
    *,
    root: str | Path,
    taxonomy: EntityTaxonomy | None = None,
) -> BootstrapValidationReport:
    """Validate a bootstrap snapshot independently of a branch diff."""
    root_path = Path(root).resolve()
    issues: list[BootstrapIssue] = []
    if document.get("schema") != _SCHEMA:
        issues.append(
            BootstrapIssue("schema_invalid", "Bootstrap must use changelog-v2.")
        )

    try:
        parse_change_log(yaml.safe_dump(dict(document), sort_keys=False))
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        issues.append(BootstrapIssue("changelog_invalid", str(exc)))

    allowed = set(taxonomy.entity_types) if taxonomy is not None else set()
    entities = document.get("entities", [])
    entity_ids: set[str] = set()
    if not isinstance(entities, Sequence) or isinstance(entities, (str, bytes)):
        issues.append(BootstrapIssue("entities_invalid", "entities must be a list."))
        entities = []
    for index, entity in enumerate(entities):
        if not isinstance(entity, Mapping):
            issues.append(
                BootstrapIssue(
                    "entity_invalid", "Entity must be a mapping.", f"entities[{index}]"
                )
            )
            continue
        entity_id = str(entity.get("id", "")).strip()
        entity_type = str(entity.get("type", "")).strip()
        action = str(entity.get("action", "")).strip()
        if not entity_id:
            issues.append(
                BootstrapIssue(
                    "entity_id_missing", "Entity id is required.", f"entities[{index}]"
                )
            )
        elif entity_id in entity_ids:
            issues.append(
                BootstrapIssue(
                    "entity_id_duplicate", f"Duplicate entity id {entity_id!r}."
                )
            )
        entity_ids.add(entity_id)
        if entity_type not in allowed:
            issues.append(
                BootstrapIssue(
                    "entity_type_not_allowed",
                    f"Unsupported entity type {entity_type!r}.",
                    entity_id,
                )
            )
        if action != "added":
            issues.append(
                BootstrapIssue(
                    "entity_action_invalid",
                    "Bootstrap entities must be added snapshot entries.",
                    entity_id,
                )
            )

    relationships = document.get("entity_relationships", [])
    if not isinstance(relationships, Sequence) or isinstance(
        relationships, (str, bytes)
    ):
        issues.append(
            BootstrapIssue(
                "relationships_invalid", "entity_relationships must be a list."
            )
        )
        relationships = []
    for index, relationship in enumerate(relationships):
        if not isinstance(relationship, Mapping):
            issues.append(
                BootstrapIssue(
                    "relationship_invalid",
                    "Relationship must be a mapping.",
                    f"entity_relationships[{index}]",
                )
            )
            continue
        source = str(relationship.get("source", "")).strip()
        target = str(relationship.get("target", "")).strip()
        if source not in entity_ids or target not in entity_ids:
            issues.append(
                BootstrapIssue(
                    "relationship_dangling",
                    "Relationship endpoints must be declared: "
                    f"{source!r} -> {target!r}.",
                )
            )

    files = document.get("files", [])
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        issues.append(BootstrapIssue("files_invalid", "files must be a list."))
        files = []
    for index, file_entry in enumerate(files):
        if not isinstance(file_entry, Mapping):
            issues.append(
                BootstrapIssue(
                    "file_invalid", "File entry must be a mapping.", f"files[{index}]"
                )
            )
            continue
        relative = str(file_entry.get("path", "")).strip()
        path = root_path / relative
        span = file_entry.get("span")
        if not relative or not path.is_file():
            issues.append(
                BootstrapIssue(
                    "file_missing", "Evidence file does not exist.", relative
                )
            )
        if (
            not isinstance(span, Mapping)
            or not isinstance(span.get("start_line"), int)
            or not isinstance(span.get("end_line"), int)
        ):
            issues.append(
                BootstrapIssue(
                    "file_span_invalid",
                    "Evidence file needs integer start_line/end_line.",
                    relative,
                )
            )
        elif span["start_line"] < 1 or span["start_line"] > span["end_line"]:
            issues.append(
                BootstrapIssue(
                    "file_span_invalid",
                    "Evidence span must be a positive ordered range.",
                    relative,
                )
            )

    return BootstrapValidationReport(not issues, tuple(issues))


def _tracked_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        paths = result.stdout.splitlines()
    else:
        paths = [
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        ]
    return tuple(sorted(path for path in paths if _is_bootstrap_file(path)))


def _is_bootstrap_file(path: str) -> bool:
    return (
        path not in _IGNORED_FILES
        and not path.startswith(_IGNORED_PREFIXES)
        and not path.startswith("docs/changelogs/")
    )


def _load_spec_documents(
    root: Path, paths: Sequence[str]
) -> list[tuple[str, Mapping[str, Any]]]:
    documents: list[tuple[str, Mapping[str, Any]]] = []
    for relative in paths:
        try:
            raw = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, Mapping):
            documents.append((relative, raw))
    return documents


def _build_document(
    *,
    root: Path,
    taxonomy: EntityTaxonomy,
    tracked_files: Sequence[str],
    spec_paths: Sequence[str],
    spec_documents: Sequence[tuple[str, Mapping[str, Any]]],
    change_id: str,
    title: str,
) -> dict[str, Any]:
    allowed = set(taxonomy.entity_types)
    entities: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}

    repository_id = f"repository:{root.name}"
    entities[repository_id] = {
        "id": repository_id,
        "type": "Repository",
        "action": "added",
    }
    for relative in tracked_files:
        entity_type = _file_entity_type(relative)
        if entity_type not in allowed:
            continue
        entity_id = f"file:{relative}"
        entities[entity_id] = {"id": entity_id, "type": entity_type, "action": "added"}

    for spec_path, spec in spec_documents:
        for raw_entity in _mappings(spec.get("entities")):
            entity_id = _text(raw_entity.get("id"))
            entity_type = _text(raw_entity.get("type"))
            if entity_id and entity_type in allowed:
                entities.setdefault(
                    entity_id, {"id": entity_id, "type": entity_type, "action": "added"}
                )
        for raw_relationship in _mappings(spec.get("entity_relationships")):
            source = _text(raw_relationship.get("source"))
            target = _text(raw_relationship.get("target"))
            relationship = _text(raw_relationship.get("relationship")) or "relates_to"
            if source in entities and target in entities:
                relationship_id = (
                    _text(raw_relationship.get("id"))
                    or f"{source}-{relationship}-{target}"
                )
                relationships.setdefault(
                    relationship_id,
                    {
                        "id": relationship_id,
                        "source": source,
                        "target": target,
                        "relationship": relationship,
                        "action": "added",
                        "description": _text(raw_relationship.get("description"))
                        or f"{source} {relationship} {target}.",
                        "rationale": _text(raw_relationship.get("rationale"))
                        or f"Inferred from {spec_path}.",
                    },
                )

    files: list[dict[str, Any]] = []
    structured_files = list(spec_paths)
    for relative in tracked_files:
        if relative in structured_files:
            continue
        line_count = len(
            (root / relative).read_text(encoding="utf-8", errors="replace").splitlines()
        )
        files.append(
            {
                "path": relative,
                "type": "modified",
                "summary": f"Capture {relative} as bootstrap source evidence.",
                "rationale": (
                    "Preserve a source anchor for future Structrr context lookup."
                ),
                "span": {"start_line": 1, "end_line": max(1, line_count)},
                "entities": [f"file:{relative}"],
            }
        )

    return {
        "schema": _SCHEMA,
        "change_id": change_id,
        "title": title,
        "intent": {
            "problem": (
                "The repository lacks a validated Structrr snapshot connecting "
                "product evidence to source files."
            ),
            "goal": (
                "Bootstrap a taxonomy-valid, source-anchored Structrr snapshot "
                "for subsequent intentional changes."
            ),
        },
        "human-decisions": [],
        "structured_files": structured_files,
        "files": files,
        "entities": [entities[key] for key in sorted(entities)],
        "entity_relationships": [relationships[key] for key in sorted(relationships)],
        "invariants": [],
        "guidance": [
            {
                "id": "bootstrap-review-before-edit",
                "action": "added",
                "description": (
                    "Review inferred entities and relationships before using this "
                    "snapshot to guide changes."
                ),
            }
        ],
        "features": [],
        "proposed_prs": [],
    }


def _file_entity_type(relative: str) -> str:
    name = Path(relative).name
    suffix = Path(relative).suffix.lower()
    if name in {"Dockerfile", "Makefile", "Justfile"} or name.endswith(".mk"):
        return "Build file"
    if suffix in {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
    }:
        return "Source file"
    if suffix in {".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".conf", ".env"}:
        return "Configuration file"
    if suffix in {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat"}:
        return "Script"
    if name.lower().startswith("readme"):
        return "README file"
    if name.lower().startswith("license"):
        return "License file"
    if "changelog" in name.lower():
        return "Changelog file"
    return "Source file"


def _mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
