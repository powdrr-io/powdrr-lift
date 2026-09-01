# Durable User Intent Implementation Plan

## Purpose

This is the implementation plan for making Powdrr reliably remember and honor
user instructions throughout planning and execution.

The plan is intentionally organized into a small number of end-to-end PRs.
Each PR must leave the system closer to the final behavior, with a concrete
acceptance gate. We should not create separate foundational PRs for a schema,
an index, prompt rendering, obligations, chat integration, task integration,
and telemetry. Those concerns are only useful together at the execution
boundary.

The target is not a larger memory system. It is a canonical intent registry and
a deterministic execution contract:

```text
one user instruction
  -> one canonical source record
  -> one or more typed clauses by reference
  -> one versioned semantic contract
  -> applicable references when selectors match
  -> current effective procedure for the next action
  -> active obligations/evidence when consequences exist
```

The original user wording is captured once. Plans, tasks, prompts, state,
obligations, events, and nested skills reference it by stable IDs and versions.

## End state

Every model-controlled execution round is determined from:

```text
EffectiveProcedure_t
+ CurrentExecutionState_t
+ LatestObservation_t
```

The event log and transcript remain available for audit, replay, diagnostics,
and user-facing history. They are not ordinary execution memory.

The effective procedure contains the relevant immutable skill instructions,
requirements, design decisions, invariants, procedures, delivery guidance,
capability constraints, and completion conditions. It is a deduplicated,
ephemeral projection, not a new persisted copy of intent.

The current execution state contains only facts needed for future decisions:

- current objective, phase, unit, and step;
- current worktree and external-resource references;
- active agent working state;
- open obligations and dependencies;
- current evidence and freshness state;
- open findings and pending decisions;
- skill-specific sufficient state; and
- effective-procedure fingerprint and referenced IDs.

Completed actions, resolved obligations, old tool output, superseded guidance,
and intermediate reasoning remain historical records rather than current state.

## Why delivery guidance is part of durable intent

Instructions such as these are not mere conversational preferences:

- “Make small incremental PRs.”
- “Be extra cautious not to regress anything.”
- “Make a big refactor in one PR.”
- “Do not touch unrelated files.”
- “Prefer a reversible rollout.”
- “Keep the public API stable.”
- “Run the full test suite before creating a PR.”
- “Use the existing pattern unless there is a compelling reason not to.”

They affect what Powdrr should plan, which actions are allowed, what evidence is
required, how a change is sliced, how much validation is needed, and whether a
PR is ready. If they remain as prose in the conversation, they will disappear
when the conversation is compacted, a nested skill starts, or a different agent
persona takes over.

The runtime must therefore distinguish four kinds of intent:

| Kind | Runtime consequence |
| --- | --- |
| Design decision | Selects an intended outcome or approach and rejects contradictory proposals |
| Invariant | Must remain true and may require structural validation or fresh evidence |
| Procedure | Creates ordered obligations and completion gates |
| Delivery guidance | Compiles into planning, risk, scope, slicing, validation, or rollout policy |

Delivery guidance may be non-blocking preference, enforceable constraint, or
conditional policy. The typed contract—not the wording alone—determines which.

## Current codebase starting point

Latest `main` already contains important execution primitives:

- `core/behavior_rule.py` stores versioned user guidance with optimistic
  version checks, but the rule owns the source text and executable scope in one
  text-oriented object.
- `execution/runtime.py::ExecutionRuntime` owns durable state, capability
  boundaries, guidance lookup, checkpoints, evidence, readiness, and kernel
  synchronization.
- `core/execution_state.py` defines typed actions, artifacts, obligations,
  evidence, findings, phases, versions, and event reduction.
- `execution/store.py` persists state and the complete event stream with
  optimistic state-version checks.
- `execution/kernel.py` provides deterministic action lifecycle events.
- `execution/relationships.py` expands action consequences into obligations.
- `execution/evidence.py` evaluates evidence freshness and readiness.
- `workflow_llm.py::WorkflowStepRunner` is the shared action loop for chat and
  automated workflows.

The remaining architectural problems are concentrated at the boundary:

- `WorkflowStepRunner` currently captures model-authored
  `decisions_and_context` as explicit guidance. This must be removed; model
  narration is not user intent.
- `ExecutionRuntime.prompt_context()` renders rule text into a runtime context
  object instead of exposing references resolved into one effective contract.
- applicability is primarily exact scope matching, not typed dynamic selector
  resolution;
- the action loop, durable runtime, and adapter prompt builders still have
  overlapping context responsibilities;
- `ExecutionState` retains cumulative action records even though the model only
  needs active execution state;
- obligation expansion is not yet keyed strongly enough to prevent duplicate
  consequence instances across retries or nested execution; and
- compaction and retrieval remain available as context mechanisms that could
  accidentally become correctness dependencies.

The implementation must consolidate these paths rather than add another
parallel memory layer.

## Canonical data model

### Intent source

`IntentSource` is the sole owner of the exact original instruction and
provenance:

```yaml
intent_id: intent-delivery-slicing
exact_text: Make small incremental PRs.
content_fingerprint: sha256:...
source_ref: conversation:123/message:456
supplied_by: user:gregory
created_at: 2026-08-31T12:00:00Z
```

It is captured once by stable source identity. Reprocessing the same message
returns the same `intent_id`.

### Intent clause

An instruction can contain multiple independent clauses. Each clause references
the source and a source span:

```yaml
clause_id: clause-delivery-slicing-v1
intent_id: intent-delivery-slicing
source_span: {start: 0, end: 31}
kind: delivery_guidance
contract_ref: delivery-policy:incremental-prs:v1
status: active
```

The clause owns classification and lifecycle. It does not own another copy of
the original text.

### Semantic contract

The semantic contract owns executable meaning once. It contains typed fields,
not conversational wording:

```yaml
contract_id: delivery-policy:incremental-prs
version: 1
kind: change_slicing_policy
parameters:
  mode: incremental
  max_scope: current_work_item
  require_independent_validation: true
selectors:
  repositories: [powdrr-lift]
  phases: [build, validate, publish_pr]
enforcement: plan_and_transition
```

The original instruction remains reachable through `clause_id -> intent_id`.
The contract does not repeat it.

### Effective procedure

`EffectiveProcedure` is derived for one execution boundary. It contains
deduplicated IDs and compact runtime data needed by the model and validators:

```yaml
fingerprint: sha256:...
contract_refs:
  - {id: delivery-policy:incremental-prs, version: 1}
  - {id: risk-policy:regression-cautious, version: 1}
open_obligation_ids: []
blocked_transition_ids: []
prompt_views:
  - id: delivery-policy:incremental-prs
    summary: Keep this work item in independently validated change slices.
```

`prompt_views` are disposable renderings. They are not stored as new intent
records and are never authoritative for enforcement.

## Guidance semantics

### Delivery guidance contract types

The initial implementation should support a deliberately small closed set:

- `change_slicing_policy` — one PR, incremental PRs, or a bounded change set;
- `scope_policy` — allowed files, modules, repositories, or unrelated-change
  restrictions;
- `risk_policy` — regression posture, required baselines, validation depth,
  rollout, and rollback expectations;
- `validation_policy` — required commands, validators, or evidence profiles;
- `rollout_policy` — staged rollout, feature flag, canary, or rollback plan;
- `compatibility_policy` — API, wire, schema, or behavior compatibility; and
- `working_style_guidance` — preferences that influence choices but do not
  independently block completion.

Do not create a new type for every phrasing. Normalize synonymous language into
one contract type and preserve the source wording only in `IntentSource`.

### “Make small incremental PRs”

Compile to:

```yaml
kind: change_slicing_policy
parameters:
  mode: incremental
  max_change_surface: current_coherent_slice
  require_independent_validation: true
  require_independent_review_boundary: true
```

Effects:

- planning must produce multiple independently coherent slices when the work
  naturally has separable boundaries;
- a proposed PR that combines unrelated slices is rejected or requires a user
  decision;
- each slice must have its own tests, validation, and review explanation;
- the current PR cannot silently absorb follow-on work;
- the agent may not create a sequence of empty or artificial PRs merely to
  satisfy a count; and
- the plan should explain why a proposed slice cannot be separated when it
  remains large.

This is a planning and PR-shape constraint. It should not force a commit for
every individual file or function.

### “Make a big refactor in one PR”

Compile to:

```yaml
kind: change_slicing_policy
parameters:
  mode: single_change_set
  max_change_surface: coherent_refactor
  require_boundary_inventory: true
  require_full_validation: true
```

Effects:

- planning may keep tightly coupled implementation changes together;
- the PR must still have one coherent purpose and explicit scope;
- the plan must identify all affected consumers before editing;
- full validation and regression evidence are required before readiness;
- unrelated cleanup remains prohibited; and
- “one PR” does not waive capability, safety, evidence, or review gates.

This guidance is not contradictory to small incremental PRs until both apply to
the same scope and delivery unit. If they do, the resolver must create an
explicit conflict decision.

### “Be extra cautious not to regress anything”

Compile to:

```yaml
kind: risk_policy
parameters:
  posture: conservative
  require_baseline_before_change: true
  require_affected_tests: true
  require_full_validation_before_publish: true
  require_diff_scope_review: true
  require_rollback_or_recovery_plan: true
  allow_unrelated_changes: false
```

Effects:

- capture baseline test and diagnostic evidence before mutation when possible;
- require targeted tests after each affected slice;
- require full validation before PR creation or publish;
- require a changed-path and dependency review;
- prevent “tests are probably enough” as a readiness claim;
- require a typed explanation for intentionally changed behavior; and
- increase review or human-decision requirements for uncertain scope.

“Be careful” must not become vague prompt language. It must compile into
observable gates and evidence requirements.

### Scope and precedence

Guidance applies at an explicit scope:

1. current action or review finding;
2. current execution unit;
3. current PR;
4. current work item;
5. repository; or
6. organization, only when explicitly requested and supported.

The default is the narrowest supported scope. A repository conversation does
not imply organization-wide policy.

Recommended precedence is:

1. capability, safety, and truth invariants;
2. explicit current user decision for the active scope;
3. narrower active contract over broader contract;
4. explicit supersession over the prior version;
5. repository policy over organization default; and
6. non-blocking preference.

Creation time alone never resolves a conflict. A conflict between “small
incremental PRs” and “one big refactor PR” must be presented as a structured
decision naming both sources, scopes, and consequences.

An explicit later instruction can supersede an earlier instruction only when it
clearly addresses the same contract and scope. It should create a new clause or
contract version linked to the original; it should not mutate historical source
text.

### Guidance strength

Every contract declares its strength:

- `preference` — affects ranking and explanation;
- `plan_constraint` — plan compilation must satisfy it;
- `action_constraint` — proposal validation can reject an action;
- `transition_gate` — completion or phase exit is blocked until evidence exists;
  or
- `safety_invariant` — runtime enforcement takes priority over model choice.

The compiler must not silently upgrade a preference to a blocker. If a user
uses normative language but the requested behavior cannot be objectively
enforced, Powdrr should acknowledge it as guidance and explain the limitation.

## How guidance is honored across execution

### At capture

The user-facing ingress path:

1. records the exact user source once;
2. nominates typed clauses;
3. resolves each clause to an existing semantic contract or creates one;
4. validates scope, references, parameters, and conflicts;
5. asks one focused question if scope or meaning is materially ambiguous;
6. persists the source, clauses, and contract references atomically; and
7. acknowledges the normalized behavior in plain language.

Model-generated action rationale is not an equivalent ingress path.

### At planning

Plan compilation resolves applicable guidance and produces a plan policy:

```yaml
plan_policy:
  slicing: incremental
  risk_posture: conservative
  required_validation_profiles: [targeted, full]
  scope: current_work_item
  rollout: reversible
  source_refs: [clause-delivery-slicing-v1, clause-regression-caution-v1]
```

The plan stores references and normalized parameters, not source prose. Plan
validation rejects violations before an execution task is created.

### At task and action selection

The effective procedure is resolved before every model request and after every
material proposal. It informs:

- available action choices;
- current change-slicing policy;
- permitted paths and resources;
- required validation actions;
- open obligation order;
- evidence needed for the next transition; and
- whether the current action is allowed.

The model receives a concise current contract. Deterministic validators consume
the same contract fingerprint.

### At mutation and evidence

Mutations update the current execution state and invalidate affected evidence.
For a conservative risk policy, editing a relevant file can require:

```text
baseline evidence -> mutation -> targeted evidence -> full evidence -> review
```

The model cannot declare the steps complete in a patch or narrative. Tool
results and registered validators produce the evidence.

### At PR creation and readiness

The publish transition evaluates the policy from structured state:

- PR shape matches the slicing policy;
- no unrelated files are included;
- required targeted and full validation evidence is fresh;
- all procedural obligations are closed;
- rollback or recovery requirements are present when requested; and
- unresolved guidance conflicts have user dispositions.

The PR description may explain the guidance and cite its source, but the prose
is a view over state, not the gate itself.

## State-growth rules

The following are hard invariants:

1. Original user wording has one canonical persisted owner.
2. Source provenance is stored once and referenced by ID.
3. Contracts own executable semantics once.
4. Indexes contain IDs and lookup keys, not copied prose.
5. Plans, tasks, checkpoints, and handoffs contain versioned references.
6. Effective contracts are ephemeral projections.
7. Repeated applicability resolution is read-only and persistence-free.
8. Multiple selector paths return one clause and one contract version.
9. Repeated obligation expansion returns the existing obligation instance.
10. Nested skills reference parent intent and obligations rather than cloning
    them.
11. Current state retains active consequences; completed history remains in the
    event log.
12. Model rationale cannot create durable intent.
13. No state field lacks ownership and retention metadata.
14. For constant active complexity, current state and prompt size remain bounded
    as action count, retries, and historical events increase.

Use explicit cardinality and byte budgets for model-facing state. The exact
budgets should be measured against the current workflows, but the tests must
assert the asymptotic property immediately rather than waiting for production
growth.

## Three implementation PRs

### PR 1: Canonical intent and delivery-policy compilation

Goal: make one-time capture and guidance interpretation correct and
inspectable, without changing ordinary action behavior yet.

Scope:

- add `IntentSource`, `IntentClause`, and typed delivery-policy contracts;
- evolve `core/behavior_rule.py` so source provenance and executable meaning
  are referenced rather than duplicated;
- replace the current text-only capture path with idempotent source capture;
- remove `WorkflowStepRunner` capture of model-authored
  `decisions_and_context`;
- add closed guidance contract types for slicing, scope, risk, validation,
  rollout, compatibility, and preference;
- add deterministic scope, precedence, conflict, and strength resolution;
- add ID-only indexes and deduplicated selector matching;
- add plan-policy compilation and validation;
- add canonical fingerprints and optimistic versions; and
- expose capture, list, explain, supersede, and revoke through shared core
  operations.

Required tests:

- same source identity is captured once;
- source text is stored once even when it yields multiple clauses;
- separate identical messages remain separate unless explicitly reused;
- action rationale does not create intent;
- “small incremental PRs” produces incremental policy;
- “big refactor in one PR” produces single-change-set policy;
- conservative regression guidance produces required validation policy;
- same-scope conflicting guidance creates a decision;
- narrower scope wins only when the scopes actually overlap;
- explicit supersession changes the active version without mutating history;
- selector matches deduplicate by canonical clause and version; and
- resolution performs no persistence or LLM calls.

Acceptance gate:

```text
one explicit instruction
  -> one source ID
  -> typed contract with strength and scope
  -> deterministic plan policy
  -> no copied wording in derived records
```

No execution behavior should depend on a model remembering to retrieve the
instruction.

### PR 2: One vertical enforcement slice through the shared runner

Goal: prove the complete behavior with one high-value procedure and one
delivery-policy path before generalizing further.

Use this vertical slice:

```text
after addressing a review comment,
validate the change and resolve the exact review thread
```

And this delivery guidance:

```text
be extra cautious not to regress anything
```

Scope:

- make `WorkflowStepRunner` the authoritative effective-procedure boundary;
- resolve policy before prompt construction and proposal validation;
- pass one identical contract projection to chat and durable tasks;
- validate proposals before `ActionKernel.start()`;
- create exact, idempotent validation and thread-resolution obligations;
- require fresh evidence for the affected change fingerprint;
- invalidate evidence after relevant edits;
- block completion and PR readiness on unresolved obligations or conservative
  risk gates;
- return policy rejection as the latest correctable observation;
- persist contract fingerprints and references in events; and
- keep only active obligations/evidence in the model-facing state projection.

This PR should include the minimum bounded state projection needed for the
vertical slice. It must not build a second parallel policy engine.

Required tests:

- review edit opens exactly one validation and one exact-thread obligation;
- retry, replay, selector overlap, and nesting do not duplicate obligations;
- wrong-thread resolution does not close the target obligation;
- failed validation returns a structured correction observation;
- later edits invalidate prior validation evidence;
- conservative risk guidance requires baseline/targeted/full evidence as
  configured;
- prompts and validators use the same contract fingerprint;
- chat and task adapters produce identical state transitions; and
- readiness is derived from state and evidence, not prompt prose.

Acceptance gate:

> A single captured procedure and risk policy change actual execution, prevent
> premature completion, and survive retries without duplicate state.

### PR 3: State-centric cutover and final guidance enforcement

Goal: remove the remaining duplicate paths and prove all guidance survives
context movement, restart, and PR delivery.

Scope:

- replace transcript-oriented prompt assembly with
  `EffectiveProcedure + CurrentExecutionState + LatestObservation`;
- remove transcript, event history, rolling context, and prior raw results from
  ordinary model requests;
- replace LLM-authored correctness compaction with deterministic state
  projection;
- make `ExecutionSnapshot` bounded by active complexity;
- move completed action, obligation, and evidence history to the event log or
  historical indexes;
- remove shadow-only execution state from the normal path;
- make nested skills and persona handoffs reference parent contracts and
  obligations;
- add explicit history/artifact inspection actions for exceptional cases;
- implement guidance effects at PR shaping, scope validation, rollout, and
  readiness boundaries;
- add user-visible explanation of which guidance affected each decision; and
- remove adapter-specific intent, policy, and obligation logic.

Required tests:

- deleting all transcript and reasoning data does not change the next action;
- deleting generated summaries does not change readiness;
- state size is bounded for a 200-action constant-complexity workflow;
- repeated resolution and obligation expansion do not increase current state;
- nested skills do not clone intent or obligations;
- restart after every action boundary reproduces the same next valid action;
- external file, Git, PR, review, and CI changes reconcile into current state;
- incremental-PR guidance rejects an over-broad proposed PR;
- single-change-set guidance permits a coherent refactor while still rejecting
  unrelated changes;
- conservative-risk guidance increases validation and blocks missing evidence;
- explicit user supersession changes behavior only at the stated scope; and
- final readiness is reproducible from state and events without conversation.

Acceptance gate:

> The old transcript-based path is no longer required for correctness, and
> delivery guidance is honored at planning, action, validation, PR, and
> readiness boundaries.

## What must not become separate PRs

Keep these within the three slices above:

- a separate “intent schema” project;
- a separate “delivery guidance” project;
- a separate index project;
- a separate prompt-only project;
- a separate obligation project for every procedure;
- one PR per guidance phrase;
- separate chat and task implementations;
- telemetry before the behavior is enforceable; or
- a compatibility layer that leaves multiple live runtime formats.

If a change does not have an end-to-end acceptance gate, it belongs inside the
next vertical PR rather than becoming another foundational PR.

## Migration and rollout

### Observe mode

PR 1 and the first part of PR 2 may record:

- captured source and clause IDs;
- effective-contract matches;
- proposed plan-policy decisions;
- predicted obligation expansions;
- policy conflicts; and
- chat/task disagreements.

Observe mode must not silently write model-derived intent or alter behavior.

### Enforce mode

Enable enforcement for the review-correction vertical slice once:

- source capture is idempotent;
- no action rationale creates intent;
- resolver output is deterministic;
- chat/task contract fingerprints agree;
- obligation expansion is idempotent;
- state replay matches persisted state; and
- negative relevance tests pass.

Expand to delivery guidance only after the slice proves that guidance changes
the relevant planning and readiness decisions.

### Migration rule

Do not support multiple live intent schemas. Migrate or explicitly reject
persisted development data at the store boundary, then run one current runtime
representation. Historical event records may retain their original schema
metadata for audit, but they do not become an alternate execution path.

## Verification matrix

| Scenario | Expected result |
| --- | --- |
| Same user message processed twice | One `IntentSource`, one clause lineage, no duplicate contract |
| “Make small incremental PRs” | Plan creates coherent independently validated slices |
| “Make a big refactor in one PR” | Plan keeps coupled refactor together but rejects unrelated scope |
| Both slicing instructions in same scope | Structured user decision; no silent precedence |
| “Be extra cautious not to regress anything” | Baseline, affected validation, full validation, and recovery requirements apply |
| Model repeats a desired design in rationale | No durable intent is captured |
| Same intent reached through three selectors | One applicable clause and one contract reference |
| Review edit retried three times | One obligation instance with multiple historical attempts |
| Nested review skill | References parent contract and obligations without cloning them |
| Context compaction | Current contract and active state survive by ID and fingerprint |
| Process restart | Same next action and readiness decision without transcript |
| Unrelated documentation edit | Database, typing, and implementation guidance do not apply |
| External force push | Affected evidence invalidates and current state reconciles |
| Final PR creation | Shape, scope, evidence, obligations, and risk gates are evaluated deterministically |

## Architectural tests

These should be permanent tests, not one-time migration tests:

- original source wording has exactly one canonical persisted owner;
- source fingerprints and IDs are stable across restart;
- every derived record references intent rather than copying it;
- action rationale cannot create or modify intent;
- applicability resolution is read-only;
- effective-contract projection is deduplicated and ephemeral;
- same-scope conflicts cannot be resolved by insertion order;
- a later instruction supersedes only the addressed contract and scope;
- plan slicing policy is validated before task creation;
- risk policy changes required evidence and readiness;
- repeated obligation expansion is idempotent;
- completed consequences leave current state;
- nested execution references parent obligations;
- model patches cannot change runtime or environment truth;
- no prompt-only rule can affect readiness;
- no generated summary is authoritative;
- no adapter computes policy independently; and
- current-state size does not grow with historical action count.

## Operational metrics

Measure the architecture directly:

- canonical source count;
- clause count per source;
- source-ingestion duplicate attempts;
- bytes of source text versus bytes of references;
- effective-contract projection size;
- active versus historical state size;
- active versus historical obligation count;
- duplicate obligation-expansion suppressions;
- policy conflicts and user decisions;
- plan-policy violations;
- action and transition blocks by contract;
- required versus produced evidence;
- stale evidence invalidations;
- chat/task fingerprint disagreements;
- restart next-action equivalence; and
- prompt size versus action count at constant active complexity.

Success means that source and history may grow for legitimate audit purposes,
while current execution state and recurring prompt input remain bounded by the
active work—not by the number of times the agent has acted.

## Final acceptance demonstration

Start one proposed-PR workflow with these user instructions:

1. Make small incremental PRs.
2. Be extra cautious not to regress anything.
3. After addressing a review comment, validate the change and resolve the exact
   review thread.

Then run a 100-action scenario that includes:

- a coherent implementation slice;
- an attempted unrelated edit;
- a validation failure;
- a missing-file result;
- a nested skill;
- a review correction;
- large irrelevant tool output;
- context compaction;
- process restart; and
- an external branch or review-state change.

The final record must prove:

- each instruction has one canonical source record;
- no derived record duplicates its wording;
- the slicing and risk contracts are present at planning and publish boundaries;
- the review procedure creates exactly one obligation chain;
- validation evidence is fresh and exact;
- irrelevant guidance is excluded;
- completed consequences leave current state;
- state size remains bounded for constant active complexity;
- restart chooses the same valid next action; and
- final readiness is reproducible without conversation history.

Once this demonstration passes, additional work should be a clearly scoped
product enhancement or a fix to a measured gap—not another foundational PR
that reopens the same architecture.
