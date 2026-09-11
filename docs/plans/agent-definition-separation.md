# Agent and Definition Separation Plan

## Objective

Refactor Powdrr Lift so agent code is a consumer of validated definitions and
execution contracts, not an alternate owner of their schema, validation, or
semantics.

Powdrr is intentionally becoming three related but distinct systems:

1. **The product/lifecycle language** describes the product and its durable
   knowledge: specification-v1 documents, current state, proposed PRs,
   requirements, architecture, implementation intent, and decisions.
2. **The process language** describes LLM-led work: skills, workflows, workflow
   templates, tasks, steps, actions, effects, outcomes, handoffs, and
   liveness/safety guarantees.
3. **The agent** authors and manipulates artifacts in both languages and
   executes processes under their compiled contracts. It is an interpreter and
   adapter, not the owner of either language's meaning.

The refactor must preserve this distinction in the package layout and import
graph. Product artifacts and process definitions may refer to one another
through typed identifiers and compiled contracts, but neither language may
depend on provider-specific agent code.

The target dependency direction is:

```text
product and lifecycle language       process language
    -> product compiler                   -> process compiler
    -> product contracts                  -> process contracts
                   \                     /
                    -> agent/execution kernel
                    -> provider / human adapter
```

The agent may propose an action or a semantic decision. It must not parse
definition formats, decide whether a workflow is valid, infer authority from
prose, or commit an execution transition. The kernel and definition compiler
own those decisions.

This plan complements the state-centric execution architecture and the LLM
execution-language safety plan. It is deliberately focused on module
boundaries and migration order; it does not redesign the workflow language.

## Why this refactor is needed

The current boundaries are logical rather than physical. The largest examples
are:

| Module | Current mixed responsibilities |
| --- | --- |
| `workflow_chat_agent.py` | Provider clients, prompt construction, skill/template loading, action schemas, tool dispatch, worktree handling, checkpoints, durable facts, nested skills, and validation |
| `workflow_task_agent.py` | Durable task orchestration, task prompts, Git/worktree lifecycle, nested skill execution, tool dispatch, and imports of private chat-agent helpers |
| `workflow_llm.py` | Provider-independent proposal/repair machinery plus workflow-specific action types, execution strategy, and progress semantics |
| `workflow_definition_analysis.py` | Definition parsing/IR, prompt-contract checks, template instantiation, action checks, handoffs, liveness, and effect analysis in one pass |
| `core/skill_specification.py` | Definition data model, serialization, schema validation, dependency validation, and some runtime-oriented action assumptions |

This creates several failure modes:

- changing a definition schema requires editing agent code;
- chat and durable task runners can interpret the same step differently;
- private helper imports make `workflow_chat_agent` an accidental framework API;
- validation is repeated at provider and runtime boundaries;
- prompt text can become an implicit definition or authority source; and
- it is difficult to test the agent loop without constructing a full workflow
  definition and execution environment.

## Target module architecture

Introduce explicit packages gradually. The exact filenames may evolve, but
the ownership and dependency rules should remain stable.

```text
powdrr_lift/
  product/
    model.py                 # product/lifecycle artifacts and versioned schemas
    parser.py                # specification-v1 and lifecycle artifact loading
    validation.py            # product coherence and reference validation
    compiler.py              # product source -> product contracts/views
    catalog.py               # product artifact discovery and context metadata
  process/
    model.py                 # skills, workflows, tasks, and step schemas
    parser.py                # process YAML/JSON loading and normalization
    validation.py            # action, effect, outcome, and reference checks
    compiler.py              # process source -> compiled process contract
    analysis.py              # CFG, liveness, effects, handoffs, diagnostics
    catalog.py               # process discovery and human-facing metadata
  contracts/
    action.py                # closed action and argument contracts
    outcome.py               # result and transition contracts
    effects.py               # effect envelopes and resource scopes
    state.py                  # operation lifecycle and state projections
    diagnostics.py           # stable compiler/runtime diagnostic types
  execution/
    kernel.py                # authority, operation lifecycle, transition commit
    runtime.py               # execution services and durable state integration
    operations.py            # operation registry and adapters
    projections.py           # current state supplied to an agent
  agent/
    protocol.py              # model client and proposal interfaces
    proposal.py              # parse/constrain/validate model proposals
    repair.py                # provider-independent repair policy
    prompts.py               # render prompts from compiled views and projections
    providers.py             # OpenAI/Anthropic/local provider adapters
    runners.py               # generic propose -> kernel -> observe loop
  adapters/
    chat.py                  # interactive UI and human handoff policy
    task.py                  # durable task selection and delivery policy
    cli.py                   # command-line composition
```

Existing `core/` and top-level modules may remain as compatibility locations
during migration. New code should use the package boundaries above, and old
imports should become thin re-exports rather than additional implementations.

## Ownership rules

### Product language owns product meaning

The product layer owns:

- specification-v1 schemas and lifecycle artifact schemas;
- requirements, architecture, implementation intent, proposed PRs, and
  current-state documents;
- product references, relationships, versioning, and coherence rules; and
- compilation into immutable product contracts and agent-facing views.

It must not import skills, workflows, provider clients, prompt transports,
tool adapters, or execution state. Product documents can be authored or
modified by an agent, but the agent cannot redefine their schema or declare a
document valid.

### Process language owns execution meaning

The process layer owns:

- skill, workflow, template, task, and step schemas and version migration;
- parsing and normalization;
- references, placeholders, and dependency resolution;
- the canonical process model;
- compiler diagnostics;
- CFG, liveness, effect, handoff, and completion analysis; and
- compilation into an immutable process execution contract.

It may depend on pure contract types and typed product references. It must not
import provider clients, prompt transports, interactive UI, tool adapters,
worktree mutation, or durable execution state.

Product and process compilers are separate even when a process consumes
product artifacts. A workflow may request product context or produce a
proposed-PR artifact, but that relationship is represented as a typed input or
output contract rather than an import from process code into product parsing
implementation.

### Contracts own shared vocabulary

Contracts own the types that cross boundaries: actions, arguments, outcomes,
transitions, operation lifecycles, effects, scopes, obligations, state
projections, and fingerprints.

Contract modules must be pure and serializable. They must not load files, call
an LLM, execute tools, or depend on a particular definition format. This is
what lets the compiler, kernel, agent, and tests agree on one meaning.

### Execution owns truth and authority

The execution layer owns:

- capability checks and effect mediation;
- operation lifecycle records;
- durable state reduction and optimistic persistence;
- evidence and obligation updates;
- transition commitment;
- retries, idempotency, reconciliation, and checkpoints; and
- read-only state projections.

It consumes compiled contracts. It must not import raw YAML/JSON definition
parsers or provider-specific prompt code.

### Agents own proposals and presentation

The agent layer owns:

- model provider protocols and adapters;
- prompt rendering from a compiled definition view and state projection;
- structured proposal parsing;
- model-facing repair and correction prompts;
- provider retry policy; and
- generic proposal-loop coordination.

It may inspect product views and the closed action/outcome schemas supplied by
the compilers, and it may propose edits to either language. It cannot add
product or process schema fields, add actions, widen scopes, validate
completion, or commit a transition. A proposal is data passed to the relevant
validator or kernel, not an execution fact.

### Adapters own product-specific policy

Chat and durable task adapters own human interaction, task selection, display,
worktree lifecycle policy, and delivery policy. They compose the generic agent
runner with an execution runtime and a compiled contract. They should not
import one another's private helpers.

## Boundary objects

The refactor should use a small number of explicit boundary objects instead of
passing `Skill`, mutable agent state, and loosely typed dictionaries through
every layer.

```python
@dataclass(frozen=True)
class CompiledDefinition:
    definition_id: str
    source_fingerprint: str
    steps: tuple[CompiledStep, ...]
    effects: EffectEnvelope
    guarantees: SafetyCertificate


@dataclass(frozen=True)
class AgentInput:
    procedure: ProcedureView
    state: StateProjection
    observation: Observation | None


@dataclass(frozen=True)
class AgentProposal:
    action: ActionProposal | None
    decision: DecisionProposal | None


@dataclass(frozen=True)
class KernelResult:
    operation_record: OperationRecord
    observation: Observation
    transition: TransitionResult
```

The names can be adapted to existing types. The important properties are
immutability at the boundary, explicit fingerprints, and no raw provider or
definition implementation leaking across it.

## Migration phases

### Phase 0: Establish the dependency contract

- Add import-boundary tests that reject agent -> parser/validator imports and
  definition -> agent/provider imports.
- Inventory public imports and identify compatibility imports used by tests and
  external callers.
- Mark private functions imported across modules, especially the
  `workflow_task_agent` -> `workflow_chat_agent` seam.
- Add characterization tests for chat and durable task behavior before moving
  code.
- Define the initial boundary objects as aliases or wrappers around existing
  types; do not change behavior yet.

Exit criterion: the intended dependency graph is executable in tests, and all
cross-layer exceptions are documented.

### Phase 1: Extract the agent protocol and provider layer

- Move provider clients, model mappings, response-schema negotiation, JSON
  completion, timeout handling, and provider serialization out of
  `workflow_chat_agent.py`.
- Split provider-independent repair and proposal machinery from workflow
  definition concepts in `workflow_llm.py`.
- Define an agent-facing `ProcedureView`, `StateProjection`, and closed
  proposal protocol.
- Keep compatibility re-exports at the old module paths.

Exit criterion: an agent-loop test can use a fake compiled contract and fake
state projection without loading a skill file or invoking definition
validation.

### Phase 2: Extract definition compilation and catalog services

- Move source models, parsers, serialization, and structural validation from
  `core/skill_specification.py` and the workflow-template/task specification
  modules into `definitions`.
- Split `workflow_definition_analysis.py` into parsing/normalization,
  contract compilation, and analysis passes.
- Move skill/template discovery and catalog presentation out of the agent
  modules into `definitions.catalog`.
- Make prompt-contract analysis consume compiled prompt/action contracts rather
  than agent implementation details.

Exit criterion: validating or compiling a definition has no import path to an
LLM client, `workflow_chat_agent`, `workflow_task_agent`, or tool adapter.

### Phase 3: Make the execution kernel the only transition owner

- Route both current runners through one compiled-contract execution service.
- Move action authorization, operation lifecycle, effect checks, evidence,
  completion, and transition commitment behind the execution boundary.
- Replace agent-local mutable state such as `_WorkflowExecutionState` with a
  state projection reduced from durable execution state.
- Make template instantiation, pre-steps, gates, and nested skill calls
  explicit kernel operations.
- Treat model output as an `AgentProposal`; remove direct agent calls to
  validation helpers and operation adapters.

Exit criterion: chat and durable task paths use the same kernel transition
function for equivalent contracts and state.

### Phase 4: Thin the product adapters

- Reduce `workflow_chat_agent.py` to interactive composition, display,
  worktree/user handoff policy, and chat-specific provider configuration.
- Reduce `workflow_task_agent.py` to task selection, durable task policy,
  Git/worktree delivery policy, and adapter composition.
- Move duplicated helpers into named agent, execution, definition, or adapter
  modules according to ownership rather than creating another shared utility
  module.
- Remove all imports of private chat-agent helpers from the task agent.

Exit criterion: the two adapters are independently testable and neither is a
framework dependency of the other.

### Phase 5: Remove compatibility implementations

- Replace old module implementations with re-export shims where compatibility
  is required.
- Delete duplicate validation, prompt-action semantics, and direct tool
  dispatch from the agent modules.
- Make import-boundary checks mandatory in CI.
- Update documentation and examples to use the new package APIs.

Exit criterion: one definition compiler, one execution kernel, one generic
agent proposal loop, and two thin product adapters remain.

## Concrete first slices

The first implementation PR should be small enough to review and should not
move the entire 12k-line chat module. Recommended order:

1. Add `contracts/agent.py` with `ProcedureView`, `StateProjection`,
   `AgentProposal`, `Observation`, and `KernelResult` wrappers.
2. Add `agent/protocol.py` and move only provider protocols and fake-provider
   test seams.
3. Add `agent/repair.py` and move provider-independent repair classification
   and repair prompt manifests from `workflow_llm.py`.
4. Change `workflow_llm.WorkflowStepRunner` to consume the boundary types while
   retaining compatibility adapters.
5. Add architectural tests proving a fake agent can run against a fake compiled
   contract without importing skill definitions.

Do not begin by renaming every file or moving all tests. First make the
dependency boundary real, then migrate implementations behind it.

## Test strategy

Maintain tests at four levels:

- contract tests for serialization, fingerprints, and compatibility;
- definition tests for parsing, validation, compilation, diagnostics, and
  liveness with no provider/runtime imports;
- execution tests for authority, operation lifecycle, state reduction,
  effects, evidence, and transitions with fake contracts; and
- agent tests for proposal parsing, repair, prompt projection, and provider
  behavior with fake compiled inputs.

Add architecture tests that assert:

- definitions do not import agent or provider modules;
- product modules do not import process compilers or agent/provider modules;
- process modules do not import product implementation modules or agent/provider
  modules;
- contracts do not import definitions, providers, or execution adapters;
- agents do not call raw definition validators or tool adapters directly;
- adapters depend on interfaces rather than private helpers; and
- chat and task runners produce equivalent kernel inputs for equivalent state.

Every migration phase should retain end-to-end tests for nested skills,
template-generated workflows, malformed proposals, denied actions, stale
evidence, retries, and human handoffs. Those tests protect behavior while the
ownership moves underneath them.

## Risks and controls

| Risk | Control |
| --- | --- |
| A new `common` module becomes another mixed layer | Allow only pure boundary types in `contracts`; reject imports by architecture tests |
| Compatibility shims preserve the old coupling indefinitely | Give each shim an owner, diagnostic, and removal milestone |
| Chat and task behavior diverges during migration | Run both through the same fake-kernel contract tests and compare kernel inputs |
| Prompt rendering recreates validation logic | Render only compiled views and projections; validation remains a compiler/kernel concern |
| Moving code changes hidden monkeypatch seams | Preserve explicit adapter interfaces and add tests for supported seams before moving |
| Definition compilation becomes runtime-dependent | Keep compilation pure and pass runtime facts only as typed observations |
| Agent code bypasses the kernel for convenience | Make tool adapters accept only kernel-issued capability contexts and enforce import boundaries |

## Completion criteria

The refactor is complete when:

- definition parsing and validation can run without importing agent code;
- product-language parsing and validation can run without importing process or
  agent code;
- process-language parsing, validation, and liveness analysis can run without
  importing product implementation, agent, or provider code;
- agent proposal tests can run without loading definition files or real tools;
- execution truth and transitions are owned by the kernel;
- chat and durable task execution share the same compiled-contract and state
  interfaces;
- no product adapter imports another adapter's private implementation;
- all cross-layer data uses immutable, versioned boundary contracts; and
- CI enforces the dependency graph in addition to functional tests.
