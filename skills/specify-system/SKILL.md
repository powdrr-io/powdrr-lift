---
name: specify-system
description: Create, fill, and validate system specification templates with the repository's system-specification CLI or MCP endpoints. Use when Codex needs to draft a system description with requirements and approach items, scoped to a work item name, enforce state-driven supersedence rules, and iterate until the specification validates.
---

# Specify System

## Overview

Use this skill to draft a system specification, reconcile it with the repo's rules, and validate it before committing it into the repository.

## Workflow

1. Create the template.
   - Run `powdrr-lift system-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_system_specification` with the work item name.
   - Use the default file at `docs/specs/<work-item-name>/system-specification.yaml` unless the task calls for a different path.
   - Keep the work item name stable for the architecture, system, and implementation specs that belong to the same effort.
2. Fill out the template.
   - Put the system's requirements in `requirements` and the implementation direction in `approach`.
   - Give every item a unique `id`.
   - Use `state: added` for new items and include a description.
   - Use `state: removed` for retired items and leave description empty.
   - Use `state: supercedes` when an item replaces same-section ids.
   - Keep every `supercedes` reference inside the same section.
3. Validate the specification.
   - Run `powdrr-lift evaluate-system-specification --work-item-name <work-item-name>`.
   - If using MCP, call `validate_system_specification` with the work item name.
   - Treat any validation failure as a cue to fix the spec and rerun the validator.
4. Iterate until clean.
   - Fix missing ids and invalid states first.
   - Then fix description rules and same-section supersedence references.
   - Repeat validation until it passes.

## Guardrails

- Do not invent states outside `added`, `removed`, and `supercedes`.
- Do not point `supercedes` at ids from the other section.
- Keep ids unique across the full document.
- If the spec reveals a real mismatch with the repo's intended system, update the system spec and revalidate rather than forcing the document to fit stale assumptions.
