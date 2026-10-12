# What survived prompt generation

The generated prompt is 2,060 words, compared with 1,172 in the previous
candidate. This comparison is about clarity and preserved decisions; a larger
prompt is not automatically worse. Here, however, requirements are repeated in
Constraints and Relevant context, and rendering introduces additional errors.

The frozen target review was completed before this prompt call. The renderer
received no solution, target entries, or review findings. Consequently it
preserves the analysis mistakes instead of being corrected from the oracle.

## Carried analysis errors

- It requires a deeply frozen schema in Requirements, Constraints, Context,
  and Acceptance criteria. The correct solution freezes only the top-level
  schema object.
- It says nested aspects contribute one callback slot per underlying trait.
  The correct solution retains one merged slot for a directly supplied aspect,
  regardless of how its constituent traits were flattened at construction.

These are decision-classification errors: fixing formatting alone cannot
correct them. The decisive distinctions are metadata assignment protection
versus freezing depth, and construction flattening versus callback projection.

## Additional rendering errors

- The focused proposals about same-value set notifications, per-constituent
  onChange callbacks, and reset behavior are marked unresolved in the analysis.
  The renderer promotes them to mandatory behavior without labeling their
  unresolved status, despite receiving an explicit instruction to preserve it.
- Acceptance criterion 2 invents `add(aspect, data)`. The solution uses bare
  `add(aspect)` or `add(aspect(data))`. The analysis never established the
  invented two-argument interface.
- It invents verification commands for `@powdrr/core`, plus a `typecheck`
  script. The original core package is `@koota/core`, and its package.json
  exposes `test` and `lint`, not `typecheck`. That package file was not in the
  generation input, so the renderer should not have guessed commands.
- It adds a prohibition on new public APIs beyond createAspect/associated
  types that is not established in the source instruction or analysis.
- It repeats policies in several sections and introduces an implementation
  prescription for an ID counter in Relevant context.

## Deterministic recovery

Both renderer attempts omit decision f004, the shallow-copy proposal. The
assembler restores that omitted decision in Relevant context using the
analysis's exact rule and alternatives. `prompt-sections.json` records the
fallback. No third renderer model call was made.

This recovery proves presence of the missing decision. It does not correct
the renderer's changed meaning elsewhere. The generated file is an experiment
output with known errors, not an approved implementation prompt.

## Implication for implementation

Resolve semantic choices before assembling the prompt. Render each resolved
decision once from the stored record, preserve its provenance/default status,
and keep unresolved choices in a consistent location. Generate acceptance
examples from those decisions as a separate final step, checking that examples
introduce no new API forms or policies. Obtain executable validation commands
from the repository rather than freeform generation.

The focused analysis has useful gains, but handing all decisions back to an
LLM to rewrite every section does not reliably preserve those gains.
