from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path

from powdrr_lift.change_log_parser import ChangeLog, Intent, Span, parse_change_log
from powdrr_lift.change_log_template import _resolve_default_branch, _resolve_repo_root
from powdrr_lift.core.index import (
    ChangelogDocument,
    EntityGraph,
    EntityOccurrence,
    ProvenanceRecord,
    RelationshipOccurrence,
    SourceIndex,
    _apply_file_patch,
    _backfill_line_state_from_blame,
    _build_commit_changes,
    _build_entity_graph,
    _collect_file_patches,
    _CommitRecord,
    _git_output,
    _load_branch_pr_description_document,
    _normalize_entity_id,
    _normalize_line_state,
    _parse_commit_log_output,
)

INDEX_CACHE_VERSION = 6


@dataclass(frozen=True, slots=True)
class BranchState:
    branch_name: str
    parent_branch: str
    branch_head_sha: str
    parent_head_sha: str
    parent_index_version: int
    index_version: int
    indexed_at: int


def code_index_db_path(repo_root: str | Path | None = None) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return repo_root_path / ".powdrr-lift" / "state" / "code_index.db"


def refresh_code_index(
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> SourceIndex:
    repo_root_path = _resolve_repo_root(repo_root)
    store = CodeIndexStore(repo_root_path)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    return store.refresh(resolved_branch, parent_branch)


def lookup_code_provenance(
    path: str,
    line_number: int,
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> ProvenanceRecord | None:
    store = CodeIndexStore(_resolve_repo_root(repo_root))
    resolved_branch = branch_name or _current_branch(store.repo_root)
    store.refresh(resolved_branch, parent_branch)
    return store.lookup(resolved_branch, path, line_number)


def lookup_code_provenance_span(
    path: str,
    start_line: int,
    end_line: int,
    branch_name: str | None = None,
    *,
    parent_branch: str,
    repo_root: str | Path | None = None,
) -> list[ProvenanceRecord]:
    store = CodeIndexStore(_resolve_repo_root(repo_root))
    resolved_branch = branch_name or _current_branch(store.repo_root)
    store.refresh(resolved_branch, parent_branch)
    return store.lookup_span(resolved_branch, path, start_line, end_line)


class CodeIndexStore:
    def __init__(self, repo_root: Path, db_path: Path | None = None) -> None:
        self.repo_root = repo_root
        self.db_path = db_path or code_index_db_path(repo_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def refresh(self, branch_name: str, parent_branch: str) -> SourceIndex:
        branch_head_sha = _git_output(
            self.repo_root,
            "rev-parse",
            branch_name,
        ).strip()
        parent_head_sha = _git_output(
            self.repo_root,
            "rev-parse",
            parent_branch,
        ).strip()
        parent_index = self._ensure_parent_index(parent_branch)
        parent_state = self._read_branch_state(parent_branch)
        parent_index_version = (
            INDEX_CACHE_VERSION if parent_state is None else parent_state.index_version
        )
        current_state = self._read_branch_state(branch_name)
        if (
            current_state is not None
            and parent_state is not None
            and current_state.index_version == INDEX_CACHE_VERSION
            and current_state.parent_index_version == parent_index_version
            and current_state.branch_head_sha == branch_head_sha
            and current_state.parent_branch == parent_branch
            and current_state.parent_head_sha == parent_head_sha
        ):
            return self._load_branch_index(branch_name)

        branch_index = _build_branch_index(
            self.repo_root,
            branch_name=branch_name,
            parent_branch=parent_branch,
            parent_index=parent_index,
        )
        self._write_branch_index(
            branch_name=branch_name,
            parent_branch=parent_branch,
            branch_head_sha=branch_head_sha,
            parent_head_sha=parent_head_sha,
            parent_index_version=parent_index_version,
            index=branch_index,
        )
        return branch_index

    def lookup(
        self,
        branch_name: str,
        path: str,
        line_number: int,
    ) -> ProvenanceRecord | None:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                  pr.id,
                  pr.kind,
                  pr.pr_number,
                  pr.commit_sha,
                  pr.commit_timestamp,
                  pr.changelog_path,
                  pr.title,
                  pr.change_id,
                  pr.intent_problem,
                  pr.intent_goal,
                  pr.file_path,
                  pr.span_start,
                  pr.span_end,
                  pr.summary,
                  pr.rationale,
                  pr.change_index
                FROM file_line fl
                JOIN provenance_record pr ON pr.id = fl.provenance_record_id
                WHERE fl.branch_name = ?
                  AND fl.file_path = ?
                  AND fl.line_number = ?
                """,
                (branch_name, path, line_number),
            ).fetchone()

        if row is None:
            return None

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            affects_by_id = self._load_provenance_affects(
                connection,
                branch_name,
                [row["id"]],
            )

        return _row_to_provenance_record(
            row,
            affects=affects_by_id.get(row["id"], ()),
        )

    def lookup_span(
        self,
        branch_name: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> list[ProvenanceRecord]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT DISTINCT
                  pr.id,
                  pr.kind,
                  pr.pr_number,
                  pr.commit_sha,
                  pr.commit_timestamp,
                  pr.changelog_path,
                  pr.title,
                  pr.change_id,
                  pr.intent_problem,
                  pr.intent_goal,
                  pr.file_path,
                  pr.span_start,
                  pr.span_end,
                  pr.summary,
                  pr.rationale,
                  pr.change_index
                FROM file_line fl
                JOIN provenance_record pr ON pr.id = fl.provenance_record_id
                WHERE fl.branch_name = ?
                  AND fl.file_path = ?
                  AND fl.line_number BETWEEN ? AND ?
                ORDER BY fl.line_number, pr.id
                """,
                (branch_name, path, start_line, end_line),
            ).fetchall()

        if not rows:
            return []

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            affects_by_id = self._load_provenance_affects(
                connection,
                branch_name,
                [row["id"] for row in rows],
            )

        return [
            _row_to_provenance_record(
                row,
                affects=affects_by_id.get(row["id"], ()),
            )
            for row in rows
        ]

    def lookup_lines(
        self,
        branch_name: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> list[tuple[int, ProvenanceRecord | None]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                  fl.line_number,
                  fl.provenance_record_id,
                  pr.id,
                  pr.kind,
                  pr.pr_number,
                  pr.commit_sha,
                  pr.commit_timestamp,
                  pr.changelog_path,
                  pr.title,
                  pr.change_id,
                  pr.intent_problem,
                  pr.intent_goal,
                  pr.file_path,
                  pr.span_start,
                  pr.span_end,
                  pr.summary,
                  pr.rationale,
                  pr.change_index
                FROM file_line fl
                LEFT JOIN provenance_record pr ON pr.id = fl.provenance_record_id
                WHERE fl.branch_name = ?
                  AND fl.file_path = ?
                  AND fl.line_number BETWEEN ? AND ?
                ORDER BY fl.line_number
                """,
                (branch_name, path, start_line, end_line),
            ).fetchall()

            affects_by_id = self._load_provenance_affects(
                connection,
                branch_name,
                [
                    row["provenance_record_id"]
                    for row in rows
                    if row["provenance_record_id"] is not None
                ],
            )

        records_by_line: dict[int, ProvenanceRecord | None] = {
            row["line_number"]: (
                None
                if row["provenance_record_id"] is None
                else _row_to_provenance_record(
                    row,
                    affects=affects_by_id.get(row["provenance_record_id"], ()),
                )
            )
            for row in rows
        }
        return [
            (line_number, records_by_line.get(line_number))
            for line_number in range(start_line, end_line + 1)
        ]

    def _load_provenance_affects(
        self,
        connection: sqlite3.Connection,
        branch_name: str,
        provenance_ids: list[int],
    ) -> dict[int, tuple[str, ...]]:
        if not provenance_ids:
            return {}

        placeholders = ", ".join("?" for _ in provenance_ids)
        rows = connection.execute(
            f"""
            SELECT provenance_record_id, entity_id
            FROM provenance_affect
            WHERE branch_name = ?
              AND provenance_record_id IN ({placeholders})
            ORDER BY provenance_record_id, entity_id
            """,
            [branch_name, *provenance_ids],
        ).fetchall()

        affects_by_id: dict[int, list[str]] = {}
        for row in rows:
            affects_by_id.setdefault(row["provenance_record_id"], []).append(
                row["entity_id"]
            )

        return {
            provenance_id: tuple(entities)
            for provenance_id, entities in affects_by_id.items()
        }

    def lookup_entity_references(
        self,
        branch_name: str,
        entity_name: str,
    ) -> list[ProvenanceRecord]:
        normalized_entity_name = _normalize_entity_id(entity_name)
        if normalized_entity_name is None:
            return []

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                  pr.id,
                  pr.kind,
                  pr.pr_number,
                  pr.commit_sha,
                  pr.commit_timestamp,
                  pr.changelog_path,
                  pr.title,
                  pr.change_id,
                  pr.intent_problem,
                  pr.intent_goal,
                  pr.file_path,
                  pr.span_start,
                  pr.span_end,
                  pr.summary,
                  pr.rationale,
                  pr.change_index
                FROM provenance_record pr
                JOIN provenance_affect pa ON pa.provenance_record_id = pr.id
                WHERE pa.branch_name = ?
                  AND pa.entity_id = ?
                  AND pr.branch_name = ?
                ORDER BY pr.commit_timestamp, pr.id
                """,
                (branch_name, normalized_entity_name, branch_name),
            ).fetchall()

            if not rows:
                return []

            affects_by_provenance_id = self._load_provenance_affects(
                connection,
                branch_name,
                [row["id"] for row in rows],
            )

        return [
            _row_to_provenance_record(
                row,
                affects=affects_by_provenance_id.get(row["id"], ()),
            )
            for row in rows
        ]

    def lookup_entity_relationships(
        self,
        branch_name: str,
        entity_name: str,
    ) -> tuple[list[EntityOccurrence], list[RelationshipOccurrence]]:
        normalized_entity_name = _normalize_entity_id(entity_name)
        if normalized_entity_name is None:
            return [], []

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            entity_rows = connection.execute(
                """
                SELECT entity_id, entity_type, action, pr_number, commit_sha,
                       commit_timestamp, changelog_path
                FROM entity_occurrence
                WHERE branch_name = ?
                  AND entity_id = ?
                ORDER BY id
                """,
                (branch_name, normalized_entity_name),
            ).fetchall()
            relationship_rows = connection.execute(
                """
                SELECT source_entity_id, target_entity_id, relationship, action,
                       rationale, pr_number, commit_sha, commit_timestamp,
                       changelog_path
                FROM relationship_record
                WHERE branch_name = ?
                  AND (source_entity_id = ? OR target_entity_id = ?)
                ORDER BY id
                """,
                (branch_name, normalized_entity_name, normalized_entity_name),
            ).fetchall()

        entity_occurrences = [
            EntityOccurrence(
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                action=row["action"],
                pr_number=row["pr_number"],
                commit_sha=row["commit_sha"],
                commit_timestamp=row["commit_timestamp"],
                changelog_path=row["changelog_path"],
            )
            for row in entity_rows
        ]
        relationship_occurrences = [
            RelationshipOccurrence(
                source=row["source_entity_id"],
                target=row["target_entity_id"],
                relationship=row["relationship"],
                action=row["action"],
                rationale=row["rationale"],
                pr_number=row["pr_number"],
                commit_sha=row["commit_sha"],
                commit_timestamp=row["commit_timestamp"],
                changelog_path=row["changelog_path"],
            )
            for row in relationship_rows
        ]
        return entity_occurrences, relationship_occurrences

    def branch_state_for(self, branch_name: str) -> BranchState | None:
        return self._read_branch_state(branch_name)

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS branch_state (
                  branch_name TEXT PRIMARY KEY,
                  parent_branch TEXT NOT NULL,
                  branch_head_sha TEXT NOT NULL,
                  parent_head_sha TEXT NOT NULL,
                  parent_index_version INTEGER NOT NULL DEFAULT 0,
                  index_version INTEGER NOT NULL DEFAULT 0,
                  indexed_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS branch_document (
                  branch_name TEXT NOT NULL,
                  pr_number INTEGER NOT NULL,
                  changelog_path TEXT NOT NULL,
                  commit_sha TEXT,
                  commit_timestamp INTEGER,
                  commit_subject TEXT,
                  title TEXT,
                  change_id TEXT,
                  intent_problem TEXT,
                  intent_goal TEXT,
                  PRIMARY KEY (branch_name, pr_number)
                );

                CREATE TABLE IF NOT EXISTS provenance_record (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  branch_name TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  pr_number INTEGER,
                  commit_sha TEXT,
                  commit_timestamp INTEGER,
                  changelog_path TEXT,
                  title TEXT,
                  change_id TEXT,
                  intent_problem TEXT,
                  intent_goal TEXT,
                  file_path TEXT,
                  span_start INTEGER,
                  span_end INTEGER,
                  summary TEXT,
                  rationale TEXT,
                  change_index INTEGER
                );

                CREATE TABLE IF NOT EXISTS provenance_affect (
                  branch_name TEXT NOT NULL,
                  provenance_record_id INTEGER NOT NULL,
                  entity_id TEXT NOT NULL,
                  PRIMARY KEY (branch_name, provenance_record_id, entity_id),
                  FOREIGN KEY (provenance_record_id) REFERENCES provenance_record(id)
                    ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_provenance_affect_lookup
                  ON provenance_affect(branch_name, entity_id, provenance_record_id);

                CREATE TABLE IF NOT EXISTS file_line (
                  branch_name TEXT NOT NULL,
                  file_path TEXT NOT NULL,
                  line_number INTEGER NOT NULL,
                  provenance_record_id INTEGER,
                  PRIMARY KEY (branch_name, file_path, line_number),
                  FOREIGN KEY (provenance_record_id) REFERENCES provenance_record(id)
                    ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_file_line_lookup
                  ON file_line(branch_name, file_path, line_number);

                CREATE INDEX IF NOT EXISTS idx_provenance_lookup
                  ON provenance_record(branch_name, file_path, span_start, span_end);

                CREATE TABLE IF NOT EXISTS entity_occurrence (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  branch_name TEXT NOT NULL,
                  entity_id TEXT NOT NULL,
                  entity_type TEXT,
                  action TEXT,
                  pr_number INTEGER,
                  commit_sha TEXT,
                  commit_timestamp INTEGER,
                  changelog_path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relationship_record (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  branch_name TEXT NOT NULL,
                  source_entity_id TEXT NOT NULL,
                  target_entity_id TEXT NOT NULL,
                  relationship TEXT,
                  action TEXT,
                  rationale TEXT,
                  pr_number INTEGER,
                  commit_sha TEXT,
                  commit_timestamp INTEGER,
                  changelog_path TEXT NOT NULL
                );
                """
            )
            branch_state_info = connection.execute(
                "PRAGMA table_info(branch_state)"
            ).fetchall()
            branch_state_columns = {row[1] for row in branch_state_info}
            if "index_version" not in branch_state_columns:
                connection.execute(
                    """
                    ALTER TABLE branch_state
                    ADD COLUMN index_version INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "parent_index_version" not in branch_state_columns:
                connection.execute(
                    """
                    ALTER TABLE branch_state
                    ADD COLUMN parent_index_version INTEGER NOT NULL DEFAULT 0
                    """
                )

    def _ensure_parent_index(self, parent_branch: str) -> SourceIndex:
        current_state = self._read_branch_state(parent_branch)
        parent_head_sha = _git_output(
            self.repo_root,
            "rev-parse",
            parent_branch,
        ).strip()
        if (
            current_state is not None
            and current_state.branch_head_sha == parent_head_sha
            and current_state.index_version == INDEX_CACHE_VERSION
        ):
            return self._load_branch_index(parent_branch)

        if parent_branch == _resolve_default_branch(self.repo_root):
            index = _build_mainline_index_at_ref(self.repo_root, parent_branch)
            parent_index_version = INDEX_CACHE_VERSION
            self._write_branch_index(
                branch_name=parent_branch,
                parent_branch=parent_branch,
                branch_head_sha=parent_head_sha,
                parent_head_sha=parent_head_sha,
                parent_index_version=parent_index_version,
                index=index,
            )
            return index

        # Fall back to refreshing the parent branch against the default branch.
        resolved_parent = _resolve_default_branch(self.repo_root)
        return self.refresh(parent_branch, resolved_parent)

    def _read_branch_state(self, branch_name: str) -> BranchState | None:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT branch_name, parent_branch, branch_head_sha,
                       parent_head_sha, parent_index_version, index_version,
                       indexed_at
                FROM branch_state
                WHERE branch_name = ?
                """,
                (branch_name,),
            ).fetchone()

        if row is None:
            return None

        return BranchState(
            branch_name=row["branch_name"],
            parent_branch=row["parent_branch"],
            branch_head_sha=row["branch_head_sha"],
            parent_head_sha=row["parent_head_sha"],
            parent_index_version=row["parent_index_version"],
            index_version=row["index_version"],
            indexed_at=row["indexed_at"],
        )

    def _load_branch_index(self, branch_name: str) -> SourceIndex:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            branch_row = connection.execute(
                """
                SELECT branch_name, parent_branch, branch_head_sha, parent_head_sha,
                       parent_index_version, index_version
                FROM branch_state
                WHERE branch_name = ?
                """,
                (branch_name,),
            ).fetchone()
            if branch_row is None:
                raise RuntimeError(f"Missing branch state for {branch_name!r}.")

            document_rows = connection.execute(
                """
                SELECT pr_number, changelog_path, commit_sha, commit_timestamp,
                       commit_subject, title, change_id, intent_problem, intent_goal
                FROM branch_document
                WHERE branch_name = ?
                ORDER BY pr_number
                """,
                (branch_name,),
            ).fetchall()
            entity_rows = connection.execute(
                """
                SELECT entity_id, entity_type, action, pr_number, commit_sha,
                       commit_timestamp, changelog_path
                FROM entity_occurrence
                WHERE branch_name = ?
                ORDER BY entity_id, id
                """,
                (branch_name,),
            ).fetchall()
            relationship_rows = connection.execute(
                """
                SELECT source_entity_id, target_entity_id, relationship, action,
                       rationale, pr_number, commit_sha, commit_timestamp,
                       changelog_path
                FROM relationship_record
                WHERE branch_name = ?
                ORDER BY id
                """,
                (branch_name,),
            ).fetchall()
            provenance_rows = connection.execute(
                """
                SELECT id, kind, pr_number, commit_sha, commit_timestamp,
                       changelog_path, title, change_id, intent_problem,
                       intent_goal, file_path, span_start, span_end, summary,
                       rationale, change_index
                FROM provenance_record
                WHERE branch_name = ?
                ORDER BY id
                """,
                (branch_name,),
            ).fetchall()
            affects_by_provenance_id = self._load_provenance_affects(
                connection,
                branch_name,
                [row["id"] for row in provenance_rows],
            )
            line_rows = connection.execute(
                """
                SELECT line_number, file_path, provenance_record_id
                FROM file_line
                WHERE branch_name = ?
                ORDER BY file_path, line_number
                """,
                (branch_name,),
            ).fetchall()

        entities_by_id: dict[str, list[EntityOccurrence]] = {}
        for row in entity_rows:
            entities_by_id.setdefault(row["entity_id"], []).append(
                EntityOccurrence(
                    entity_id=row["entity_id"],
                    entity_type=row["entity_type"],
                    action=row["action"],
                    pr_number=row["pr_number"],
                    commit_sha=row["commit_sha"],
                    commit_timestamp=row["commit_timestamp"],
                    changelog_path=row["changelog_path"],
                )
            )

        relationship_records = [
            RelationshipOccurrence(
                source=row["source_entity_id"],
                target=row["target_entity_id"],
                relationship=row["relationship"],
                action=row["action"],
                rationale=row["rationale"],
                pr_number=row["pr_number"],
                commit_sha=row["commit_sha"],
                commit_timestamp=row["commit_timestamp"],
                changelog_path=row["changelog_path"],
            )
            for row in relationship_rows
        ]

        provenance_by_id = {
            row["id"]: _row_to_provenance_record(
                row,
                affects=affects_by_provenance_id.get(row["id"], ()),
            )
            for row in provenance_rows
        }
        file_lines: dict[str, list[ProvenanceRecord | None]] = {}
        for row in line_rows:
            file_lines.setdefault(row["file_path"], [])
            lines = file_lines[row["file_path"]]
            while len(lines) < row["line_number"]:
                lines.append(None)

            provenance_record: ProvenanceRecord | None = (
                provenance_by_id.get(row["provenance_record_id"])
                if row["provenance_record_id"] is not None
                else None
            )
            lines[row["line_number"] - 1] = provenance_record

        documents = [
            ChangelogDocument(
                pr_number=row["pr_number"],
                changelog_path=Path(row["changelog_path"]),
                changelog=ChangeLog(
                    version=1,
                    change_id=row["change_id"],
                    title=row["title"],
                    intent=Intent(
                        problem=row["intent_problem"],
                        goal=row["intent_goal"],
                    ),
                ),
                commit_sha=row["commit_sha"],
                commit_timestamp=row["commit_timestamp"],
                commit_subject=row["commit_subject"],
            )
            for row in document_rows
        ]
        changes = [provenance_by_id[row["id"]] for row in provenance_rows]
        return SourceIndex(
            repo_root=self.repo_root,
            default_branch_name=branch_row["parent_branch"],
            documents=documents,
            entity_graph=EntityGraph(
                entities={
                    entity_id: tuple(occurrences)
                    for entity_id, occurrences in entities_by_id.items()
                },
                relationships=tuple(relationship_records),
            ),
            changes=changes,
            file_lines={path: tuple(lines) for path, lines in file_lines.items()},
        )

    def _write_branch_index(
        self,
        branch_name: str,
        parent_branch: str,
        branch_head_sha: str,
        parent_head_sha: str,
        parent_index_version: int,
        index: SourceIndex,
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "DELETE FROM branch_state WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM branch_document WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM provenance_record WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM file_line WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM entity_occurrence WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM relationship_record WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                "DELETE FROM provenance_affect WHERE branch_name = ?",
                (branch_name,),
            )
            connection.execute(
                """
                INSERT INTO branch_state (
                  branch_name, parent_branch, branch_head_sha, parent_head_sha,
                  parent_index_version, index_version, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    branch_name,
                    parent_branch,
                    branch_head_sha,
                    parent_head_sha,
                    parent_index_version,
                    INDEX_CACHE_VERSION,
                    int(time.time()),
                ),
            )

            for document in index.documents:
                connection.execute(
                    """
                    INSERT INTO branch_document (
                      branch_name, pr_number, changelog_path, commit_sha,
                      commit_timestamp, commit_subject, title, change_id,
                      intent_problem, intent_goal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        branch_name,
                        document.pr_number,
                        str(document.changelog_path),
                        document.commit_sha,
                        document.commit_timestamp,
                        document.commit_subject,
                        document.changelog.title,
                        document.changelog.change_id,
                        document.changelog.intent.problem,
                        document.changelog.intent.goal,
                    ),
                )

            for entity_id, occurrences in index.entity_graph.entities.items():
                for occurrence in occurrences:
                    connection.execute(
                        """
                        INSERT INTO entity_occurrence (
                          branch_name, entity_id, entity_type, action, pr_number,
                          commit_sha, commit_timestamp, changelog_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            branch_name,
                            entity_id,
                            occurrence.entity_type,
                            occurrence.action,
                            occurrence.pr_number,
                            occurrence.commit_sha,
                            occurrence.commit_timestamp,
                            occurrence.changelog_path,
                        ),
                    )

            for relationship in index.entity_graph.relationships:
                connection.execute(
                    """
                    INSERT INTO relationship_record (
                      branch_name, source_entity_id, target_entity_id, relationship,
                      action, rationale, pr_number, commit_sha, commit_timestamp,
                      changelog_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        branch_name,
                        relationship.source,
                        relationship.target,
                        relationship.relationship,
                        relationship.action,
                        relationship.rationale,
                        relationship.pr_number,
                        relationship.commit_sha,
                        relationship.commit_timestamp,
                        relationship.changelog_path,
                    ),
                )

            provenance_id_by_record: dict[int, int] = {}
            for record in index.changes:
                cursor = connection.execute(
                    """
                    INSERT INTO provenance_record (
                      branch_name, kind, pr_number, commit_sha, commit_timestamp,
                      changelog_path, title, change_id, intent_problem, intent_goal,
                      file_path, span_start, span_end, summary, rationale, change_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        branch_name,
                        record.kind,
                        record.pr_number,
                        record.commit_sha,
                        record.commit_timestamp,
                        record.changelog_path,
                        record.title,
                        record.change_id,
                        record.intent_problem,
                        record.intent_goal,
                        record.file,
                        None if record.span is None else record.span.start_line,
                        None if record.span is None else record.span.end_line,
                        record.summary,
                        record.rationale,
                        record.change_index,
                    ),
                )
                provenance_id = cursor.lastrowid
                if provenance_id is None:
                    raise RuntimeError("Missing provenance row id after insert.")

                provenance_id_by_record[id(record)] = provenance_id
                for entity_id in record.affects:
                    connection.execute(
                        """
                        INSERT INTO provenance_affect (
                          branch_name, provenance_record_id, entity_id
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            branch_name,
                            provenance_id,
                            entity_id,
                        ),
                    )

            for file_path, lines in index.file_lines.items():
                for line_number, line_record_ref in enumerate(lines, start=1):
                    line_record: ProvenanceRecord | None = line_record_ref
                    line_provenance_id: int | None = None
                    if line_record is not None:
                        line_provenance_id = provenance_id_by_record.get(
                            id(line_record)
                        )
                    connection.execute(
                        """
                        INSERT INTO file_line (
                          branch_name, file_path, line_number, provenance_record_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            branch_name,
                            file_path,
                            line_number,
                            line_provenance_id,
                        ),
                    )

            connection.commit()


def _build_branch_index(
    repo_root: Path,
    branch_name: str,
    parent_branch: str,
    parent_index: SourceIndex,
) -> SourceIndex:
    branch_document = _resolve_branch_document(repo_root, parent_branch, branch_name)
    if branch_document is None:
        branch_document = _load_branch_pr_description_document(
            repo_root,
            branch_name,
            parent_branch,
        )

    line_state: dict[str, list[ProvenanceRecord | None]] = {
        path: list(lines) for path, lines in parent_index.file_lines.items()
    }
    documents = list(parent_index.documents)
    if branch_document is not None and all(
        document.pr_number != branch_document.pr_number for document in documents
    ):
        documents.append(branch_document)
    changes = list(parent_index.changes)
    changes_by_commit_and_file: dict[tuple[str, str], list[ProvenanceRecord]] = {}

    commits = _collect_branch_commits(repo_root, parent_branch, branch_name)
    for commit in commits:
        if branch_document is not None:
            commit = replace(
                commit,
                pr_number=branch_document.pr_number,
                changelog_document=branch_document,
            )
        file_patches = _collect_file_patches(repo_root, commit.sha, commit.parent_sha)
        commit_changes = _build_commit_changes(
            repo_root,
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

    _normalize_line_state(repo_root, line_state)
    _backfill_line_state_from_blame(
        repo_root,
        line_state,
        changes_by_commit_and_file,
    )
    return SourceIndex(
        repo_root=repo_root,
        default_branch_name=parent_branch,
        documents=documents,
        entity_graph=_build_entity_graph(documents),
        changes=changes,
        file_lines={path: tuple(lines) for path, lines in sorted(line_state.items())},
    )


def _build_mainline_index_at_ref(repo_root: Path, ref: str) -> SourceIndex:
    documents = _load_changelog_documents_at_ref(repo_root, ref)
    document_by_pr = {document.pr_number: document for document in documents}
    commits = _load_mainline_commits_at_ref(repo_root, ref, document_by_pr)

    changes: list[ProvenanceRecord] = []
    line_state: dict[str, list[ProvenanceRecord | None]] = {}
    changes_by_commit_and_file: dict[tuple[str, str], list[ProvenanceRecord]] = {}

    for commit in commits:
        file_patches = _collect_file_patches(
            repo_root,
            commit.sha,
            commit.parent_sha,
        )
        commit_changes = _build_commit_changes(
            repo_root,
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

    _normalize_line_state(repo_root, line_state)
    _backfill_line_state_from_blame(
        repo_root,
        line_state,
        changes_by_commit_and_file,
    )
    documents = list(document_by_pr.values())
    return SourceIndex(
        repo_root=repo_root,
        default_branch_name=ref,
        documents=documents,
        entity_graph=_build_entity_graph(documents),
        changes=changes,
        file_lines={path: tuple(lines) for path, lines in sorted(line_state.items())},
    )


def _collect_branch_commits(
    repo_root: Path,
    parent_branch: str,
    branch_name: str,
) -> list[_CommitRecord]:
    output = _git_output(
        repo_root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x1f%ct%x1f%s%x1f%b%x1f%P%x1e",
        f"{parent_branch}..{branch_name}",
    )
    commits: list[_CommitRecord] = []
    for sha, timestamp_text, subject, commit_body, parents in _parse_commit_log_output(
        output
    ):
        parent_sha = parents.split(" ", maxsplit=1)[0] if parents else None
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

    return commits


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
        pr_number = None
        match = _extract_pr_number_from_subject(subject)
        if match is not None:
            pr_number = match
        changelog_document = (
            document_by_pr.get(pr_number) if pr_number is not None else None
        )
        if changelog_document is not None and pr_number is not None:
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


def _resolve_branch_document(
    repo_root: Path,
    parent_branch: str,
    branch_name: str,
) -> ChangelogDocument | None:
    output = _git_output(
        repo_root,
        "diff",
        "--name-only",
        f"{parent_branch}...{branch_name}",
    )
    for path_text in output.splitlines():
        if not path_text.startswith("docs/changelogs/PR-") or not path_text.endswith(
            "-changelog.yaml"
        ):
            continue

        changelog_path = repo_root / path_text
        if not changelog_path.exists():
            continue

        match = path_text.rsplit("/", maxsplit=1)[-1]
        pr_number_text = match.removeprefix("PR-").removesuffix("-changelog.yaml")
        if not pr_number_text.isdigit():
            continue

        pr_number = int(pr_number_text)
        content = changelog_path.read_text(encoding="utf-8")
        changelog = parse_change_log(content)
        return ChangelogDocument(
            pr_number=pr_number,
            changelog_path=changelog_path,
            changelog=changelog,
            commit_sha=None,
            commit_timestamp=None,
            commit_subject=None,
        )

    return None


def _row_to_provenance_record(
    row: sqlite3.Row,
    *,
    affects: tuple[str, ...] = (),
) -> ProvenanceRecord:
    return ProvenanceRecord(
        kind=row["kind"],
        pr_number=row["pr_number"],
        commit_sha=row["commit_sha"],
        commit_timestamp=row["commit_timestamp"],
        changelog_path=row["changelog_path"],
        title=row["title"],
        change_id=row["change_id"],
        intent_problem=row["intent_problem"],
        intent_goal=row["intent_goal"],
        file=row["file_path"],
        span=(
            None
            if row["span_start"] is None and row["span_end"] is None
            else _span_from_row(row)
        ),
        summary=row["summary"],
        rationale=row["rationale"],
        affects=affects,
        change_index=row["change_index"],
    )


def _span_from_row(row: sqlite3.Row) -> Span:
    return Span(start_line=row["span_start"], end_line=row["span_end"])


def _current_branch(repo_root: Path) -> str:
    return _git_output(repo_root, "branch", "--show-current").strip()


def _extract_pr_number_from_subject(subject: str) -> int | None:
    marker = "(#"
    if marker not in subject:
        return None

    start = subject.rfind(marker)
    end = subject.find(")", start)
    if start == -1 or end == -1:
        return None

    pr_text = subject[start + len(marker) : end]
    if not pr_text.isdigit():
        return None

    return int(pr_text)
