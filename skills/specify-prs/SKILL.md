---
name: specify-prs
description: Create, fill, and validate proposed PR specification templates with the repository's pr-specification CLI or MCP endpoints. Use when Codex needs to describe a proposed PR with feature references, intent, reasoning, and optional detail sections, scoped to a work item name, then validate that the PR id is unique and referenced features still exist.
---

# Specify PRs

## Overview

Use this skill to draft a PR specification that ties a proposed PR to current feature ids from the codebase state and repository files.

## Workflow

1. Create the template.
   - Run `powdrr-lift pr-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_pr_specification` with the work item name.
   - Create one template per proposed PR.
   - Use the default file at `docs/specs/<work-item-name>/proposed-pr-specification.yaml` unless the task calls for a different path.
   - Keep the work item name stable for the proposed PR spec and the implementation work it describes.
2. Fill out the template.
   - Set `id` to a globally unique proposed PR id.
   - Reference one or more current feature ids from the current codebase state.
   - Put the PR goal and reasoning in `intent.goal` and `intent.reasoning`.
   - Add the optional detail lists when they help clarify the work.
3. Validate the specification.
   - Run `powdrr-lift evaluate-pr-specification --work-item-name <work-item-name>`.
   - If using MCP, call `validate_pr_specification` with the work item name.
   - Treat any validation failure as a cue to fix the template and rerun.
4. Iterate until clean.
   - Fix duplicate PR ids first.
   - Then fix unknown feature ids.
   - Then fix missing intent fields or missing detail sections.
   - Repeat validation until it passes.

## Guardrails

- Do not invent feature ids.
- Do not reuse a proposed PR id that already exists in the repository.
- Prefer the smallest PR spec that captures the intent.
- If validation reveals a real code or spec mismatch, update the code or the source specs and revalidate rather than forcing the PR spec to fit stale data.
