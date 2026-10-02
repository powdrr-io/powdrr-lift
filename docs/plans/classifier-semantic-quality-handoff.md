# Handoff: improve semantic classification and generated prompt quality

## Objective and task boundary

Improve the general instruction-to-prompt pipeline so its final implementation
prompt preserves requirements, applies them to the correct subjects and scopes,
and makes useful implementation choices without inventing source requirements.
The task instruction, reference solution, and validation patches are fixed.
Reference solutions are evaluation oracles; ordinary production classification
must work from the instruction, bootstrap, and permitted repository evidence.

The user wants actionable analysis of the form:

> Today the prompt says Y. It needs to express X. Change this upstream decision
> about this instruction, and make downstream generation consume that decision.

Evaluate success primarily on the resulting prompt and worker outcome. Classifier
accuracy is diagnostic evidence. Do not substitute a large provenance project
or better intermediate labels for a demonstrated improvement in prompt meaning.

This document specifies future implementation work. The PR adding this document
does not implement the changes. Follow the sequence below in separate, reviewable
PRs. Keep existing repository worktree, shared-environment, and PR instructions.

## Established examples and desired decisions

The state-machine task is a development example, not a production special case.
Its instruction is at
`~/code/powdrr-deep-swe/tasks/python-statemachine-state-data-scoping/instruction.md`.
Its reference patches are `solution/solution.patch` and `tests/test.patch` under
that task directory. The checked-in reference prompt is
[`deepswe-python-statemachine-reference-prompt-v2.md`](../evaluations/deepswe-python-statemachine-reference-prompt-v2.md).

The inspected capture is
`/private/tmp/state-data-evidence-fix-capture-eval-20261003/artifacts/prompts/harbor-task-implement-harbor-task-code-task-001-prompt-capture-1.txt`.
Its metadata has no Powdrr revision. Treat it as evidence of observed output, not
as a reproducible baseline tied to today's main. Temporary files may disappear;
capture a new versioned baseline before claiming a before/after improvement.

| Instruction input | Observed prompt problem | Desired source decision | Required prompt effect |
| --- | --- | --- | --- |
| Line 9: "History recall restores saved data snapshots -- deep for full descendants, shallow for direct children." | Split clauses have `validation_relation=alternatives`; rendering says the source allows alternative outcomes. | Both behaviors are required. Each applies to its named mode. Selecting a mode is not permission to omit support for the other mode. | Preserve both mode-specific requirements and their conditions. |
| Line 7: "Hierarchical scoping merges ancestor data into child callbacks, child shadowing parent on collision." Line 11 separately defines `get_state_data(state)`. | A related-requirement statement connects the getter to the same data injected into callbacks. | The merge applies to callback input. The getter is an operation on the named state's data. Similar vocabulary does not establish identical views. | Keep callback scope and state query scope distinct. Do not transfer ancestor merging to the getter. |
| Line 9: "Data persists through on_enter and on_exit callbacks." | Scenario wording adds the same dictionary instance and persistence of direct callback writes. | Lifecycle availability/persistence is stated. Dictionary identity and propagation of direct writes are not stated. | Preserve availability through enter and exit; avoid an unsupported identity/write-through guarantee. |
| Line 3: "On entry, data initializes as a fresh copy of the defaults." | Scenario wording promises independent nested mutable defaults. | Freshness is stated. Copy depth is unspecified by this sentence. | Preserve fresh initialization without asserting recursive copying as a source requirement. |
| Line 13: "DataVar rejects simultaneous default and factory." | The source rule does not resolve absent arguments or explicit `None`. Earlier discussion incorrectly suggested the source classifier should emit the solution's non-`None` rule. | Mutual exclusion is stated. Argument-presence versus value-presence semantics and the neither-present case are unspecified. | Preserve the rejection rule. Resolve material implementation gaps through the existing evidence/assumption policy, not by mislabeling them as source facts. |

The reference implementation uses a fresh outer mapping and retained ordinary
value references; callbacks receive a merged dictionary copy; the getter returns
a shallow copy of a state's own mapping; `DataVar()` initializes to `None`, and
`default=None` plus a factory invokes the factory. These implementation details
help score prompt outcomes. They are not all entailed by the source, and the
validation patch does not directly test every detail. Source-only gold labels
must retain that distinction.

## Existing implementation to extend

Read these files before implementation. Recheck their contents against the branch
base; line numbers and function placement will change.

| Location | Current role | Planned use |
| --- | --- | --- |
| `docs/procedrr/skill-definitions/design-interview.yaml` | Atomicity split instructions, scenario generation, capability matrices, and benchmark assumptions. | Change semantic questions and generation rules; keep JSON schemas and runtime bindings synchronized. |
| `src/powdrr_lift/core/instruction_ledger.py` | Deterministic source capture; bounded model splits; compiler-owned IDs/spans; validation groups. | Preserve source modifiers and separate semantic relationships from test grouping. |
| `src/powdrr_lift/workrr/command_catalog.py` | Prepares/binds atomicity, source decisions, extracted contracts, and scenario artifacts. | Carry new bounded context and decisions through workflow command boundaries. |
| `src/powdrr_lift/workrr/semantic_contract_compiler.py` | Routing and dependent classifiers; exact subject/behavior/modifier extraction; partial-contract compilation. | Improve inputs and targeted semantic questions without replacing the compiler. |
| `src/powdrr_lift/core/classifier_input.py` | Serializes optional containing/neighboring source context. | Reuse for bounded context; currently available helper does not prove context is wired into each decision. |
| `src/powdrr_lift/core/semantic_decision.py` | Allowed labels, unresolved reasons, decisions, exact spans, fingerprints. | Version any changed decision contracts; preserve provider independence. |
| `src/powdrr_lift/core/semantic_contract.py` | Partial semantic contract model. | Carry scope/modifiers and conditional dimensions as needed. |
| `src/powdrr_lift/core/semantic_faithfulness.py` | Source entailment reviews for existing semantic fields. | Extend existing checking to added scenario claims and relationships. |
| `src/powdrr_lift/core/behavior_contract.py` | Validates and renders scenarios, capabilities, relations, assumptions. | Render accepted decisions accurately and retain explicit assumption status. |
| `src/powdrr_lift/core/implementation_packet.py` and `src/powdrr_lift/workrr/coding_agent.py` | Compile implementation content and the actual worker prompt. | Ensure classifier decisions constrain every relevant worker-facing section. |
| `src/powdrr_lift/workrr/deepswe_design_evaluation.py` | Evaluates actual captured prompts using task reference artifacts. | Extend outcome comparisons and detect contradictory elaborations. |
| `tests/test_instruction_ledger.py`, `tests/test_semantic_contract_compiler.py`, `tests/test_semantic_decision.py`, `tests/test_behavior_contract.py`, `tests/test_deepswe_design_evaluation.py` | Existing contract, workflow, rendering, and evaluation tests. | Add behavior-changing regressions here; discover other applicable tests before creating new suites. |

`contract_observation_relation` already appears in the decision label registry,
with values including `same_observation`, `distinct_observations`, and
`mutual_exclusion`. The inspected revision has no other use of it. Determine
whether it fits the required role before extending it; an enum declaration is
not a working classifier or a substitute for requirement relationships.

Related design documents:

- [`source-anchored-semantic-contract-compilation.md`](../design/source-anchored-semantic-contract-compilation.md)
- [`local-semantic-classifier-implementation-plan.md`](local-semantic-classifier-implementation-plan.md)
- [`prompt-formatting-structure-handoff.md`](../evaluations/prompt-formatting-structure-handoff.md)

This plan improves meanings and their use. The local-model plan addresses provider
replacement and training. The formatting handoff addresses presentation. Coordinate
shared renderer/schema changes, but score semantic changes independently.

## PR 1: freeze a small, useful evaluation baseline

### Implement

1. Add a machine-readable case format containing source text, target proposition,
   permitted local context, case family, expected decision(s), explicitly
   unspecified dimensions, and expected/forbidden final-prompt claims.
2. Start with at least ten independently authored base cases for each of these
   six families: joint requirements versus alternatives; conditional modes;
   scope/ownership; lifecycle versus mutation/identity; copy semantics; validation
   presence boundaries. Use at least three domains across the suite, such as
   state machines, report/query APIs, and configuration/cache behavior.
3. Include positive cases that explicitly require deep copying, shared identity,
   write-through views, real alternatives, and argument-presence semantics.
   The suite must reject both invented behavior and loss of explicit requirements.
4. Generate paraphrases and minimal contrast pairs automatically. Validate them
   against the intended gold meaning. Group all variants of a base case and task
   in the same split; split before generating variants. Reserve entire domains or
   task families for held-out checks.
5. Reuse the existing classifier/evaluation runner where possible. Record exact
   decision inputs, provider/config revision, code revision, results, prompt text,
   latency, call count, and tokens. Do not expand the existing artifact system
   beyond what a paired comparison needs.
6. Capture the actual current worker prompt for the fixed state-machine task.
   Add at least two other existing tasks to the initial prompt-level comparison,
   chosen to exercise different confusion families.

### Annotation and evidence rules

- Gold source labels are based only on the source and permitted context.
- Reference solutions and validation patches are available only to offline
  evaluators judging the target prompt/worker behavior.
- Synthetic model output is a candidate annotation, not automatically truth.
- Review the base-case semantics once; review generated variants only on
  disagreement, invalid quotation, or suspected meaning change.
- Do not require exact prompt wording. Assert semantic obligations, scope, and
  forbidden strengthening. Literal checks are supplemental.

Use a format like this for development fixtures; finalize its schema in PR 1:

```yaml
case_id: export-modes-001
family_id: export-mode-requirements
domain: reporting
split: development
source: "Export supports CSV for tables and JSON for structured records."
target: "Export supports CSV for tables and JSON for structured records."
local_context: {}
expected_decisions:
  requirement_relation: conditional_required
required_prompt_claims:
  - CSV export is supported for tables.
  - JSON export is supported for structured records.
forbidden_prompt_claims:
  - Supporting either format alone satisfies the requirement.
unspecified_dimensions:
  - Behavior for inputs outside the named categories.
```

Include the contrasting sentence "For this export, either CSV or JSON is
acceptable" with `allowed_alternatives`. Add explicit positive controls such as
"Recursively copy nested settings," "Writes to the view update the backing
store," and "Reject when both arguments are supplied, even if one is null."
Those stronger behaviors must survive classification and rendering. Fixture
examples belong in evaluation data; they do not authorize domain-specific
production rules or require embedding worked task examples in classifier prompts.

### Acceptance

The baseline can be replayed, split leakage is rejected, and it reports both
classifier decisions and final prompt failures. Known contradictions must fail
even when another section repeats the source correctly. Unstated shallow/deep or
`None` semantics must not receive manufactured source gold labels.

## PR 2: repair requirement and condition relationships

### Decision contract

Separate two questions:

1. Which requirements are mandatory, permitted alternatives, conditional, or
   ordered according to the instruction?
2. Which assertions must be checked together in one scenario?

The current `validation_relation` serves both purposes. Preserve its testing role
while adding a small explicit requirement-relation contract, or migrate it to two
fields. Choose the least disruptive representation that makes both questions
unambiguous. Document label definitions before changing prompts.

Recommended semantic labels are `all_required`, `conditional_required`,
`allowed_alternatives`, and `ordered_required`, plus an unresolved result. An
ungrouped included clause remains a requirement; it does not need a new group to
be mandatory. A conditional group requires each branch when its condition holds.
`allowed_alternatives` requires affirmative evidence that any permitted outcome
satisfies the requirement; the word "or" alone is insufficient.

### Implement

1. Update the split question and rules in `design-interview.yaml` with generic
   examples: mode-specific requirements, an actual allowed choice, and separate
   assertions of one response. Do not add production state-machine special cases.
2. Keep each condition and governing action attached to its split child. When
   splitting elliptical prose, make inherited context explicit without adding a
   new condition or behavior.
3. Bind group membership, clause IDs, and exact evidence using compiler code.
   Require a parent-source entailment decision before accepting a semantic
   relationship that changes whether a clause is required. A valid enum value
   alone is insufficient.
4. Render semantic relationships separately from test-group descriptions.
   Source-authorized alternatives and alternative test paths are different.
5. Version the split/decision contract, serialization, fingerprints, and affected
   stored-artifact fixtures. Interpret legacy artifacts under their original
   version or require regeneration; do not silently relabel legacy `alternatives`.
6. Add meaningful tests for the two mode-specific requirements, genuine
   alternatives, joint response assertions, ordered behavior, and nested branch
   conditions. Include a compiler-to-worker-prompt regression.

### Failure behavior and acceptance

For a failed split or relationship review, retain the original parent requirement
and its meaning; use existing conservative source-preserving fallback mechanisms.
Do not discard a requirement or let an uncertain relationship weaken it.
All regression families must preserve mandatory branches, and actual choices must
remain choices. This is the first runtime improvement to land.

## PR 3: preserve scope, governing subjects, and modifiers

### Implement

1. Wire bounded local context into classifier and extractor requests where it can
   resolve meaning. Use the containing original sentence, split parent, and at
   most relevant neighboring source sentences. Do not supply the reference
   solution, hidden validation patch, or model-generated conclusions as source.
2. Keep the target proposition clearly distinguished from supporting context.
   Classifiers must label the target, not route or extract the whole paragraph.
3. Include context in decision input fingerprints and cache keys. A decision made
   with one context cannot be replayed against another as an identical request.
4. Improve existing extraction and coverage rules to preserve scope-bearing
   phrases: recipient, owner, lifecycle interval, mode, precedence, and exclusions.
   Reuse full-clause deterministic spans when they already preserve meaning;
   do not undo the existing deterministic extraction work to obtain shorter text.
5. Where current fields cannot carry the restriction, add a minimal scoped
   modifier record with its role and exact source span. Suggested roles are
   `owner`, `recipient`, `temporal_boundary`, `mode_condition`, and `precedence`.
   The role is a generic semantic label; the subject value comes from source text.
6. Before accepting a cross-clause relationship, classify whether it preserves
   subjects, scopes, and conditions. Shared nouns alone do not prove identical
   results, storage, or identity. Consider reusing the observation-relation enum
   for this narrower question, with its own question/evidence contract.
7. Feed those accepted restrictions into scenario generation. Preserve separate
   scopes even when the prompt groups requirements together for readability.

### Acceptance cases

- "Merge inherited settings into handler input" does not imply a configuration
  getter also merges settings.
- "Each account owns its records" remains scoped per account after extraction.
- Explicitly identical views can still be classified as identical.
- "Child overrides parent on collision" preserves both precedence and its
  collision boundary.
- Source context can resolve a pronoun without changing a neighboring clause's
  obligations or converting present-state context into requested behavior.

Require a final-prompt regression for scope leakage, not only exact-span tests.

## PR 4: classify material semantic dimensions selectively

Add targeted decisions only after checking whether improved extraction and
existing fields already solve the problem. Keep common clauses on the current
fast path. Use a conservative applicability screen and test its false negatives;
a lexical screen must not silently miss paraphrases of copying or persistence.

| Dimension | Proposed source-only answers | Meaning boundary |
| --- | --- | --- |
| Copy depth | `outer_container`, `recursive`, `unspecified` | "Fresh" does not determine depth. |
| Mutation propagation | `write_through`, `detached_mapping`, `unspecified` | Availability does not establish write propagation; detached mapping does not imply detached nested values. |
| Object identity | `same_object`, `distinct_objects`, `unspecified` | Equal values do not establish identical objects. |
| Persistence boundary | Exact source interval/condition or unspecified | Retain lifecycle and event qualifiers without adding identity. |
| Argument-presence rule | `argument_supplied`, `non_null_value`, `unspecified`, with exact condition text | Absence and explicit null are different only when evidence establishes that distinction. |

These are proposed label contracts, not assertions about current registry values.
Represent inapplicability separately from `unspecified`. Classifier/provider
abstention is also separate: `unspecified` means the source genuinely leaves the
dimension open; abstention means the provider could not determine the answer.

### Implement

1. Define each question, allowed answers, positive examples, and minimal contrast
   pairs. Require exact supporting spans for concrete source answers.
2. Add contract fields, decision values, revisioning, serialization, and binding
   through the existing provider-neutral envelope. Do not let providers author
   structural IDs or fingerprints.
3. Bundle applicable dimension questions into one structured provider request
   when the current provider supports it, but bind/validate results independently.
   Measure latency rather than assuming batching is faster.
4. Preserve unresolved dimensions through projection and compilation. An
   unspecified result cannot become a required concrete behavior just because
   the scenario needs a complete expected outcome.
5. Feed only material unresolved dimensions to the existing repository-evidence
   or assumption policy. Prefer explicit source/accepted design, then available
   repository evidence; do not select shallow copying globally because one
   reference solution uses it.
6. Keep benchmark continuation behavior: choose a definite compatible assumption
   when the existing policy requires it, identify it as an assumption, and
   continue. Interactive clarification follows the existing mode policy.

### Acceptance

Explicit deep-copy/write-through/identity cases retain those requirements. Silent
cases stay unspecified at source classification. `DataVar` terminology or task
names never appear in production branching logic. No-op clauses do not incur
every new classifier call. Material uncertainties yield useful implementation
guidance through the existing policy, not a vague prompt that passes by omitting
everything difficult.

## PR 5: constrain scenario elaboration with accepted decisions

Better labels must change the actual prompt. Current scenario instructions ask
for concrete normal results, errors, continuation, cleanup, compatibility, and
negative boundaries; benchmark mode fills unresolved dimensions with defaults.
Revisit that pressure alongside classifier work.

### Implement

1. Supply accepted scope, relationship, and semantic-dimension decisions as
   constraints to scenario generation. Modify prompts and response validation
   together; prompting alone is not the acceptance gate.
2. Keep source-supported expectations separate from necessary implementation
   assumptions. The rendered product contract must not describe an assumption
   as something the instruction explicitly guarantees.
3. Extend the existing faithfulness machinery to claims introduced in `then`,
   dimension text, `capability_matrix`, and `related_requirements`. Existing
   reviews of subject/behavior/result fields do not cover all later additions.
4. Evaluate bounded claims against their source clauses and accepted context:
   `entailed`, `contradicted`, or `not_stated`. Reuse the current entailment label
   contract rather than introducing synonymous labels.
5. Treat `contradicted` as requiring correction. A material `not_stated` claim may
   enter the established evidence/default-resolution path; it must not silently
   become a source obligation. Remove irrelevant speculative claims.
6. Deduplicate equivalent claims before review. Use deterministic acceptance for
   exact re-statements where sound; batch independent reviews when available.
   Avoid adding one remote call per repeated sentence in the prompt.
7. Limit regeneration to the offending scenario/field using existing review and
   retry limits. If semantic repair remains unresolved, preserve the source
   requirement and follow the existing mode-specific continuation/clarification
   policy. Do not add another coding-agent invocation as a repair mechanism.
8. Check all worker-facing sections for contradictions. Repeating the correct
   source at the top does not excuse an incompatible scenario farther down.

### Acceptance

Compiler-to-prompt tests demonstrate that unsupported same-instance, merged-getter,
deep-copy, and rejection claims cannot slip into source obligations. Supported
behavior remains actionable. Recorded assumptions are consistent across sections,
and interactive/benchmark mode behavior remains intact.

## PR 6: demonstrate prompt and worker improvements

Run paired before/after evaluation with frozen instructions, bootstrap, model
settings, task bases, and validation patches. Pin baseline/candidate revisions
and preserve the exact worker prompt for each run. Use fresh environments and the
repository's established benchmark workflow; consult the clean-run skill before
executing a live benchmark.

Report three levels together:

1. Decision results: requiredness mistakes, lost scope/modifiers, unsupported
   specificity, abstentions, and assumption resolution.
2. Prompt results: omitted explicit requirements, contradictions, unsupported
   mandatory claims, unresolved material behavior, and usefulness of assumptions.
3. Worker results: fixed validation pass counts/rate, completed implementation,
   and failures attributable to prompt choices versus worker execution variance.

Use the state-machine task as a development case and other held-out tasks for
generalization. If budget permits, repeat matched runs on tasks where stochastic
variance could change the conclusion. Report sample sizes and uncertainty; a
single passing implementation does not prove a classifier improvement caused it.

Initial promotion criteria:

- Every fixed critical regression case passes semantic prompt checks, including
  positive controls requiring stronger behavior.
- No explicit required behavior is lost on the held-out prompt suite.
- Unsupported mandatory claims and scope/relationship errors decrease relative
  to baseline. Do not count removing necessary detail as an improvement.
- Worker validation outcomes do not regress on the initial paired tasks; show
  whether targeted failures improved. If they do not, investigate before claiming
  success based on classifier scores.
- Report classifier and total planning p50/p95 latency, calls, tokens, fallback
  frequency, and review burden. Keep inactive dimensions free of additional
  provider calls. Any measured active-path cost increase must be justified by
  prompt/worker evidence and reduced where possible.

Fix the existing prompt evaluator if it accepts a contradictory expansion merely
because another section quotes the instruction. It must assess the full prompt
contract and reject invalid/absent evidence quotes through existing validation.

## PR 7: training and rollout only after semantics stabilize

Use the accepted decision contracts and cases with the existing local-classifier
plan. Do not make new training infrastructure or a larger model a prerequisite
for PRs 2 through 5.

1. Accumulate disagreements and accepted repairs as candidate training examples.
2. Review gold semantics before training; retain task/family-grouped held-out data.
3. Train only decision kinds whose remaining errors justify it. Compare updated
   questions/examples with training using the same frozen evaluations.
4. Calibrate acceptance/abstention thresholds per decision kind; retain provider
   fallback. Do not use a fabricated confidence number to resolve an ambiguity.
5. Promote incrementally and keep revisioned rollback for each decision kind.
   Monitor final-prompt failures alongside local classification scores.

## Verification and delivery for the implementation agent

Each PR should contain a concrete before/after example, the decision change, its
effect on the final prompt, focused behavior tests, and measured evaluation results
when runtime behavior changes. Run the repository-required formatting, lint,
type, workflow-definition/scenario validation, and test suites before pushing.
Use the shared environment with `PYTHONPATH` pointing to the current worktree;
do not create an environment or run routine dependency synchronization.

For schema changes, enumerate every producer and consumer, update serialized
fixtures, and demonstrate stale decision/context fingerprints are rejected.
Tests that supply expected model output validate compilation, not live classifier
quality; label those separately from live semantic evaluations.

Maintain a short progress table in this document or a linked report containing
the PR, fixed failure family, baseline/candidate revisions, prompt result, worker
result where measured, and latency impact. Human review should concentrate on
label definitions, unresolved semantic choices, and evaluation disagreements.

The work is complete when the generic pipeline preserves mandatory relationships
and scopes, represents materially unstated dimensions honestly, produces useful
and consistent assumptions when needed, and shows improved final prompts and
non-regressing worker outcomes across the fixed task and held-out tasks. Another
trace artifact or a new enum alone does not satisfy those criteria.
