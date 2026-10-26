from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workrr.planning.source import (
    EquivalenceDecision,
    PinnedSourceDocument,
    PlanningSourceError,
    Requirement,
    allocate_requirements,
    build_block_disposition_request,
    build_equivalence_request,
    build_extract_requirements_request,
    build_fidelity_request,
    merge_equivalent_requirements,
    parse_block_disposition,
    parse_equivalence_decision,
    parse_fidelity_decision,
    parse_requirement_page,
    read_pinned_source,
    segment_markdown_source,
    source_block_input_digest,
    validate_source_coverage,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _committed_repository(tmp_path: Path, source: str) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "planner@example.test")
    _git(repository, "config", "user.name", "Planner Test")
    (repository / "plan.md").write_text(source, encoding="utf-8")
    _git(repository, "add", "plan.md")
    _git(repository, "commit", "-qm", "source")
    return repository, _git(repository, "rev-parse", "HEAD")


def _document(
    text: str,
    *,
    commit: str = "a" * 40,
    blob: str = "b" * 40,
    path: str = "plan.md",
) -> PinnedSourceDocument:
    return PinnedSourceDocument(
        "repo",
        commit,
        path,
        blob,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text,
    )


def test_reads_exact_tracked_source_from_pinned_commit(tmp_path: Path) -> None:
    repository, commit = _committed_repository(tmp_path, "# Plan\n\nKeep this exact.\n")

    source = read_pinned_source(str(repository), commit, "plan.md")

    assert source.commit_sha == commit
    assert source.text == "# Plan\n\nKeep this exact.\n"
    assert source.blob_sha == _git(repository, "rev-parse", f"{commit}:plan.md")
    with pytest.raises(PlanningSourceError, match="normalized"):
        read_pinned_source(str(repository), commit, "../plan.md")
    with pytest.raises(PlanningSourceError, match="tracked file"):
        read_pinned_source(str(repository), commit, "missing.md")


def test_segments_markdown_with_headings_late_setup_and_cross_references() -> None:
    text = (
        "# Proposal\n\n"
        "The worker must preserve every source anchor.\n\n"
        "## Implementation\n\n"
        "1. Keep dependencies explicit.\n"
        "2. Reuse the first requirement's artifact.\n\n"
        "## Human setup\n\n"
        "Before validation, the human must install the SubQ API key.\n\n"
        "| Setting | Meaning |\n| --- | --- |\n| SUBQ_KEY | credential |\n"
    )
    doc = PinnedSourceDocument(
        "repo",
        "a" * 40,
        "plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )

    segmented = segment_markdown_source(doc, max_block_chars=100)

    assert len(segmented.blocks) >= 7
    assert any("Human setup" in block.heading_context for block in segmented.blocks)
    late_block = next(
        block for block in segmented.blocks if "install the SubQ" in block.text
    )
    assert late_block.clause_ids
    anchor = next(
        item for item in segmented.anchors if item.id in late_block.anchor_ids
    )
    assert text[anchor.start_offset : anchor.end_offset].startswith("Before validation")
    assert any(block.kind == "table" for block in segmented.blocks)
    assert all(
        block.next_block_id == segmented.blocks[index + 1].id
        for index, block in enumerate(segmented.blocks[:-1])
    )
    assert source_block_input_digest(late_block).startswith("sha256:")


def test_oversized_fence_is_split_without_losing_ranges() -> None:
    text = "# Code\n\n```text\n" + ("0123456789\n" * 20) + "```\n"
    doc = _document(text)

    segmented = segment_markdown_source(doc, max_block_chars=32)

    fence_blocks = [item for item in segmented.blocks if item.kind == "fence"]
    assert len(fence_blocks) > 1
    assert all(
        item.continuation_of == fence_blocks[0].continuation_of for item in fence_blocks
    )
    assert (
        "".join(item.text for item in fence_blocks)
        == "```text\n" + ("0123456789\n" * 20) + "```\n"
    )
    assert [item.continuation_index for item in fence_blocks] == list(
        range(1, len(fence_blocks) + 1)
    )


def test_setext_markdown_headings_are_preserved_as_heading_context() -> None:
    text = "Overview\n========\n\nThe worker records each decision.\n"
    document = _document(text)

    segmented = segment_markdown_source(document)

    body = next(block for block in segmented.blocks if "worker records" in block.text)
    assert body.heading_context == ("Overview",)
    assert any(block.kind == "heading" for block in segmented.blocks)


def test_block_question_digest_changes_when_pinned_source_changes() -> None:
    first = _document("First behavior.\n")
    second = _document("Changed behavior.\n", commit="c" * 40, blob="d" * 40)

    first_block = segment_markdown_source(first).blocks[0]
    second_block = segment_markdown_source(second).blocks[0]

    assert source_block_input_digest(first_block) != source_block_input_digest(
        second_block
    )

    appended = _document(
        "First behavior.\n\nUnrelated later behavior.\n",
        commit="e" * 40,
        blob="f" * 40,
    )
    appended_first_block = segment_markdown_source(appended).blocks[0]
    assert source_block_input_digest(first_block) == source_block_input_digest(
        appended_first_block
    )


def test_extraction_contract_rejects_unknown_or_unanchored_obligations() -> None:
    answer = {
        "records": [
            {
                "statement": "The human installs the API key before validation.",
                "kind": "human_setup",
                "source_refs": ["anchor-0001"],
                "related_source_refs": ["anchor-0002"],
            }
        ],
        "has_more": False,
        "next_anchor": None,
    }

    parsed = parse_requirement_page(
        answer,
        allowed_source_refs={"anchor-0001", "anchor-0002"},
        pagination_anchor_ids=("anchor-0001",),
    )

    assert parsed.records[0]["kind"] == "human_setup"
    unanchored_answer = {
        "records": [
            {
                "statement": "The human installs the API key before validation.",
                "kind": "human_setup",
                "source_refs": [],
                "related_source_refs": ["anchor-0002"],
            }
        ],
        "has_more": False,
        "next_anchor": None,
    }
    with pytest.raises(ValueError, match="cite at least one anchor"):
        parse_requirement_page(
            unanchored_answer,
            allowed_source_refs={"anchor-0001", "anchor-0002"},
            pagination_anchor_ids=("anchor-0001",),
        )


def test_dispositions_require_resolved_obligations_and_cover_each_block() -> None:
    normative = parse_block_disposition(
        {"disposition": "normative", "requirement_ids": ["req-0001"]},
        block_id="block-0001",
        known_requirement_ids={"req-0001"},
    )
    context = parse_block_disposition(
        {"disposition": "context", "reason": "Explains project background."},
        block_id="block-0002",
        known_requirement_ids={"req-0001"},
    )
    blocks = segment_markdown_source(
        _document("First.\n\nBackground.\n", path="p.md")
    ).blocks
    requirement = Requirement(
        "req-0001", "First behavior.", "behavior", ("anchor-0001",), (), ()
    )
    validate_source_coverage(blocks, (normative, context), (requirement,))
    with pytest.raises(PlanningSourceError, match="unresolved"):
        validate_source_coverage(blocks, (normative,), (requirement,))
    with pytest.raises(ValueError, match="unknown requirement"):
        parse_block_disposition(
            {
                "disposition": "duplicate",
                "requirement_ids": ["req-nope"],
                "reason": "Same.",
            },
            block_id="block-0002",
            known_requirement_ids={"req-0001"},
        )


def test_fidelity_and_equivalence_are_tagged_and_strict() -> None:
    assert (
        parse_fidelity_decision({"decision": "accept", "reason": "Faithful."}).statement
        is None
    )
    revised = parse_fidelity_decision(
        {
            "decision": "revise",
            "statement": "Keep every source anchor.",
            "reason": "Clarifies scope.",
        }
    )
    assert revised.statement == "Keep every source anchor."
    assert parse_equivalence_decision(
        {"equivalent": False, "reason": "Partial overlap."}
    ) == EquivalenceDecision(False, "Partial overlap.")


def test_equivalent_duplicate_merge_keeps_both_source_anchors() -> None:
    original = Requirement(
        "req-0001", "Keep each source anchor.", "constraint", ("anchor-0001",), (), ()
    )
    duplicate = Requirement(
        "req-0002",
        "Preserve all source anchors.",
        "constraint",
        ("anchor-0002",),
        (),
        (),
    )

    merged = merge_equivalent_requirements(
        original, duplicate, EquivalenceDecision(True, "Same obligation.")
    )

    assert merged.id == original.id
    assert merged.source_refs == ("anchor-0001", "anchor-0002")
    with pytest.raises(PlanningSourceError, match="cannot be merged"):
        merge_equivalent_requirements(
            original, duplicate, EquivalenceDecision(False, "Partial overlap.")
        )


def test_requirement_ids_are_code_owned_and_related_anchors_are_retained() -> None:
    document = _document(
        "Implement the new command.\n\nReuse the result from the first command.\n"
    )
    segmented = segment_markdown_source(document)
    first, second = [item for item in segmented.blocks if item.kind == "paragraph"]
    record = {
        "statement": "The second command reuses the first command's result.",
        "kind": "constraint",
        "source_refs": [second.anchor_ids[0]],
        "related_source_refs": [first.anchor_ids[0]],
    }

    requirements, drafts = allocate_requirements(
        [record],
        first_ordinal=7,
        anchors={item.id: item for item in segmented.anchors},
        ledger=segmented.instruction_ledger,
    )

    assert requirements[0].id == "req-0007"
    assert requirements[0].source_refs == (second.anchor_ids[0],)
    assert drafts[0].related_source_refs == (first.anchor_ids[0],)


def test_source_question_builders_bind_context_examples_and_digests() -> None:
    text = (
        "# Plan\n\nThe first requirement must be implemented.\n\n"
        "The second requirement reuses its output.\n"
    )
    document = _document(text)
    segmented = segment_markdown_source(document)
    first_block, second_block = (
        block for block in segmented.blocks if block.kind == "paragraph"
    )
    extraction = build_extract_requirements_request(
        first_block,
        anchors={item.id: item for item in segmented.anchors},
        related_blocks=(second_block,),
    )
    assert extraction.question.kind == "extract_requirements"
    assert len(extraction.source_excerpts) == 2
    extraction.validate()

    requirement_a = Requirement(
        "req-0001",
        "Implement the first behavior.",
        "behavior",
        first_block.anchor_ids,
        (),
        (),
    )
    requirement_b = Requirement(
        "req-0002",
        "Reuse the first output.",
        "behavior",
        second_block.anchor_ids,
        (),
        (),
    )
    disposition = build_block_disposition_request(
        first_block, requirement_ids=(requirement_a.id,)
    )
    fidelity = build_fidelity_request(requirement_a, segmented=segmented)
    equivalence = build_equivalence_request(
        requirement_a, requirement_b, segmented=segmented
    )
    assert disposition.question.kind == "classify_source_block"
    assert fidelity.question.kind == "check_requirement_fidelity"
    assert equivalence.question.kind == "compare_requirement_equivalence"
    for request in (disposition, fidelity, equivalence):
        request.validate()


def test_block_without_extracted_requirements_can_be_classified_as_context() -> None:
    block = next(
        block
        for block in segment_markdown_source(
            _document("# Background\n\nContext only.\n")
        ).blocks
        if block.kind == "paragraph"
    )

    request = build_block_disposition_request(block, requirement_ids=())

    request.validate()
    assert request.empty_example == {
        "disposition": "context",
        "reason": "This block supplies background only.",
    }


def test_requirement_extraction_pagination_cursor_must_advance_in_block() -> None:
    page = {
        "records": [],
        "has_more": True,
        "next_anchor": "anchor-0002",
    }
    parsed = parse_requirement_page(
        page,
        allowed_source_refs={"anchor-0001", "anchor-0002"},
        pagination_anchor_ids=("anchor-0001", "anchor-0002"),
        after_anchor="anchor-0001",
    )
    assert parsed.next_anchor == "anchor-0002"
    with pytest.raises(ValueError, match="must advance"):
        parse_requirement_page(
            {**page, "next_anchor": "anchor-0001"},
            allowed_source_refs={"anchor-0001", "anchor-0002"},
            pagination_anchor_ids=("anchor-0001", "anchor-0002"),
            after_anchor="anchor-0001",
        )


def test_high_density_prose_is_subdivided_before_anchor_context_overflows() -> None:
    text = " ".join(
        f"Requirement number {index} must be preserved." for index in range(90)
    )
    document = _document(text)

    segmented = segment_markdown_source(document)

    assert len(segmented.blocks) > 1
    assert all(len(block.clause_ids) <= 64 for block in segmented.blocks)
    assert "".join(block.text for block in segmented.blocks) == text
