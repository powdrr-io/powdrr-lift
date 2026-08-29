# OpenCode-Inspired Agent Execution Proposal

## Decision requested

Adopt a typed phase controller around Powdrr's existing specifications, proposed
PRs, skills, and deterministic workflow tasks. The controller should make the
path from prompt to plan to code to validation explicit runtime state, rather
than relying on every skill to restate the path correctly.

This is Option B below. It preserves Powdrr's differentiator—structured,
validated intent—while adopting the strongest parts of OpenCode's execution
runtime: capability boundaries, explicit plan/build transitions, persisted tool
state, checkpoints, immediate diagnostic feedback, bounded context, and
specialized child agents.

The recommendation is not to reproduce OpenCode's interactive session runtime.
Powdrr should use the proposed PR as the durable statement of **what and why**,
derive an execution plan for **how and in what order**, and accumulate validation
evidence proving **what actually passed**.

## Scope and source snapshot

This proposal compares the current Powdrr implementation with OpenCode at commit
[`dc4449d`](https://github.com/anomalyco/opencode/tree/dc4449df0d52199704ea4989a5a993ebbc605612),
reviewed on 2026-08-29. The analysis used OpenCode's source rather than only its
product documentation, with particular attention to agents, plan mode, session
processing, tools, permissions, compaction, snapshots, and revert behavior.

Relevant Powdrr starting points are:

- [`templates/execute-proposed-pr.yaml`](../../templates/execute-proposed-pr.yaml),
  which already separates context gathering, execution planning, test creation,
  implementation, deterministic checks, scope review, and PR preparation.
- [`skill-definitions/start-implementing-feature.yaml`](../../skill-definitions/start-implementing-feature.yaml),
  which creates validated proposed PRs and their durable workflows.
- [`skill-definitions/run-tests-and-fix.yaml`](../../skill-definitions/run-tests-and-fix.yaml),
  which already models deterministic test execution, structured diagnosis,
  deferred edit validation, repair, and rerun.
- [`src/powdrr_lift/workflow_chat_agent.py`](../../src/powdrr_lift/workflow_chat_agent.py)
  and [`src/powdrr_lift/workflow_task_agent.py`](../../src/powdrr_lift/workflow_task_agent.py),
  which implement action prompting, validation, repair, handoffs, observer
  integration, and context compaction.
- The existing
  [prompt-reduction](workflow-llm-prompt-reduction.md),
  [observer](workflow-agent-observer.md), and
  [definition-iteration](workflow-definition-iteration.md) plans. This proposal
  extends those investments instead of creating parallel mechanisms.

## What OpenCode actually does

OpenCode's prompt-to-code reliability is a composition of runtime mechanisms.
No single prompt or tool is responsible for the result.

### Plan and build are capability modes

OpenCode defines primary agents, subagents, and hidden housekeeping agents as
runtime records with a model, prompt, permissions, mode, and optional step limit.
Its built-in Plan agent denies edits except for its plan file, while Build has
broad execution access. Explore is a read-only subagent and compaction, title,
and summary agents have no tool access. The implementation is in
[`agent.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/agent/agent.ts),
and the public documentation describes the same primary/subagent split and
permission-based restriction in [Agents](https://opencode.ai/docs/agents/).

This matters because “do not edit while planning” is not merely prose. Tool
availability and permission evaluation enforce it.

### Planning has an explicit transition

The Plan prompt uses a staged process: explore the repository, design an
approach, review critical files, write a plan with paths and end-to-end
verification, then request a transition to Build. The source is
[`plan-mode.txt`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/prompt/plan-mode.txt).

The transition itself is implemented by the
[`plan_exit` tool](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/tool/plan.ts).
After approval it creates a new user message assigned to the Build agent. That
means the approved transition changes runtime state and capabilities; it does
not depend on the same model remembering that planning has ended.

### The agent loop persists operational state

OpenCode's session loop resolves the current agent and tools for each turn,
processes pending subtasks and compaction tasks, enforces step limits, and keeps
running while tool calls remain. See
[`session/prompt.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/prompt.ts).

Each tool call is persisted through pending, running, completed, or error
states. The processor captures step snapshots and file patches, detects three
identical tool calls as a doom loop, retries eligible provider errors, and
triggers compaction on context overflow. See
[`session/processor.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/processor.ts).

The useful lesson is not the exact event schema. It is that execution state is
first-class and inspectable, so correction, UI reporting, replay, and recovery
do not need to infer what happened from prose.

### Tool access is resolved at the point of use

OpenCode merges agent and session permission rules, hides fully denied tools,
and evaluates allow, ask, or deny rules against tool-specific patterns. Rules
can restrict a tool by path or command, and the last matching rule wins. The
behavior is documented in [Permissions](https://opencode.ai/docs/permissions/)
and implemented in
[`permission/index.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/permission/index.ts).

Tools are dynamically resolved with the current agent and session context.
Every call passes through before/after plugin hooks and a shared permission
context. See
[`session/tools.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/tools.ts).
The public [Tools documentation](https://opencode.ai/docs/tools/) similarly
presents permissions as the tool control surface.

### Corrections are placed next to the failed action

The patch tool applies changes, notifies language servers, collects diagnostics,
and appends file-specific errors to the tool result so the next model turn sees
the correction immediately. See
[`tool/apply_patch.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/tool/apply_patch.ts).
OpenCode also exposes symbol and reference operations through an experimental
LSP tool. Its [LSP guidance](https://opencode.ai/docs/lsp/) correctly notes that
language-server diagnostics are useful but should not replace project lint and
type-check commands.

Malformed arguments, permission rejection, tool execution errors, provider
errors, and context overflow take different paths. A model-correctable tool
failure remains attached to the tool call rather than collapsing every failure
into a generic session error.

### Recovery and long-running context are built in

OpenCode captures snapshots around model steps, records patches, and can revert
at a message or part boundary while preserving a diff. See
[`session/revert.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/revert.ts).

Its compaction process prunes older tool output, protects selected high-value
tool results, creates a summary through a hidden no-tools agent, and can replay
the interrupted request after compaction. See
[`session/compaction.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/session/compaction.ts).
Large individual tool results are also bounded: OpenCode stores the full result
and returns a preview plus instructions to search or read a range from the saved
output. See
[`tool/truncate.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/tool/truncate.ts).

Subagents run in child sessions with derived permissions and can be resumed by
ID rather than restarted with a fresh context. See
[`tool/task.ts`](https://github.com/anomalyco/opencode/blob/dc4449df0d52199704ea4989a5a993ebbc605612/packages/opencode/src/tool/task.ts).

## Powdrr's stronger foundation

Powdrr should not treat OpenCode as the target architecture. Powdrr already has
several stronger abstractions for professional software delivery:

1. System, architecture, and implementation specifications preserve intent
   outside a transient conversation.
2. Proposed PRs divide a feature into reviewable changes and assign
   specification effects to an implementation boundary.
3. Workflow tasks have explicit input and output state, deterministic pre-steps,
   gates, and nested skills.
4. Step actions are validated against the current step, and prompt construction
   excludes prior-step instructions.
5. `run-tests-and-fix` validates a deferred repair before applying it and then
   reruns the authoritative suite.
6. Observer, replay, tuning, and error logging infrastructure already models
   semantic stalls and correction quality.

These are closer to a typed delivery process than OpenCode's plan Markdown and
todo list. The opportunity is to make Powdrr's runtime as explicit as its
artifacts.

## Current gaps

### The delivery phase is inferred, not owned

Powdrr knows the current skill step or workflow task, but it does not have one
top-level state representing Discover, Plan, Await Approval, Build, Validate,
Review, or Publish. Similar restrictions therefore appear in template details,
skill details, generic prompts, action validation, and repair prompts.

The current per-step `actions` contract is valuable and should remain. It is a
fine-grained capability declaration, not a replacement for a delivery phase.
The two should be intersected by the runtime:

```text
effective capabilities = phase capabilities ∩ current-step actions ∩ policy
```

This would make it impossible for a planning step to edit product code even if
its prose or generated response is wrong.

### The execution plan is a handoff, not a governed artifact

`execute-proposed-pr` already asks for criteria-to-test mappings, implementation
files, commands, and risks. That plan is passed as task output state, but it has
no dedicated schema, evaluator, approval status, version, or deviation policy.
Later work can drift from it without creating an explicit plan amendment.

### Validation evidence is distributed

Test results, format results, type checks, completeness review, and scope review
are separate handoffs. There is no single obligation ledger answering:

- Which proposed-PR acceptance criterion is covered?
- Which implementation change and test provide the evidence?
- Which exact command passed, against which tree state?
- Which obligations became stale after a later edit?

Passing tests is currently guarded from being mistaken for full completeness,
but the relationship is still reconstructed by a review step.

### Tool lifecycle and recovery differ by execution path

Interactive skill execution and durable workflow execution share useful driver
logic, but their event projections, supported actions, material-state checks,
and correction construction still differ. File edits are not uniformly wrapped
in a checkpoint with attached diagnostics. Recovery generally asks the model for
a different action; it cannot consistently restore the last known-good state
and retry from a typed boundary.

### Progressive discovery is mostly prompt-level

Powdrr conditionally includes action and catalog guidance, and its built-in
tools expose help. However, durable task prompts still advertise a broad tool
catalog, and generic action rules remain substantial. The engine validates
unsupported actions after generation instead of always omitting unavailable
action schemas and tools before generation.

## Options

| Option | Shape | Benefits | Costs and limitations |
|---|---|---|---|
| A. Definition retrofit | Add plan/evidence shapes to skills and `execute-proposed-pr`; strengthen prompts and gates; do not add a phase runtime. | Lowest implementation risk; quickly improves plan quality and traceability; uses current runners. | Continues to encode safety and correction behavior in prose; duplicated rules will drift; limited checkpoint/revert support; weaker foundation for other agent surfaces. |
| **B. Typed phase controller** | Add delivery phases, a validated execution-plan artifact, capability resolution, an evidence ledger, common tool lifecycle hooks, and checkpoints around the existing runners. | **Recommended.** Preserves current skills/workflows and structured specs while making transitions, safety, correction, and evidence engine-owned. Can ship incrementally and be evaluated in shadow mode. | Moderate runtime work; requires migrating skills to thinner declarations; temporary compatibility layer while both paths exist. |
| C. Event-sourced session runtime | Replace the current chat/task loops with a unified persistent session processor modeled after OpenCode, with messages, parts, child sessions, snapshots, and dynamic tools as the primary abstraction. | Maximum flexibility, resumability, UI observability, and parity with interactive coding agents. | Largest rewrite and migration risk; can subordinate Powdrr's specification/workflow model to a conversation model; delays direct improvements to proposed-PR execution. |

Option A is useful as an implementation spike but is not a stable endpoint.
Option C may become attractive if Powdrr needs to be a general interactive coding
client. It is unnecessary for making proposed-PR execution materially better.

## Recommended target design

### 1. Introduce a delivery phase controller

Add an engine-owned phase enum and transition record:

```text
discover -> plan -> awaiting_plan_approval -> build
         -> validate -> review -> publish -> complete
```

`recover` should be a reasoned transition to a prior phase, not a parallel happy
path. For example, validation can return to Build with a typed failed-obligation
packet, or Review can return to Plan when the implementation requires a scope
change.

Each transition should record:

- prior and next phase;
- initiating action and actor;
- artifact versions and tree fingerprint;
- satisfied and outstanding gates;
- reason, including whether the transition was automatic or human-approved.

The runtime should reject illegal transitions before an LLM call where possible.
Prompt text should explain the current phase, not enforce it.

### 2. Make the execution plan a typed derivative of the proposed PR

Create an `execution-plan-v1` artifact in the instantiated workflow directory.
It should be generated from an already valid proposed PR and contain:

- proposed PR ID and source specification fingerprint;
- assumptions and unresolved decisions;
- ordered implementation units with exact paths and symbols where known;
- an acceptance-criterion-to-test-and-validation matrix;
- declared validation commands and expected outcomes;
- allowed file scope and explicit exclusions;
- risks, checkpoints, and rollback boundaries;
- approval state and plan version.

The proposed PR remains authoritative. The plan cannot alter intent, acceptance
criteria, dependencies, or scope silently. A plan evaluator should report
missing coverage, unknown files/symbols, unsupported commands, dependency
violations, and scope expansion.

Plan approval should be policy-driven:

- Auto-approve when the proposed PR is already approved, the evaluator passes,
  no unresolved decision remains, and the plan does not expand scope.
- Require user approval for new assumptions, scope expansion, risky migration,
  destructive operation, or a material plan revision.

This keeps the useful explicit transition from OpenCode without asking the user
to approve a mechanical restatement of an already approved proposed PR.

### 3. Compile the approved plan into execution tasks

Do not ask later task prompts to reinterpret the plan. Compile its ordered units
and obligations into the existing workflow task model:

```text
validated proposed PR
  -> validated execution plan
  -> compiled workflow tasks and gates
  -> implementation events
  -> validation evidence
  -> PR readiness decision
```

The static `execute-proposed-pr` template becomes a skeleton and policy source.
The compiler fills paths, commands, criteria, and dependencies from the plan.
The generated tasks should carry only the plan slice and handoffs they need.

### 4. Resolve capabilities from phase and step

Keep `actions` as the one declaration of actions supported by a skill step.
Add engine-owned phase profiles such as:

- Discover: read, search, symbol discovery, context gathering, and questions.
- Plan: all Discover capabilities plus writing only the execution-plan artifact.
- Build: scoped edits, file operations, code navigation, and approved commands.
- Validate: declared validators, bounded diagnostic reads, and repair transition.
- Review: read-only diff/spec/evidence inspection.
- Publish: scoped Git and GitHub operations after all evidence gates pass.

The model should receive schemas only for the effective actions and tools.
Generic instructions should be attached by the runtime when an action is
available, as Powdrr already does for step actions. Tool help remains a
progressive-discovery fallback, not part of every tool schema.

Path and command scopes should be derived from the plan. A Build edit outside
the plan should be denied or trigger a plan-amendment transition, not merely
produce generic correction prose.

### 5. Separate the durable plan from mutable execution state

OpenCode's plan file and todo list serve different purposes. Powdrr should make
that distinction explicit:

- The execution plan is reviewed, versioned, and changed only through an
  amendment.
- Execution state is a mutable list of units and obligations with pending,
  running, passed, failed, blocked, or stale status.

The LLM may propose the next unit, but the runtime owns status transitions. This
allows interruption and resume without asking a compaction summary to recreate
what remains.

### 6. Add a validation evidence ledger

Create a runtime-owned ledger keyed by plan obligation and acceptance criterion.
Every evidence entry should include:

- criterion or obligation ID;
- command or review that produced it;
- exact result and normalized summary;
- relevant files, tests, and symbols;
- tree or checkpoint fingerprint;
- status and timestamp;
- superseding edit, when the evidence became stale.

Any later edit should invalidate affected evidence according to declared path
scope. The final PR gate should be deterministic: all required obligations must
have current passing evidence, all criteria must be covered, and the final diff
must fit approved scope.

The completeness review then becomes a semantic audit of a ledger rather than a
fresh attempt to reconstruct relationships from several handoffs.

### 7. Unify tool-call lifecycle and hooks

Introduce a shared event contract for both workflow chat and durable tasks:

```text
action proposed -> validated -> pending -> running -> completed | failed
```

Add common before/after hooks that can:

- enforce phase, path, command, and proposed-PR policy;
- capture a checkpoint before a mutating action;
- attach changed paths and diffs after an edit;
- run fast diagnostics for touched files;
- invalidate stale evidence;
- publish observer and replay events.

Existing action handlers can remain behind this contract during migration.

### 8. Feed diagnostics back immediately, then run authoritative gates

After each edit, run the cheapest relevant feedback available:

- syntax parsing for touched files;
- BasedPyright diagnostics and symbol checks for Python where configured;
- targeted formatter or linter checks when inexpensive;
- YAML parse and Powdrr schema validation for specification edits.

Attach normalized diagnostics to the edit result, with exact paths, ranges,
codes, and corrective hints. Do not treat these as final validation. The plan's
declared full test, lint, format, and type-check commands remain authoritative.

Powdrr already exposes BasedPyright symbol and structure operations. Extend that
surface toward definition, reference, and diagnostic discovery before adding a
general LSP subsystem. This gains the code-navigation benefit with less process
management and cross-language complexity.

### 9. Make correction policy depend on typed failure

Powdrr's `PowdrrExecutionError` is the right boundary for failures the agent can
repair. Extend it with structured fields instead of choosing recovery from
message substrings:

| Failure class | Default recovery |
|---|---|
| Invalid response or unsupported action | Return the effective action schemas and exact field issue; retry the same decision boundary. |
| Invalid tool arguments | Return tool-specific validation and help reference; retry the call. |
| Read/discovery miss | Offer exact candidates or a bounded discovery action. |
| Edit conflict or stale range | Restore the pre-action checkpoint, reread the target, and regenerate the edit. |
| Fast diagnostic failure | Keep the edit visible, attach diagnostics, and enter a bounded repair attempt. Revert if the repair regresses or exhausts policy. |
| Declared validation failure | Mark the obligation failed, transition to Build with the failure packet, then rerun all invalidated obligations. |
| Scope or acceptance-criteria mismatch | Return to Plan and require a plan amendment or user decision. |
| Permission denial or explicit user correction | Preserve the feedback as a durable constraint and do not silently retry. |
| Provider/transient infrastructure failure | Retry with provider policy without asking the model to “fix” it. |
| Internal invariant failure | Stop as a product defect with replay/checkpoint data; do not ask the agent to improvise. |

The observer should intervene only after deterministic policy cannot select a
safe correction, or when semantic progress regresses. Its output should remain
guidance; validators and the phase controller stay authoritative.

### 10. Add checkpoints and bounded rollback

Capture a lightweight checkpoint before every mutating action and at every phase
transition. Retain:

- tree fingerprint and changed paths;
- active plan version and execution unit;
- evidence ledger version;
- event boundary.

Expose `revert_action` and `revert_to_checkpoint` as runtime operations, normally
selected by correction policy rather than directly by the model. Reverting must
also restore execution and evidence state, not only files.

This enables safe experimentation inside an approved unit and prevents a repair
loop from accumulating broken edits.

### 11. Compact from typed state first

Powdrr's current compaction correctly protects the latest actionable failure.
The next step is to make most compaction deterministic:

Always preserve verbatim:

- user intent and proposed PR identity;
- approved execution-plan version;
- current phase and unit;
- unresolved decisions and user corrections;
- outstanding validation obligations;
- latest failed action and diagnostics;
- current checkpoint and changed-file summary.

Prune or summarize old reads, successful tool output, superseded repair attempts,
and completed unit discussion. Use an LLM compactor only for residual prose, not
to recreate typed state. Skills and specifications should be referenced by ID
and loaded progressively when needed.

### 12. Use specialized child agents with least privilege

Add runtime roles, not monolithic new personalities:

- Explorer: read/search/symbol/context only; returns source-backed findings.
- Planner: reads proposed PR plus explorer findings; writes only the plan.
- Implementer: edits only the active unit's approved paths.
- Validator: runs declared commands and records evidence; cannot edit.
- Reviewer: read-only comparison of diff, plan, proposed PR, and evidence.

Each child receives a clean context packet and returns a typed output. Child
work should be resumable by execution-unit ID. Parallelize independent discovery
or validation, but not overlapping edits.

## Skill changes

### Existing skills to simplify

`start-implementing-feature` should stop embedding execution mechanics after it
has produced valid proposed PRs and instantiated workflows. It should hand the
first executable proposed PR to the phase controller.

`run-tests-and-fix` should remain a reusable deterministic repair workflow, but
its test results and repair edits should update the shared obligation ledger and
checkpoint state. Its freeform diagnosis can become the recovery policy for a
failed test obligation rather than an independently orchestrated loop.

`finish-pr-prep` and `create-pull-request` should consume a deterministic PR
readiness report. They should not independently rediscover whether tests,
criteria, scope, and staged files are complete.

Specification skills should add stable acceptance-criterion IDs and validation
obligation metadata where absent. Those IDs are the join keys from proposed PR
to plan to evidence.

### New skills

Add thin, phase-specific skills whose supported actions activate generic runtime
instructions:

- `plan-proposed-pr-execution`: derive an execution plan from gathered proposed
  PR context and code structure.
- `review-execution-plan`: inspect coverage, risk, file scope, symbols, commands,
  and unresolved decisions without editing code.
- `amend-execution-plan`: make an explicit versioned change after scope drift,
  failed assumptions, or user guidance.
- `diagnose-validation-failure`: classify one failed obligation and produce a
  bounded repair packet.
- `verify-proposed-pr-evidence`: semantically audit the completed ledger against
  the proposed PR.
- `summarize-execution-evidence`: create the concise evidence section used in PR
  review and changelog preparation.

Avoid a generic `implement-plan` skill containing all phase instructions. The
controller and compiled workflow own sequencing; skills supply focused judgment.

## Tool additions and changes

The following are conceptual tool contracts. Some should be internal runtime
services rather than model-callable tools.

| Capability | Purpose | Model-callable? |
|---|---|---|
| `execution_plan` | Create, inspect, validate, diff, and propose an amendment to the typed plan. | Yes, only in Plan or recovery-to-Plan. |
| `execution_status` | Return the current phase, unit, obligations, checkpoint, and allowed transitions. | Read-only, progressively discoverable. |
| `record_evidence` | Normalize a deterministic command/review result into the obligation ledger. | Normally runtime-only. |
| `checkpoint` / `revert_checkpoint` | Capture or restore files plus execution/evidence state. | Runtime policy first; optionally exposed with permission. |
| `diagnostics` | Return bounded diagnostics and code locations for touched paths. | Yes in Build/Validate; automatically run after edits when configured. |
| `symbol` extensions | Definition, references, callers, implementers, and diagnostics using BasedPyright. | Yes in Discover/Plan/Build. |
| `scope_diff` | Compare the current tree against approved plan paths and specification effects. | Read-only; automatic at Review. |
| `validation_obligations` | List current/stale/failed obligations and exact rerun commands. | Read-only; writes are runtime-only. |

Every model-callable tool should return a discriminated result such as
`success`, `correctable_error`, `denied`, or `infrastructure_error`, plus stable
error code, concise message, structured details, and suggested next actions.
`PowdrrExecutionError` should carry that structure end to end.

## Suggested implementation sequence

Each item should be a separate proposed PR with its own acceptance criteria and
rollout switch.

1. **Execution schemas and shadow phase state.** Add phase, transition,
   execution-plan, execution-unit, obligation, evidence, and checkpoint models.
   Derive and log phase state during existing executions without changing
   behavior.
2. **Plan generator and evaluator.** Turn the current detailed-execution-plan
   handoff into an `execution-plan-v1` artifact, validate complete criterion
   coverage and scope, and retain compatibility with existing workflow tasks.
3. **Capability resolver.** Intersect phase profile, step `actions`, plan scope,
   and policy; render only effective action/tool schemas. Begin in report-only
   mode, then enforce planning and review read-only boundaries.
4. **Shared action lifecycle and typed errors.** Normalize chat/task events and
   extend `PowdrrExecutionError` with error code, category, details, retryability,
   and suggested recovery.
5. **Checkpoints and edit diagnostics.** Wrap mutating handlers, attach diffs and
   fast diagnostics, and implement bounded revert of files plus execution state.
6. **Validation evidence ledger.** Convert deterministic pre-step and validation
   results into evidence, invalidate them after relevant edits, and gate PR
   readiness on current evidence.
7. **Compile plans into workflow tasks.** Replace generic task reinterpretation
   with generated task slices and obligations. Migrate `execute-proposed-pr`,
   `run-tests-and-fix`, PR preparation, and PR creation.
8. **Specialized child roles and deterministic compaction.** Add clean typed
   packets, least-privilege child execution, resumable unit IDs, and
   state-preserving compaction.
9. **Remove compatibility inference.** Once all checked-in skills and workflows
   declare actions and consume typed handoffs, remove legacy action inference
   and duplicated phase instructions from prompts and skill prose.

## Validation and rollout

Use the existing workflow replay and tuning direction to compare the current and
new paths against identical fixtures. Required metrics should include:

- valid first-action rate per phase and step;
- repairs per completed unit, by failure category;
- repeated-action and semantic-stall rate;
- plan evaluator failures and post-approval plan amendments;
- edits outside approved file scope;
- acceptance criteria with current evidence at PR readiness;
- validation reruns avoided through targeted invalidation;
- prompt tokens per phase and total tokens per completed proposed PR;
- successful resume after interruption or compaction;
- rollback success after a deliberately broken repair;
- user questions, separated into required decisions and avoidable discovery.

Roll out in four stages:

1. Shadow state and metrics with no changed decisions.
2. Enforce Plan and Review read-only boundaries and typed plan validation.
3. Enforce evidence-based Build/Validate transitions and PR readiness.
4. Make compiled execution the default and delete legacy prompt-only controls.

Deterministic CI scenarios should cover at least: a straightforward proposed PR,
an ambiguous plan requiring user input, an invalid plan, a syntax error after an
edit, a failing full suite, a stale-evidence edit, scope expansion, a denied
operation, a provider retry, compaction during repair, checkpoint revert, and
resume after process interruption.

## Risks and safeguards

- **Too much ceremony for small changes.** Allow a compact plan with one unit
  and auto-approval, but never skip criterion coverage or validation evidence.
- **Plan and proposed PR drift into competing sources of truth.** Store the
  proposed PR fingerprint in the plan and reject intent/scope changes as plan
  amendments requiring policy evaluation.
- **Runtime complexity moves bugs out of prompts and into state transitions.**
  Keep transition rules small, pure, replayable, and covered by table-driven
  tests before enforcement.
- **Diagnostics become slow or noisy.** Configure fast diagnostics by project,
  bound their output, and retain declared full commands as the authoritative
  gates.
- **Checkpoint storage grows.** Store content-addressed diffs and retain phase
  boundaries plus a bounded number of recent action checkpoints.
- **Child agents fragment context.** Pass typed source references and outputs,
  not prose-only summaries; let the parent reopen exact evidence as needed.
- **Compatibility logic becomes permanent.** Attach removal criteria and metrics
  to every fallback, and make the final migration PR delete inferred action and
  phase behavior.

## What not to copy from OpenCode

- Do not make a Markdown plan the canonical delivery contract; Powdrr can
  validate a structured plan and tie it to specification IDs.
- Do not treat a mutable todo list as evidence or source of truth.
- Do not enable every tool by default. Powdrr knows the phase, step, proposed PR
  scope, and validation policy and can expose less.
- Do not give general subagents broad edit permission when a child has a narrow
  role.
- Do not replace project tests, lint, format, and type checks with LSP
  diagnostics.
- Do not force every valid proposed PR through a redundant human plan approval;
  ask only when the derived plan introduces a decision or risk not already
  approved.
- Do not rebuild Powdrr around UI message parts unless a future product goal
  requires a general-purpose interactive coding client.

## Final recommendation

Proceed with Option B through the proposed-PR sequence above. The first useful
vertical slice is:

```text
approved proposed PR
  -> typed execution plan
  -> deterministic plan evaluator
  -> explicit Plan-to-Build transition
  -> one scoped implementation unit
  -> automatic diagnostics
  -> one evidence-backed validation obligation
  -> checkpointed completion
```

That slice proves the architecture without replacing either runner. Once it is
reliable, expand the same contracts across the full `execute-proposed-pr` flow
and remove the duplicated procedural instructions from skills and prompts.
