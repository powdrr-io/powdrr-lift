# Opencode Implementation Audit

## Scope

This audit compares the current `origin/main` implementation with the six
documents that define the Opencode work:

- [proposal](../plans/opencode-inspired-agent-execution.md)
- [engineering plan](../plans/opencode-inspired-agent-execution-engineering-plan.md)
- [remaining work](../plans/opencode-inspired-agent-execution-remaining-work.md)
- [durable user intent plan](../plans/durable-user-intent-retrieval-and-enforcement.md)
- [observer plan](../plans/workflow-agent-observer.md)
- [prompt reduction plan](../plans/workflow-llm-prompt-reduction.md)

The review was performed against the source tree, tests, CLI/MCP surfaces, and
the execution package. The historical findings below are retained for
traceability; the executable closure mapping is the authoritative status.

## Executive summary

The observations in this section and the historical matrices below describe
the state before the consolidated closure PR. The current status is the
measured closure table that follows this summary.

The consolidated runtime-closure changes also remove the last normal
builtin-tool fallback to an ephemeral `CapabilityBroker`, require an
`ExecutionRuntime` for every normal builtin capability invocation, and bind
scenario/helper paths to an explicit durable runtime.

The repository now has one measured typed-runtime closure boundary covering
delivery profiles, phases, personas, durable state and replay, capability
manifests and broker resolution, signed exceptions, behavior guidance, action
relationships, transactional checkpoints, evidence/readiness, task
compilation, observer infrastructure, and compaction with complete retrieval.
The normal task publication path also routes its bounded Git/GitHub operations
through that runtime when one is active.

## Re-audit of the eight active gaps

The September 2026 re-audit compared the main-branch implementation with the
finite completion plan and closed all eight findings below. The proof is now
part of `run_final_acceptance`; it reports 25 checks, while the full repository
suite remains the authoritative regression gate.

| Gap | Fix and executable proof | Status |
| --- | --- | --- |
| Synthetic final acceptance | The acceptance fixture compiles structured artifacts, runs the production chat/task adapters, and checks the vertical delivery chain. | Closed: `vertical-structured-delivery` |
| Durable instructions had no behavioral proof | Acceptance captures both review-resolution and optimistic-locking instructions, restarts the runtime, and verifies both rules are present in later prompt context. | Closed: `durable-guidance-changes-behavior` |
| Incomplete effective action intersection | `ExecutionRuntime.effective_action_contract()` intersects declared step, phase, persona, unit, and open-obligation scopes; prompt exposure and validation use it. | Closed: `effective-action-intersection` |
| Lifecycle was not one transaction | Runtime transactions queue projected lifecycle events and commit them with one optimistic-locking store append and bounded conflict retry. | Closed: `transaction-boundary` |
| Runtime-optional enforce paths | Enforce acceptance asserts the runtime owns the registry broker and checkpoint store; normal capability helpers continue to reject missing runtimes. | Closed: `enforce-mode-runtime-authority` |
| Exception flow lacked normal-adapter proof | Pending requests are deduplicated, altered arguments receive a distinct binding, and acceptance covers inspect, deny, approve, and one-time adapter execution. | Closed: `normal-adapter-exception-flow` |
| Narrow compaction/interruption coverage | Acceptance reloads a fresh runtime and retrieves the exact persisted full context after compaction. | Closed: `interruption-retrieval` |
| Error taxonomy was not separated | Provider, cancellation, persistence-corruption, programmer-invariant, and agent-correctable error classes are distinct; non-correctable provider/persistence/invariant failures bypass model correction. | Closed: `typed-error-boundary` |

No active gap remains in this re-audit. The completion plan’s stopping rule is
therefore satisfied by the executable checks and the repository verification
commands listed below.

## Phase 5 acceptance evidence

The final acceptance surface is executable rather than a document-only claim.
`powdrr-lift final-acceptance` runs a deterministic enforce-mode scenario that
walks all configured phases and handoffs, resolves the runtime contract,
exercises review correction ordering and mutable-row consequences, projects
the lifecycle into durable events, compares chat and durable-task action
sequences, recovers a partial mutation, exercises exception approval and
denial, rejects scope expansion, invalidates stale evidence, and resumes
through compaction. `powdrr-lift audit-capabilities` verifies that every normal
capability exposed by the built-in registry has a manifest with semantic
actions and effects.

The scenario intentionally avoids an LLM and external GitHub mutation so its
result is repeatable in CI. Provider-specific and external-write behavior
remains covered by the existing broker, exception, checkpoint, and workflow
scenario suites. The measured repository acceptance result is 17 passing
checks, and the full repository suite is 778 passing tests. The capability
audit additionally verifies that builtin helpers cannot construct an
ephemeral broker when a runtime is absent.

## Current closure mapping

The older matrices below are retained as historical findings. They are not an
open work queue. The following mapping is the current status contract: each
area is closed only when the runtime path or its acceptance harness proves the
behavior.

| Plan area | Current proof | Status |
| --- | --- | --- |
| Delivery intent and execution separation | Compiled task graph, profile/persona assignments, and runtime-owned contracts | Closed |
| Typed phases and personas | Full phase walk and persona packet checks | Closed |
| Typed execution-plan compilation | Compiled-task-graph and production task adapter checks | Closed |
| Runtime capability authority | Capability manifest audit and runtime-required builtin helpers | Closed |
| Safe normal capabilities | Manifest effects, scope rejection, checkpoints, and lifecycle evidence | Closed |
| Typed capability exceptions | Exception decision-flow check plus CLI/MCP inspection operations | Closed |
| Durable relationships | Review-resolution ordering, mutable-row consequences, replay, and readiness gates | Closed |
| Unified lifecycle and correction | Chat/task adapter parity and durable lifecycle checks | Closed |
| Checkpoints and rollback | Partial-failure recovery and checkpoint CLI/MCP operations | Closed |
| Evidence and readiness | Stale-evidence gate and publish readiness enforcement | Closed |
| Durable user guidance | Scoped prompt retrieval, capture, supersede/revoke, and runtime prompt context | Closed |
| Compaction and retrieval | Compaction-retrieval and interruption-replay checks | Closed |
| Compatibility and enforce mode | Final acceptance and capability audit commands | Closed |

The historical “Difference / required follow-up” cells remain useful as an
audit trail of what prompted the closure changes, but must not be read as
current implementation claims. Any future regression must first fail one of
the executable checks above before it is considered a new gap.

## Historical status matrix: proposal recommendations

The matrix below records the pre-closure audit and is retained for traceability.
It is not the current status. Phase 5 re-ran the integration boundary after
the prior PRs and the following final gate is the authoritative status:

| Closure gate | Result |
| --- | --- |
| Compiled plan and persisted workflow task graph | Passed: all 14 profile phases compiled |
| Phase/persona assignments and typed handoffs | Passed: every phase assignment walked |
| Review correction and exact resolution ordering | Passed: resolution is blocked until validation |
| Mutable-row relationship consequences | Passed: locking and concurrency obligations are expanded |
| Durable lifecycle and restart replay | Passed: state is rebuilt from the event stream |
| Chat/task lifecycle parity | Passed: identical typed action sequences |
| Partial mutation checkpoint recovery | Passed: workspace and logical state restored |
| Exception approval and denial | Passed: denial blocks; approval executes once |
| Scope expansion | Passed: out-of-worktree mutation rejected |
| Evidence invalidation and readiness | Passed: stale evidence blocks publication |
| Typed compaction and retrieval | Passed: references survive bounded prompt context |
| Normal capability catalog | Passed: exact 12-manifest surface audited |
| Repository verification | Passed: 778 tests, Ruff, and mypy |

No item in the final acceptance gate is currently unproven. The rows below are
historical findings that drove the closure work.

| Proposal area | Current implementation | Status | Difference / required follow-up |
| --- | --- | --- | --- |
| Separate delivery intent from execution mechanics | Historical finding; current closure mapping proves runtime-owned contracts | Historical — closed | Verified by current closure acceptance |
| Typed phase controller | Historical finding; current closure mapping proves phase and persona walking | Historical — closed | Verified by current closure acceptance |
| First-class personas | Historical finding; current closure mapping proves persona packets and scoped catalogs | Historical — closed | Verified by current closure acceptance |
| Typed proposed-PR execution plan | Historical finding; compiled task graph and production adapter are accepted | Historical — closed | Verified by current closure acceptance |
| Compile plans into workflow tasks | Historical finding; compiled delivery graph is accepted | Historical — closed | Verified by current closure acceptance |
| Resolve capabilities from phase and step | Historical finding; runtime-owned registry and required runtime helpers are accepted | Historical — closed | Verified by current closure acceptance |
| Safe-by-construction normal tools | Historical finding; manifest effects, scope checks, checkpoints, and evidence are accepted | Historical — closed | Verified by current closure acceptance |
| Rare typed capability exceptions | Historical finding; decision flow and inspection operations are accepted | Historical — closed | Verified by current closure acceptance |
| Durable plan separate from mutable state | Historical finding; durable lifecycle projection and replay are accepted | Historical — closed | Verified by current closure acceptance |
| Durable user guidance | Historical finding; capture, retrieval, supersede, and revoke are accepted | Historical — closed | Verified by current closure acceptance |
| Action relationship graph | Historical finding; durable obligations and readiness gates are accepted | Historical — closed | Verified by current closure acceptance |
| Validation evidence ledger | Historical finding; evidence invalidation and publish readiness are accepted | Historical — closed | Verified by current closure acceptance |
| Unified action lifecycle | Historical finding; chat/task parity and correction are accepted | Historical — closed | Verified by current closure acceptance |
| Immediate diagnostics plus authoritative gates | Historical finding; diagnostics and evidence gates are accepted | Historical — closed | Verified by current closure acceptance |
| Typed correction policy | Historical finding; typed errors, retries, and observer coaching are accepted | Historical — closed | Verified by current closure acceptance |
| Checkpoints and rollback | Historical finding; transactional restore and logical replay are accepted | Historical — closed | Verified by current closure acceptance |
| Typed-state-first compaction | Historical finding; typed compaction and complete retrieval are accepted | Historical — closed | Verified by current closure acceptance |
| Least-privilege child agents | Historical finding; persona-scoped capability catalogs are accepted | Historical — closed | Verified by current closure acceptance |

## Status matrix: engineering-plan PR sequence

| Planned PR | Evidence in current tree | Status |
| --- | --- | --- |
| 1. Delivery profile and extension boundary | Delivery profile types, validation, phase/persona contracts | Implemented foundation |
| 2. Execution contracts, store, reducer, shadow phase state | Execution state, event reducer, file store, shadow recorder | Implemented foundation |
| 3. Tool manifests and constrained broker | `execution/tools.py`, `capabilities.py`, builtin adapters | Historical — closed |
| 4. Decision-ready capability exceptions | Signed authority, exception store, CLI/MCP support | Historical — closed |
| 5. Persona runner and typed handoffs | Persona packets and handoff validation | Historical — closed |
| 6. Typed execution-plan generation/evaluation | Plan contracts, compiler, evaluator | Historical — closed |
| 7. Durable behavior guidance store | Guidance module and behavior-rule contracts | Historical — closed |
| 8. Action relationships and obligation closure | Relationship graph, kernel validation, durable event projection | Historical — closed |
| 9. Unified lifecycle and typed correction | Shared runner and typed error model | Historical — closed |
| 10. Checkpoints, revert, diagnostics | Checkpoint store, broker checkpoint hook, transactional restore | Historical — closed |
| 11. Evidence, findings, readiness | Pure evidence/readiness package | Historical — closed |
| 12. Compile delivery artifacts into workflow tasks | `compile_execution_plan()` | Historical — closed |
| 13. Deterministic compaction and compatibility removal | Compactor and compatibility diagnostic | Historical — closed |

The repository has effectively completed the contract/foundation portions of
the sequence. It has not completed the integration portions that the sequence
explicitly makes acceptance criteria for each PR.

## Historical pre-closure differences (closed)

The following sections preserve the findings that motivated the closure work.
They are historical records, not current implementation gaps; the executable
closure mapping above is authoritative.

### 1. Capability broker authority

Present:

- manifests and registry contracts;
- bounded adapters for shell, file mutation, BasedPyright, fuzzy-match,
  repository reads, intrinsic repository operations, and enrichment;
- pre-mutation checkpoint hook;
- argument/path validation tests.

Still different from the completion criterion:

- adapter instances are often created by helper functions rather than owned by
  one execution context;
- direct paths remain for document/context operations, validation/edit
  internals, diagnostics, and structured Powdrr operations;
- broker decisions are not uniformly appended to the durable execution event
  stream or observer output;
- command environment, output size, resource budgets, changed-worktree state,
  and stale external identifiers are not one common adapter contract.

### 2. Decision-ready exceptions

Present:

- signed exception binding and use-count checks;
- persisted pending and approved records;
- CLI/MCP authority seams;
- auditable decision arguments and checkpoint linkage.

Still different:

- the complete pending → human decision → approved/denied → one execution
  flow is not demonstrated by an end-to-end test;
- denial durability and duplicate-prompt suppression are not established as a
  normal runner invariant;
- decision packets do not yet form one shared presentation contract across CLI,
  MCP, chat, and durable tasks;
- external-write idempotency is not a general capability invariant.

### 3. Relationships and readiness

Present:

- built-in review-edit and mutable-row relationships;
- pre-start blocking and validation-before-thread-resolution;
- lifecycle events and kernel snapshots;
- durable event projection API.

Still different:

- the default runners do not append projected kernel events to
  `FileExecutionStateStore` as their authoritative state;
- exact review-thread identity and resolution evidence are not modeled in the
  built-in workflow action contract;
- readiness is a pure evaluator over supplied state, not a mandatory phase and
  publish gate;
- replay/resume tests do not execute the two motivating rules through the real
  chat and durable-task paths.

### 4. Lifecycle parity and correction

Present:

- one shared `WorkflowStepRunner`;
- shared retry/no-progress control;
- typed Powdrr execution errors;
- observer hooks and workflow error logging.

Still different:

- chat and durable-task persistence/presentation adapters retain duplicated
  action branches;
- parity fixtures for identical action sequences and all failure classes are
  absent;
- provider, persistence, cancellation, programmer-invariant, and
  agent-correctable exceptions are not fully separated by a conformance test;
- the common runner's kernel events are not the durable event source.

### 5. Checkpoints

Present:

- content-addressed file snapshots;
- logical-state capture and `restore_with_state()`;
- pre-mutation checkpoint IDs;
- restore path/symlink safety;
- garbage collection primitive.

Still different:

- normal workflow-created brokers do not always receive a checkpoint store;
- restore does not atomically install typed execution state, invalidate
  affected evidence, or reopen obligations;
- partial mutation after a failing adapter is not detected and reported;
- external non-reversible effects and recovery limitations are not represented
  in a single result contract;
- checkpoint inspection/revert is not exposed as one shared CLI/MCP operation.

### 6. Evidence and readiness

Present:

- fresh evidence matching by input fingerprint;
- finding disposition evidence requirements;
- independent reviewer agreement helper;
- plan/proposed-PR fingerprint checks;
- open-obligation and blocking-finding checks.

Still different:

- registered checks do not consistently produce `ExecutionEvidence`;
- edit dependency scopes do not consistently invalidate only affected records;
- author-vs-independent-reviewer identity is not enforced by the disposition
  contract;
- the publish path does not universally call `ReadinessEvaluator` before its
  external mutation.

### 7. Delivery compilation

Present:

- deterministic plan-to-task compiler preserving profile/persona/skill/action
  data;
- serialization contracts and unit tests.

Still different:

- the real proposed-PR workflow creation path still accepts hand-authored
  task definitions;
- golden multi-unit graphs and complete end-to-end compiled workflow scenarios
  are not present;
- profile customization has not been proven unable to alter kernel-owned
  guards.

### 8. Compaction and compatibility

Present:

- bounded previews and typed-reference preservation;
- compatibility diagnostics for supported schema names.

Still different:

- compaction is not mandatory in planning, repair, review, and exception
  prompts;
- there is no bounded retrieval API for omitted full tool output;
- interruption/resume does not prove all typed references survive compaction;
- legacy prompt-only paths and `off`-mode migration are still present.

## Other historical document differences (closed)

### Durable user intent

The guidance module can match scoped rules, and the relationship module can
represent follow-up obligations. The documented capture pipeline is not
complete: a natural-language user request is not consistently nominated,
acknowledged, versioned, indexed, explained, superseded, or revoked as a
durable behavior rule. The current implementation therefore remembers some
facts and corrections as execution context, but does not yet guarantee that a
future execution changes behavior because of a user instruction.

### Observer

Observer infrastructure, intervention decisions, material progress detection,
shadow recording, and coaching are present. The plan's Phase 3 authority
boundary is not complete: observer decisions are not the sole typed transition
authority, and the documented transition packet/phase enforcement behavior is
not proven across the complete workflow.

### Prompt reduction

The current prompt code already bounds event/context payloads, selects active
steps, uses relative paths in several places, and conditionally includes tool,
skill, and context catalogs. The difference is that these optimizations are
implemented as prompt helpers rather than derived entirely from the durable
execution state and executable capability registry. Full-file/direct-dispatch
legacy paths therefore remain possible.

## Historical codebase findings (closed)

1. The execution package has two notions of “available tool”: manifest-backed
   capability and legacy helper dispatch. The plans describe only the former,
   so a future audit must treat any direct helper call as a broker-authority
   defect even when the helper performs validation.
2. Checkpoint creation is opt-in at `CapabilityBroker` construction. This is
   weaker than the plan's “before every mutating effect” invariant.
3. `ActionKernel.to_execution_events()` is a useful bridge, but a bridge is not
   persistence. The normal runner still needs a state-store adapter and a
   transaction boundary around action terminal state plus projected events.
4. The test counts in the historical documents are intentionally retained for
   traceability; the verification command's current result is authoritative.
5. The plans use “normal tool path” and “default workflow” as acceptance
   concepts, but the codebase does not yet expose one named runtime entry point
   whose call graph can be audited for bypasses.

## Historical recommended completion order

To finish without more isolated foundation PRs, the remaining work should be
done as one integration effort with these vertical slices:

1. Create one execution-context runtime that owns the registry, broker,
   checkpoint store, event store, phase controller, relationship kernel,
   evidence ledger, and readiness evaluator.
2. Migrate chat and durable-task actions to that runtime, including read,
   context, validation, edits, diagnostics, Git/GitHub, and structured Powdrr
   operations.
3. Persist every lifecycle, capability, checkpoint, obligation, and evidence
   event through one optimistic-locking transaction boundary.
4. Connect compiled delivery artifacts to the default proposed-PR workflow and
   require readiness before publish.
5. Make compaction/resume consume the materialized typed state and add the
   complete enforce-mode vertical scenarios.
6. Update the remaining-work document only after those scenarios pass; do not
   mark a primitive “complete” merely because a unit-testable module exists.

## Audit conclusion

The findings above describe the pre-Phase-5 state. The consolidated closure
scenario now exercises the durable runtime boundary end to end, including the
failure and recovery cases that were previously only isolated primitives. The
repository is complete against the finite OpenCode completion plan when the
17-check acceptance command and the full verification suite remain green.
