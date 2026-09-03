---
name: design-interview
description: Walk a feature through the complete specification-v1 entity taxonomy and relationship model, then produce and validate a feature/PR proposal document.
---

# Design Interview

Use this skill when a feature needs an explicit inventory of what it adds,
depends on, and removes. The interview proceeds through every entity-type group
in `software_development_entity_taxonomy.md`, followed by a relationship pass.

At each entity stage, answer this question for every type in that taxonomy group:

> This is the current set of `<entity_type>`. To support all the outcomes for
> `<this_feature>`, create a list of all the new entities of this type that we
> need, all the entities we will depend on, and all the entities that should be
> deleted.

Keep additions and dependencies separate. Use exact existing ids for dependencies
and deletions. Use an empty list when a type is irrelevant; do not manufacture
entities merely to fill the list. Record the evidence or reasoning supporting
each non-empty decision.

After the entity stages, identify relationships that must be added, updated, or
deleted. Then synthesize the interview into
`docs/proposals/<work-item-name>/feature-pr-specification.yaml` using the normal
specification-v1 format. The proposal is the only document edited by this skill;
current-state documents remain unchanged.

Finally, run the deterministic evaluator and repair every reported issue until
the proposal passes. The evaluator, not the interview model, is authoritative on
schema shape, ids, states, and references.
