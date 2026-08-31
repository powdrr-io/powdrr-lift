# State-Centric Long-Horizon Execution Architecture

## Purpose

Powdrr should manage agent execution as a sequence of validated state
transitions, not as an increasingly long conversation.

This document adapts the central ideas from
[`SKILL.state: Scalable Long-Horizon Agent Skills`](https://arxiv.org/html/2608.26263)
to Powdrr's stronger requirements for durable user intent, sandboxed tools,
typed obligations, evidence freshness, deterministic replay, and shared
interactive and automated execution.

The architectural claim is:

> The event log explains how execution arrived here. The execution state
> determines what happens next.

The implementation is complete when a long-running workflow can be interrupted
at any action boundary, lose all conversation and model reasoning, restart from
its persisted current state, and choose the same valid next action without
forgetting a requirement, decision, invariant, procedure, obligation, or
unresolved result.

## Thirty-second explanation

Most agents treat their transcript as their working memory. Every new decision
requires the model to reread an expanding mixture of old observations,
superseded plans, tool output, errors, and reasoning, then infer what is still
true. Summaries and sliding windows reduce the size but preserve the same
failure mode: current execution state remains implicit in prose.

Powdrr should instead give the model exactly three inputs on every round:

1. the immutable procedure and currently applicable policy;
2. the complete, typed state that matters for future execution; and
3. the latest environment observation.

The model proposes a typed state patch and one action. Powdrr validates both,
executes the action through the shared kernel, records the result, and reduces
it into the next state. Intermediate reasoning is discarded. Full history
remains available for audit and replay but does not participate in ordinary
next-action decisions.

## Paper model and value

`SKILL.state` defines each decision at execution step `t` as:

```text
A_t = (P, Sigma_t, O_t)
```

Where:

- `P` is the immutable procedural specification;
- `Sigma_t` is structured current execution state; and
- `O_t` is the latest environment observation.

The model produces:

```text
(R_t, DeltaSigma_t, a_t)
```

Where `R_t` is transient reasoning, `DeltaSigma_t` is a proposed state patch,
and `a_t` is the next action. After validation, the patch is applied and the
reasoning is discarded. The next prompt does not receive prior reasoning,
actions, or observations.

The paper's value does not come merely from serializing some state alongside a
conversation. Its state-plus-history baseline still degraded with execution
horizon. The value comes from making structured state a sufficient statistic
for future execution and removing the historical transcript as a reasoning
substrate.

This changes prompt growth from history-dependent quadratic cumulative token
usage to bounded per-step input and linear cumulative usage, assuming the
procedure, current state, and latest observation are themselves bounded. The
paper also reports improved long-horizon correctness, noise resistance, and
immediate recovery from external state changes compared with transcript,
summary, and state-plus-transcript runtimes.

For Powdrr, the most important benefits are:

- requirements and procedural obligations cannot disappear because an old
  turn was truncated or summarized incorrectly;
- obsolete failures and abandoned reasoning stop competing with current truth;
- the same state can drive chat, durable tasks, nested skills, restarts, and
  persona handoffs;
- prompt size depends on current task complexity rather than elapsed turns;
- validation errors become current observations instead of permanent prompt
  debris;
- replay can reproduce state and readiness without reproducing model prose;
  and
- missing state becomes an observable schema defect rather than an intermittent
  memory failure.

## Required execution invariant

Every ordinary model-controlled execution round must be representable as:

```text
ExecutionInput_t = (
  EffectiveProcedure_t,
  ExecutionSnapshot_t,
  LatestObservation_t,
)

ExecutionProposal_t = (
  AgentStatePatch_t,
  ActionProposal_t,
)
```

No normal next-action request may require:

- the full conversation;
- a rolling window of prior turns;
- the execution event stream;
- prior model reasoning;
- an LLM-generated history summary;
- prior tool results already incorporated into state; or
- free-form context whose operational meaning is not represented by the state
  schema.

A repair request for a malformed or rejected proposal may include that proposal
and its deterministic validation error as the latest observation. Once the
proposal is corrected, those repair messages do not become future execution
input.

## Separate the five runtime records

Powdrr currently uses overlapping transcript, event, context, fact, and typed
state records. The target architecture assigns each record one responsibility.

| Record | Responsibility | Recurring model input |
| --- | --- | --- |
| Specification | Desired outcome, procedure, applicable policy, and completion semantics | Relevant immutable projection |
| Execution state | Everything currently true that may affect future execution | Yes |
| Latest observation | New environment information not yet fully incorporated into state | Once |
| Event log | Audit, replay, provenance, diagnostics, and metrics | No |
| Transcript | Human interaction record | Only a current unanswered interaction |

Intermediate reasoning is not a sixth durable record. It is transient
computation used to propose a state transition and action.

### Specification

The specification says what should happen. It contains stable references to:

- selected skill and current step;
- root requirements and desired outcomes;
- structured design decisions;
- applicable invariants;
- applicable procedures and action relationships;
- action and capability contracts;
- validation profiles; and
- completion and readiness conditions.

The full repository specification does not need to enter every prompt. Powdrr
resolves an immutable `EffectiveProcedure` containing exactly the parts that
apply to the current execution state.

### Execution state

Execution state says what is currently true. It is a rebuildable materialized
projection, validated by a domain schema, versioned with optimistic concurrency
control, and bounded by active execution complexity rather than elapsed turns.

### Latest observation

The latest observation is the newest typed fact from outside the model's prior
state: a tool result, validation diagnostic, user answer, repository change,
review event, provider failure, or proposal validation error. It is presented
once. Future-relevant information must be incorporated into state during the
transition.

### Event log

The event log retains every accepted proposal, action lifecycle transition,
tool result reference, state change, obligation change, evidence decision,
checkpoint, user decision, and external reconciliation. It is authoritative
for replay and audit, but it is not ordinary prompt context.

### Transcript

The transcript remains useful for the user interface and historical record. A
current unanswered user question or decision is represented in typed
interaction state. Old conversation does not determine execution.

## Current Powdrr foundation

Powdrr already has most of the necessary primitives:

| Capability | Existing seam | Relevance to the target |
| --- | --- | --- |
| Shared model/action loop | `workflow_llm.py::WorkflowStepRunner` | Natural owner of the state transition protocol for chat and durable tasks |
| Action lifecycle | `execution/kernel.py::ActionKernel` | Deterministic proposal, start, completion, and failure boundary |
| Typed materialized state | `core/execution_state.py::ExecutionState` | Starting point for the canonical current-state projection |
| Event reduction | `core/execution_state.py::reduce_execution_event` | Deterministic state reconstruction |
| Optimistic persistence | `execution/store.py::FileExecutionStateStore` | State-version and event-sequence conflict protection |
| Lifecycle projection | `execution/shadow.py::ShadowExecutionRecorder` | Migration bridge from the current workflow runtime into typed events |
| Obligations and relationships | `execution/relationships.py` and execution state records | Durable procedural consequences |
| Evidence and readiness | `execution/evidence.py` | Runtime-owned truth and freshness |
| Typed reference compaction | `execution/compaction.py` | Useful projection rule, but not yet the live prompt contract |
| Durable user intent | behavior rules, specification decisions, invariants, and entity relationships | Input to the immutable effective procedure |

The recent shared-runner and durable execution-state work should be extended,
not replaced. The primary change is to make typed state authoritative instead
of shadowing a transcript-oriented runtime.

## Current divergences

### The live state is still transcript-oriented

`workflow_chat_agent.py::_WorkflowExecutionState` maintains separate mutable
collections for:

- `transcript`;
- `execution_events`;
- `execution_context`;
- `durable_facts`;
- handoff records;
- validation gates;
- file and fuzzy-match caches; and
- stalled-step context.

Some of these are valid state concerns, but they are not expressed through one
schema or reducer. Operational truth can therefore exist in several records,
and the next prompt must assemble all of them.

### Prompts still include history

`_build_step_execution_messages()` sends a bounded transcript, recent event
metadata, latest action, step context, durable facts, handoffs, current-file
context, and other derived fields. Bounding this data improves cost but does
not make current state sufficient.

### Compaction can affect correctness

`workflow_task_agent.py::_compact_workflow_task_context()` asks an LLM to
preserve requirements, decisions, errors, outputs, and other actionable facts
from task history. That summary is useful as a diagnostic artifact, but it
cannot safely be an authority for future execution. Omitting or paraphrasing a
fact can change behavior.

### Typed state is observational rather than authoritative

The shared runner writes action lifecycle events through a shadow recorder.
Recorder failure is deliberately isolated from the working workflow path. That
is appropriate for migration, but it means the typed state does not yet control
the live action loop.

### Materialized state grows with history

`ExecutionState.actions` retains completed action records. Obligations,
evidence, findings, and artifacts can also accumulate terminal records. A JSON
history is still history. The prompt projection must contain current active
truth, while completed historical records remain in the event stream.

### There is no state-patch protocol

The model proposes an action and includes free-form `decisions_and_context`,
but it does not return a schema-validated patch describing which
future-relevant beliefs or working facts should survive the round.

### Chat and task adapters still own context semantics

The adapters share action parsing and execution through `WorkflowStepRunner`,
but each still constructs and mutates its own transcript, events, and context.
This prevents one canonical execution-state path.

## Powdrr execution protocol

Introduce the following conceptual contracts in `execution/protocol.py`:

```python
@dataclass(frozen=True, slots=True)
class ExecutionInput:
    procedure: EffectiveProcedure
    state: ExecutionSnapshot
    observation: EnvironmentObservation | None


@dataclass(frozen=True, slots=True)
class ExecutionProposal:
    expected_state_version: int
    state_patch: AgentStatePatch
    action: ActionProposal


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    accepted: bool
    next_state: ExecutionSnapshot | None
    correction: EnvironmentObservation | None
    emitted_events: tuple[ExecutionEvent, ...]
```

The shared round becomes:

```text
1. Load state Sigma_t at version n.
2. Reconcile any external observation into typed state.
3. Resolve applicable specification and durable intent into P_t.
4. Project the bounded ExecutionInput(P_t, Sigma_t, O_t).
5. Request exactly {expected_state_version, state_patch, action}.
6. Validate patch syntax, schema, ownership, references, and invariants.
7. Validate action capabilities, prerequisites, and open obligations.
8. Persist the accepted proposal event.
9. Execute through ActionKernel.
10. Normalize the actual result into O_t+1.
11. Deterministically reduce result and accepted agent patch into Sigma_t+1.
12. Append events atomically using expected state version n.
13. Evaluate transitions and readiness from Sigma_t+1.
14. Repeat with only P_t+1, Sigma_t+1, and O_t+1.
```

The runtime should apply model-maintained working-state changes only after
schema and ownership validation. Runtime and environment truth changes only
through deterministic reducers backed by actual outcomes.

## State ownership

Every field in the execution schema must declare an owner.

### Runtime-owned state

Only Powdrr reducers may change:

- execution, phase, unit, and step identity;
- action lifecycle;
- capability decisions;
- obligation status and dependency eligibility;
- evidence success and freshness;
- finding disposition;
- checkpoint identity;
- contract and specification fingerprints;
- allowed transitions; and
- readiness.

### Environment-owned state

Only normalized observations from constrained tools may establish:

- file contents and worktree fingerprints;
- Git branch, commit, and dirty state;
- pull request and review-thread state;
- CI and validation results;
- external issue state;
- tool-produced artifacts; and
- remote resource existence.

### Agent-owned state

The model may patch validated fields such as:

- current hypothesis;
- selected implementation approach;
- active targets;
- tested hypotheses;
- unresolved questions;
- concise working notes;
- expected next objective; and
- classification proposals awaiting deterministic or user confirmation.

The model cannot make a validator pass, close an obligation, resolve a review
thread, accept an artifact, or declare readiness by editing state.

## Execution state schema

Use a common envelope plus a skill-specific state extension:

```yaml
schema_version: execution-state-v2
execution_id: execution-123
state_version: 42
event_sequence: 108

control:
  profile_id: proposed-pr-delivery
  phase: validate
  phase_revision: 3
  active_unit_id: unit-7
  active_step_id: run-tests
  persona_id: implementation-agent
  allowed_transitions: [repair, review]

objective:
  root_requirement_ids: [requirement-17]
  current_goal: Make the persistence implementation pass mypy.
  completion_gate_ids: [gate-mypy, gate-tests]

working_state:
  current_hypothesis: The return annotation is too broad.
  selected_approach: Narrow the repository protocol return type.
  active_paths: [src/powdrr_lift/execution/store.py]
  tested_hypotheses: []
  unresolved_questions: []

artifacts:
  accepted: []
  pending: []

obligations:
  open: [obligation-run-mypy]
  blocked: []
  eligible: [obligation-run-mypy]

evidence:
  current: []
  stale: [evidence-mypy-before-edit]
  required: [mypy]

findings:
  open: []
  awaiting_validation: []

resources:
  current_file: src/powdrr_lift/execution/store.py
  worktree_fingerprint: sha256:...
  branch: codex/example
  pull_request: null

interaction:
  unanswered_question: null
  pending_user_decision: null

effective_contract:
  fingerprint: sha256:...
  rule_ids: [python-changes-pass-mypy]
  decision_ids: []
  invariant_ids: [typed-python]
  procedure_ids: []

skill_state:
  schema_id: run-tests-and-fix-state-v1
  value:
    failing_tests: []
    active_failure: mypy:arg-type:store.py:88
    files_examined: [src/powdrr_lift/execution/store.py]
    repair_attempts: 1
```

The envelope provides common orchestration and safety semantics. The skill
schema captures the smallest domain-specific sufficient statistic.

## Skill state schemas

Each executable skill should declare:

```yaml
execution_state:
  schema: run-tests-and-fix-state-v1
  initial: {...}
  agent_writable:
    - /working_state/current_hypothesis
    - /working_state/selected_approach
    - /skill_state/value/active_failure
    - /skill_state/value/files_examined
  retention:
    tested_hypotheses: bounded_set:20
    files_examined: bounded_set:50
  accepted_observations:
    - validation_failed
    - validation_passed
    - file_changed
    - action_rejected
  completion:
    - all_validation_obligations_fresh_and_successful
```

Schemas are authored once per execution domain, not synthesized for every
task. Start with:

- `run-tests-and-fix`;
- review-comment correction;
- specification authoring; and
- proposed-PR execution.

Schema validation must reject unknown paths, wrong types, unauthorized writes,
invalid references, size-limit violations, and incompatible versions.

## Bounded-state rules

Making state structured is insufficient if it grows with every turn. Each
field must declare retention behavior:

- `current`: one current value replaces the previous value;
- `active_set`: retain only unresolved active records;
- `bounded_set:N`: retain at most `N` deduplicated current items;
- `durable_until:<scope>`: retain until step, unit, phase, or execution exit;
- `artifact_ref`: retain a content-addressed reference, not the body;
- `historical_only`: emit to the event log and omit from current state; or
- `derived`: recompute deterministically and do not persist as independent
  truth.

Specific changes to `ExecutionState` should include:

- replace cumulative `actions` with the active action and bounded recent
  failure signatures needed for control;
- move completed action records entirely to the event stream;
- retain only open or operationally relevant obligations in the prompt
  projection;
- retain current and required evidence, not every prior validation result;
- retain open findings and dispositions that still constrain readiness;
- represent large output through typed artifact references; and
- enforce state and field size limits independently of prompt token limits.

Terminal records may remain in the rebuildable materialized store for indexing
if needed, but they must not enter `ExecutionSnapshot`, the model-facing
sufficient-statistic projection.

## Observation protocol

Tool output must be normalized before it becomes execution input:

```yaml
observation_id: observation-193
kind: validation_failed
source_action_id: action-192
scope:
  paths: [src/powdrr_lift/execution/store.py]
payload:
  validator: mypy
  diagnostics:
    - path: src/powdrr_lift/execution/store.py
      line: 88
      code: arg-type
      message: Argument has incompatible type.
artifact_ref: sha256:complete-output
```

Observation schemas should exist for:

- action completed or failed;
- proposal or patch rejected;
- file read, changed, missing, or moved;
- command and validation result;
- Git and GitHub state;
- user answer or decision;
- provider failure;
- external state reconciliation; and
- checkpoint restoration.

The complete raw output is stored as an artifact when needed. The next model
request receives the normalized observation once. Facts needed later are
projected into state; everything else expires from execution input.

This makes error correction precise. A validation error is sent back as the
latest observation with allowed corrective actions. It is not appended to an
ever-growing conversation.

## Durable intent as part of the procedure

The durable-user-intent architecture is the policy half of this model. The
`EffectiveProcedure` should be composed from:

```text
selected skill specification
+ current step contract
+ applicable requirements and desired outcomes
+ applicable design decisions
+ applicable invariants
+ applicable procedures and action relationships
+ capability constraints
+ completion and readiness conditions
```

Static and dynamic applicability resolution produces this immutable projection
before every model request and after every proposed action. The prompt and the
runtime validators use the same object.

Instructions such as these therefore survive independently of conversation:

- after addressing a review comment, validate the change and resolve the exact
  thread;
- mutable database rows use optimistic locking; and
- Python implementation changes include mypy-compatible type information and
  fresh mypy evidence.

The agent does not need to remember to retrieve them. They become part of `P_t`
when state and action selectors make them applicable. Their consequences become
runtime-owned obligations and evidence requirements in `Sigma_t`.

## State patches and action execution

The model response should contain exactly one state patch and one action:

```json
{
  "expected_state_version": 42,
  "state_patch": [
    {
      "operation": "replace",
      "path": "/working_state/current_hypothesis",
      "value": "The protocol return type is too broad."
    },
    {
      "operation": "add_unique",
      "path": "/skill_state/value/files_examined",
      "value": "src/powdrr_lift/execution/store.py"
    }
  ],
  "action": {
    "kind": "edit",
    "file_path": "src/powdrr_lift/execution/store.py",
    "edits": []
  }
}
```

Use a closed patch operation set rather than arbitrary dictionary merge:

- `replace` for scalar current values;
- `remove` for nullable agent-owned values;
- `add_unique` and `remove_value` for bounded sets; and
- skill-specific closed operations where structural validation requires them.

Each operation is validated against schema, ownership, retention, and size
rules. The proposal includes `expected_state_version` so concurrent writes or
external reconciliations cause a correction instead of silently overwriting
new truth.

The action and patch are one proposal but have different authority. An accepted
agent patch may update working knowledge. The action's claimed external effects
are not committed until observed. Failed actions produce an observation and may
update failure-related state but cannot commit the expected world transition.

## External change and state recovery

Before constructing a request, Powdrr should reconcile cheap authoritative
state such as worktree and active remote-resource fingerprints. Material drift
produces a typed observation and deterministic state invalidation.

Examples:

- a force push invalidates branch-based evidence and planned commit ancestry;
- a later edit invalidates affected validation evidence;
- an externally resolved review thread satisfies or obsoletes its exact
  obligation only after authoritative confirmation;
- a closed pull request clears the active PR resource and opens a decision or
  replacement obligation; and
- a renamed file invalidates stale path state and returns a recoverable
  file-missing observation with candidate paths.

The next decision sees current truth immediately. It does not need to overcome
contradictory historical observations still present in the prompt.

## History inspection as an exception

The paper correctly identifies cases where fixed state may not be sufficient:

1. the relevant schema is not known in advance;
2. an old observation becomes relevant only after its importance was missed;
3. the historical trajectory is itself the requested output; and
4. multiple agents concurrently update shared state.

Powdrr should retain the full event log and artifacts to handle these cases
without making history the default substrate.

Provide explicit constrained actions such as:

```text
inspect_event(event_id)
inspect_artifact(artifact_ref, range)
query_history(event_type, scope, limit)
```

Their selected result becomes the latest observation. If history inspection
changes the next action because an operationally relevant fact was absent from
state, record a `state_schema_gap` event. Repeated gaps should drive a schema
revision or new deterministic projection.

Audit, debugging, provenance, and explanation views may consume the full
history because history is their subject. Ordinary workflow execution may not.

For concurrent agents, all writes require expected state versions. Conflicts
are resolved through field ownership and deterministic merge rules; ambiguous
agent-owned conflicts create a typed decision rather than last-write-wins
prose.

## Checkpoints, restart, and replay

A checkpoint should contain or reference:

- execution state version and fingerprint;
- effective-procedure fingerprint and referenced versions;
- current latest-observation ID;
- active worktree or external-resource fingerprint;
- open obligation and current evidence IDs; and
- content-addressed artifact references.

It should not require a transcript snapshot for correctness.

Restart loads the checkpoint or materialized state, verifies it against the
event stream, reconciles current environment state, resolves the effective
procedure, and resumes from `(P, Sigma, O)`.

Replay deterministically reduces events into each state version. Prompt replay
uses the stored effective-procedure fingerprint, model-facing state projection,
and latest observation. It does not reconstruct a conversation.

## Explanation and user experience

State-centric execution should remain inspectable. For every proposed or
blocked action, Powdrr should be able to answer:

- What is the current objective?
- What does Powdrr currently believe?
- Which facts came from tools, the runtime, the user, or the model?
- Which decisions and invariants apply?
- Which obligations are open and why?
- Which evidence is current or stale?
- Why is this action allowed or blocked?
- What changed in the last transition?
- Which source events can reconstruct this state?

The UI may render a narrative summary, timeline, or conversation, but those are
views over structured state and events. They are not hidden execution memory.

## Engineering plan

Implement the architecture in independently mergeable pull requests. Maintain
the current runtime in observe mode until state and action parity are measured.

### PR 1: Execution protocol and schema ownership

Goal: define the formal contracts without changing live behavior.

Required changes:

- add `execution/protocol.py` with `ExecutionInput`, `EffectiveProcedure`,
  `ExecutionSnapshot`, `EnvironmentObservation`, `AgentStatePatch`,
  `ExecutionProposal`, and `TransitionDecision`;
- define closed observation and patch operation enums;
- define runtime, environment, and agent field ownership;
- add retention and size policies;
- define canonical serialization and fingerprints;
- add schema and reference validation; and
- document the invariant that transcripts, events, and generated summaries are
  not execution inputs.

Tests:

- reject unknown fields, patch operations, and observation kinds;
- reject model writes to runtime- and environment-owned paths;
- reject stale expected state versions;
- prove canonical fingerprints independent of map ordering;
- enforce bounded collection and scalar limits; and
- round-trip every protocol object.

Acceptance gate: a complete model round can be represented by the new protocol
with no transcript or prior-event field.

### PR 2: Bounded execution-state projection

Goal: evolve the materialized state into the canonical sufficient statistic.

Required changes:

- add control, objective, working, active artifact, obligation, evidence,
  finding, resource, interaction, effective-contract, and skill-state records;
- split rebuildable storage state from model-facing `ExecutionSnapshot`;
- replace cumulative action prompt state with active lifecycle state;
- add deterministic projection and retention enforcement;
- move terminal history to events and indexes;
- retain content-addressed references for large values; and
- version and migrate development execution state to the current schema without
  parallel live formats.

Tests:

- replay produces an identical current snapshot;
- terminal actions do not increase prompt-state size;
- only active obligations and current evidence enter the snapshot;
- large artifacts enter as references;
- scope exit removes `durable_until` fields; and
- a 200-step constant-complexity fixture has bounded serialized state.

Acceptance gate: `ExecutionSnapshot` size depends on active concerns, not event
count.

### PR 3: Skill state schemas and typed observations

Goal: make domain-specific state explicit for the first high-value workflows.

Required changes:

- extend skill specifications with execution-state schema, initial value,
  writable paths, retention, accepted observations, and completion predicates;
- implement schemas for `run-tests-and-fix`, review-comment correction,
  specification authoring, and proposed-PR execution;
- normalize action, file, validation, Git, GitHub, user, and provider results;
- persist full raw output through artifact references; and
- emit schema-gap events when explicit history recovery reveals missing state.

Tests:

- each skill initializes valid state;
- every action result normalizes to a closed observation type;
- future-relevant error data survives through state after its observation
  expires;
- file-not-found observations expose typed alternatives;
- malformed or oversized skill patches are rejected; and
- completion predicates depend only on specification and state.

Acceptance gate: each initial skill can resume correctly without transcript or
generated summary.

### PR 4: Authoritative shared state runner

Goal: make `WorkflowStepRunner` the sole owner of state transitions.

Required changes:

- replace optional shadow recording with a required execution-state session;
- load and persist state around every shared-runner round;
- resolve the immutable effective procedure before each request;
- parse `{expected_state_version, state_patch, action}` once for both adapters;
- validate patches and actions before `ActionKernel.start()`;
- normalize results and reduce state after terminal lifecycle events;
- route policy, relationship, obligation, evidence, and readiness decisions
  through the state transition; and
- return proposal rejection as the latest correctable observation.

Tests:

- chat and durable tasks produce identical transitions from identical inputs;
- rejected patches do not mutate state;
- failed actions do not commit expected environment changes;
- successful actions update agent and runtime fields atomically;
- concurrent version conflicts return a typed correction;
- interruption at every lifecycle boundary recovers deterministically; and
- observer or diagnostic failures cannot corrupt authoritative state.

Acceptance gate: typed state controls live execution rather than shadowing it.

### PR 5: Prompt cutover to `(P, Sigma, O)`

Goal: remove history from ordinary execution prompts.

Required changes:

- replace chat and task prompt builders with one protocol renderer;
- stop sending transcript, prior event lists, rolling execution context, and
  prior action results;
- represent current user interaction in typed interaction state;
- present the latest observation once;
- remove LLM-authored context compaction from correctness;
- retain summaries only as human or diagnostic artifacts;
- add explicit history and artifact inspection actions; and
- enforce prompt component budgets separately for procedure, state, and
  observation.

Tests:

- prompt content is exactly the effective procedure, state snapshot, latest
  observation, tool schema, and response schema;
- prompt size does not increase with completed roundtrips;
- empty or different diagnostic summaries cannot change decisions;
- old observations disappear after state incorporation;
- repair prompts disappear after successful correction; and
- no adapter independently adds transcript or events.

Acceptance gate: prompt size is independent of workflow history for a bounded
state fixture.

### PR 6: Restart, replay, and external reconciliation

Goal: prove state sufficiency under real disruptions.

Required changes:

- persist state-centric checkpoints;
- restore without transcript snapshots;
- reconcile worktree, branch, PR, review, and validation fingerprints;
- invalidate affected evidence and obligations deterministically;
- render state transitions and provenance for users; and
- add event-to-state replay and prompt-only replay fixtures.

Tests:

- restart after every action boundary chooses the same next valid action;
- force push, closed PR, external thread resolution, file rename, and stale CI
  each produce correct state changes immediately;
- event replay reproduces exact state and fingerprints;
- checkpoint restore does not consult transcript prose; and
- history inspection is explicit, bounded, and recorded.

Acceptance gate: losing all conversation and reasoning does not affect restart
correctness.

### PR 7: State-execution benchmark and enforce-mode rollout

Goal: measure and then enforce the architectural value.

Compare:

1. current transcript runtime;
2. bounded transcript;
3. structured state plus transcript;
4. pure `(P, Sigma, O)` execution.

Measure:

- task and workflow completion correctness;
- skipped obligations;
- stale-evidence acceptance;
- repeated failed actions;
- external-change recovery steps;
- average and cumulative input tokens;
- serialized current-state size;
- irrelevant-noise sensitivity;
- restart and replay equivalence;
- state schema gaps; and
- chat/task transition disagreement.

Run at 10, 25, 50, 100, and 200 model-controlled actions with deterministic
scenario seeds and forced disruptions. Start the new runtime in observe mode,
compare its proposed transitions with the live path, and enable enforcement
after parity thresholds pass.

Acceptance gate: pure state execution maintains correctness while prompt size
tracks current complexity rather than elapsed actions, and every final decision
is reproducible from specification, state, and latest observation.

## Verification scenarios

| Scenario | Required proof |
| --- | --- |
| Run tests and fix | Active failure, attempted repair, affected paths, and fresh validation status survive without prior tool turns. |
| Review correction | Edit opens validation and exact-thread obligations; completion waits for both in order. |
| Mutable database row | Applicable optimistic-locking invariant and concurrency evidence are present even when the model never mentions them. |
| Python implementation | Typing invariant applies and current mypy evidence is required after relevant edits. |
| Missing file | Latest observation records the missing path and candidates; the next action moves to a valid file without retaining the failed transcript. |
| Repeated validation failure | Tested hypothesis and current diagnostic persist, while raw old output and reasoning do not. |
| Irrelevant telemetry | Noise that does not change state disappears after one observation and cannot accumulate attention drag. |
| Force push | Branch and evidence state reconcile immediately without historical contradiction. |
| Process restart | Next valid action and readiness are identical with no transcript loaded. |
| Nested skill | Child receives a narrowed procedure and state projection while parent obligations remain authoritative. |
| User decision | Answer updates structured decision and interaction state; old question/answer turns are unnecessary. |
| Replay | Event reduction produces the exact snapshot and effective-procedure fingerprint used originally. |

## Architectural tests

Add tests that enforce the design itself:

- no ordinary prompt builder accepts a transcript parameter;
- no ordinary prompt contains execution event history;
- no transition guard parses model prose or a generated summary;
- no model patch can change runtime or environment truth;
- no obligation closes from narrative text;
- no evidence becomes successful without an accepted producer result;
- no compacted summary is authoritative;
- no adapter maintains independent execution-state semantics;
- no terminal action history enters the model-facing snapshot;
- no state field lacks ownership and retention metadata;
- no relevant durable instruction depends on voluntary retrieval; and
- no restart requires prior reasoning or conversation.

## Operational metrics

Record:

- effective-procedure tokens;
- execution-snapshot tokens and bytes;
- latest-observation tokens and artifact size;
- total input tokens by action count;
- active versus historical record counts;
- patch rejection count by reason;
- optimistic version conflicts;
- state schema gaps and history inspections;
- observation incorporation and expiry;
- obligation and evidence transitions;
- external reconciliation latency;
- replay fingerprint mismatches;
- chat/task transition mismatches; and
- restart next-action equivalence.

The target is not the smallest possible state. It is the smallest complete
sufficient statistic: every fact that can affect future execution, no obsolete
history, and identical behavior before and after restart.

## Final acceptance demonstration

Run a proposed-PR workflow containing at least 100 model-controlled actions.
Before execution, supply these durable instructions:

1. after addressing a review comment, validate the change and resolve the exact
   thread;
2. mutable database rows always use optimistic locking; and
3. Python implementation changes include mypy-compatible type information and
   pass mypy.

During execution:

- inject large irrelevant tool output;
- return a file-not-found error with alternative candidates;
- fail validation and require a different repair;
- compact or delete all conversation and reasoning;
- stop and restart the process;
- force-push the branch or change a remote review state; and
- invoke a nested skill with a different persona.

At every boundary, persist the model-facing `EffectiveProcedure`,
`ExecutionSnapshot`, and `LatestObservation` fingerprints. The run passes only
if:

- prompt size remains bounded by current complexity;
- the three durable instructions become applicable at the correct actions;
- their obligations and evidence requirements are enforced;
- obsolete errors and irrelevant output disappear from future prompts;
- external changes are reflected immediately;
- restart chooses the same valid next action;
- chat and durable-task adapters produce the same transition;
- readiness is derived entirely from current structured state; and
- the final result can be reproduced from events without reconstructing a
  conversation.

That demonstration proves Powdrr is managing execution state rather than
managing an agent's memory of a conversation.
