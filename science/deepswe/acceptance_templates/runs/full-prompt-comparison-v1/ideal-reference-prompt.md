# Objective

Add composite trait aspects with unified entity operations, merged query data and completeness-aware subscriptions. Preserve the existing constituent trait storage and notification behavior.

# Requirements

1. Export createAspect and the public Aspect type.
2. Flatten nested aspects, deduplicate underlying traits by identity, retain first occurrence order, and require at least two unique traits after flattening.
3. Reject relation constituents and overlapping data schema field names at creation time.
4. Accept tags and merge schema fields from data constituents; tags contribute no fields.
5. Every call returns a fresh instance with a distinct numeric ID.
6. Expose non-reassignable id/traits/schema; freeze traits and schema at the top level, retaining nested schema references.
7. An aspect is callable with optional initial field values and the configured result is accepted by entity.add; bare aspect addition uses defaults.
8. has requires every constituent; get returns undefined if any is missing; otherwise merge data fields, omit tag data, and return an empty object for a complete tag-only aspect.
9. Return a new shallow merged object: top-level assignments do not write back; nested object/array references are shared, not deeply cloned.
10. If any constituent is missing, perform no writes and no notifications.
11. Route supplied schema fields to their owners, ignore unknown fields, and preserve unspecified fields.
12. Forward triggerChanged: false suppresses notifications while applying writes.
13. Add only missing constituents, use trait defaults for omitted fields, and never overwrite data of existing constituents.
14. Remove present constituent traits, tolerate absent constituents, and leave unrelated traits alone.
15. Require all constituents, compose with ordinary traits and other aspects, and update matching as constituent membership changes.
16. Use one merged data slot per directly supplied aspect, including an empty object for a tag-only aspect; ordinary tag parameters and Not modifiers contribute no data slots; retain surrounding data parameter order.
17. Write back changed existing fields to their constituent stores and detect changes per constituent using top-level strict inequality, not recursive value comparison.
18. changeDetection never still commits writes but suppresses change notifications.
19. For aspect queries, all non-never modes share the same branch and emit notifications for constituents detected as changed.
20. After the callback, skip writeback if the entity has been destroyed.
21. Dispatch detected constituent change notifications after processing the entity iteration, rather than during each callback.
22. For a single aspect within a mask generation, match entities missing at least one constituent, including none; update membership in both directions.
23. Require all constituents currently present and accept any recorded constituent data change; unchanged entities do not match.
24. Track incomplete-to-complete and complete-to-incomplete membership against existing modifier tracking history, including complete spawn and excluding removals from previously incomplete sets.
25. Expand an aspect into independently matching constituent alternatives: Or(aspect(A,B), C) is A OR B OR C.
26. onAdd gates constituent add callbacks on completeness; onRemove gates removals on the other constituents being present; onChange gates constituent change callbacks on completeness; callbacks receive the affected entity.
27. Return an unsubscribe function which removes all constituent subscriptions.
28. Notifications remain per constituent, so one aspect operation updating two constituents may invoke the aspect subscriber twice; do not coalesce to once per aspect operation.
29. Aspect instances participate distinctly in query hashing rather than being represented as ordinary constituent query parameters.
30. Combine constituents from multiple aspect Not parameters into a single notAll set; do not retain a separate negative predicate for each aspect.
31. checkAspectTrackingGroup returns the result for the first relevant generation; negative masks are evaluated separately per generation.
32. Function-valued schemas contribute no merged schema entries or field owners; their fields are not routed by aspect set/add through the field map.
33. Aspect queries continue working across world.reset through existing reset behavior.
34. For each constituent with supplied recognized fields, use the existing setTraitForTrait path, which emits change when triggerChanged is true even if supplied values equal existing values.
35. Aspect onAdd, onRemove and onChange callbacks are invoked with the affected entity as the first argument and undefined as the second argument.

# Constraints

- Preserve existing ordinary trait, query and subscription APIs.
- Powdrr manages branching and commits; implement the task in the supplied checkout.

# Relevant context

- Existing regular-trait setters notify when triggerChanged is true without checking equality; aspect updateEach separately compares stored field values.
- Relevant existing paths are packages/core/src/trait, packages/core/src/query and packages/core/src/world. The core package is @koota/core.

# Acceptance criteria

1. For nested inputs containing repeated trait identities, construction produces the first-occurrence unique trait list; fewer than two unique traits, relations and overlapping field names fail.
2. Metadata reassignment and top-level traits/schema mutation cannot change the public metadata; nested schema objects retain identity and remain unfrozen.
3. A complete entity returns merged data; an incomplete entity returns undefined and aspect set performs no writes or notifications. Modifying a returned top-level field does not persist, while nested references are shared.
4. Configured add accepts aspect(data), adds missing constituents with defaults for omitted fields and preserves existing data. Unknown fields are ignored.
5. Equal-value aspect set notifies an affected constituent with triggerChanged true; false still writes but emits no notification. Updating two constituents can deliver two aspect onChange callbacks while the entity remains complete.
6. A directly supplied nested aspect contributes one merged callback slot; a tag-only aspect contributes {}; ordinary tags and Not contribute no slots.
7. Aspect updateEach commits recognized fields, compares top-level values with strict inequality and skips destroyed entities. Never mode commits without notifications; other modes notify detected changes after iteration.
8. Changed requires current completeness. Or(aspect(A,B),C) matches A OR B OR C. Added/Removed observe completeness transitions against existing tracking history, including complete spawn and excluding incomplete removals.
9. Unsubscribe detaches all constituent hooks, distinct aspect query identities remain distinct, and aspect querying works after world.reset.

The remaining numbered requirements, including multiple-Not aggregation, mask generations and function-schema ownership, are required completion conditions.

# Verification and completion

- Add focused validations for each numbered requirement and run existing core tests once: pnpm --filter @koota/core test --run.
- Run core lint: pnpm --filter @koota/core lint.
- Report changed files, performed checks, failures and unresolved issues.
