---
name: review-pr-changelog
description: "Use during code review when the change includes a changelog. This skill complements general code-review skills; do not replace normal review. Check for the changelog first, validate it, then review each change against the PR intent and report the feedback."
---

# Review PR Changelog

Use this skill when a PR needs changelog-focused review.

## Workflow

1. Inspect the PR for a changelog YAML.
   - Look for `docs/changelogs/PR-<num>-changelog.yaml`.
   - If it is missing, report that as feedback and stop.
2. Validate the changelog YAML.
   - Run the changelog validation flow used by `powdrr-lift` for that PR.
   - Example: `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`
   - If validation fails, report the validation errors and stop.
3. Review the changelog against the PR intent.
   - Read the PR title and description to determine intent.
   - For each `change` entry, compare the file, summary, affects list, and rationale to the PR intent.
   - Use `get_edit_context` for each changed file span to gather the matching provenance and the other code areas edited in the same changelog entry.
   - Use `coedited_files` to identify sibling code areas that should be reviewed alongside the searched span.
   - Ask whether the change supports the intent.
   - Ask whether the change is strictly necessary to satisfy the intent.
4. Check the branch-level invariants and decisions.
   - Call `get_invariants` to retrieve the current invariants.
   - Call `get_current_decisions` to retrieve only decisions that are still current.
   - Use a separate subagent for each invariant to verify it still holds after the PR.
   - Use a separate subagent for each current decision to verify the PR honors it.
   - If the PR introduces a decision that supersedes a previous decision, the new decision must explicitly mark `replaces`; otherwise fail the review.
5. Report feedback for every change.
   - Call out any change that does not support the intent.
   - Call out any change that seems unnecessary or over-scoped.
   - Keep the feedback specific to the individual change entry.

## Review Rules

- Do not skip from missing or invalid changelog straight to change-by-change review.
- Treat a missing changelog as a blocking review issue.
- Treat an invalid changelog as a blocking review issue.
- When the changelog is valid, every change entry should receive a judgment.
- Treat a missing `replaces` on a superseding decision as a blocking review issue.
- Prefer concise, actionable review comments over broad summaries.
