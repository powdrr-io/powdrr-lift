from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.planning.contracts import PlanningContractError
from powdrr_lift.workrr.planning.evidence import (
    Evidence,
    EvidenceError,
    bind_coverage_answer,
    build_requirement_coverage_request,
    create_requirement_coverage,
    lookup_symbols_in_pinned_file,
    read_pinned_code_file,
    record_file_evidence,
)
from powdrr_lift.workrr.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
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


def test_symbol_lookup_reads_file_before_search_and_never_searches_a_path(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    calls: list[list[str]] = []

    def finder(
        queries: list[str], *, worktree_root: Path, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        assert worktree_root == repository.resolve()
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


def test_invented_symbols_paths_dirty_checkouts_and_unpinned_heads_fail(
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
    with pytest.raises(EvidenceError, match="clean pinned checkout"):
        lookup_symbols_in_pinned_file(
            repository,
            commit,
            "src/schema.py",
            ["parse_payload"],
            kind="implementation",
            first_ordinal=1,
            symbol_finder=invented_symbol,
        )
    original = _git(repository, "show", f"{commit}:src/schema.py")
    (repository / "src/schema.py").write_text(original, encoding="utf-8")
    _git(repository, "add", "src/schema.py")
    _git(repository, "commit", "-qm", "advance base")
    with pytest.raises(EvidenceError, match="does not match pinned"):
        lookup_symbols_in_pinned_file(
            repository,
            commit,
            "src/schema.py",
            ["parse_payload"],
            kind="implementation",
            first_ordinal=1,
            symbol_finder=invented_symbol,
        )


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
    assert set(request.question.context_ref_ids) == {
        *requirement.source_refs,
        implementation.id,
        test.id,
        requirement.id,
    }


def test_test_evidence_requires_a_test_or_fixture_path(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    with pytest.raises(EvidenceError, match="test or fixture path"):
        record_file_evidence(
            repository, commit, "src/schema.py", kind="test", ordinal=1
        )
