from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.coverage import (
    build_requirement_coverage_request as build_service_coverage_request,
)
from powdrr_service.planning.questions import (
    MAX_QUESTION_INPUT_CHARS,
    build_question_messages,
)
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
)

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.planning import evidence as evidence_module
from powdrr_lift.workrr.planning.evidence import (
    Evidence,
    EvidenceError,
    bind_coverage_answer,
    build_requirement_coverage_request,
    create_requirement_coverage,
    lookup_symbols_in_pinned_file,
    read_pinned_code_file,
    record_file_evidence,
    search_dependency_reference_paths,
    search_pinned_workspace_symbols,
    search_requirement_evidence,
    search_requirements_evidence,
    search_structural_document_analysis,
    search_structural_document_symbols,
    search_workspace_symbols_from_worktree,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "evidence@example.test")
    _git(repository, "config", "user.name", "Evidence Test")
    (repository / "src/schema.py").write_text(
        "def parse_payload(raw: str) -> dict[str, str]:\n"
        '    return {"value": raw.strip()}\n\n'
        "def reject_oversized(raw: str, limit: int) -> bool:\n"
        "    return len(raw) > limit\n",
        encoding="utf-8",
    )
    (repository / "tests/test_schema.py").write_text(
        "from src.schema import parse_payload\n\n"
        "def test_parse_payload():\n"
        '    assert parse_payload(" a ") == {"value": "a"}\n',
        encoding="utf-8",
    )
    (repository / "pyproject.toml").write_text("[tool.example]\n", encoding="utf-8")
    _git(repository, "add", "src/schema.py", "tests/test_schema.py", "pyproject.toml")
    _git(repository, "commit", "-qm", "baseline")
    return repository, _git(repository, "rev-parse", "HEAD")


def _requirement(commit: str = "a" * 40) -> tuple[Requirement, Any]:
    text = "# Parsing\n\nThe parser trims surrounding whitespace.\n"
    document = PinnedSourceDocument(
        "repo",
        commit,
        "plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )
    segmented = segment_markdown_source(document)
    block = next(item for item in segmented.blocks if item.kind == "paragraph")
    requirement = Requirement(
        "req-0001",
        "Trim surrounding whitespace when parsing.",
        "behavior",
        (block.anchor_ids[0],),
        ("group-conditional-1",),
        (),
    )
    return requirement, segmented


def _symbol(name: str, path: str, start: int, end: int) -> dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "range": {"start_line": start, "end_line": end},
    }


def test_dependency_reference_evidence_maps_planned_paths_to_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit = _repository(tmp_path)
    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.find_workspace_reference_paths",
        lambda paths, *, worktree_root: {
            "src/schema.py": ("tests/test_schema.py",),
        },
    )

    result = search_dependency_reference_paths(
        repository,
        base_commit,
        {
            "unit-implementation": ("src/schema.py",),
            "unit-test": ("tests/test_schema.py",),
        },
    )

    assert result == {
        ("unit-implementation", "unit-test"): (
            "src/schema.py -> tests/test_schema.py",
        ),
    }


def test_pinned_file_evidence_reads_exact_commit_and_rejects_invented_paths(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)

    path, content = read_pinned_code_file(repository, commit, "src/schema.py")
    evidence = record_file_evidence(
        repository, commit, "src/schema.py", kind="implementation", ordinal=1
    )

    assert path == "src/schema.py"
    assert "parse_payload" in content
    assert evidence.base_commit == commit
    assert evidence.content_sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert Evidence.from_mapping(evidence.to_data()) == evidence
    with pytest.raises(EvidenceError, match="normalized repository path"):
        Evidence.from_mapping({**evidence.to_data(), "path": "../schema.py"})
    with pytest.raises(EvidenceError, match="tracked file"):
        read_pinned_code_file(repository, commit, "src/not_real.py")
    with pytest.raises(EvidenceError, match="regular tracked blob"):
        (repository / "src/link.py").symlink_to(repository / "src/schema.py")
        _git(repository, "add", "src/link.py")
        _git(repository, "commit", "-qm", "symlink")
        read_pinned_code_file(
            repository, _git(repository, "rev-parse", "HEAD"), "src/link.py"
        )


def test_pinned_symbol_queries_reuse_revision_artifact_and_keep_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit = _repository(tmp_path)
    provider_calls: list[tuple[str, ...]] = []

    def document_symbols(
        paths: list[str], *, worktree_root: Path
    ) -> dict[str, list[dict[str, Any]]]:
        assert worktree_root.name == "repository"
        provider_calls.append(tuple(paths))
        return {
            path: [
                {
                    "name": "parse_payload",
                    "kind": "function",
                    "range": {
                        "start_line": 1,
                        "start_character": 0,
                        "end_line": 2,
                        "end_character": 0,
                    },
                    "children": [],
                }
            ]
            for path in paths
            if path == "src/schema.py"
        }

    monkeypatch.setattr(
        evidence_module, "find_workspace_document_symbols", document_symbols
    )

    first = search_pinned_workspace_symbols(
        repository, base_commit, ["parse_payload"], limit=20
    )
    second = search_pinned_workspace_symbols(
        repository, base_commit, ["parse_payload"], limit=20
    )
    structural = search_structural_document_symbols(
        repository, base_commit, ["src/schema.py"]
    )

    assert len(provider_calls) == 1
    assert first == second
    symbol = first["parse_payload"][0]
    assert symbol["path"] == "src/schema.py"
    assert symbol["range"]["start_line"] == 1
    assert symbol["artifact_id"].startswith("structrr-code-symbols:sha256:")
    assert symbol["artifact_digest"]
    assert structural["src/schema.py"][0]["name"] == "parse_payload"
    checked_in_artifacts = list(
        (repository / "artifacts" / "structrr" / "code-symbols").glob("sha256-*.yaml")
    )
    assert len(checked_in_artifacts) == 1
    assert (repository / ".powdrr" / "cache" / "structrr-code-symbols").is_dir()

    _git(repository, "add", "artifacts/structrr/code-symbols")
    _git(repository, "commit", "-qm", "check in symbol artifact")
    artifact_commit = _git(repository, "rev-parse", "HEAD")
    after_artifact_commit = search_pinned_workspace_symbols(
        repository, artifact_commit, ["parse_payload"], limit=20
    )
    assert len(provider_calls) == 1
    assert (
        after_artifact_commit["parse_payload"][0]["artifact_id"]
        == symbol["artifact_id"]
    )
    checked_in_reference, _ = evidence_module.pinned_code_symbol_artifact_reference(
        repository, artifact_commit
    )
    assert checked_in_reference["source_revision"] == base_commit


def test_worktree_symbol_artifact_invalidates_when_dirty_python_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _repository(tmp_path)
    provider_calls = 0

    def document_symbols(
        paths: list[str], *, worktree_root: Path
    ) -> dict[str, list[dict[str, Any]]]:
        nonlocal provider_calls
        provider_calls += 1
        return {
            "src/schema.py": [
                {
                    "name": "parse_payload",
                    "kind": "function",
                    "range": {
                        "start_line": 1,
                        "start_character": 0,
                        "end_line": 2,
                        "end_character": 0,
                    },
                }
            ]
        }

    monkeypatch.setattr(
        evidence_module, "find_workspace_document_symbols", document_symbols
    )
    first = search_workspace_symbols_from_worktree(
        repository, ["parse_payload"], limit=10
    )
    (repository / "src/schema.py").write_text(
        "def parse_payload(raw: str) -> dict[str, str]:\n"
        '    return {"value": raw.strip()}  # revised\n',
        encoding="utf-8",
    )
    second = search_workspace_symbols_from_worktree(
        repository, ["parse_payload"], limit=10
    )

    assert provider_calls == 2
    assert (
        first["parse_payload"][0]["artifact_id"]
        != second["parse_payload"][0]["artifact_id"]
    )
    assert (
        first["parse_payload"][0]["artifact_digest"]
        != second["parse_payload"][0]["artifact_digest"]
    )


def test_unavailable_reference_lookup_keeps_indexed_symbol_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit = _repository(tmp_path)
    monkeypatch.setattr(
        evidence_module,
        "find_workspace_document_symbols",
        lambda paths, **_kwargs: {
            "src/schema.py": [
                {
                    "name": "parse_payload",
                    "kind": "function",
                    "range": {
                        "start_line": 1,
                        "start_character": 0,
                        "end_line": 2,
                        "end_character": 0,
                    },
                }
            ]
        },
    )

    def unavailable_references(*_args: Any, **_kwargs: Any) -> Any:
        raise PowdrrExecutionError("reference request cap reached")

    monkeypatch.setattr(
        evidence_module,
        "find_workspace_document_analysis",
        unavailable_references,
    )

    symbols, references, status = search_structural_document_analysis(
        repository, base_commit, ["src/schema.py"]
    )

    assert status == "unavailable"
    assert references == {}
    assert symbols["src/schema.py"][0]["name"] == "parse_payload"
    assert symbols["src/schema.py"][0]["artifact_id"].startswith(
        "structrr-code-symbols:sha256:"
    )


def test_pinned_code_file_reads_each_repository_blob_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, commit = _repository(tmp_path)
    original = evidence_module.read_pinned_file
    reads = 0

    def count_reads(root: str | Path, sha: str, path: str) -> Any:
        nonlocal reads
        reads += 1
        return original(root, sha, path)

    monkeypatch.setattr(evidence_module, "read_pinned_file", count_reads)

    first = read_pinned_code_file(repository, commit, "src/schema.py")
    second = read_pinned_code_file(str(repository), commit, "src/schema.py")

    assert first == second
    assert reads == 1


def test_requirement_search_supplies_pinned_implementation_and_test_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _repository(tmp_path)
    requirement, source = _requirement(commit)
    requirement = Requirement(
        requirement.id,
        "Parse payload payload values with the existing parser.",
        requirement.kind,
        requirement.source_refs,
        requirement.source_group_ids,
        requirement.related_requirement_ids,
    )

    def find_document_symbols(
        paths: list[str], *, worktree_root: Path
    ) -> dict[str, list[dict[str, Any]]]:
        assert worktree_root != repository.resolve()
        assert worktree_root == worktree_root.resolve()
        assert (worktree_root / "src/schema.py").is_file()
        return {
            path: [
                {
                    "name": symbol,
                    "kind": "function",
                    "range": {
                        "start_line": start,
                        "start_character": 0,
                        "end_line": end,
                        "end_character": 0,
                    },
                    "children": [],
                }
            ]
            for path, symbol, start, end in (
                ("src/schema.py", "parse_payload", 1, 2),
                ("tests/test_schema.py", "test_parse_payload", 3, 4),
            )
            if path in paths
        }

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.find_workspace_document_symbols",
        find_document_symbols,
    )

    search = search_requirement_evidence(
        repository,
        commit,
        requirement,
        first_ordinal=17,
    )
    assert any(
        item.path == "src/schema.py" and item.kind == "implementation"
        for item in search.evidence
    )
    assert any(
        item.path == "tests/test_schema.py" and item.kind == "test"
        for item in search.evidence
    )
    request = build_requirement_coverage_request(
        requirement,
        source=source,
        evidence=search.evidence,
        repository_root=repository,
        repository_search=search.to_data(),
    )
    service_request = build_service_coverage_request(
        requirement,
        source=source,
        evidence=search.evidence,
        evidence_excerpts={
            item.id: "".join(
                read_pinned_code_file(repository, commit, item.path)[1].splitlines(
                    keepends=True
                )[item.start_line - 1 : item.end_line]
            )
            for item in search.evidence
        },
        repository_search=search.to_data(),
    )
    request.validate()
    service_request.validate()
    assert service_request.question == request.question
    assert service_request.exact_question == request.exact_question
    assert "at most 300 characters" in request.exact_question
    assert "at most 300 characters" in request.answer_field_definitions["reason"]
    assert service_request.repository_evidence == request.repository_evidence
    assert service_request.accepted_facts == request.accepted_facts
    with pytest.raises(EvidenceError, match="content digest"):
        build_service_coverage_request(
            requirement,
            source=source,
            evidence=search.evidence,
            evidence_excerpts={
                item.id: "!"
                + "".join(
                    read_pinned_code_file(repository, commit, item.path)[1].splitlines(
                        keepends=True
                    )[item.start_line - 1 : item.end_line]
                )[1:]
                for item in search.evidence
            },
            repository_search=search.to_data(),
        )
    messages = build_question_messages(request)

    assert search.terms
    assert search.matching_line_count >= 2
    assert search.basedpyright_query_count == 1
    assert search.basedpyright_match_count == 2
    assert search.basedpyright_available
    assert search.rg_match_count > 0
    assert {item.kind for item in search.evidence} == {"implementation", "test"}
    assert {item.id for item in search.evidence} == {
        f"evidence-{ordinal:04d}" for ordinal in range(17, 17 + len(search.evidence))
    }
    assert request.question.context_ref_ids[-1] == "repository-search"
    assert request.repository_evidence[-1].ref_id == "repository-search"
    assert request.accepted_facts[0].ref_id == requirement.id
    assert "repository-search" in messages[1]["content"]
    assert any(
        "parse_payload" in record.value["source_excerpt"]
        for record in request.repository_evidence
    )
    assert request.repository_evidence[-1].value["matching_path_count"] == 2
    assert any(item.symbol == "parse_payload" for item in search.evidence)
    implementation = next(
        item for item in search.evidence if item.kind == "implementation"
    )
    test = next(item for item in search.evidence if item.kind == "test")
    assert (
        request.parse_answer(
            {
                "status": "complete",
                "evidence_ids": [implementation.id, test.id],
                "reason": (
                    "The pinned parser implementation and its test trim whitespace."
                ),
            }
        ).status
        == "complete"
    )


def test_requirement_search_with_no_matches_allows_missing_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _repository(tmp_path)
    requirement, source = _requirement(commit)
    requirement = Requirement(
        requirement.id,
        "Implement an orbital telemetry compressor.",
        requirement.kind,
        requirement.source_refs,
        requirement.source_group_ids,
        requirement.related_requirement_ids,
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.find_workspace_document_symbols",
        lambda paths, **kwargs: {path: [] for path in paths},
    )

    search = search_requirement_evidence(
        repository,
        commit,
        requirement,
        first_ordinal=1,
    )
    request = build_requirement_coverage_request(
        requirement,
        source=source,
        evidence=search.evidence,
        repository_root=repository,
        repository_search=search.to_data(),
    )
    request.validate()

    assert search.evidence == ()
    assert search.matching_line_count == 0
    assert (
        request.parse_answer(
            {
                "status": "missing",
                "evidence_ids": [],
                "reason": (
                    "The bounded search found no implementation; this work remains."
                ),
            }
        ).status
        == "missing"
    )


def test_requirement_search_reports_agent_command_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _repository(tmp_path)
    requirement, _ = _requirement(commit)
    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.find_workspace_document_symbols",
        lambda paths, **kwargs: {path: [] for path in paths},
    )
    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.execute_command",
        lambda *_args, **_kwargs: (
            SimpleNamespace(status="timed_out", exit_code=None),
            SimpleNamespace(data=b""),
            SimpleNamespace(data=b""),
        ),
    )

    with pytest.raises(EvidenceError, match="rg search timed out"):
        search_requirement_evidence(
            repository,
            commit,
            requirement,
            first_ordinal=1,
        )


def test_requirement_search_uses_rg_for_non_python_when_basedpyright_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _ = _repository(tmp_path)
    (repository / "src/telemetry.swift").write_text(
        'func encode_telemetry_payload() { print("telemetry") }\n',
        encoding="utf-8",
    )
    _git(repository, "add", "src/telemetry.swift")
    _git(repository, "commit", "-qm", "add telemetry encoder")
    commit = _git(repository, "rev-parse", "HEAD")
    requirement, _ = _requirement(commit)
    requirement = Requirement(
        requirement.id,
        "Encode telemetry payload safely.",
        requirement.kind,
        requirement.source_refs,
        requirement.source_group_ids,
        requirement.related_requirement_ids,
    )

    def fail_basedpyright(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        del args, kwargs
        raise PowdrrExecutionError("basedpyright is unavailable")

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.evidence.find_workspace_document_symbols",
        fail_basedpyright,
    )

    search = search_requirement_evidence(
        repository,
        commit,
        requirement,
        first_ordinal=1,
    )

    assert search.basedpyright_query_count == 1
    assert search.basedpyright_match_count == 0
    assert search.rg_match_count > 0
    assert not search.basedpyright_available
    assert any(item.path == "src/telemetry.swift" for item in search.evidence)


def test_symbol_lookup_reads_file_before_search_and_never_searches_a_path(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    calls: list[list[str]] = []

    def finder(
        queries: list[str], *, worktree_root: Path, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        assert worktree_root != repository.resolve()
        assert (worktree_root / "src/schema.py").is_file()
        assert limit == 32
        calls.append(queries)
        return {"parse_payload": [_symbol("parse_payload", "src/schema.py", 1, 2)]}

    result = lookup_symbols_in_pinned_file(
        repository,
        commit,
        "src/schema.py",
        ["parse_payload"],
        kind="implementation",
        first_ordinal=3,
        symbol_finder=finder,
    )

    assert calls == [["parse_payload"]]
    assert result.evidence[0].id == "evidence-0003"
    assert result.evidence[0].symbol == "parse_payload"
    assert result.unresolved_queries == ()
    with pytest.raises(EvidenceError, match="not a path"):
        lookup_symbols_in_pinned_file(
            repository,
            commit,
            "src/schema.py",
            ["src/schema.py"],
            kind="implementation",
            first_ordinal=1,
            symbol_finder=finder,
        )


def test_symbol_search_uses_the_pinned_tree_for_dirty_or_advanced_checkouts(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)

    def invented_symbol(
        queries: list[str], *, worktree_root: Path, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        del worktree_root, limit
        return {queries[0]: [_symbol("invented_parser", "src/schema.py", 1, 2)]}

    absent = lookup_symbols_in_pinned_file(
        repository,
        commit,
        "src/schema.py",
        ["invented_parser"],
        kind="implementation",
        first_ordinal=1,
        symbol_finder=invented_symbol,
    )
    assert absent.evidence == ()
    assert absent.unresolved_queries == ("invented_parser",)

    with pytest.raises(EvidenceError, match="tracked file"):
        lookup_symbols_in_pinned_file(
            repository,
            commit,
            "src/invented.py",
            ["parse_payload"],
            kind="implementation",
            first_ordinal=1,
            symbol_finder=invented_symbol,
        )

    (repository / "src/schema.py").write_text("# dirty\n", encoding="utf-8")

    def pinned_symbol(
        queries: list[str], *, worktree_root: Path, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        del limit
        pinned_content = (worktree_root / "src/schema.py").read_text(encoding="utf-8")
        assert "parse_payload" in pinned_content
        assert "dirty" not in pinned_content
        return {queries[0]: [_symbol("parse_payload", "src/schema.py", 1, 2)]}

    result = lookup_symbols_in_pinned_file(
        repository,
        commit,
        "src/schema.py",
        ["parse_payload"],
        kind="implementation",
        first_ordinal=2,
        symbol_finder=pinned_symbol,
    )
    assert result.evidence[0].symbol == "parse_payload"

    original = _git(repository, "show", f"{commit}:src/schema.py")
    (repository / "src/schema.py").write_text(original, encoding="utf-8")
    _git(repository, "add", "src/schema.py")
    _git(repository, "commit", "-qm", "advance base")
    advanced = lookup_symbols_in_pinned_file(
        repository,
        commit,
        "src/schema.py",
        ["parse_payload"],
        kind="implementation",
        first_ordinal=3,
        symbol_finder=pinned_symbol,
    )
    assert advanced.evidence[0].symbol == "parse_payload"


def test_requirement_coverage_search_uses_pinned_code_not_local_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, commit = _repository(tmp_path)
    requirement, source = _requirement(commit)
    requirement = Requirement(
        requirement.id,
        "Parse payload values with the existing parser.",
        requirement.kind,
        requirement.source_refs,
        requirement.source_group_ids,
        requirement.related_requirement_ids,
    )
    (repository / "src/schema.py").write_text(
        "def unrelated_dirty_code():\n    return False\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        evidence_module,
        "find_workspace_symbols",
        lambda queries, *, worktree_root, limit: {query: [] for query in queries},
    )

    result = search_requirements_evidence(repository, commit, (requirement,))[
        requirement.id
    ]

    assert result.base_commit == commit
    assert any(item.path == "src/schema.py" for item in result.evidence)


def test_coverage_contract_distinguishes_complete_partial_and_missing(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    implementation = record_file_evidence(
        repository, commit, "src/schema.py", kind="implementation", ordinal=1
    )
    test = record_file_evidence(
        repository, commit, "tests/test_schema.py", kind="test", ordinal=2
    )
    evidence = {implementation.id: implementation, test.id: test}

    complete = bind_coverage_answer(
        {
            "status": "complete",
            "evidence_ids": [implementation.id, test.id],
            "reason": "Parser and relevant test cover whitespace trimming.",
        },
        evidence_by_id=evidence,
        supplied_evidence_ids=tuple(evidence),
    )
    assert complete.status == "complete"

    partial = bind_coverage_answer(
        {
            "status": "partial",
            "evidence_ids": [implementation.id],
            "reason": "The parser exists, but no focused test covers it.",
            "remaining_statement": "Add a test for surrounding whitespace.",
        },
        evidence_by_id=evidence,
        supplied_evidence_ids=tuple(evidence),
    )
    requirement, _ = _requirement()
    assessment = create_requirement_coverage(
        requirement,
        partial,
        evidence_by_id=evidence,
        supplied_evidence_ids=tuple(evidence),
        revision=2,
    )
    assert assessment.residual is not None
    assert assessment.residual.id == "req-0001-residual-0002"
    assert assessment.residual.source_refs == requirement.source_refs
    assert assessment.residual.source_group_ids == requirement.source_group_ids
    next_assessment = create_requirement_coverage(
        assessment.residual,
        partial,
        evidence_by_id=evidence,
        supplied_evidence_ids=tuple(evidence),
        revision=3,
    )
    assert next_assessment.residual is not None
    assert next_assessment.residual.id == "req-0001-residual-0003"
    assert next_assessment.residual.original_requirement_id == requirement.id

    missing = bind_coverage_answer(
        {"status": "missing", "evidence_ids": [], "reason": "No matching behavior."},
        evidence_by_id=evidence,
        supplied_evidence_ids=tuple(evidence),
    )
    assert missing.status == "missing"
    with pytest.raises(PlanningContractError):
        bind_coverage_answer(
            {
                "status": "missing",
                "evidence_ids": [],
                "reason": "No matching behavior.",
                "remaining_item": {"title": "Invented work item"},
            },
            evidence_by_id=evidence,
            supplied_evidence_ids=tuple(evidence),
        )


def test_coverage_evidence_policy_and_request_context_are_strict(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    implementation = record_file_evidence(
        repository, commit, "src/schema.py", kind="implementation", ordinal=1
    )
    config = record_file_evidence(
        repository, commit, "pyproject.toml", kind="configuration", ordinal=2
    )
    test = record_file_evidence(
        repository, commit, "tests/test_schema.py", kind="test", ordinal=3
    )
    evidence = {item.id: item for item in (implementation, config, test)}

    with pytest.raises(PlanningContractError, match="plus test"):
        bind_coverage_answer(
            {
                "status": "complete",
                "evidence_ids": [implementation.id],
                "reason": "The implementation appears to be present.",
            },
            evidence_by_id=evidence,
            supplied_evidence_ids=(implementation.id,),
        )
    with pytest.raises(PlanningContractError, match="outside the question context"):
        bind_coverage_answer(
            {
                "status": "complete",
                "evidence_ids": [implementation.id, test.id],
                "reason": "Code and tests cover it.",
            },
            evidence_by_id=evidence,
            supplied_evidence_ids=(implementation.id,),
        )
    with pytest.raises(PlanningContractError, match="implementation evidence"):
        bind_coverage_answer(
            {
                "status": "partial",
                "evidence_ids": [test.id],
                "reason": "A test exists.",
                "remaining_statement": "Implement the behavior.",
            },
            evidence_by_id=evidence,
            supplied_evidence_ids=(test.id,),
        )

    requirement, segmented = _requirement(commit)
    request = build_requirement_coverage_request(
        requirement,
        source=segmented,
        evidence=(implementation, test),
        repository_root=repository,
    )
    request.validate()
    assert "def parse_payload" in request.repository_evidence[0].value["source_excerpt"]
    assert request.question.subject_ids == (requirement.id,)
    assert request.source_excerpts == ()
    assert (
        "both relevant implementation/configuration evidence and relevant test"
        in request.exact_question
    )
    assert request.valid_example == {
        "status": "complete",
        "evidence_ids": [implementation.id, test.id],
        "reason": "Pinned implementation and relevant tests cover it.",
    }
    assert set(request.question.context_ref_ids) == {
        implementation.id,
        test.id,
        requirement.id,
    }


def test_coverage_question_does_not_repeat_large_source_anchors(
    tmp_path: Path,
) -> None:
    paragraph = "The repository must preserve this behavior " + ("with detail " * 520)
    text = "# Plan\n\n" + "\n\n".join([paragraph] * 4) + "\n"
    document = PinnedSourceDocument(
        "repo",
        "a" * 40,
        "plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )
    segmented = segment_markdown_source(document)
    source_refs = tuple(item.id for item in segmented.anchors)
    assert len(text) > MAX_QUESTION_INPUT_CHARS
    requirement = Requirement(
        "req-0001",
        "Preserve the validated repository behavior.",
        "behavior",
        source_refs,
        (),
        (),
    )

    request = build_requirement_coverage_request(
        requirement,
        source=segmented,
        evidence=(),
        repository_root=tmp_path,
    )
    messages = build_question_messages(request)

    assert request.source_excerpts == ()
    assert request.accepted_facts[0].value["statement"] == requirement.statement
    assert request.question.context_ref_ids == (requirement.id,)
    assert len(messages[1]["content"]) < MAX_QUESTION_INPUT_CHARS


def test_test_evidence_requires_a_test_or_fixture_path(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    with pytest.raises(EvidenceError, match="test or fixture path"):
        record_file_evidence(
            repository, commit, "src/schema.py", kind="test", ordinal=1
        )


def test_attestation_source_path_is_not_misclassified_as_test() -> None:
    assert not evidence_module._is_test_path(
        "attestation/verifier/src/powdrr_attestation/decision.py"
    )
    assert evidence_module._is_test_path("attestation/verifier/tests/test_decision.py")
