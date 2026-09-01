# Historical remaining work for the Opencode-inspired execution plan

> This open-ended inventory is retained as an audit reference. The finite,
> dependency-ordered implementation plan is
> [`opencode-inspired-agent-execution-completion-plan.md`](opencode-inspired-agent-execution-completion-plan.md).

## Purpose

This document is now a historical inventory. The finite completion plan has
been executed through its final consolidated PR. Current closure is proved by
`powdrr-lift final-acceptance` and `powdrr-lift audit-capabilities`, not by the
older item-by-item notes below.

The merged implementation establishes the typed execution-kernel vocabulary and
the consolidated closure PR makes that kernel authoritative for the measured
normal runtime paths. This document retains the pre-closure inventory for
traceability; it is not an active backlog. The current status is defined by the
closure mapping and executable acceptance checks in the audit document.
All sections after the current-status summary are historical findings from the
pre-closure audit and must be read as already addressed by the current proof.

The pre-closure baseline was healthy: the full suite passed (757 tests), formatting,
linting, and mypy pass, and the final enforce-mode acceptance gate passes all
17 checks, including phase walking, replay, partial-failure recovery, typed
exception decisions, scope rejection, readiness, and compaction.

## Current status

The following foundations exist and are merged:

- typed delivery profiles, phases, personas, and artifact handoffs;
- durable execution state, event reduction, replay, and shadow recording;
- typed tool manifests, adapters, registry, and capability classifications;
- exact capability exceptions with signed tokens and file persistence;
- persona packets and basic typed handoff validation;
- typed execution plans and deterministic plan evaluation;
- scoped, versioned behavior guidance;
- action relationships and obligation expansion primitives;
- a shared action lifecycle and structured `PowdrrExecutionError`;
- content-addressed checkpoints and bounded diagnostics;
- evidence, findings, and readiness primitives;
- delivery-plan task compilation and typed-context compaction primitives.

These are kernel seams and unit-tested building blocks. Several are currently
reachable only through direct Python APIs or tests.

The final acceptance implementation now provides executable closure checks for
the integrated path. Run `powdrr-lift final-acceptance` for the deterministic
compiled-plan, full phase/handoff walk, effective-contract, relationship,
lifecycle, adapter-parity, checkpoint recovery, exception decision, scope,
readiness, replay, and compaction scenario, and `powdrr-lift
audit-capabilities` for the built-in capability manifest audit. These commands
are covered by the full test suite and are the required evidence for the final
phase.

The normal builtin capability path is runtime-authoritative: helpers require
one durable `ExecutionRuntime`, and scenario/compatibility paths create an
explicit runtime rather than falling back to an ephemeral broker. The full
historical closure run reported 778 passing tests; run the current verification
suite for the authoritative count.

## Historical closure inventory

The sections below describe the gaps that drove the closure work. Their
completion criteria are now exercised by `powdrr-lift final-acceptance` and
`powdrr-lift audit-capabilities`; future work should be added as a new,
versioned audit finding rather than appended to this historical inventory.

### 1. Make the capability broker authoritative

Every normal tool invocation must pass through a registered adapter and the
capability broker before execution.

Required work:

- Define manifests and conformance adapters for repository reads, bounded file
  edits, file management, registered checks, Git, GitHub, BasedPyright, and
  structured Powdrr operations.
- Route intrinsic dispatch, shell/process execution, Git/GitHub operations,
  edits, enrichment, and diagnostics through `ToolRegistry` and
  `CapabilityBroker`.
- Ensure schemas shown to the LLM are derived from executable capabilities,
  not from ambient Python dispatch or duplicated prompt declarations.
- Enforce active worktree, path, command, environment, output-size, and
  resource constraints at the adapter boundary.
- Record every broker decision in execution events and observer output,
  including shadow-mode disagreements.
- Add adapter conformance tests for command injection, environment leakage,
  symlink escape, changed worktrees, duplicate mutations, stale GitHub
  identifiers, and oversized output.

Completion criterion: in observe mode every existing normal tool call has a
manifest decision, and enforce mode can execute the complete normal tool set
without routine human permission prompts.

### 2. Finish decision-ready capability exceptions

The signed exception substrate exists, but it is not yet a user-facing
decision workflow.

Required work:

- Add shared CLI and MCP operations to inspect pending exception requests and
  approve or deny them.
- Persist requests and denials as durable execution records, not only approved
  decisions.
- Bind and verify execution ID, adapter, manifest fingerprint, semantic action,
  arguments, effects, target, expiration, and use count.
- Prevent altered arguments, effects, targets, adapters, executions, or
  expiration windows from reusing a token.
- Require sufficient context before presenting a request to a human.
- Make denied decisions durable so the same request cannot repeatedly prompt.
- Use stable idempotency keys for approved external-write retries.
- Present a decision packet containing the exact requested operation, affected
  resources, effects, reversibility, reason, expiry, and expected consequence.

Completion criterion: one deliberately unsupported effect produces one exact
decision artifact; the approved call runs once and no broader call can reuse
the decision.

### 3. Wire action relationships into execution and readiness

Relationship expansion currently exists as a standalone API. It must become a
kernel rule rather than optional caller behavior.

Required work:

- Expand relationships during action validation before tool execution.
- Persist relationship expansion as obligation-opened events with source action
  and relationship provenance.
- Satisfy obligations only from exact matching completed actions or typed
  evidence.
- Enforce prerequisite order, especially validation before review-thread
  resolution.
- Integrate review-comment correction with exact thread identity and
  resolution tracking.
- Trigger optimistic-locking and concurrency-evidence obligations for mutable
  row changes even when the proposed edit omits those labels.
- Detect conflicting relationships and produce a structured decision rather
  than choosing an arbitrary order.
- Make readiness and phase transitions reject open relationship obligations.
- Verify obligation replay and resume from the durable event log.

Completion criterion: the two motivating rules change actual execution,
survive resume, and block readiness until their follow-up actions are complete.

### 4. Complete lifecycle parity and typed correction handling

`WorkflowStepRunner` now records a shared lifecycle, but chat and durable-task
adapters still need a parity proof and migration of duplicated behavior.

Required work:

- Define small input, presentation, persistence, and correction protocols for
  chat and task adapters.
- Route both adapters through the same capability decisions, lifecycle events,
  retry counts, no-progress handling, relationship expansion, and final-state
  updates.
- Normalize observer events, error-log events, and execution events.
- Compare identical parsed action sequences through both adapters.
- Classify malformed responses, invalid actions, invalid arguments, constraint
  violations, correctable tool failures, diagnostic failures, provider
  exhaustion, semantic stalls, and terminal persistence failures.
- Replace remaining voluntary agent-correctable `RuntimeError` raises with
  structured `PowdrrExecutionError`; retain provider, cancellation,
  persistence-corruption, and programmer-invariant exceptions as distinct
  types.
- Remove duplicate correction, retry, and no-progress branches only after
  side-by-side parity tests pass.

Completion criterion: identical action sequences produce identical decisions,
events, obligations, retry counts, and final state in chat and task execution.

### 5. Make checkpoints part of mutating execution

The content-addressed checkpoint store and diagnostics are not automatically
used by capability execution.

Required work:

- Create a checkpoint before every mutating effect.
- Associate checkpoint IDs with action lifecycle and execution events.
- Restore both workspace contents and logical execution state on revert.
- Invalidate affected evidence and reopen affected obligations after revert.
- Preserve durable behavior rules across workspace revert.
- Detect partial mutations when a tool fails after changing some files.
- Report external, non-reversible effects without claiming they were restored.
- Add retention and garbage collection that preserves all referenced objects.
- Add checkpoint/revert inspection and decision operations to CLI/MCP where
  recovery is user-facing.

Completion criterion: a deliberately broken repair restores the exact
workspace and logical state from immediately before the action, with complete
audit history.

### 6. Make evidence and readiness enforceable

Evidence and readiness are currently pure APIs; normal checks, edits, review,
and publishing do not consistently feed or consume them.

Required work:

- Have registered validation checks produce typed evidence records with input
  fingerprints and freshness.
- Invalidate only evidence affected by an edit or changed dependency scope.
- Require supporting evidence for fixed, not-applicable, and accepted finding
  dispositions according to configured policy.
- Prevent authors from closing their own blocking findings without the required
  independent evidence or review disposition.
- Model independent reviewer agreement and useful disagreement as typed
  findings/decisions.
- Require complete review, fresh evidence, closed obligations, accepted
  artifacts, and current plan/proposed-PR fingerprints before Publish.
- Add passing and failing guards for every readiness requirement.

Completion criterion: no PR can publish with stale evidence, open required
obligations, unresolved blocking findings, incomplete review, or an outdated
plan/proposed-PR fingerprint.

### 7. Finish delivery-artifact compilation and migrate the default workflow

The compiler can produce tasks, but the existing proposed-PR path does not yet
run from compiled delivery artifacts.

Required work:

- Compile delivery-profile phase assignments and execution-plan units into
  `WorkflowTaskTemplate` and `WorkflowTask` records in the real workflow
  creation path.
- Migrate execute-proposed-PR, run-tests-and-fix, specification review,
  proposed-PR review, code review, review correction/resolution, PR prep, and
  PR creation definitions.
- Preserve actions as the single action declaration on generated tasks.
- Preserve dependencies, artifact inputs/outputs, personas, models, skills,
  and phase references through serialization and reload.
- Ensure profile customization changes assignment details without changing
  kernel-owned guards.
- Add golden one-unit and multi-unit task-graph fixtures.
- Run straightforward, ambiguous-plan, syntax-error, failing-suite,
  scope-expansion, review-correction, and PR-readiness end-to-end scenarios.

Completion criterion: the default feature path runs in enforce mode from a
structured specification to a ready proposed PR through compiled typed tasks.

### 8. Integrate compaction and complete compatibility removal

Compaction currently truncates a supplied mapping but is not part of prompt
construction, resume, or migration.

Required work:

- Integrate compaction into planning, implementation, repair, review, and
  exception handling prompts.
- Retain exact current phase, persona, artifact, plan, action, obligation,
  evidence, finding, exception, rule, and checkpoint references across every
  compaction boundary.
- Add bounded full-output retrieval for truncated tool results.
- Retain resumable typed references across process interruption and restart.
- Add migration diagnostics and compatibility metrics for old persisted
  workflows.
- Remove inferred actions, duplicate orchestration prose, routine permission
  prompts, and duplicate correction/retry loops from the new path.
- Make enforce mode the default for new executions and remove `off` mode after
  the documented migration window.

Completion criterion: new executions have one authoritative control path,
resume/replay parity is proven, and no normal-path human safety prompt appears
in the complete scenario suite.

## Cross-cutting verification still required

The current 684-test result is a baseline, not the final acceptance proof.
Add and run:

- full adapter conformance tests;
- capability decision coverage and shadow disagreement reports;
- exception approval/denial/replay tests;
- action-relationship and obligation event replay tests;
- chat/task lifecycle parity fixtures;
- checkpoint partial-failure and logical-state-revert tests;
- evidence invalidation and readiness guard matrix;
- compiled task-graph golden fixtures;
- compaction/resume/interruption fixtures;
- complete enforce-mode vertical scenarios from specification through ready PR;
- migration tests for supported old persisted workflow schemas and explicit
  failures for unsupported schemas.

Every phase should run the full suite after its new files are committed,
because the repository contains clone-based integration tests that do not see
uncommitted modules.

## Final acceptance gate

The plan is complete only when a default enforce-mode execution can:

1. accept a structured user request;
2. assign the correct phase and persona;
3. produce validated specifications and a proposed PR;
4. compile an execution plan into typed workflow tasks;
5. execute only broker-approved capabilities;
6. create and close relationship-derived obligations;
7. checkpoint mutating work and provide bounded diagnostics;
8. record fresh evidence and typed review findings;
9. correct failures through typed correction packets;
10. compact and resume without losing typed references; and
11. reach Publish only after the deterministic readiness evaluator passes.

The final scenario must also demonstrate that a review-comment correction
resolves the exact thread after validation and that a mutable-row change uses
optimistic locking with concurrency evidence, without requiring a routine
human permission prompt.
