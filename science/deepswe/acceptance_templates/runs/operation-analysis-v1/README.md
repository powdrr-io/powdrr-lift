# Koota composite aspects: operation decisions before acceptance criteria

## Result

Providing original repository code improved some inherited API decisions, but
the broad analysis still largely restated the instruction. Asking about one
behavioral decision at a time recovered more of the authoritative solution.
It also exposed two incorrect choices and several cases where a matching
proposal was withheld because the model failed to supply verifiable evidence.
This is progress in identifying the needed analysis shape, not a complete
prompt-quality solution or a demonstrated coding success.

The solution is correct by definition. Its exact behavior, including its
composition and edge policies, is the target. No normative criticism of the
solution is used in this experiment.

## Artifacts

- [Frozen target decisions](oracle.json): 34 agent-authored target entries,
  mapped to instruction lines and solution patch locations.
- [Original instruction](input.json) and [original repository evidence](repository-evidence.json).
- [Production routing/splitting results](instruction-analysis.json).
- Analysis from [instruction alone](instruction-only/analysis.json),
  [instruction plus repository](repository-grounded/analysis.json), and
  [focused questions plus repository](focused/analysis.json).
- [Per-target comparison](decision-review.json), including citations and
  explanations; [previous candidate review](prior-prompt-review.json).
- [Generated six-section prompt](focused/prompt.md) and its
  [decision mappings](focused/prompt-sections.json).
- [Prompt audit](prompt-audit.md): the formatter carries the two wrong choices,
  converts some unresolved proposals to mandates, invents an add signature and
  repository commands, and repeats policies across sections. This output is not
  an approved prompt.
- Reproduction scripts: [run_experiment.py](run_experiment.py),
  [focused_analysis.py](focused_analysis.py), [review_targets.py](review_targets.py).

## Method and boundaries

1. Extracted target decisions from the solution and froze their JSON/hash
   before making analysis calls. Entries group related facets of a contract;
   these are not atomically normalized requirement counts.
2. Retrieved Koota at the task's base commit
   `9c434858b2b522002f8c5eb4a554fa8836a7cf3c`. Only source at that commit enters
   generation. The final evidence bundle contains 24 existing modules selected
   from the instruction's named APIs and their accessor dependencies.
3. Reused production deterministic sentence capture, Typesafe.ai JEV atomicity
   classification/routing, and DeepSeek Flash splitting. The instruction went
   from 21 clauses to 28: 25 include, one context, two exclude, no unresolved
   routes. No classifier substitution was allowed.
4. Asked DeepSeek `deepseek-ai/DeepSeek-V4-Flash-0731` for decisions in five
   operation groups, with and without repository evidence. Both arms receive
   the same instruction, routing, analysis instructions and schemas.
5. After the broad analysis largely missed the requested dimensions, asked 16
   narrower questions. The questions instantiate generic dimensions (missing
   state, unknown fields, copy depth, equality, notification controls/unit,
   callback slots, identity, mutability and lifecycle). Their selection was
   manual and exploratory after the oracle was authored. The model receives
   the questions, instruction and base code, but no target answers. Therefore
   this arm demonstrates focused resolution, not automated or blinded
   discovery of which questions to ask.
6. Reviewed each analysis against the frozen target before generating the
   final prompt. Rendering receives the original instruction and analysis
   results only. It receives no oracle, solution, validation patch or review
   verdicts. Focused statements supersede broad statements on the same policy.

The model requests and responses are preserved in `call-checkpoints.tar.gz`.
`manifest.json` records artifact hashes and the original model boundary. The
previous hand-written candidate was itself solution/test-informed; it is a
specification comparator, not the captured standard Powdrr-generated prompt.
No standard prompt or coding-agent result is substituted into the control arm.

## Static comparison

| Artifact | Full | Partial | Missing | Wrong | Unresolved |
| --- | ---: | ---: | ---: | ---: | ---: |
| Previous six-section candidate | 11 | 8 | 14 | 1 | 0 |
| Instruction-only analysis | 6 | 12 | 12 | 4 | 0 |
| Analysis with original code | 11 | 12 | 11 | 0 | 0 |
| Focused questions plus original code | 16 | 7 | 4 | 2 | 5 |

Full means an entire target entry is resolved, not merely mentioned. Partial
means some facets remain unstated. Wrong means an explicit policy conflicts
with the authoritative solution. Unresolved means the text proposes matching
behavior but does not establish a resolved instruction for the coding agent.
Four target entries are edge policies (non-never mode equivalence, multiple
Not-set aggregation, mask generations, and function-schema ownership). Removing
them leaves 16 full, six partial, one missing, two wrong and five unresolved in
the focused arm. Exact per-entry judgments and evidence are in the review JSON.

These are single-author agent review labels over one development task. They
are not human-adjudicated precision/recall estimates, atomically normalized
scores, or test-pass rates. A matching policy can still have false evidence
attribution; that is recorded separately in the explanations.

## Concrete findings for the generic pipeline

| Requirement wording | What analysis must resolve | Observed outcome |
| --- | --- | --- |
| `set` distributes fields and triggers per-trait change detection | Missing-constituent precondition; same-value notification; explicit suppression | Focus recovered the no-op policy and correctly proposed unconditional ordinary-set notifications/pass-through suppression. The no-op was incorrectly called an instruction fact; the notification proposal lacked a verified citation. |
| `get` returns a merged object; `readEach` delivers merged data | Top-level copying versus nested aliasing | Focus proposed the correct shallow/shared-reference behavior. Fabricated annotations in code quotes caused evidence rejection. |
| `updateEach` distributes writes | Equality, disabled notifications, callback destruction, dispatch timing | Focus recovered shallow comparison, never-mode writes without notification, skipping destroyed entities, and notifications after iteration. The destruction guard had directly verifiable evidence. |
| Each aspect exposes id, traits, schema | Assignment protection versus shallow/deep freezing | Focus overextended existing read-only properties and an empty frozen tag schema into deep freezing. This conflicts with the target's shallow freeze. |
| Nested aspects flatten; callbacks receive merged data | Which operation flattening applies to; what remains one callback slot | Focus incorrectly split nested-aspect constituents into separate callback slots. Construction flattening was applied to projection layout. |
| Aspects compose with modifiers | Explicit matching predicates, including constituent-level Or | Focus stated the target Or predicate, but its rationale calls the policy an assumption while the basis field claims repository fact. Predicate selection and support classification must be assessed separately. |
| `onChange` fires when a constituent changes | Per-constituent versus per-operation callbacks | Focus proposed the target per-constituent unit but supplied no verifiable evidence. |

The main new evidence is that a checklist of dimensions does not force the
model to resolve them. A focused decision question does better. Supplying more
code alone did not eliminate the missing policies or establish source support.

## Changes justified next

- Keep routing/splitting and scope each downstream decision to its owning
  operation. Construction flattening, logical expansion and callback data
  projection must be resolved independently.
- Turn the generic semantic dimensions into explicit questions with bounded
  alternatives. For example, copy policy is live reference, shallow copy,
  deep copy or unspecified; metadata assignment protection and freezing depth
  are separate decisions. The model should explain its selected alternative
  rather than invent unrestricted prose for all dimensions at once.
- Supply small repository excerpts with stable evidence IDs. Let the model
  cite IDs and let code attach the original text. This avoids asking the model
  to reproduce code quotations and losing otherwise useful proposals to
  fabricated comments, ellipses, or punctuation changes.
- Classify source support independently from the behavioral choice. A literal
  code quote does not establish that a new composite must inherit every
  behavior, and a plausible matching default is not an explicit instruction.
- Generate acceptance criteria after those choices, using cases that distinguish
  alternatives: same-value set; nested-aspect callback slot; shallow versus
  deep schema freezing. Preserve chosen defaults and open choices visibly.

Question planning has not been automated in this experiment. That is the next
piece to implement and evaluate against this frozen decision set, rather than
shipping the broad freeform analysis as a production improvement.

## Failures and recovery

The first broad code-grounded request put evidence-category labels into the
behavior field. The first broad instruction-only response kept enumerating
speculative cases until the 65,536-character output bound stopped it. Those
artifacts remain under `initial-format/` and in the checkpoint archive.

The recovery used a clarified behavior field, scoped operation groups and a
4,096-token response budget. Source-ID aliases were mapped back to original
line IDs. Instruction quotes ending a selected clause with an added period
were aligned back to the original clause; the original model quote was retained.
Unverifiable code quotations were retained as rejected evidence. They were not
silently accepted as repository proof; repository-based proposals lacking a
verified citation were withheld or marked unresolved. Some correction calls
were made before these deterministic citation fixes; all attempts remain in
the recorded cost.

The final renderer checks that every analysis decision ID appears in a prompt
section. Its first response omitted a copy-policy decision and was rejected;
the correction also omitted the same ID. The assembler materialized the missing
decision in its correct unresolved context from the analysis record, reusing
the captured response without a third model call. ID completeness checks
protect presence, not semantic correctness; the prompt audit records additional
semantic and interface errors introduced by rendering.

No coding agent, task verifier, or tests were run. Production behavior was not
changed. See `run-summary.json` for actual call counts, elapsed call time, prompt
size and generation status; missing provider token usage is left unknown.

## Reproduction

This experiment imports the existing science Recorder and production analysis
helpers from the science branch (PR #963). It is reviewed as a separate change
on top of those helpers. Use the shared environment and current worktree paths
from AGENTS.md; no dependency installation is needed.

Run `run_experiment.py prepare` with the DeepSWE task directory and a Koota
checkout detached at the recorded base commit. Then run `analyze`,
`focused_analysis.py`, and `review_targets.py` before
`run_experiment.py prompt --arm focused`. Recorder reuses checkpoints whose
request fingerprints match; extract the checkpoint archive to this experiment
directory to inspect or resume the captured calls. Provider credentials must
already be configured, including Typesafe.ai. Do not rerun these phases to
recompute the static comparison: `review_targets.py` alone makes no model calls.

The scripts passed Ruff formatting/lint and scoped mypy checks. Tests were
omitted at the user's request.
