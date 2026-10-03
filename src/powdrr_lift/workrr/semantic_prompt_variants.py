"""Generate and semantically review grouped evaluation case variants."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from powdrr_lift.workrr.semantic_prompt_cases import (
    SemanticPromptCaseError,
    validate_semantic_prompt_cases,
)


class JsonCompletionClient(Protocol):
    """Minimal provider-neutral interface used by variant generation and review."""

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


def generate_semantic_prompt_variants(
    base_cases: Sequence[Mapping[str, Any]],
    *,
    generator: JsonCompletionClient,
    reviewer: JsonCompletionClient,
) -> dict[str, Any]:
    """Generate paraphrases and contrasts; include only reviewer-approved cases.

    The generator may propose wording only. Gold labels for contrast cases are
    authored independently by the reviewer from the changed source statement.
    Invalid or rejected proposals are retained in ``rejected`` for audit.
    """
    validate_semantic_prompt_cases(list(base_cases))
    generated: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen_ids = {str(case["case_id"]) for case in base_cases}

    for base in base_cases:
        proposals = _propose(generator, base)
        if not isinstance(proposals, list):
            raise SemanticPromptCaseError(
                f"generator output for {base['case_id']} must contain a variants array"
            )
        for proposal_index, proposal in enumerate(proposals, start=1):
            if not isinstance(proposal, Mapping):
                rejected.append(
                    {
                        "case_id": str(base["case_id"]),
                        "reason": "proposal is not an object",
                    }
                )
                continue
            kind = proposal.get("kind")
            if kind not in {"paraphrase", "contrast"}:
                rejected.append(
                    {"case_id": str(base["case_id"]), "reason": "unknown variant kind"}
                )
                continue
            source_text = proposal.get("source_text")
            if not isinstance(source_text, str) or not source_text.strip():
                rejected.append(
                    {"case_id": str(base["case_id"]), "reason": "missing source_text"}
                )
                continue

            candidate_id = _candidate_id(base, kind, proposal_index)
            if candidate_id in seen_ids:
                raise SemanticPromptCaseError(
                    f"generated duplicate case_id: {candidate_id}"
                )
            seen_ids.add(candidate_id)
            review = _review(reviewer, base, proposal, kind)
            reason = _review_rejection(review, base, proposal, kind)
            if reason:
                rejected.append({"case_id": candidate_id, "reason": reason})
                continue

            case = {
                key: base[key]
                for key in (
                    "group_id",
                    "family",
                    "domain",
                    "split",
                    "permitted_local_context",
                )
            }
            case.update(
                {
                    "case_id": candidate_id,
                    "source_text": source_text.strip(),
                    "target_proposition": str(
                        proposal.get("target_proposition", source_text)
                    ).strip(),
                    "expected_decisions": dict(base["expected_decisions"])
                    if kind == "paraphrase"
                    else review["expected_decisions"],
                    "explicitly_unspecified": list(base["explicitly_unspecified"])
                    if kind == "paraphrase"
                    else review["explicitly_unspecified"],
                    "required_prompt_claims": list(base["required_prompt_claims"])
                    if kind == "paraphrase"
                    else review["required_prompt_claims"],
                    "forbidden_prompt_claims": list(base["forbidden_prompt_claims"])
                    if kind == "paraphrase"
                    else review["forbidden_prompt_claims"],
                }
            )
            case["variant_of" if kind == "paraphrase" else "contrast_of"] = str(
                base["case_id"]
            )
            case["variant_review"] = {
                "reviewer_approved": True,
                "rationale": str(review["rationale"]).strip(),
                "evidence_quote": str(review["evidence_quote"]).strip(),
            }
            generated.append(case)

    combined = [dict(case) for case in base_cases] + generated
    validate_semantic_prompt_cases(combined)
    return {"cases": generated, "rejected": rejected}


def _propose(client: JsonCompletionClient, base: Mapping[str, Any]) -> Any:
    messages = [
        {
            "role": "system",
            "content": (
                "Create two candidate variants of the evaluation case: "
                "one faithful paraphrase and one minimal contrast that changes one "
                "semantic decision. Return JSON with a variants array. Do not provide "
                "gold labels or change the domain. Keep wording natural and concise."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "case": base,
                    "required_candidate_fields": [
                        "kind",
                        "source_text",
                        "target_proposition",
                    ],
                },
                sort_keys=True,
            ),
        },
    ]
    return client.complete_json(messages).get("variants")


def _review(
    client: JsonCompletionClient,
    base: Mapping[str, Any],
    proposal: Mapping[str, Any],
    kind: str,
) -> dict[str, Any]:
    required = ["accepted", "rationale", "evidence_quote"]
    if kind == "contrast":
        required.extend(
            [
                "expected_decisions",
                "explicitly_unspecified",
                "required_prompt_claims",
                "forbidden_prompt_claims",
            ]
        )
    output = client.complete_json(
        [
            {
                "role": "system",
                "content": (
                    "Independently validate a candidate against its base. Accept a "
                    "paraphrase only if it preserves meaning, scope, and uncertainty. "
                    "Accept a contrast only if one minimal source change supports a "
                    "different gold decision. Use candidate source text "
                    "and permitted context as evidence. Return JSON with fields: "
                    + ", ".join(required)
                    + ". evidence_quote must exactly match candidate source_text. "
                    "For contrasts, provide complete gold fields based on the source; "
                    "do not copy labels or claims unless still entailed."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"kind": kind, "base": base, "candidate": proposal},
                    sort_keys=True,
                ),
            },
        ]
    )
    return output


def _review_rejection(
    review: Mapping[str, Any],
    base: Mapping[str, Any],
    proposal: Mapping[str, Any],
    kind: str,
) -> str | None:
    if review.get("accepted") is not True:
        return str(review.get("rationale") or "reviewer rejected candidate")
    quote = review.get("evidence_quote")
    source_text = proposal.get("source_text")
    if not isinstance(quote, str) or not quote.strip() or quote not in str(source_text):
        return "review evidence_quote is not an exact source_text substring"
    if not isinstance(review.get("rationale"), str) or not review["rationale"].strip():
        return "review is missing a rationale"
    if kind == "paraphrase":
        if _normalize(str(proposal.get("target_proposition", ""))) != _normalize(
            str(base["target_proposition"])
        ):
            return "paraphrase changes target_proposition"
    else:
        if (
            not isinstance(review.get("expected_decisions"), Mapping)
            or not review["expected_decisions"]
        ):
            return "contrast is missing expected_decisions"
        for field in (
            "explicitly_unspecified",
            "required_prompt_claims",
            "forbidden_prompt_claims",
        ):
            value = review.get(field)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return f"contrast has invalid {field}"
        if review["expected_decisions"] == base["expected_decisions"]:
            return "contrast does not change a gold decision"
        if set(review["required_prompt_claims"]) & set(
            review["forbidden_prompt_claims"]
        ):
            return "contrast has contradictory required and forbidden claims"
    return None


def _candidate_id(base: Mapping[str, Any], kind: str, index: int) -> str:
    suffix = "para" if kind == "paraphrase" else "contrast"
    safe_id = re.sub(r"[^a-zA-Z0-9-]+", "-", str(base["case_id"])).strip("-")
    return f"{safe_id}-{suffix}-{index:02d}"


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
