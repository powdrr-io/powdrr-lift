# Objective

Add composite trait aspects with unified entity, query and subscription operations. Trait groups lack unified operations, forcing manual listing and merging across systems.

# Requirements

1. The core exports a new `createAspect` which accepts two or more traits and returns an aspect.
2. Overlapping field names between constituents throw at creation time.
3. Relation constituents throw at creation time.
4. Tag traits are valid constituents.
5. Nested aspects flatten to their individual traits.
6. Each aspect exposes `id`, `traits`, and `schema`.
7. `has` returns true when the entity has every constituent trait.
8. `get` returns a merged object of all constituent fields, or undefined if any constituent is missing.
9. `set` distributes each field to its owning constituent.
10. `set` triggers per-trait change detection.
11. `add` adds only the constituents the entity does not already have.
12. `add` distributes initial values by field.
13. `remove` removes all constituent traits.
14. An aspect used as a query parameter requires all its constituents.
15. `readEach` delivers a merged data object.
16. `updateEach` distributes writes back to constituent stores.
17. Aspects compose with all query modifiers.
18. `Not` with an aspect matches entities missing at least one constituent.
19. `Changed` matches when any constituent data changed.
20. `Added` matches the transition to all-present.
21. `Removed` matches the transition from all-present.
22. `onAdd` fires when an entity transitions from incomplete to complete.
23. `onRemove` fires when an entity transitions from complete to incomplete.
24. `onChange` fires when any constituent changes while all are present.
25. Each `createAspect` call returns a distinct instance.
26. A directly supplied aspect contributes one merged readEach callback data object, including when constructed from nested aspects.
27. Setting a supplied constituent field to its existing value still triggers that constituent's change notification when triggerChanged=true.
28. The aspect's id, traits and schema properties cannot be reassigned.

# Constraints

- Powdrr manages branching and commits for this task. Implement the requested changes in the supplied checkout.

# Relevant context

- Relevant existing core modules: `src/trait/trait.ts`, `src/trait/types.ts`, `src/query/query-result.ts`, `src/query/types.ts`, `src/query/query.ts`, `src/world/world.ts` and `src/index.ts` under `packages/core`.
- Existing regular-trait `setTraitForTrait` writes through `ctx.set` and calls `setChanged` when `triggerChanged` is true, without an equality guard.
- Existing `createQuery` freezes its query object; that freeze does not freeze the referenced parameters array.

# Acceptance criteria

1. Given data traits A={x:1}, B={y:2}, C={z:3}, a complete entity and Outer=createAspect(createAspect(A,B),C), readEach on a query with Outer as its only parameter supplies state with one element: {x:1,y:2,z:3}.
2. Given an entity with every constituent of aspect(A,B), A.x=1, and an A change subscriber registered after setup, setting the aspect with {x:1} and triggerChanged=true invokes that subscriber once and leaves A.x equal to 1.
3. Given a created aspect and saved values of its id, traits and schema, attempts to assign replacement values do not change those property values; assignment may throw in strict mode.

The remaining numbered requirements are also required completion conditions; the examples above cover only the resolved clarifications.

# Verification and completion

- Add or update focused tests for the numbered requirements and acceptance scenarios. Run the core tests once without watching: `pnpm --filter @koota/core test --run`.
- Run the existing core lint command: `pnpm --filter @koota/core lint`.
- Report changed files, checks actually performed, failures and any remaining requirement ambiguity when finished.
