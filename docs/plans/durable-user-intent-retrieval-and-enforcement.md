# Durable User Intent Retrieval and Enforcement Plan

## Purpose

The primary reliability goal for Powdrr is that an instruction from a user
continues to affect execution at the right time, even after the conversation
moves on, the process restarts, context is compacted, a nested skill runs, or a
different agent persona takes over.

Examples include:

- "After addressing a review comment, resolve the review thread."
- "Always use optimistic locking for mutable database rows."
- "Python changes must include mypy type information and pass mypy."
- "Preserve this API shape even if the implementation changes."

These instructions must not depend on the model recalling prose from an earlier
turn or deciding to invoke a context-retrieval tool. Powdrr must capture them as
durable structured intent, resolve their applicability deterministically, place
the relevant subset into each model request, and enforce objective consequences
through obligations, evidence, and transition guards.

This document is a focused companion to the broader
[`OpenCode-inspired execution engineering plan`](opencode-inspired-agent-execution-engineering-plan.md)
and its
[`remaining-work audit`](opencode-inspired-agent-execution-remaining-work.md).
It specifies the end-to-end path for remembered design decisions, invariants,
and procedures.

## Required outcome

The implementation is complete when a user can state a decision, invariant, or
procedure once and Powdrr can subsequently:

1. preserve the original wording and source;
2. represent the executable meaning in a validated structured contract;
3. bind the contract to relevant repositories, work items, entities,
   relationships, paths, phases, actions, or languages;
4. retrieve it automatically when those selectors become relevant;
5. show the model only the currently applicable contract;
6. prevent actions and transitions that violate enforceable requirements;
7. open, order, satisfy, invalidate, and replay derived obligations;
8. explain which instruction affected a decision and why;
9. survive restart, nested execution, handoff, and compaction without relying on
   conversation prose; and
10. supersede or revoke the instruction without leaving contradictory hidden
    behavior.

## Core principle

User intent is runtime input, not prompt decoration.

The model may help translate natural language into candidate structure, but the
model is not the memory system and is not the authority for applicability or
completion. The runtime owns:

- persistence;
- scope and selector matching;
- precedence and conflict detection;
- relationship expansion;
- obligation ordering and closure;
- evidence freshness;
- prompt inclusion;
- action validation; and
- transition and readiness decisions.

The agent should not have to remember to call `get_invariants`,
`get_current_decisions`, or `get_entity_relationships`. Those remain useful
inspection surfaces, but automatic execution behavior must use the same core
queries before the model acts.

## Instruction classes

Powdrr should distinguish four classes of remembered intent because they have
different runtime consequences.

### Design decision

A design decision selects an intended system outcome or implementation
approach. It belongs in the structured specification and relates to the
affected requirements, entities, modules, tools, and proposed PRs.

Examples:

- preserve an API response shape;
- store workflow state in repository-local files;
- use optimistic locking rather than pessimistic locking.

A decision is retrieved as authoritative context. A proposed plan or edit that
contradicts it creates a structured conflict; prompt order does not decide which
statement wins.

### Invariant

An invariant states something that must remain true across relevant changes.
It has a scope, a machine-checkable assertion when possible, and optional
evidence requirements.

Examples:

- mutable rows use a version/check-and-update pattern;
- public Python functions include mypy-compatible type information;
- a wire protocol remains backward compatible.

An invariant may reject a proposal, open remediation obligations, require
validation evidence, or block phase completion.

### Procedure

A procedure describes required sequencing between semantic actions.

Examples:

- after changing code for a review comment, run validation and then resolve the
  exact review thread;
- after changing a database migration, regenerate and inspect the schema;
- after updating generated source, rerun the generator consistency check.

A procedure compiles to trigger, prerequisite, follow-up, ordering, and
completion semantics. It is not merely a checklist in the prompt.

### Guidance

Guidance influences choices but is not independently completion-blocking.

Examples:

- prefer a small focused module;
- retain existing naming conventions;
- minimize public API surface.

Guidance is included in the effective contract and remains explainable, but it
does not manufacture evidence or override deterministic truth.

## Current foundation and gaps

Powdrr already contains the necessary foundation:

| Capability | Existing seam | Current limitation |
| --- | --- | --- |
| Versioned scoped guidance | `core/behavior_rule.py` | The rule is primarily text plus exact scope matching; it has no typed trigger, requirement, or evidence semantics. |
| Specification decisions and invariants | `core/codebase_state.py`, `core/entity_context.py`, `core/spec_context.py` | Retrieval is exposed through CLI/MCP and explicit context actions, not automatically resolved by the execution loop. |
| Action relationships | `core/action_relationship.py`, `execution/relationships.py` | Relationships are built-in standalone primitives and are not compiled from matched user intent or persisted by the live runner. |
| Durable obligations and evidence | `core/execution_state.py`, `execution/evidence.py` | Normal actions do not consistently open, satisfy, invalidate, or gate on them. |
| Shared action lifecycle | `workflow_llm.py`, `execution/kernel.py` | The shared runner records lifecycle events but does not own policy resolution and enforcement. |
| Typed context compaction | `execution/compaction.py` | Rule and obligation IDs are preservable, but the compactor is not yet the authoritative prompt/resume path. |
| Plan/task compilation | `core/execution_plan.py`, `execution/compile.py` | Compiled tasks do not yet carry an automatically resolved intent contract. |

The central missing component is a policy runtime joining these primitives at
the shared action boundary.

## Target architecture

The target flow is:

```text
user instruction
  -> nominate and validate structured intent
  -> persist source + current typed contract
  -> index selectors and specification relationships
  -> compile statically relevant IDs into plan units and tasks
  -> resolve the effective contract for the current execution context
  -> render the relevant contract into the model request
  -> validate the proposed action against the contract
  -> execute through the shared action kernel
  -> record facts, obligations, and evidence
  -> guard next_step, complete, phase exit, and readiness
```

This requires two retrieval stages.

### Static resolution

When Powdrr compiles an execution plan and workflow tasks, it resolves intent
known to be relevant from:

- proposed-PR feature and requirement IDs;
- entities and entity relationships;
- planned paths and ownership;
- delivery phase and persona;
- declared validation profiles; and
- repository, work-item, and proposed-PR scope.

The resulting task stores typed references such as `decision_ids`,
`invariant_ids`, `procedure_ids`, and `rule_ids`. It does not copy unversioned
prose into every task.

### Dynamic resolution

Some relevance is knowable only after the model proposes or completes an
action. Before every request and again after every parsed proposal, Powdrr
resolves intent using:

- active execution unit and phase;
- current step and semantic actions;
- proposed action kind and arguments;
- affected paths;
- entities related to those paths;
- review finding or thread identity;
- changed languages and artifact types;
- open obligations; and
- evidence invalidated by prior mutations.

Dynamic resolution catches emergent relevance such as discovering that an edit
touches a mutable row even when the execution plan did not label the operation
correctly.

## Structured contracts

### Preserve source separately from executable meaning

Every remembered instruction retains:

- the exact user text;
- source conversation, message, workflow, or specification reference;
- who supplied or confirmed it;
- creation time;
- current version;
- supersession and revocation history; and
- the structured interpretation used by the runtime.

The original text is evidence of intent and is useful for explanation. Runtime
behavior is based on validated typed fields.

### Behavior rule contract

Replace the text-only runtime interpretation with one current rule schema. Do
not maintain parallel old and new runtime paths. A one-time migration may
translate persisted development data before enforce mode, but the runtime sees
only the current typed object.

Conceptually, a rule contains:

```yaml
schema_version: behavior-rule-v2
rule_id: resolve-review-thread-after-fix
kind: procedure
text: After addressing a review comment, resolve the review thread.
source_ref: conversation:123/message:456
scope:
  repository: powdrr-lift
selectors:
  source_actions: [edit_for_review_comment]
  phases: [resolve_findings]
trigger:
  event: action_completed
  action: edit_for_review_comment
requirements:
  - requirement_id: validate-review-fix
    type: action
    action: run_validation
  - requirement_id: resolve-exact-thread
    type: action
    action: resolve_review_thread
    after: [validate-review-fix]
completion_gate: all_requirements_satisfied
precedence: 100
version: 1
active: true
```

Closed enums should define rule kinds, selector keys, trigger events,
requirement types, and completion gates. Unknown fields or values fail
validation.

### Effective contract

Introduce an immutable, non-authorable `EffectiveContract` computed for one
execution boundary. It contains:

- contract fingerprint;
- current rule IDs and versions;
- relevant decision and invariant IDs;
- rendered guidance;
- proposal preconditions;
- obligations already open;
- obligations that the proposed action would create;
- required evidence and freshness state;
- blocked transitions with reasons; and
- provenance suitable for explanation and replay.

The effective contract is derived state. Persist its references and fingerprint
in execution events, but rebuild it from authoritative rules and specifications.

## Capture pipeline

### Candidate nomination

Explicit normative language is a candidate for durable capture, including
"always", "never", "must", "after", "before", "every time", "from now on",
and direct requests to remember an outcome or procedure.

The model may nominate a candidate structure, but nomination cannot silently
create broad behavior. The runtime validates:

- rule kind;
- source reference;
- narrowest supported scope;
- selectors;
- trigger and requirements;
- referenced entities, actions, checks, and phases;
- conflicts with active rules and structured decisions; and
- whether enforcement is objectively testable or only guidance.

### Scope behavior

Default to the narrowest context supported by the instruction:

1. current execution unit or review finding;
2. current proposed PR;
3. current work item;
4. current repository; then
5. organization scope only when explicitly requested and supported.

If a materially broader scope is plausible but not explicit, ask one focused
question before persisting the rule. Do not infer organization-wide policy from
a repository conversation.

### User acknowledgment

After capture, Powdrr should report the interpreted contract in plain language:

> Recorded repository invariant `mutable-rows-use-optimistic-locking`. It
> applies when an execution unit or edit affects an entity classified as a
> mutable database row and requires optimistic-lock implementation plus fresh
> concurrency-test evidence.

This acknowledgment catches incorrect scope or semantics while the source
instruction is still fresh.

## Applicability and indexing

Create a deterministic `IntentIndex` rather than scanning all prose for every
roundtrip. It should support exact lookup by:

- repository and work-item ID;
- proposed-PR, requirement, decision, invariant, and feature ID;
- entity and entity-relationship ID;
- module, tool, artifact, and validation-profile ID;
- normalized repository path or path prefix;
- language;
- delivery phase and persona;
- semantic action; and
- review finding or thread ID.

Structured selectors are authoritative. Embedding or semantic search may
nominate additional candidates for classification, but it cannot decide that a
rule applies or that an obligation is satisfied.

The resolver should return both matches and near misses. Near misses are useful
for diagnostics and tests, but only exact validated matches enter the effective
contract.

## Shared runner integration

Introduce `execution/policy.py` with a `PolicyRuntime` owned by
`WorkflowStepRunner`. Chat and durable-task adapters must not invoke policy
functions independently.

The runtime interface should be conceptually equivalent to:

```python
class PolicyRuntime(Protocol):
    def resolve_context(self, context: PolicyContext) -> EffectiveContract: ...

    def evaluate_proposal(
        self,
        action: object,
        context: PolicyContext,
        contract: EffectiveContract,
    ) -> ProposalDecision: ...

    def record_outcome(
        self,
        action: object,
        outcome: object,
        context: PolicyContext,
        contract: EffectiveContract,
    ) -> PolicyOutcome: ...

    def evaluate_transition(
        self,
        transition: str,
        context: PolicyContext,
    ) -> TransitionDecision: ...
```

The shared loop becomes:

1. obtain typed execution context from the adapter;
2. resolve the effective contract;
3. pass that contract into prompt construction;
4. parse the model response;
5. resolve any action-specific selectors;
6. evaluate the proposal before `ActionKernel.start()`;
7. execute the action if allowed;
8. record completion or failure;
9. open, satisfy, obsolete, or invalidate obligations and evidence; and
10. evaluate any requested transition.

A policy rejection is a structured, model-correctable response containing the
rule, source, failed requirement, and allowed next actions. It is not a generic
provider error and does not rely on the model inferring the relevant rule from
the full specification.

## Prompt contract

Each request receives a concise `effective_contract` containing only relevant
material:

```json
{
  "contract_fingerprint": "...",
  "decisions": [
    {"id": "decision-optimistic-lock", "summary": "Use versioned updates"}
  ],
  "invariants": [
    {"id": "mutable-row-locking", "requirement": "Use optimistic locking"}
  ],
  "procedures": [
    {
      "id": "resolve-review-thread-after-fix",
      "remaining_actions": ["run_validation", "resolve_review_thread"]
    }
  ],
  "open_obligations": ["validate-review-fix"],
  "blocked_transitions": ["complete"]
}
```

The prompt should explain that these are current runtime facts, not optional
suggestions. It should not include every repository rule, historical version,
or unrelated decision.

Prompt construction must be derived from the same `EffectiveContract` used by
proposal and transition validation. There must not be a prompt-only policy
path.

## Procedures and obligation closure

Relationship expansion should create durable obligations only after the
triggering action completes successfully. Proposal evaluation may preview the
obligations that would be created and may enforce prerequisites for the
proposed action.

Extend obligations to include:

- source rule and relationship IDs;
- source action instance ID;
- target semantic action or evidence requirement;
- exact target identity, such as review thread ID;
- dependency obligation IDs;
- applicable path/entity scope;
- status and disposition reason; and
- evidence IDs that satisfied the obligation.

An obligation is satisfied only by an exact completed semantic action or by
fresh typed evidence accepted by its requirement. Similar prose from the model
does not close it.

### Review comment example

```text
edit_for_review_comment(thread=R123)
  -> open validate_review_fix(scope=fingerprint)
  -> open resolve_review_thread(thread=R123, after=validate_review_fix)
  -> successful validation produces fresh evidence
  -> validation obligation closes
  -> resolve_review_thread(R123) becomes eligible
  -> exact GitHub thread resolution closes final obligation
  -> review-correction phase may complete
```

The runtime must reject resolution of a different thread and must reopen
validation if a later edit invalidates the evidence.

## Invariants and evidence

Invariants should use the strongest available enforcement form:

1. structural validation of the proposed action or changed artifact;
2. registered deterministic validation command;
3. typed evidence from a constrained tool;
4. independent review finding and disposition; or
5. prompt guidance only when no objective enforcement exists.

### Optimistic locking example

When an affected entity is a mutable database row:

- inject the relevant design decision and invariant before implementation;
- validate that the implementation contains the configured version or compare-
  and-update behavior;
- open a concurrency-evidence obligation;
- require fresh successful concurrency evidence for the affected fingerprint;
- invalidate that evidence when relevant implementation or schema inputs
  change; and
- block Build, Validate, Review, and Publish transitions while the invariant is
  unresolved.

If Powdrr cannot classify whether the entity is mutable, it should create an
explicit classification decision rather than silently omitting the invariant.

### Mypy example

For a repository rule applying to Python implementation changes:

- planned Python paths receive the invariant during static resolution;
- newly discovered Python edits receive it during dynamic resolution;
- changed public signatures may be structurally checked for annotations;
- edits invalidate mypy evidence for affected inputs;
- a registered mypy validation action produces typed evidence; and
- completion requires fresh successful evidence.

The requirement is applied even if the model never mentions typing in its
plan.

## Conflict and precedence

Conflicts must be explicit and deterministic. Recommended precedence is:

1. kernel safety, capability, and truth invariants;
2. current explicit user decision for the active scope;
3. narrower active rule over a broader rule;
4. repository rule over organization default;
5. newer version only when it explicitly supersedes the earlier rule.

Creation time alone does not silently override an unrelated active rule. A
material unresolved conflict creates a typed user decision and blocks the
affected action or transition.

Remembered intent may narrow behavior, select among safe alternatives, add
obligations, or strengthen validation. It may not broaden tool effects, escape
the worktree, suppress failing evidence, waive a blocking finding, or convert a
failed check into success.

## Persistence, restart, and compaction

The durable event stream records:

- rule nomination, validation, activation, supersession, and revocation;
- effective-contract fingerprint and matched rule versions at each action;
- relationship expansion and obligation changes;
- evidence production and invalidation;
- conflicts and user dispositions; and
- transition decisions.

Materialized execution state stores current typed references. Context
compaction must preserve exact:

- rule IDs and versions;
- decision and invariant IDs;
- effective-contract fingerprint;
- obligation IDs and dependency edges;
- evidence IDs and input fingerprints;
- review finding and thread IDs; and
- source references needed for explanation.

After restart or compaction, Powdrr rebuilds the same effective contract from
typed state. It never treats a generated summary as the authoritative copy of a
user instruction.

Nested skills and persona handoffs inherit the applicable contract by ID and
version. A child may receive a narrower effective contract, but cannot drop a
parent obligation relevant to its work.

## Explanation and user control

Add shared CLI/MCP operations backed by core functions:

- `remember-intent`: nominate, validate, and persist a decision, invariant,
  procedure, or guidance rule;
- `list-intent`: list active and superseded intent by scope;
- `explain-effective-contract`: show why each item applies to the current unit,
  path, entity, phase, or action;
- `explain-obligation`: show source instruction, trigger action, dependencies,
  and accepted closure evidence;
- `supersede-intent`: create an explicit replacement version; and
- `revoke-intent`: deactivate a rule with optimistic version checking.

CLI, MCP, chat, and workflow-task surfaces call the same implementation. The UI
should make remembered behavior inspectable without showing the entire event
log by default.

## Engineering plan

Implement this as independently mergeable PRs. Start in observe mode, prove
retrieval and parity, then make policy decisions authoritative.

### PR 1: Typed intent contracts and index

Goal: establish one validated representation and deterministic applicability
lookup without changing action behavior.

Required changes:

- evolve `core/behavior_rule.py` to the current typed schema;
- add closed enums and dataclasses for selectors, triggers, requirements,
  completion gates, and source provenance;
- add `core/effective_contract.py` for derived contract records;
- add `core/intent_index.py` for exact selector indexes;
- connect decisions, invariants, guidance, entities, and relationships from
  `codebase_state.py`, `entity_context.py`, and `spec_context.py`;
- add strict parsing, unknown-field rejection, graph validation, and stable
  fingerprints;
- replace current development rule data through a one-time migration and keep
  only the current runtime schema; and
- expose read-only inspection and explanation through shared core functions.

Tests:

- round-trip every rule kind;
- reject unknown selectors, actions, entities, and cycles;
- prove narrow scope wins and unrelated rules do not match;
- prove stable fingerprints regardless of input ordering;
- prove revocation and explicit supersession; and
- prove semantic search cannot activate a rule.

Acceptance gate: given typed execution context, the resolver returns the exact
applicable intent IDs, versions, and provenance with no LLM call.

### PR 2: Capture and effective-contract delivery

Goal: capture explicit user intent and place the relevant contract into every
model request without enforcement.

Required changes:

- add candidate nomination and deterministic validation;
- persist exact source references and narrowest scope;
- add acknowledgment rendering;
- add `execution/policy.py` with `PolicyRuntime.resolve_context()`;
- add a typed `PolicyContext` supplied by the shared strategy boundary;
- attach statically relevant IDs during plan/task compilation;
- resolve dynamic selectors before request construction;
- render one `effective_contract` in both chat and durable-task prompts;
- retain contract references through compaction and nested-skill handoff; and
- record observe-mode matches and disagreements.

Tests:

- explicit procedure, invariant, decision, and guidance capture;
- ambiguous broad scope requires one decision;
- both adapters receive identical contracts for identical context;
- nested and resumed execution preserve applicable IDs;
- prompt contains relevant rules and excludes unrelated rules; and
- compaction preserves exact IDs and versions.

Acceptance gate: a rule stated in one session appears automatically in a later
relevant session and does not appear in an unrelated task.

### PR 3: Proposal validation and obligation lifecycle

Goal: make procedures and proposal preconditions authoritative in the shared
runner.

Required changes:

- integrate `PolicyRuntime` into `WorkflowStepRunner` before
  `ActionKernel.start()` and after terminal lifecycle events;
- expand applicable action relationships deterministically;
- extend execution obligations with rule provenance, exact target identity,
  dependency IDs, and accepted evidence;
- persist obligation-opened, satisfied, obsolete, and reopened events;
- enforce prerequisite order;
- return typed correction packets for policy violations;
- guard `next_step`, `complete`, and phase exits on open obligations; and
- remove adapter-specific policy handling after chat/task parity tests pass.

Tests:

- review edit opens validation and exact-thread obligations;
- thread resolution is rejected before validation;
- resolving the wrong thread does not close the obligation;
- failure of the triggering action creates no durable follow-up obligation;
- restart and replay reconstruct identical obligation state;
- relationship conflicts create a structured decision; and
- chat and task adapters produce identical events and corrections.

Acceptance gate: the review-comment procedure changes real execution and blocks
completion until validation and exact-thread resolution occur in order.

### PR 4: Invariant validation and evidence freshness

Goal: enforce objective invariants and connect them to current evidence.

Required changes:

- map invariant requirements to structural validators, registered validation
  profiles, evidence producers, and review policies;
- resolve entities and languages from proposed and completed edits;
- produce scoped input fingerprints;
- invalidate only affected evidence after mutation or revert;
- reopen obligations whose satisfying evidence becomes stale;
- add invariant failures to typed correction packets;
- include invariant/evidence state in readiness evaluation; and
- explain whether enforcement is structural, evidence-backed, review-backed,
  or guidance-only.

Tests:

- mutable-row change requires optimistic-lock structure and concurrency
  evidence;
- Python change requires annotation policy and fresh mypy evidence;
- unrelated edits do not invalidate evidence;
- relevant later edits invalidate evidence and reopen completion;
- missing entity classification creates a decision; and
- no rule can suppress failed evidence or broaden capability scope.

Acceptance gate: both optimistic-locking and mypy invariants are enforced even
when the model omits them from its plan and narrative.

### PR 5: Enforce-mode rollout and removal of voluntary retrieval

Goal: make automatic policy resolution the sole normal execution path.

Required changes:

- compare observe-mode and expected decisions across scenario fixtures;
- add contract, obligation, evidence, and conflict telemetry;
- enable enforcement for new executions after parity thresholds pass;
- route CLI/MCP inspection through the same resolver used by execution;
- remove prompts instructing the model to remember to retrieve mandatory
  invariants or decisions;
- remove duplicate prompt-only policy and adapter-specific enforcement;
- add migration diagnostics for unsupported persisted intent data; and
- document user inspection, supersession, and revocation workflows.

Tests:

- full specification-to-PR scenarios in chat and durable-task modes;
- process interruption before and after compaction;
- cross-session and cross-persona instruction persistence;
- conflict and supersession scenarios;
- complete review-correction and mutable-row scenarios; and
- negative relevance fixtures proving irrelevant intent is not injected.

Acceptance gate: new executions use one authoritative policy path and all
completion decisions are reproducible from structured state without consulting
conversation history.

## Verification strategy

Every PR runs formatting, linting, mypy, and the full test suite. In addition,
maintain a focused scenario matrix:

| Scenario | Expected proof |
| --- | --- |
| Review comment correction | Validation occurs before exact-thread resolution; completion is blocked until both close. |
| Mutable database row | Optimistic locking and concurrency evidence are required even if omitted by the model. |
| Python API change | Applicable typing decision is supplied and fresh mypy evidence is required. |
| Unrelated documentation change | Database and mypy implementation rules are not injected. |
| Restart after edit | Open obligations and applicable rule versions are identical after replay. |
| Compaction before validation | Rule, obligation, target, and evidence fingerprints survive exactly. |
| Conflicting user instruction | A decision is requested; prompt order does not choose a winner. |
| Rule supersession | Only the explicitly superseding version applies and the explanation shows lineage. |
| Nested review skill | Child receives relevant contract and cannot drop the parent's open obligations. |

Add invariants to tests themselves:

- no model call is used for applicability matching;
- no transition guard parses prompt prose;
- no obligation closes from narrative text;
- no compacted summary is an authority for rule content;
- no adapter computes an effective contract independently; and
- no enforced rule can broaden a capability or weaken evidence truth.

## Operational metrics

Observe and enforce modes should record:

- applicable rule count by task and action;
- irrelevant-rule exclusion count;
- contract size and prompt token contribution;
- policy proposal rejection count by rule;
- obligations opened, reopened, and closed;
- obligation closure latency;
- evidence invalidation reason;
- transition blocks by invariant or procedure;
- conflicts requiring user decisions;
- chat/task policy disagreement count; and
- replay or restart contract-fingerprint mismatches.

The desired result is not a high number of injected rules. It is precise
retrieval: every relevant instruction, no irrelevant instruction, and the same
decision before and after restart.

## Final acceptance demonstration

The final end-to-end demonstration should begin with three user instructions:

1. after addressing a review comment, validate the change and resolve the exact
   thread;
2. mutable database rows always use optimistic locking; and
3. Python implementation changes include mypy-compatible type information and
   pass mypy.

The execution should then move through planning, implementation, validation,
review correction, compaction, process restart, and PR readiness. The final
record must show:

- where each instruction was captured;
- the structured interpretation and scope;
- each action where it became applicable;
- the concise contract delivered to the model;
- obligations and evidence it created;
- any action or transition it blocked;
- exact closure evidence;
- survival across compaction and restart; and
- a deterministic final readiness decision with no dependence on remembered
  chat prose.

That demonstration is the product-level proof that Powdrr does not forget the
design or procedure the user already supplied.
