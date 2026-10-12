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
26. createAspect accepts two or more traits (or nested aspects) and returns a new aspect object with a unique id, a flattened list of constituent traits, and a merged schema, throwing an error if any constituent is a relation or if any field names overlap across constituents.
27. The core package exports `createAspect` and its associated types from the main index, making it available to consumers.
28. Each aspect exposes a read-only `id` (unique number), a `traits` array containing the flattened constituent traits, and a `schema` object that merges the schemas of all constituents.
29. When an aspect is passed as a constituent to createAspect, it is flattened into its individual traits, so the resulting aspect's traits array contains only the underlying traits, not the nested aspect object.
30. `has` returns true only when the entity has every constituent trait of the aspect, and false otherwise.
31. `get` returns a new merged object containing all fields from all constituent traits when the entity has every constituent, and returns undefined if any constituent is missing.
32. `set` accepts a merged object of fields, writes each field to its owning constituent's store, and triggers per-trait change detection for each constituent that had a field written.
33. `add` adds only the constituent traits the entity does not already have, initializing each newly added constituent with the values from the provided merged object for its fields, and leaves already-present constituents unchanged.
34. `remove` removes all constituent traits from the entity, regardless of whether the entity currently has them, and does nothing for constituents that are already absent.
35. When an aspect is passed as a query parameter, the query matches only entities that have all constituent traits, and the query result's readEach and updateEach operate on the merged aspect data.
36. readEach invokes the callback with a merged object containing all constituent fields for each entity that has all constituents, and returns the query result for chaining.
37. updateEach invokes the callback with a merged object, then distributes each field back to its owning constituent store, triggering per-trait change detection for changed fields.
38. The `Not` modifier, when given an aspect, matches an entity if and only if the entity lacks at least one of the aspect's constituent traits.
39. The `Or` modifier, when given an aspect, matches an entity if and only if the entity has at least one of the aspect's constituent traits.
40. The `Changed` modifier, when given an aspect, matches an entity if and only if at least one of the aspect's constituent traits has changed while the entity still has all constituents.
41. The `Added` modifier, when given an aspect, matches an entity if and only if the entity transitions from having at least one missing constituent to having all constituents.
42. The `Removed` modifier, when given an aspect, matches an entity if and only if the entity transitions from having all constituents to having at least one missing constituent.
43. When an aspect is used as a query parameter, the query requires all constituent traits, and when an aspect is used inside `Not`, `Or`, `Changed`, `Added`, or `Removed`, the modifier's trait list is the aspect's constituent traits, and tracking groups are created per modifier with AND logic for top-level tracking modifiers and OR logic for nested tracking modifiers inside `Or`.
44. The aspect's onAdd subscription fires exactly once when an entity transitions from having at least one but not all constituent traits to having all constituent traits, and the callback receives the entity as its sole argument.
45. The aspect's onRemove subscription fires exactly once when an entity transitions from having all constituent traits to having at least one but not all, and the callback receives the entity as its sole argument.
46. The aspect's onChange subscription fires whenever any constituent trait's data changes while the entity has all constituent traits, and the callback receives the entity as its sole argument.
47. Each of onAdd, onRemove, and onChange returns an unsubscribe function that, when called, removes the specific callback from the aspect's subscription set, and subsequent calls are no-ops.
48. When the world is reset, all aspect subscriptions are cleared and no further callbacks fire for entities created after the reset, and the aspect's constituent traits are re-registered as needed.
49. entity.set(aspect, data) should be a no-op if any constituent trait is missing, leaving the entity unchanged and not triggering change detection.
50. Aspect set and configured add should ignore unknown fields and apply only provided fields, leaving omitted fields unchanged, consistent with the existing SoA set accessor contract.
51. In readEach/updateEach callbacks, directly supplied aspects, ordinary data traits, and tag-only aspects each occupy one slot in the state array, with tag-only aspects yielding an empty object; Not modifiers contribute no slot, and nested aspects flatten to their constituent traits, each occupying its own slot.
52. In aspect updateEach, a constituent is considered changed if its new value is not shallow-equal to the previously stored value, where shallow equality treats equal primitive values as unchanged and separately allocated nested references as changed unless they are the same reference.
53. updateEach should accept a changeDetection option that, when set to 'never', commits writes to stores without triggering change detection or notifications, while 'always' and 'auto' commit writes and trigger change detection/notifications as currently implemented.
54. If an updateEach callback destroys its entity, aspect writes and notifications for that entity should be skipped entirely, as the existing query-result code checks `world.has(entity)` after the callback and continues without committing changes or triggering change events.
55. Aspect updateEach should dispatch constituent change notifications after the callback iteration completes, following the existing query-result pattern where changed pairs are collected during the loop and setChanged is called in a separate loop after all entities have been processed.
56. Repeated occurrences of the same trait through direct or nested aspect inputs should be deduplicated, preserving the first occurrence's position in the flattened traits list, and the minimum constituent count is the number of unique traits after deduplication.
57. Aspect's `id`, `traits`, and `schema` properties should be read-only (non-writable, non-configurable) via `Object.defineProperty`, the `traits` array should be frozen with `Object.freeze` to prevent mutation, and the `schema` object should be deeply frozen to prevent any nested value changes.
58. Changed(aspect) should match an entity only if all constituent traits are present and at least one constituent has changed, so an entity with a changed constituent but another constituent missing does not match.
59. When an aspect is passed to Or alongside a regular trait, the aspect's constituent traits are expanded into separate alternatives within the Or, so the predicate is a disjunction over each individual constituent trait and the regular trait, not a grouped conjunction.
60. Aspect subscription methods should return an unsubscribe function that, when invoked, detaches the callback from the aspect's constituent trait subscription sets, and repeated calls to that unsubscribe function have no further effect (they are idempotent).

# Constraints

- Powdrr manages branching and commits for this task. Implement the requested changes in the supplied checkout.

# Relevant context

- Relevant existing core modules: `src/trait/trait.ts`, `src/trait/types.ts`, `src/query/query-result.ts`, `src/query/types.ts`, `src/query/query.ts`, `src/world/world.ts` and `src/index.ts` under `packages/core`.
- Existing regular-trait `setTraitForTrait` writes through `ctx.set` and calls `setChanged` when `triggerChanged` is true, without an equality guard.
- Existing `createQuery` freezes its query object; that freeze does not freeze the referenced parameters array.

# Acceptance criteria

Every numbered requirement is a required completion condition. Validate its stated membership, data, notification or public API behavior with focused tests. No additional scenario is synthesized.

# Verification and completion

- Add or update focused tests for the numbered requirements and acceptance scenarios. Run the core tests once without watching: `pnpm --filter @koota/core test --run`.
- Run the existing core lint command: `pnpm --filter @koota/core lint`.
- Report changed files, checks actually performed, failures and any remaining requirement ambiguity when finished.
