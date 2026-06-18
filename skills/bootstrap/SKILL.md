---
name: bootstrap
description: Analyze repository specs and source to identify taxonomy-compliant entities, draft a validated changelog v2 document, and commit it. Use when creating or updating a PR changelog from code, specs, docs, or source-tree analysis and when the work must map entities to the repository taxonomy, validate the YAML, and commit the result.
---

# Bootstrap Changelog

Use this skill to turn repo evidence into a validated `docs/changelogs/PR-<num>-changelog.yaml`.

## Workflow

1. Identify the PR number and repository root.
2. Read the source tree, specs, and any design docs that describe the change.
3. Read `software_development_entity_taxonomy.md` and use only entity types from that file.
4. Draft the changelog in version 2 format.
   - Include `files`, `entities`, `entity_relationships`, `invariants`, and `guidance`.
   - Base every entry on evidence from the repo.
   - Do not invent entities, relationships, or rationale.
5. Validate the draft.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
   - Fix validation issues before continuing.
6. Commit the changelog.
   - Commit only after validation passes.
   - Keep the commit scoped to the changelog unless the user explicitly asked for more.

## Guardrails

- Prefer exact taxonomy types from the repo file.
- If an entity is ambiguous, omit it rather than guessing.
- Keep the changelog scoped to the PR.
- Do not skip validation.
- Do not merge or push unless the user asks.
