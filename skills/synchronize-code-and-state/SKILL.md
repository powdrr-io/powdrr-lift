---
name: synchronize-code-and-state
description: Generate the current codebase-state snapshot, compare it to the source tree and changelog index, and reconcile mismatches by changing code and/or the changelog while preserving the repo's intent. Use when Codex needs to align actual code with the indexed state after a PR, merged change, or state drift.
---

# Synchronize Code And State

## Overview

Use this skill to reconcile the generated codebase state with the repository's actual code and changelog history. The goal is agreement that best preserves the repo's intent, decisions, guidance, and invariants.

## Workflow

1. Generate the current codebase state.
   - Run `powdrr-lift codebase-state`.
   - If the CLI is unavailable, use the MCP tool `get_codebase_state`.
   - Treat the generated state file as the working artifact for reconciliation.
2. Compare the snapshot to the repository source.
   - Read entities, relationships, invariants, guidance, decisions, and intents together.
   - Compare them against the actual code, docs, and changelog index.
   - Identify stale state, missing state, contradictions, and overly broad state.
3. Decide what should change.
   - Change the code when the indexed state reflects the desired intent but the implementation does not.
   - Change the changelog when the code is correct but the indexed state is stale or incomplete.
   - Change both only when that is the clearest way to preserve the overall intent.
4. Reconcile the mismatch.
   - Make the smallest coherent set of code edits and create or update the PR changelog.
   - Use best judgment to honor the repository's intent, decisions, guidance, and invariants.
   - Do not force the code to match the snapshot or the snapshot to match the code if doing so would violate intent.
5. Validate the result.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
   - Run the relevant formatter, linter, and tests for the touched code.
   - Regenerate the codebase state if the change affects the snapshot.
6. Commit the reconciliation.
   - Keep the changelog at `docs/changelogs/PR-<num>-changelog.yaml`.
   - Keep the change set scoped to the reconciliation unless the user asks for more.

## Guardrails

- Do not treat the generated state as more authoritative than the code.
- Do not treat the code as more authoritative than the generated state.
- Prefer evidence from the source tree and changelog history over memory.
- Resolve disagreements explicitly instead of papering over them.
