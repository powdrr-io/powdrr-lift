---
name: code-edit-context
description: "Use when you are about to edit code and need index-backed context for a file and line ranges. Ask powdrr-lift for the file and line ranges, inspect prior intent and justification, then decide whether to honor or supersede the earlier work."
---

# Code Edit Context

Use this skill before editing source files.

## Workflow

1. Identify the exact file and line ranges you plan to change.
2. Run `powdrr-lift edit-context --file <path> --range <start:end> --parent-branch <branch>`.
3. Read the returned report:
   - `matching_changes` contains the prior intent and rationale.
   - `requested_ranges` maps each requested line to the provenance record that currently owns it.
4. Before editing, decide whether the existing intent still applies.
   - If it does, preserve it.
   - If it does not, explicitly state that the new work supersedes it.

## Guardrails

- Do not edit blindly.
- Inspect context for every file and line range you plan to touch.
- Prefer honoring prior intent when it is still valid.
