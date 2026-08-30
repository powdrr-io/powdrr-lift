# OpenCode-Inspired Agent Execution Engineering Plan

## Purpose

This document turns the architectural proposal in
[`opencode-inspired-agent-execution.md`](opencode-inspired-agent-execution.md)
into an implementation sequence. It is written for engineers changing Powdrr,
not for users configuring a workflow.

The target is a typed execution system that can take a request through
specification, proposed-PR decomposition, implementation planning, code changes,
validation, independent review, correction, and PR readiness. Users retain a
declarative way to customize what each delivery phase should accomplish and
which persona owns it. Powdrr owns the lower-level mechanics that parse model
responses, resolve tools, enforce boundaries, retry failures, retain guidance,
track related obligations, checkpoint mutations, and prove readiness.

This is a multi-PR migration. Every PR below must be independently mergeable,
keep the existing workflow path working, and add observable evidence before it
enforces new behavior.

## Required outcomes

The implementation is complete when all of the following are true:

1. A checked-in delivery profile assigns typed phases to Architect,
   Engineering Manager, Engineer, Specification Reviewer, and Code Reviewer
   personas.
2. Workflow tasks and skill steps still declare supported semantic actions in
   exactly one place: `actions`. Profiles and personas do not repeat generic
   action instructions.
3. The execution kernel computes allowed actions from the current phase,
   current step, persona, active execution unit, tool manifest, and unresolved
   obligations.
4. Normal software-development operations run through constrained tools without
   human permission prompts.
5. Effects outside those constraints produce one typed, bounded,
   decision-ready capability exception rather than an ambient approval prompt.
6. A user instruction can become a durable, scoped behavior rule, can imply
   related action obligations, survives restart and compaction, and can be
   explained, superseded, or revoked.
7. Each proposed PR has a typed execution plan, current validation evidence,
   independent findings, and a deterministic readiness decision.
8. Chat execution and durable workflow-task execution use the same action
   lifecycle, error model, retry policy, checkpoint behavior, and event schema.
9. Existing workflow replay, scenario, observer, tuning, CLI, and MCP surfaces
   can inspect and exercise the new state.
10. Legacy prompt-only orchestration, inferred action availability, routine
    permission prompts, and duplicate correction mechanics are removed after
    parity is demonstrated.

## Non-goals

- Do not replace structured specifications or proposed PRs with a Markdown plan.
- Do not turn persona prompts into executable policy.
- Do not create a generic shell permission engine with `allow`, `ask`, and
  `deny` as its primary safety model.
- Do not let delivery-profile customization change transition semantics,
  retries, checkpoint rules, capability constraints, evidence freshness, or
  readiness truth.
- Do not move generic action instructions into every skill. A declared action
  name activates kernel-owned instructions.
- Do not introduce a second action declaration beside `SkillStep.actions`,
  `WorkflowTask.actions`, and `WorkflowTaskTemplate.actions`.
- Do not require plan approval when the proposed PR already authorizes the
  scope and the generated plan introduces no new decision.
- Do not use LSP diagnostics as a substitute for the repository's declared
  format, lint, type-check, and test commands.

## Architectural invariants

These invariants are implementation constraints, not configurable defaults.

### One semantic action source

For a skill step or workflow task, `actions` is the sole declaration of actions
the model may return. `next_step` remains kernel-supported for every step. The
kernel renders its generic instruction only when the step has required outputs;
when there are no outputs, the default response contract is sufficient and the
skill does not need to mention `next_step`.

The effective action set is an intersection, never a union:

```text
effective actions =
    declared step actions
  ∩ phase envelope
  ∩ persona envelope
  ∩ active execution-unit scope
  ∩ available validated tool adapters
```

`next_step` is added by the kernel after this intersection. A terminal action
is accepted only when its required outputs and transition guards are satisfied.

### Customizable delivery model, closed execution kernel

Delivery profiles may select:

- a closed `phase_type`;
- a persona and model profile;
- skills and prompt catalogs;
- validation commands from registered command profiles;
- reviewer composition and review topology;
- typed artifact handoffs;
- policy values explicitly exposed by the kernel, such as whether a low-risk
  plan is auto-approved.

Delivery profiles may not define new action semantics, tool effects, transition
code, retry code, exception behavior, evidence truth, relationship traversal,
or readiness rules. Unknown fields fail validation instead of being passed to a
prompt.

### Safety comes from construction

A tool is automatically executable only when Powdrr can prove that its adapter,
manifest, runtime arguments, sandbox, and active execution scope bound every
declared effect. Human approval is not part of the normal path. If that proof
cannot be made, the tool is not exposed as a normal action.

### State is typed and replayable

Every phase transition, action attempt, obligation change, evidence update,
finding disposition, checkpoint, rule change, and exception disposition is an
append-only event. A materialized execution state is a cache reconstructed from
those events. State writes use optimistic version checks and atomic replacement.

### Durable guidance cannot weaken truth or safety

Remembered user behavior can add obligations, narrow behavior, choose among
safe alternatives, or affect delivery conventions. It cannot expose a tool,
broaden a path or effect scope, hide a failing check, suppress a blocking
finding, bypass a transition, or convert a deterministic failure into success.

### CLI and MCP are adapters

All validation, mutation, and query behavior lives in importable core/runtime
functions. CLI and MCP handlers translate arguments and results only. A feature
is not complete if its CLI and MCP implementations can make different policy
decisions.

## Current code seams

The migration should extend these existing seams rather than build a parallel
agent:

| Responsibility | Current source | Migration role |
| --- | --- | --- |
| Shared model request and action loop | `src/powdrr_lift/workflow_llm.py` | Host the common action lifecycle and typed correctable failures. |
| Repetition and failure accounting | `src/powdrr_lift/workflow_execution.py` | Feed kernel action attempts and semantic-stall events. |
| Interactive skill execution | `src/powdrr_lift/workflow_chat_agent.py` | Become a presentation/input adapter over the kernel. |
| Durable task execution | `src/powdrr_lift/workflow_task_agent.py` | Become a task persistence adapter over the kernel. |
| Workflow task persistence | `src/powdrr_lift/core/workflow_task_specification.py` | Add phase/persona references while retaining root task YAML. |
| Workflow template persistence | `src/powdrr_lift/core/workflow_template_specification.py` | Compile delivery profiles and plans into typed tasks. |
| Skill step contracts | `src/powdrr_lift/core/skill_specification.py` | Retain `actions` as the semantic action authority. |
| Workflow relationships | `src/powdrr_lift/core/workflow_relationships.py` | Remain specification/task relationships; do not overload it with runtime obligations. |
| Observer interventions | `src/powdrr_lift/workflow_observer.py` | Observe typed kernel events and shadow/enforcement differences. |
| Replay and evaluation | `src/powdrr_lift/workflow_replay.py`, `workflow_scenario.py`, `workflow_tuning.py` | Compare old and new decisions on identical fixtures. |
| Existing development tools | `intrinsic_edit.py`, `intrinsic_git_gh.py`, `intrinsic_enrich.py`, `basedpyright_tools.py`, `file_management.py` | Wrap with manifests and constrained adapters. |
| User surfaces | `src/powdrr_lift/cli.py`, `src/powdrr_lift/mcp_server.py` | Expose shared profile, execution, guidance, and exception operations. |

`load_workflow_tasks()` scans only root-level `*.yaml`, `*.yml`, and `*.json`
files. Runtime artifacts therefore belong in an `execution/` child directory of
the workflow instance. This avoids teaching task loading to ignore an expanding
set of metadata filenames.

## Target module layout

Use `core` for serializable contracts and a new `execution` package for the
hardened runtime. Keeping the kernel cohesive is preferable to adding more
orchestration to the already large chat agent.

```text
src/powdrr_lift/
  core/
    delivery_profile.py
    execution_plan.py
    execution_state.py
    tool_manifest.py
    behavior_rule.py
    action_relationship.py
  execution/
    __init__.py
    store.py
    events.py
    phases.py
    tools.py
    capabilities.py
    personas.py
    guidance.py
    relationships.py
    evidence.py
    checkpoints.py
    kernel.py
  workflow_llm.py
  workflow_chat_agent.py
  workflow_task_agent.py
```

The module boundaries are:

- `core/*`: frozen dataclasses, enums, `to_data`/`from_data`, schema-version
  constants, and pure validation reports. These modules do no I/O.
- `execution/store.py`: atomic files, event append, materialized snapshots,
  optimistic versions, and file locking where required.
- `execution/events.py`: event envelope and pure event reduction.
- `execution/phases.py`: transition table and transition guards.
- `execution/tools.py`: adapter protocol and registry.
- `execution/capabilities.py`: manifest/argument/scope resolution and exception
  construction.
- `execution/personas.py`: typed packets, prompt overlays, child-run identity,
  and resume.
- `execution/guidance.py`: rule matching, provenance, conflict handling, and
  explanation.
- `execution/relationships.py`: action-triggered obligation closure.
- `execution/evidence.py`: evidence production, invalidation, finding
  disposition, and readiness evaluation.
- `execution/checkpoints.py`: content-addressed workspace snapshots and revert.
- `execution/kernel.py`: coordinates the above through protocols; contains no
  CLI, MCP, provider, GitHub, or terminal presentation logic.

Avoid importing `workflow_chat_agent` or `workflow_task_agent` from the new
package. Dependency direction must point from those adapters into `execution`.

## Core contracts

All persisted contracts carry a `schema_version`. Parsing rejects an unknown
major version and ignores no unknown fields. A migration function may accept an
older supported version and must return the current typed object before runtime
logic sees it.

### Delivery profile

`core/delivery_profile.py` should define:

```python
class PhaseType(StrEnum):
    INTAKE = "intake"
    SPECIFY = "specify"
    REVIEW_SPECIFICATIONS = "review_specifications"
    DECOMPOSE = "decompose"
    REVIEW_PROPOSED_PRS = "review_proposed_prs"
    PLAN_PR = "plan_pr"
    AWAIT_PLAN_DECISION = "await_plan_decision"
    BUILD = "build"
    VALIDATE = "validate"
    REVIEW_PR = "review_pr"
    RESOLVE_FINDINGS = "resolve_findings"
    CONFIRM_READINESS = "confirm_readiness"
    PUBLISH_PR = "publish_pr"
    COMPLETE_FEATURE = "complete_feature"


class PersonaType(StrEnum):
    ARCHITECT = "architect"
    ENGINEERING_MANAGER = "engineering_manager"
    ENGINEER = "engineer"
    SPECIFICATION_REVIEWER = "specification_reviewer"
    CODE_REVIEWER = "code_reviewer"
```

Required records:

- `PersonaDefinition`: ID, persona type, model profile, prompt catalogs, skill
  references, and optional bounded step budget.
- `ArtifactHandoff`: source phase, destination phase, artifact type, schema
  version, required flag, and owner persona.
- `ReviewAssignment`: reviewer persona, reviewed artifact types, independence
  requirement, and blocking finding severities.
- `PhaseAssignment`: phase type, persona ID, input/output artifact types,
  validation profile references, and declared policy options.
- `DeliveryProfile`: schema version, profile ID, assignments, personas,
  handoffs, review topology, and profile invariants.

The default profile is checked in under:

```text
delivery-profiles/default-software-delivery.yaml
```

The validator must prove:

- every referenced persona exists and has the required persona type;
- every required artifact has exactly one owner and a reachable consumer;
- phase assignments follow the closed transition topology;
- Architect, Engineering Manager, and Reviewer assignments cannot obtain
  product-code mutation envelopes;
- review independence rules do not assign an author as its sole reviewer;
- every validation profile is registered;
- profile values cannot name arbitrary commands or tool adapters;
- no profile field resembles a kernel override.

### Execution plan

`core/execution_plan.py` should define `execution-plan-v1`:

```python
@dataclass(frozen=True, slots=True)
class ExecutionUnit:
    unit_id: str
    description: str
    criterion_ids: tuple[str, ...]
    specification_ids: tuple[str, ...]
    expected_paths: tuple[str, ...]
    prerequisite_unit_ids: tuple[str, ...]
    validation_profile_ids: tuple[str, ...]
    risk_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    schema_version: str
    plan_id: str
    proposed_pr_id: str
    proposed_pr_fingerprint: str
    revision: int
    status: PlanStatus
    units: tuple[ExecutionUnit, ...]
    criterion_coverage: tuple[CriterionCoverage, ...]
    introduced_decisions: tuple[PlanDecision, ...]
```

The plan evaluator is deterministic. It validates identifier references,
acyclic prerequisites, complete acceptance-criterion coverage, path scope,
validation coverage, and whether a plan introduces a decision not authorized
by the proposed PR. Only the last case or an explicitly configured risk policy
requires a human plan decision.

Plan amendments increment `revision`, retain the prior plan, record a reason,
and re-evaluate only the guards affected by the amendment. A changed proposed-PR
fingerprint invalidates approval and blocks Build.

### Execution state and events

`core/execution_state.py` should define:

- `ExecutionId`, `ActionInstanceId`, `ObligationId`, `EvidenceId`, `FindingId`,
  `CheckpointId`, and `PersonaRunId` as validated strings or lightweight value
  records rather than interchangeable bare IDs at internal boundaries.
- `ActionStatus`: proposed, validated, running, completed, correctable_error,
  terminal_error, reverted.
- `ObligationStatus`: open, satisfied, waived, obsolete.
- `FindingStatus`: open, accepted, fixed, not_applicable, superseded.
- `ExecutionState`: current phase, phase revision, active persona run, active
  unit, artifact references, actions, obligations, evidence, findings,
  checkpoints, pending exception, sequence, and state version.
- `ExecutionEvent`: schema version, event ID, execution ID, monotonically
  increasing sequence, expected prior state version, timestamp, actor, type,
  and typed payload.

Events use a closed `ExecutionEventType` enum. At minimum it includes phase
entered/exited, persona started/completed, artifact produced/accepted, plan
amended, action proposed/validated/started/completed/failed/reverted, obligation
opened/satisfied/waived, evidence recorded/invalidated, finding opened/disposed,
checkpoint created/reverted, rule matched, exception requested/decided/expired,
and execution completed.

### Tool manifest

`core/tool_manifest.py` should define a manifest independently of a tool's LLM
schema:

```python
class ToolEffect(StrEnum):
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS_EXECUTION = "process_execution"
    NETWORK_READ = "network_read"
    GIT_MUTATION = "git_mutation"
    GITHUB_MUTATION = "github_mutation"
    EXTERNAL_WRITE = "external_write"
    SECRET_READ = "secret_read"


@dataclass(frozen=True, slots=True)
class ToolManifest:
    schema_version: str
    tool_name: str
    semantic_actions: tuple[str, ...]
    effects: tuple[ToolEffect, ...]
    argument_constraints: tuple[ArgumentConstraint, ...]
    scope_deriver: str
    sandbox_profile: str
    reversible: bool
    idempotency: IdempotencyKind
    evidence_producers: tuple[str, ...]
```

`scope_deriver`, `sandbox_profile`, and evidence producers name registered code;
they are not import paths supplied by configuration. The manifest validator
must reject an effect that the adapter's conformance tests do not cover.

### Behavior rules

`core/behavior_rule.py` should define:

- exact original user wording and source interaction ID;
- normalized intent and trigger predicates;
- narrowest applicable scope: repository, project, workflow type, artifact
  type, action type, entity type, or global only when explicitly requested;
- required, prohibited, and follow-up action references;
- precedence, version, supersedes, status, and expiration;
- extraction confidence and whether user confirmation was required;
- immutable creation metadata and append-only disposition history.

The first implementation should support deterministic predicates over typed
context, not free-form embeddings as the final authority. Semantic retrieval
may nominate candidate rules, but a typed predicate must match before a rule
changes behavior.

### Action relationships

`core/action_relationship.py` should define a closed ontology:

- `requires_before`: the related action must complete first;
- `requires_after`: a follow-up action becomes obligatory;
- `invalidates`: prior evidence or completion becomes stale;
- `validates`: successful evidence may satisfy an obligation;
- `resolves`: completion disposes the exact originating item;
- `reviews`: a reviewer must inspect the artifact/action result;
- `conflicts_with`: both actions or rules cannot be active together;
- `scopes`: the source narrows the valid target set.

Each relationship carries source provenance, trigger predicates, target
selector, ordering semantics, severity, deduplication key, and explanation.
Relationship traversal is bounded, cycle checked, and deterministic.

### Evidence and findings

Evidence records must include:

- producer action instance;
- command/tool identity and normalized arguments;
- repository/worktree identity;
- input fingerprint covering relevant files and configuration;
- start/end timestamps and exit/result status;
- bounded inline output plus a reference to complete output;
- criterion, execution-unit, and finding IDs it supports;
- freshness state and invalidation reason.

Findings must include reviewer persona run, reviewed artifact fingerprint,
severity, category, exact locations or source IDs, required disposition,
blocking status, and disposition evidence. A model cannot close its own
blocking finding by returning `complete`; readiness consumes only typed finding
state.

### Capability exception

A `CapabilityExceptionRequest` contains:

- exact requested operation and effect;
- why no registered constrained tool can perform it;
- target resources and maximum scope;
- actor, phase, persona, execution unit, and originating user goal;
- expected result, risks, reversibility, and rollback;
- alternatives attempted or considered;
- requested duration/use count and idempotency key;
- a deterministic fingerprint of all authorized fields.

Approval creates a non-transferable token bound to that fingerprint, execution
ID, adapter, arguments, scope, use count, and expiration. Any changed argument
requires a new request. Denial is a terminal decision for that fingerprint and
is included in subsequent model context without repeatedly prompting the user.

## Persistence layout and transaction model

For a workflow instance directory `WORKFLOW_DIR`, use:

```text
WORKFLOW_DIR/
  <existing root workflow task files>.yaml
  execution/
    profile.yaml
    plan.yaml
    state.json
    events.jsonl
    guidance-matches.json
    evidence/
      <evidence-id>.json
      output/
        <content-hash>.txt
    findings/
      <finding-id>.json
    exceptions/
      <exception-id>.json
    checkpoints/
      <checkpoint-id>.json
      objects/
        <content-hash>
```

Repository- or organization-scoped behavior rules must live in a separate
configured guidance store, because they outlive one workflow. Workflow state
stores only matched rule IDs, versions, and the obligations they generated.
For the first release, support a repository-local store under a configured
Powdrr metadata directory and make its path explicit in `ExecutionConfig`; do
not infer a global writable location in core code.

`ExecutionStateStore` should expose:

```python
class ExecutionStateStore(Protocol):
    def load(self, execution_id: str) -> ExecutionState: ...
    def append(
        self,
        execution_id: str,
        expected_version: int,
        events: Sequence[ExecutionEvent],
    ) -> ExecutionState: ...
    def verify(self, execution_id: str) -> ExecutionVerificationReport: ...
```

`append` performs one logical transaction:

1. acquire the execution lock;
2. read and verify current state/version;
3. reject a stale `expected_version` with a typed conflict;
4. reduce all candidate events in memory;
5. validate state invariants;
6. fsync an event batch temporary file and atomically append/replace according
   to the selected portable strategy;
7. atomically replace `state.json`;
8. release the lock;
9. notify observers after commit.

Interrupted writes must leave either the old state or the complete new state.
On startup, `verify` compares the materialized state to a fresh event reduction,
checks sequence continuity and content hashes, repairs only the cache, and never
silently edits the event log.

Large outputs and checkpoint objects are written content-addressably before the
event transaction. Unreferenced objects are safe to garbage collect after a
retention period.

## Phase controller

`execution/phases.py` owns a closed transition table. It accepts typed state and
returns a decision; it never asks an LLM whether a transition is valid.

| From | To | Minimum deterministic guards |
| --- | --- | --- |
| intake | specify | request and repository context captured |
| specify | review_specifications | schema-valid specification artifact exists |
| review_specifications | decompose | blocking specification findings disposed |
| decompose | review_proposed_prs | proposed PRs validate and cover approved intent |
| review_proposed_prs | plan_pr | selected proposed PR accepted and dependencies ready |
| plan_pr | await_plan_decision | plan introduces a user decision or policy requires approval |
| plan_pr | build | plan valid, fully covers criteria, and needs no decision |
| await_plan_decision | build | exact plan revision approved |
| build | validate | all execution units complete and build obligations closed |
| validate | review_pr | required evidence is successful and current |
| review_pr | resolve_findings | blocking findings exist |
| review_pr | confirm_readiness | no blocking findings and required reviews complete |
| resolve_findings | validate | changes invalidate evidence or require verification |
| resolve_findings | review_pr | finding dispositions need reviewer confirmation |
| confirm_readiness | publish_pr | all readiness predicates pass |
| publish_pr | complete_feature | PR artifact exists and feature-level policy is satisfied |

The phase controller also declares an immutable phase capability envelope. For
example, `specify`, `decompose`, and review phases may write only their owned
structured artifacts; only `build` and `resolve_findings` may mutate product
code. Profile validation may narrow an envelope but cannot widen it.

Every rejected transition returns a typed list of unsatisfied guards with IDs,
human-readable explanations, and source references. Those guards become model
correction context without asking the model to reinterpret the phase rules.

## Persona runner and handoffs

`execution/personas.py` should expose:

```python
class PersonaRunner(Protocol):
    def start(self, assignment: PersonaAssignment) -> PersonaRun: ...
    def next_action(self, packet: PersonaPacket) -> WorkflowAction: ...
    def resume(self, run_id: str, packet: PersonaPacket) -> WorkflowAction: ...
```

A `PersonaPacket` contains only current typed information:

- assignment and stable persona instructions;
- current phase and allowed action schemas;
- current step instructions, inputs, and required outputs;
- accepted input artifact references;
- active plan unit and open obligations;
- applicable durable rules with explanations;
- current evidence/findings relevant to this action;
- bounded recent events and references to older complete data;
- the latest correctable error, when present.

It must not contain previous-step instructions or actions. It must not include
all persona prompts. The packet builder selects one active assignment and one
current step, then the shared action renderer adds generic instructions for the
effective action set.

Persona behavior is stable responsibility, not procedure duplication:

- Architect owns structured intent and specification consistency.
- Engineering Manager owns proposed-PR boundaries, dependencies, and acceptance
  coverage.
- Engineer owns the execution plan, implementation, corrections, and evidence.
- Specification Reviewer independently checks implementation against structured
  intent.
- Code Reviewer independently checks correctness, maintainability, tests, and
  repository conventions.

Reusable procedures remain skills. Error repair, retry, schema correction,
action validation, tool resolution, and transition behavior remain kernel code.

## Shared action lifecycle

`ExecutionKernel.execute_next()` should implement this ordering:

```text
load and verify state
  -> resolve current phase/persona/step
  -> build current-only persona packet and effective action schemas
  -> request and parse one action
  -> persist action.proposed
  -> validate declared action and required arguments
  -> match durable guidance
  -> expand/deduplicate related obligations
  -> resolve constrained tool capability
  -> create checkpoint for mutating effects
  -> persist action.validated + action.started atomically
  -> invoke adapter
  -> collect immediate diagnostics and output
  -> persist action.completed or action.failed
  -> invalidate/produce evidence
  -> satisfy or open obligations
  -> evaluate transition/readiness guards
  -> continue, transition, correct, or terminate
```

Parsing or validation failures before a tool runs do not create a checkpoint.
A failed mutating tool retains the checkpoint and records whether any effect was
observed. A correction sees the exact action, typed error, constraint failure,
and relevant source location—not a generic retry instruction.

`WorkflowLLMExecutionDriver` should become an adapter over this lifecycle in
stages. During migration it may continue to own provider roundtrips, but it must
emit the same action events and use the same capability resolver as both chat
and task execution. The final state has one loop in the kernel; chat/task
strategies only build inputs and present outcomes.

## Tool adapter and capability broker

`execution/tools.py` should define:

```python
class ToolAdapter(Protocol):
    @property
    def manifest(self) -> ToolManifest: ...
    def validate(self, context: ToolContext, arguments: Mapping[str, Any])
        -> ToolValidationReport: ...
    def execute(self, context: ToolContext, arguments: Mapping[str, Any])
        -> ToolResult: ...
```

`ToolRegistry` rejects duplicate names, unknown semantic actions, unvalidated
manifests, and adapters whose runtime manifest differs from the tested manifest
fingerprint.

`CapabilityBroker.resolve()` returns one of:

- `ExecutableCapability`: adapter plus arguments normalized and bound to exact
  worktree, paths, external resources, effect budget, idempotency key, and
  evidence hooks;
- `CorrectableConstraintFailure`: the requested semantic action is valid but
  arguments can be corrected, such as a path outside the active unit;
- `CapabilityExceptionRequired`: no safe adapter can represent the effect and a
  complete exception artifact has been built;
- `DeniedCapability`: the effect is prohibited and cannot be escalated, such as
  a profile attempting to weaken the kernel.

Initial adapters should cover:

1. bounded repository reads and symbol/search discovery;
2. structured edits and file operations inside the worktree and active unit;
3. registered test/format/lint/type-check commands with bounded environment and
   output capture;
4. Git status/diff/add/commit/branch operations scoped to the worktree;
5. GitHub PR create/update/read, review comment read/reply/resolve, and checks
   inspection with exact repository/PR/thread identifiers;
6. BasedPyright diagnostics and registered project diagnostics;
7. structured specification, proposed-PR, skill, workflow, and task operations.

Arbitrary shell remains outside the first normal catalog. Add a semantic
command adapter for each repeated development effect instead of treating a
command string as a sufficient safety boundary.

Each mutating adapter needs conformance tests proving path/resource scope,
argument normalization, symlink handling, idempotent retry behavior, observed
effects, checkpoint compatibility, and output bounding. Network and GitHub
adapters additionally need fake clients that prove no unmanifested request is
sent.

## Durable guidance and relationship closure

Guidance processing has two separate operations:

1. `extract_rule` converts a user request to a candidate typed rule and stores
   original wording. It uses deterministic patterns where possible and a model
   only to propose normalization. Validation and scope selection remain code.
2. `match_rules` evaluates active typed rules against an action context and
   returns rule matches with provenance and explanations.

Never write a durable rule merely because the model inferred a preference from
silence. Explicit phrases such as “always,” “when you,” “from now on,” or a
direct request to remember behavior are candidates. If the intended scope would
materially change behavior across repositories or organizations, require a
single explicit scope decision.

`RelationshipEngine.expand()` receives the proposed action, context, matched
rules, existing obligations, and registered relationship definitions. It
returns new obligations and conflicts. Expansion must be pure so replay can
reproduce it.

The two motivating rules compile as follows:

```text
rule: after changing code in response to a review comment, resolve the comment
trigger: action source contains review_thread_id AND action effect is code write
relationship: change_action requires_after validate_changed_scope
relationship: validate_changed_scope requires_after resolve_review_thread
target selector: exact originating review_thread_id
completion guard: successful fresh evidence exists before resolution
```

```text
rule: use optimistic locking for mutable database rows
trigger: changed entity is a mutable persisted row
relationship: row_mutation requires implementation_pattern optimistic_locking
relationship: implementation_pattern requires_after concurrency_validation
completion guard: version/check-and-update behavior and concurrent-write test
                  evidence are present
```

The optimistic-locking trigger cannot rely only on the model labeling an edit.
It should combine structured project entities, changed paths/symbols, migration
or model metadata, and explicit action context. When certainty is insufficient,
open an explainable obligation for the Engineer to classify the entity; do not
silently skip the rule.

Conflict resolution order is deterministic:

1. kernel safety and deterministic truth;
2. explicit instruction in the current request;
3. narrower active behavior rule;
4. newer rule that explicitly supersedes an older rule;
5. delivery profile convention;
6. persona or skill preference.

An unresolved conflict that changes deliverable behavior becomes a user
decision artifact. It is not resolved by whichever rule happened to be loaded
last.

## Typed errors and correction

Extend `PowdrrExecutionError` without creating a hierarchy for every message:

```python
class ExecutionErrorKind(StrEnum):
    INVALID_ACTION = "invalid_action"
    INVALID_ARGUMENTS = "invalid_arguments"
    CONSTRAINT_VIOLATION = "constraint_violation"
    TOOL_FAILURE = "tool_failure"
    DIAGNOSTIC_FAILURE = "diagnostic_failure"
    STALE_STATE = "stale_state"
    TRANSITION_BLOCKED = "transition_blocked"
    EVIDENCE_STALE = "evidence_stale"
    RELATIONSHIP_CONFLICT = "relationship_conflict"


class PowdrrExecutionError(RuntimeError):
    kind: ExecutionErrorKind
    code: str
    message: str
    correctable: bool
    action_instance_id: str | None
    field_errors: tuple[FieldError, ...]
    source_references: tuple[SourceReference, ...]
    retry_after_seconds: float | None
    cause_error: Exception | None
```

All errors voluntarily raised because an agent action can be corrected use
`PowdrrExecutionError`. Provider transport exhaustion, process interruption,
programmer invariant violations, and corrupted persistence remain distinct
terminal exceptions. Adapters must not catch arbitrary `RuntimeError` and tell
the model to retry.

The correction packet contains only the typed failure, current action schema,
relevant obligations, and changed state. It does not resend prior-step
instructions. Repeated failures are counted by normalized code plus semantic
arguments, not raw prose.

## Checkpoints and edit diagnostics

A checkpoint captures:

- Git head, index fingerprint, and worktree patch fingerprint;
- files about to be mutated and content hashes;
- execution state version and active action;
- obligations, evidence, and findings affected by the action;
- external idempotency keys, but not a claim that external writes are reversible.

Revert restores workspace mutations and emits compensating state events. It
does not delete durable user guidance. It marks evidence produced after the
checkpoint stale, reopens obligations satisfied only by reverted actions, and
preserves findings and event history.

After an edit, run the fastest registered diagnostics for changed files and
attach them to the tool result. Diagnostics can cause a correctable failure but
cannot establish final readiness unless they are also declared authoritative
validation evidence.

## Readiness evaluation

`ReadinessEvaluator.evaluate(state, proposed_pr, profile)` is pure and returns
a report with one result per guard. Publication is allowed only when:

- the current plan revision matches the proposed-PR fingerprint;
- every criterion has implementation and current validation coverage;
- all required execution units are complete;
- all required evidence is successful and fresh;
- no required action obligation is open;
- no blocking finding is open or improperly disposed;
- review assignments and independence requirements are satisfied;
- no capability exception remains pending or expired mid-operation;
- workspace/Git scope matches the proposed PR and profile policy;
- required PR metadata and originating review-thread dispositions are complete.

The evaluator reports facts. The LLM may propose remediation but cannot change
the result directly.

## User-facing operations

Add thin CLI commands and matching MCP tools as their backing services land:

| Operation | CLI | MCP result |
| --- | --- | --- |
| Validate a profile | `validate-delivery-profile` | typed validation report |
| Validate manifests | `validate-tool-manifests` | manifest conformance summary |
| Inspect execution | `execution-status` | current phase, unit, obligations, evidence, findings |
| Verify/rebuild state | `verify-execution` | event/state integrity report |
| Explain a blocked transition | `explain-execution-block` | failed guards and source references |
| List guidance | `list-behavior-rules` | scoped active/superseded rules |
| Remember guidance | `remember-behavior-rule` | candidate/validated rule and scope |
| Explain behavior | `explain-behavior` | rule and relationship provenance |
| Revoke guidance | `revoke-behavior-rule` | append-only disposition result |
| Inspect exception | `capability-exception` | decision-ready request and status |
| Decide exception | `decide-capability-exception` | exact bounded token or denial |
| Evaluate readiness | `evaluate-pr-readiness` | deterministic guard report |

Mutating MCP operations accept an expected state/rule version and idempotency
key. Human-facing exception decisions display the exact arguments, resources,
effects, reversibility, alternatives, and expiration before accepting a
decision.

## Rollout controls

Use one mode enum rather than unrelated booleans:

```text
execution_kernel_mode = off | observe | enforce
```

- `off`: existing behavior; new parsers and validators may still be called by
  explicit validation commands.
- `observe`: build phase/persona/action/capability/guidance/readiness decisions,
  persist them separately, and report differences without changing execution.
- `enforce`: the kernel decision is authoritative.

Individual PRs may have temporary component modes internally, but public
configuration should converge on the one mode. Every fallback must record a
metric and have deletion criteria in the PR that introduces it.

An execution created in `enforce` mode cannot resume in `off`. Moving from
`observe` to `enforce` requires a successful state verification and profile
validation. The mode is persisted in the execution identity so restarts do not
silently change semantics.

## Pull request implementation sequence

The following PRs are ordered dependencies. Do not combine them unless the
result remains reviewable and independently reversible.

### PR 1: Delivery profile and extension boundary

Goal: define exactly what users may customize before runtime behavior changes.

Add:

- `src/powdrr_lift/core/delivery_profile.py`;
- `delivery-profiles/default-software-delivery.yaml`;
- `tests/test_delivery_profile.py`;
- default-profile fixtures with valid and invalid reviewer topologies.

Change:

- `core/workflow_task_specification.py` to accept optional `phase_type` and
  `persona_id`, preserving current `assignee_role` parsing;
- `core/workflow_template_specification.py` with the same optional references;
- `core/__init__.py` and package exports;
- `cli.py` and `mcp_server.py` for shared profile validation.

Migration:

- Map `AgentRole.ARCHITECT` to the Architect persona,
  `AgentRole.CODER` to Engineer, and `AgentRole.REVIEWER` to Code Reviewer when
  explicit persona data is absent.
- Do not add action fields to the profile.
- Do not add persona instructions to skill YAML.

Tests:

- round-trip all records;
- reject unknown phase/persona/profile fields;
- reject missing artifact owners and invalid transitions;
- reject a Reviewer or Architect assignment that requests a product-code write
  envelope;
- prove the default profile resolves all required artifact handoffs;
- prove old task/template fixtures parse and serialize without semantic change.

Acceptance gate: the default profile validates through both CLI and MCP, while
all existing workflows run unchanged.

### PR 2: Execution contracts, store, reducer, and shadow phase state

Goal: make execution state durable and replayable without enforcing it.

Add:

- `core/execution_state.py`;
- `execution/events.py`, `execution/store.py`, and `execution/phases.py`;
- `tests/test_execution_state.py`, `test_execution_store.py`, and
  `test_execution_phases.py`.

Change:

- chat and task runners to emit shadow action and transition events around
  existing behavior;
- observer packets to include execution/event IDs;
- replay fixtures to compare reduced state.

Implementation notes:

- Start event logging at the shared `WorkflowLLMExecutionDriver` boundaries.
- Persist under `WORKFLOW_DIR/execution/`; do not place metadata at workflow
  root.
- Keep event timestamps injectable for deterministic tests.
- Use expected state versions for every append.

Tests:

- table-test every allowed and rejected transition;
- property-test event reduction equivalence across valid event batching;
- simulate stale writes, truncated cache, duplicate sequence, interrupted
  replacement, and cache reconstruction;
- verify current runners and shadow state agree on current/terminal status.

Acceptance gate: observe mode can replay a complete existing workflow into the
same final phase and reports any disagreement without changing the exit code.

### PR 3: Tool manifests and constrained capability broker

Goal: prove that normal tools are safe to execute automatically.

Add:

- `core/tool_manifest.py`;
- `execution/tools.py` and `execution/capabilities.py`;
- manifests and adapters for repository read, bounded edits/file management,
  registered checks, Git, GitHub, BasedPyright, and structured Powdrr operations;
- adapter conformance-test helpers.

Change:

- existing intrinsic tool dispatch to resolve through `ToolRegistry`;
- shared prompt tool schemas to come only from executable capabilities;
- observer output to record shadow broker decisions.

Tests:

- one conformance suite per adapter;
- path traversal, absolute path, symlink escape, changed worktree, command
  injection, environment leakage, duplicate GitHub mutation, stale PR/thread
  identifier, and oversized output cases;
- effective action intersection tests proving a profile/persona cannot add an
  action omitted by the current step;
- prove `next_step` behavior with and without required outputs.

Acceptance gate: in observe mode, every existing normal tool call receives a
manifest decision; coverage and disagreement reports identify any ambient
effect before enforcement.

### PR 4: Decision-ready capability exceptions

Goal: replace rare unsafe escape approvals with exact capability decisions.

Add:

- exception contracts to `core/execution_state.py` or a focused
  `core/capability_exception.py` if the module would otherwise become unwieldy;
- exception persistence and token verification in `execution/capabilities.py`;
- shared CLI/MCP inspect and decide operations.

Tests:

- missing context prevents request presentation;
- approval token fails for altered arguments, effect, target, execution, use
  count, adapter, or expiration;
- denial is durable and prevents repeated prompts;
- expired approval cannot start an operation;
- external write retries use the same idempotency key.

Acceptance gate: a deliberately unsupported effect produces one complete
artifact; the exact approved call can run once and no broader call can reuse
the decision.

### PR 5: Persona runner and typed handoffs

Goal: make phase ownership operational rather than a label.

Add:

- `execution/personas.py`;
- persona packet and run records;
- default persona prompt catalogs containing responsibility/posture only;
- child-run resume fixtures.

Change:

- prompt construction to consume a `PersonaPacket`;
- model selection to resolve from the active assignment;
- chat/task compaction to retain typed run/artifact references;
- action resolution to intersect persona and phase envelopes.

Tests:

- packet snapshot tests prove no previous-step or inactive-persona instructions
  appear;
- handoff rejects missing, stale, wrong-version, or wrong-owner artifacts;
- Reviewer and Architect product-code edits are rejected before tool execution;
- child runs resume by ID with the same assignment and bounded capabilities;
- adding a second reviewer in a profile does not change kernel semantics.

Acceptance gate: one specification-to-proposed-PR-to-plan fixture crosses three
persona runs using typed artifacts and never relies on a prose-only handoff.

### PR 6: Typed execution-plan generation and evaluation

Goal: govern implementation mechanics without competing with the proposed PR.

Add:

- `core/execution_plan.py` and deterministic evaluator;
- plan generation skill output schema;
- plan artifact persistence and amendment events.

Change:

- `templates/execute-proposed-pr.yaml` planning output to `execution-plan-v1`;
- Engineer persona packet to include the accepted proposed-PR fingerprint;
- phase guards for plan approval/build entry.

Tests:

- complete/incomplete criterion coverage;
- cyclic units, unknown IDs, out-of-scope paths, missing validation, and changed
  proposed-PR fingerprint;
- auto-approval for a one-unit plan with no introduced decision;
- explicit decision artifact for material scope/risk choices;
- amendments invalidate only affected state and preserve history.

Acceptance gate: a valid proposed PR deterministically produces either Build
eligibility or an exact list of user decisions; free-form plan prose cannot
enter Build.

### PR 7: Durable behavior guidance store

Goal: remember explicit user operating rules with scope and provenance.

Add:

- `core/behavior_rule.py` and `execution/guidance.py`;
- repository-local guidance store and versioned mutations;
- remember/list/explain/revoke CLI and MCP operations.

Change:

- current user-message processing to nominate explicit durable guidance;
- persona packet construction to include applicable rule matches only;
- compaction to retain rule IDs/versions and original source references.

Tests:

- extraction and normalization of explicit durable requests;
- no rule from ordinary one-off instructions or inferred preferences;
- narrow scope selection, precedence, supersession, conflict, expiration, and
  revocation;
- stale update conflict using expected versions;
- a rule survives restart and remains explainable from original wording;
- a rule cannot broaden capabilities or suppress deterministic failures.

Acceptance gate: the review-comment rule can be stored, matched, explained,
revoked, and replayed without yet changing execution obligations.

### PR 8: Action relationships and obligation closure

Goal: turn matched guidance and built-in delivery relationships into enforceable
follow-up work.

Add:

- `core/action_relationship.py` and `execution/relationships.py`;
- obligation event reducers and explanation output;
- built-in relationship definitions for evidence invalidation, review finding
  correction, review-thread resolution, and mutable-row optimistic locking.

Change:

- action validation to run relationship expansion before execution;
- phase/readiness guards to consume open obligations;
- action completion to satisfy exact matching obligations.

Tests:

- bounded closure, cycle detection, deduplication, source-path explanation, and
  deterministic ordering;
- comment-driven edit opens validation then exact-thread resolution obligations;
- thread resolution before successful validation is rejected;
- mutable-row change triggers optimistic-locking and concurrency evidence even
  if the proposed edit lacks that label;
- unrelated edits do not trigger either rule;
- conflicting relationships produce a decision instead of arbitrary order.

Acceptance gate: both motivating rules change execution behavior, survive
resume, and block readiness until their related actions are complete.

### PR 9: Unified action lifecycle and typed correction

Goal: remove behavioral drift between chat and durable task execution.

Add/change:

- extend `PowdrrExecutionError` with structured correctable fields;
- move the action lifecycle into `execution/kernel.py`;
- adapt `WorkflowLLMExecutionDriver` to the kernel or replace its loop once both
  adapters have parity;
- make chat/task runners implement small input/presentation/persistence
  protocols;
- normalize observer and error-log events.

Migration:

- Replace voluntary generic `RuntimeError` raises for agent-correctable action
  failures with `PowdrrExecutionError`.
- Do not convert provider, cancellation, persistence-corruption, or programmer
  invariant errors into correctable failures.
- Delete duplicate retry/no-progress/correction branches only after side-by-side
  tests prove parity.

Tests:

- run identical parsed actions through chat and task adapters and compare event
  traces;
- malformed response, invalid action, invalid argument, constraint violation,
  correctable tool failure, diagnostics failure, provider retry/exhaustion,
  semantic stall, and terminal persistence failure;
- correction packet snapshots contain current instructions only.

Acceptance gate: the same action sequence has the same capability decisions,
events, retry counts, obligations, and final state in both runners.

### PR 10: Checkpoints, revert, and immediate diagnostics

Goal: make correction safe and recoverable.

Add:

- `execution/checkpoints.py` and content-addressed object storage;
- registered fast-diagnostic hooks;
- checkpoint/revert CLI and MCP inspection operations if user-facing recovery
  is required by existing workflows.

Change:

- capability execution to checkpoint before every mutating effect;
- edit results to include bounded diagnostics;
- event reduction for revert, evidence invalidation, and obligation reopening.

Tests:

- multi-file edit checkpoint and exact restore;
- partial tool failure after one observed mutation;
- revert after evidence/finding changes;
- durable behavior rules survive revert;
- external non-reversible effects are reported and never falsely restored;
- retention and garbage collection preserve referenced objects.

Acceptance gate: a deliberately broken repair can be reverted to the exact
workspace and logical state before the action, with complete audit history.

### PR 11: Validation evidence, findings, and readiness

Goal: make “done” a deterministic fact rather than a persona assertion.

Add:

- `execution/evidence.py` and pure `ReadinessEvaluator`;
- typed reviewer finding schemas and disposition actions;
- readiness CLI/MCP operation and scenario reports.

Change:

- registered checks to produce evidence records;
- edits to invalidate evidence by input fingerprint/dependency scope;
- reviewer persona outputs to typed findings;
- Publish transition to require a passing readiness report.

Tests:

- targeted and full-suite evidence freshness;
- relevant edit invalidates evidence while unrelated edit does not;
- blocking finding cannot be closed by author assertion;
- fixed/not-applicable/accepted dispositions require their configured evidence;
- independent reviewer agreement and useful disagreement;
- every readiness guard has a failing and passing case.

Acceptance gate: a PR cannot publish with stale evidence, open required
obligations, unresolved blocking findings, incomplete review, or an outdated
plan/proposed-PR fingerprint.

### PR 12: Compile delivery artifacts into workflow tasks

Goal: migrate the full proposed-PR path to the layered runtime.

Change:

- compile default delivery-profile assignments and `ExecutionPlan.units` into
  `WorkflowTaskTemplate`/`WorkflowTask` records with phase/persona references;
- migrate `execute-proposed-pr`, `run-tests-and-fix`, specification review,
  proposed-PR review, code review, review-comment correction/resolution, PR
  preparation, and PR creation definitions;
- ensure generated tasks retain `actions` as their only action declaration;
- update workflow definition validators and renderers.

Tests:

- golden compiled task graphs for one-unit and multi-unit plans;
- dependencies, artifact inputs/outputs, personas, and actions round-trip;
- profile customization changes models/skills/reviewer composition but not
  kernel guards;
- end-to-end scenarios for straightforward work, ambiguous plan, syntax error,
  failing suite, scope expansion, review correction, and PR readiness.

Acceptance gate: the default feature path runs in enforce mode from a structured
specification through a ready proposed PR using compiled typed tasks.

### PR 13: Deterministic compaction and compatibility removal

Goal: make the new path the only path and remove prompt/runtime duplication.

Add/change:

- compaction that summarizes prose/tool previews while retaining exact typed
  phase, persona, artifact, plan, action, obligation, evidence, finding,
  exception, rule, and checkpoint references;
- bounded full-output retrieval for truncated tool results;
- compatibility metrics and migration diagnostics for old persisted workflows;
- remove inferred actions, routine permission prompts, duplicate orchestration
  prose, and duplicate correction/retry loops;
- remove `off` mode for newly created executions after a documented migration
  window.

Tests:

- compaction during planning, repair, review, and exception handling;
- resume after process interruption before and after compaction;
- exact current-step prompt isolation after multiple transitions;
- old supported workflow migration and explicit error for unsupported schema;
- no normal-path human permission prompt in the complete scenario suite.

Acceptance gate: enforce mode is default, old and new control paths no longer
coexist for new executions, and the full suite proves resume/replay parity.

## Test strategy

### Unit tests

Every core contract needs round-trip, unknown-field, invalid-enum, duplicate-ID,
and schema-version tests. Every reducer and evaluator should be table-driven and
pure. Inject clocks, ID generators, model clients, filesystem adapters, command
runners, GitHub clients, and content stores.

### Adapter conformance tests

Create reusable contracts in `tests/execution/conformance/` for:

- manifest/adapter effect agreement;
- path and resource scope;
- idempotency and retry;
- checkpoint behavior;
- bounded output and complete-output references;
- evidence production and invalidation;
- exception-token enforcement.

An adapter cannot be registered in enforce mode unless it passes the applicable
conformance contracts.

### Integration tests

Run the same scripted model responses through chat and durable task adapters.
Assert event traces and materialized state, not terminal prose. Use fake
Git/GitHub/command clients for failure injection and exact-effect assertions.

### Scenario and replay tests

Extend `workflow-evals/scenarios` and replay support with at least:

- straightforward one-unit proposed PR;
- ambiguous plan requiring one decision;
- invalid/incomplete plan;
- syntax error immediately after edit;
- targeted check passing but required full suite failing;
- stale evidence after a relevant edit;
- out-of-scope edit corrected automatically;
- unsupported effect with a complete exception request;
- denied and expired exceptions;
- provider retry and exhaustion;
- compaction during repair;
- checkpoint revert;
- restart between action start and completion;
- review-comment change, validation, and exact thread resolution;
- mutable-row change requiring optimistic locking and concurrency evidence;
- profile customization and profile capability-escalation rejection.

Each scenario records expected phases, actions, tool decisions, obligations,
evidence, findings, user decisions, exceptions, and final readiness.

### Negative security tests

Include path traversal, symlink escape, environment/secret access, unregistered
command, changed external identifier, replayed mutation token, stale state
write, malicious profile field, prompt-requested phase bypass, model-declared
evidence, relationship cycle/explosion, rule-based capability broadening, and
reviewer product-code mutation.

### Required repository verification

Every implementation PR runs the repository's full CI-equivalent suite before
push:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

Targeted tests are useful during development but do not replace the full suite.

## Observability and evaluation

Emit structured counters and distributions keyed by phase, persona, action,
adapter, profile, and enforcement mode. Do not include secrets, full prompts, or
unbounded tool output.

Minimum rollout metrics:

- first-response schema/action validity;
- current-step action leakage attempts;
- shadow/enforced phase decision disagreement;
- capability broker executable/correctable/exception/denied counts;
- normal tool calls requiring human action, with a target of zero;
- repeated exception effect fingerprints suitable for a new safe adapter;
- rule candidates, matches, false positives, conflicts, and revocations;
- obligation closure latency and readiness blocks by relationship type;
- plan evaluation failures and post-approval amendments;
- evidence reuse and targeted invalidation rate;
- finding rates and disposition cycles by reviewer persona;
- checkpoint and recovery success;
- correctable failures and retries by typed error code;
- resume and replay integrity failures;
- prompt tokens per phase and per completed proposed PR.

Observer interventions should reference event and action IDs. Existing tuning
reports can compare an old replay with observe/enforce traces, but tuning must
not mutate kernel transition or safety rules.

## Migration and compatibility rules

- Existing workflow task files without `phase_type` or `persona_id` load through
  the compatibility mapping until PR 13.
- Existing `AgentRole` remains serialized while mixed-version workflows are
  supported; new code treats it as a compatibility projection, not policy.
- Existing skill and workflow `actions` fields are unchanged and required where
  they are required today.
- A persisted execution records profile and schema fingerprints. Resume refuses
  silent reinterpretation after incompatible profile/kernel changes.
- Observe-mode state is not silently promoted to authoritative state. Promotion
  validates the full event log, profile, manifests, open obligations, evidence,
  and current workspace fingerprint.
- Old workflows that cannot be safely migrated remain runnable only through an
  explicitly selected compatibility command during the migration window.
- Every compatibility branch logs its workflow/schema ID so removal is based on
  measured use.

## Implementation review checklist

Reviewers should answer these questions for every PR:

1. Did this change add a second source for step actions or generic action
   instructions?
2. Can any profile, persona, skill, prompt, remembered rule, or model response
   widen a kernel capability or redefine success?
3. Is each new state change represented by a typed event with deterministic
   reduction and optimistic versioning?
4. Is model-correctable failure represented by `PowdrrExecutionError`, while
   terminal infrastructure/programmer errors stay distinct?
5. Does a mutating effect have a manifest, adapter conformance tests,
   checkpoint/idempotency behavior, and exact observed scope?
6. Does the prompt include only the current phase, persona, step, effective
   actions, relevant obligations, and current correction?
7. Can a restart or compaction recover the same typed state without trusting a
   prose summary?
8. Are CLI and MCP handlers thin adapters over the same implementation?
9. Is the old behavior preserved or intentionally gated by observe/enforce mode?
10. Does the PR include its fallback removal criteria and full verification?

## First vertical slice

The first demonstrable end-to-end slice should be assembled as PRs 1 through 11
land, before migrating every workflow definition:

```text
Architect
  -> validates one structured specification change
Engineering Manager
  -> creates one proposed PR with one acceptance criterion
Engineer
  -> creates one-unit execution-plan-v1
  -> performs one bounded edit through a manifested adapter
  -> receives immediate diagnostics
  -> runs registered validation and records fresh evidence
Specification Reviewer + Code Reviewer
  -> independently return typed findings
Engineer
  -> resolves blocking findings and any related review-thread obligation
Kernel
  -> evaluates deterministic readiness and permits PR publication
```

Run the slice with these variations:

- default profile;
- profile with a different Engineer model and a second Code Reviewer;
- remembered review-thread-resolution rule;
- remembered optimistic-locking rule against a mutable row;
- one out-of-scope edit corrected without human approval;
- one truly unsupported external effect producing a bounded exception request;
- interruption after the edit followed by exact resume;
- revert of a broken correction followed by a successful alternative.

The slice passes only if profile customization changes delivery choices while
the action lifecycle, safety boundary, relationship closure, correction,
evidence, and readiness behavior remain identical.

## Final definition of done

The program is done when the default delivery profile can reliably execute the
full prompt-to-specification-to-proposed-PR-to-code-to-validation path in
enforce mode; users can customize the declared delivery model without modifying
the execution kernel; normal development tools require no safety approval;
exceptional effects receive exact decision-ready handling; explicit user
behavior is durable, scoped, explainable, and enforceable through related
obligations; and PR readiness is derived entirely from current typed state and
evidence.

At that point, the implementation should contain one shared action loop, one
current-step action declaration, one transition authority, one capability
broker, and one deterministic readiness evaluator. Anything left in persona or
skill prose that attempts to reproduce those mechanics should be deleted.
