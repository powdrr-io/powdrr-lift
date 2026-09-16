"""Deterministically bootstrap a validated Structrr snapshot from a repository."""

from __future__ import annotations

import ast
import re
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
_BOOTSTRAP_SCHEMA = "https://powdrr.io/schema/structrr-bootstrap-v1"
_DEFAULT_OUTPUT_DIRECTORY = Path("docs/structrr")
_IGNORED_PREFIXES = (
    ".git/",
    ".github/",
    ".worktrees/",
    "node_modules/",
    "vendor/",
)
_IGNORED_FILES = {".DS_Store"}
_SOURCE_FILE_TYPES = {"Build file", "Configuration file", "Script", "Source file"}
_SOURCE_LINKABLE_TYPES = {
    "Application",
    "Build file",
    "CLI app",
    "Class",
    "Command",
    "Configuration file",
    "Function",
    "Interface",
    "Library",
    "Method",
    "Module",
    "Package",
    "Plugin",
    "Script",
    "Service",
    "Skill",
    "Tool",
}
_STATEMENT_KINDS = {
    "requirement",
    "invariant",
    "constraint",
    "validation_rule",
    "approach",
    "guidance",
    "acceptance_criterion",
}
_PYTHON_SOURCE_SUFFIXES = {".py", ".pyi"}


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
    selected_output = (
        Path(output_path) if output_path is not None else _default_output_path(root)
    )
    resolved_output = (
        root / selected_output if not selected_output.is_absolute() else selected_output
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


def _default_output_path(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    short_hash = result.stdout.strip() if result.returncode == 0 else "unknown"
    return _DEFAULT_OUTPUT_DIRECTORY / f"baseline-{short_hash}.yaml"


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
    if document.get("structrr_schema") != _BOOTSTRAP_SCHEMA:
        issues.append(
            BootstrapIssue(
                "bootstrap_schema_invalid",
                "Bootstrap must declare structrr-bootstrap-v1.",
            )
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

    _validate_source_model(
        issues,
        document.get("source_subjects"),
        document.get("source_bindings"),
        entity_ids,
        root_path,
    )

    _validate_lifecycle_section(
        issues, document.get("invariants"), "invariant", root_path
    )
    _validate_lifecycle_section(issues, document.get("guidance"), "guidance", root_path)
    _validate_tools(issues, document.get("tools"), root_path)
    _validate_statements(issues, document.get("statements"), root_path)

    return BootstrapValidationReport(not issues, tuple(issues))


def _validate_lifecycle_section(
    issues: list[BootstrapIssue],
    value: object,
    kind: str,
    root: Path,
) -> None:
    entries = _validated_mapping_list(issues, value, kind)
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        item_id = _text(entry.get("id"))
        if not item_id:
            issues.append(
                BootstrapIssue(
                    f"{kind}_id_missing", f"{kind} id is required.", f"{kind}s[{index}]"
                )
            )
        elif item_id in seen:
            issues.append(
                BootstrapIssue(
                    f"{kind}_id_duplicate", f"Duplicate {kind} id {item_id!r}.", item_id
                )
            )
        seen.add(item_id)
        if _text(entry.get("action")) != "added":
            issues.append(
                BootstrapIssue(
                    f"{kind}_action_invalid", f"{kind} action must be added.", item_id
                )
            )
        if not _text(entry.get("description")):
            issues.append(
                BootstrapIssue(
                    f"{kind}_description_missing",
                    f"{kind} description is required.",
                    item_id,
                )
            )
        _validate_statement_metadata(issues, entry, kind, item_id)
        _validate_provenance(issues, entry, root, item_id)


def _validate_tools(issues: list[BootstrapIssue], value: object, root: Path) -> None:
    entries = _validated_mapping_list(issues, value, "tool")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        tool_id = _text(entry.get("id"))
        if not tool_id:
            issues.append(
                BootstrapIssue(
                    "tool_id_missing", "tool id is required.", f"tools[{index}]"
                )
            )
        elif tool_id in seen:
            issues.append(
                BootstrapIssue(
                    "tool_id_duplicate", f"Duplicate tool id {tool_id!r}.", tool_id
                )
            )
        seen.add(tool_id)
        if _text(entry.get("action")) != "added":
            issues.append(
                BootstrapIssue(
                    "tool_action_invalid", "Tool action must be added.", tool_id
                )
            )
        related_modules = entry.get("related_modules")
        if related_modules is not None and (
            not isinstance(related_modules, Sequence)
            or isinstance(related_modules, (str, bytes))
        ):
            issues.append(
                BootstrapIssue(
                    "tool_related_modules_invalid",
                    "Tool related_modules must be a list.",
                    tool_id,
                )
            )
        _validate_provenance(issues, entry, root, tool_id)


def _validate_statements(
    issues: list[BootstrapIssue], value: object, root: Path
) -> None:
    entries = _validated_mapping_list(issues, value, "statement")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        statement_id = _text(entry.get("id"))
        kind = _text(entry.get("kind"))
        if not statement_id:
            issues.append(
                BootstrapIssue(
                    "statement_id_missing",
                    "statement id is required.",
                    f"statements[{index}]",
                )
            )
        elif statement_id in seen:
            issues.append(
                BootstrapIssue(
                    "statement_id_duplicate",
                    f"Duplicate statement id {statement_id!r}.",
                    statement_id,
                )
            )
        seen.add(statement_id)
        if kind not in _STATEMENT_KINDS:
            issues.append(
                BootstrapIssue(
                    "statement_kind_invalid",
                    "Statement kind is not recognized.",
                    statement_id,
                )
            )
        declared_kind = _text(entry.get("declared_kind"))
        if declared_kind not in {"requirement", "invariant", "approach", "guidance"}:
            issues.append(
                BootstrapIssue(
                    "statement_declared_kind_invalid",
                    "Statement declared_kind is not recognized.",
                    statement_id,
                )
            )
        classification = _text(entry.get("classification"))
        if classification not in {"declared", "inferred"}:
            issues.append(
                BootstrapIssue(
                    "statement_classification_invalid",
                    "Statement classification must be declared or inferred.",
                    statement_id,
                )
            )
        if _text(entry.get("action")) != "added":
            issues.append(
                BootstrapIssue(
                    "statement_action_invalid",
                    "Statement action must be added.",
                    statement_id,
                )
            )
        if not _text(entry.get("description")):
            issues.append(
                BootstrapIssue(
                    "statement_description_missing",
                    "Statement description is required.",
                    statement_id,
                )
            )
        _validate_provenance(issues, entry, root, statement_id)


def _validate_provenance(
    issues: list[BootstrapIssue],
    entry: Mapping[str, Any],
    root: Path,
    item_id: str,
) -> None:
    source = _text(entry.get("source"))
    if source and not (root / source).is_file():
        issues.append(
            BootstrapIssue(
                "semantic_source_missing",
                "Semantic source file does not exist.",
                item_id,
            )
        )


def _validate_source_model(
    issues: list[BootstrapIssue],
    raw_subjects: object,
    raw_bindings: object,
    entity_ids: set[str],
    root: Path,
) -> None:
    subjects = _validated_mapping_list(issues, raw_subjects, "source_subject")
    subject_ids: set[str] = set()
    stable_keys: set[str] = set()
    for index, subject in enumerate(subjects):
        subject_id = _text(subject.get("id"))
        path_text = _text(subject.get("path"))
        span = subject.get("span")
        if not subject_id:
            issues.append(
                BootstrapIssue(
                    "source_subject_id_missing",
                    "Source subject id is required.",
                    f"source_subjects[{index}]",
                )
            )
        elif subject_id in subject_ids:
            issues.append(
                BootstrapIssue(
                    "source_subject_id_duplicate",
                    f"Duplicate source subject id {subject_id!r}.",
                    subject_id,
                )
            )
        subject_ids.add(subject_id)
        stable_key = _text(subject.get("stable_key"))
        if not stable_key:
            issues.append(
                BootstrapIssue(
                    "source_subject_stable_key_missing",
                    "Source subject stable_key is required.",
                    subject_id,
                )
            )
        elif stable_key in stable_keys:
            issues.append(
                BootstrapIssue(
                    "source_subject_stable_key_duplicate",
                    f"Duplicate source subject stable_key {stable_key!r}.",
                    subject_id,
                )
            )
        stable_keys.add(stable_key)
        if not path_text or not (root / path_text).is_file():
            issues.append(
                BootstrapIssue(
                    "source_subject_path_missing",
                    "Source subject path does not exist.",
                    subject_id,
                )
            )
        _validate_span(issues, span, "source_subject", subject_id)

    bindings = _validated_mapping_list(issues, raw_bindings, "source_binding")
    binding_ids: set[str] = set()
    for index, binding in enumerate(bindings):
        binding_id = _text(binding.get("id"))
        subject_id = _text(binding.get("subject_id"))
        entity_id = _text(binding.get("entity_id"))
        if not binding_id:
            issues.append(
                BootstrapIssue(
                    "source_binding_id_missing",
                    "Source binding id is required.",
                    f"source_bindings[{index}]",
                )
            )
        elif binding_id in binding_ids:
            issues.append(
                BootstrapIssue(
                    "source_binding_id_duplicate",
                    f"Duplicate source binding id {binding_id!r}.",
                    binding_id,
                )
            )
        binding_ids.add(binding_id)
        if subject_id not in subject_ids:
            issues.append(
                BootstrapIssue(
                    "source_binding_subject_unknown",
                    "Source binding subject_id must reference a source subject.",
                    binding_id,
                )
            )
        if entity_id not in entity_ids:
            issues.append(
                BootstrapIssue(
                    "source_binding_entity_unknown",
                    "Source binding entity_id must reference an entity.",
                    binding_id,
                )
            )
        binding_path = _text(binding.get("path"))
        if not binding_path or not (root / binding_path).is_file():
            issues.append(
                BootstrapIssue(
                    "source_binding_path_missing",
                    "Source binding path does not exist.",
                    binding_id,
                )
            )
        if _text(binding.get("relationship")) not in {
            "implemented_by",
            "defined_in",
            "verified_by",
        }:
            issues.append(
                BootstrapIssue(
                    "source_binding_relationship_invalid",
                    "Source binding relationship is not recognized.",
                    binding_id,
                )
            )
        _validate_span(issues, binding.get("span"), "source_binding", binding_id)


def _validate_span(
    issues: list[BootstrapIssue], value: object, kind: str, item_id: str
) -> None:
    if not isinstance(value, Mapping):
        issues.append(
            BootstrapIssue(
                "source_span_invalid", "Source span must be a mapping.", item_id
            )
        )
        return
    start = value.get("start_line")
    end = value.get("end_line")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or start > end
    ):
        issues.append(
            BootstrapIssue(
                "source_span_invalid",
                f"{kind} span must be a positive ordered line range.",
                item_id,
            )
        )


def _validate_statement_metadata(
    issues: list[BootstrapIssue],
    entry: Mapping[str, Any],
    declared_kind: str,
    item_id: str,
) -> None:
    normalized_kind = _text(entry.get("kind"))
    if normalized_kind not in _STATEMENT_KINDS:
        issues.append(
            BootstrapIssue(
                "lifecycle_kind_invalid",
                "Lifecycle statement kind is not recognized.",
                item_id,
            )
        )
    if _text(entry.get("declared_kind")) != declared_kind:
        issues.append(
            BootstrapIssue(
                "lifecycle_declared_kind_invalid",
                "Lifecycle declared_kind must match its source section.",
                item_id,
            )
        )
    if _text(entry.get("classification")) not in {"declared", "inferred"}:
        issues.append(
            BootstrapIssue(
                "lifecycle_classification_invalid",
                "Lifecycle classification must be declared or inferred.",
                item_id,
            )
        )


def _validated_mapping_list(
    issues: list[BootstrapIssue], value: object, kind: str
) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(BootstrapIssue(f"{kind}s_invalid", f"{kind}s must be a list."))
        return []
    entries: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            issues.append(
                BootstrapIssue(
                    f"{kind}_invalid",
                    f"{kind} must be a mapping.",
                    f"{kind}s[{index}]",
                )
            )
            continue
        entries.append(item)
    return entries


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


def _collect_lifecycle_items(
    destination: dict[str, dict[str, Any]],
    value: object,
    source: str,
    declared_kind: str,
) -> None:
    for raw_item in _mappings(value):
        item_id = _text(raw_item.get("id"))
        description = _text(raw_item.get("description"))
        if not item_id or not description:
            continue
        item: dict[str, Any] = {
            "id": item_id,
            "declared_kind": declared_kind,
            "action": "added",
            "description": description,
            "source": source,
        }
        kind, classification, reason = _classify_statement(declared_kind, description)
        item.update(
            {
                "kind": kind,
                "classification": classification,
                "classification_reason": reason,
            }
        )
        rationale = _text(raw_item.get("rationale"))
        if rationale:
            item["rationale"] = rationale
        related = _snapshot_related(raw_item.get("related"))
        if related:
            item["related"] = related
        destination.setdefault(item_id, item)


def _collect_tools(
    destination: dict[str, dict[str, Any]],
    value: object,
    source: str,
) -> None:
    for raw_tool in _mappings(value):
        tool_id = _text(raw_tool.get("id"))
        if not tool_id:
            continue
        tool = {str(key): value for key, value in raw_tool.items()}
        tool["id"] = tool_id
        tool["action"] = "added"
        tool["source"] = source
        destination.setdefault(tool_id, tool)


def _collect_statements(
    destination: dict[str, dict[str, Any]],
    kind: str,
    value: object,
    source: str,
) -> None:
    for raw_statement in _mappings(value):
        statement_id = _text(raw_statement.get("id"))
        description = _text(raw_statement.get("description"))
        if not statement_id or not description:
            continue
        statement: dict[str, Any] = {
            "id": statement_id,
            "declared_kind": kind,
            "kind": kind,
            "action": "added",
            "description": description,
            "source": source,
            "classification": "declared",
            "classification_reason": "Preserved from the specification section.",
        }
        state = _text(raw_statement.get("state"))
        if state:
            statement["state"] = state
        destination.setdefault(statement_id, statement)


def _classify_statement(declared_kind: str, description: str) -> tuple[str, str, str]:
    if declared_kind != "invariant":
        return (
            declared_kind,
            "declared",
            "Preserved from the specification section.",
        )

    normalized = description.lower()
    validation_markers = (
        "must resolve",
        "must be unique",
        "must have",
        "must point to",
        "must include",
        "must survive",
        "must remain intact",
        "validator",
        "round-trip",
        "roundtrip",
    )
    requirement_markers = (
        "must expose",
        "must support",
        "must install",
        "must be generated",
        "should expose",
    )
    constraint_markers = (
        "must not",
        "cannot",
        "never",
        "exactly once",
        "without altering",
        "owns ",
    )
    if any(marker in normalized for marker in validation_markers):
        return (
            "validation_rule",
            "inferred",
            "The statement describes validation or referential integrity.",
        )
    if any(marker in normalized for marker in requirement_markers):
        return (
            "requirement",
            "inferred",
            "The statement describes a desired product or system capability.",
        )
    if any(marker in normalized for marker in constraint_markers):
        return (
            "constraint",
            "inferred",
            "The statement describes a hard implementation or execution limit.",
        )
    return (
        "invariant",
        "declared",
        "No stronger normalized classification was inferred.",
    )


def _snapshot_related(value: object) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    related: dict[str, list[str]] = {}
    for key in ("entities", "entity_relationships", "invariants", "guidance"):
        values = value.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        normalized = [_text(item) for item in values if _text(item)]
        if normalized:
            related[key] = normalized
    return related


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
    spec_entity_sources: dict[str, tuple[str, str]] = {}
    invariants: dict[str, dict[str, Any]] = {}
    guidance: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    statements: dict[str, dict[str, Any]] = {}

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
                spec_entity_sources.setdefault(
                    entity_id,
                    (
                        entity_type,
                        " ".join(
                            value
                            for value in (
                                _text(raw_entity.get("summary")),
                                _text(raw_entity.get("rationale")),
                            )
                            if value
                        ),
                    ),
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
        _collect_lifecycle_items(
            invariants, spec.get("invariants"), spec_path, "invariant"
        )
        _collect_lifecycle_items(guidance, spec.get("guidance"), spec_path, "guidance")
        _collect_tools(tools, spec.get("tools"), spec_path)
        _collect_statements(
            statements, "requirement", spec.get("requirements"), spec_path
        )
        _collect_statements(statements, "approach", spec.get("approach"), spec_path)

    for item in (*invariants.values(), *guidance.values()):
        statements.setdefault(item["id"], item)

    for tool_id in tools:
        entities.setdefault(
            tool_id,
            {"id": tool_id, "type": "Tool", "action": "added"},
        )

    relationships.update(
        _infer_source_relationships(
            entities=entities,
            spec_entity_sources=spec_entity_sources,
            tracked_files=tracked_files,
        )
    )
    source_subjects, source_bindings = _extract_python_source_model(
        root=root,
        tracked_files=tracked_files,
        entities=entities,
        spec_entity_sources=spec_entity_sources,
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
        "structrr_schema": _BOOTSTRAP_SCHEMA,
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
        "source_subjects": source_subjects,
        "source_bindings": source_bindings,
        "invariants": [invariants[key] for key in sorted(invariants)],
        "guidance": [guidance[key] for key in sorted(guidance)]
        + [
            {
                "id": "bootstrap-review-before-edit",
                "declared_kind": "guidance",
                "kind": "guidance",
                "action": "added",
                "classification": "declared",
                "classification_reason": "Generated bootstrap review guidance.",
                "description": (
                    "Review inferred entities and relationships before using this "
                    "snapshot to guide changes."
                ),
            }
        ],
        "tools": [tools[key] for key in sorted(tools)],
        "statements": [statements[key] for key in sorted(statements)],
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
    if suffix in {".md", ".rst"}:
        return "Design doc"
    return "Source file"


def _extract_python_source_model(
    *,
    root: Path,
    tracked_files: Sequence[str],
    entities: Mapping[str, Mapping[str, Any]],
    spec_entity_sources: Mapping[str, tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    subjects: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for relative in tracked_files:
        if Path(relative).suffix.lower() not in _PYTHON_SOURCE_SUFFIXES:
            continue
        source_path = root / relative
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        module_subject = _make_source_subject(
            relative=relative,
            qualified_name=_python_module_name(relative),
            kind="module",
            start_line=1,
            end_line=max(1, len(lines)),
        )
        subjects.append(module_subject)
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node, qualified_name, kind in _walk_python_symbols(
            tree, _python_module_name(relative)
        ):
            subjects.append(
                _make_source_subject(
                    relative=relative,
                    qualified_name=qualified_name,
                    kind=kind,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )

    for subject in subjects:
        relative_path = str(subject["path"])
        file_entity_id = f"file:{relative_path}"
        subject["file_entity_id"] = file_entity_id
        for entity_id, (entity_type, _) in _source_entity_candidates(
            subject, spec_entity_sources
        ):
            if entity_id not in entities:
                continue
            relationship = _source_relationship_kind(relative_path)
            binding_id = (
                f"binding:{_binding_slug(subject['id'])}-{_binding_slug(entity_id)}"
            )
            bindings.append(
                {
                    "id": binding_id,
                    "subject_id": subject["id"],
                    "entity_id": entity_id,
                    "relationship": relationship,
                    "path": relative_path,
                    "span": subject["span"],
                    "confidence": "high"
                    if entity_type in _SOURCE_LINKABLE_TYPES
                    else "medium",
                    "evidence": "identifier and qualified-name match",
                }
            )
    subjects.sort(key=lambda subject: str(subject["id"]))
    bindings.sort(key=lambda binding: str(binding["id"]))
    return subjects, bindings


def _make_source_subject(
    *, relative: str, qualified_name: str, kind: str, start_line: int, end_line: int
) -> dict[str, Any]:
    subject_id = f"python:{relative}::{qualified_name}"
    return {
        "id": subject_id,
        "stable_key": f"python::{qualified_name}",
        "language": "python",
        "kind": kind,
        "qualified_name": qualified_name,
        "path": relative,
        "span": {"start_line": start_line, "end_line": end_line},
    }


def _walk_python_symbols(
    tree: ast.AST,
    module_name: str,
) -> list[tuple[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str, str]]:
    symbols: list[
        tuple[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str, str]
    ] = []

    def visit_body(
        body: Sequence[ast.stmt], prefix: tuple[str, ...], in_class: bool
    ) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualified_name = ".".join((*prefix, node.name))
                symbols.append((node, qualified_name, "class"))
                visit_body(node.body, (*prefix, node.name), True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = ".".join((*prefix, node.name))
                symbols.append(
                    (node, qualified_name, "method" if in_class else "function")
                )
                visit_body(node.body, (*prefix, node.name), False)

    if isinstance(tree, ast.Module):
        visit_body(tree.body, (module_name,), False)
    return symbols


def _python_module_name(relative: str) -> str:
    path = Path(relative)
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _source_entity_candidates(
    subject: Mapping[str, Any],
    spec_entity_sources: Mapping[str, tuple[str, str]],
) -> list[tuple[str, tuple[str, str]]]:
    subject_text = " ".join(
        str(subject[key]) for key in ("path", "qualified_name") if key in subject
    )
    subject_tokens = _identifier_tokens(subject_text)
    candidates: list[tuple[int, str, tuple[str, str]]] = []
    for entity_id, evidence in spec_entity_sources.items():
        entity_type, _ = evidence
        if entity_type not in _SOURCE_LINKABLE_TYPES:
            continue
        entity_tokens = _identifier_tokens(entity_id)
        if entity_tokens and entity_tokens <= subject_tokens:
            candidates.append((100, entity_id, evidence))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [(entity_id, evidence) for _, entity_id, evidence in candidates[:3]]


def _infer_source_relationships(
    *,
    entities: Mapping[str, Mapping[str, Any]],
    spec_entity_sources: Mapping[str, tuple[str, str]],
    tracked_files: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Link semantic entities to source files only when path evidence is strong."""
    source_files = [
        relative
        for relative in tracked_files
        if _file_entity_type(relative) in _SOURCE_FILE_TYPES
    ]
    inferred: dict[str, dict[str, Any]] = {}
    for entity_id, (entity_type, source_text) in spec_entity_sources.items():
        if entity_type not in _SOURCE_LINKABLE_TYPES:
            continue
        candidates = sorted(
            (
                (_source_link_score(entity_id, source_text, relative), relative)
                for relative in source_files
            ),
            reverse=True,
        )
        for score, relative in candidates[:3]:
            if score < 50:
                continue
            file_id = f"file:{relative}"
            if file_id not in entities:
                continue
            relationship_id = (
                f"{entity_id}-{_source_relationship_kind(relative)}-"
                f"{_relationship_slug(relative)}"
            )
            relationship = _source_relationship_kind(relative)
            inferred[relationship_id] = {
                "id": relationship_id,
                "source": entity_id,
                "target": file_id,
                "relationship": relationship,
                "action": "added",
                "description": (
                    f"{entity_id} is linked to {relative} by deterministic "
                    "identifier and path evidence."
                ),
                "rationale": (
                    f"Bootstrap source-link confidence score: {score}; "
                    "review inferred links before treating them as authoritative."
                ),
            }
    return inferred


def _source_link_score(entity_id: str, source_text: str, relative: str) -> int:
    entity_tokens = _identifier_tokens(entity_id)
    path_tokens = _identifier_tokens(relative)
    if not entity_tokens or not path_tokens:
        return 0
    entity_slug = "".join(sorted(entity_tokens))
    path_slug = "".join(sorted(path_tokens))
    if entity_slug in path_slug:
        return 100
    overlap = entity_tokens & path_tokens
    if len(entity_tokens) >= 2 and overlap == entity_tokens:
        return 85
    if len(entity_tokens) == 1 and entity_tokens <= path_tokens:
        return 75
    if entity_id.lower() in source_text.lower():
        return 55
    return 0


def _identifier_tokens(value: str) -> set[str]:
    tokens = {
        token.lower() for token in re.findall(r"[A-Za-z0-9]+", value) if len(token) > 1
    }
    if "changelog" in tokens:
        tokens.remove("changelog")
        tokens.update({"change", "log"})
    return tokens


def _relationship_slug(value: str) -> str:
    return "-".join(sorted(_identifier_tokens(value)))


def _source_relationship_kind(relative: str) -> str:
    if relative.startswith(("tests/", "hardening_tests/")):
        return "verified_by"
    if _file_entity_type(relative) == "Configuration file":
        return "defined_in"
    return "implemented_by"


def _binding_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def _mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
