# OpenCode-Inspired Execution Completion Plan

## Purpose and stopping rule

This is the finite implementation plan for closing the gaps identified in the
[OpenCode-inspired execution plan](opencode-inspired-agent-execution.md) and
the [implementation audit](../audits/opencode-implementation-audit.md). It
replaces the open-ended sequence of foundation changes with five dependent
implementation PRs and one final acceptance PR.

No PR in this plan is complete because a type or isolated helper exists. Each
PR must connect its behavior to the normal specification-to-proposed-PR path,
and must include the end-to-end fixtures needed to prove that connection.

The work is complete only when the final acceptance scenario satisfies every
criterion in the OpenCode plan's final acceptance gate: prompt, specification,
proposed PR, typed implementation plan, code, validation, review, correction,
compaction/resume, and readiness-controlled publication.

## Invariants for every PR

- The user-facing delivery intent remains in structured specifications and
  proposed-PR documents; execution mechanics remain in the typed runtime.
- Profiles may customize phase assignments, personas, models, skills, and
  policy values, but cannot weaken kernel-owned safety, lifecycle, relationship,
  evidence, or readiness guards.
- Every normal action has exactly one declared action set, one capability
  decision, one lifecycle record, and one durable execution outcome.
- Routine safe operations run without human permission prompts. Human review is
  reserved for a typed, decision-ready exception containing exact scope,
  effects, reversibility, expiry, and consequence.
- Every implementation PR runs the full test suite, Ruff format/check, mypy,
  adapter conformance tests, and the affected vertical scenarios before push.

## Re-audit closure: eight gaps identified on main

The September 2026 re-audit identified eight proof and implementation gaps in
the earlier closure claim. They are closed by the current runtime and
acceptance changes. The numbered findings are retained in the audit document
for traceability; `run_final_acceptance` now reports 25 checks, including one
named executable check for each finding. The stopping rule is satisfied only
when those checks and the full verification suite pass.

See [`docs/audits/opencode-implementation-audit.md`](../audits/opencode-implementation-audit.md)
for the fix and proof associated with each gap.

## PR sequence

### PR 1 — Authoritative execution runtime and action transaction

**Depends on:** current merged foundation.

Make `ExecutionRuntime` the only normal execution authority. Move the broker,
registry, phase controller, persona envelope, relationship kernel, event store,
checkpoint hooks, evidence ledger, and readiness evaluator behind one runtime
context. Chat and durable-task runners become presentation/input adapters for
the same action transaction.

**Implementation scope**

- Create a runtime-owned built-in registry factory; remove one-off brokers and
  direct helper dispatch from chat, task, intrinsic, Git/GitHub, edit, shell,
  diagnostic, and enrichment paths.
- Derive LLM-visible tool/action descriptions from the active runtime
  capability envelope and current phase/persona, including child sessions.
- Make `PhaseController` the sole transition authority and make persona
  packets enforce phase actions and effects at invocation time.
- Run relationship expansion during proposal validation. Persist provenance,
  obligation creation, closure, conflicts, and exact action/evidence matches.
- Add one optimistic-locking transaction around proposal, start, completion or
  failure, capability decision, relationship events, and durable state
  projection. Retry stale state by reloading and replaying, with bounded retry.
- Normalize chat/task correction, observer, error, and execution records.
- Finish the typed exception taxonomy for agent-correctable failures while
  retaining separate provider, cancellation, persistence-corruption, and
  programmer-invariant errors.

**Acceptance gate**

Identical parsed action sequences through chat and durable-task adapters produce
the same capability decisions, lifecycle events, obligations, retries, typed
errors, and final state. A normal tool invocation cannot bypass the runtime
registry. A review edit opens a resolution obligation; a mutable-row edit opens
the locking/concurrency obligation; both survive replay and block transition.

### PR 2 — Safe mutation, exception decisions, checkpoints, and evidence

**Depends on:** PR 1.

Turn safety and validation primitives into mandatory runtime gates.

**Implementation scope**

- Create a checkpoint before every mutating effect and attach its ID to the
  action and execution events.
- Detect partial mutation after a failing adapter and report changed paths and
  reversibility instead of claiming a clean failure.
- Make restore atomically recover workspace and typed execution state; invalidate
  affected evidence and reopen affected obligations while preserving behavior
  rules and the restore audit event.
- Add shared pending/approve/deny/inspect/revoke exception operations for CLI
  and MCP, backed by one decision-packet schema used by chat and tasks.
- Persist denials and suppress duplicate prompts. Verify all token bindings and
  use stable idempotency keys for external writes.
- Make registered checks produce typed, fingerprinted evidence. Implement
  dependency-scoped invalidation and require fresh evidence for finding
  dispositions.
- Enforce independent reviewer identity and disagreement handling.

**Acceptance gate**

An unsupported effect creates exactly one human decision packet; approval runs
exactly the bound operation once, denial is durable, and altered arguments or
targets cannot reuse it. A deliberately failing repair restores the exact
pre-action workspace and logical state, records partial-mutation evidence, and
reopens affected obligations. Publish is blocked by stale evidence, open
obligations, unresolved blocking findings, incomplete review, or stale plan/PR
fingerprints.

### PR 3 — Compiled phases, personas, and the default proposed-PR workflow

**Depends on:** PR 1 and PR 2.

Make the high-level structured delivery artifacts drive the normal workflow.

**Implementation scope**

- Compile delivery-profile phases/personas and execution-plan units into the
  real `WorkflowTask` records, preserving dependencies, artifacts, skills,
  models, phase, action set, and fingerprints through serialization/reload.
- Migrate specification intake, architecture/system/design specification,
  decomposition, proposed-PR review, implementation, validation, code review,
  review correction/resolution, PR preparation, and publication to compiled
  tasks.
- Enforce the phase transition table and persona-specific capability envelope
  for every generated task, including child agents.
- Keep user-customizable intent and profile fields above the generated task
  structure; generated tasks must not introduce alternate safety rules.
- Add golden one-unit, multi-unit, dependency, ambiguous-plan, scope-expansion,
  syntax-error, failing-suite, review-correction, and ready-PR fixtures.

**Acceptance gate**

A default feature request becomes validated structured specifications, then a
  proposed PR, then a typed execution plan and compiled workflow tasks. The
  generated workflow executes in enforce mode with the assigned architect,
  engineering-manager, engineer, and reviewer personas and reaches a proposed
  PR only through the runtime readiness gate.

### PR 4 — Durable intent, compaction, interruption, and migration

**Depends on:** PR 1–3.

Finish behavior change across executions and make bounded context a runtime
property rather than a prompt helper.

**Implementation scope**

- Nominate explicit user instructions as versioned behavior rules, acknowledge
  them, explain which rule applies, resolve precedence conflicts, and support
  supersede/revoke with optimistic locking.
- Project active guidance and relationship-derived follow-ups into every phase
  and child-persona prompt; prove that a future execution changes behavior.
- Require typed compaction at planning, implementation, repair, review,
  exception, and resume boundaries.
- Preserve phase, persona, artifact, plan, action, obligation, evidence,
  finding, exception, rule, and checkpoint references across compaction.
- Store omitted full tool output behind bounded retrieval references and make
  references durable across process interruption/restart.
- Add migration diagnostics and compatibility metrics for supported persisted
  workflow versions; reject unsupported versions explicitly.
- Remove legacy inferred-action, duplicate orchestration, prompt-only
  compaction, and routine permission paths from new enforce-mode executions.

**Acceptance gate**

The instruction “when a review-driven change is made, resolve the comment” and
the instruction to use optimistic locking both alter a later execution without
repeating the instruction. A process interrupted at every phase boundary can
resume with equivalent typed state and decisions, including after compaction.

### PR 5 — Final vertical acceptance, parity, and compatibility removal

**Depends on:** PR 1–4.

This is the final code PR. It is not a new feature slice; it closes any defects
found by the acceptance harness and removes every compatibility path that the
completed design no longer permits.

**Implementation scope**

- Add one deterministic enforce-mode scenario from structured user request to
  ready proposed PR, with architect, engineering manager, engineer, and
  independent reviewer personas.
- Run the same scenario in chat and durable-task modes and compare decisions,
  events, obligations, evidence, checkpoints, corrections, compaction points,
  and final state.
- Include review-comment correction with exact thread resolution after
  validation, mutable-row optimistic locking with concurrency evidence,
  deliberate tool failure/partial mutation, exception approval and denial,
  stale evidence, scope expansion, process interruption, and replay.
- Add an automated audit that enumerates every normal action/tool path and
  fails if it bypasses the runtime, lacks a manifest decision, lacks lifecycle
  persistence, or exposes a legacy permission/inferred-action path.
- Update the audit and remaining-work documents with measured scenario results,
  current test counts, and explicit closure of each item in this plan.

**Final acceptance gate**

The scenario must demonstrate all eleven steps in the OpenCode plan's final
acceptance gate and must publish only after deterministic readiness passes. It
must complete without a routine human safety prompt. The only human interaction
must be an intentionally exercised typed exception decision, when applicable.
No audit item may remain marked “Partial,” “Missing,” or “Not proven”; any
failure is fixed in this PR before the work is considered complete.

## Coverage matrix

| OpenCode-plan requirement | Owning PR | Final proof |
| --- | --- | --- |
| Runtime authority, safe tools, typed phases/personas | 1, 3 | bypass audit and parity fixture |
| Capability exceptions and human decision packets | 2 | approval/denial/reuse scenario |
| Relationships and durable obligations | 1, 5 | review and mutable-row replay |
| Unified lifecycle and typed correction | 1, 5 | chat/task event equivalence |
| Checkpoints and diagnostics | 2, 5 | partial-failure restore |
| Evidence, findings, and readiness | 2, 5 | failing readiness matrix |
| Compiled delivery artifacts | 3 | golden task graphs and vertical run |
| Durable user intent | 4, 5 | cross-execution behavior change |
| Compaction and resumability | 4, 5 | interruption/restart equivalence |
| Compatibility removal and enforce mode | 4, 5 | legacy-path audit |
| Complete prompt-to-publish acceptance | 5 | final deterministic scenario |

## Definition of done

The work is done only after PR 5 passes all repository checks and the final
scenario produces an auditable record showing:

- structured specifications and proposed-PR intent remain the source of what
  should be built;
- compiled typed tasks and phase/persona assignments govern how it is built;
- every normal action is broker-approved, bounded, and durably recorded;
- corrections, relationships, checkpoints, evidence, guidance, and resume all
  change or preserve behavior as specified; and
- deterministic readiness, not an optimistic LLM response, permits publication.
