---
name: specify-prs
description: Create, fill, and validate proposed PR specification templates with the repository's pr-specification CLI or MCP endpoints. Use when Codex needs to describe a proposed PR with feature references, intent, reasoning, and optional file updates, then validate that the PR id is unique and referenced features/files exist.
---

# Specify PRs

## Overview

Use this skill to draft a PR specification that ties a proposed PR to current feature ids from the codebase state and repository files.

## Workflow

1. Create the template.
   - Run `powdrr-lift pr-specification`.
   - If using MCP, call `create_pr_specification`.
   - Create one template per proposed PR.
   - Use the default file at `docs/prs/proposed-pr-specification.yaml` unless the task calls for a different path.
2. Fill out the template.
   - Set `id` to a globally unique proposed PR id.
   - Reference one or more current feature ids from the current codebase state.
   - Put the PR goal and reasoning in `intent.goal` and `intent.reasoning`.
   - Fill in `acceptance_criteria`, `expected_tests`, `expected_outcomes`,
     `non_goals`, and `risks` with concrete `id` and `description` pairs.
   - Keep the proposed PR self-contained; use the section lists to describe
     what changes, not a file list.
3. Validate the specification.
   - Run `powdrr-lift evaluate-pr-specification`.
   - If using MCP, call `validate_pr_specification`.
   - Treat any validation failure as a cue to fix the template and rerun.
4. Iterate until clean.
   - Fix duplicate PR ids first.
   - Then fix unknown feature ids.
   - Then fix missing files or missing intent fields.
   - Repeat validation until it passes.

## Guardrails

- Do not invent feature ids.
- Do not reuse a proposed PR id that already exists in the repository.
- Prefer the smallest PR spec that captures the intent.
- If validation reveals a real code or spec mismatch, update the code or the source specs and revalidate rather than forcing the PR spec to fit stale data.
