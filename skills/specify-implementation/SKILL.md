---
name: specify-implementation
description: Create, fill, and validate implementation specification templates with the repository's implementation-specification CLI or MCP endpoints. Use when Codex needs to define an implementation spec for a known architecture id, scoped to a work item name, keep entity and relationship references constrained to that architecture version, and ensure feature and decision ids are unique.
---

# Specify Implementation

## Overview

Use this skill to produce an implementation specification that stays consistent with a chosen architecture specification.

## Workflow

1. Confirm the source architecture specification exists and has an `id`.
   - If needed, create or update it with `specify-architecture` first.
   - Treat the architecture id as the authoritative version key for the implementation spec.
2. Create the template.
   - Run `powdrr-lift implementation-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_implementation_specification` with the work item name.
   - Use the architecture specification path for the architecture version you are targeting.
   - Use the default file at `docs/specs/<work-item-name>/implementation-specification.yaml` unless the task calls for a different path.
   - Keep the work item name stable for the architecture, system, and implementation specs that belong to the same effort.
3. Fill out the template.
   - Keep `architecture_id` aligned with the source architecture specification.
   - Choose entity ids and relationship ids only from that architecture version.
   - Give each feature a unique id, a description, and functional requirements.
   - Give each decision a unique id and description.
4. Validate the specification.
   - Run `powdrr-lift evaluate-implementation-specification --work-item-name <work-item-name>`.
   - If using MCP, call `validate_implementation_specification` with the work item name.
   - Treat any validation failure as a cue to fix the spec and rerun the validator.
5. Iterate until clean.
   - Fix unknown architecture references first.
   - Then fix duplicate feature or decision ids.
   - Repeat validation until it passes.

## Guardrails

- Do not invent entity ids or relationship ids outside the source architecture specification.
- Do not reuse ids between features and decisions.
- Keep the implementation spec scoped to the architecture version it names.
- If validation exposes a real architecture mismatch, update the source architecture specification and revalidate rather than forcing the implementation spec to fit stale architecture data.
