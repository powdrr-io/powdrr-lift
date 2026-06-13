from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.code_index import CodeIndexStore, _current_branch
from powdrr_lift.core.index import ProvenanceRecord


@dataclass(frozen=True, slots=True)
class EditContextLine:
    line_number: int
    provenance_ref: int | None


@dataclass(frozen=True, slots=True)
class EditContextRange:
    start_line: int
    end_line: int
    lines: list[EditContextLine] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EditContextReport:
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    indexed_at: int
    file_path: str
    matching_changes: list[ProvenanceRecord] = field(default_factory=list)
    requested_ranges: list[EditContextRange] = field(default_factory=list)


def parse_line_range(range_text: str) -> tuple[int, int]:
    if ":" not in range_text:
        raise ValueError(f"Line range {range_text!r} must use start:end format.")

    start_text, end_text = range_text.split(":", maxsplit=1)
    start_line = int(start_text)
    end_line = int(end_text)
    if start_line < 1:
        raise ValueError("Line ranges must start at line 1 or later.")
    if end_line < start_line:
        raise ValueError("Line range end must be greater than or equal to start.")

    return start_line, end_line


def parse_line_ranges(range_texts: Sequence[str]) -> list[tuple[int, int]]:
    return [parse_line_range(range_text) for range_text in range_texts]


def lookup_edit_context(
    file_path: str,
    line_ranges: Sequence[tuple[int, int]],
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> EditContextReport:
    repo_root_path = _resolve_repo_root(repo_root)
    store = CodeIndexStore(repo_root_path)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    store.refresh(resolved_branch, parent_branch)
    branch_state = store.branch_state_for(resolved_branch)
    if branch_state is None:
        raise RuntimeError(f"Missing branch state for {resolved_branch!r}.")

    matching_changes: list[ProvenanceRecord] = []
    change_ref_by_record: dict[ProvenanceRecord, int] = {}
    requested_ranges: list[EditContextRange] = []

    for start_line, end_line in line_ranges:
        if start_line < 1:
            raise ValueError("Line ranges must start at line 1 or later.")
        if end_line < start_line:
            raise ValueError("Line range end must be greater than or equal to start.")

        lines: list[EditContextLine] = []
        for line_number, provenance in store.lookup_lines(
            resolved_branch,
            file_path,
            start_line,
            end_line,
        ):
            provenance_ref = None
            if provenance is not None:
                provenance_ref = change_ref_by_record.get(provenance)
                if provenance_ref is None:
                    matching_changes.append(provenance)
                    provenance_ref = len(matching_changes)
                    change_ref_by_record[provenance] = provenance_ref

            lines.append(
                EditContextLine(
                    line_number=line_number,
                    provenance_ref=provenance_ref,
                )
            )

        requested_ranges.append(
            EditContextRange(
                start_line=start_line,
                end_line=end_line,
                lines=lines,
            )
        )

    return EditContextReport(
        branch_name=resolved_branch,
        parent_branch=parent_branch,
        branch_head_sha=branch_state.branch_head_sha,
        parent_head_sha=branch_state.parent_head_sha,
        indexed_at=branch_state.indexed_at,
        file_path=file_path,
        matching_changes=matching_changes,
        requested_ranges=requested_ranges,
    )


def render_edit_context_report(report: EditContextReport) -> str:
    return yaml.safe_dump(_edit_context_report_to_data(report), sort_keys=False)


def _edit_context_report_to_data(report: EditContextReport) -> dict[str, Any]:
    return {
        "branch_name": report.branch_name,
        "parent_branch": report.parent_branch,
        "branch_head_sha": report.branch_head_sha,
        "parent_head_sha": report.parent_head_sha,
        "indexed_at": report.indexed_at,
        "file_path": report.file_path,
        "matching_changes": [
            {
                "ref": change_ref,
                "kind": change.kind,
                "pr_number": change.pr_number,
                "commit_sha": change.commit_sha,
                "commit_timestamp": change.commit_timestamp,
                "changelog_path": change.changelog_path,
                "title": change.title,
                "change_id": change.change_id,
                "intent_problem": change.intent_problem,
                "intent_goal": change.intent_goal,
                "file_path": change.file,
                "span": (
                    None
                    if change.span is None
                    else {
                        "start_line": change.span.start_line,
                        "end_line": change.span.end_line,
                    }
                ),
                "summary": change.summary,
                "rationale": change.rationale,
                "change_index": change.change_index,
            }
            for change_ref, change in enumerate(report.matching_changes, start=1)
        ],
        "requested_ranges": [
            {
                "start_line": requested_range.start_line,
                "end_line": requested_range.end_line,
                "lines": [
                    {
                        "line_number": line.line_number,
                        "provenance_ref": line.provenance_ref,
                    }
                    for line in requested_range.lines
                ],
            }
            for requested_range in report.requested_ranges
        ],
    }
