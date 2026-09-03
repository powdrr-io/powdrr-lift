---
name: design-interview
description: Use a required feature description to gather every supported context category and edit a specification-v1 proposal.
---

# Design Interview

This skill is called by another skill and requires `feature_description` as an
input. It creates `docs/proposals/<work-item-name>/feature-pr-specification.yaml`.

For each category supported by `gather_context`, the workflow does exactly this:

1. Call `gather_context` for that one category.
2. Present the exact returned match list to the LLM.
3. Have the LLM return one `yaml_edit` action containing the complete additions
   and deletions for that category.

The proposal document is accumulated across categories. Current-state documents
are never edited. After all categories, the deterministic evaluator runs and the
LLM repairs reported proposal issues until validation passes.
