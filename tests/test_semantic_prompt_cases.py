from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.semantic_prompt_cases import (
    SemanticPromptCaseError,
    load_semantic_prompt_cases,
    validate_semantic_prompt_cases,
)
from powdrr_lift.workrr.semantic_prompt_variants import (
    generate_semantic_prompt_variants,
)

CASE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "evaluations"
    / "semantic-prompt-cases-v1.jsonl"
)


def _case(case_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "group_id": case_id,
        "family": "joint_vs_alternatives",
        "domain": "domain-a",
        "split": "development",
        "source_text": "The task requires both a label and a value.",
        "target_proposition": "Both a label and a value are required.",
        "permitted_local_context": [],
        "expected_decisions": {"requirement_relation": "all_required"},
        "explicitly_unspecified": [],
        "required_prompt_claims": ["Include both label and value."],
        "forbidden_prompt_claims": ["Either field alone is enough."],
        **overrides,
    }


def test_semantic_prompt_case_corpus_has_required_family_and_domain_coverage() -> None:
    cases = load_semantic_prompt_cases(CASE_PATH)

    summary = validate_semantic_prompt_cases(cases)

    assert summary["case_count"] == 72
    assert set(summary["family_counts"]) == {
        "joint_vs_alternatives",
        "conditional_modes",
        "scope_and_ownership",
        "lifecycle_mutation_identity",
        "copy_semantics",
        "validation_presence_boundaries",
    }
    assert all(count >= 10 for count in summary["family_counts"].values())
    assert set(summary["family_domains"]["copy_semantics"]) == {
        "configuration_cache",
        "reporting_query",
        "state_management",
    }
    assert set(summary["split_counts"]) == {"development", "held_out"}
    assert summary["held_out_domains"] == ["configuration_cache"]


def test_corpus_contains_explicit_positive_controls() -> None:
    cases = {case["case_id"]: case for case in load_semantic_prompt_cases(CASE_PATH)}

    assert cases["copy-003"]["expected_decisions"]["copy_depth"] == "deep"
    assert cases["lifecycle-011"]["expected_decisions"]["object_identity"] == (
        "same_object"
    )
    assert cases["lifecycle-012"]["expected_decisions"]["mutation_propagation"] == (
        "write_through"
    )
    assert cases["joint-011"]["expected_decisions"]["requirement_relation"] == (
        "allowed_alternatives"
    )
    assert cases["validation-010"]["expected_decisions"]["argument_presence"] == (
        "both_arguments_forbidden"
    )


def test_case_group_cannot_leak_across_splits() -> None:
    cases = [
        _case("base", group_id="same-group"),
        _case("variant", group_id="same-group", split="held_out"),
        _case("third", domain="domain-b"),
    ]

    with pytest.raises(SemanticPromptCaseError, match="leaks across"):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_domain_cannot_be_split_between_development_and_held_out() -> None:
    cases = [
        _case("development"),
        _case("held-out", split="held_out"),
    ]

    with pytest.raises(SemanticPromptCaseError, match="domain domain-a appears"):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_variant_must_share_a_base_group_and_split() -> None:
    cases = [
        _case("base"),
        _case("variant", variant_of="base", group_id="different-group"),
        _case("third", domain="domain-b"),
    ]

    with pytest.raises(
        SemanticPromptCaseError, match="does not share its base case group"
    ):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_variant_must_preserve_base_gold_decisions() -> None:
    cases = [
        _case("base"),
        _case(
            "variant",
            variant_of="base",
            group_id="base",
            expected_decisions={"requirement_relation": "allowed_alternatives"},
        ),
        _case("third", domain="domain-b"),
        _case("fourth", domain="domain-c"),
    ]

    with pytest.raises(
        SemanticPromptCaseError, match="changes its base case expected_decisions"
    ):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_contrast_must_change_a_gold_decision_and_keep_lineage() -> None:
    cases = [
        _case("base"),
        _case(
            "contrast",
            contrast_of="base",
            group_id="base",
            expected_decisions={"requirement_relation": "allowed_alternatives"},
        ),
        _case("third", domain="domain-b"),
        _case("fourth", domain="domain-c"),
    ]

    summary = validate_semantic_prompt_cases(
        cases,
        minimum_cases_per_family=1,
        expected_families=frozenset({"joint_vs_alternatives"}),
    )

    assert summary["case_count"] == 4


def test_contrast_cannot_reuse_base_gold_decisions() -> None:
    cases = [
        _case("base"),
        _case("contrast", contrast_of="base", group_id="base"),
        _case("third", domain="domain-b"),
        _case("fourth", domain="domain-c"),
    ]

    with pytest.raises(SemanticPromptCaseError, match="does not change any expected"):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_case_cannot_be_both_paraphrase_and_contrast() -> None:
    cases = [
        _case("base"),
        _case("variant", variant_of="base", contrast_of="base", group_id="base"),
        _case("third", domain="domain-b"),
        _case("fourth", domain="domain-c"),
    ]

    with pytest.raises(
        SemanticPromptCaseError, match="both a paraphrase and a contrast"
    ):
        validate_semantic_prompt_cases(
            cases,
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


class _VariantGenerator:
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        case = json.loads(messages[1]["content"])["case"]
        return {
            "variants": [
                {
                    "kind": "paraphrase",
                    "source_text": (
                        "The session keeps both the current user "
                        "and selected workspace."
                    ),
                    "target_proposition": case["target_proposition"],
                },
                {
                    "kind": "contrast",
                    "source_text": (
                        "The session keeps either the current user "
                        "or selected workspace."
                    ),
                    "target_proposition": "The session keeps either field.",
                },
            ]
        }


class _VariantReviewer:
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = json.loads(messages[1]["content"])
        source_text = payload["candidate"]["source_text"]
        if payload["kind"] == "paraphrase":
            return {
                "accepted": True,
                "rationale": "The candidate preserves the joint requirement.",
                "evidence_quote": source_text,
            }
        decisions = dict(payload["base"]["expected_decisions"])
        current_relation = decisions.get("requirement_relation")
        decisions["requirement_relation"] = (
            "all_required"
            if current_relation == "allowed_alternatives"
            else "allowed_alternatives"
        )
        return {
            "accepted": True,
            "rationale": "Either is explicitly permitted in this contrast.",
            "evidence_quote": source_text,
            "target_proposition": "Either field is sufficient.",
            "expected_decisions": decisions,
            "explicitly_unspecified": [],
            "required_prompt_claims": ["Either field is sufficient."],
            "forbidden_prompt_claims": ["Both fields are required."],
        }


def test_variant_generation_reviews_candidates_and_preserves_split_groups() -> None:
    cases = load_semantic_prompt_cases(CASE_PATH)
    result = generate_semantic_prompt_variants(
        cases,
        generator=_VariantGenerator(),
        reviewer=_VariantReviewer(),
    )

    assert len(result["cases"]) == 2 * len(cases)
    assert result["rejected"] == []
    generated = result["cases"]
    bases_by_id = {case["case_id"]: case for case in cases}
    assert all(
        case["group_id"]
        == bases_by_id[case.get("variant_of", case.get("contrast_of"))]["group_id"]
        for case in generated
    )
    assert all(
        case["split"] == "held_out"
        for case in generated
        if case["domain"] == "configuration_cache"
    )
    paraphrases = [case for case in generated if "variant_of" in case]
    assert all(
        case["target_proposition"]
        == bases_by_id[case["variant_of"]]["target_proposition"]
        for case in paraphrases
    )


def test_variant_generator_normalizes_model_kind_names_and_candidate_targets() -> None:
    class SynonymGenerator:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            case = json.loads(messages[1]["content"])["case"]
            return {
                "variants": [
                    {
                        "kind": "faithful_paraphrase",
                        "source_text": case["source_text"] + " It keeps both.",
                        "target_proposition": "A model-written alternate target.",
                    },
                    {
                        "kind": "minimal_contrast",
                        "source_text": "Only one of these fields is stored.",
                        "target_proposition": "One field is sufficient.",
                    },
                ]
            }

    cases = load_semantic_prompt_cases(CASE_PATH)
    result = generate_semantic_prompt_variants(
        cases,
        generator=SynonymGenerator(),
        reviewer=_VariantReviewer(),
    )

    assert len(result["cases"]) == 2 * len(cases)
    assert not result["rejected"]


def test_variant_generation_rejects_unchanged_source_for_contrast() -> None:
    class UnchangedContrastGenerator:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            case = json.loads(messages[1]["content"])["case"]
            return {
                "variants": [
                    {
                        "kind": "paraphrase",
                        "source_text": case["source_text"] + " Both are kept.",
                        "target_proposition": case["target_proposition"],
                    },
                    {
                        "kind": "minimal_contrast",
                        "source_text": case["source_text"],
                        "target_proposition": "An unsupported contrast.",
                    },
                ]
            }

    cases = load_semantic_prompt_cases(CASE_PATH)
    result = generate_semantic_prompt_variants(
        cases,
        generator=UnchangedContrastGenerator(),
        reviewer=_VariantReviewer(),
    )

    assert len(result["cases"]) == len(cases)
    assert len(result["rejected"]) == len(cases)
    assert all(
        item["reason"] == "variant source_text is unchanged from its base case"
        for item in result["rejected"]
    )


def test_variant_generation_rejects_nonverbatim_review_evidence() -> None:
    class BadReviewer:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "accepted": True,
                "rationale": "Looks equivalent.",
                "evidence_quote": "invented evidence not in source",
            }

    cases = load_semantic_prompt_cases(CASE_PATH)
    result = generate_semantic_prompt_variants(
        cases,
        generator=_VariantGenerator(),
        reviewer=BadReviewer(),
    )

    assert not result["cases"]
    assert len(result["rejected"]) == 2 * len(cases)
    assert all(
        "exact source_text substring" in item["reason"] for item in result["rejected"]
    )


def test_required_claim_cannot_also_be_forbidden() -> None:
    case = _case(
        "contradictory",
        forbidden_prompt_claims=["Include both label and value."],
    )

    with pytest.raises(SemanticPromptCaseError, match="both requires and forbids"):
        validate_semantic_prompt_cases(
            [case],
            minimum_cases_per_family=1,
            expected_families=frozenset({"joint_vs_alternatives"}),
        )


def test_jsonl_reports_malformed_line_number(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(_case("first")) + "\nnot json\n", encoding="utf-8")

    with pytest.raises(SemanticPromptCaseError, match="line 2"):
        load_semantic_prompt_cases(cases_path)
