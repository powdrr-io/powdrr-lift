---
name: bootstrap
description: Analyze repository specs and source to identify taxonomy-compliant entities, draft a validated changelog v2 document, and prepare it in a pull request. Use when creating or updating a PR changelog from code, specs, docs, or source-tree analysis and when the work must map entities to the repository taxonomy, validate the YAML, and publish the result for review.
---

# Bootstrap Changelog

Use this skill to turn repo evidence into a validated `docs/changelogs/PR-<num>-changelog.yaml`.

## Workflow

1. Identify the repository root and inspect the current branch for an existing pull request.
   - If a pull request already exists, reuse its number and URL.
   - If one does not exist, push the current feature branch and create a draft pull
     request with a concise evidence-based title and summary. Record its number
     and URL before generating the changelog, because the number is part of the
     changelog filename.
2. Read the source tree, specs, and any design docs that describe the change.
3. Read `software_development_entity_taxonomy.md` and use only entity types from that file.
4. Generate the changelog template first.
   - Run `powdrr-lift init --pr-number <num>`.
   - Use the generated `docs/changelogs/PR-<num>-changelog.yaml` as the starting point.
   - Do not hand-write the file from scratch unless the template generator is unavailable.
5. Draft the changelog in version 2 format.
   - Include `files`, `entities`, `entity_relationships`, `invariants`, and `guidance`.
   - Base every entry on evidence from the repo.
   - Do not invent entities, relationships, or rationale.
6. Validate the draft.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
   - Fix validation issues before continuing.
7. Commit and publish the changelog.
   - Commit only after validation passes.
   - Keep the commit scoped to the changelog unless the user explicitly asked for more.
   - Push the commit to the pull request branch and verify that the pull request
     contains the validated changelog.
8. Report the pull request URL, changelog path, commit, and validation command.

## Guardrails

- Prefer exact taxonomy types from the repo file.
- If an entity is ambiguous, omit it rather than guessing.
- Keep the changelog scoped to the PR.
- Do not skip validation.
- Never merge the pull request; leave it open for user review.
- Do not push to `main` or another protected branch.
