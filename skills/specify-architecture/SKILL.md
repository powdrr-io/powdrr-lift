---
name: specify-architecture
description: Create, fill, and validate architecture specification templates with the repository's architecture-specification CLI or MCP endpoints. Use when Codex needs to define an architecture spec from a provided set of entity types, ensure entity types are allowed, and verify that relationship, invariant, and guidance references point to listed entities.
---

# Specify Architecture

## Overview

Use this skill to produce an architecture specification that stays consistent with the repository's allowed entity types and explicit references.

## Workflow

1. Create the template.
   - Run `powdrr-lift architecture-specification --entity-type <type> ...`.
   - If using MCP, call `create_architecture_specification` with the same
     allowed entity type list.
   - Provide the full allowed entity type set every time you generate the template.
   - Use the default file at `docs/architecture/architecture-specification.yaml` unless the task calls for a different path.
2. Fill out the template.
   - Keep each entity's `type` within the provided entity type set.
   - Put entity-to-entity links in `entity_relationships`.
   - Use `related.entities` and `related.entity_relationships` in `invariants` and `guidance` whenever those items refer to specific entities or relationships.
   - Keep every entity mentioned anywhere in the spec listed in `entities`.
3. Validate the specification.
   - Run `powdrr-lift evaluate-architecture-specification --entity-type <type> ...`.
   - If using MCP, call `validate_architecture_specification` with the same
     allowed entity type list.
   - Use the same allowed entity type set you used when creating the template.
   - Treat any validation failure as a cue to fix the spec and rerun the validator.
4. Iterate until clean.
   - Fix unknown entity types first.
   - Then fix missing entity references in relationships, invariants, and guidance.
   - Repeat validation until it passes.

## Guardrails

- Do not invent entity types outside the provided set.
- Do not leave relationship, invariant, or guidance references pointing at entities that are not listed.
- Prefer the smallest spec change that preserves the intended architecture.
- If the spec reveals a real code mismatch, update the code and revalidate the spec rather than forcing the spec to fit stale code.
