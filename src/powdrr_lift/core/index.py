from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from powdrr_lift.change_log_parser import (
    ChangeEntity,
    ChangeFile,
    ChangeLog,
    Intent,
    Span,
    parse_change_log,
)
from powdrr_lift.change_log_template import (
    _resolve_default_branch,
    _resolve_repo_root,
)

_PR_FILE_NAME_RE = re.compile(r"^PR-(\d+)-changelog\.yaml$")
_PR_SUBJECT_RE = re.compile(r"\(#(?P<pr_number>\d+)\)")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True, slots=True)
class ChangelogDocument:
    pr_number: int
    changelog_path: Path
    changelog: ChangeLog
    commit_sha: str | None
    commit_timestamp: int | None
    commit_subject: str | None


@dataclass(frozen=True, slots=True)
class EntityOccurrence:
    entity_id: str
    entity_type: str | None
    action: str | None
    pr_number: int | None
    commit_sha: str | None
    commit_timestamp: int | None
    changelog_path: str | None


@dataclass(frozen=True, slots=True)
class RelationshipOccurrence:
    source: str
    target: str
    relationship: str | None
    action: str | None
    rationale: str | None
    pr_number: int | None
    commit_sha: str | None
    commit_timestamp: int | None
    changelog_path: str | None


@dataclass(frozen=True, slots=True)
class EntityGraph:
    entities: dict[str, tuple[EntityOccurrence, ...]] = field(default_factory=dict)
    relationships: tuple[RelationshipOccurrence, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    kind: str
    pr_number: int | None
    commit_sha: str | None
    commit_timestamp: int | None
    changelog_path: str | None
    title: str | None
    change_id: str | None
    intent_problem: str | None
    intent_goal: str | None
    file: str | None
    span: Span | None
    summary: str | None
    rationale: str | None
    affects: tuple[str, ...] = field(default_factory=tuple)
    change_index: int | None = None


@dataclass(frozen=True, slots=True)
class SourceIndex:
    repo_root: Path
    default_branch_name: str
    documents: list[ChangelogDocument] = field(default_factory=list)
    entity_graph: EntityGraph = field(default_factory=EntityGraph)
    changes: list[ProvenanceRecord] = field(default_factory=list)
    file_lines: dict[str, tuple[ProvenanceRecord | None, ...]] = field(
        default_factory=dict
    )

    def provenance_for(self, path: str, line_number: int) -> ProvenanceRecord | None:
        if line_number < 1:
            raise ValueError("Line numbers are 1-based.")

        file_lines = self.file_lines.get(path)
        if file_lines is None or line_number > len(file_lines):
            return None

        return file_lines[line_number - 1]


@dataclass(frozen=True, slots=True)
class _CommitRecord:
    sha: str
    parent_sha: str | None
    timestamp: int
    subject: str
    commit_body: str
    pr_number: int | None
    changelog_document: ChangelogDocument | None


@dataclass(frozen=True, slots=True)
class _FilePatch:
    status: str
    path: str
    old_path: str | None
    hunks: list[_PatchHunk]


@dataclass(frozen=True, slots=True)
class _PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


def build_changelog_index(
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
    changelog_dir: str | Path = "docs/changelogs",
) -> SourceIndex:
    repo_root_path = _resolve_repo_root(repo_root)
    default_branch_name = default_branch or _resolve_default_branch(repo_root_path)
    documents = _load_changelog_documents(repo_root_path, changelog_dir)
    document_by_pr = {document.pr_number: document for document in documents}
    commits = _load_mainline_commits(
        repo_root_path,
        default_branch_name,
        document_by_pr,
    )

    return _build_source_index(
        repo_root_path=repo_root_path,
        default_branch_name=default_branch_name,
        commits=commits,
        documents=list(document_by_pr.values()),
    )


def build_changelog_index_at_ref(
    repo_root: str | Path | None = None,
    ref: str | None = None,
    changelog_dir: str | Path = "docs/changelogs",
) -> SourceIndex:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_ref = ref or _resolve_default_branch(repo_root_path)
    documents = _load_changelog_documents_at_ref(
        repo_root_path,
        resolved_ref,
        changelog_dir=changelog_dir,
    )
    document_by_pr = {document.pr_number: document for document in documents}
    commits = _load_mainline_commits_at_ref(
        repo_root_path,
        resolved_ref,
        document_by_pr,
    )
    return _build_source_index(
        repo_root_path=repo_root_path,
        default_branch_name=resolved_ref,
        commits=commits,
        documents=list(document_by_pr.values()),
    )


def _load_changelog_documents(
    repo_root: Path,
    changelog_dir: str | Path,
) -> list[ChangelogDocument]:
    changelog_root = repo_root / Path(changelog_dir)
    if not changelog_root.exists():
        return []

    documents: list[ChangelogDocument] = []
    for changelog_path in sorted(changelog_root.glob("PR-*-changelog.yaml")):
        match = _PR_FILE_NAME_RE.match(changelog_path.name)
        if match is None:
            continue

        pr_number = int(match.group(1))
        content = changelog_path.read_text(encoding="utf-8")
        documents.append(
            ChangelogDocument(
                pr_number=pr_number,
                changelog_path=changelog_path,
                changelog=parse_change_log(content),
                commit_sha=None,
                commit_timestamp=None,
                commit_subject=None,
            )
        )

    return documents


def _load_changelog_documents_at_ref(
    repo_root: Path,
    ref: str,
    changelog_dir: str | Path = "docs/changelogs",
) -> list[ChangelogDocument]:
    changelog_root = Path(changelog_dir)
    listed_paths = _git_output(
        repo_root,
        "ls-tree",
        "-r",
        "--name-only",
        ref,
        str(changelog_root),
    )
    documents: list[ChangelogDocument] = []
    for path_text in sorted(listed_paths.splitlines()):
        if not path_text.startswith("docs/changelogs/PR-") or not path_text.endswith(
            "-changelog.yaml"
        ):
            continue

        pr_number_text = path_text.rsplit("/", maxsplit=1)[-1]
        pr_number_text = pr_number_text.removeprefix("PR-").removesuffix(
            "-changelog.yaml"
        )
        if not pr_number_text.isdigit():
            continue

        content = _git_output(repo_root, "show", f"{ref}:{path_text}")
        documents.append(
            ChangelogDocument(
                pr_number=int(pr_number_text),
                changelog_path=repo_root / path_text,
                changelog=parse_change_log(content),
                commit_sha=None,
                commit_timestamp=None,
                commit_subject=None,
            )
        )

    return documents


def _build_source_index(
    repo_root_path: Path,
    default_branch_name: str,
    commits: Sequence[_CommitRecord],
    documents: list[ChangelogDocument],
) -> SourceIndex:
    changes: list[ProvenanceRecord] = []
    line_state: dict[str, list[ProvenanceRecord | None]] = {}
    changes_by_commit_and_file: dict[tuple[str, str], list[ProvenanceRecord]] = {}
    entity_graph = _build_entity_graph(documents)

    for commit in commits:
        file_patches = _collect_file_patches(
            repo_root_path,
            commit.sha,
            commit.parent_sha,
        )
        commit_changes = _build_commit_changes(
            repo_root_path,
            commit,
            file_patches,
        )
        changes.extend(commit_changes)
        for record in commit_changes:
            if record.file is None:
                continue

            changes_by_commit_and_file.setdefault((commit.sha, record.file), []).append(
                record
            )

        for file_patch in file_patches:
            _apply_file_patch(
                line_state=line_state,
                commit=commit,
                file_patch=file_patch,
                commit_changes=commit_changes,
            )

    _normalize_line_state(repo_root_path, line_state)
    _backfill_line_state_from_blame(
        repo_root_path,
        line_state,
        changes_by_commit_and_file,
    )
    return SourceIndex(
        repo_root=repo_root_path,
        default_branch_name=default_branch_name,
        documents=documents,
        entity_graph=entity_graph,
        changes=changes,
        file_lines={path: tuple(lines) for path, lines in sorted(line_state.items())},
    )


def _load_mainline_commits(
    repo_root: Path,
    default_branch_name: str,
    document_by_pr: dict[int, ChangelogDocument],
) -> list[_CommitRecord]:
    output = _git_output(
        repo_root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x1f%ct%x1f%s%x1f%b%x1f%P%x1e",
        default_branch_name,
    )
    commits: list[_CommitRecord] = []
    for sha, timestamp_text, subject, commit_body, parents in _parse_commit_log_output(
        output
    ):
        parent_sha = parents.split(" ", maxsplit=1)[0] if parents else None
        pr_number = _extract_pr_number(subject)
        if pr_number is None:
            commits.append(
                _CommitRecord(
                    sha=sha,
                    parent_sha=parent_sha,
                    timestamp=int(timestamp_text),
                    subject=subject,
                    commit_body=commit_body,
                    pr_number=None,
                    changelog_document=None,
                )
            )
            continue

        changelog_document: ChangelogDocument | None = document_by_pr.get(pr_number)
        if changelog_document is None:
            changelog_document = _load_pr_description_document(repo_root, pr_number)
            if changelog_document is not None:
                document_by_pr[pr_number] = changelog_document

        if changelog_document is not None:
            changelog_document = ChangelogDocument(
                pr_number=changelog_document.pr_number,
                changelog_path=changelog_document.changelog_path,
                changelog=changelog_document.changelog,
                commit_sha=sha,
                commit_timestamp=int(timestamp_text),
                commit_subject=subject,
            )
            document_by_pr[pr_number] = changelog_document

        commits.append(
            _CommitRecord(
                sha=sha,
                parent_sha=parent_sha,
                timestamp=int(timestamp_text),
                subject=subject,
                commit_body=commit_body,
                pr_number=pr_number,
                changelog_document=changelog_document,
            )
        )

    return commits


def _load_mainline_commits_at_ref(
    repo_root: Path,
    ref: str,
    document_by_pr: dict[int, ChangelogDocument],
) -> list[_CommitRecord]:
    output = _git_output(
        repo_root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x1f%ct%x1f%s%x1f%b%x1f%P%x1e",
        ref,
    )
    commits: list[_CommitRecord] = []
    for sha, timestamp_text, subject, commit_body, parents in _parse_commit_log_output(
        output
    ):
        parent_sha = parents.split(" ", maxsplit=1)[0] if parents else None
        pr_number = _extract_pr_number(subject)
        changelog_document: ChangelogDocument | None = None
        if pr_number is not None and pr_number in document_by_pr:
            changelog_document = document_by_pr[pr_number]
            changelog_document = ChangelogDocument(
                pr_number=changelog_document.pr_number,
                changelog_path=changelog_document.changelog_path,
                changelog=changelog_document.changelog,
                commit_sha=sha,
                commit_timestamp=int(timestamp_text),
                commit_subject=subject,
            )
            document_by_pr[pr_number] = changelog_document
        else:
            changelog_document = (
                None if pr_number is None else document_by_pr.get(pr_number)
            )

        commits.append(
            _CommitRecord(
                sha=sha,
                parent_sha=parent_sha,
                timestamp=int(timestamp_text),
                subject=subject,
                commit_body=commit_body,
                pr_number=pr_number,
                changelog_document=changelog_document,
            )
        )

    return commits


def _load_pr_description_document(
    repo_root: Path,
    pr_number: int,
) -> ChangelogDocument | None:
    metadata = _fetch_pr_metadata(repo_root, "view", str(pr_number))
    if metadata is None:
        return None

    return _build_pr_description_document(repo_root, metadata)


def _load_branch_pr_description_document(
    repo_root: Path,
    branch_name: str,
    parent_branch: str,
) -> ChangelogDocument | None:
    metadata = _fetch_pr_metadata(
        repo_root,
        "list",
        "--head",
        branch_name,
        "--base",
        parent_branch,
        "--state",
        "all",
        "--limit",
        "1",
    )
    if metadata is None:
        return None

    return _build_pr_description_document(repo_root, metadata)


def _build_pr_description_document(
    repo_root: Path,
    metadata: dict[str, object],
) -> ChangelogDocument:
    pr_number = int(str(metadata["number"]))
    title = str(metadata.get("title") or f"PR {pr_number}")
    body = str(metadata.get("body") or "")
    description_path = _description_changelog_path(repo_root, pr_number)
    return ChangelogDocument(
        pr_number=pr_number,
        changelog_path=description_path,
        changelog=ChangeLog(
            version=1,
            change_id=str(pr_number),
            title=title,
            intent=Intent(
                problem=title,
                goal=body.strip() or title,
            ),
        ),
        commit_sha=None,
        commit_timestamp=None,
        commit_subject=None,
    )


def _description_changelog_path(repo_root: Path, pr_number: int) -> Path:
    return repo_root / ".powdrr-lift" / "state" / f"PR-{pr_number}-description.md"


def _fetch_pr_metadata(
    repo_root: Path,
    *gh_args: str,
) -> dict[str, object] | None:
    try:
        output = _gh_output(
            repo_root,
            *gh_args,
            "--json",
            "number,title,body",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    data = json.loads(output)
    if isinstance(data, list):
        if not data:
            return None

        data = data[0]

    if not isinstance(data, dict):
        return None

    return data


def _gh_output(repo_root: Path, *args: str) -> str:
    process = subprocess.run(
        ["gh", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout


def _build_commit_changes(
    repo_root: Path,
    commit: _CommitRecord,
    file_patches: Sequence[_FilePatch],
) -> list[ProvenanceRecord]:
    if commit.changelog_document is None:
        if commit.pr_number is None:
            return [
                _build_commit_comment_provenance(
                    commit=commit,
                    file_patch=file_patch,
                )
                for file_patch in file_patches
            ]

        fallback_document = _load_pr_description_document(repo_root, commit.pr_number)
        if fallback_document is None:
            return []

        commit_document = fallback_document
    else:
        commit_document = commit.changelog_document

    declared_changes_by_file = _group_declared_changes(commit_document)
    if commit.pr_number is not None and any(
        not _is_changelog_artifact_path(file_patch.path, commit.pr_number)
        and file_patch.path not in declared_changes_by_file
        for file_patch in file_patches
    ):
        fallback_document = _load_pr_description_document(repo_root, commit.pr_number)
        if fallback_document is not None:
            declared_changes_by_file = _group_declared_changes(fallback_document)

    commit_changes: list[ProvenanceRecord] = []
    for file_patch in file_patches:
        if _is_changelog_artifact_path(file_patch.path, commit.pr_number):
            commit_changes.append(
                _build_artifact_provenance(
                    repo_root=repo_root,
                    commit=commit,
                    changelog_document=commit_document,
                    file_patch=file_patch,
                )
            )
            continue

        declared_changes = declared_changes_by_file.get(file_patch.path, [])
        if declared_changes:
            for file_change, file_change_index in declared_changes:
                commit_changes.append(
                    _build_declared_provenance(
                        commit=commit,
                        changelog_document=commit_document,
                        file_patch=file_patch,
                        file_entry=file_change,
                        file_change_index=file_change_index,
                    )
                )
            continue

        commit_changes.append(
            _build_implicit_provenance(
                repo_root=repo_root,
                commit=commit,
                changelog_document=fallback_document or commit_document,
                file_patch=file_patch,
            )
        )

    return commit_changes


def _build_entity_graph(documents: Sequence[ChangelogDocument]) -> EntityGraph:
    entity_occurrences: dict[str, list[EntityOccurrence]] = {}
    relationships: list[RelationshipOccurrence] = []
    seen_documents: set[str] = set()

    for changelog_document in documents:
        changelog_path = str(changelog_document.changelog_path)
        if changelog_path in seen_documents:
            continue

        seen_documents.add(changelog_path)
        document_entities = _document_entity_changes(changelog_document)
        entity_ids_in_document = {
            normalized_entity_id
            for entity in document_entities
            if (normalized_entity_id := _normalize_entity_id(entity.id)) is not None
        }
        for entity in document_entities:
            entity_id = _normalize_entity_id(entity.id)
            if entity_id is None:
                continue

            entity_occurrences.setdefault(entity_id, []).append(
                EntityOccurrence(
                    entity_id=entity_id,
                    entity_type=entity.type,
                    action=entity.action,
                    pr_number=changelog_document.pr_number,
                    commit_sha=changelog_document.commit_sha,
                    commit_timestamp=changelog_document.commit_timestamp,
                    changelog_path=changelog_path,
                )
            )

        for relationship_change in (
            changelog_document.changelog.entity_relationship_changes or []
        ):
            source = _normalize_entity_id(relationship_change.source)
            target = _normalize_entity_id(relationship_change.target)
            if (
                source is None
                or target is None
                or source not in entity_ids_in_document
                or target not in entity_ids_in_document
            ):
                continue

            relationships.append(
                RelationshipOccurrence(
                    source=source,
                    target=target,
                    relationship=relationship_change.relationship,
                    action=relationship_change.action,
                    rationale=relationship_change.rationale,
                    pr_number=changelog_document.pr_number,
                    commit_sha=changelog_document.commit_sha,
                    commit_timestamp=changelog_document.commit_timestamp,
                    changelog_path=changelog_path,
                )
            )

    return EntityGraph(
        entities={
            entity_id: tuple(occurrences)
            for entity_id, occurrences in sorted(entity_occurrences.items())
        },
        relationships=tuple(relationships),
    )


def _document_entity_changes(document: ChangelogDocument) -> list[ChangeEntity]:
    if document.changelog.version != 1 or not document.changelog_path.exists():
        return list(document.changelog.entity_changes or [])

    try:
        raw_content = yaml.safe_load(
            document.changelog_path.read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        return list(document.changelog.entity_changes or [])

    if not isinstance(raw_content, dict):
        return list(document.changelog.entity_changes or [])

    raw_entities = raw_content.get("entities")
    if not isinstance(raw_entities, list):
        return list(document.changelog.entity_changes or [])

    parsed_entities: list[ChangeEntity] = []
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            continue

        parsed_entities.append(
            ChangeEntity(
                id=_normalize_entity_id(raw_entity.get("id")),
                type=(
                    None
                    if raw_entity.get("type") is None
                    else str(raw_entity.get("type")).strip() or None
                ),
                action=(
                    None
                    if raw_entity.get("action") is None
                    else str(raw_entity.get("action")).strip() or None
                ),
            )
        )

    return parsed_entities or list(document.changelog.entity_changes or [])


def _file_change_entity_ids(file_change: ChangeFile) -> list[str]:
    return list(file_change.entities or file_change.related.entities)


def _build_declared_provenance(
    commit: _CommitRecord,
    changelog_document: ChangelogDocument,
    file_patch: _FilePatch,
    file_entry: ChangeFile,
    file_change_index: int,
) -> ProvenanceRecord:
    return ProvenanceRecord(
        kind="declared",
        pr_number=commit.pr_number,
        commit_sha=commit.sha,
        commit_timestamp=commit.timestamp,
        changelog_path=str(changelog_document.changelog_path),
        title=changelog_document.changelog.title,
        change_id=changelog_document.changelog.change_id,
        intent_problem=changelog_document.changelog.intent.problem,
        intent_goal=changelog_document.changelog.intent.goal,
        file=file_patch.path,
        span=file_entry.span,
        summary=file_entry.summary,
        rationale=file_entry.rationale,
        affects=_normalize_entity_ids(_file_change_entity_ids(file_entry)),
        change_index=file_change_index,
    )


def _build_artifact_provenance(
    repo_root: Path,
    commit: _CommitRecord,
    changelog_document: ChangelogDocument,
    file_patch: _FilePatch,
) -> ProvenanceRecord:
    line_count = _read_commit_line_count(repo_root, commit.sha, file_patch.path)
    span = Span(start_line=1, end_line=line_count)
    return ProvenanceRecord(
        kind="artifact",
        pr_number=commit.pr_number,
        commit_sha=commit.sha,
        commit_timestamp=commit.timestamp,
        changelog_path=str(changelog_document.changelog_path),
        title=changelog_document.changelog.title,
        change_id=changelog_document.changelog.change_id,
        intent_problem=changelog_document.changelog.intent.problem,
        intent_goal=changelog_document.changelog.intent.goal,
        file=file_patch.path,
        span=span,
        summary=f"Create changelog artifact for PR {commit.pr_number}",
        rationale="Store the validated PR changelog alongside the code change.",
        affects=(),
        change_index=None,
    )


def _build_commit_comment_provenance(
    commit: _CommitRecord,
    file_patch: _FilePatch,
) -> ProvenanceRecord:
    comment_body = commit.commit_body.strip()
    return ProvenanceRecord(
        kind="commented",
        pr_number=None,
        commit_sha=commit.sha,
        commit_timestamp=commit.timestamp,
        changelog_path=str(_commit_comment_path(commit.sha)),
        title=commit.subject,
        change_id=None,
        intent_problem=commit.subject,
        intent_goal=comment_body or commit.subject,
        file=file_patch.path,
        span=_resolve_patch_span(file_patch),
        summary=commit.subject,
        rationale=comment_body or "No commit body was provided.",
        affects=(),
        change_index=None,
    )


def _build_implicit_provenance(
    repo_root: Path,
    commit: _CommitRecord,
    changelog_document: ChangelogDocument,
    file_patch: _FilePatch,
) -> ProvenanceRecord:
    span = _resolve_patch_span(file_patch)
    return ProvenanceRecord(
        kind="implicit",
        pr_number=commit.pr_number,
        commit_sha=commit.sha,
        commit_timestamp=commit.timestamp,
        changelog_path=str(changelog_document.changelog_path),
        title=changelog_document.changelog.title,
        change_id=changelog_document.changelog.change_id,
        intent_problem=changelog_document.changelog.intent.problem,
        intent_goal=changelog_document.changelog.intent.goal,
        file=file_patch.path,
        span=span,
        summary=f"Unlisted change in {file_patch.path}",
        rationale=(
            "Captured from the PR changelog because no explicit file entry matched."
        ),
        affects=(),
        change_index=None,
    )


def _commit_comment_path(commit_sha: str) -> Path:
    return Path(".powdrr-lift") / "state" / f"commit-{commit_sha[:12]}-comment.md"


def _group_declared_changes(
    changelog_document: ChangelogDocument,
) -> dict[str, list[tuple[ChangeFile, int]]]:
    grouped: dict[str, list[tuple[ChangeFile, int]]] = {}
    for file_change_index, file_change in enumerate(
        changelog_document.changelog.file_changes
    ):
        if file_change.path is None or file_change.path == "":
            continue

        grouped.setdefault(file_change.path, []).append(
            (file_change, file_change_index)
        )

    return grouped


def _apply_file_patch(
    line_state: dict[str, list[ProvenanceRecord | None]],
    commit: _CommitRecord,
    file_patch: _FilePatch,
    commit_changes: Sequence[ProvenanceRecord],
) -> None:
    if file_patch.status.startswith("D"):
        line_state.pop(file_patch.path, None)
        if file_patch.old_path is not None and file_patch.old_path != file_patch.path:
            line_state.pop(file_patch.old_path, None)
        return

    if file_patch.status.startswith("R") and file_patch.old_path is not None:
        previous_lines = line_state.pop(file_patch.old_path, [])
        line_state[file_patch.path] = list(previous_lines)
    elif file_patch.status.startswith("C") and file_patch.old_path is not None:
        previous_lines = line_state.get(file_patch.old_path, [])
        line_state[file_patch.path] = list(previous_lines)
    elif file_patch.path not in line_state:
        line_state[file_patch.path] = []

    current_lines = line_state[file_patch.path]
    commit_changes_for_file = [
        change for change in commit_changes if change.file == file_patch.path
    ]
    next_lines = _apply_patch(
        current_lines,
        file_patch.hunks,
        lambda new_line_number: _resolve_line_origin(
            file_patch.path,
            new_line_number,
            commit,
            commit_changes_for_file,
        ),
    )
    line_state[file_patch.path] = next_lines


def _apply_patch(
    old_lines: Sequence[ProvenanceRecord | None],
    hunks: Sequence[_PatchHunk],
    resolve_origin: Callable[[int], ProvenanceRecord | None],
) -> list[ProvenanceRecord | None]:
    new_lines: list[ProvenanceRecord | None] = []
    old_index = 1
    new_index = 1

    for hunk in hunks:
        while old_index < hunk.old_start and new_index < hunk.new_start:
            new_lines.append(_safe_line(old_lines, old_index))
            old_index += 1
            new_index += 1

        for line in hunk.lines:
            if line.startswith(" "):
                new_lines.append(_safe_line(old_lines, old_index))
                old_index += 1
                new_index += 1
                continue

            if line.startswith("-"):
                old_index += 1
                continue

            if line.startswith("+"):
                new_lines.append(resolve_origin(new_index))
                new_index += 1
                continue

    while old_index <= len(old_lines):
        new_lines.append(_safe_line(old_lines, old_index))
        old_index += 1
        new_index += 1

    return new_lines


def _resolve_line_origin(
    file_path: str,
    new_line_number: int,
    commit: _CommitRecord,
    commit_changes_for_file: Sequence[ProvenanceRecord],
) -> ProvenanceRecord | None:
    if not commit_changes_for_file:
        return None

    if _is_changelog_artifact_path(file_path, commit.pr_number):
        return commit_changes_for_file[0]

    matching_changes = [
        change
        for change in commit_changes_for_file
        if change.span is not None
        and change.span.start_line is not None
        and change.span.end_line is not None
        and change.span.start_line <= new_line_number <= change.span.end_line
    ]
    if matching_changes:
        return min(
            matching_changes,
            key=_span_sort_key,
        )

    return commit_changes_for_file[0]


def _normalize_line_state(
    repo_root: Path,
    line_state: dict[str, list[ProvenanceRecord | None]],
) -> None:
    for file_path in _tracked_files(repo_root):
        current_line_count = _read_worktree_line_count(repo_root, file_path)
        file_lines = line_state.get(file_path)
        if file_lines is None:
            line_state[file_path] = [None] * current_line_count
            continue

        if len(file_lines) < current_line_count:
            file_lines.extend([None] * (current_line_count - len(file_lines)))
            continue

        if len(file_lines) > current_line_count:
            del file_lines[current_line_count:]


def _backfill_line_state_from_blame(
    repo_root: Path,
    line_state: dict[str, list[ProvenanceRecord | None]],
    changes_by_commit_and_file: dict[tuple[str, str], list[ProvenanceRecord]],
) -> None:
    for file_path, file_lines in line_state.items():
        if all(provenance is not None for provenance in file_lines):
            continue

        blame_output = _git_output(
            repo_root,
            "blame",
            "--line-porcelain",
            "--",
            file_path,
        )
        if blame_output == "":
            continue

        for commit_sha, start_line, line_count in _parse_blame_porcelain(blame_output):
            records = changes_by_commit_and_file.get((commit_sha, file_path))
            if not records:
                continue

            provenance = _select_backfill_provenance(records, start_line)
            if provenance is None:
                continue

            for line_number in range(start_line, start_line + line_count):
                index = line_number - 1
                if 0 <= index < len(file_lines) and file_lines[index] is None:
                    file_lines[index] = provenance


def _collect_file_patches(
    repo_root: Path,
    commit_sha: str,
    parent_sha: str | None,
) -> list[_FilePatch]:
    if parent_sha is None:
        return []

    name_status_output = _git_output(
        repo_root,
        "diff",
        "--name-status",
        "--find-renames",
        "--find-copies",
        parent_sha,
        commit_sha,
    )
    patches: list[_FilePatch] = []
    for line in name_status_output.splitlines():
        if not line:
            continue

        parts = line.split("\t")
        status = parts[0]
        old_path: str | None
        if status.startswith(("R", "C")) and len(parts) >= 3:
            old_path = parts[1]
            path = parts[2]
        else:
            old_path = None if status != "D" else parts[1]
            path = parts[-1]

        patch_output = _git_output(
            repo_root,
            "diff",
            "--unified=0",
            "--no-color",
            "--find-renames",
            "--find-copies",
            parent_sha,
            commit_sha,
            "--",
            path,
        )
        patches.append(
            _FilePatch(
                status=status,
                path=path,
                old_path=old_path,
                hunks=_parse_patch_hunks(patch_output),
            )
        )

    return patches


def _parse_patch_hunks(patch_output: str) -> list[_PatchHunk]:
    hunks: list[_PatchHunk] = []
    current_hunk: _PatchHunk | None = None
    for line in patch_output.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match is not None:
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = _PatchHunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or "1"),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or "1"),
                lines=[],
            )
            continue

        if current_hunk is not None and line[:1] in {" ", "+", "-"}:
            current_hunk.lines.append(line)

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


def _parse_blame_porcelain(blame_output: str) -> list[tuple[str, int, int]]:
    blame_entries: list[tuple[str, int, int]] = []
    for line in blame_output.splitlines():
        if len(line) < 41:
            continue

        commit_sha = line[:40]
        if any(character not in "0123456789abcdef" for character in commit_sha):
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        try:
            final_line = int(parts[2])
            line_count = int(parts[3])
        except ValueError:
            continue

        blame_entries.append((commit_sha, final_line, line_count))

    return blame_entries


def _select_backfill_provenance(
    records: Sequence[ProvenanceRecord],
    line_number: int,
) -> ProvenanceRecord | None:
    matching_records = [
        record
        for record in records
        if record.span is not None
        and record.span.start_line is not None
        and record.span.end_line is not None
        and record.span.start_line <= line_number <= record.span.end_line
    ]
    if matching_records:
        return min(matching_records, key=_span_sort_key)

    return records[0] if records else None


def _resolve_patch_span(file_patch: _FilePatch) -> Span:
    span_ranges: list[tuple[int, int]] = []
    for hunk in file_patch.hunks:
        if hunk.new_count > 0:
            span_ranges.append((hunk.new_start, hunk.new_start + hunk.new_count - 1))
        elif hunk.old_count > 0:
            span_ranges.append((hunk.old_start, hunk.old_start + hunk.old_count - 1))

    if not span_ranges:
        return Span(start_line=1, end_line=1)

    return Span(
        start_line=min(span[0] for span in span_ranges),
        end_line=max(span[1] for span in span_ranges),
    )


def _span_sort_key(change: ProvenanceRecord) -> tuple[int, int]:
    span = change.span
    if span is None:
        return (0, change.change_index or 0)

    start_line = span.start_line or 0
    end_line = span.end_line or start_line
    return (end_line - start_line, change.change_index or 0)


def _read_commit_line_count(repo_root: Path, commit_sha: str, path: str) -> int:
    content = _git_output(repo_root, "show", f"{commit_sha}:{path}")
    if content == "":
        return 0

    return len(content.splitlines())


def _read_worktree_line_count(repo_root: Path, path: str) -> int:
    file_path = repo_root / path
    if not file_path.exists():
        return 0

    content = file_path.read_text(encoding="utf-8")
    if content == "":
        return 0

    return len(content.splitlines())


def _tracked_files(repo_root: Path) -> list[str]:
    output = _git_output(repo_root, "ls-files")
    return [line for line in output.splitlines() if line]


def _safe_line(
    lines: Sequence[ProvenanceRecord | None],
    line_number: int,
) -> ProvenanceRecord | None:
    if line_number < 1 or line_number > len(lines):
        return None

    return lines[line_number - 1]


def _is_changelog_artifact_path(path: str, pr_number: int | None) -> bool:
    if pr_number is None:
        return False

    return path == f"docs/changelogs/PR-{pr_number}-changelog.yaml"


def _extract_pr_number(subject: str) -> int | None:
    match = _PR_SUBJECT_RE.search(subject)
    if match is None:
        return None

    return int(match.group("pr_number"))


def _normalize_entity_id(entity_id: str | None) -> str | None:
    if entity_id is None:
        return None

    normalized_entity_id = entity_id.strip()
    return normalized_entity_id or None


def _normalize_entity_ids(entity_ids: Sequence[str]) -> tuple[str, ...]:
    normalized_entity_ids: list[str] = []
    seen_entity_ids: set[str] = set()
    for entity_id in entity_ids:
        normalized_entity_id = _normalize_entity_id(entity_id)
        if normalized_entity_id is None or normalized_entity_id in seen_entity_ids:
            continue

        seen_entity_ids.add(normalized_entity_id)
        normalized_entity_ids.append(normalized_entity_id)

    return tuple(normalized_entity_ids)


def _git_output(repo_root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout


def _parse_commit_log_output(
    output: str,
) -> list[tuple[str, str, str, str, str]]:
    records: list[tuple[str, str, str, str, str]] = []
    for record in output.split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue

        parts = record.split("\x1f", maxsplit=4)
        if len(parts) != 5:
            continue

        sha, timestamp_text, subject, commit_body, parents = parts
        records.append((sha, timestamp_text, subject, commit_body, parents))

    return records
