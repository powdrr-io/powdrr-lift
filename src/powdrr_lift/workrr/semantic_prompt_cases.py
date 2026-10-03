"""Load and validate grouped semantic prompt evaluation cases."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SEMANTIC_PROMPT_CASES = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "evaluations"
    / "semantic-prompt-cases-v1.jsonl"
)

SEMANTIC_PROMPT_CASE_FAMILIES = frozenset(
    {
        "joint_vs_alternatives",
        "conditional_modes",
        "scope_and_ownership",
        "lifecycle_mutation_identity",
        "copy_semantics",
        "validation_presence_boundaries",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "case_id",
        "group_id",
        "family",
        "domain",
        "split",
        "source_text",
        "target_proposition",
        "permitted_local_context",
        "expected_decisions",
        "explicitly_unspecified",
        "required_prompt_claims",
        "forbidden_prompt_claims",
    }
)
_HELD_OUT_DOMAINS = frozenset({"configuration_cache"})


class SemanticPromptCaseError(ValueError):
    """A semantic prompt case set is malformed or leaks across splits."""


def load_semantic_prompt_cases(path: Path) -> list[dict[str, Any]]:
    """Read JSONL cases and reject malformed records with line references."""
    cases: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SemanticPromptCaseError(f"could not read case file: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise SemanticPromptCaseError(
                f"invalid JSON on line {line_number}: {error.msg}"
            ) from error
        if not isinstance(raw, dict):
            raise SemanticPromptCaseError(
                f"case on line {line_number} must be a JSON object"
            )
        cases.append(raw)
    validate_semantic_prompt_cases(cases)
    return cases


def validate_semantic_prompt_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    minimum_cases_per_family: int = 10,
    expected_families: frozenset[str] = SEMANTIC_PROMPT_CASE_FAMILIES,
) -> dict[str, Any]:
    """Check case schemas, family/domain coverage, and group-split boundaries."""
    if not cases:
        raise SemanticPromptCaseError("case set is empty")
    ids: set[str] = set()
    group_splits: dict[str, str] = {}
    domain_splits: dict[str, str] = {}
    family_counts: Counter[str] = Counter()
    family_domains: dict[str, set[str]] = defaultdict(set)
    split_counts: Counter[str] = Counter()

    for index, case in enumerate(cases, start=1):
        missing = _REQUIRED_FIELDS - set(case)
        if missing:
            raise SemanticPromptCaseError(
                f"case {index} is missing fields: {', '.join(sorted(missing))}"
            )
        case_id = _nonempty_text(case, "case_id")
        group_id = _nonempty_text(case, "group_id")
        family = _nonempty_text(case, "family")
        domain = _nonempty_text(case, "domain")
        split = _nonempty_text(case, "split")
        if case_id in ids:
            raise SemanticPromptCaseError(f"duplicate case_id: {case_id}")
        ids.add(case_id)
        if family not in expected_families:
            raise SemanticPromptCaseError(
                f"case {case_id} has unknown family: {family}"
            )
        if split not in {"development", "held_out"}:
            raise SemanticPromptCaseError(
                f"case {case_id} has unsupported split: {split}"
            )
        previous_split = group_splits.setdefault(group_id, split)
        if previous_split != split:
            raise SemanticPromptCaseError(
                f"case group {group_id} leaks across {previous_split} and {split}"
            )
        previous_domain_split = domain_splits.setdefault(domain, split)
        if previous_domain_split != split:
            raise SemanticPromptCaseError(f"domain {domain} appears in multiple splits")
        expected_split = "held_out" if domain in _HELD_OUT_DOMAINS else "development"
        if split != expected_split:
            raise SemanticPromptCaseError(
                f"domain {domain} must remain in the {expected_split} split"
            )

        _nonempty_text(case, "source_text")
        _nonempty_text(case, "target_proposition")
        if (
            not isinstance(case.get("expected_decisions"), Mapping)
            or not case["expected_decisions"]
        ):
            raise SemanticPromptCaseError(
                f"case {case_id} has no expected_decisions object"
            )
        _text_list(case, "permitted_local_context", allow_empty=True)
        _text_list(case, "explicitly_unspecified", allow_empty=True)
        required = _text_list(case, "required_prompt_claims")
        forbidden = _text_list(case, "forbidden_prompt_claims")
        overlap = set(required) & set(forbidden)
        if overlap:
            raise SemanticPromptCaseError(
                f"case {case_id} both requires and forbids: "
                f"{', '.join(sorted(overlap))}"
            )

        family_counts[family] += 1
        family_domains[family].add(domain)
        split_counts[split] += 1

        variant_of = case.get("variant_of")
        if variant_of is not None:
            if not isinstance(variant_of, str) or not variant_of.strip():
                raise SemanticPromptCaseError(
                    f"case {case_id} has an invalid variant_of reference"
                )

    _validate_variant_parents(cases, group_splits)
    if set(family_counts) != expected_families:
        missing_families = expected_families - set(family_counts)
        raise SemanticPromptCaseError(
            "missing case families: " + ", ".join(sorted(missing_families))
        )
    for family in sorted(expected_families):
        count = family_counts[family]
        if count < minimum_cases_per_family:
            raise SemanticPromptCaseError(
                f"family {family} has {count} cases; "
                f"requires at least {minimum_cases_per_family}"
            )
        if len(family_domains[family]) < 3:
            raise SemanticPromptCaseError(
                f"family {family} must cover at least three domains"
            )

    return {
        "case_count": len(cases),
        "family_counts": dict(sorted(family_counts.items())),
        "family_domains": {
            family: sorted(domains)
            for family, domains in sorted(family_domains.items())
        },
        "split_counts": dict(sorted(split_counts.items())),
        "held_out_domains": sorted(
            domain for domain, split in domain_splits.items() if split == "held_out"
        ),
    }


def _nonempty_text(case: Mapping[str, Any], field: str) -> str:
    value = case.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SemanticPromptCaseError(f"{field} must be a non-empty string")
    return value


def _text_list(
    case: Mapping[str, Any], field: str, *, allow_empty: bool = False
) -> list[str]:
    value = case.get(field)
    if not isinstance(value, list) or (not value and not allow_empty):
        raise SemanticPromptCaseError(f"{field} must be a non-empty string array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SemanticPromptCaseError(f"{field} must contain non-empty strings")
    return value


def _validate_variant_parents(
    cases: Sequence[Mapping[str, Any]], group_splits: Mapping[str, str]
) -> None:
    by_id = {
        case["case_id"]: case for case in cases if isinstance(case.get("case_id"), str)
    }
    for case in cases:
        variant_of = case.get("variant_of")
        if variant_of is None:
            continue
        case_id = str(case["case_id"])
        parent = by_id.get(variant_of)
        if parent is None:
            raise SemanticPromptCaseError(
                f"case {case_id} refers to missing base case {variant_of}"
            )
        if parent.get("variant_of") is not None:
            raise SemanticPromptCaseError(
                f"case {case_id} must refer directly to a base case"
            )
        if case.get("group_id") != parent.get("group_id"):
            raise SemanticPromptCaseError(
                f"variant {case_id} does not share its base case group"
            )
        for field in (
            "family",
            "domain",
            "split",
            "target_proposition",
            "expected_decisions",
            "explicitly_unspecified",
            "required_prompt_claims",
            "forbidden_prompt_claims",
        ):
            if case.get(field) != parent.get(field):
                raise SemanticPromptCaseError(
                    f"variant {case_id} changes its base case {field}"
                )
        if group_splits.get(str(case.get("group_id"))) != parent.get("split"):
            raise SemanticPromptCaseError(
                f"variant {case_id} does not share its base case split"
            )


if __name__ == "__main__":
    cases = load_semantic_prompt_cases(DEFAULT_SEMANTIC_PROMPT_CASES)
    summary = validate_semantic_prompt_cases(cases)
    print(json.dumps(summary, indent=2, sort_keys=True))
