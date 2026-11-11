# Structrr User Stories and Behavioral Testing

Status: story contracts, identity allocation, graph validation, digests,
projection/rebase, bootstrap preservation and queries are implemented. The second
slice adds interactive story authoring, bounded semantic readiness reviews,
actual-user digest confirmation and proposal publication. The third slice carries
pinned stories through planning, multi-PR integration allocation, execution preflight
and repair, with approved-amendment impact reports. Behavioral test authoring and
execution evidence remain planned. See the
[story contracts](../design/structrr-user-stories.md) and
[interactive story design](../design/structrr-story-interview.md) and
[story planning](../design/structrr-story-planning.md).
Date: 2026-11-06

## 1. Objective and accepted decisions

Make customer behavior a durable design contract. The design chat agent starts
with English prose, interviews the user, and refines the story into connected
steps and alternatives. Each step has a short, technical English description.
Planning, implementation, generated tests, review, and execution receipts must
reference the same recorded behavior.

The following decisions are settled:

- The interview may propose assumptions and confirm them with the user.
- The interview resolves all material behavioral ambiguity before a story is
  ready for planning. Silence is not confirmation.
- User stories belong in Structrr and evolve through ordinary proposed Structrr
  diffs. Discoveries during implementation or execution can initiate such diffs.
- Steps have durable IDs within their story. Changes can add, modify, or remove
  steps and transitions; removed identities are never reassigned.
- Tests reference the particular story revision, steps, alternatives, and
  outcomes they verify. A passing test command alone does not verify a story.
- Existing behavior scenarios, obligations, required test cases, and verification
  receipts remain the execution contracts. Extend them to consume stories rather
  than introducing another test-generation pipeline.

This plan implements these decisions. It does not select a new BDD framework,
require Gherkin, create a hosted service, or redesign the overall agent/service
split. Private interviewing, compilation, semantic review, and test policy stay in
the service. The agent gathers repository evidence, presents interview questions,
executes tools and MiniSWE, and transports typed requests/results.

## 2. Current implementation and concrete integration points

Inspect these files before implementing their corresponding work packages:

| Area | Existing implementation | Required extension |
|---|---|---|
| Design interview | `packages/service/src/powdrr_service/resources/processes/design-interview.yaml` | Story discovery, graph refinement, assumption confirmation, readiness gate |
| Interactive feature specification | `packages/service/src/powdrr_service/resources/processes/specify-a-feature.yaml` | Show and confirm a readable story graph; support later additions and amendments |
| Compiler-owned design IDs | `packages/service/src/powdrr_service/planning/feature_obligation.py` | Compile story references into obligations and required test cases |
| Behavioral scenarios | `packages/service/src/powdrr_service/behavior_contract.py` | Retain graph path, revision, and observation references |
| Specification authoring | `src/powdrr_lift/core/feature_planning_specification.py` | Emit and validate story primitives in proposals and input templates |
| Bootstrap and section versions | `packages/service/src/powdrr_service/structrr/bootstrap.py` | Preserve declared stories; validate sections and references |
| Snapshot hashing | `packages/service/src/powdrr_service/structrr/snapshot.py` | Normalize new unordered collections and compute story contract digests |
| Structural comparison/rebase | `packages/service/src/powdrr_service/structrr/rebase.py` | Compare story primitives and detect concurrent edits |
| Proposed and observed changes | `packages/service/src/powdrr_service/structrr/proposal.py`, `actual_diff.py`, `candidate_comparison.py` | Carry story operations and classify declaration changes separately from implementation evidence |
| Plan diff and projection | `src/powdrr_lift/core/plan_diff_specification.py`; `packages/service/src/powdrr_service/planning/structrr.py`, `multipr_structrr.py` | Allocate story changes and preserve them through intermediate/final snapshots |
| Planner | `packages/service/src/powdrr_service/planning/source.py`, `assembly.py`, `driver.py`, `compiler.py`, `plan_models.py`, `plan_document.py` | Preserve story coverage allocation across PRs |
| Plan repair | `packages/service/src/powdrr_service/planning/repair_service.py` | Re-plan work affected by an approved story amendment |
| Feature execution adapter | `src/powdrr_lift/workrr/feature_endpoint.py` | Transport story context, enforce the readiness gate, collect actual execution evidence |
| Test authoring | `packages/service/src/powdrr_service/verification/execute_pr_test_authoring.py` | Supply reviewed story paths and expected observations to the existing author |
| Test obligations and results | `packages/service/src/powdrr_service/verification/test_obligations.py`, `execute_pr_test_quality.py`, `execute_pr_test_validation.py`, `execute_pr_acceptance.py`, `probe_results.py` | Validate story references, assertion evidence, test identity, outcomes, and freshness |
| Probe and correction consumers | `packages/service/src/powdrr_service/verification/postrun_probes.py`, `probe_repair.py`, `execute_pr_test_corrections.py` | Preserve the same story references through existing probe execution and MiniSWE correction flows |
| Verification linkage | `packages/service/src/powdrr_service/structrr/verification_contract.py`, `verification_obligations.py`, `obligation_evidence.py`, `verification_health.py` | Compile required story coverage and report current evidence |

These are integration targets, not a claim that every file needs edits. Trace
the active call paths on the implementation branch. Search for section whitelists,
literal unions, schema enums, serializers, templates, query filters, packaged
resources, and public exports; update every active consumer of the changed
contract. Do not leave a second path silently dropping stories.

The current execute-PR author assigns ordered `AC-01` identifiers and asks for
`# Covers:` comments. Those identifiers are local to one acceptance list. They
must not become durable story identities or sufficient proof of story coverage.

## 3. Structrr representation and storage

### 3.1 Four first-class collections

Add four optional sections to the existing Structrr document family:

1. `user_stories`: customer purpose, scope, preconditions, entry point,
   confirmation, and durable identity allocation state.
2. `user_story_steps`: actions, decisions, and terminal outcomes.
3. `user_story_transitions`: directed edges, each with a condition and stable ID.
4. `user_story_cases`: finite execution paths and their expected observations,
   used to compile test obligations.

Stories, steps, transitions, and cases are addressable Structrr subjects. They
participate in snapshot projection, queries, hashing, add/modify/remove
operations, diffs, rebase, validation, and review. They are not untyped text in
`guidance`, entities inferred from function names, or a parallel private database.

Store the canonical records in the repository's existing reviewed Structrr
specification/proposal sources. Materialize current state through the existing
bootstrap plus accepted changelist projection. Do not require users to edit both
a story file and a duplicate Structrr inventory. An interview checkpoint may
store a draft; that checkpoint is not the canonical approved story.

Existing repositories with no story sections remain readable. An omitted section
means no stories are declared; it does not certify that customer behavior has
been modeled. New interactive customer-feature designs must create or explicitly
reuse a ready story. Internal maintenance can declare that no customer behavior
changes, with a reason subject to review. Do not fabricate a user story for every
internal edit or silently grandfather new customer features.

Use the current section-version mechanism to register version 1 of all four
sections. Extend the current proposal/changelog schema and typed models together;
do not introduce a compatibility shim or a second operation vocabulary. Old
records without stories parse with empty collections; new records are validated
strictly, and unsupported section versions fail explicitly.

### 3.2 Normative field contracts

All records reject unknown fields. All IDs and references are compiler-assigned
or resolved against a supplied catalog, never invented as free-form LLM output.
Descriptions are English prose, nonempty and free of unresolved placeholders.
Set a generous technical-description limit of 2,000 characters. Length alone is
not a reason to truncate behavior; split a step when it contains multiple actions.

| Record | Required fields | Optional fields |
|---|---|---|
| Story | `id`, `title`, `narrative`, `actor`, `goal`, `preconditions`, `scope`, `entry_step_id`, `source_refs`, `identity_state` | `invariant_refs`, `related_modules`, `related_story_refs`, `confirmation` |
| Step | `id`, `story_id`, `kind`, `description`, `source_refs` | `action`, `expected_observations`, `functionality_refs`, `outcome`, `state_effects` |
| Transition | `id`, `story_id`, `from_step_id`, `to_step_id`, `condition`, `source_refs` | `description` |
| Case | `id`, `story_id`, `description`, `initial_conditions`, `transition_ids`, `expected_observations`, `source_refs` | `input_examples`, `coverage_tags` |

Lists may be empty only where meaningful: a new story can have no preconditions
or existing functionality bindings; a case must have observations; an outcome
cannot have outgoing transitions. The parser enforces the kind-specific rules
below instead of accepting arbitrary combinations of optional fields.

`identity_state` requires `next_step`, `next_transition`, `next_case`, and
`next_observation` positive integers plus an array of unique `retired_ids`.
Counters must exceed every allocated local ordinal, including retired ordinals.
Use story-specific ID validation; do not apply workflow-slug rules that reject
the namespace separators in these IDs. `related_story_refs` entries contain
`story_id` and `relationship`, where relationship is `uses` or `related`.
Track the referenced story digests in review and execution evidence, rather than
recursively hashing linked graphs. This avoids circular digest dependencies.

Case `expected_observations` is an array of existing observation IDs, not copied
oracle text. `input_examples` is an array of typed fixture mappings; descriptions
of setup remain in `initial_conditions`. A case must include every terminal
observation on its path; additional intermediate observations may be covered by
separate cases. Required observations need coverage even if their step is not
terminal. Arbitrary sequence execution and arbitrary shell strings are never
part of a graph primitive; execution uses the existing validated tool adapters.

`source_refs` point to existing durable instruction-ledger clauses, confirmed
interview answer records, or reviewed source anchors. Preserve original prose
and the interview decisions in those records. Do not copy private chat text into
every primitive. Never store secret values in stories or example inputs.

`functionality_refs` are typed `{section, subject_id}` references to real Structrr
entities/features/interfaces supported by the existing catalog. A source symbol
may be associated through existing source bindings. This is a many-to-many
relationship: a step can use several features; one feature can support several
stories. A missing implementation binding is permitted during design, but is
reported as unmapped and must be resolved by planning/implementation or explicitly
justified before final verification.

Step kinds are exactly `user_action`, `system_action`, `decision`, and `outcome`.
User/system actions require an `action` with `actor`, `operation`, and `input`
descriptions. An outcome requires `outcome` equal to `success`, `failure`, or
`cancelled`, and at least one expected observation. Decisions have no action.
Any step can declare externally observable state effects and observations.

Observation records contain a durable story-local `id`, `description`, and
`boundary` description. State effects specify `subject`, `before`, `after`, and
`persistence` in technical English. Story-wide invariants use existing Structrr
invariant references. Keep internal algorithms out unless the user requires them.

Transition `condition` is `{kind, description}`. Kinds are `always`, `predicate`,
and `otherwise`. Only one unconditional edge is allowed from a nondecision step.
A decision has at least two outgoing edges; a fallback is explicit and describes
what it covers. Exhaustive explicit predicates are allowed without a fallback
after semantic review. No model-generated executable predicate language is
required. Where input examples include machine-readable values, normal typed
schema validation applies.

Use `coverage_tags` from the bounded catalog `normal`, `boundary`, `invalid`,
`recovery`, `cancellation`, and `interaction`. Tags help select tests; they do not
define behavior or prove coverage. Do not force every tag onto every story.

### 3.3 Example: a complete small story graph

This example omits no graph nodes or transitions. The confirmation digest below
is explanatory; the compiler must compute a real value.

```yaml
user_stories:
  - id: story:session-resume
    title: Resume interrupted planning
    narrative: A maintainer resumes planning without losing accepted decisions.
    actor: Repository maintainer
    goal: Complete interrupted planning while retaining valid accepted answers.
    preconditions: [A planning session and its checkpoint exist.]
    scope: [Resume an existing session with unchanged planning inputs.]
    entry_step_id: story:session-resume:step:1
    source_refs: [interview:resume:answer:1]
    identity_state:
      next_step: 5
      next_transition: 4
      next_case: 3
      next_observation: 3
      retired_ids: []
user_story_steps:
  - id: story:session-resume:step:1
    story_id: story:session-resume
    kind: user_action
    description: The maintainer requests resume using an existing session ID.
    action: {actor: Maintainer, operation: Resume planning, input: Existing session ID}
    source_refs: [interview:resume:answer:1]
  - id: story:session-resume:step:2
    story_id: story:session-resume
    kind: decision
    description: Determine whether the session checkpoint can be read.
    source_refs: [interview:resume:answer:2]
  - id: story:session-resume:step:3
    story_id: story:session-resume
    kind: outcome
    description: Complete remaining planning questions and publish the plan.
    outcome: success
    expected_observations:
      - id: story:session-resume:observation:1
        description: The published plan preserves accepted decisions and includes newly completed answers.
        boundary: Published plan and persisted session checkpoint
    source_refs: [interview:resume:answer:1]
  - id: story:session-resume:step:4
    story_id: story:session-resume
    kind: outcome
    description: Report checkpoint unavailability and stop resume without changing accepted decisions.
    outcome: failure
    expected_observations:
      - id: story:session-resume:observation:2
        description: Resume reports the unavailable checkpoint and preserves the session's accepted decisions.
        boundary: Command result and persisted session state
    source_refs: [interview:resume:answer:2]
user_story_transitions:
  - id: story:session-resume:transition:1
    story_id: story:session-resume
    from_step_id: story:session-resume:step:1
    to_step_id: story:session-resume:step:2
    condition: {kind: always, description: Resume was requested.}
    source_refs: [interview:resume:answer:1]
  - id: story:session-resume:transition:2
    story_id: story:session-resume
    from_step_id: story:session-resume:step:2
    to_step_id: story:session-resume:step:3
    condition: {kind: predicate, description: The checkpoint can be read.}
    source_refs: [interview:resume:answer:2]
  - id: story:session-resume:transition:3
    story_id: story:session-resume
    from_step_id: story:session-resume:step:2
    to_step_id: story:session-resume:step:4
    condition: {kind: otherwise, description: The checkpoint cannot be read.}
    source_refs: [interview:resume:answer:2]
```

The normal case traverses transitions `[1, 2]` and asserts observation 1. The
failure case traverses `[1, 3]` and asserts observation 2, using full IDs in stored
records. Both cases are required. If execution details inside step 3 introduce
material alternatives, the interview must split that step before confirmation.

## 4. Identity, revision, and confirmation

Allocate a story ID once when the draft is created. Allocate local step,
transition, case, and observation IDs using persisted monotonically increasing
counters. The compiler allocates them through typed edit operations and persists
the allocation with the edit checkpoint. Replay reuses that allocation. Do not
derive IDs from list position, current description, source line, or graph order.

Persist removed local IDs in `identity_state.retired_ids`; retain complete
before-records in normal Structrr change history. Removing a whole story retains
its ID in repository change history. An imported ID must be checked against
current state and accepted history before allocation. Names are editable labels;
they are not identity.

Maintain two distinct digests:

- `story_contract_digest`: hash of canonical behavior fields in the story and
  its steps/transitions, including observation definitions, invariant references,
  and functionality references. Exclude confirmation, allocator counters,
  transcript pointers, and derived test cases. Sort unordered records by ID;
  preserve meaningful list order, such as a case's transition walk.
- `case_digest`: hash of the case, its story contract digest, concrete fixtures,
  and required observations. A changed case invalidates its receipts without
  pretending the customer's behavior changed.

Keep titles and narrative in the story contract digest in version 1. Even wording
changes require confirmation refresh; optimize presentation-only changes only
after evidence supports a safe rule. Never use an LLM judgment to silently reuse
an old confirmation digest after a changed contract.

Confirmation is `{contract_digest, decision_ref, authority}`. For interactive
design, authority is `user_confirmation`; the adapter supplies an actual recorded
user response referencing the displayed candidate digest. A model cannot create
this authority by returning `confirmed: true`. If the displayed design changes
before the response arrives, the response cannot approve the changed version.

Existing reviewed source can establish authority `reviewed_source` with immutable
source revision and review references. An inferred story reconstructed from code
has no confirmation authority. Benchmark normative defaults and unresolved
assumptions must not be mislabeled as confirmed user behavior. Preserve existing
benchmark isolation: hidden verifier tests are never design or probe input.

Derived readiness is `draft`, `needs_confirmation`, or `ready`. A ready story has
valid structure, current semantic review, no unresolved applicable dimensions,
and valid confirmation authority for exactly its contract digest. Store review
and confirmation evidence durably; derive the status rather than trusting a
model's status label. Derived cases require review against the confirmed story
but do not require repeating the user interview unless they expose new behavior.

## 5. Validation rules and error behavior

### 5.0 Compiler interface to implement

Expose these pure service functions (names are the intended implementation API):

```python
parse_story_catalog(snapshot) -> StoryCatalog
validate_story_catalog(catalog, reference_catalog) -> StoryValidationReport
allocate_story_edit(catalog, edit_request, checkpoint_key) -> AllocatedStoryEdit
apply_story_edits(catalog, allocated_edits, expected_snapshot_digest) -> StoryCatalog
story_contract_digest(catalog, story_id) -> str
compile_story_cases(catalog, story_id, budget) -> StoryCaseCompilation
compile_story_test_obligations(catalog, selected_case_ids) -> StoryObligationSet
compare_story_contracts(before, after) -> StoryImpactReport
validate_story_test_links(obligations, links, collected_tests) -> StoryTestLinkReport
```

Use typed dataclasses and existing mapping serializers. The signatures above
omit concrete annotation imports for readability; do not implement untyped public
functions. `StoryCatalog` contains indexed versions of the four collections.
Reports distinguish structural errors, semantic findings, uncovered targets, and
execution failures. `StoryCaseCompilation` always contains an explicit `complete`
boolean plus uncovered IDs and budget diagnostics. Empty results are successful
only when there genuinely are no requested targets.

Draft edit requests are typed `add_step`, `modify_step`, `remove_step`,
`add_transition`, `modify_transition`, `remove_transition`, and corresponding
story/case operations. Semantic model responses contain prose and selections
from a compiler-provided catalog. The allocator produces IDs and expected-before
fingerprints; the normal Structrr proposal compiler produces final section
operations. This is an authoring interface, not an alternative persisted diff
format. Batch edits can temporarily leave a draft graph incomplete, but only a
complete projected graph can pass readiness or be published as a valid proposal.

Semantic review artifacts require `story_id`, `contract_digest`,
`dependency_digests`, `reviewer_policy_version`, and `findings`. Each finding has
`subject_refs`, `dimension`, `classification`, `explanation`, and `source_refs`.
Unresolved findings also have a concise proposed interview question. Persist the
artifact digest in the readiness evidence. The review adapter records provider
failure separately; it cannot manufacture an empty findings list on failure.

`StoryImpactReport` contains added/removed/modified subject IDs, affected story
IDs, invalidated confirmation digests, affected case/test/plan refs, and conflict
records. `StoryTestLinkReport` contains valid links, unresolved selectors,
unproven observation IDs, and stale references. Deterministic validity is separate
from the semantic assertion-quality review status in the final receipt.

### 5.1 Deterministic validation

Implement a service-owned `structrr/user_stories.py` containing typed records,
parsing, canonical serialization, graph indexes, and structural validation.
Add a focused `user_story_validation.py` if the validator outgrows that module.
Reuse existing schema/error reporting conventions.

Reject or explicitly block readiness for:

1. Unknown fields, unsupported versions, duplicate IDs, invalid source refs,
   unresolved typed functionality/invariant references, or reused retired IDs.
2. Steps/transitions/cases with an absent or inconsistent owning story.
3. Missing entry point, unreachable steps, edges leaving outcomes, dead-end
   nonterminal steps, or reachable steps from which no outcome can be reached.
4. Illegal kind/field combinations, missing action inputs or outcome observations,
   multiple unconditional successors, or malformed decision alternatives.
5. References removed without rewiring/removing incoming edges and updating cases
   and test obligations in the same candidate projection.
6. A case whose transition sequence is not a connected walk from the entry point
   to a terminal outcome, or whose referenced observations are absent.
7. A confirmation/review whose digest differs from the projected story contract.
8. A required coverage target with no planned case, or generated test references
   that resolve only to another revision or story.

Graph validation operates on the entire projected transaction, not after each
operation. Adding nodes and their edges must be possible atomically. Validate
both each planned intermediate PR snapshot and the complete projected plan.

Loops are permitted. Interview and case review must specify retry/exit behavior;
finite test cases may repeat transition IDs. Do not require enumerating every
possible path or assume graph reachability proves termination. Parallel behavior
is represented in version 1 by a system-action contract with explicit observable
ordering/concurrency constraints and separate interaction cases. Do not falsely
encode simultaneous actions as a prescribed sequential order. A full fork/join
graph language is outside this implementation.

Emit stable error codes with story ID, subject ID, field path, and corrective
action. Suggested codes: `story_reference_unknown`, `story_id_reused`,
`story_step_unreachable`, `story_outcome_unreachable`, `story_branch_invalid`,
`story_case_walk_invalid`, `story_confirmation_stale`, `story_review_unavailable`,
`story_coverage_missing`, `story_test_reference_stale`.

### 5.2 Semantic review and interview readiness

Deterministic checks cannot prove that two English predicates are disjoint or
that all real-world conditions are covered. Use bounded semantic reviews for
one story or one decision neighborhood at a time. Check:

- Fidelity to original prose and confirmed assumptions; no invented behavior.
- A single concrete action/result per step and unambiguous actor/input/output.
- Branch overlap, completeness, and an explicit outcome for relevant failures.
- State before/after, persistence, repeated actions, retries, cancellation,
  authorization, timing, and external dependency behavior where applicable.
- Consistency with related stories, shared functionality, and invariants.
- Observable assertions that could distinguish correct behavior from a plausible
  defective implementation.

Ask Jev for a small categorization such as resolved/ambiguous/contradictory/
not-applicable; ask the LLM separately for short explanations and proposed edits.
Use the existing typed decision API, bounded catalogs, and structured tools.
The compiler owns IDs, references, graph edits, and digests. Do not ask a model
to return a whole graph with arbitrary IDs as one complex JSON answer. Tool/schema
errors are execution failures, not a resolved finding or an empty review.

Record `resolved`, `not_applicable` with a rationale, or an interview question for
each materially applicable dimension. Silence is not not-applicable. Avoid asking
about details already resolved elsewhere in the story or confirmed sibling
clauses. Review a specific digest; invalidate it when its semantic inputs change.

## 6. Design process and later story addition

### 6.1 Initial interview sequence

1. Persist the original request in the existing instruction ledger.
2. Identify candidate customer goals; resolve whether each is a new story or an
   extension of an existing story. Retrieve relevant current Structrr context.
3. Draft actor, purpose, scope, preconditions, primary path, and outcomes.
4. Refine one step or branching decision per bounded question. Present a proposed
   assumption when helpful, explain its behavioral consequence, and confirm it
   with the user. Prefer simple choices with an ordinary free-text escape.
5. Ask about material alternative paths and state effects. Run structural
   validation after each accepted edit and semantic review on each coherent
   candidate. Fix mechanical problems automatically; ask the user about intent.
6. Display a readable flow/table with short technical descriptions, conditions,
   destinations, state effects, and terminal outcomes. Include cross-story
   consequences and proposed assumptions. Ask the user to confirm that candidate.
7. Persist exact-digest confirmation, compile source-linked story primitives, and
   generate/review cases against that confirmed behavior.
8. Publish the story proposal through the existing Structrr PR process. Planning
   can consume a validated confirmed proposal before merge when pinned to its
   proposal revision; durable approved state advances only through the existing
   accepted-change process.

Use the existing interactive design entry point and Procedrr processes. Extend
typed inputs with `story_mode: create|amend` and optional existing story reference;
do not require a separate service endpoint per operation. Draft checkpoints
contain candidate primitives, allocated IDs, question/answer provenance, review
results, and pending confirmation digest. Resume must not duplicate IDs, repeat
accepted questions with unchanged inputs, or infer approval from elapsed time.

### 6.2 Adding stories to an existing repository

Load current Structrr at the requested branch and SHA. Search by customer goal,
entry interface, linked functionality, and related modules. Show possible overlap
to the design agent. A semantic duplicate decision requires bounded review;
keyword overlap alone cannot merge stories.

A newly discovered story can describe existing functionality. It receives the
same interview and confirmation process; its tests may pass on the baseline.
Do not force a code change or a baseline test failure merely because the story
record is new. Existing code/source bindings are candidates for functionality
links, not proof that the customer's story is satisfied.

Bootstrap must preserve approved declared stories and apply accepted diffs even
when the source scanner cannot infer them. It must not invent a customer's goal
from code, delete a declared story because no function matches, or label inferred
behavior as approved. Suggestions reconstructed from code remain draft proposals.

If a story calls another story, version 1 records a typed related-story reference
and describes the observable boundary in a normal step. Do not add recursive
subflow execution semantics in this slice. Referenced-story contract changes
trigger impact review; cycles in related-story references do not imply execution
and cannot be expanded recursively into test paths.

## 7. Evolving stories through Structrr diffs

Use the existing add/modify/remove proposal envelope with the four new section
names. Keep stable step identity when modifying the same action. Splitting a
step adds replacement IDs, rewires edges, removes the old step, and retires its
ID. Combining steps follows the same rule. A removed story must remove its owned
graph/cases and retire or replace referencing test obligations explicitly.

Every amendment includes baseline snapshot digest, expected-before fingerprints,
source decision refs, and a reason. Project all operations together, validate the
result, calculate the new story digest, review affected behavior, and obtain the
required confirmation for the changed contract. Human-owned behavioral choices
return to the design interview; the coding worker cannot grant that confirmation.

For example, adding retry after checkpoint failure requires adding a retry action
step and transitions, modifying the old terminal failure flow, adding finite retry
cases, and updating test obligations. It is a design diff even if motivated by a
failed execution. The failing observation is evidence for the proposed amendment,
not authority to rewrite the intended result.

Rebase rules:

- Unchanged referenced story digests replay unchanged.
- Disjoint primitive edits may project mechanically, then must pass whole-graph
  validation and receive confirmation/review for the combined changed digest.
- Concurrent edits to the same primitive, removal of referenced subjects, or an
  edge pointing at a concurrently removed step are explicit conflicts.
- Never resolve a semantic conflict with last-writer-wins or silently redirect a
  removed ID to a different action.
- Adding an independent story must not invalidate unrelated story evidence.

Produce an impact report with changed stories, steps, transitions, observations,
cases, linked functionality, allocated plan items, required test cases, and stale
receipts. Conservatively invalidate verification for the entire changed story in
version 1. Retain historical receipts with their old digests; never relabel them
as current. Unchanged stories still require applicable execution evidence for the
current candidate; a past passing result is historical evidence, not a new run.

## 8. Planning and implementation integration

Include the confirmed story graph and selected cases in requirement context.
Preserve original requirement IDs and source refs; story refs supplement them.
Each work unit and plan item records typed `story_refs` containing story ID,
contract digest, owned step/transition IDs, and case IDs. The compiler validates
allocations and serializes them through the reviewed execution-plan document.

Stories may span PRs. Allocate functionality and tests to useful implementation
slices, then add a required integration validation item when no earlier item can
execute the complete story. Persist its dependencies. Do not mark the whole story
verified because a partial item passes. During intermediate PRs distinguish
`planned`, `implemented_partially`, `ready_for_validation`, and `verified` using
recorded work/evidence; do not mutate confirmed design status based on execution.

Targeted context includes related stories and affected functionality, not every
story in the repository. If context exceeds the existing budget, partition work
and retain shared references. Never silently truncate branches or observations.

An implementation discovery can report `implementation_defect`,
`environment_blocked`, or `design_amendment_needed` through the existing bounded
decision path, with details separately. Only an approved story diff changes the
contract. Repairs to implementation may proceed within the existing authority;
changing requested customer behavior requires the interview/confirmation route.

## 9. Cases, generated tests, and evidence

### 9.1 Derive finite coverage obligations

After story confirmation, compile candidate walks using deterministic graph
traversal with stable transition-ID ordering. Require every reachable transition,
terminal outcome, and declared observation to have at least one case. Use bounded
semantic case refinement to choose meaningful fixtures, boundary values, and
necessary interactions. Do not treat graph coverage as complete input-domain
coverage or enumerate all combinations automatically.

Use a configurable path-search budget (default 1,000 edge expansions per story).
If exhausted, return `case_expansion_incomplete` with uncovered targets and ask
the planning/design stage to partition or supply explicit finite paths. Never
drop required targets, falsely report complete coverage, or reject a valid large
story solely because automatic enumeration hit its budget. Loops require explicit
zero/one/repeated-attempt cases where those counts are meaningful to the agreed
behavior; include the declared limit/exit condition where applicable.

Each case stores a complete connected walk, initial conditions, concrete input
examples when needed, and references to observations defined by the story.
Expected values must follow the confirmed design. Case review blocks invented
requirements and untestable outcomes; missing behavioral decisions reopen the
interview rather than becoming an author's assumptions.

### 9.2 Extend existing test contracts

Add optional `story_refs` to existing required test cases, behavior scenarios,
verification contracts, and test obligations. Required when that obligation was
derived from a story. Each reference contains:

```yaml
story_refs:
  - story_id: story:session-resume
    contract_digest: sha256:<compiler-calculated-story-digest>
    case_id: story:session-resume:case:1
    case_digest: sha256:<compiler-calculated-case-digest>
    step_ids: [story:session-resume:step:1, story:session-resume:step:2, story:session-resume:step:3]
    transition_ids: [story:session-resume:transition:1, story:session-resume:transition:2]
    observation_ids: [story:session-resume:observation:1]
```

The transition sequence carries execution order; steps are resolved from the walk
and checked by the compiler. Preserve repeated edges in loop cases. One test can
cover several cases, and several tests can prove one case. All asserted coverage
links must resolve to supplied obligations; unknown IDs are errors.

Give the existing MiniSWE test author the confirmed, pinned story slice, case
fixtures, public entry-point evidence, expected observations, and required
obligation IDs. Keep current acceptance-criterion coverage for obligations that
have no story. AC IDs stay local aliases; emit durable story/case references
alongside them. Do not introduce another probe author or repair the MiniSWE code
directly. Use the existing supported author/correction workflow and preserve
authored artifacts and failure diagnostics.

Keep tests in the target repository's supported framework. The story contract
and coverage manifest are language-neutral. Extend the current Python author
first where it is the active path; route other languages through their existing
author adapters, reporting unsupported authoring explicitly where absent.

### 9.3 Record assertion-level linkage

Add a versioned, machine-readable test-link manifest emitted through a typed tool
or existing structured artifact writer. The tool validates IDs against the pinned
obligation catalog; the LLM does not have to format arbitrary JSON. Each link
records the actual collected test selector, test source digest, obligation refs,
and assertion descriptions/source spans proving particular observation IDs.

For an assertion span, the syntax adapter confirms an executable assertion or
recognized framework assertion exists in that test. Resolve final selectors from
test collection; do not accept an invented function name. Comments such as
`# Covers:` remain navigation aids and cannot alone satisfy linkage validation.
An LLM quality review checks semantic adequacy of assertions; the compiler checks
reference integrity and actual collection. Neither should claim the other proves
correctness.

Tests must enter through a supported user/API/CLI boundary or the narrowest real
integration boundary that proves the case. Mocking the behavior under test cannot
establish coverage. Controlled external dependencies may be substituted, with
the substitution boundary recorded. Inspect state through supported observable
interfaces when possible; direct database/file inspection can supplement a
persisted-state assertion but cannot replace the user action itself.

### 9.4 Execution and completion

Execution receipts bind story/case digests, test source digest and selector,
repository/candidate SHA, relevant configuration/environment, actual result,
and evidence artifacts. Preserve `passed`, `failed`, `blocked`, `not_run`, and
`stale` distinctly. Collection failure, missing assertions, skipped tests,
unavailable dependencies, or an empty author output never count as passing.

For newly implemented behavior, require a trustworthy baseline signal: behavioral
failure where an executable public boundary exists, or an explicitly classified
missing-interface condition where it does not. Import/syntax/environment errors
are not behavioral failures. For stories documenting existing behavior, baseline
pass is valid; confirm sensitivity with an appropriate negative control or a
targeted mutation when needed. Do not require every test to fail before every
change. Do not edit the agreed oracle to force a desired baseline result.

Use focused defect checks where they materially challenge the proof: bypass a
required persistence write, reverse one branch outcome, or suppress a required
error and confirm the corresponding case detects it. Perform mutations only in
isolated disposable execution workspaces; never alter MiniSWE's submitted output
to make the verification succeed. Broad mutation testing is not a prerequisite
for each PR.

Report a matrix of required transitions, outcomes, observations, cases, collected
tests, assertion review, and execution results. A story is currently verified only
when every required case's observations are established by valid links, quality
review, and passing applicable execution receipts for the pinned candidate.
Represent partial coverage explicitly. Do not turn the matrix into a single
percentage that hides an untested failure outcome.

## 10. Implementation sequence and acceptance criteria

Implement in five cohesive PRs. Each PR must include executable integration tests
for its vertical slice, updated packaged process definitions/templates where
affected, a validated changelog, formatting, lint, and configured type checks.
Do not publish placeholders that claim readiness before consumers are connected.

### PR 1 — Structrr story contracts, identity, and diffs

1. Add typed models, parser/serializer, graph indexes, durable ID allocation,
   digest computation, and structural error codes.
2. Register all four sections in bootstrap, proposal/changelog parsing, section
   versions, snapshot normalization, projection, queries, rebase, and actual diff.
3. Preserve declared story state during source bootstrap. Classify story edits as
   intent declarations; do not count them as observed implementation changes.
4. Add tests for a full graph round trip; reordered unordered collections; stable
   case path order; atomic split/remove/rewire; ID retirement/replay; graph errors;
   stale confirmation; disjoint merge and conflicting edits; old documents with
   no stories; and source bootstrap preservation.

Acceptance: a real repository snapshot plus a proposal containing story operations
projects into a validated candidate, produces the expected actual declaration
diff, and survives reload without losing stories or changing IDs/digests.

### PR 2 — Interactive story design and later additions

1. Extend current design/specification processes and input/output schemas with
   draft stories and typed compiler edits. Keep each semantic question small.
2. Add scoped story retrieval, assumption confirmation, branch refinement,
   semantic readiness review, readable graph presentation, and digest-bound
   confirmation capture.
3. Support adding a story to existing functionality and amending an existing
   story. Preserve accepted question answers/allocated IDs across checkpoint resume.
4. Prevent `normative_default`, review failure, and inferred code behavior from
   acquiring false user-confirmed status.

Acceptance: a scripted user interview begins from prose, resolves an alternative
using a proposed assumption confirmed by the user, produces a ready graph, and
publishes a normal Structrr proposal. An interrupted interview resumes correctly;
a changed candidate cannot reuse the old confirmation. A later story can reuse
existing functionality without forcing implementation work.

### PR 3 — Planning allocation and approved story evolution

1. Carry typed story refs through source requirements, work units, plan items,
   YAML serialization, compilation, Structrr proposals, and repair paths.
2. Allocate full-story integration validation across PRs and enforce valid
   intermediate projections. Include changed related-story/invariant context.
3. Build amendment impact reports and route design discoveries to the existing
   interview/proposal workflow. Refresh confirmation and stale evidence after
   approved changes; preserve historical receipts.

Acceptance: a story spanning two implementation PRs and a final integration item
retains exact references throughout. Splitting a step produces normal add/remove/
rewire operations and invalidates the affected story's evidence. A concurrent
edit/removal produces a concrete conflict rather than a silently rewritten plan.

### PR 4 — Story cases and existing test-author integration

1. Compile finite candidate walks and uncovered targets with explicit budget
   exhaustion. Review fixtures and observations against the confirmed graph.
2. Extend existing obligations, scenarios, required test cases, author prompts,
   and test quality review with durable story/case references.
3. Add typed test-link manifest writing, selector collection, assertion linkage,
   and explicit unsupported-framework results through existing adapters.
4. Keep one MiniSWE author path and retain non-story AC coverage.

Acceptance: normal, failure, and retry cases drive independently generated tests
through real public wiring. Unknown references, disconnected paths, bogus
selectors, comments without assertions, tests that mock the target behavior,
and branch omissions are rejected with actionable diagnostics. A loop cannot
cause infinite case enumeration or silent coverage truncation.

### PR 5 — Execution evidence, review, and complete product proof

1. Bind receipts to story/case/test/candidate/environment identities and expose
   current coverage through existing artifact/query/report paths.
2. Enforce completion separately from partial PR success and design readiness.
3. Run an end-to-end fixture from prose interview through story proposal,
   multi-PR allocation, independent test authoring, execution, review, and an
   approved post-execution story amendment.
4. Demonstrate that a planted customer-visible defect fails the corresponding
   case even when focused implementation unit tests pass. Add negative controls
   appropriate to that fixture; do not manufacture a second implementation path.

Acceptance: the report identifies the exact failing transition/outcome and its
assertion evidence; after a permitted implementation repair all required cases
pass on the candidate. After a design amendment old receipts become historical,
tests are refreshed, and only evidence for the newly confirmed revision can
complete the changed story. Existing-behavior stories can verify on a passing
baseline; unavailable execution remains blocked rather than passed.

## 11. Required verification matrix

Use hermetic provider fixtures for deterministic integration coverage and an
explicit opt-in MiniSWE smoke run for real authoring. A scripted provider test
proves the pipeline contract, not LLM quality. Preserve real-run artifacts so the
generated tests and observed behavior can be inspected.

| Scenario | Required result |
|---|---|
| Prose with an unstated failure result | Interview proposes/confirms behavior before readiness |
| Predicate overlap or missing alternative | Semantic review returns a focused question |
| Valid graph with no useful oracle | Case/test review blocks verification, even though graph parsing succeeds |
| Duplicate/reused step ID | Deterministic rejection with exact subject reference |
| Step split/removal | Atomic projected graph valid; old IDs retired; cases and refs updated |
| Changed story after confirmation | Old confirmation cannot authorize the changed digest |
| New story for existing code | Existing bindings reused; baseline-pass tests permitted |
| Code scan cannot reconstruct story | Approved story survives bootstrap unchanged |
| Repeated action, loop, cancellation | Explicit conditions, finite cases, observable state assertions |
| Cross-PR story | Partial PR pass cannot complete the whole story |
| Two test files prove one observation | Valid many-to-many links and actual collected selectors |
| Test comment claims coverage without assertion | Link/quality validation fails |
| Story test skipped or environment unavailable | Explicit not-run/blocked status; no verified story |
| Source implementation disagrees with approved story | Defect or proposed amendment; oracle is preserved |
| Concurrent amendments | Whole-graph validation and explicit conflict handling |
| Large graph exhausts case search | Uncovered targets and incomplete status; no silent omission |
| Input/code/test/environment changes | Receipts cannot be silently reused as current proof |
| Older repository has no stories | Readable with absent story coverage; no fabricated approval |

## 12. Completion and implementation guardrails

This project is complete when a user can confirm an unambiguous story flow,
inspect it in Structrr, add another story later, evolve either story through
reviewed diffs with durable IDs, and inspect collected/executed tests proving
specific paths and observations at the current candidate revision.

Before claiming completion, demonstrate all four collections through every active
parser/projector/serializer, both feature-design and plan execution entry points,
bootstrap preservation, code/design diff distinction, actual test execution,
and amendment impact. A schema-only implementation or a prompt-only implementation
does not satisfy this plan. Do not mark a story verified from the existence of
functionality links, test comments, an author success message, or a green unrelated
validation command.

Do not repair MiniSWE's output manually, consume hidden benchmark verifier tests
as design input, add direct provider calls to agent code, invent new user intent
to satisfy a test, silently truncate case requirements, or conflate a failed
review with an empty successful review. Resolve routine compiler mechanics in
code; return genuine behavioral choices to the user interview.
