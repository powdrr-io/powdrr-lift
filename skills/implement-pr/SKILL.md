---
name: implement-pr
description: Find a proposed PR by fuzzy search, inspect the full proposal, validate it against the current indexed specs and changelogs, implement the requested changes, review the proposal again, and then optionally generate the matching PR changelog. Use when Codex needs to carry out a proposed PR from its specification document.
---

# Implement PR

## Overview

Use this skill to turn a proposed PR specification into code changes that still line up with the repo's current indexed state.

## Workflow

1. Find the proposed PR.
   - Run `powdrr-lift search-proposed-prs <query>`.
   - If using MCP, call `search_proposed_prs`.
   - Review the fuzzy matches and pick the intended proposed PR id.
2. Inspect the proposal.
   - Run `powdrr-lift show-proposed-pr <proposed-pr-id>`.
   - If using MCP, call `show_proposed_pr`.
   - Read every section: `feature_ids`, `intent`, `acceptance_criteria`, `expected_tests`, `expected_outcomes`, `non_goals`, and `risks`.
3. Validate the proposal.
   - Run `powdrr-lift evaluate-pr-specification --work-item-name <work-item-name>`.
   - If using MCP, call `validate_pr_specification`.
   - Fix any validation problems before continuing.
4. Implement the requested changes.
   - Make the smallest code changes that satisfy the proposal.
   - Keep the proposal's intent, acceptance criteria, and risks in view while editing.
5. Review the proposal again.
   - Re-read every section after the implementation to confirm the code matches the proposal.
   - If the code diverges from the proposal, reconcile it before moving on.
6. Decide whether to continue.
   - If more feedback is needed, ask the user.
   - Otherwise, generate or refresh the PR changelog for the branch.

## Guardrails

- Use the proposal as the source of intent, not as a license to widen scope.
- Prefer current indexed feature ids only.
- Keep the implementation aligned with the proposal's acceptance criteria and expected tests.
- If the proposal is stale, update the proposal or the source specs first, then revalidate.
