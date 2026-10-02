# Handoff: instruction-to-prompt formatting and structure

## Goal

Improve the readability and organization of generated implementation prompts
without silently changing the product behavior they specify. Keep formatting
and structure work separate from semantic corrections.

## Evidence to use

- Latest available captured prompt:
  `/private/tmp/state-data-evidence-fix-capture-eval-20261003/artifacts/prompts/harbor-task-implement-harbor-task-code-task-001-prompt-capture-1.txt`
- Rebuilt reference prompt and test trace:
  `docs/evaluations/deepswe-python-statemachine-reference-prompt-v2.md`
- The captured prompt is about 40.7 KB. Its run metadata has no Powdrr
  revision, so its exact source commit is unknown.

## Formatting and structure findings

1. **The same requirements are repeated across sections.** The product
   summary, JSON-like “Required product changes” list, detailed scenarios,
   relationship statements, and repository-evidence notes often restate the
   same behavior. Reduce repetition while retaining compact traceability from
   requirements to checks.
2. **Internal metadata leaks into worker-facing content.** Design-sentence
   records include fields such as `intent_effect` and section labels. Keep
   provenance in concise identifiers or separate metadata instead of
   embedding compiler explanations in implementation instructions.
3. **Related-requirement lists are too long.** Scenarios repeatedly attach many
   sibling requirements, even when they do not help interpret that check.
   Prefer a small set of readable checks grouped by feature and important
   interactions.
4. **Some generated scenarios are malformed or hard to scan.** Examples
   include “The source instruction: Given The implementation is evaluated
   against…” and the diagram sentence ending with “Unspecified behavior was
   resolved using the recorded normative defaults.” Render complete,
   grammatical statements.
5. **Repository-evidence output is repetitive and not actionable.** Many
   entries say “No matching declaration was found…” without naming a useful
   path or symbol. Omit repeated inventory boilerplate or replace it with
   concise, concrete locations that help the worker inspect the code.

## Keep semantic issues separate

The following are known semantic discrepancies. Do not silently resolve them
as part of a formatting-only change; preserve them as explicit review items:

- Deep and shallow history are both required, but the captured prompt labels
  them as alternative outcomes.
- Callback `state_data` is a merged scope, while `get_state_data(state)` is
  state-owned data. The captured prompt blurs those results.
- The prompt implies callback data is the same mutable dictionary across
  callbacks and API calls; the reference solution constructs a callback scope
  separately, and tests do not establish direct-mutation persistence.
- The prompt describes defaults as independent copies, while the reference
  solution recreates the outer mapping but reuses ordinary values by
  reference. The task instruction and tests do not resolve nested mutable
  default semantics.
- Generated fallback and error behavior for diagrams, history snapshots, and
  SCXML inputs is not established by the instruction or added tests.

## Suggested output

Provide a proposed worker-prompt structure and show how the current sections
map into it. Separate any semantic edits from editorial changes. Preserve
source/test traceability without repeating full requirement prose in every
section. Do not claim that a shorter prompt is better unless a worker outcome
comparison supports that claim.

## Comparison checks

- Every explicit source requirement remains visible in the worker-facing
  contract or a concise traceable check.
- A requirement has one canonical explanation; cross-references point to it
  instead of restating it.
- Checks are grouped around observable behavior and interactions, not emitted
  mechanically one per source sentence.
- Process instructions and repository inspection guidance are clearly
  separated from product behavior.
- Formatting changes do not alter requirement strength, turn alternatives
  into requirements, or introduce fallback semantics.
