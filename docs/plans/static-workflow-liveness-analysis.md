# Static Workflow Liveness Analysis Plan

## Objective

Detect workflow definitions that can deadlock, livelock, or repeatedly perform
work without making material progress before an LLM, tool, repository mutation,
or live workflow is involved.

The analyzer should prove progress properties from checked-in definitions, the
execution kernel's transition rules, and declarative tool-effect contracts. It
should report the exact step and a concrete repair through the existing
`validate-workflow-definitions` command and normal CI.

This is a focused extension of the static analyzer described in
`docs/plans/workflow-definition-iteration.md`. That plan covers definition
quality, prompt snapshots, replay, and scenarios. This plan specifies the
missing static liveness model.

## Motivating failure

The former `stage-validated-artifacts` step in
`start-implementing-feature.yaml` asked the LLM to invoke one fixed `git add`
operation. After success, the step remained active and the same operation
remained available. Verification belonged to the following step, while
advancement depended on the LLM choosing `next_step`.

`git add` is idempotent once the requested paths are staged. Repeating it can
succeed without changing repository or workflow state:

```text
stage step entered
  -> git add succeeds and stages paths
  -> stage step remains active
  -> git add succeeds with no state change
  -> stage step remains active
  -> ...
```

The transition was possible, so this was a livelock hazard rather than an
unavoidable deadlock. The runtime stall detector eventually stopped it, but
only after executing the skill and spending LLM round trips.

Static analysis should reject the definition with a diagnostic such as:

```text
idempotent_action_without_auto_advance:
git add is deterministic and idempotent, but this governed step still relies
on an LLM transition after success. Convert it to an invoke_tool pre-step or
declare a machine-owned success transition.
```

## Terminology

- A **deadlock** is a reachable non-terminal state with no valid outgoing
  transition.
- A **livelock** is a reachable cycle whose transitions can succeed but cannot
  produce new material state after the first traversal.
- A **liveness hazard** is a reachable non-progress cycle with an exit that
  depends on an LLM choosing a transition over a repeatable action.
- **Material progress** is a durable change to control flow, repository state,
  validation state, handoff state, human input, or required outputs.
- An **abstract effect** declares what an action may read, mutate, produce, or
  invalidate. Static analysis never performs the effect.
- A **machine-owned transition** is taken automatically by the execution kernel
  after a structured condition is satisfied.

The analyzer should distinguish proven errors from uncertainty:

- Proven deadlocks and non-progress cycles are errors.
- Mechanical LLM-owned actions with a voluntary exit are errors when their
  deterministic effect can be runner-owned.
- Unknown shell effects and ambiguous prose produce warnings.

## Scope

The analyzer covers:

- Skills under `skill-definitions/`.
- Workflow templates under `templates/`.
- Instantiated workflow tasks that use the typed step contract.
- Governed, coding-loop, invoke-tool, gate, predicated, and nested-skill steps.
- Declared actions, tool invocations, pre-steps, completion guards, gate
  redirects, outputs, handoffs, and explicit step overrides.
- Built-in Git, GitHub, repository, edit, context, and validation capabilities.

The first version does not predict arbitrary LLM behavior, execute commands,
inspect command output, infer precise effects for unrestricted shell scripts,
or treat natural-language interpretation as authoritative.

## Design principles

- Analyze production contracts rather than a second approximation of them.
- Keep action authority, transition guards, and completion conditions
  structured instead of encoding them only in prose.
- Model unknown behavior conservatively and expose the source of uncertainty.
- Separate safety, liveness, and prompt-quality diagnostics.
- Use stable diagnostic codes with exact paths and concrete remediation.
- Require deterministic mechanical operations to be runner-owned.
- Run without provider credentials, network access, temporary worktrees, or
  repository mutation.

## Existing foundation

`workflow_definition_analysis.py` is the integration point. It already:

- Parses and schema-validates definitions.
- Compiles skills into `WorkflowIR` control-flow graphs.
- Finds unreachable steps.
- Performs definite-assignment analysis for handoffs.
- Validates placeholders and embedded action examples.
- Renders production prompt snapshots.

The liveness analyzer should extend `WorkflowIR` and reuse production behavior
from `workflow_step_behavior.py`. It must not duplicate rules for whether a step
invokes an LLM, auto-advances, runs a gate, or publishes predicated outputs.

## Declarative execution model

### Step control contract

Compile each step into a normalized contract:

```python
@dataclass(frozen=True)
class StepControlContract:
    index: int
    step_id: str
    owner: Literal["runner", "llm"]
    actions: tuple[AbstractAction, ...]
    entry_requirements: tuple[Condition, ...]
    completion_requirements: tuple[Condition, ...]
    success_transition: Transition
    failure_transitions: tuple[Transition, ...]
    successors: tuple[int, ...]
```

Ownership derives from existing behavior:

- `invoke_tool`, `gate`, and `uses_skill` are runner-owned.
- `governed` and `coding_loop` are LLM-owned.
- `predicated` is LLM-owned until its structured completion condition is
  satisfied, after which advancement is runner-owned.

`next_step` must be represented as a real transition even though the kernel
makes it universally available for most LLM steps.

### Capability effect contract

Every bounded capability declares static metadata beside its runtime
implementation:

```python
@dataclass(frozen=True)
class CapabilityEffect:
    operation: str
    determinism: Literal["deterministic", "conditional", "unknown"]
    idempotence: Literal["idempotent", "non_idempotent", "unknown"]
    reads: frozenset[StateDomain]
    writes: frozenset[StateDomain]
    produces: frozenset[str]
    invalidates: frozenset[str]
    success_postconditions: tuple[Condition, ...]
```

Initial state domains include control flow, worktree, index, history, remote
repository, files, outputs, handoffs, validation, discovered context, and human
input.

Initial built-in summaries:

| Capability | Reads | Writes | Deterministic | Idempotent after success |
| --- | --- | --- | --- | --- |
| `git status` | worktree, index | output | yes | yes |
| `git add` | files | index | yes | yes |
| `git move` | files, index | files, index | yes | conditional |
| `git commit` | index, history | history | conditional | no |
| `repository-state` | worktree, index, history | output | yes | yes |
| `read_document` | files | context | yes | yes |
| `gather_context` | specifications | context | yes | yes |
| `edit`, `yaml_edit` | files | files | conditional | unknown |
| validation command | files | validation | conditional | per fingerprint |
| `prompt_user` | none | human input | conditional | no |

The runtime intrinsic registry remains authoritative for operation names.
Validation fails when a referenced intrinsic operation has no effect summary.
An unrestricted shell command has an unknown effect unless it matches a
checked-in tool declaration carrying explicit metadata.

### Abstract state

Track only facts that affect progress:

```python
@dataclass(frozen=True)
class AbstractWorkflowState:
    step_index: int
    successful_actions: frozenset[ActionId]
    satisfied_conditions: frozenset[ConditionId]
    available_outputs: frozenset[str]
    changed_domains: frozenset[StateDomain]
    validation_epoch: int
    bounded_iterations: tuple[tuple[str, int], ...]
```

States that differ only in prose, rationale, or repeated reads are equivalent.
This keeps the graph finite and aligns static progress with the runtime's
material-state fingerprint.

### Abstract transitions

Each action produces outcome classes rather than real results:

- `success_with_progress`
- `success_without_progress`
- `correctable_failure`
- `terminal_failure`
- `transition`

An idempotent action normally has both success outcomes: its first success may
change a domain, while repetition reaches `success_without_progress`. Gate
outcomes produce success and retry edges. Predicated completion produces an
automatic edge only when required outputs are definitely available. Coding
loops use their iteration bound to keep the graph finite.

## Analysis algorithm

### 1. Compile control flow

Extend `_compile_skill` to normalize sequential advancement,
`next_step_override`, gate success and repair targets, predicated advancement,
declared `goto_step` targets, nested-skill calls and returns, and terminal
completion.

### 2. Build the abstract execution graph

Start at the first step with declared initial inputs. Apply abstract actions
until a fixed point is reached. Canonicalize equivalent states so idempotent
repeats become self-edges instead of an infinite graph.

Unknown shell effects branch conservatively into progress and no-progress
outcomes. They must not hide a proven deadlock on another branch.

### 3. Detect terminally stuck states

Report `terminal_state_without_completion` when a reachable state is not a
successful terminal, has no valid outgoing transition, and cannot satisfy its
missing requirement through an available action effect.

### 4. Detect non-progress cycles

Run strongly connected component analysis over reachable abstract states. A
cyclic component makes progress only if an edge can advance control flow,
produce a new required output, change a domain observed by a guard, consume new
human input, or reach a terminal state.

Report `non_progress_cycle` if no edge can do so. If an exit exists but the
internal actions are deterministic and repeatable while the exit requires an
unguarded LLM choice, report `idempotent_action_without_auto_advance`.

### 5. Prove retry usefulness

For each gate failure or backward transition, compare domains observed by the
failed condition with domains writable on the retry path. If they do not
intersect, report `retry_without_relevant_effect`.

### 6. Check completion observability

Every machine-owned completion condition must be backed by an action result,
output, or state domain recorded by the runner. Every model-owned mechanical
step must publish a structured result, auto-advance after success, or delegate
the operation to a runner-owned step.

### 7. Compare rendered prompts with structural authority

After structural analysis, check prompt snapshots for secondary contradictions:

- Prose requires an action excluded by the step contract.
- Prose says to repeat a deterministic pre-step.
- Prose forbids advancement while no other action can make progress.
- An embedded example cannot satisfy a declared guard.

These are warnings unless the structural model independently proves the
contradiction. Authoritative requirements should migrate out of prose rather
than expanding a heuristic natural-language rules engine.

## Initial diagnostics

### `model_owned_deterministic_action`

A governed step exists only to run one fixed deterministic operation whose
parameters require no LLM judgment. Convert it to an invoke-tool pre-step.

### `idempotent_action_without_auto_advance`

An LLM-owned step directs an idempotent action, repetition can leave abstract
state unchanged, and advancement remains an LLM choice. Make the action
runner-owned and advance after success.

### `required_action_after_satisfied_postcondition`

An action remains mandatory when all its success postconditions are already
satisfied. Guard the action with the unsatisfied condition or advance.

### `unobservable_completion`

Completion depends on state no declared action records and no guard inspects.
Publish the result or add a deterministic observing step.

### `forbidden_verification_action`

A structured example or recognized instruction names an action excluded by the
effective contract. Declare it or move verification to a deterministic step.

### `retry_without_relevant_effect`

Every retry path is unable to modify a domain observed by the failed gate.
Redirect to a corrective step with a relevant write effect.

### `non_progress_cycle`

A reachable cycle has no edge capable of material progress or exit. Add a
bounded exit, relevant corrective action, or terminal blocked outcome.

### `runner_result_not_consumed`

A deterministic pre-step result is consumed by no output, condition, gate, or
successor. Wire it into the contract or remove the operation.

### `unbounded_coding_loop`

A coding loop lacks a positive iteration bound or a stopping condition tied to
a verification result. Add both structurally.

## Nested skills and templates

Compute an interprocedural summary for each nested skill:

- Required inputs and definitely produced outputs.
- State domains it may read and write.
- Whether it may prompt, return, block, or fail.
- Whether every internal path can return.

Analyze the skill call graph for recursion and reject recursive components
without an explicit bound or decreasing structured measure.

Analyze workflow templates both symbolically with declared placeholders and
after instantiation with resolved operations, dependencies, and handoffs. Do
not infer file-existence preconditions from path-shaped strings; creation
workflows legitimately reference paths that do not exist yet.

## CLI and result format

Extend the existing command:

```bash
powdrr-lift validate-workflow-definitions \
  skill-definitions templates \
  --liveness \
  --json
```

Once stable, enable liveness analysis by default. A diagnostic should include a
stable code, severity, definition and step path, shortest entry path, smallest
cycle, abstract state, message, and remediation:

```json
{
  "code": "idempotent_action_without_auto_advance",
  "severity": "error",
  "definition": "skill-definitions/start-implementing-feature.yaml",
  "path": "steps[stage-validated-artifacts]",
  "state": {
    "step": "stage-validated-artifacts",
    "successful_actions": ["git:add"],
    "changed_domains": ["repository_index"]
  },
  "cycle": ["git:add:success_without_progress"],
  "message": "A repeatable idempotent action can leave this LLM-owned step active.",
  "remediation": "Convert it to an invoke_tool pre-step with automatic advancement."
}
```

## Implementation phases

### Phase 1: Effect registry and normalized contracts

- Add capability-effect and state-domain types.
- Summarize bounded built-in operations.
- Compile `SkillStep` and `StepBehavior` into normalized control contracts.
- Fail when a referenced intrinsic operation lacks effect metadata.

Acceptance: compilation executes no tool, checked-in definitions have stable
contract snapshots, and a new intrinsic without metadata fails validation.

### Phase 2: Intra-skill liveness graph

- Add abstract state and transition expansion.
- Canonicalize states to a fixed point.
- Detect terminal sinks and non-progress strongly connected components.
- Diagnose deterministic ownership, idempotent loops, completion observability,
  and ineffective retries.

Acceptance: the historical governed `git add` fixture fails, its deterministic
replacement passes, and fixtures cover a hard deadlock, pure read loop,
ineffective retry, and valid bounded coding loop.

### Phase 3: Nested skills and workflow templates

- Add skill effect and return summaries.
- Validate the skill call graph.
- Analyze symbolic templates and instantiated tasks.
- Diagnose recursion and cross-skill handoff failures.

Acceptance: missing outputs remain visible across calls, unbounded recursion
fails, and a valid repair skill proves it can change its gate inputs.

### Phase 4: Prompt warnings and CI enforcement

- Compare structural authority with rendered prompts.
- Run repository-wide in warning mode.
- Add narrow suppressions with owner, reason, and expiration.
- Promote proven liveness errors to required CI checks.

Acceptance: prompts cannot silently prescribe unavailable actions, broad or
expired suppressions fail, and proven liveness errors block merges.

## Test strategy

Unit tests cover effect lookup, abstract-state canonicalization, outcome
generation, strongly connected components, retry read/write intersections,
completion observability, and shortest diagnostic paths.

Golden fixtures cover:

- The historical governed `git add` loop.
- Deterministic staging with automatic advancement.
- Repeated document reads with no exit.
- Ineffective and effective gate retries.
- A required output with no producer.
- Predicated automatic advancement.
- Bounded coding loops.
- Unbounded nested-skill recursion.
- Unknown shell effects alongside an independently proven error.

Repository tests run the analyzer against every checked-in definition, snapshot
normalized contracts, verify both CLI formats, and prove analysis performs no
subprocess, network, Git, file-write, or LLM operation.

## CI rollout

1. Report all diagnostics without failing CI.
2. Fail new or worsened errors against an explicit baseline.
3. Repair checked-in definitions, remove the baseline, and fail all proven
   liveness errors.

Warnings remain advisory until their false-positive rate is understood. Every
production non-progress failure should add a minimal static regression fixture
when the abstract model can express it.

## Risks and mitigations

### Unknown shell effects

Do not assume they make progress. Report uncertainty and require critical steps
to use bounded capabilities or checked-in effect metadata.

### Intentional LLM iteration

Require an explicit coding-loop type, bound, stopping condition, and relevant
effect. Do not infer intentional iteration from prose.

### State explosion

Track domains and predicates rather than values, canonicalize idempotent states,
summarize nested skills, and stop after the shortest diagnostic proof.

### Runtime drift

Build transitions from production `StepBehavior` and capability registries. Add
conformance tests comparing static summaries with runtime decisions for parsed
synthetic actions without executing tools.

### Prose remains authoritative

Treat prose-only requirements as warnings and migrate them into structured
conditions. Do not build an expanding natural-language parser.

## Suggested pull request sequence

1. Capability-effect registry and contract snapshots.
2. Abstract graph, deadlock detection, and shortest-path diagnostics.
3. Idempotent livelock and ineffective-retry detection with the `git add`
   regression fixture.
4. Nested-skill summaries, recursion checks, and template analysis.
5. Prompt warnings, repository baseline, and CI enforcement.

Each pull request should be independently useful and include deterministic
positive and negative fixtures.

## Definition of done

- `validate-workflow-definitions` detects proven deadlocks and non-progress
  cycles without running workflows.
- The historical `git add` pattern is rejected before an LLM runs.
- Deterministic runner-owned staging passes.
- Gate retries are proven capable of changing what they re-evaluate.
- Nested skills and templates use the same liveness model.
- Diagnostics identify the shortest path, cycle, exact location, and repair.
- Analysis performs no mutation, subprocess, network, or provider call.
- CI blocks new proven liveness errors.
