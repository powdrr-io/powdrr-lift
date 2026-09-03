---
name: design-interview
description: Use required feature inputs to gather every supported context category, store LLM add/remove decisions, and apply them to a specification-v1 proposal.
---

# Design Interview

This skill is called by another skill and requires `work_item_name` and
`feature_description` inputs.

For every category supported by `gather_context`, the workflow performs this
sequence:

1. Call `gather_context` with exactly that category.
2. Examine and present the exact returned list to the LLM.
3. Store the LLM's JSON output with exactly `added` and `deleted` lists.

The interview outputs are passed into the specification-v1 template generator.
Only after the template is generated does a later step apply those outputs to
`docs/proposals/<work-item-name>/feature-pr-specification.yaml`. Current-state
documents are never edited.

The deterministic evaluator then validates the proposal, and the repair loop
fixes every reported issue before completion.
