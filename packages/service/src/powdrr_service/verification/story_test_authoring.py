"""Compile pinned story cases into the existing execute-PR author contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from powdrr_service.planning.story_allocation import parse_story_refs
from powdrr_service.structrr.story_coverage import (
    StoryCoverageError,
    declared_case_coverage,
    expand_story_cases,
    story_case_obligations,
)
from powdrr_service.structrr.user_stories import (
    case_digest,
    parse_story_catalog,
    story_contract_digest,
)


def prepare_story_test_obligations(proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = parse_story_refs(proposed.get("story_refs", []))
    if not refs:
        return []
    context = proposed.get("story_context")
    if not isinstance(context, Mapping):
        raise StoryCoverageError(
            "story test author requires the complete pinned story context"
        )
    catalog = parse_story_catalog(context)
    current_case_digests = {
        case.id: case_digest(catalog, case.id) for case in catalog.user_story_cases
    }
    for ref in refs:
        if ref.story_id not in catalog.stories_by_id:
            raise StoryCoverageError(f"{ref.story_id}: author context lost the story")
        if ref.contract_digest != story_contract_digest(catalog, ref.story_id):
            raise StoryCoverageError(
                f"{ref.story_id}: author context has a stale story contract"
            )
        if any(
            current_case_digests.get(identity) != digest
            for identity, digest in ref.case_digests
        ):
            raise StoryCoverageError(
                f"{ref.story_id}: author context has stale case fixtures"
            )
    # Implementation slices keep their normal AC contracts. Full story proof is
    # allocated to the designated integration gate, never every partial PR.
    if proposed.get("story_validation") is not True:
        return []
    result = []
    for ref in refs:
        missing = declared_case_coverage(catalog, ref.story_id)
        if missing:
            expansion = expand_story_cases(catalog, ref.story_id)
            raise StoryCoverageError(
                "Story cases require refinement and a normal Structrr proposal "
                "before test authoring: "
                + json.dumps(
                    {
                        "story_id": ref.story_id,
                        "missing_declared_targets": missing,
                        "expansion": expansion.to_data(),
                        "route": "story-design-interview",
                    },
                    sort_keys=True,
                )
            )
        result.extend(story_case_obligations(catalog, ref.case_ids))
    if not result:
        raise StoryCoverageError("full-story validation has no pinned execution cases")
    return result


def require_supported_story_author(
    profile_command: list[str] | tuple[str, ...],
    story_obligations: list[dict[str, Any]],
) -> None:
    if story_obligations and not any(
        token == "pytest" or token.endswith("/pytest") for token in profile_command
    ):
        raise StoryCoverageError(
            "unsupported_story_test_author: this adapter requires the repository's "
            "pytest profile; route other frameworks through their supported adapter"
        )
