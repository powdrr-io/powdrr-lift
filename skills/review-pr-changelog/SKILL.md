---
name: review-pr-changelog
description: "Use when reviewing a pull request that should include a changelog YAML. Check for the changelog first, validate it, then review each change against the PR intent and report the feedback."
---

# Review PR Changelog

Use this skill when a PR needs changelog-focused review.

## Workflow

1. Inspect the PR for a changelog YAML.
   - Look for `docs/changelogs/PR-<num>-changelog.yaml`.
   - If it is missing, report that as feedback and stop.
2. Validate the changelog YAML.
   - Run the changelog validation flow used by `powdrr-lift` for that PR.
   - If validation fails, report the validation errors and stop.
3. Review the changelog against the PR intent.
   - Read the PR title and description to determine intent.
   - For each `change` entry, compare the file, summary, affects list, and rationale to the PR intent.
   - Ask whether the change supports the intent.
   - Ask whether the change is strictly necessary to satisfy the intent.
4. Report feedback for every change.
   - Call out any change that does not support the intent.
   - Call out any change that seems unnecessary or over-scoped.
   - Keep the feedback specific to the individual change entry.

## Review Rules

- Do not skip from missing or invalid changelog straight to change-by-change review.
- Treat a missing changelog as a blocking review issue.
- Treat an invalid changelog as a blocking review issue.
- When the changelog is valid, every change entry should receive a judgment.
- Prefer concise, actionable review comments over broad summaries.
