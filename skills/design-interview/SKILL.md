---
name: design-interview
description: Interview a feature against every gather_context category, apply the resulting edits to a specification-v1 proposal, and validate it.
---

# Design Interview

The feature description is an explicit input to this skill. The workflow first
creates a feature/PR specification-v1 proposal, then processes every category
supported by `gather_context` independently.

For each category it performs this loop:

1. Call `gather_context` with exactly that category.
2. Treat the returned match list as the current set and present it to the LLM.
3. Have the LLM return one complete set of `yaml_edit` operations for the
   proposal document based on the feature description and gathered list.

The proposal is written to
`docs/proposals/<work-item-name>/feature-pr-specification.yaml`. Current-state
documents are read-only. Existing proposal edits are preserved as later
categories are processed.

After all categories are complete, the workflow runs the deterministic evaluator
and repairs every reported issue until the proposal validates successfully.
