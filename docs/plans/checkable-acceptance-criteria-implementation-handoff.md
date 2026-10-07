# Implementation handoff: derive useful acceptance criteria from instructions

## Objective, boundaries, and execution instructions

Improve the generic instruction-to-prompt pipeline so the coding worker receives
observable acceptance criteria that preserve the instruction's meaning. A useful
criterion identifies what to exercise and what to inspect, and distinguishes a
correct implementation from a plausible incorrect implementation. Merely repeating
a requirement in Given/when/then syntax does not meet that standard.

The next implementation work following the observed post-implementation failures
is specified in the
[prompt quality recovery handoff](prompt-quality-recovery-implementation-handoff.md).
That handoff supplies captured failures, current interfaces, and prompt-only
acceptance checks. Inspect the current implementation before using this document's
historical seven-PR sequence.

This is an implementation specification, not an implemented feature. Execute the
seven PRs below in order. Each PR must integrate its changes into the real workflow
and demonstrate its stated prompt effect; adding unused schemas or classifier
labels does not complete a step. Follow repository AGENTS.md instructions for
worktrees, the shared Python environment, verification, and review. Do not merge
your own PR. Rebase each subsequent change on the merged predecessor.

The source map below was inspected at main revision
`6f3885ae717770138727c2afee731713524a46d9`. Recheck symbols and workflow contracts at
the implementation branch base. Line numbers are not stable API identifiers.

Required boundaries:

- Powdrr owns branches, commits, PRs, and task worktrees. Route source delivery
  instructions out of product obligations. MiniSWE receives Powdrr's worker policy.
- Production interpretation uses the instruction, bootstrap, and permitted
  repository evidence. Reference solutions, validation patches, and ideal prompts
  are available only to offline evaluation; never feed them to production
  classifiers, grouping, criterion generation, or repair.
- Retain compiler-owned source spans, IDs, fingerprints, and existing routing,
  prohibition, source-faithfulness, and coverage safeguards. Do not restart the
  expensive model-selected source-quotation path.
- Context creates no obligations. A contextual sentence may explain a reference
  or ambiguity, but must not become a new required behavior through grouping.
- Fixture values illustrate requirements. They must not silently introduce
  product constraints, implementation names, or support guarantees.
- Do not build GraphQL-specific production rules. GraphQL is a development case;
  use additional domains and held-out tasks to establish generality.
- Improve actual worker prompts and measured worker outcomes. Intermediate
  classifier accuracy and provenance are diagnostics, not completion evidence.

Related plans:

- [Earlier semantic classifier handoff](classifier-semantic-quality-handoff.md):
  prior classifier work and source-meaning examples; audit existing implementation
  before duplicating any of it.
- [Source contract design](../design/source-anchored-semantic-contract-compilation.md).
- [Formatting handoff](../evaluations/prompt-formatting-structure-handoff.md).
- [Existing baseline](../evaluations/semantic-prompt-baseline-2026-10-04/README.md).

## Observed failure and target behavior

The GraphQL incremental-delivery capture on Powdrr revision
`5847fa456854d7cf6175c2bd5e1b10f080ce3cfe` contains 29 product clauses and two
delivery clauses. Twenty-eight product clauses have invariant-fallback artifacts.
The worker prompt renders most checks as source restatements. Examples:

| Source | Current prompt behavior | Required upstream change | Desired acceptance behavior |
| --- | --- | --- | --- |
| Data is accumulated across payloads, not raw deltas. | Repeats the sentence as the expected result. | Identify a sequence-dependent output rule and its contrast. | Show two payloads and assert the combined result after the second. |
| Stream path's last integer is the insertion start index. | Separate clause checks for `items` and index semantics. | Relate payload shape and list update operation. | Exercise a nonterminal start index; reject append-only handling. |
| Support nested paths through lists, null values, overwrites, and concurrent fields. | Splits into phrases such as navigating through field overwrites. | Determine modifier scope and keep ambiguous attachment explicit. | Separate named capabilities; preserve uncertainty about unspecified null traversal. |
| Extensions come from that specific payload. | Repeats the clause without successive observations. | Relate extensions to the same yielded-result contract as accumulated data. | Assert data accumulates while extensions are replaced by the current payload's value. |
| WebSocket transport forwards payloads through the existing protocol. | Adds cancellation, malformed-payload, and error-stop defaults. | Classify missing dimensions before resolving them; require evidence for needed choices. | Preserve forwarding; resolve additional behavior only when necessary and evidenced. |

Two observed generated scenarios exceeded the claim limit (accumulation had 34
candidate claims; defer merging had 41). Other fallback records include unresolved
semantic fields and an assumption/dimension mismatch. These are observed failure
categories, not proof that every source requirement was intrinsically ambiguous.

The coding run ended with a DeepInfra HTTP 402 and no completed official score.
Do not attribute coding success or failure to this prompt from that run.

Original captured artifacts, if still available:

```text
/private/tmp/pier-gql-main-20261005-01/gql-incremental-graphql-main-20261005-01/
  gql-incremental-graphql-delivery__NpEvBc6/artifacts/
    powdrr-gql-main-20261005-01-artifacts/
      implementation-prompt.md
      instruction-ledger.json
      instruction-coverage-audit.json
      semantic-contracts/<clause-id>/invariant-fallback.json
      semantic-contracts/<clause-id>/scenario-faithfulness.json
```

Task reference inputs are under
`~/code/powdrr-deep-swe/tasks/gql-incremental-graphql-delivery/`:
`instruction.md`, `solution/solution.patch`, and `tests/test.patch`.
Temporary captures can disappear. In PR 1 retain the prompt, relevant fallback
summaries, revision, instruction fingerprint, and outcome limitation in a compact
versioned evaluation fixture. If unavailable, make a new prompt-only baseline and
label its revision; do not reconstruct the old prompt from memory.

## Existing source map

| Location | Work required |
| --- | --- |
| `core/instruction_ledger.py` | Preserve source ancestry; extend split relations and qualifier attachment. |
| `core/classifier_input.py` | Assemble bounded containing/neighboring context for semantic questions. |
| `core/semantic_decision.py` | Define and version new bounded decisions; retain provider independence. |
| `workrr/semantic_contract_compiler.py` | Attach semantic interpretation to deterministic source spans; avoid treating unknown family as unknown meaning. |
| `core/semantic_contract.py` | Version and validate the additional interpretation fields. |
| `core/behavior_contract.py` | Introduce criterion quality and typed acceptance structures; change rendering. |
| `core/semantic_faithfulness.py` | Review assertion-level additions; distinguish illustrative setup from required behavior; repair overflow locally. |
| `workrr/command_catalog.py` | Register, prepare, bind, persist, and connect new stages; replace false resolved fallbacks. |
| `docs/procedrr/skill-definitions/design-interview.yaml` | Change actual atomicity, scenario-generation, review, and repair workflow contracts. |
| `core/implementation_packet.py`, `workrr/coding_agent.py` | Carry grouped requirements and criteria into the exact worker prompt. |
| `workrr/semantic_prompt_cases.py`, `workrr/semantic_prompt_variants.py` | Extend existing case validation and replay; keep variants with parents in one split. |
| `workrr/deepswe_design_evaluation.py` | Score generated prompt claims separately from reference-only desired outcomes. |

Paths above are relative to `src/powdrr_lift/` except the workflow YAML. Reuse
existing tests in `tests/test_instruction_ledger.py`,
`test_semantic_contract_compiler.py`, `test_semantic_decision.py`,
`test_behavior_contract.py`, `test_implementation_packet.py`,
`test_coding_agent.py`, `test_coding_agent_end_to_end.py`,
`test_semantic_prompt_cases.py`, and `test_deepswe_design_evaluation.py`.
Find workflow-binding and faithfulness tests by symbol before adding new suites.

Current details that must change deliberately:

1. `compile_deterministic_source_extractions` assigns the whole original source
   span to requested semantic fields. Preserve this evidence behavior; add meaning
   separately rather than pretending the span isolates a subject or output.
2. `merge_as_source_invariant` constructs a synthetic scenario and declares it
   resolved. Replace its quality semantics and worker rendering.
3. `prepare_scenario_claim_reviews` can clear provider claims on overflow;
   `finalize_scenario_claim_reviews` rejects the entire candidate. Keep limits,
   but introduce bounded splitting/repair.
4. `BehaviorScenario` v1 requires all seven behavior dimensions. That structural
   requirement must not force unsupported semantic defaults.
5. Existing consumers still require fields such as `expected_test` and
   `behavior_scenario`. Migrate through explicit adapters, not invented assertions
   that satisfy old shape checks.

## Target pipeline and compatibility strategy

```text
instruction + bootstrap
  -> deterministic ledger and sentence ancestry
  -> scope-preserving atomic splits
  -> existing routing/source decisions + semantic interpretation
  -> behavioral contract assembly
  -> bounded repository evidence for material open questions
  -> typed acceptance criteria
  -> structural + assertion-faithfulness review
  -> at most two targeted repair rounds
  -> requirement/criterion coverage report
  -> implementation packet -> exact MiniSWE prompt
```

Version persisted structures independently. Proposed names below are the default
implementation contracts, not claims that these modules or schemas exist today.
Use additive migration in early PRs; readers must accept existing v1 artifacts.
Writers use the new version only after the real workflow consumes it. A legacy
scenario is not automatically checkable merely because it contains Given/when/then.
Replayed legacy records require explicit assessment or get `source_only` status.
Never silently reinterpret an old fingerprint as a new schema's fingerprint.

Use one bounded interpretation request per source sentence/group, and one
criterion-generation request per behavioral contract. Keep current unrelated
classifiers initially; do not add one model call per schema field. Cache decisions
with source, permitted-context, provider/config, and schema revision fingerprints.
Existing deterministic rules and checks should bypass model calls where valid.

## PR 1: honest criterion quality and reproducible baseline

### Data and integration

Add `CriterionQuality` alongside current scenario serialization:

```json
{
  "schema_version": "criterion-quality-v1",
  "requirement_status": "preserved",
  "criterion_status": "source_only",
  "failure_stage": "scenario_faithfulness",
  "failure_reason": "scenario_claim_limit_exceeded",
  "repair_attempts": 0
}
```

- `requirement_status`: `preserved` or `missing`; never derive it from quality.
- `criterion_status`: `checkable`, `source_only`, or `unresolved`.
- Add `unassessed` for legacy or normally generated scenarios whose adequacy has
  not yet been reviewed, and `not_applicable` for context/process clauses. Do not
  mark the current nonfallback scenarios checkable merely because they parsed.
- `checkable`: supported operation, observation, and assertion have passed review;
  this does not mean an executable test exists or has passed.
- `source_only`: source obligation retained without a useful check.
- `unresolved`: material competing interpretations remain; include a question.
- Failure fields are nullable for accepted criteria; nonnegative repair count.

Propagate quality through scenario artifacts, implementation packets, and prompt
capture metadata. Add acceptance coverage to the existing coverage report instead
of replacing instruction coverage. Report included product requirements,
requirements with accepted criteria, source-only requirements, unresolved
requirements, and generation failures. Context/excluded clauses are not in the
product-criterion denominator. Guidance may be source-only with an explicit
nonbehavioral rationale; do not manufacture a behavior to improve the score.

Replace fallback prompt text with the preserved requirement and material open
question if present. Do not report all dimensions as not applicable to disguise a
failed analysis. Benchmark runs continue with degraded criteria. Preserve existing
strict-run behavior; quality-based stopping must be configured explicitly rather
than newly blocking all production runs.

### Tests and completion

- Fallback preserves instruction and source references, but reports source-only.
- Failed criteria cannot increase checkable coverage.
- Context/process instructions cannot enter the denominator or worker obligations.
- Legacy artifacts load; old synthetic checks are not promoted automatically.
- Prompt-only GraphQL replay shows honest quality counts without extra unsupported
  claims; compare exact worker text, not only intermediate JSON.
- Baseline package records references as offline-only and incomplete run outcome.

## PR 2: atomicity relations and modifier attachment

Extend the current atomicity response with `semantic_relations` and
`modifier_attachments`. Keep existing test-group relations separate; reuse already
implemented semantic-relation contracts where they have the required semantics.

Bounded decisions:

| Decision | Labels and meaning |
| --- | --- |
| `list_relation` | `independent_required`, `shared_predicate`, `allowed_alternatives`, `ordered_required`, `unclear` |
| `modifier_attachment` | `one_child`, `specified_children`, `entire_group`, `unclear`; child references supplied separately |
| `condition_attachment` | Same attachment labels; distinguishes which requirements are conditional |
| `reference_resolution` | `resolved`, `ambiguous`, `unresolved`; target source/child IDs supplied separately |

Every split child retains parent clause ID and original source span. Model-returned
child references use local indexes; compiler creates stable IDs and validates
indexes. Each relation cites its parent/neighbor evidence. If the model proposes
an out-of-range child or unrelated evidence, reject that relation and repair once
or preserve the original unsplit clause. Do not discard the whole requirement.

Give the analyzer the complete parent sentence, containing paragraph, and at most
one neighboring sentence on each side. Include referenced clauses by ID within
the existing bounded input budget; record truncation. Context informs reference
resolution but remains nonobligating.

Source rules:

- "A and B must reject X" shares rejection across both subjects.
- "A must reject X and support B" contains two operations on A.
- "Support CSV and JSON" requires both; "either is acceptable" permits alternatives.
- "CSV for tables and JSON for records" requires both capabilities conditionally;
  it is not permission to implement only one.
- "Optional label and initial_count" permits omission; it does not resolve null
  handling, default count, or argument combinations by itself.
- An ambiguous list attachment remains open. Do not interpret null values as
  navigation through null parents without supporting source/evidence.

Tests must verify downstream prompt claims as well as split labels. Add negative
controls for lost negation, exception transfer, misplaced modifiers, and invented
requirements from context. Finish when the actual workflow consumes these fields
and the GraphQL overwrite clause no longer acquires a navigation predicate.

## PR 3: semantic operation interpretation

Add `SourceInterpretation` to the partial contract, keeping exact-span provenance
separate. Suggested module: `core/source_interpretation.py`; its compiler/binder
may live alongside `semantic_contract_compiler.py` to reuse provider contracts.

```json
{
  "schema_version": "source-interpretation-v1",
  "source_refs": ["instruction-009"],
  "subject": "yielded result",
  "operation": "process incoming payloads",
  "affected_value": ".data",
  "rule": "accumulate across payloads",
  "contrast": "return only the current delta",
  "behavior_form": "state_transition",
  "conditions": [],
  "exceptions": [],
  "unresolved_fields": []
}
```

Semantic strings are supported interpretations, not exact quotations. Review
their entailment against source and bounded context; attach source refs and
decision fingerprints. Nullable fields mean unstated or unresolved, with a reason;
do not use fabricated placeholders such as "the source instruction" as an actor.

Decision questions:

- `behavior_form`: `interface`, `transformation`, `state_transition`, `invariant`,
  `rejection`, `compatibility`, `guidance`, `unclear`.
- `result_presence`: `explicit`, `entailed`, `unspecified`.
- `event_scope`: `single_event`, `event_sequence`, `continuous`, `unspecified`.
- `contrast_presence`: `explicit`, `absent`.
- Existing condition/exception, argument-presence, ownership, temporal, and
  polarity decisions remain authoritative; do not add contradictory parallel labels.

Unknown ontology family and unresolved repository binding are independent from
understood source behavior. Record three separate gaps: source meaning missing,
implementation binding missing, and registry label missing. Only the first can
make the source interpretation itself unresolved. An unknown family uses `other`
with retained operation/rule; do not expand the family registry with task names.

Regression pairs include accumulate/replace, payload-local/global, absent/null,
fresh copy/deep copy, callback scope/getter scope, required rejection/non-goal, and
current problem/future behavior. Check explicit stronger requirements survive;
avoiding invention must not suppress stated deep copy or write-through behavior.

Complete when interpreted roles feed contract assembly and criteria generation,
and an unregistered accumulation rule produces useful acceptance guidance.

## PR 4: assemble contracts across related clauses

Add `BehavioralContract` and deterministic validation in
`core/acceptance_contract.py`; add preparation/binding in
`workrr/acceptance_contract_compiler.py` and wire commands through the catalog.

Required fields: schema version, compiler-owned contract ID, operation description,
member requirement IDs, supporting context IDs, role interpretations, typed
relationships, unresolved questions, and fingerprint.

Relationship kinds:

- `defines_interface`: identifies an operation or result shape.
- `constrains_output`: restricts a named result/property.
- `applies_when`: adds the source condition to specified requirements.
- `exception_to`: limits specified requirements.
- `ordered_with`: source requires a sequence.
- `required_across_paths`: applies to each named integration/mode.
- `allowed_alternative`: source permits either specified outcome.

Each edge has source evidence and requirement targets. Similar terminology alone
does not justify an edge. Do not conflate shared operation, mandatory joint support,
and tests that must run in one scenario. Preserve existing validation groups.

Generate candidate groups using parent sentence, resolved references, and explicit
operation/subject matches. Confirm candidate membership in bounded model requests;
do not perform all-pairs clause comparison. Cap a request at 16 requirements;
partition larger groups by operation/path and carry explicit shared constraints.
Every included requirement belongs to at least one contract or has a source-only
record. Duplicate membership is allowed for genuine shared constraints, with IDs.

GraphQL expected groups: execution/result semantics; defer/stream updates;
HTTP multipart; WebSocket forwarding; DSL interface. Groups may share cited
constraints. Do not infer that all requirements apply to every transport.

Tests: getter versus callback views remain distinct, mode-specific requirements
retain conditions, context cannot create members, both transport paths remain
required, invalid edges are rejected, and group partitioning loses no clauses.
Complete when grouped context reaches the real criterion-generation request.

## PR 5: typed observable acceptance criteria

Define `AcceptanceCriterion` v1 in `core/acceptance_contract.py`. Required fields:
compiler-owned ID, contract ID, kind, source refs, setup, operation/events,
assertions, unresolved questions, quality, and fingerprint. Criterion forms:

| Kind | Required observable content |
| --- | --- |
| `interface` | Named API/property and availability, shape, or source-specified signature assertion |
| `transformation` | Inputs, operation, output observation, expected relation |
| `state_transition` | Initial state, events, observations at relevant steps |
| `invariant` | Source population/condition and property checked over it |
| `rejection` | Source invalid condition, operation, source-specified failure assertion |
| `compatibility` | Existing evidenced behavior and expectation preserved by the change |

Assertions contain ID, observation, relation (`equals`, `contains`, `absent`,
`raises`, `satisfies`, `unchanged`), expected value/property, source refs, and basis.
`satisfies` requires a concrete predicate; "works correctly" is invalid. These are
behavior descriptions, not executable code. Never accept generated shell commands,
test selectors, imports, or unsafe fixture execution as part of this schema.

Default per generation request: up to four criteria and four assertions per
criterion. Partition requirements into subsequent bounded requests if necessary;
do not drop overflow to stay within limits. Generate for interface and specified
normal behavior first, then source-named boundaries and interactions. Do not
enumerate every possible cross-product or fill all seven old behavior dimensions.

Example (illustrative values; operation/path behavior is source-derived):

```json
{
  "schema_version": "acceptance-criterion-v1",
  "kind": "state_transition",
  "source_refs": ["instruction-009", "instruction-011", "instruction-015"],
  "setup": {"initial_data": {"users": [{"name": "A"}]}},
  "operation": "process incremental delivery payloads",
  "events": [
    {"incremental_item": {"path": ["users", 0], "data": {"age": 30}}}
  ],
  "assertions": [
    {
      "observation": "next_result.data",
      "relation": "equals",
      "expected": {"users": [{"name": "A", "age": 30}]},
      "basis": "source_derived"
    }
  ]
}
```

Compiler adds IDs, fingerprints, quality, and assertion source refs after binding;
the example omits compiler-owned fields. Do not infer unsupported null-parent
creation, list padding, object identity, error accumulation, or custom-scalar
handling from this example.

Validate structure deterministically. Validate expected semantics using the
assembled contract and permitted evidence. Also ask one bounded adequacy question
per criterion: identify a plausible incorrect behavior and whether this assertion
distinguishes it. Store the answer in evaluation/diagnostics; do not inflate the
worker prompt with counterexamples. A fluent judge endorsement alone is not proof
of usefulness; PR 7 tests the judge using deliberately wrong criteria.

Coverage maps each requirement to the assertions that exercise it. One assertion
may cover several clauses, but only with reviewed support. An empty incremental
array and a hasNext-only payload are distinct source cases; a combined clause
must not be marked fully covered after checking only one.

Completion tests: circular criteria fail; missing observations fail; preserved
conditions survive; sequence rules assert intermediate/final state; one criterion
can cover output shape plus behavior; interface checks do not require invented
fixture inputs; omitted/null cases remain distinct.

## PR 6: assertion review, local repair, and necessary choices

Extend faithfulness review with assertion IDs and these categories:
`source_supported`, `repository_supported`, `illustrative_setup`,
`decision_required`, `unsupported`, `contradicts_source`.

Review whole behavioral assertions, not every literal leaf of fixture JSON. Values
such as names and ages are illustrative; exact index/path/output relationships
carry semantics. Setup is not exempt from review: record why it stays within the
source-supported domain. Attach references to repository-backed claims, including
location and repository revision/fingerprint.

Repair algorithm:

1. Structural failure: return field-specific errors for bounded regeneration.
2. Review failure: retain supported assertions; remove or revise failed ones.
3. Overflow: partition assertion units or regenerate a smaller candidate. Do not
   raise the global claim limit as the primary remedy.
4. Regenerate only missing requirement coverage with contract context and failure
   codes. Review all changed/new assertions; unchanged assertions may reuse current
   evidence only when dependency fingerprints match.
5. Permit at most two repair rounds per contract after initial generation, using
   the same per-request bounds. At exhaustion retain valid criteria and emit
   source-only/unresolved records for the remaining requirements.
6. Persist attempt reason, changed assertion IDs, and terminal quality. Do not
   transform a repair failure into a resolved invariant.

Dimension state must distinguish `specified`, `unspecified`, `not_applicable`,
and `needed_for_implementation`. Not mentioned is not equivalent to not applicable.
Resolve unspecified behavior only if the implementation must choose among outcomes
that materially affect correctness or compatibility. Reuse existing evidence and
uncertainty policies rather than adding an independent defaults policy.

Decision records contain question, affected requirement/assertion IDs, alternatives,
selected interpretation, source constraints, basis/evidence, confidence, and
remaining uncertainty. Prefer local repository conventions; claim external standards
only with identifiable sources. Conservative choices with insufficient evidence
remain labeled assumptions. They cannot override explicit requirements and cannot
be counted as source-supported acceptance coverage.

Migration: keep `BehaviorScenario` v1 readers. Adapt new criteria into legacy
containers only where the consumer needs a wrapper, with explicit quality and
assumption status. Update relevant consumers before removing the old mandatory
seven-dimension generation rule. Do not insert manufactured defaults to make the
adapter validate.

Tests: overflow retains supported checks; fixture literals do not exhaust claim
budgets; an unsupported cancellation claim is removed without losing forwarding;
assumptions never overwrite source results; stale evidence cannot be reused;
two-round exhaustion terminates with accurate quality; clarify and benchmark
policies retain their intended behavior.

## PR 7: final rendering and automated evaluation

Render one section per contract: concise required behavior, numbered observable
criteria, necessary repository decisions/assumptions, and material open questions.
Use IDs in artifacts and minimal anchors in worker text. Avoid classifier labels,
generic Given/when filler, standalone obligation-ID lists, and repeated source
paragraphs. Preserve prohibitions, conditions, named execution paths, and source-only
requirements even when deduplicating text. Compare semantics across the entire
prompt; a correct source quote does not excuse a contradictory later assertion.

Ensure `implementation_packet.py`, coding-agent prompt construction, request
fingerprints, exact prompt capture, repair prompts, and downstream review packets
consume the same criteria. Do not improve only `implementation-prompt.md` while
the actual MiniSWE request uses older content. Assert exact captured request text
in an end-to-end workflow test. Preserve Powdrr's worker/delivery policy.

Extend the existing corpus instead of creating an unrelated evaluator. Minimum
new regression matrix:

| Case | Required acceptance effect | Forbidden effect |
| --- | --- | --- |
| Accumulate across events | Second observation includes earlier values | Delta-only check counted as adequate |
| Per-event metadata | Current metadata observed independently of accumulated state | Metadata accumulation invented |
| Indexed updates | Specified index affects placement | Append-only result accepted |
| Optional argument | Omission supported | Null/default semantics invented |
| Required rejection | Specified invalid combination rejected | Capability treated as a non-goal |
| Conditional modes | Each mode retains its own behavior | Either mode alone satisfies both |
| Fresh mapping | Required fresh mapping preserved | Recursive copying inferred |
| Explicit deep copy | Nested independence asserted | Strong requirement weakened |
| Callback/getter scopes | Distinct observations retained | Shared vocabulary conflates scopes |
| Current limitation | Remains context | Becomes an obligation |
| Lifecycle persistence | Source duration preserved | Identity/write-through inferred |
| Process instruction | Powdrr handles delivery | MiniSWE told to create branches/commits |
| Guidance | Preserved at appropriate strength | Mandatory runtime behavior invented |

Include positive controls for stronger explicitly stated behavior. Generate
paraphrases and contrast pairs only after grouping base cases into development and
held-out splits. Keep variants and parent tasks together. Hold out whole task/domain
families where practical. Review synthetic annotations on disagreement; never
treat generated annotations as automatically correct.

Add criterion mutations: wrong expected value, removed condition, append instead
of insertion, accumulated metadata, stopped continuation, invented identity, and
unsupported default. The evaluation must reject these failures even when the
source is quoted elsewhere. Report missed mutations separately from prompt quality.

Prompt-level evaluation distinguishes source-explicit, source-entailed,
repository-supported, and reference-only desired behavior. Reference-only details
can expose performance gaps, but do not change source gold labels. Record prompt
criterion evidence and whether the reference validation directly exercises it.
Do not promise a hidden-test outcome from source coverage alone.

Reproduce captures for GraphQL and the existing state-machine, ytt, and skrub
examples where available. Use development/held-out assignment consistently; a
task used to tune prompts cannot also be reported as held-out. Record source,
revision, configuration/provider, seed when supported, raw actual worker prompt,
criterion report, elapsed time, call count, and usage when supplied. Missing token
usage is unavailable, not zero.

After prompt checks pass, run paired coding workers with the same permitted inputs,
worker model, repository base, budget, and validation environment. Preserve the
real agent boundary and hide reference patches. Compare current versus improved
prompt on one development and at least one held-out task; repeat only when results
are inconclusive or regressions require investigation. Capture generation cost
separately from worker cost. Report raw verifier outcomes and infrastructure
failures; incomplete billing/network runs provide no correctness score.

Completion gates:

- No existing critical source-preservation or prohibition regression.
- Deliberate critical criterion mutations are detected in the regression suite.
- No source-only restatement is scored as checkable.
- The named source-specified behaviors have concrete reviewed checks or explicit
  unresolved/source-only records, with no silent losses.
- GraphQL produces useful sequence/index criteria rather than 28 restatement
  fallbacks; measure and report the new exact count rather than choosing a score.
- Paired prompts improve observed acceptance usefulness across multiple domains;
  report worker results without claiming general success from a small sample.
- Generation time/call counts are reported. Use batching/caching and bounded
  repair; investigate increases instead of concealing them with coverage scores.

## Verification, PR contents, and handoff checkpoints

Each implementation PR includes schema/version notes, new decision definitions,
workflow binding changes, migration behavior, focused regressions, and at least
one before/after worker-prompt excerpt grounded in an unchanged source. PRs 1-4
may use prompt-only replays. PRs 5-7 must demonstrate the new criteria reaching
the actual worker request. Store compact evaluation artifacts; avoid raw secret
configuration and bulky unrelated logs.

Run focused tests during development. Before pushing, complete repository-required
formatting, lint, type checks, full tests, and deterministic workflow verification,
using the shared environment and the current worktree's source imports. Recheck
`.github/workflows/ci.yml` and AGENTS.md for current requirements. Do not run live
provider/benchmark tests as an accidental part of ordinary unit verification.
Document any baseline or infrastructure failure precisely.

After every merged PR, update this checklist and record its PR/revision:

- [ ] PR 1: accurate quality statuses, fallback rendering, and baseline fixture.
- [ ] PR 2: scope-preserving splits wired into workflow and prompt regressions.
- [ ] PR 3: source interpretation usable without ontology/repository resolution.
- [ ] PR 4: validated grouped contracts reaching generation.
- [ ] PR 5: typed assertions and requirement-to-assertion coverage.
- [ ] PR 6: bounded local repair and necessary, evidenced decisions.
- [ ] PR 7: actual worker rendering, automated mutation checks, paired evaluation.

At each checkpoint answer: what did the prompt previously say, what does it say
now, which upstream decision changed, what evidence supports that interpretation,
and what remains unresolved? This is the minimum evidence for declaring progress.
