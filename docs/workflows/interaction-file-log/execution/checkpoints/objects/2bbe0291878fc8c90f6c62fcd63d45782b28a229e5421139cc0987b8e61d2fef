# Workflow Agent Observer Plan

## Objective

Add an event-driven observer that watches skill and workflow execution, helps an
agent recover when it is struggling, and checks that work remains aligned with
the intent of the parent skill or workflow.

The observer must not shadow every agent roundtrip. Deterministic logic decides
when observation is useful, and only then invokes the configured high-reasoning
LLM. Healthy execution should add zero or very few observer calls.

## Design principles

- Use deterministic checks for repetition, failures, validation progress, and
  state transitions before considering an LLM call.
- Give the observer a compact summary rather than the working agent's complete
  prompt or conversation.
- Deduplicate observations using a material-state fingerprint.
- Keep deterministic validators and gates authoritative.
- Let the observer diagnose and constrain the next action, but never edit files
  or invoke tools itself.
- Apply the same observer contracts to skill chat and workflow-task execution.
- Reuse the existing `high_reasoning` provider and model lookup.
- Log every observer trigger, packet, decision, intervention, and outcome.
- Degrade gracefully when the observer model is unavailable.

## Phase 1: Observer infrastructure and visibility

### Goal

Establish the observer contract, trigger framework, state tracking, and logs
without allowing observer decisions to change execution.

### Shared observer model

Add shared types used by both skill execution and workflow-task execution.

```python
ObserverTriggerKind = Literal[
    "repeated_action",
    "repeated_failure",
    "semantic_stall",
    "repair_regression",
    "human_prompt",
    "step_transition",
    "completion",
    "pull_request_creation",
]
```

```python
@dataclass
class ObserverPacket:
    execution_mode: str
    trigger: ObserverTrigger
    root_intent: str
    skill_or_workflow: str
    current_step_id: str
    current_step_intent: str
    recent_actions: tuple[ObserverActionSummary, ...]
    recent_failures: tuple[ObserverFailureSummary, ...]
    changed_files: tuple[str, ...]
    validation_state: Mapping[str, object]
    handoff_state: Mapping[str, object]
    progress_state: ObserverProgressState
```

```python
@dataclass
class ObserverDecision:
    verdict: Literal[
        "continue",
        "coach",
        "redirect",
        "block_transition",
        "request_human",
    ]
    reason: str
    guidance: tuple[str, ...]
    expected_progress: str | None
    target_step_id: str | None
```

### Intent chain

Build an immutable intent chain for each execution:

```text
User request
  -> root skill or workflow intent
    -> nested skill intent
      -> current task or step intent
```

For workflow tasks, also include:

- Proposed PR intent.
- Workflow-template invariants.
- Current task description.
- Declared input and output state.
- Dependency state.

This gives the observer enough context to detect local actions that satisfy a
narrow step while contradicting the parent objective.

### Material-state fingerprint

Add a fingerprint that includes:

- Execution identity.
- Current skill or workflow.
- Current step.
- Changed-file hashes.
- Validation issue fingerprint.
- Outstanding obligations.
- Handoff state.
- Latest action failure signature.

Store observer execution state:

```python
@dataclass
class ObserverState:
    last_fingerprint: str | None
    last_observed_action_index: int | None
    last_decision: ObserverDecision | None
    intervention_pending: bool
    observation_epoch: int
```

If the fingerprint has not materially changed, do not call the observer again
unless a new high-priority trigger appears.

### Deterministic trigger detector

Calculate triggers without invoking an LLM:

- The same action signature repeats.
- The same failure signature repeats.
- Several actions produce no material state change.
- Validation issue count grows after a repair.
- The agent asks a human while repository context may still be available.
- The agent requests `next_step`, `complete`, or PR creation.

Phase 1 records which trigger would have fired but does not enforce the
observer's decision.

### Compact observer packet and prompt

The packet should contain:

- Observer role and strict scope.
- Intent chain.
- Current step contract.
- Trigger reason.
- Recent material actions and outcomes.
- Current errors and outstanding obligations.
- Changed files.
- Handoff state.
- Progress summary.
- Structured response schema and one complete valid JSON example.

Example packet:

```json
{
  "parent_intent": "Implement the selected proposed PR",
  "current_skill": "start-implementing-feature",
  "current_step": {
    "id": "validate-implementation-specifications",
    "intent": "All generated specifications must validate"
  },
  "recent_actions": [],
  "recent_failures": [],
  "validation_state": {},
  "changed_files": [],
  "handoff_state": {},
  "progress_summary": {
    "last_material_progress_roundtrip": 17,
    "repeated_action_count": 3
  }
}
```

Do not include the complete agent prompt, every prior roundtrip, or entire
specification bodies by default.

### Model selection

Use the existing `high_reasoning` provider lookup. Do not introduce a separate
provider configuration path for the observer.

### Shadow mode

Invoke the observer for a limited set of diagnostic triggers but do not enforce
its decisions. Record each observation in the repository-root workflow LLM
diagnostic log:

```json
{
  "execution_mode": "workflow_task",
  "skill_or_workflow": "execute-proposed-pr",
  "step_id": "repair-tests",
  "trigger": "repeated_failure",
  "fingerprint": "...",
  "observer_packet": {},
  "observer_decision": {},
  "action_index": 42
}
```

### Phase 1 acceptance criteria

- Skill chat and workflow-task agents use the same observer types.
- Trigger detection is deterministic.
- Observer packets are compact and testable.
- The observer uses the configured high-reasoning model.
- Identical fingerprints do not cause repeated calls.
- Observer failures never crash the working agent.
- Observer decisions are logged but do not affect execution.
- Tests cover prompt shape, trigger selection, fingerprints, deduplication,
  logging, and failure handling.

## Phase 2: Unsticking and corrective coaching

### Goal

Allow the observer to intervene when the agent is demonstrably struggling while
remaining out of healthy executions.

### Active triggers

Enable intervention for:

- Repeated actions.
- Repeated failures.
- Semantic stalls.
- Repair regressions.
- Repeated invalid tool calls.
- Repeated validation without reducing issues.

Healthy transitions and completion are not semantically gated in this phase.

### Material progress definition

Material progress includes:

- A changed file hash.
- A reduced validation issue fingerprint.
- A newly satisfied obligation.
- A new required context result.
- A valid handoff output.
- A successful deterministic command not previously successful.
- A step transition.

The following do not count as progress:

- Reworded reasoning.
- Repeating `next_step`.
- Re-running the same failing action.
- Re-reading identical context.
- Changing an action without changing its result.
- Growing or reshuffling equivalent errors.

### Coaching injection

For a `coach` decision, add a temporary section to the next working-agent
prompt:

```text
Observer intervention

Reason:
The previous three YAML edits produced the same validation errors.

Required change in approach:
- Re-read the current validator output.
- Do not repeat the previous YAML operation.
- Gather context for the unknown identifier.
- Apply all related corrections together.

Evidence expected before continuing:
The next validation result must have fewer issue fingerprints.
```

The intervention must:

- Be scoped to the current observation epoch.
- Remain present until expected progress occurs.
- Disappear after progress is demonstrated.
- Never accumulate indefinitely across unrelated steps.

### Redirect behavior

For a `redirect` decision:

- Validate that `target_step_id` exists.
- Validate that the target is permitted by the skill or workflow.
- Have the driver perform the transition deterministically.
- Record the redirect as an execution event.
- Restore the appropriate checkpoint when existing `goto_step` behavior
  requires it.

Typical redirects include returning to generation after an invalid document,
returning to context gathering for missing identifiers, and returning to repair
after validation still fails. The observer cannot invent arbitrary step IDs.

### Anti-loop behavior

After coaching:

- Reject an unchanged retry of the exact failed action.
- Include the previous action signature in the next prompt.
- Do not call the observer again until material state changes or a distinct
  failure appears.

If coaching fails, the next observer packet includes the prior decision, the
agent action taken afterward, whether expected progress occurred, and the new
failure information. This lets the observer escalate from `coach` to
`redirect`.

### Human-prompt review

Before allowing a struggling agent to ask a human, determine whether the
missing information appears discoverable. The observer may coach the agent to
use a declared context or document tool, redirect to a context-gathering step,
or allow `request_human` when the missing item is genuinely a human decision.
The observer must not invent the missing answer.

### Call-frequency controls

- Do not call the observer during healthy deterministic actions.
- Use a default cooldown of four material actions after an observation.
- Do not observe the same fingerprint twice.
- Allow a new failure class or repeated failure to bypass the cooldown.
- Do not impose a fixed lifetime observer-call limit.
- Let material progress naturally suppress observation.

### Phase 2 acceptance criteria

- Healthy workflows add zero or very few observer calls.
- Repeated failures trigger one observer call per material state.
- Observer guidance appears in the next working-agent prompt.
- The exact failed action cannot be repeated unchanged.
- Successful progress clears the intervention.
- Redirects can target only declared steps.
- Human prompts are intercepted only when deterministic evidence suggests more
  discovery is possible.
- Observer outages fall back to existing stall handling.
- Tests cover stuck YAML repair, repeated context gathering, invalid tools,
  failed coaching, successful coaching, human prompts, and redirects.

## Phase 3: Intent and transition enforcement

### Goal

Ensure the final result remains aligned with the intent of the parent skill,
workflow, proposed PR, and current task.

### Consequential transition review

Invoke the observer before:

- Completing the root skill.
- Completing a workflow.
- Declaring a workflow blocked.
- Creating a pull request.
- Requesting human intervention that pauses execution.
- Leaving a major validation or review stage after corrective work.

Do not invoke it for every ordinary `next_step`.

### Transition packet

Use an outcome-focused packet:

```json
{
  "transition": "complete",
  "root_intent": "...",
  "current_step_contract": "...",
  "declared_outputs": {},
  "handoff_records": {},
  "changed_files": [],
  "validation_results": [],
  "outstanding_obligations": [],
  "created_pull_requests": [],
  "known_inconsistencies": []
}
```

Example observer response:

```json
{
  "verdict": "block_transition",
  "reason": "The workflow claims completion but no proposed PR specification was added.",
  "guidance": [
    "Return to the proposed PR generation step.",
    "Generate the missing specification.",
    "Rerun branch evaluation."
  ],
  "expected_progress": "A validated proposed PR specification exists in the changed files.",
  "target_step_id": "generate-proposed-pr-specifications"
}
```

### Authority boundaries

The observer may block a transition only for semantic inconsistencies supported
by the packet. It cannot:

- Override a passing deterministic validator with invented requirements.
- Ignore a failing deterministic validator.
- Add unrelated scope.
- Demand stylistic preferences not declared by the skill.
- Execute tools or edit files.
- Redirect to an undeclared step.

Deterministic gates remain authoritative. Observer review complements them
where intent cannot be fully expressed through schemas and validators.

### Completion behavior

A `continue` verdict allows completion.

A `coach` or `block_transition` verdict rejects the transition, adds guidance
to the next prompt, records the intervention, and requires evidence of progress
before another completion attempt.

A `redirect` verdict moves to a valid corrective step.

The same completion fingerprint cannot cause another observer call. If the
agent retries completion without changing anything, reuse the prior observer
decision deterministically.

### Pull-request creation behavior

Before PR creation, check whether:

- Changed files match the declared work.
- Required generated artifacts exist.
- Validation obligations are complete.
- The PR is non-empty.
- Unrelated files were accidentally included.
- The result aligns with the proposed PR intent.

Run deterministic branch evaluation and PR-file validators first, then provide
their results as evidence to the observer.

### Workflow-task alignment

For every workflow task, compare:

```text
proposed PR intent
  <-> workflow-template invariants
  <-> task description
  <-> changed files
  <-> task output state
```

This catches tasks that are mechanically complete but operated on the wrong
artifact or omitted a required deliverable.

### Parent-skill alignment

For nested skills, provide the full intent chain. A nested skill cannot declare
success merely because it completed its narrow action when the parent step
requires resulting issues to be corrected and revalidated.

### Phase 3 acceptance criteria

- Root completion is reviewed once per material completion state.
- PR creation cannot proceed after a concrete observer-identified intent
  mismatch.
- Completion retries without state changes reuse the prior decision without
  another LLM call.
- Observer blocks contain actionable guidance and expected evidence.
- Redirects remain constrained to declared steps.
- Existing deterministic gates retain priority.
- Healthy runs incur only one consequential observation near completion or PR
  creation, with duplicate transitions sharing the same observation.
- Tests cover missing artifacts, wrong-file changes, incomplete repair,
  unrelated changes, nested-skill mismatch, valid completion, and observer
  service failure.

## Delivery

Implement the design in three pull requests:

1. Observer contracts, fingerprints, deterministic triggers, high-reasoning
   calls, shadow-mode decisions, and logs.
2. Active stall coaching, redirects, intervention lifecycle, anti-loop
   behavior, and human-prompt review.
3. Completion, PR creation, workflow-task, and parent-intent transition
   enforcement.

This sequence makes observer behavior visible before it becomes authoritative,
adds immediate unsticking value in the second phase, and reserves semantic
transition enforcement for the final phase.
