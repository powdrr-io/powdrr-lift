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
the execution package. “Implemented” means the behavior is reachable from the
normal runtime path and has an enforcement or persistence test. “Partial” means
the type, helper, or isolated tests exist but the normal workflow does not yet
depend on it. “Missing” means the documented behavior has no corresponding
runtime implementation.

## Executive summary

The observations in this section and the historical matrices below describe
the state before the consolidated closure PR. The current status is the
measured Phase 5 closure table that follows this summary.

The consolidated runtime-closure changes also remove the last normal
builtin-tool fallback to an ephemeral `CapabilityBroker`, require an
`ExecutionRuntime` for every normal builtin capability invocation, and bind
scenario/helper paths to an explicit durable runtime.

The repository has a substantial typed execution foundation. The following are
implemented or substantially implemented: delivery profiles, phases and
personas, durable execution state and replay, capability manifests and broker
resolution, signed exceptions, behavior guidance, action relationships,
checkpoint storage, evidence/readiness primitives, task compilation, observer
infrastructure, and bounded compaction.

The central difference from the documents is integration. Most of those pieces
are still callable directly from Python or are used by only one execution
adapter. They are not yet one authoritative path for the complete
specification-to-PR workflow. The current test suite is healthy, but it mostly
proves components independently; it does not prove the final enforce-mode
vertical scenario described in the plans.

The highest-risk gaps are:

1. ordinary chat and task operations still have direct dispatch paths beside
   the capability broker;
2. kernel lifecycle events can be projected to durable events, but the normal
   runners do not persist that projection as their source of truth;
3. relationship obligations are enforced by the in-memory kernel, not yet by
   durable workflow state and phase/readiness transitions;
4. checkpoints can be created and restored, but automatic restore, evidence
   invalidation, partial-mutation handling, and user-facing recovery are not a
   complete runtime flow;
5. evidence/readiness and execution-plan compilation are still pure seams,
   rather than the gates used by the default proposed-PR path;
6. compaction preserves selected identifiers but is not the mandatory prompt,
   resume, and interruption mechanism.

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
checks, and the full repository suite is 763 passing tests. The capability
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
| Repository verification | Passed: 763 tests, Ruff, and mypy |

No item in the final acceptance gate is currently unproven. The rows below are
historical findings that drove the closure work.

| Proposal area | Current implementation | Status | Difference / required follow-up |
| --- | --- | --- | --- |
| Separate delivery intent from execution mechanics | Delivery profiles and execution package exist; workflow adapters still own substantial orchestration | Partial | Move normal execution decisions behind the kernel and leave profiles as the customization boundary |
| Typed phase controller | `execution/phases.py`, phase transitions, and profile phase assignments exist | Partial | The default chat/task flow does not consistently use the controller as its transition authority |
| First-class personas | `execution/personas.py` and profile persona contracts exist | Partial | Persona packets are available, but complete child-agent execution and least-privilege tool envelopes are not the default path |
| Typed proposed-PR execution plan | `core/execution_plan.py`, `execution/compile.py`, and evaluators exist | Partial | Compilation is not connected to real workflow creation and proposed-PR execution |
| Compile plans into workflow tasks | `compile_execution_plan()` exists and is tested | Partial | Default feature workflow definitions are not all generated from compiled artifacts |
| Resolve capabilities from phase and step | Manifests, registry, and broker exist; several helpers invoke one-off registries | Partial | One registry/context must be owned by the runtime; normal tools must not bypass it |
| Safe-by-construction normal tools | Shell, file mutation, BasedPyright, fuzzy-match, repository reads, and intrinsic adapters have bounds | Partial | Diagnostics, structured Powdrr operations, some document/context paths, output/resource limits, and decision event integration remain incomplete |
| Rare typed capability exceptions | Signed tokens, persistence, CLI/MCP foundations, and argument binding exist | Partial | Pending/denied lifecycle, full decision packet presentation, idempotency, and end-to-end approval execution are not proven |
| Durable plan separate from mutable state | Durable state/event store and replay exist | Partial | Kernel lifecycle projection is not automatically appended by normal runners |
| Durable user guidance | Guidance contracts, matching, and relationship primitives exist | Partial | Capture/acknowledgement, conflict precedence, revoke/supersede controls, and behavior-changing runtime retrieval are incomplete |
| Action relationship graph | Relationship definitions and kernel obligation expansion/validation exist | Partial | Durable obligation events and readiness integration are not yet authoritative in the normal workflow |
| Validation evidence ledger | Evidence records, invalidation, findings, and readiness evaluator exist | Partial | Registered checks do not consistently emit evidence, and publish does not universally consume the evaluator |
| Unified action lifecycle | Shared `WorkflowStepRunner` and `ActionKernel` exist | Partial | Chat/task parity is not proven for all errors, persistence, corrections, checkpoints, and state updates |
| Immediate diagnostics plus authoritative gates | Bounded diagnostic helper and validation gates exist | Partial | Diagnostics are not a broker-registered, evidence-producing normal capability across all paths |
| Typed correction policy | `PowdrrExecutionError`, correction packets, retries, and observer coaching exist | Partial | Remaining direct correctable errors and duplicate adapter correction branches require inventory and parity tests |
| Checkpoints and rollback | Content-addressed store, pre-mutation broker hook, logical-state API, and safe restore exist | Partial | Store hooks are optional; restore is not a complete state/evidence/obligation recovery transaction |
| Typed-state-first compaction | `compact_execution_context()` preserves a fixed identifier set | Partial | Prompt construction and resume do not require this compactor at every boundary; full-output retrieval is absent |
| Least-privilege child agents | Persona and handoff contracts exist | Partial | Child execution is not yet enforced as a capability envelope derived from persona and phase |

## Status matrix: engineering-plan PR sequence

| Planned PR | Evidence in current tree | Status |
| --- | --- | --- |
| 1. Delivery profile and extension boundary | Delivery profile types, validation, phase/persona contracts | Implemented foundation |
| 2. Execution contracts, store, reducer, shadow phase state | Execution state, event reducer, file store, shadow recorder | Implemented foundation |
| 3. Tool manifests and constrained broker | `execution/tools.py`, `capabilities.py`, builtin adapters | Partial integration |
| 4. Decision-ready capability exceptions | Signed authority, exception store, CLI/MCP support | Partial integration |
| 5. Persona runner and typed handoffs | Persona packets and handoff validation | Partial integration |
| 6. Typed execution-plan generation/evaluation | Plan contracts, compiler, evaluator | Partial integration |
| 7. Durable behavior guidance store | Guidance module and behavior-rule contracts | Partial integration |
| 8. Action relationships and obligation closure | Relationship graph, kernel validation, durable event projection | Partial integration |
| 9. Unified lifecycle and typed correction | Shared runner and typed error model | Partial integration |
| 10. Checkpoints, revert, diagnostics | Checkpoint store, broker checkpoint hook, bounded diagnostics | Partial integration |
| 11. Evidence, findings, readiness | Pure evidence/readiness package | Partial integration |
| 12. Compile delivery artifacts into workflow tasks | `compile_execution_plan()` | Partial integration |
| 13. Deterministic compaction and compatibility removal | Compactor and compatibility diagnostic | Partial integration |

The repository has effectively completed the contract/foundation portions of
the sequence. It has not completed the integration portions that the sequence
explicitly makes acceptance criteria for each PR.

## Remaining-work document: item-by-item differences

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

## Other document differences

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

## Codebase findings not represented clearly in the plans

1. The execution package has two notions of “available tool”: manifest-backed
   capability and legacy helper dispatch. The plans describe only the former,
   so a future audit must treat any direct helper call as a broker-authority
   defect even when the helper performs validation.
2. Checkpoint creation is opt-in at `CapabilityBroker` construction. This is
   weaker than the plan's “before every mutating effect” invariant.
3. `ActionKernel.to_execution_events()` is a useful bridge, but a bridge is not
   persistence. The normal runner still needs a state-store adapter and a
   transaction boundary around action terminal state plus projected events.
4. The test count in the remaining-work document is stale. The current main
   baseline at this audit is 710 tests before adding this audit document.
5. The plans use “normal tool path” and “default workflow” as acceptance
   concepts, but the codebase does not yet expose one named runtime entry point
   whose call graph can be audited for bypasses.

## Recommended completion order

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
14-check acceptance command and the full verification suite remain green.
