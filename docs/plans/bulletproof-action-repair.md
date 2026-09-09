# Bulletproof Action Repair Plan

## Objective

Make malformed, invalid, failed, and non-progressing LLM actions recover through
a bounded sequence of materially different strategies. The system must never
send the same effective repair request repeatedly and hope for a different
answer.

The repair mechanism cannot guarantee that an LLM will produce a useful answer.
It can guarantee that:

- every failure is classified deterministically;
- every retry changes the repair environment in a meaningful, recorded way;
- rejected strategies cannot be repeated under cosmetic variations;
- later attempts receive smaller and more constrained prompts;
- deterministic recovery is used when the next action is unambiguous;
- model fallback and human escalation occur at explicit boundaries; and
- repair always terminates with a valid action or a durable failure result.

## Current behavior

Repair responsibility is currently distributed across several layers:

- `workflow_chat_agent.py` owns JSON-response repair, prompt construction,
  model fallback, and chat-specific action correction.
- `workflow_llm.py` owns the shared action request/execution loop, action failure
  counting, semantic action signatures, and no-progress detection.
- `workflow_task_agent.py` turns durable-task failures into a
  `response_correction` string that is added to the next task prompt.
- Chat execution restores a step checkpoint after repeated stalls and includes
  `stalled_step_context` in the next full step prompt.
- The observer can diagnose a failure and recommend an action, but it is not the
  authoritative repair policy.

There are useful guards already:

- malformed JSON receives a corrective prompt;
- response fingerprints detect exact repeated invalid payloads;
- action failure signatures omit narrative fields so changes to rationale do
  not look like progress;
- repeated action failures and material-state stalls are bounded;
- configured backup models can be selected after semantic repair exhaustion;
- chat step checkpoints can discard mutations from a stalled attempt.

The central weakness is that most repair attempts preserve the original prompt
and append another correction. The model continues to see the same framing,
transcript, action history, and previous answer. The request is syntactically
different but often semantically equivalent. Durable tasks and interactive
skills also apply different repair behavior despite sharing the same execution
loop.

## Desired invariants

The implementation must enforce these invariants in code:

1. A repair attempt has a stable identity consisting of the execution
   boundary, failure class, rejected strategy, prompt profile, action space,
   and model.
2. Two attempts with the same identity cannot be issued.
3. Each escalation must do at least one of the following:
   reduce the action space, remove context, change the reasoning task, apply a
   deterministic transformation, or change the model/provider.
4. Transport retries do not count as semantic repair attempts and may reuse the
   same prompt only for classified transient transport failures.
5. A rejected semantic action signature remains rejected for the lifetime of
   the current step or task attempt, including after checkpoint restoration.
6. No action is executed until it passes parsing, response-schema validation,
   step-contract validation, runtime proposal validation, and rejected-strategy
   checks.
7. Repair state is cleared only after material progress or a step/task
   transition, not merely after receiving a differently worded action.
8. Repair terminates after a configured bounded ladder. Exhaustion produces a
   structured failure or human handoff rather than another retry.

## Failure taxonomy

Introduce one shared `RepairFailureClass` enumeration. Classification must be
deterministic and happen before selecting a repair strategy.

| Failure class | Examples | Initial recovery |
| --- | --- | --- |
| `transport_transient` | timeout, rate limit, temporary overload | identical transport retry |
| `transport_terminal` | authentication, unsupported model | model/provider fallback or stop |
| `response_empty` | no content, incomplete stream | targeted response repair |
| `response_syntax` | invalid JSON, non-object response | targeted response repair |
| `response_schema` | missing action field, invalid parameter type | schema-constrained repair |
| `action_contract` | action not allowed for the step | narrowed action selection |
| `action_precondition` | missing file, invalid line range, unmet obligation | clean-room replanning |
| `action_execution` | tool or deterministic action failure | clean-room replanning |
| `no_material_progress` | repeated read/edit with unchanged state | clean-room replanning |
| `validation_regression` | issue fingerprints unchanged or worse | strategy exclusion and replanning |
| `completion_blocked` | required outputs/actions remain missing | completion-focused constrained choice |

Errors should carry a machine-readable code, action kind, affected target,
failed precondition, remediation, and retryability. String matching remains a
compatibility fallback while existing errors are migrated to typed codes.

## Shared repair coordinator

Add a `WorkflowRepairCoordinator` in `workflow_llm.py`. It belongs beside
`WorkflowStepRunner` because the shared runner is the only layer that sees all
response, proposal, execution, and progress outcomes.

The coordinator owns:

- failure classification;
- the rejected-strategy ledger;
- semantic attempt counting;
- escalation-stage selection;
- prompt-profile selection;
- action-space narrowing;
- repair-attempt identity and duplicate prevention;
- model-switch recommendations; and
- terminal recovery decisions.

The coordinator must not know chat or durable-task prompt formats. Strategies
provide typed repair context and render requests selected by the coordinator.

Proposed core types:

```python
class RepairStage(Enum):
    TARGETED = "targeted"
    CLEAN_ROOM = "clean_room"
    SELECT_ACTION = "select_action"
    FILL_ACTION = "fill_action"
    DETERMINISTIC = "deterministic"
    MODEL_FALLBACK = "model_fallback"
    HUMAN_HANDOFF = "human_handoff"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class RepairFailure:
    classification: RepairFailureClass
    error_code: str
    message: str
    action_payload: Mapping[str, Any] | None
    action_signature: str | None
    target_signature: str | None
    remediation: str | None


@dataclass(frozen=True)
class RepairContext:
    execution_id: str
    boundary_id: str
    objective: str
    deterministic_state: Mapping[str, Any]
    allowed_actions: tuple[str, ...]
    action_schemas: Mapping[str, Mapping[str, Any]]
    required_outputs: tuple[str, ...]
    open_obligations: tuple[Mapping[str, Any], ...]
    rejected_strategies: tuple[RejectedStrategy, ...]
    last_material_progress: Mapping[str, Any] | None


@dataclass(frozen=True)
class RepairDirective:
    stage: RepairStage
    prompt_profile: str
    allowed_actions: tuple[str, ...]
    selected_action: str | None
    model_policy: str
    reason: str
```

`WorkflowActionRequest` should carry the normal request plus a strategy-owned
repair request factory. The shared runner supplies a `RepairDirective` and
`RepairContext`; the adapter renders the actual messages and response schema.
This keeps prompt differences behind the step/task abstraction while keeping
escalation policy shared.

## Escalation ladder

### Stage 0: normal request

Use the existing production prompt and full response schema. Record the prompt
profile, model, allowed actions, context fingerprint, and response.

This is not a repair attempt.

### Stage 1: targeted correction

Use only for empty, syntax, or simple response-schema failures where the action
strategy itself has not been rejected.

The prompt may retain the original messages, followed by:

- the exact machine-readable error;
- the rejected payload;
- the relevant response-schema fragment;
- one valid shape for the intended action kind; and
- an explicit statement that every required field must be regenerated.

The response schema should be narrowed to the intended action kind when the
kind is known and valid. This stage gets one semantic attempt. A second failure
escalates; it does not append another correction to the growing prompt.

### Stage 2: clean-room replanning

This is the mandatory materially different prompt requested by this plan. It
must be constructed from scratch and must not contain the original transcript,
the original system prompt, assistant reasoning, raw tool history, or the full
invalid response.

The system message should identify the request as constrained recovery:

```text
You are recovering a stalled workflow boundary. Do not continue the previous
conversation. Choose one legal action that materially advances the supplied
objective from the supplied state. Rejected strategies are forbidden.
```

The user message contains only:

- boundary identity and objective;
- current material state;
- allowed action names and compact descriptions;
- current required outputs and open obligations;
- concise facts from the most recent successful action;
- classified failure details;
- rejected semantic strategy summaries; and
- the narrowed response schema.

Large command outputs should be represented by structured facts or bounded
diagnostics. The previous payload may be represented only by its rejected
strategy summary, never copied wholesale.

### Stage 3: constrained action selection

If clean-room replanning still produces an invalid, rejected, or stalled
strategy, split reasoning from parameter generation.

The first call returns only:

```json
{
  "action": "one-name-from-the-allowed-enum",
  "why_it_can_progress": "short explanation",
  "required_facts_present": ["fact-id"]
}
```

The response schema uses an enum containing only currently legal action names.
The coordinator rejects choices whose action-level signature is already
excluded or whose deterministic preconditions are false.

If no action kind is selectable, skip directly to deterministic recovery,
model fallback, or human handoff.

### Stage 4: constrained parameter generation

After action selection, make a separate request for parameters of that action
only. Do not expose the union schema for every action.

The prompt contains:

- the selected action name;
- that action's parameter schema;
- deterministic state relevant to those parameters;
- known invalid targets or parameter combinations;
- required outputs the action can produce; and
- a requirement to return the complete selected action object.

The selected action kind cannot be changed in this response. A different kind
is a schema failure and immediately exhausts this selected strategy.

### Stage 5: deterministic recovery

Before changing models, ask a deterministic resolver whether there is exactly
one legal next action or whether a known transformation can repair the action.

Examples include:

- advancing a step after all predicated completion conditions are satisfied;
- constructing `emit_outputs` when all output values are already in durable
  state;
- choosing a required validation command when only one obligation remains;
- correcting a known repository-relative path from an authoritative lookup;
- applying an already validated deferred edit; and
- transforming a legacy action shape into its canonical schema.

Deterministic repair must be allowlisted by error code and transformation. It
must never invent file contents, acceptance decisions, or user intent.

### Stage 6: model fallback with clean context

When a backup model is available, send the clean-room or selected-action prompt
to that model. Do not send the accumulated repair conversation. The fallback
attempt must retain the rejected-strategy ledger so it cannot repeat a known
bad approach.

Provider transport fallback remains separate from semantic fallback. A model
must not be switched merely because of a transient timeout unless transport
retry policy is exhausted.

### Stage 7: human handoff or structured exhaustion

If the action space is ambiguous and all model strategies are exhausted,
request one concrete decision from the user. If interaction is unavailable,
persist a terminal repair report and stop.

The terminal result includes:

- boundary and objective;
- final material state;
- failures grouped by class;
- attempted prompt profiles and models;
- rejected strategy summaries;
- legal actions that remained available;
- why deterministic recovery was unavailable; and
- the exact decision or external fact needed to continue.

## Rejected-strategy ledger

Replace the single previous-action comparison with a boundary-scoped ledger.
Keep exact payload fingerprints for malformed response diagnosis, but use
semantic signatures for strategy exclusion.

Signatures should be layered:

- `payload_signature`: the canonical complete response;
- `action_signature`: action kind and all material parameters, excluding
  rationale, model hints, and incidental outputs;
- `target_signature`: action kind plus target resource, such as file path,
  tool operation, skill name, or destination step;
- `strategy_signature`: action kind, target, operation family, and relevant
  obligation or validation fingerprint.

Examples:

- Two edits to the same file and line range are the same target strategy when
  both fail for the same structural reason, even if replacement text differs.
- Two reads of the same nonexistent path are the same action strategy.
- Re-running the same validation command against an unchanged repository and
  unchanged issue fingerprint is the same strategy.
- Editing a different file or addressing a different validation obligation is
  materially different and remains allowed.

Each ledger entry records failure class, error code, material-state fingerprint,
validation fingerprint, stage, model, and attempt number. A strategy may become
eligible again only after relevant material state changes.

## Dynamic action-space narrowing

The response schema should represent what is legal now, not everything the
agent could theoretically do during the step.

Before each repair request, derive candidates by intersecting:

1. actions declared by the skill step or workflow task;
2. actions allowed by the step behavior;
3. actions allowed by the execution runtime;
4. actions whose deterministic preconditions are currently satisfiable;
5. actions not prohibited by an open validation obligation; and
6. strategies not rejected for the current material state.

If the intersection is empty, do not call the LLM. Produce a contract failure
that identifies which layers eliminated the action space. This is also a static
analysis target: definitions whose action space is necessarily empty should
fail repository validation.

## Prompt profiles

Prompt generation should be explicit and testable. Add named profiles:

- `normal_full_context`
- `targeted_schema_correction`
- `clean_room_replan`
- `constrained_action_selection`
- `constrained_action_parameters`
- `human_recovery_question`

Each profile has a fixed content policy. Tests should assert both required and
forbidden fields. In particular, `clean_room_replan` must prove that it excludes
the original transcript, raw prior assistant response, and unrelated execution
events.

Prompt profiles should be rendered through strategy methods so governed,
predicated, nested-skill, and durable-task differences stay behind their
existing abstractions. The coordinator chooses a profile; it does not assemble
messages.

## Material prompt difference enforcement

Do not define a different prompt as merely a different serialized string. A
timestamp, appended warning, reordered key, or changed rationale would produce
a new hash without changing the problem presented to the model. Distinctness
must be enforced at the prompt-construction boundary and audited across several
material dimensions.

### Construction isolation

Give normal, targeted, clean-room, action-selection, and parameter-generation
prompts separate builder functions. In particular, the clean-room builder must
not accept the original `messages` or transcript as arguments. Its input type is
only `RepairContext`, `RepairFailure`, the rejected-strategy ledger, and the
current narrowed action schemas.

This makes accidental reuse of the original conversation impossible without an
explicit code change. Do not implement clean-room recovery by copying messages
and deleting selected entries; construct it from structured state.

### Prompt manifest

Every builder returns messages together with a machine-readable manifest:

```python
@dataclass(frozen=True)
class RepairPromptManifest:
    profile: str
    source_sections: tuple[str, ...]
    history_policy: str
    event_categories: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    response_schema_fingerprint: str
    reasoning_mode: str
    model: str
    message_fingerprint: str
    structural_fingerprint: str
    estimated_tokens: int
```

`source_sections` identifies semantic inputs such as `objective`,
`material_state`, `open_obligations`, and `rejected_strategies`; it is not a
list of arbitrary text blocks. `history_policy` is an enum such as `full`,
`bounded`, or `none`. `reasoning_mode` distinguishes direct action generation,
action selection, and parameter filling.

The structural fingerprint is computed from profile, source sections, history
policy, allowed actions, response-schema fingerprint, reasoning mode, and model.
The message fingerprint is a canonical hash of the final serialized messages.
Both are stored on every attempt.

### Runtime material-difference predicate

Before sending a semantic repair request, the coordinator compares its manifest
with the previous semantic attempt. Exact message fingerprint equality is
always forbidden. In addition, the repair is materially different only when:

1. the prompt profile changed; and
2. at least one material dimension changed:
   - history policy became more restrictive;
   - source sections were removed or replaced by structured summaries;
   - allowed actions became a strict subset;
   - the response schema was narrowed;
   - reasoning mode changed from direct generation to selection or parameter
     filling;
   - rejected strategies were added;
   - relevant material state changed; or
   - the model/provider changed.

For the mandatory clean-room transition, enforce stronger rules regardless of
the general predicate:

- profile is `clean_room_replan`;
- history policy is `none`;
- there are exactly system and user messages;
- there are no prior assistant messages;
- the raw rejected payload is absent;
- transcript and raw tool-result sections are absent;
- only allowlisted structured source sections are present; and
- the prompt is below a configurable fraction of the original prompt's token
  estimate, initially 40 percent unless the required deterministic state alone
  exceeds that bound.

If the prompt audit fails, do not call the LLM. Raise a programmer-invariant
error containing both manifests and the failed material-difference rule.

### Provenance instead of text heuristics

The primary guarantee comes from typed inputs and manifests, not lexical
similarity. Text-overlap measures are fragile because necessary objectives and
schemas will legitimately appear in both prompts.

As a secondary diagnostic, compute token-shingle overlap between normal and
clean-room prompts and report unexpectedly high overlap. Use it to detect prompt
regressions and review changes, but do not make it the sole production gate.

### Leakage tests

Prompt tests should insert unique canary strings into every prohibited source:

- transcript canary;
- previous assistant-response canary;
- rejected-payload canary;
- unrelated tool-output canary; and
- superseded correction canary.

Build the clean-room prompt and assert that no canary appears. Also assert that
required objective, material-state, obligation, and rejected-strategy canaries
do appear. These tests prove source isolation more reliably than snapshot review
alone.

Property-based tests should generate arbitrary histories and payloads, then
verify that clean-room construction cannot leak them. Mutation tests should
deliberately add the original messages back to the builder and confirm that the
runtime audit and canary tests fail.

The resulting guarantee is concrete: the second repair is not merely reworded.
It is built by a different function from different typed inputs, has a different
reasoning contract and schema, excludes conversational history, and is rejected
before transmission if its manifest does not prove those differences.

## Integration with the shared runner

Refactor `WorkflowStepRunner.run` so every recoverable failure is passed to one
method:

```python
directive = repair_coordinator.on_failure(
    failure,
    context=strategy.repair_context(action),
)
```

The runner then applies the directive:

- request another action using a selected prompt profile;
- request action selection or action parameters;
- execute an approved deterministic repair;
- switch to a backup model;
- request human input; or
- persist exhaustion and return the configured exit code.

`record_response_error`, `record_action_error`, and `record_no_progress` remain
adapter hooks for persistence and display, but they no longer decide the next
repair prompt or retry policy.

`_complete_json_with_repair` should be reduced to transport and JSON-envelope
responsibilities. Semantic action repair belongs in the shared runner after a
payload has been received. This removes duplicated loops and prevents a payload
from being repeatedly repaired without the coordinator seeing the attempts.

## Chat adapter changes

The chat strategy should provide:

- current skill and step objective;
- step behavior and declared actions;
- response schemas by action kind;
- handoff and completion obligations;
- validation-gate fingerprints;
- checkpoint identity and material-state snapshot;
- compact facts from successful prior actions; and
- prompt renderers for each profile.

Checkpoint restoration remains chat-specific. Restoring the checkpoint must
not clear the rejected-strategy ledger. The ledger is cleared only when the
step index changes or relevant material state demonstrably changes.

`stalled_step_context` becomes a rendered view of the shared repair ledger
rather than independent repair state.

## Durable-task adapter changes

Replace the free-form `response_correction` field with shared typed repair
state. `_build_task_messages` should accept a prompt profile and `RepairContext`
instead of a list containing one correction string.

The durable adapter should provide:

- task objective, details, input state, and output-state contract;
- ready upstream state and deterministic task state;
- allowed runtime actions;
- task tool and nested-skill contracts;
- recent successful facts rather than the entire event stream; and
- a durable location for repair-attempt events.

Repeated no-progress currently stops a durable task. After this change it first
uses the same clean-room and constrained-action ladder as chat execution. It
stops only after the shared policy is exhausted.

## Observer role

The observer remains advisory and failure-isolated. It should not maintain a
second retry policy.

Observer diagnoses may contribute:

- a proposed failure classification;
- a suggested action kind;
- a target to exclude; or
- a missing fact that requires human input.

The coordinator validates observer recommendations against the same action
space and rejected-strategy ledger. An observer recommendation never bypasses
action validation or deterministic exhaustion limits.

## Persistence and observability

Add a `repair_attempt` event for every semantic attempt:

```json
{
  "kind": "repair_attempt",
  "boundary_id": "step-or-task-id",
  "attempt": 2,
  "stage": "clean_room",
  "failure_class": "action_precondition",
  "error_code": "file_not_found",
  "prompt_profile": "clean_room_replan",
  "model": "provider/model",
  "allowed_actions": ["gather_context", "read_document"],
  "rejected_strategy_signatures": ["..."],
  "material_state_fingerprint": "...",
  "outcome": "invalid_action"
}
```

Record prompt and response fingerprints by default. Record full messages only
under the existing verbose/exchange logging controls. Error reports and harness
reports should aggregate:

- repair success rate by stage and failure class;
- repeated-strategy prevention count;
- deterministic recovery count;
- model-fallback count;
- human-handoff count;
- average semantic attempts before recovery; and
- terminal exhaustion count.

## Configuration

Expose one policy object rather than unrelated retry integers:

```python
@dataclass(frozen=True)
class RepairPolicy:
    targeted_attempts: int = 1
    clean_room_attempts: int = 1
    action_selection_attempts: int = 1
    parameter_attempts: int = 1
    deterministic_recovery: bool = True
    model_fallback_attempts: int = 1
    allow_human_handoff: bool = True
```

Transport retry count and backoff remain separate. CLI compatibility flags can
populate the policy during migration, but new code should consume only
`RepairPolicy`.

Production defaults should keep total semantic LLM repair calls bounded. A
typical failed action should recover in one targeted or clean-room request; the
full ladder is reserved for genuine stalls.

## Static validation additions

Extend workflow-definition validation to reject repair configurations that
cannot work:

- a step has no legal action after step-type and runtime restrictions;
- a required output has no action capable of publishing it;
- a predicated step requires outputs but cannot emit outputs;
- an action-selection schema would contain no action names;
- a deterministic fallback is configured for an unsupported error code;
- a step forbids human interaction but has no terminal behavior after repair
  exhaustion; or
- a configured model fallback resolves back to the same provider/model pair.

Add prompt snapshot support for every repair profile so definition changes can
be reviewed against normal and clean-room prompts.

## Test strategy

### Unit tests

Test the coordinator as a pure state machine:

- each failure class selects the expected first stage;
- duplicate attempt identities are rejected;
- action spaces only narrow during the same material state;
- material progress resets the boundary ledger;
- cosmetic response changes produce the same semantic signature;
- relevant state changes permit a previously rejected target;
- transport retries do not consume semantic attempts;
- exhaustion always terminates; and
- policy limits cannot create an infinite loop.

Test prompt profiles:

- targeted repair includes the rejected payload and schema fragment;
- clean-room repair excludes transcript and raw prior responses;
- clean-room repair includes all open obligations and rejected strategies;
- action selection exposes only an enum of legal action names;
- parameter generation exposes only one action schema; and
- all repair prompts fit configured context budgets.

### Shared-runner tests

Use scripted clients and fake strategies to cover:

- malformed response, targeted repair, valid action;
- valid JSON but illegal action, clean-room recovery;
- repeated semantic action with changed rationale, strategy rejection;
- clean-room failure followed by two-pass success;
- unique legal action resolved deterministically without an LLM call;
- primary-model exhaustion followed by clean-context fallback success;
- complete ladder exhaustion and structured failure; and
- material progress clearing the ledger for the next boundary.

### Adapter tests

Run every shared scenario through both chat and durable-task adapters. Assert
that they select the same repair stages and differ only in rendered context and
persistence.

Chat-specific cases:

- checkpoint restoration preserves rejected strategies;
- nested skill boundaries receive independent repair ledgers;
- validation-gate regression excludes the ineffective repair strategy; and
- step transition clears repair state.

Durable-task cases:

- repair state survives event persistence and process restart;
- task output contracts appear in clean-room prompts;
- no-progress uses escalation before stopping; and
- human-unavailable exhaustion persists an actionable terminal report.

### Scenario and harness tests

Create deterministic scenario fixtures for common production failures:

- repeated nonexistent file read;
- malformed edit parameters;
- editing the wrong file repeatedly;
- validation command repeated against unchanged state;
- premature `next_step` with missing outputs;
- invalid nested-skill invocation;
- model repeats an exact malformed response; and
- model returns different payloads representing the same failed strategy.

Add assertions for the trajectory, not only final success: expected stages,
maximum repair calls, prompt-profile transitions, and absence of duplicate
attempt identities.

Real-provider evaluation should replay captured failures and measure whether
the clean-room and two-pass profiles improve recovery over the current appended
correction. It remains opt-in and is not authoritative CI.

## Implementation sequence

### PR 1: typed failure and repair state

- Add failure classes, repair policy, repair context, directives, attempt
  identities, and ledger types in `workflow_llm.py`.
- Migrate existing response/action/no-progress observations into typed failures.
- Preserve current behavior behind a compatibility coordinator.
- Add pure state-machine and signature tests.

Exit criterion: all current behavior passes through the coordinator and every
failure has a structured classification, without changing production retry
behavior.

### PR 2: prompt profiles and clean-room repair

- Add strategy hooks for repair context and prompt rendering.
- Implement targeted and clean-room profiles for chat and durable tasks.
- Replace repeated appended corrections after the first targeted attempt.
- Add required/forbidden prompt snapshot assertions.

Exit criterion: a second semantic failure always receives a clean-room prompt
whose fingerprint differs materially and which excludes prior transcript noise.

### PR 3: semantic strategy ledger and dynamic schemas

- Add layered action, target, and strategy signatures.
- Persist rejected strategies by boundary and material-state fingerprint.
- Derive the current legal action set.
- Generate narrowed response schemas from that set.

Exit criterion: semantically equivalent failed actions are rejected before
execution, and an empty legal action set fails without another LLM call.

### PR 4: two-pass recovery

- Implement constrained action selection.
- Implement action-specific parameter generation.
- Validate the selected action before requesting parameters.
- Add shared chat/task scripted scenarios.

Exit criterion: after clean-room failure, the model never receives the full
union action schema and cannot change action kinds during parameter generation.

### PR 5: deterministic recovery and fallback

- Add the allowlisted deterministic resolver.
- Route semantic model fallback through clean repair context.
- Add structured human handoff and terminal exhaustion artifacts.
- Separate transport and semantic retry metrics.

Exit criterion: every ladder terminates, deterministic actions bypass needless
LLM calls, and fallback models cannot repeat rejected strategies unknowingly.

### PR 6: static checks, replay evaluation, and rollout

- Add static repairability checks to repository-wide definition validation.
- Add repair prompt snapshots and captured-failure replay metrics.
- Run the new coordinator in shadow comparison against existing behavior.
- Make it authoritative after scenario and live replay thresholds pass.

Exit criterion: CI rejects structurally unrecoverable definitions, and live
replays demonstrate a lower terminal stall rate without increased unsafe action
execution.

## Rollout

1. Land shared types and event recording with no behavioral change.
2. Run the coordinator in shadow mode and compare its directives with current
   retries.
3. Enable targeted plus clean-room repair for deterministic scenarios.
4. Enable it for the feature runner using `deepinfra-cheap` and collect repair
   metrics.
5. Enable two-pass recovery and deterministic resolution.
6. Remove the duplicated JSON semantic-repair loops and adapter-specific retry
   decisions after parity tests pass.

Rollback is a policy switch back to compatibility behavior. Persisted repair
events must remain readable regardless of which policy is active.

## Acceptance criteria

The feature is complete when:

- chat and durable tasks use one shared repair coordinator;
- transport retry and semantic repair are separate mechanisms;
- no semantic repair attempt can repeat the same attempt identity;
- the second failed semantic attempt uses a clean-room prompt;
- clean-room prompts exclude transcript history and raw previous responses;
- rejected semantic strategies survive checkpoint restoration;
- action-selection and parameter-generation calls use narrowed schemas;
- deterministic recovery is used only for allowlisted, unambiguous actions;
- every repair sequence terminates within its configured budget;
- model fallback receives clean context plus the rejected-strategy ledger;
- terminal exhaustion produces a durable, actionable report;
- repository validation detects statically unrecoverable action contracts;
- deterministic tests cover every stage and transition for both adapters; and
- replay evaluation shows fewer terminal stalls than the current repair loop.

## Success metrics

Track these metrics before and after rollout:

- percentage of failed first actions recovered without human input;
- percentage recovered at each repair stage;
- repeated semantic strategies blocked before execution;
- median and maximum semantic repair calls per recovered failure;
- terminal stalls per 100 workflow boundaries;
- unsafe or contract-invalid actions executed after repair;
- context tokens used by normal versus repair prompts; and
- recovery success by model, provider, failure class, and definition.

The primary success condition is not that the model always succeeds. It is that
the execution system responds to failure predictably, presents genuinely
different recovery problems, prevents known-bad strategies from recurring, and
stops with useful evidence when recovery is impossible.
