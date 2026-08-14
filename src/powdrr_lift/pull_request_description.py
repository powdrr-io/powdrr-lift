"""Generate the prescribed pull-request description template."""

from __future__ import annotations

from typing import Final

_COMMON_TEMPLATE: Final[str] = """# Pull Request Description

Fill every section below with evidence from the current worktree, diff, skill
context, and validation results. Do not leave placeholders. If a section does
not apply, write `Not applicable` and explain why. Never claim a check passed
unless it actually ran.

## Summary

State what changed and why it matters.

## Problem

State what was wrong, missing, or costly before this change.

## Behavior

### Before

- Describe the relevant previous behavior.

### After

- Describe the resulting behavior.

## Scope

### Included

- List the changes included in this PR.

### Explicitly not included

- List meaningful non-goals.

## Implementation

- Describe the important modules, decisions, and control flow.

### Data, API, schema, or workflow changes

- Describe compatibility or migration impact, or explain why none applies.

### Compatibility considerations

- Describe supported behavior preserved and any breaking implications.

## Validation

| Check | Command | Result |
| --- | --- | --- |
| Focused tests |  |  |
| Full tests |  |  |
| Formatting |  |  |
| Lint |  |  |
| Type checks |  |  |

## Risks and Mitigations

| Risk | Impact | Mitigation or evidence |
| --- | --- | --- |
|  |  |  |

## Reviewer Guide

Please focus on:

- Identify the highest-value review points.

Open questions or decisions needing review:

- None.

## Dependencies and Follow-up

- Depends on:
- Follow-up work:
- Rollback considerations:

## References

- Feature/specification:
- Related PRs/issues:
- Review comments addressed:
"""

_KIND_SECTIONS: Final[dict[str, str]] = {
    "feature": (
        "\n## Feature Plan\n\n"
        "List the feature specification, proposed PRs, execution workflows, "
        "and dependency order.\n"
    ),
    "project-structure": (
        "\n## Project Structure Evidence\n\n"
        "Name the generated artifact and the source evidence used to derive it.\n"
    ),
    "ci-fix": (
        "\n## CI Failure\n\n"
        "Record the failing check, run, command, root cause, local reproduction, "
        "fix, and post-fix validation.\n"
    ),
    "merge-conflict": (
        "\n## Conflict Resolution\n\n"
        "For each conflicted file, record the competing intent, selected "
        "resolution, and validation.\n"
    ),
    "review-comments": (
        "\n## Review Feedback Addressed\n\n"
        "For each actionable comment, record its location, requested outcome, "
        "resolution, and validation.\n"
    ),
}


def render_pull_request_description_template(kind: str = "general") -> str:
    """Return the common template plus the requested workflow-specific section."""
    normalized_kind = kind.strip().lower()
    if normalized_kind not in {"general", *_KIND_SECTIONS}:
        allowed = ", ".join(("general", *_KIND_SECTIONS))
        raise ValueError(f"Unsupported pull-request description kind; use: {allowed}")
    return _COMMON_TEMPLATE + _KIND_SECTIONS.get(normalized_kind, "")
