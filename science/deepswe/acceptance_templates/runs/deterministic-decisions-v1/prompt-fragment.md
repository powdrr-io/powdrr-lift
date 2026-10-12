# Focused prompt fragment

## Requirements

1. A directly supplied aspect contributes one merged readEach callback data object, including when constructed from nested aspects.
2. Setting a supplied constituent field to its existing value still triggers that constituent's change notification when triggerChanged=true.
3. The aspect's id, traits and schema properties cannot be reassigned.

## Relevant context

- Existing createQuery freeze behavior: queryRef properties cannot be reassigned, but the referenced array is not frozen by this call.
- Existing setTraitForTrait notification guard: the call is guarded by triggerChanged, not value inequality.

## Acceptance criteria

- Given data traits A={x:1}, B={y:2}, C={z:3}, a complete entity and Outer=createAspect(createAspect(A,B),C), readEach on a query with Outer as its only parameter supplies state with one element: {x:1,y:2,z:3}.
- Given an entity with every constituent of aspect(A,B), A.x=1, and an A change subscriber registered after setup, setting the aspect with {x:1} and triggerChanged=true invokes that subscriber once and leaves A.x equal to 1.
- Given a created aspect and saved values of its id, traits and schema, attempts to assign replacement values do not change those property values; assignment may throw in strict mode.
