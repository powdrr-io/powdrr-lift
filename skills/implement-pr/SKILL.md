---
name: implement-pr
description: Locate a proposed PR with fuzzy search, validate the proposal, inspect every section, implement the requested changes, and re-check the proposal before optionally preparing the PR changelog.
---

# Implement PR

Use this skill when the task is to carry out a proposed PR from its specification.

## Workflow

1. Find the proposed PR.
   - Run `powdrr-lift search-proposed-prs "<query>"`.
   - If using MCP, call `search_proposed_prs`.
   - Use the search results to identify the best proposed PR.
   - If the match is ambiguous, inspect the top candidates before proceeding.
2. Load the full proposal.
   - Run `powdrr-lift show-proposed-pr <pr-number>`.
   - If using MCP, call `show_proposed_pr`.
   - Read every section of the proposal before making changes.
3. Validate the proposal.
   - Run `powdrr-lift evaluate-pr-specification --input docs/proposals/PR-<num>-proposed-pr-specification.yaml`.
   - If using MCP, call `validate_pr_specification`.
   - Treat validation failure as a blocker until the proposal or the repo state is corrected.
4. Implement the requested changes.
   - Make the smallest code and doc changes that satisfy the proposal.
   - Keep the proposal, implementation, and current repo state aligned.
5. Review the proposal again.
   - Re-read every section after the code changes.
   - Confirm the implementation still covers the proposal intent, acceptance criteria, tests, outcomes, non-goals, and risks.
6. Decide whether to continue or stop.
   - If more user feedback is needed, ask for it.
   - Otherwise continue with the repository's PR changelog workflow
     (`prepare-pr-changelog` in this repo).

## Guardrails

- Do not start coding until the relevant proposed PR is identified and validated.
- Do not assume the first search result is the right one if the query is broad.
- Do not skip the second review pass after implementation.
- Prefer to fix the code or the proposal when they disagree; do not force either side to fit stale context.
