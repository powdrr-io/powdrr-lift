---
name: plan-and-implement-feature
description: Plan a feature from the codebase, generate the system-map and feature+PR templates, implement the change, and prepare the changelog before declaring the work done.
---

# Plan And Implement Feature

## Workflow

1. Build the system map.
   - Run `powdrr-lift system-map-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_system_map_specification`.
   - Analyze the full codebase deeply before filling anything in.
   - Fill the sections one at a time, double-check each section, and remove the instructions when done.
2. Build the feature and PR template.
   - Run `powdrr-lift feature-pr-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_feature_pr_specification`.
   - Use the completed system map and the requested feature to fill each section with the required changes, validation conditions, and outcomes.
   - Double-check that nothing needed to implement the feature is missing.
3. Plan and execute the code changes.
   - Use the feature+PR template as the source of truth for the implementation plan.
   - Make the smallest coherent code changes that satisfy the plan.
   - Run the relevant tests and fix any failures before moving on.
4. Prepare the PR changelog.
   - Run `powdrr-lift init --pr-number <num>`.
   - If using MCP, use the changelog template endpoint available in this repo.
   - Fill out the changelog from the implemented code changes.
5. Generate the plan diff.
   - Run `powdrr-lift plan-diff --feature-plan-specification <path> --changelog <path>`.
   - If using MCP, call `create_plan_diff_specification`.
   - Compare the filled feature plan specification against the changelog files.
   - Treat each difference as a single actionable item and work through them one at a time.
6. Examine the plan diff and make code-only changes.
   - Use the plan diff as the source of truth for closing the gaps between the plan and the implemented change.
   - Make changes only to code. Do not revise the plan, the feature template, or the changelog to hide a mismatch.
   - When there are gaps, delete the old changelog and the diff file, regenerate the changelog template, follow its instructions, regenerate the plan diff, and repeat until the diff has no entries.
7. Compare and validate.
   - Compare the changelog against the feature plan template and ensure every required change, validation condition, and outcome is reflected.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
8. Finish only when the feature, changelog, and validation all agree.

## Guardrails

- Do not skip the system map step.
- Do not skip the feature+PR template step.
- Do not skip the plan diff step.
- Do not mark the work done until the changelog validates.
- Do not mark the work done until the plan diff is empty.
- Prefer the smallest change set that fully satisfies the requested feature.
