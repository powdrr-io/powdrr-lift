---
name: prepare-pr-changelog
description: "Use when preparing a pull request or getting a PR ready. Guides the agent through the PR changelog workflow with powdrr-lift."
---

# Prepare PR Changelog

Use the `powdrr-lift` CLI for PR preparation. Follow the workflow in order.

## Workflow

1. Resolve the PR number.
   - Use the current PR number if it is already known.
   - Otherwise determine it before continuing.
2. Generate the changelog template.
   - Run `powdrr-lift init --pr-number <num>`.
   - The template should be written to `docs/changelogs/PR-<num>-changelog.yaml`.
3. Fill out the template.
   - Follow the inline instructions in the generated file.
   - Keep the YAML valid.
   - Replace `null` values with concrete content when known.
4. Validate the filled-out file.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
   - Fix any validation issues before moving on.
5. Include the final file in the PR.
   - Keep the file at `docs/changelogs/PR-<num>-changelog.yaml`.
   - Do not treat the PR as ready until validation passes.

## Guardrails

- Prefer the CLI over manual steps.
- Do not skip validation.
- Keep the changelog path and filename exactly as specified.
