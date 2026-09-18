# Intent-Preserving Change Control Implementation Plan

## Purpose

This document turns
[`intent-preserving-change-control.md`](intent-preserving-change-control.md)
into an incremental implementation sequence for the current Powdrr codebase.
Each change set establishes a complete vertical capability and leaves the
normal feature flow working.

The implementation must reuse the existing Structrr snapshot/rebase,
Procedrr evaluator and single-decision validation, Workrr coding-agent boundary,
execution obligations, evidence, and readiness mechanisms. It must not create a
second orchestration stack beside them.

## Current implementation seams

### Structrr

- `src/powdrr_lift/structrr/bootstrap.py` produces validated, source-anchored
  repository snapshots.
- `src/powdrr_lift/structrr/rebase.py` computes stable snapshot digests and
  classifies changes that affect proposal references.
- `src/powdrr_lift/structrr/intent.py` stores versioned sources and clauses with
  optimistic updates, exact selectors, supersession, and revocation.

### Procedrr

- `src/procedrr/parser.py` parses and validates procedures and rejects judge
  outputs that are not in single-decision normal form.
- `src/procedrr_evaluator/evaluator.py` owns deterministic operation execution,
  judge invocation, state binding, worklists, branches, and limits.
- `docs/procedrr/skill-definitions/implement-feature.yaml` is the production
  high-level flow used by the Workrr feature endpoint.

### Workrr and OpenCode

- `src/powdrr_lift/workrr/coding_agent.py` defines the bounded
  `ImplementationRequest`, OpenCode permission policy, provider process, attempt
  persistence, observed paths, and diff fingerprint.
- `src/powdrr_lift/workrr/feature_endpoint.py` connects Structrr planning,
  Procedrr execution, OpenCode implementation, validation, review, and PR
  creation.
- `src/powdrr_lift/workrr/coding_agent_validation.py` records validation
  evidence independently of the coding worker.

### Execution control

- `src/powdrr_lift/core/execution_state.py` defines durable obligations,
  evidence, findings, and event reduction.
- `src/powdrr_lift/execution/kernel.py` blocks actions behind open obligations.
- `src/powdrr_lift/execution/phases.py` owns closed phase transitions.
- `src/powdrr_lift/execution/evidence.py` evaluates readiness and invalidates
  stale evidence.

## Architectural rule for all increments

Each increment follows the same ownership rule:

```text
Structrr computes facts and pure state transitions.
Procedrr decides which atomic checks are required and whether progression is legal.
Workrr performs bounded semantic work and implementation.
OpenCode attempts edits under one immutable work order.
```

No increment may add an LLM branch that decides whether a mandatory gate runs.

## Change set 1: versioned Structrr intent transactions

### Goal

Represent each chat update and proposal revision as an explicit, applicable
diff against one accepted Structrr state.

### Add

- `src/powdrr_lift/structrr/state.py`
  - `StructrrStateRef`
  - canonical state fingerprinting
  - repository-tree binding
  - parent-state reference
- `src/powdrr_lift/structrr/intent_diff.py`
  - `ChatUpdate`
  - `SourceSpanDisposition`
  - `IntentOperation`
  - intent-diff parsing and validation
- `src/powdrr_lift/structrr/proposal.py`
  - `ArchitectureOperation`
  - `AcceptanceRequirement`
  - `ProposalRevision`
  - immutable proposal fingerprinting
- `src/powdrr_lift/structrr/apply.py`
  - pure proposal application
  - resulting-state validation
  - candidate snapshot generation

### Extend

- Extend `structrr/intent.py` with explicit intent kinds needed by proposal
  diffs while preserving current source/contract compatibility.
- Extend `structrr/rebase.py` to compare intent clauses and verification
  contracts in addition to current entities, relationships, source subjects,
  and source bindings.
- Add strict schemas under `schemas/` for chat updates, intent transitions, and
  proposal revisions.

### Deterministic validations

- Complete non-whitespace source-span partition.
- Operation target existence and uniqueness.
- Before-fingerprint matching.
- Explicit supersession and subtraction semantics.
- Acyclic clause lifecycle.
- Reference integrity after application.
- Stable candidate-state fingerprint.

### Tests

- Table-driven tests for every operation and invalid precondition.
- Property tests that canonical ordering does not affect fingerprints.
- Apply/reapply idempotence tests where the operation permits idempotence.
- Conflicting and stale proposal fixtures.
- Source-span overlap and omission fixtures.

### Exit condition

A proposal can be parsed, applied to a known snapshot, rejected when stale or
inconsistent, and rendered with a deterministic expected-state fingerprint.

## Change set 2: typed decision obligations and gate compiler

### Goal

Compile proposal validation into durable, replayable, single-decision
obligations whose scheduling and consequences are kernel-owned.

### Add

- `src/powdrr_lift/core/decision_obligation.py`
  - `DecisionSpecification`
  - `DecisionResult`
  - finite outcomes and consequence mappings
  - input and evidence fingerprints
- `src/powdrr_lift/execution/decision_runtime.py`
  - stable worklist ordering
  - deterministic evaluator dispatch
  - Workrr judge dispatch
  - evidence validation
  - stale-result reopening
- `src/powdrr_lift/structrr/gate_compiler.py`
  - proposal-consistency decision expansion
  - implementation-applicability decision expansion
  - post-implementation reconciliation expansion

### Extend

- Add decision-obligation and decision-result event types to execution state.
- Make readiness treat pending, failed, unknown, and stale required decisions as
  blocking.
- Bind each result to predicate version, subject, proposal, Structrr state,
  repository tree, and evidence fingerprints.
- Extend Procedrr validation so all checked-in intent-control judges satisfy
  the existing single-decision validator.

### Required proposal decision families

- source coverage;
- intent operation validity;
- architecture intent-effect declaration;
- hidden observable-behavior change;
- authority change;
- guarantee narrowing or expansion;
- lifecycle and failure-semantics change;
- affected-intent disposition;
- resulting-state consistency; and
- acceptance verifier completeness.

### Tests

- Golden expansion tests: the same proposal always produces the same ordered
  decision IDs.
- One-subject/one-predicate schema tests.
- Replay tests that restore the same open worklist.
- Mutation tests proving that omitting each required decision causes an
  acceptance failure.
- Unknown-result and clarification routing tests.

### Exit condition

Procedrr can accept or reject a proposal only by exhausting the gate compiler's
complete decision worklist. No aggregate Workrr completion Boolean can bypass
the worklist.

## Change set 3: proposal-time intent review

### Goal

Make hidden intent changes and self-inconsistent resulting states block proposal
acceptance in the normal feature flow.

### Add

- A checked-in Procedrr proposal-review definition using deterministic
  `for_each` expansion over architecture operations and affected intent clauses.
- Workrr judge prompts for one semantic predicate at a time.
- An accepted-proposal receipt containing all decision and evidence IDs.

### Integrate

- Insert proposal review after Structrr diff planning and before execution-plan
  compilation in `workrr/feature_endpoint.py`.
- Store proposal revisions and receipts in the feature-run artifact directory.
- Prevent `run_opencode` unless the exact proposal fingerprint has a valid
  acceptance receipt.

### Tests

- Proposal with explicit matching intent change passes.
- Architecture change with no intent-effect declaration fails.
- Architecture change that claims preservation but narrows behavior fails.
- Proposal with contradictory additions fails resulting-state validation.
- Repair creates revision `N+1`; revision `N` remains immutable.

### Exit condition

The production Workrr feature path cannot invoke OpenCode for an unaccepted or
semantically incomplete proposal revision.

## Change set 4: implementation-start revalidation

### Goal

Revalidate every accepted proposal against the current Structrr and repository
state immediately before implementation.

### Extend Structrr rebase

- Accept proposal references to intent clauses, verification contracts,
  entities, relationships, source subjects, and source bindings.
- Produce operation-pair compatibility facts.
- Distinguish clean, mechanical, context-refresh, targeted-update, conflict,
  and invalidated outcomes.
- Preserve deterministic remapping evidence.

### Procedrr flow

1. Compare accepted proposal base fingerprints with current fingerprints.
2. If unchanged, issue an applicability receipt.
3. If changed, expand one compatibility decision per affected proposal
   operation.
4. For mechanical changes, create a new immutable proposal revision.
5. Route the new revision through proposal review.
6. Block conflicts and invalidations with exact evidence.

### Tests

- Unrelated accepted state changes remain clean.
- Source moves with stable identity rebase mechanically.
- Changed referenced behavior requires targeted update.
- Removed target invalidates the proposal.
- A revised proposal cannot inherit stale proposal-review evidence.
- A process restart reruns applicability when the current base changes.

### Exit condition

OpenCode cannot start from a stale proposal receipt. Every attempt records an
applicability receipt for its exact Structrr state and repository base.

## Change set 5: targeted OpenCode intent packets

### Goal

Compile the smallest complete, explainable intent closure for each OpenCode
attempt and enforce its mechanical constraints.

### Add

- `src/powdrr_lift/structrr/intent_resolution.py`
  - seed selection from proposal operations
  - typed graph closure
  - global invariant inclusion
  - relevance paths
  - conflict reporting
- `src/powdrr_lift/workrr/work_order.py`
  - `IntentPacket`
  - `OpenCodeWorkOrder`
  - deterministic prompt renderer
  - packet and prompt fingerprints
- `src/powdrr_lift/workrr/discovery_agent.py`
  - read-only change-surface request and result contracts

### Evolve `ImplementationRequest`

Replace free-standing copied prompt facts with references plus a compiled
packet. Preserve backward-compatible parsing for persisted v1 requests during a
bounded migration window.

The new request includes:

- proposal and Structrr fingerprints;
- proposal operation IDs;
- required intent changes;
- retained intent and relevance paths;
- prohibited changes and non-goals;
- readable and writable paths;
- target symbols and capabilities;
- acceptance and evidence requirements;
- required actualization-manifest schema; and
- the exact rendered-prompt fingerprint.

### Procedrr pre-invocation checks

- accepted proposal receipt exists;
- applicability receipt is current;
- objective is singular;
- operation coverage is complete;
- affected-intent dispositions are complete;
- relevance paths resolve;
- no conflicts remain;
- scope and capabilities are explicit;
- acceptance verifiers are complete; and
- packet fits the configured context budget.

If the packet is too large, split the execution unit deterministically rather
than dropping intent.

### Enforcement

- Generate OpenCode permissions from packet capabilities and commands.
- Continue checking `HEAD`, clean start state, and observed changed paths.
- Exclude proposal and accepted Structrr state paths from implementation writes.
- If feasible with the provider event stream, inspect proposed mutation targets
  before execution and suspend on newly applicable intent.
- Otherwise require read-only discovery for uncertain scopes and keep edit
  attempts bounded to one known operation.

### Tests

- Exact intent closure and relevance-path fixtures.
- Global invariant inclusion.
- Irrelevant old intent omission.
- Negative-constraint inclusion for a relevant capability.
- Conflict blocks packet rendering.
- Prompt rendering is deterministic.
- Mandatory packet overflow causes unit splitting or a blocking result.
- Newly discovered path adds intent and invalidates the old packet.
- OpenCode receives the exact persisted prompt and permission policy.

### Exit condition

Every production OpenCode attempt is traceable to one accepted proposal,
current Structrr state, targeted intent packet, exact prompt, and matching
permission policy.

## Change set 6: actualization and post-implementation reconciliation

### Goal

Prove that actual changes fulfill explicit additions and subtractions, realize
expected architecture changes, preserve retained intent, and contain no
unexplained drift.

### Add

- `src/powdrr_lift/workrr/actualization.py`
  - actualization-manifest parsing
  - observed Git and symbol change inventory
  - proposal-operation candidate mapping
- `src/powdrr_lift/structrr/reconciliation.py`
  - candidate snapshot construction from observed state
  - actual semantic diff
  - retained-intent impact closure
- A checked-in Procedrr reconciliation definition.

### Decision expansion

Create one decision for each:

- intent operation fulfillment;
- architecture operation realization;
- acceptance requirement;
- actual change explanation;
- affected retained-intent preservation;
- invalidated evidence replacement; and
- candidate-state consistency condition.

### Acceptance transaction

Add a Structrr acceptance receipt and optimistic state installation operation.
It accepts only when:

- proposal and applicability receipts are current;
- all reconciliation decisions pass;
- evidence is fresh for the candidate repository tree;
- the expected prior Structrr fingerprint still matches; and
- no blocking obligations or findings remain.

### Tests

- Full implementation passes with exact operation mappings.
- Missing proposed behavior is incomplete.
- Unexpected source change is drift.
- Wrong behavioral realization is a semantic mismatch.
- Explicit subtraction removes only the intended guarantee.
- Retained old intent regression blocks acceptance.
- Changed verification code invalidates its prior evidence.
- Concurrent Structrr advancement rejects state installation.
- Replay produces the same acceptance decision.

### Exit condition

The normal feature flow cannot publish or install a new Structrr state until
proposal-to-actual reconciliation is complete.

## Change set 7: cumulative drift monitoring

### Goal

Detect slow drift that may survive individual proposal reviews because of
missing or outdated relationship data.

### Add

- Intent health records containing architecture links, source bindings,
  verification contracts, evidence fingerprints, last verified tree, and
  freshness policy.
- Deterministic triggers for full reconciliation.
- A Procedrr worklist that checks one active intent clause at a time.
- Reports for orphaned intent, missing enforcement, stale evidence, and accepted
  exceptions nearing expiry.

### Trigger conditions

- global invariant change;
- removal of a source binding or verifier;
- clause with no remaining architecture link;
- evidence freshness expiry;
- identity migration;
- release milestone; or
- configured accepted-transition interval.

### Tests

- Orphaned active intent is detected.
- Removed test reopens its protected clause.
- Unrelated changes do not invalidate evidence globally.
- Full audit survives interruption and resumes at the same work item.

### Exit condition

Powdrr can produce an evidence-backed health disposition for every active intent
without loading the historical conversation into a model.

## Migration strategy

### Existing Structrr snapshots

- Treat existing snapshots as legacy state with no intent-transition history.
- Generate one migration state that preserves all current entities,
  relationships, source subjects, and source bindings.
- Import current durable intent clauses by stable ID.
- Record missing verification relationships as explicit health findings rather
  than inventing them.

### Existing proposals

- Existing proposals may run through a compatibility adapter only in observe
  mode.
- Enforce mode requires a versioned proposal revision and proposal-review
  receipt.
- Do not infer subtraction from absence during migration.

### Existing implementation requests

- Continue reading `implementation-request-v1` artifacts for audit and replay.
- New production attempts use `opencode-work-order-v1`.
- Do not silently upgrade and rerun an old request under a new packet.

## Verification strategy

Every change set runs:

- focused unit and integration tests;
- Procedrr parser and evaluator tests;
- Workrr/OpenCode adapter conformance tests;
- Structrr real-repository fixtures;
- production feature-flow scenarios;
- `ruff format --check .`;
- `ruff check .`;
- `mypy src tests`; and
- the complete pytest suite.

The final vertical scenario must demonstrate:

1. a user update that adds intent;
2. an architecture operation that initially omits the intent effect;
3. proposal review blocking the omission;
4. a corrected and accepted proposal revision;
5. an intervening Structrr change before implementation;
6. implementation-start revalidation and proposal revision;
7. targeted intent-packet compilation;
8. a real or deterministic OpenCode implementation attempt;
9. observed actual diff and validation evidence;
10. detection and repair of one drift finding;
11. successful reconciliation;
12. atomic accepted-state installation; and
13. restart/replay with the same terminal state.

## Recommended PR order

1. Structrr intent transaction schemas and pure apply functions.
2. Decision obligations and gate compiler.
3. Proposal-time intent review in the production feature flow.
4. Implementation-start applicability and revision loop.
5. Targeted OpenCode intent packets and discovery mode.
6. Actualization and post-implementation reconciliation.
7. Cumulative drift monitoring and final vertical acceptance.

Each PR should be independently reviewable and should add one enforced boundary,
not merely unused schema types.

## Final stopping rule

The program is complete only when the production Workrr feature command cannot:

- invoke OpenCode from an unreviewed proposal;
- invoke OpenCode from a stale proposal;
- omit governing intent from a work order without a failing decision;
- accept an unexplained actual change;
- accept stale evidence;
- publish before every required decision is terminal and passing; or
- install a Structrr state against a stale parent fingerprint.

