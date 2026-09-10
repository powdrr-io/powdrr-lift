# LLM Execution Language Safety Implementation Plan

## Objective

Evolve the current skill and workflow implementation into a compiler-enforced
execution language that can issue explicit safety, liveness, and termination
guarantees. Preserve the useful flexibility of nested skills while preventing
undeclared effects, hidden authority amplification, unsupported completion
claims, ineffective retries, and unbounded execution.

The target design is defined in
`docs/design/llm-execution-language-safety.md`. This document maps that design
onto the current repository and identifies the required code changes,
migration order, and acceptance tests.

## Current foundation

The repository already contains substantial pieces of the target system.

| Area | Current implementation | Useful property |
| --- | --- | --- |
| Skill schema | `core/skill_specification.py` | Typed steps, inputs, outputs, actions, pre-steps, gates, completion, and nested skills |
| Runtime step policy | `workflow_step_behavior.py` | Central ownership rules for governed, predicated, runner, gate, and nested-skill steps |
| Provider action schema | `workflow_chat_agent.py::_step_action_response_schema` | Per-step action enumeration and typed output envelopes |
| Shared action engine | `workflow_llm.py` | Common parsing, repair, progress observation, and execution strategy boundary |
| Static compiler | `workflow_definition_analysis.py` | Parsing, CFG construction, handoff checks, prompt checks, liveness diagnostics, baselines, and warning budgets |
| Abstract liveness model | `workflow_liveness.py` | Capability summaries, abstract states, transitions, progress, and runtime/static conformance |
| Runtime tool contract | `core/tool_manifest.py` and `execution/tools.py` | Semantic actions, coarse effects, idempotency, evidence producers, adapter validation, and observed effects |
| Capability enforcement | `execution/capabilities.py` | Step action checks, effect checks, checkpoints, exception resolution, and decision history |
| Capability exceptions | `core/capability_exception.py` | Exact argument binding, manifest fingerprint, signed approval, expiration, and use count |
| Durable truth | `core/execution_state.py` | Action, obligation, evidence, finding, checkpoint, and capability decision events |
| Workflow compilation | `execution/compile.py` | Closed phase action contracts and durable task generation |

This is not a greenfield design. The main work is to make these components use
one semantic contract and to close compatibility paths that weaken guarantees.

## Current gaps

### Actions are closed only in some definitions

`SkillStep.actions_declared` distinguishes an explicit empty action set from an
omitted legacy set. When actions are omitted, `workflow_chat_agent.py` infers a
broad action catalog and adds universal actions. This preserves compatibility
but prevents a general action-safety guarantee.

`prompt_user` and `next_step` are also added implicitly for many steps. A
validator cannot claim that the authored action set is closed while runtime
behavior expands it.

### Actions and outcomes are conflated

The provider returns an `action`, and `next_step`, `complete`, and
`emit_outputs` carry some outcome semantics. `WorkflowActionOutcome` records
only whether execution continues and an optional process exit code. There is no
closed semantic outcome type shared by definitions, the compiler, and runtime.

Consequences include:

- advancement remains model-selectable for governed steps;
- result-to-transition mapping is spread across runner code;
- failure classes do not form an exhaustive per-step union; and
- local outcome completeness cannot be checked directly.

### Operation state is durable data but not yet a language contract

`core/execution_state.py` records actions, evidence, obligations, and related
events, but a definition cannot yet require a closed lifecycle for each
operation or declare which terminal record permits an output or transition.
The LLM can therefore receive prose about earlier work without a standard,
read-only projection of whether the underlying operation succeeded, failed, or
is ambiguous.

Template instantiation has the same gap. A generated workflow is useful only
after it has been persisted, parsed, validated, compiled, and fingerprinted,
but those facts are not yet one typed runner-owned operation result consumed
by the next workflow step.

### Static and runtime effects are separate models

`workflow_liveness.CapabilityEffect` contains reads, writes, products,
invalidations, determinism, and postconditions. `core.tool_manifest.ToolManifest`
contains coarse `ToolEffect` values, scope, sandbox profile, idempotency, and
evidence producers. The duplication allows drift and requires conformance tests
instead of making drift structurally impossible.

Shell effect recognition is currently a static command classifier. Runtime
shell execution is represented primarily as `PROCESS_EXECUTION`, which is too
coarse to prove path, network, Git, or evidence properties.

There is no current trust classification that distinguishes a contract that is
kernel-enforced from one supplied by an adapter or observed only after the
fact. Nor is there a dedicated adversarial conformance suite that attempts to
make a tool violate its own effect declaration.

### Resource scope is too coarse

`ToolContext` carries repository/worktree roots, semantic actions, and allowed
effect kinds. It does not express an authorization such as:

- edit only `src/payment/**` and `tests/payment/**`;
- stage only the current proposed PR's changed paths;
- comment on PR 123 but do not merge it;
- access only one network host; or
- read one named secret without allowing it to flow to prompts or network.

Adapter-specific validation can enforce some of this, but the compiler cannot
currently summarize it as part of a workflow certificate.

### Nested-skill effects are analyzed but not an execution contract

`workflow_liveness.summarize_skill` provides an initial child summary and the
static analyzer checks nested-skill references and recursion. The summary is
not yet the authoritative transitive effect envelope used by runtime
capability resolution. Changes to child effects do not produce a reusable
certificate dependency graph.

### Completion is narrower than the durable execution model

Predicated completion currently focuses on required outputs and required
actions. Durable execution state already contains evidence, obligations,
findings, fingerprints, and idempotency keys, but skill completion cannot yet
express most of those conditions declaratively.

### Termination proofs are mostly inferred

The liveness analyzer detects non-progress cycles, ineffective retry paths, and
unbounded coding loops. The language does not yet let an author declare a
cycle's variant, finite work set, or resource consumption directly. This limits
the strength and explainability of termination proofs.

### Exceptions grant effects but do not describe weakened proofs

Capability exceptions are already tightly bound at runtime. They do not yet
update or annotate a workflow's safety certificate, bind to a step/call path
explicitly, or carry compensating controls and non-transitive delegation rules.

## Target architecture

Compilation should produce one immutable `CompiledWorkflowContract` consumed by
both static analysis and runtime execution:

```python
@dataclass(frozen=True)
class CompiledWorkflowContract:
    definition_fingerprint: str
    steps: tuple[CompiledStepContract, ...]
    transitive_effects: EffectEnvelope
    resource_scope: ResourceScope
    obligations: tuple[ObligationSpec, ...]
    guarantees: SafetyCertificate
    dependency_fingerprints: tuple[str, ...]
```

The runtime must refuse to execute when the contract fingerprint does not match
the validated definition and dependency set. This removes the possibility that
the validator proves one interpretation while the runtime executes another.

### Compiled step contract

Add normalized types in a new module such as
`core/workflow_contract.py`:

```python
@dataclass(frozen=True)
class CompiledStepContract:
    step_id: str
    owner: Literal["kernel", "llm", "human"]
    inputs: tuple[ValueSpec, ...]
    actions: tuple[ActionContract, ...]
    outcomes: tuple[OutcomeContract, ...]
    completion: CompletionContract
    progress_measure: ProgressMeasure | None
```

`workflow_step_behavior.py` should remain the source for behavior selection,
but it should produce or participate in this compiled contract rather than
being reinterpreted independently by each runner.

### Durable operation lifecycle and state projection

Add normalized operation-state types to `core/workflow_contract.py` and persist
their transitions through `core/execution_state.py`:

```python
class OperationStatus(StrEnum):
    PLANNED = "planned"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    step_activation_id: str
    contract_fingerprint: str
    arguments_fingerprint: str
    idempotency_key: str | None
    status: OperationStatus
    result: Mapping[str, Any] | None
    observed_effects: tuple[ObservedEffect, ...]
    output_bindings: tuple[OutputBinding, ...]
    evidence_ids: tuple[str, ...]
```

The kernel writes every state transition. `SUCCEEDED` is allowed only after
the broker has validated the result variant, effect conformance, and declared
postconditions. `AMBIGUOUS` is a reconciliation state, never an implicit
success or an automatic repeat.

Define a pure `StepStateProjection` reduced from durable records. Before each
LLM decision, pass this projection--rather than prior conversational prose--to
the provider schema/context. It includes the active activation, applicable
operation statuses and result handles, current typed outputs, valid evidence,
open obligations, resource state, and still-legal actions. The provider cannot
write this projection or manufacture an operation receipt.

### Closed outcome contract

Extend skill, workflow-template, and workflow-task schemas with outcomes:

```python
@dataclass(frozen=True)
class StepOutcomeSpec:
    name: str
    payload_schema: Mapping[str, Any] | None
    requires: tuple[ConditionSpec, ...]
    produces: tuple[str, ...]
    consumes: tuple[ResourceConsumption, ...]
    transition: TransitionSpec
```

Required built-in terminal kinds should include `completed`, `failed`,
`blocked`, `cancelled`, and `suspended`. These are semantic states, not process
exit codes.

For runner-owned operations, the tool adapter maps its structured result into
one declared outcome. For LLM-owned judgment steps, the model may propose an
outcome payload only when the outcome represents judgment rather than an
unverified effect. The kernel validates requirements and commits the outcome.

### Unified effect contract

Replace the parallel static and runtime vocabularies with one operation-level
contract. `ToolManifest` should contain operation-specific entries rather than
one effect set shared by an entire adapter:

```python
@dataclass(frozen=True)
class OperationContract:
    name: str
    arguments_schema: Mapping[str, Any]
    reads: tuple[ResourceEffect, ...]
    writes: tuple[ResourceEffect, ...]
    produces_evidence: tuple[str, ...]
    invalidates_evidence: tuple[str, ...]
    determinism: DeterminismKind
    idempotency: IdempotencyKind
    reversible: bool
    result_variants: tuple[ResultVariant, ...]
    trust_tier: ToolTrustTier
```

`CapabilityEffect` should either be removed or become a projection of
`OperationContract`. Static analysis and runtime `effects_for` must use the
same operation contract and resource-selector evaluator.

Adopt the useful distinctions in MCP's tool annotations--read-only,
destructive, idempotent, and open-world--as derived properties of this richer
contract, not as authority-granting hints. The contract must retain concrete
selectors and operation-specific effects; a trusted annotation alone does not
qualify an operation for a proven guarantee.

### Mediated effect enforcement and trust tiers

Add a kernel-owned effect mediation boundary around every externally visible
operation channel:

- filesystem access validates normalized paths, symlink resolution, and
  read/write selectors;
- process execution validates registered commands, arguments, working
  directory, environment, and inherited capabilities;
- network access validates destination, method, and data-flow policy;
- Git/GitHub operations use typed semantic brokers where possible; and
- secret and external-mutation interfaces require named operations and record
  their concrete targets.

The capability check before invocation remains necessary but is insufficient.
Each broker must emit an observed-effect trace and deny requests outside the
intersection of the operation declaration and active authority. Define
`ToolTrustTier` as `enforced`, `tested`, `attested`, or `opaque`; only
`enforced` operations may contribute a `proven` effect or scope guarantee.
The other tiers must be visible in the certificate and safety-profile policy.

### Resource scopes

Add typed selectors instead of only coarse effect enums:

```python
@dataclass(frozen=True)
class ResourceScope:
    repositories: tuple[str, ...] = ()
    readable_paths: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    stageable_paths: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    pull_requests: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    readable_secrets: tuple[str, ...] = ()
```

Selectors must have deterministic subset/intersection operations. A child call
receives the intersection of the root scope, parent scope, and any call-site
restriction. No child or exception may widen a selector implicitly.

## Required code changes

### 1. Establish canonical contract types

Create `core/workflow_contract.py` containing:

- action contracts;
- step outcome and transition contracts;
- condition expressions;
- progress measures;
- operation effects and resource selectors;
- transitive effect envelopes;
- proof statuses and safety certificates; and
- stable serialization and fingerprints.

Keep these types free of provider, filesystem, and runtime side effects. Add
round-trip serialization and fingerprint tests before integrating them.

### 2. Compile every definition into the canonical contract

Refactor `workflow_definition_analysis.py` so parsing and normalization produce
the canonical contract first. Every subsequent pass should consume that
contract:

- CFG and reachability;
- definite assignment and handoffs;
- action closure;
- outcome exhaustiveness;
- effect and scope checking;
- retry relevance;
- progress and termination;
- nested-skill composition; and
- certificate generation.

The current `WorkflowIR`, `StepControlContract`, and abstract-state types should
be migrated or wrapped rather than duplicated again.

Move diagnostics into pass-oriented functions with stable input/output types.
Each diagnostic should identify the guarantee affected and include the shortest
counterexample path when applicable.

Add diagnostics for a transition or completion condition that consumes an
operation without a permitted durable terminal status, an output that lacks a
successful producer record, and any operation whose result cannot be included
in the active step's state projection.

### 3. Close action contracts

Change `core/skill_specification.py` and corresponding template/task schemas so
new definitions must declare `actions`, including an explicit empty list.

Remove implicit authority from `_step_actions` in
`workflow_chat_agent.py` in stages:

1. emit a migration diagnostic when actions are omitted;
2. compile omitted actions into an explicit legacy contract;
3. migrate all checked-in definitions;
4. require explicit actions for the current schema version; and
5. remove broad runtime inference.

`prompt_user`, `next_step`, and `complete` should not be globally ambient LLM
actions. Human suspension and step transitions should be explicit outcomes or
kernel behavior. `emit_outputs` may remain a provider protocol detail, but it
must compile to an outcome rather than expand semantic authority silently.

### 4. Introduce step outcomes

Add outcome parsing and validation to:

- `core/skill_specification.py`;
- `core/workflow_template_specification.py`;
- `core/workflow_task_specification.py`; and
- template instantiation and execution-plan compilation.

Update `_step_action_response_schema` so provider schemas expose only the
decision payload appropriate to the active step. Do not ask the LLM to return a
success outcome for a runner-owned effect.

Replace or extend `workflow_llm.WorkflowActionOutcome` with a semantic result:

```python
@dataclass(frozen=True)
class StepActivationResult:
    outcome: str
    payload: Mapping[str, Any]
    operation_record_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
```

Centralize outcome validation and transition commitment in the shared
execution engine. Interactive and durable runners should not implement
different transition rules.

An outcome requirement that refers to an operation or output must name the
allowed terminal operation statuses and result variants. The compiler rejects a
transition that relies on an operation with no durable result mapping. Runtime
transition commitment reads `OperationRecord`s from durable state; it does not
accept an LLM assertion or a text rendering of the tool result as proof.

Model template instantiation explicitly as a runner-owned operation. Its
`SUCCEEDED` result variant may bind `workflow_instance_ref` only after the
generated definition is persisted, schema-validated, checked against the
template's contract, compiled, and fingerprinted. The following step consumes
that typed reference from its state projection. `FAILED` returns typed parser,
validation, or compilation diagnostics; `AMBIGUOUS` requires reconciliation.

### 5. Unify static and runtime effects

Refactor `core/tool_manifest.py` to expose operation-level contracts. Update
built-in adapters in `execution/builtin_tools.py` to declare argument schemas,
resource effects, result variants, and evidence invalidation.

Change `execution/tools.py::ToolContext` to carry a structured resource scope
and compiled contract fingerprint. Change `execution/capabilities.py` to:

- resolve an operation contract;
- validate arguments against its schema;
- evaluate concrete resource effects from arguments;
- prove those effects are inside the active scope;
- checkpoint according to the operation contract;
- compare observed and declared effects; and
- emit typed result/evidence records.

Add a broker-level conformance assertion after each invocation:

```text
observed_effects subset_of declared_effects intersection granted_effects
```

The assertion is defense in depth for mediated channels and is the diagnostic
mechanism for observed-only channels. A raw, unrestricted process capability
cannot be classified as enforced merely because its observed trace happened to
be narrow.

Change `workflow_liveness.py` to project its abstract effects directly from
these contracts. Remove the separate hard-coded effect registry once all
built-ins are migrated.

For shell, provide three categories:

- a bounded registered command with a complete operation contract;
- a checked-in author-time exception with conservative unknowns; or
- an unregistered command, rejected under strict profiles.

### 6. Compile transitive nested-skill envelopes

Replace the current shallow skill summary with a fixed-point, call-graph
compiler:

1. compile direct actions and effects for every skill;
2. resolve `uses_skill` bindings;
3. propagate child envelopes to callers;
4. intersect child resource scopes with call-site restrictions;
5. reject undeclared or widening effects;
6. record child definition fingerprints; and
7. invalidate callers when a child contract changes.

Recursive strongly connected components require an explicit decreasing
measure or finite recursion bound. A recursive summary cannot be assumed from
one traversal.

Expose a CLI command or extend `explain-effective-contract` to show:

- direct and transitive actions;
- direct and transitive effects;
- resource scopes;
- child call paths contributing each effect; and
- guarantees changed by each dependency.

### 7. Expand completion over durable truth

Extend `SkillStepCompletion` beyond required outputs and actions. Add typed
conditions for:

- fresh successful evidence;
- satisfied obligations;
- disposed blocking findings;
- current input/resource fingerprints;
- completed idempotency keys; and
- child outcomes.

Use `core/execution_state.py` as the source of truth. Completion evaluation must
be a pure function over compiled conditions and reduced execution state.

Define invalidation relationships in operation contracts. After a mutation,
the runtime emits `EVIDENCE_INVALIDATED` for evidence whose input or resource
fingerprint is no longer current. Completion cannot use stale evidence.

### 8. Add explicit progress measures

Extend step and workflow schemas with optional progress declarations:

```yaml
progress:
  variant: unresolved_findings
  order: strictly_decreases
  lower_bound: 0
```

Also support finite resources:

```yaml
resources:
  retry_budget: 3

outcomes:
  retryable_failure:
    consumes:
      retry_budget: 1
```

Update `workflow_liveness.py` to prove cycles by lexicographic variants or
finite resource consumption. Continue inferring obvious progress, but prefer
explicit declarations when external or semantic state prevents inference.

The analyzer must distinguish:

- a proven terminating cycle;
- a proven non-progress cycle;
- a bounded cycle that may fail but terminates;
- an unknown cycle requiring an author declaration; and
- an external wait represented as suspension rather than a cycle.

### 9. Make suspension and partial completion first-class

Add terminal status types for `blocked`, `suspended`, `partial`, `failed`, and
`cancelled`. Define which may resume and what event is required.

Do not represent human input or external availability as repeated model calls.
Persist a suspension record with:

- reason and structured requirement;
- permitted resume event;
- contract and state fingerprints;
- expiration/cancellation policy; and
- outstanding obligations.

Partial completion must enumerate valid outputs and must not satisfy global
success gates.

### 10. Integrate exceptions with certificates

Extend `CapabilityExceptionRequest` with:

- active step and nested call path;
- resource selectors;
- guarantees weakened from `proven` to `unknown`;
- compensating controls;
- checkpoint requirement; and
- explicit non-delegation.

The binding must include every new field. Exception use should emit a
certificate amendment in durable state. The original certificate remains
immutable; the amendment records the exact scope and duration of the relaxed
claim.

Secret access should remain non-exceptionable unless a separate policy and
data-flow model is introduced. Exceptions must never authorize forged evidence,
suppressed failures, or unrecorded effects.

### 11. Add safety profiles and enforcement modes

Define profile requirements independently from proof generation:

```python
@dataclass(frozen=True)
class SafetyProfile:
    required_proofs: frozenset[Guarantee]
    permitted_unknowns: frozenset[Guarantee]
    permitted_exception_effects: frozenset[EffectKind]
    sandbox_profile: str
```

Map existing `ExecutionMode` values carefully:

- `observe` records certificate violations without granting a proof;
- `enforce` rejects contracts or actions outside the profile; and
- `off` is legacy execution and must not emit a safety certificate.

Do not make profile names change proof results. A permissive profile may accept
an unknown property, but the certificate must continue to say `unknown`.

### 12. Bind runtime execution to compiled certificates

At execution creation, persist:

- root definition fingerprint;
- compiled contract fingerprint;
- nested dependency fingerprints;
- tool manifest fingerprints;
- safety profile fingerprint; and
- initial certificate.

Before each action, the kernel verifies that the active step contract and tool
manifest still match. A mismatch suspends or fails execution with a stable
diagnostic; it must not silently recompile halfway through a run.

## Compiler passes and diagnostics

The compiler should use explicit passes in this order:

1. parse and schema validation;
2. normalization and compatibility expansion;
3. reference and handoff resolution;
4. action and outcome closure;
5. operation lifecycle and output-producer resolution;
6. operation and effect resolution;
7. resource-scope checking;
8. nested-skill fixed-point composition;
9. CFG and abstract-state construction;
10. completion and evidence observability;
11. retry relevance;
12. progress and termination proof;
13. replay/idempotency proof; and
14. certificate generation.

Add stable diagnostic families for:

- omitted or implicitly expanded authority;
- incomplete outcome unions;
- unmapped operation result variants;
- scope widening at nested calls;
- undeclared transitive effects;
- stale or unproducible evidence requirements;
- cycles without a variant;
- non-decreasing variants;
- non-idempotent effects without reconciliation;
- certificate/runtime fingerprint drift; and
- exception attempts that weaken forbidden guarantees.

Every graph diagnostic should include a shortest reachable path and cycle when
available. Every composition diagnostic should include the nested call path
that introduced the effect.

## Migration strategy

### Phase 1: Canonical model without behavior changes

- Add canonical contract, effect, scope, outcome, and certificate types.
- Compile current definitions into compatibility contracts.
- Compare old and new runtime behavior in tests.
- Emit reports but do not enforce new proof requirements.

Acceptance gate: every checked-in definition compiles, and contract
fingerprints are stable across processes and platforms.

### Phase 2: Closed actions and explicit outcomes

- Migrate all checked-in steps to explicit actions.
- Add outcomes while compiling legacy transitions for compatibility.
- Remove implicit `prompt_user`, `next_step`, and `complete` authority from the
  current schema version.
- Make the kernel own outcome commitment.

Acceptance gate: deleting an action or outcome from a contract makes the
corresponding runtime behavior impossible, not merely discouraged.

### Phase 3: Unified effects and scopes

- Migrate built-in tools to operation contracts.
- Make static analysis consume runtime manifests.
- Introduce path, repository, remote-object, host, and secret selectors.
- Reject observed effects outside declared contracts.

Acceptance gate: there is one authoritative effect description per operation,
and mutation tests cannot create an undeclared effect without a runtime denial
or a contract-conformance failure. Strict profiles accept only enforced
operations for effect- and scope-proven claims.

### Phase 4: Interprocedural composition

- Compute nested-skill fixed points and dependency fingerprints.
- Surface transitive envelopes in CLI and certificates.
- Enforce root and call-site resource scopes.

Acceptance gate: adding a remote mutation to a deeply nested skill either
changes the root certificate visibly or fails compilation under the root
profile.

### Phase 5: Completion, progress, and termination

- Expand completion over durable evidence and obligations.
- Add evidence invalidation.
- Add variants and finite resource declarations.
- Prove all reachable cycles or classify them as bounded, disproven, or
  unknown.

Acceptance gate: all checked-in strict workflows have no unobservable
completion, ineffective retry, non-progress cycle, or unknown unbounded cycle.

### Phase 6: Exceptions and strict enforcement

- Amend certificates when exceptions are approved.
- Add safety profiles and environment policy.
- Reject legacy or unknown contracts in strict execution.
- Retire temporary baselines as definitions migrate.

Acceptance gate: an exception can authorize only its exact bound operation and
cannot transitively broaden a child, forge evidence, or change an unrelated
proof.

## Verification strategy

### Unit tests

- canonical serialization and fingerprints;
- operation lifecycle transition legality and durable record reduction;
- state-projection contents, immutability, and provider serialization;
- outcome exhaustiveness and payload schemas;
- resource selector subset and intersection laws;
- operation result-to-outcome mapping;
- completion evaluation and evidence invalidation;
- variant decrease and finite-budget consumption; and
- exception binding and expiration.

### Compiler golden tests

Maintain minimal fixtures for every diagnostic and every proof status. Golden
reports should include stable codes, paths, call paths, counterexample states,
and cycles.

### Static/runtime conformance tests

For every step type and built-in operation, assert that:

- runtime ownership matches compiled ownership;
- runtime actions equal the closed compiled set;
- concrete effects are covered by the compiled envelope;
- runtime transition selection uses declared outcomes; and
- completion uses the same condition evaluator as static analysis; and
- the LLM receives kernel-derived operation statuses rather than prior tool
  prose as the source of operation truth.

### Adversarial operation-conformance tests

For every built-in operation contract, run it through mediated test fixtures
that attempt to exceed the declaration:

- reads and writes outside selector roots, including traversal and symlink
  escapes;
- undeclared subprocesses, arguments, environments, hooks, and inherited
  credentials;
- unapproved network hosts, methods, and external mutation targets;
- indirect effects through libraries, redirects, or child processes; and
- repeated calls for operations declared naturally or keyed idempotent.

Record the full observed-effect trace and assert it is contained by both the
declared contract and the granted authority. Maintain mutation fixtures that
deliberately add an undeclared write, network request, or subprocess to a
cooperative tool; the test must prove the mediator and harness detect it.

Run prompt-injection suites such as AgentDojo-style hostile tool-output cases
as end-to-end regressions. Their assertion is not that the model refuses the
instruction; it is that no resulting operation can exceed the compiled effect
and scope contract.

### Property and model-based tests

Generate small workflow graphs and verify:

- no proven-safe graph contains a reachable stuck state;
- every termination-safe graph has no infinite abstract trace;
- scope intersection never widens authority;
- adding a nested effect never reduces the parent envelope;
- evidence-invalidating mutations prevent stale completion; and
- replay never duplicates keyed effects.

### End-to-end tests

Exercise:

- deterministic success;
- semantic repair followed by fresh validation;
- ineffective retry rejection;
- nested transitive effect denial;
- crash and reconciliation around a non-idempotent effect;
- template generation that advances only after a persisted, validated,
  compiled, and fingerprinted `workflow_instance_ref` record;
- template generation failure that exposes diagnostics but cannot bind the
  workflow instance or advance a dependent step;
- human suspension and resume;
- approved and denied capability exceptions; and
- certificate drift after a child or tool manifest changes.

CI should continue tracking advisory counts, but strict proof failures must not
be suppressible through the warning budget.

## Immediate implementation slice

The first change set should remain intentionally narrow:

1. Add canonical `StepOutcomeSpec`, `TransitionSpec`, `ProofStatus`, and
   `SafetyCertificate` types with serialization tests.
2. Add optional `outcomes` to skill steps and compile existing `next_step`,
   gates, and predicated completion into compatibility outcomes.
3. Add `outcome_safe` certificate generation and diagnostics for missing,
   duplicate, non-terminal, or unreachable outcomes.
4. Persist the compiled contract fingerprint in execution creation state.
5. Add static/runtime conformance tests without removing compatibility behavior.

This slice creates the architectural seam needed for later effect unification
without attempting a repository-wide language migration in one PR.

## What not to do

- Do not build a second runtime inside the static analyzer.
- Do not infer authoritative effects or completion from prompt prose.
- Do not call a workflow safe because one successful path exists.
- Do not treat a warning baseline as a proof.
- Do not require parents to restate every nested child action manually.
- Do not use one global safety level to hide which properties are unknown.
- Do not add a blanket unsafe mode that bypasses audit or evidence.
- Do not make exceptions transitive by default.
- Do not repeat non-idempotent effects after ambiguous failures.

## Completion criteria

The implementation is complete when:

- definitions compile into one runtime-consumed contract;
- every LLM boundary has a closed action and outcome union;
- every executable operation has one static/runtime effect contract;
- nested effects and resource scopes compose transitively;
- completion depends on fresh structured evidence and obligations;
- every reachable cycle is proven terminating, explicitly bounded, or rejected;
- exceptions weaken only named guarantees within exact scope;
- execution is bound to definition, dependency, manifest, and profile
  fingerprints; and
- CI publishes an explainable certificate for every checked-in strict workflow.
