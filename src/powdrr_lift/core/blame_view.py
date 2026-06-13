from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.code_index import CodeIndexStore, _current_branch
from powdrr_lift.core.index import ProvenanceRecord, SourceIndex
from powdrr_lift.core.pr_analysis import resolve_default_branch


@dataclass(frozen=True, slots=True)
class RepoTreeNode:
    name: str
    path: str
    kind: str
    children: list[RepoTreeNode] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BlameLine:
    line_number: int
    text: str
    provenance_ref: int | None


@dataclass(frozen=True, slots=True)
class BlameChunk:
    chunk_index: int
    start_line: int
    end_line: int
    provenance_ref: int | None
    lines: list[BlameLine] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BlameProvenance:
    ref: int
    kind: str
    pr_number: int | None
    commit_sha: str | None
    commit_timestamp: int | None
    changelog_path: str | None
    title: str | None
    change_id: str | None
    intent_problem: str | None
    intent_goal: str | None
    file_path: str | None
    span_start: int | None
    span_end: int | None
    summary: str | None
    rationale: str | None
    change_index: int | None


@dataclass(frozen=True, slots=True)
class BlameFileView:
    path: str
    line_count: int
    lines: list[BlameLine] = field(default_factory=list)
    chunks: list[BlameChunk] = field(default_factory=list)
    provenances: list[BlameProvenance] = field(default_factory=list)
    selected_chunk_ref: int | None = None


@dataclass(frozen=True, slots=True)
class BlameViewState:
    repo_root: Path
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    tree: list[RepoTreeNode] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    selected_file: str = ""
    file_view: BlameFileView | None = None


def build_blame_view_state(
    repo_root: str | Path | None = None,
    branch_name: str | None = None,
    parent_branch: str | None = None,
    selected_file: str | None = None,
) -> BlameViewState:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    resolved_parent = parent_branch or resolve_default_branch(repo_root_path)

    store = CodeIndexStore(repo_root_path)
    source_index = store.refresh(resolved_branch, resolved_parent)
    branch_state = store.branch_state_for(resolved_branch)
    if branch_state is None:
        raise RuntimeError(f"Missing branch state for {resolved_branch!r}.")

    tracked_files = _collect_tracked_files(repo_root_path)
    tree = build_repo_tree(tracked_files)
    resolved_selected_file = _resolve_selected_file(tracked_files, selected_file)
    file_view = (
        build_blame_file_view(
            repo_root_path,
            source_index,
            resolved_selected_file,
        )
        if resolved_selected_file is not None
        else None
    )

    return BlameViewState(
        repo_root=repo_root_path,
        branch_name=resolved_branch,
        parent_branch=resolved_parent,
        branch_head_sha=branch_state.branch_head_sha,
        parent_head_sha=branch_state.parent_head_sha,
        indexed_at=branch_state.indexed_at,
        tree=tree,
        files=tracked_files,
        selected_file="" if resolved_selected_file is None else resolved_selected_file,
        file_view=file_view,
    )


def build_blame_file_view(
    repo_root: Path,
    source_index: SourceIndex,
    file_path: str,
) -> BlameFileView:
    file_path_obj = repo_root / file_path
    text_lines = file_path_obj.read_text(encoding="utf-8").splitlines()
    provenance_ref_by_record: dict[ProvenanceRecord, int] = {}
    provenances: list[BlameProvenance] = []
    lines: list[BlameLine] = []

    for line_number, text in enumerate(text_lines, start=1):
        provenance = source_index.provenance_for(file_path, line_number)
        provenance_ref = None
        if provenance is not None:
            provenance_ref = provenance_ref_by_record.get(provenance)
            if provenance_ref is None:
                provenance_ref = len(provenances) + 1
                provenance_ref_by_record[provenance] = provenance_ref
                provenances.append(
                    _provenance_to_blame_provenance(provenance_ref, provenance)
                )

        lines.append(
            BlameLine(
                line_number=line_number,
                text=text,
                provenance_ref=provenance_ref,
            )
        )

    chunks = _build_chunks(lines)
    selected_chunk_ref = _select_default_chunk_ref(chunks)
    return BlameFileView(
        path=file_path,
        line_count=len(text_lines),
        lines=lines,
        chunks=chunks,
        provenances=provenances,
        selected_chunk_ref=selected_chunk_ref,
    )


def build_repo_tree(paths: Sequence[str]) -> list[RepoTreeNode]:
    root: dict[str, Any] = {}
    for path in paths:
        node = root
        parts = Path(path).parts
        for index, part in enumerate(parts):
            is_file = index == len(parts) - 1
            child = node.setdefault(
                part,
                {"kind": "file" if is_file else "dir", "children": {}},
            )
            node = child["children"]

    return [
        _tree_node_from_mapping(name, child, prefix="")
        for name, child in sorted(root.items(), key=lambda item: item[0])
    ]


def _build_chunks(lines: Sequence[BlameLine]) -> list[BlameChunk]:
    if not lines:
        return []

    chunks: list[BlameChunk] = []
    current_lines: list[BlameLine] = []
    current_ref = lines[0].provenance_ref
    current_start = lines[0].line_number

    for line in lines:
        if line.provenance_ref != current_ref and current_lines:
            chunks.append(
                BlameChunk(
                    chunk_index=len(chunks),
                    start_line=current_start,
                    end_line=current_lines[-1].line_number,
                    provenance_ref=current_ref,
                    lines=list(current_lines),
                )
            )
            current_lines = []
            current_ref = line.provenance_ref
            current_start = line.line_number

        current_lines.append(line)

    if current_lines:
        chunks.append(
            BlameChunk(
                chunk_index=len(chunks),
                start_line=current_start,
                end_line=current_lines[-1].line_number,
                provenance_ref=current_ref,
                lines=list(current_lines),
            )
        )

    return chunks


def _select_default_chunk_ref(chunks: Sequence[BlameChunk]) -> int | None:
    for chunk in chunks:
        if chunk.provenance_ref is not None:
            return chunk.provenance_ref

    return chunks[0].provenance_ref if chunks else None


def _provenance_to_blame_provenance(
    ref: int,
    provenance: ProvenanceRecord,
) -> BlameProvenance:
    return BlameProvenance(
        ref=ref,
        kind=provenance.kind,
        pr_number=provenance.pr_number,
        commit_sha=provenance.commit_sha,
        commit_timestamp=provenance.commit_timestamp,
        changelog_path=provenance.changelog_path,
        title=provenance.title,
        change_id=provenance.change_id,
        intent_problem=provenance.intent_problem,
        intent_goal=provenance.intent_goal,
        file_path=provenance.file,
        span_start=(None if provenance.span is None else provenance.span.start_line),
        span_end=None if provenance.span is None else provenance.span.end_line,
        summary=provenance.summary,
        rationale=provenance.rationale,
        change_index=provenance.change_index,
    )


def _collect_tracked_files(repo_root: Path) -> list[str]:
    output = _git_output(repo_root, "ls-files")
    return [line for line in output.splitlines() if line]


def _resolve_selected_file(
    tracked_files: Sequence[str],
    selected_file: str | None,
) -> str | None:
    if selected_file is not None and selected_file in tracked_files:
        return selected_file

    if "README.md" in tracked_files:
        return "README.md"

    return tracked_files[0] if tracked_files else None


def _tree_node_from_mapping(
    name: str,
    mapping: dict[str, Any],
    prefix: str,
) -> RepoTreeNode:
    path = f"{prefix}/{name}" if prefix else name
    children = mapping["children"]
    if mapping["kind"] == "file":
        return RepoTreeNode(name=name, path=path, kind="file", children=[])

    return RepoTreeNode(
        name=name,
        path=path,
        kind="dir",
        children=[
            _tree_node_from_mapping(child_name, child_mapping, path)
            for child_name, child_mapping in sorted(
                children.items(),
                key=lambda item: item[0],
            )
        ],
    )


def _git_output(repo_root: Path, *args: str) -> str:
    import subprocess

    process = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout


def blame_view_state_to_data(state: BlameViewState) -> dict[str, Any]:
    return {
        "repo_root": str(state.repo_root),
        "branch_name": state.branch_name,
        "parent_branch": state.parent_branch,
        "branch_head_sha": state.branch_head_sha,
        "parent_head_sha": state.parent_head_sha,
        "indexed_at": state.indexed_at,
        "files": state.files,
        "tree": [blame_tree_node_to_data(node) for node in state.tree],
        "selected_file": state.selected_file,
        "file_view": (
            None
            if state.file_view is None
            else blame_file_view_to_data(state.file_view)
        ),
    }


def blame_file_view_to_data(view: BlameFileView) -> dict[str, Any]:
    return {
        "path": view.path,
        "line_count": view.line_count,
        "lines": [
            {
                "line_number": line.line_number,
                "text": line.text,
                "provenance_ref": line.provenance_ref,
            }
            for line in view.lines
        ],
        "chunks": [
            {
                "chunk_index": chunk.chunk_index,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "provenance_ref": chunk.provenance_ref,
                "lines": [
                    {
                        "line_number": line.line_number,
                        "text": line.text,
                        "provenance_ref": line.provenance_ref,
                    }
                    for line in chunk.lines
                ],
            }
            for chunk in view.chunks
        ],
        "provenances": [
            {
                "ref": provenance.ref,
                "kind": provenance.kind,
                "pr_number": provenance.pr_number,
                "commit_sha": provenance.commit_sha,
                "commit_timestamp": provenance.commit_timestamp,
                "changelog_path": provenance.changelog_path,
                "title": provenance.title,
                "change_id": provenance.change_id,
                "intent_problem": provenance.intent_problem,
                "intent_goal": provenance.intent_goal,
                "file_path": provenance.file_path,
                "span_start": provenance.span_start,
                "span_end": provenance.span_end,
                "summary": provenance.summary,
                "rationale": provenance.rationale,
                "change_index": provenance.change_index,
            }
            for provenance in view.provenances
        ],
        "selected_chunk_ref": view.selected_chunk_ref,
    }


def blame_tree_node_to_data(node: RepoTreeNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "path": node.path,
        "kind": node.kind,
        "children": [blame_tree_node_to_data(child) for child in node.children],
    }
