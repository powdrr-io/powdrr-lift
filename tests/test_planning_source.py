from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from powdrr_local.planning.source import read_pinned_source
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.questions import (
    MAX_SOURCE_EXTRACTION_BATCH_INPUT_CHARS,
    batch_question_requests,
    build_question_messages,
)
from powdrr_service.planning.source import (
    EquivalenceDecision,
    PinnedSourceDocument,
    PlanningSourceError,
    Requirement,
    allocate_requirements,
    build_block_disposition_request,
    build_equivalence_request,
    build_extract_requirements_requests,
    build_fidelity_request,
    deduplicate_requirement_records,
    extract_implementation_phases,
    merge_equivalent_requirements,
    parse_block_disposition,
    parse_equivalence_decision,
    parse_fidelity_decision,
    parse_requirement_page,
    requirement_extraction_anchor_pages,
    segment_markdown_source,
    source_block_input_digest,
    validate_source_coverage,
)
from powdrr_service.planning.source import (
    pinned_source_document as service_pinned_source_document,
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

    (repository / "plan.md").write_text("uncommitted change\n", encoding="utf-8")
    source = read_pinned_source(str(repository), commit, "plan.md")

    assert source.commit_sha == commit
    assert source.text == "# Plan\n\nKeep this exact.\n"
    assert source.blob_sha == _git(repository, "rev-parse", f"{commit}:plan.md")
    with pytest.raises(PlanningSourceError, match="normalized"):
        read_pinned_source(str(repository), commit, "../plan.md")
    with pytest.raises(PlanningSourceError, match="tracked file"):
        read_pinned_source(str(repository), commit, "missing.md")


def test_service_source_document_validates_git_blob_identity(tmp_path: Path) -> None:
    repository, commit = _committed_repository(tmp_path, "# Plan\n\nPinned.\n")
    blob_sha = _git(repository, "rev-parse", f"{commit}:plan.md")
    content = (repository / "plan.md").read_bytes()

    source = service_pinned_source_document(
        "powdrr-io/example", commit, "plan.md", blob_sha, content
    )

    assert source.repository == "powdrr-io/example"
    assert source.text == "# Plan\n\nPinned.\n"
    with pytest.raises(PlanningSourceError, match="do not match"):
        service_pinned_source_document(
            "powdrr-io/example", commit, "plan.md", "0" * len(blob_sha), content
        )


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


def test_extracts_explicit_implementation_pr_sequence_and_dependencies() -> None:
    source = _document(
        "# Plan\n\n"
        "## Implementation PR sequence\n\n"
        "| PR | Title | Dependencies | Reviewable result |\n"
        "| --- | --- | --- | --- |\n"
        "| 01 | Define schemas | None | Frozen contract |\n"
        "| 02 | Implement verifier | 01 | Offline verifier |\n"
        "| 03 | Release | All prior PRs | Reviewed release |\n\n"
        "### PR 01: Define schemas\n\nWrite schemas.\n"
    )

    phases = extract_implementation_phases(segment_markdown_source(source))

    assert [
        (phase.id, phase.title, phase.dependencies, phase.reviewable_result)
        for phase in phases
    ] == [
        ("pr-01", "Define schemas", (), "Frozen contract"),
        ("pr-02", "Implement verifier", ("pr-01",), "Offline verifier"),
        (
            "pr-03",
            "Release",
            ("pr-01", "pr-02"),
            "Reviewed release",
        ),
    ]


def test_rejects_forward_dependency_in_implementation_sequence() -> None:
    source = _document(
        "# Plan\n\n"
        "## Implementation PR sequence\n\n"
        "| PR | Title | Dependencies | Reviewable result |\n"
        "| --- | --- | --- | --- |\n"
        "| 01 | First | 02 | First output |\n"
        "| 02 | Second | None | Second output |\n"
    )

    with pytest.raises(PlanningSourceError, match="unknown or forward"):
        extract_implementation_phases(segment_markdown_source(source))


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
        ]
    }

    parsed = parse_requirement_page(
        answer,
        allowed_source_refs={"anchor-0001", "anchor-0002"},
        page_anchor_ids=("anchor-0001",),
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
        ]
    }
    with pytest.raises(ValueError, match="cite at least one anchor"):
        parse_requirement_page(
            unanchored_answer,
            allowed_source_refs={"anchor-0001", "anchor-0002"},
            page_anchor_ids=("anchor-0001",),
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
        "req-0001",
        "Keep each source anchor.",
        "constraint",
        ("anchor-0001",),
        (),
        (),
        source_validation_groups=(("validation:source-a", "conditional"),),
    )
    duplicate = Requirement(
        "req-0002",
        "Preserve all source anchors.",
        "constraint",
        ("anchor-0002",),
        (),
        (),
        source_validation_groups=(("validation:source-b", "alternatives"),),
    )

    merged = merge_equivalent_requirements(
        original, duplicate, EquivalenceDecision(True, "Same obligation.")
    )

    assert merged.id == original.id
    assert merged.source_refs == ("anchor-0001", "anchor-0002")
    assert merged.source_validation_groups == (
        ("validation:source-a", "conditional"),
        ("validation:source-b", "alternatives"),
    )
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


def test_requirement_allocation_carries_source_validation_relations() -> None:
    segmented = segment_markdown_source(
        _document("The parser handles timeouts and network failures.\n")
    )
    clause = segmented.instruction_ledger.clauses[0]
    grouped_clause = replace(
        clause,
        validation_group_id="validation:instruction-001:1",
        validation_relation="conditional",
    )
    ledger = replace(segmented.instruction_ledger, clauses=(grouped_clause,))
    segmented = replace(segmented, instruction_ledger=ledger)
    anchor = next(
        item for item in segmented.anchors if item.clause_id == clause.clause_id
    )

    requirements, _drafts = allocate_requirements(
        [
            {
                "statement": "Handle parser timeouts.",
                "kind": "behavior",
                "source_refs": [anchor.id],
                "related_source_refs": [],
            }
        ],
        first_ordinal=1,
        anchors={item.id: item for item in segmented.anchors},
        ledger=segmented.instruction_ledger,
    )

    assert requirements[0].source_group_ids == ("validation:instruction-001:1",)
    assert requirements[0].source_validation_groups == (
        ("validation:instruction-001:1", "conditional"),
    )


def test_exact_duplicate_extractions_merge_related_refs_without_merging_locations() -> (
    None
):
    duplicate_a = {
        "statement": "Implement the requested behavior.",
        "kind": "behavior",
        "source_refs": ["anchor-a"],
        "related_source_refs": ["context-a"],
    }
    duplicate_b = {
        "statement": "  IMPLEMENT the requested behavior.  ",
        "kind": "behavior",
        "source_refs": ["anchor-a"],
        "related_source_refs": ["context-b", "context-a"],
    }
    repeated_elsewhere = {
        **duplicate_a,
        "source_refs": ["anchor-b"],
    }

    result = deduplicate_requirement_records(
        [duplicate_a, duplicate_b, repeated_elsewhere]
    )

    assert result == (
        {
            **duplicate_a,
            "related_source_refs": ["context-a", "context-b"],
        },
        repeated_elsewhere,
    )


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
    extraction = build_extract_requirements_requests(
        first_block, anchors={item.id: item for item in segmented.anchors}
    )[0]
    assert extraction.question.kind == "extract_requirements"
    assert len(extraction.source_excerpts) == 1
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


def test_requirement_extraction_example_does_not_invent_an_obligation() -> None:
    segmented = segment_markdown_source(
        _document(
            "# Plan\n\nNavigation:\n\n- [Trust assumptions](#trust-assumptions)\n"
        )
    )
    block = next(item for item in segmented.blocks if "Navigation:" in item.text)
    anchors = {item.id: item for item in segmented.anchors}

    request = build_extract_requirements_requests(block, anchors=anchors)[0]

    assert request.valid_example["records"] == []
    request.parse_answer(request.valid_example)


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


def test_requirement_extraction_uses_fixed_non_overlapping_windows() -> None:
    clauses = [
        f"Requirement {index:02d} must preserve behavior and validation."
        for index in range(1, 19)
    ]
    segmented = segment_markdown_source(_document("# Plan\n\n" + " ".join(clauses)))
    block = next(item for item in segmented.blocks if item.kind == "paragraph")
    anchors = {item.id: item for item in segmented.anchors}
    expected = tuple(
        ref for ref in block.anchor_ids if anchors[ref].clause_id is not None
    )
    windows = requirement_extraction_anchor_pages(block, anchors)

    assert tuple(ref for window in windows for ref in window) == expected
    assert all(1 <= len(window) <= 8 for window in windows)
    assert len({ref for window in windows for ref in window}) == len(expected)
    requests = build_extract_requirements_requests(block, anchors=anchors)
    assert len(requests) == len(windows)
    for request, window in zip(requests, windows, strict=True):
        assert request.question.context_ref_ids.count(block.id) == 1
        assert request.answer_schema["required"] == ["records"]
        assert "has_more" not in request.answer_schema["properties"]
        assert "next_anchor" not in request.answer_schema["properties"]
        window_fact = next(
            fact
            for fact in request.accepted_facts
            if fact.ref_id.startswith("source-window-")
        )
        assert window_fact.value["anchor_ids"] == list(window)
        assert request.parse_answer({"records": []}).records == ()

    first_request = requests[0]
    first_page = windows[0]
    valid_record = {
        "statement": "Preserve the behavior required by this clause.",
        "kind": "behavior",
        "source_refs": [first_page[0]],
        "related_source_refs": [first_page[1]],
    }
    assert first_request.parse_answer({"records": [valid_record]}).records[0][
        "source_refs"
    ] == (first_page[0],)
    with pytest.raises(PlanningContractError, match="outside supplied context"):
        first_request.parse_answer(
            {
                "records": [
                    {
                        **valid_record,
                        "source_refs": [windows[1][0]],
                        "related_source_refs": [],
                    }
                ]
            }
        )
    with pytest.raises(PlanningContractError, match="at most 64"):
        first_request.parse_answer({"records": [valid_record] * 65})


def test_source_edit_invalidates_deterministic_extraction_windows() -> None:
    first = segment_markdown_source(
        _document("# Plan\n\nThe source requires stable behavior.\n")
    )
    second = segment_markdown_source(
        _document("# Plan\n\nThe source requires stable output.\n")
    )
    first_block = next(item for item in first.blocks if item.kind == "paragraph")
    second_block = next(item for item in second.blocks if item.kind == "paragraph")
    first_request = build_extract_requirements_requests(
        first_block, anchors={item.id: item for item in first.anchors}
    )[0]
    second_request = build_extract_requirements_requests(
        second_block, anchors={item.id: item for item in second.anchors}
    )[0]

    assert first_request.question.question_id != second_request.question.question_id
    assert first_request.question.input_digest != second_request.question.input_digest


def test_source_extraction_windows_keep_question_input_bounded() -> None:
    clauses = [
        f"Requirement {index:02d} must preserve its own behavior and validation result"
        for index in range(1, 41)
    ]
    segmented = segment_markdown_source(
        _document("# Plan\n\n" + ". ".join(clauses) + ".\n")
    )
    block = next(item for item in segmented.blocks if item.kind == "paragraph")
    anchors = {item.id: item for item in segmented.anchors}
    windows = requirement_extraction_anchor_pages(block, anchors)

    assert len(windows) >= 5
    requests = build_extract_requirements_requests(block, anchors=anchors)
    for request, window in zip(requests, windows, strict=True):
        build_question_messages(request)
        assert len(window) <= 8
        assert request.parse_answer(request.valid_example).records == ()


def test_extraction_questions_batch_with_distinct_source_windows() -> None:
    segmented = segment_markdown_source(
        _document(
            "# Plan\n\n"
            + "\n\n".join(
                f"Requirement {index} must be implemented." for index in range(1, 7)
            )
            + "\n"
        )
    )
    blocks = [block for block in segmented.blocks if block.kind == "paragraph"]
    assert len(blocks) == 6
    anchors = {anchor.id: anchor for anchor in segmented.anchors}
    requests = tuple(
        build_extract_requirements_requests(block, anchors=anchors)[0]
        for block in blocks
    )

    (batch,) = batch_question_requests(requests)
    assert len(batch.batch_members) == 6
    assert batch.max_input_chars == MAX_SOURCE_EXTRACTION_BATCH_INPUT_CHARS
    packet = json.loads(build_question_messages(batch)[1]["content"])
    shared_contract = next(
        item["value"]
        for item in packet["accepted_facts"]
        if item["ref_id"] == "shared-member-question-contract"
    )
    assert shared_contract["prompt"] == requests[0].exact_question
    for request in requests:
        request.parse_answer(request.valid_example)
        assert "next_anchor" not in request.answer_schema["properties"]
        window_fact = next(
            fact
            for fact in request.accepted_facts
            if fact.ref_id.startswith("source-window-")
        )
        assert len(window_fact.value["anchor_ids"]) == 1


def test_requirement_extraction_excludes_block_alias_when_clause_anchor_exists() -> (
    None
):
    segmented = segment_markdown_source(
        _document("# Plan\n\nImplement the requested behavior.\n")
    )
    block = next(item for item in segmented.blocks if item.kind == "paragraph")
    anchors = {anchor.id: anchor for anchor in segmented.anchors}
    clause_ids = tuple(
        ref for ref in block.anchor_ids if anchors[ref].clause_id is not None
    )
    assert len(clause_ids) == 1

    request = build_extract_requirements_requests(block, anchors=anchors)[0]

    assert clause_ids[0] in request.question.context_ref_ids
    assert block.anchor_ids[0] not in request.question.context_ref_ids
    with pytest.raises(PlanningContractError, match="outside supplied context"):
        request.parse_answer(
            {
                "records": [
                    {
                        "statement": "Implement the requested behavior.",
                        "kind": "behavior",
                        "source_refs": [block.anchor_ids[0]],
                        "related_source_refs": [],
                    }
                ],
            }
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
