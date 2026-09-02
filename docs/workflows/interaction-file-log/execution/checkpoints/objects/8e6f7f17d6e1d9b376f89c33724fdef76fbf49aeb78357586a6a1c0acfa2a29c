"""Generate the prescribed pull-request description template."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

_COMMON_TEMPLATE: Final[str] = """# Pull Request Description

Fill every section below with evidence from the current worktree, diff, skill
context, and validation results. Do not leave placeholders. If a section does
not apply, write `Not applicable` and explain why. Never claim a check passed
unless it actually ran.

If an existing PR body is included below, treat it as source content that must
be preserved. Carry forward every informative section, validation result,
review decision, reference, and follow-up unless it is explicitly superseded.
Reconcile stale statements with the current diff and append or revise content
without silently deleting history. The completed output must be one coherent
PR description, not a template plus a separate discarded old body.

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


def find_existing_pull_request(
    repo_root: Path | None = None,
) -> tuple[str, str] | None:
    """Return the current branch's PR URL and body when one exists."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "url,body"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Cannot inspect pull requests because gh is not installed"
        ) from None

    if result.returncode != 0:
        error = result.stderr.strip()
        normalized_error = error.lower()
        if (
            "no pull request" in normalized_error
            or "no pull requests" in normalized_error
        ):
            return None
        raise RuntimeError(
            f"Cannot determine whether this branch has a pull request: {error}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            "gh returned invalid JSON while inspecting the pull request"
        ) from None
    url = payload.get("url")
    body = payload.get("body")
    if not isinstance(url, str) or not isinstance(body, str):
        raise RuntimeError("gh returned incomplete pull-request metadata")
    return url, body


def render_pull_request_description_template(
    kind: str = "general",
    *,
    existing_pull_request: tuple[str, str] | None = None,
) -> str:
    """Return a create or update-safe pull-request description template."""
    normalized_kind = kind.strip().lower()
    if normalized_kind not in {"general", *_KIND_SECTIONS}:
        allowed = ", ".join(("general", *_KIND_SECTIONS))
        raise ValueError(f"Unsupported pull-request description kind; use: {allowed}")
    existing_content = ""
    if existing_pull_request is not None:
        url, body = existing_pull_request
        existing_content = (
            "\n## Existing PR Body (preserve and reconcile)\n\n"
            f"Current PR: {url}\n\n"
            "<!-- BEGIN EXISTING PR BODY: retain all informative content -->\n"
            f"{body}\n"
            "<!-- END EXISTING PR BODY -->\n"
        )
    return existing_content + _COMMON_TEMPLATE + _KIND_SECTIONS.get(normalized_kind, "")
