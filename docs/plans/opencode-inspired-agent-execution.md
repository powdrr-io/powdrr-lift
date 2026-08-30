# OpenCode-Inspired Agent Execution Proposal

## Decision requested

Adopt a typed phase controller around Powdrr's existing specifications, proposed
PRs, skills, and deterministic workflow tasks. Implement it as two deliberately
separate layers:

- A customizable delivery model describes what needs to be accomplished, which
  persona owns each phase, which structured artifacts cross each boundary, and
  what approval or review policy applies.
- A hardened execution kernel orchestrates LLM roundtrips and tools, validates
  actions and transitions, corrects recoverable errors, manages checkpoints and
  retries, and records evidence. Ordinary skill and workflow customization must
  not be able to replace or weaken this machinery.

The controller should make the path from prompt to specification to proposed PR
to code to validation explicit runtime state, rather than relying on every
skill to restate the path correctly.

This is Option B below. It preserves Powdrr's differentiator—structured,
validated intent—while adopting the strongest parts of OpenCode's execution
runtime: capability boundaries, explicit plan/build transitions, persisted tool
state, checkpoints, immediate diagnostic feedback, bounded context, and
phase-specific agents. The recommended default delivery model uses an Architect
to author structured specifications, an Engineering Manager to turn those
specifications into proposed PRs, and Engineers plus independent Reviewers to
execute and validate each PR.

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
| **B. Layered typed phase controller** | Add a customizable delivery-profile layer over an engine-owned phase runtime, validated artifacts, capability resolution, an evidence ledger, common tool lifecycle hooks, and checkpoints around the existing runners. | **Recommended.** Users retain control over objectives, personas, handoffs, and review policy while transitions, safety, correction, and evidence stay engine-owned. Can ship incrementally and be evaluated in shadow mode. | Moderate runtime work; requires a clear extension boundary and migration to thinner skill declarations; temporary compatibility layer while both paths exist. |
| C. Event-sourced session runtime | Replace the current chat/task loops with a unified persistent session processor modeled after OpenCode, with messages, parts, child sessions, snapshots, and dynamic tools as the primary abstraction. | Maximum flexibility, resumability, UI observability, and parity with interactive coding agents. | Largest rewrite and migration risk; can subordinate Powdrr's specification/workflow model to a conversation model; delays direct improvements to proposed-PR execution. |

Option A is useful as an implementation spike but is not a stable endpoint.
Option C may become attractive if Powdrr needs to be a general interactive coding
client. It is unnecessary for making proposed-PR execution materially better.

## Recommended target design

### 1. Separate delivery intent from execution mechanics

The central architecture boundary should be explicit in code and schemas:

| Customizable delivery model | Hardened execution kernel |
|---|---|
| Phase objectives and completion criteria | LLM request/response loop and structured-output parsing |
| Persona descriptions, prompt overlays, model profiles, and interaction style | Action-schema generation and validation |
| Required input/output artifact types | Legal state-transition enforcement |
| Skill selection and domain-specific instructions | Effective capability and permission resolution |
| Approval policy within system safety constraints | Tool lifecycle, timeouts, provider retries, and cancellation |
| Review topology, such as code, security, or specification reviewers | Typed error classification and correction routing |
| Organization-specific quality requirements and validation commands | Checkpoints, rollback, evidence invalidation, and resume |
| Optional additional phases and handoff requirements | Event persistence, replay, observer triggers, and compaction |

The delivery model says **what good work looks like and who is responsible**.
The kernel decides **how to obtain a valid next action safely and reliably**.
Skills and workflows must not contain custom JSON-repair loops, retry policies,
checkpoint behavior, generic tool instructions, or prose that can bypass a
failed gate.

Represent the customizable layer as a validated `delivery-profile-v1` document.
For example:

```yaml
schema: https://powdrr.io/schemas/delivery-profile-v1
id: default-software-delivery
personas:
  architect:
    description: Own coherent structured design specifications.
    model_profile: high_reasoning
    skills: [specify-system, specify-architecture, specify-implementation]
  engineering_manager:
    description: Decompose approved specifications into reviewable proposed PRs.
    model_profile: high_reasoning
    skills: [plan-proposed-prs, review-proposed-pr-sequence]
  engineer:
    description: Plan and implement one approved proposed PR.
    model_profile: standard_reasoning
    skills: [plan-proposed-pr-execution, execute-approved-plan]
  specification_reviewer:
    description: Verify the implementation evidence satisfies the proposed PR.
    model_profile: high_reasoning
    skills: [verify-proposed-pr-evidence]
  code_reviewer:
    description: Review correctness, maintainability, risk, and test quality.
    model_profile: high_reasoning
    skills: [review-implementation-diff]
phases:
  - type: specify
    persona: architect
    produces: [system-specification, architecture-specification, implementation-specification]
  - type: decompose
    persona: engineering_manager
    consumes: [implementation-specification]
    produces: [proposed-pr-specification]
  - type: execute_pr
    persona: engineer
    consumes: [proposed-pr-specification]
    produces: [execution-plan, implementation-diff, validation-evidence]
  - type: review_pr
    personas: [specification_reviewer, code_reviewer]
    consumes: [proposed-pr-specification, implementation-diff, validation-evidence]
    produces: [review-findings, pr-readiness-decision]
```

This profile intentionally does not define action JSON, tool parameters, retry
counts, correction prompts, or event mechanics. A phase `type` selects an
engine-owned capability envelope and transition contract. Users may customize
the persona and objective, add validated requirements, select skills and models,
or add a reviewer, but they cannot grant a persona capabilities outside that
envelope or declare failed evidence to be passing.

The extension contract should distinguish three levels:

1. **Safe customization:** prompts, personas, model profiles, skills, artifact
   quality rules, validation commands, approval policy, and additional reviews.
2. **Validated structural customization:** adding or ordering phase types and
   handoffs when their schemas and transition requirements compose correctly.
3. **Kernel changes:** new action types, phase envelopes, retry behavior,
   correction routing, checkpoint semantics, or evidence rules. These require
   product code and tests, not a user-authored skill edit.

### 2. Introduce a delivery phase controller

Add engine-owned phase types and transition records. The default delivery
profile composes them into this graph:

```text
intake
  -> specify -> review_specifications
  -> decompose -> review_proposed_prs
  -> for each proposed PR:
       plan_pr -> awaiting_plan_approval -> build
       -> validate -> review_pr -> resolve_findings
       -> validate -> confirm_readiness -> publish_pr
  -> complete_feature
```

The profile controls which configured persona receives each assignment and may
insert compatible reviews or approvals. The kernel owns the semantics and
minimum entry/exit requirements of every phase type. For example, no profile can
enter `build` without a valid proposed PR and execution plan, or enter
`publish_pr` with stale evidence or unresolved blocking findings.

`recover` should be a reasoned transition to a prior phase, not a parallel happy
path. For example, validation can return to Build with a typed failed-obligation
packet, review can return to `resolve_findings`, and a scope change can return to
the Engineering Manager's decomposition phase with a typed amendment request.

Each transition should record:

- prior and next phase;
- initiating action and actor;
- artifact versions and tree fingerprint;
- satisfied and outstanding gates;
- reason, including whether the transition was automatic or human-approved.

The runtime should reject illegal transitions before an LLM call where possible.
Prompt text should explain the current phase, not enforce it.

### 3. Make phase personas first-class

The default delivery profile should use personas with distinct organizational
responsibilities, not only generic Explorer, Planner, and Implementer labels:

```text
User prompt
  -> Architect
       authors and reconciles structured system, architecture, and
       implementation specifications
  -> Engineering Manager
       decomposes approved specification effects into ordered proposed PRs,
       dependencies, acceptance criteria, risk, and ownership
  -> Engineer (one active proposed PR)
       derives the execution plan, writes tests and code, and responds to
       deterministic validation and reviewer findings
  -> Specification Reviewer + Code Reviewer
       independently review intent coverage and implementation quality
  -> Engineer
       repairs actionable findings and produces fresh validation evidence
  -> Reviewers / Engineering Manager
       confirm readiness and advance or publish the PR
```

Persona boundaries should be artifact boundaries:

- The Architect owns design coherence and specification quality. It cannot edit
  product code or decide implementation success.
- The Engineering Manager owns decomposition, ordering, PR scope, dependencies,
  acceptance criteria, and escalation of unresolved product decisions. It does
  not implement the PR.
- The Engineer owns the low-level execution plan and implementation within one
  approved proposed PR. It cannot broaden PR scope without returning a typed
  amendment request to the Engineering Manager.
- The Specification Reviewer independently checks the diff and evidence against
  the proposed PR and its source specifications.
- The Code Reviewer independently checks correctness, maintainability, tests,
  security, and implementation risk. Additional reviewer personas can be added
  by a delivery profile.
- Deterministic validation is a kernel service, not a persona. A Validator agent
  may diagnose a failure, but it cannot turn a failing result into passing
  evidence.

Personas should have their own system prompt overlay, model profile, allowed
skills, interaction style, and clean input packet. Their effective tools and
actions still come from the engine-owned phase envelope intersected with the
current skill step and policy. Persona prose never grants permission.

Review coordination should use typed findings rather than a shared freeform
conversation. Each finding should contain a stable ID, reviewer role, severity,
category, affected criterion or code location, evidence, requested change, and
disposition. The Engineer returns a resolution for every blocking finding;
reviewers then verify the new diff and fresh evidence. This supports real
coordination while preserving independence and replayability.

### 4. Make the execution plan a typed derivative of the proposed PR

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

### 5. Compile the approved plan into execution tasks

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

### 6. Resolve capabilities from phase and step

Keep `actions` as the one declaration of actions supported by a skill step.
Add engine-owned phase-type envelopes such as:

- Specify: read, search, symbol/context discovery, questions, and writes limited
  to structured specification artifacts.
- Decompose: specification and project-structure reads plus writes limited to
  proposed-PR and assignment artifacts.
- Plan PR: all relevant discovery capabilities plus writes limited to the
  execution-plan artifact.
- Build: scoped edits, file operations, code navigation, and approved commands
  for the active execution unit.
- Validate: declared validators, bounded diagnostic reads, evidence recording,
  and a repair transition; deterministic results are immutable.
- Review: read-only diff/spec/plan/evidence inspection and typed finding output.
- Resolve Findings: the Engineer's scoped Build capabilities plus mandatory
  finding dispositions and evidence invalidation.
- Publish: scoped Git and GitHub operations after all evidence and finding gates
  pass.

The model should receive schemas only for the effective actions and tools.
Generic instructions should be attached by the runtime when an action is
available, as Powdrr already does for step actions. Tool help remains a
progressive-discovery fallback, not part of every tool schema.

Path and command scopes should be derived from the plan. A Build edit outside
the plan should be denied or trigger a plan-amendment transition, not merely
produce generic correction prose.

### 7. Separate the durable plan from mutable execution state

OpenCode's plan file and todo list serve different purposes. Powdrr should make
that distinction explicit:

- The execution plan is reviewed, versioned, and changed only through an
  amendment.
- Execution state is a mutable list of units and obligations with pending,
  running, passed, failed, blocked, or stale status.

The LLM may propose the next unit, but the runtime owns status transitions. This
allows interruption and resume without asking a compaction summary to recreate
what remains.

### 8. Add a validation evidence ledger

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

### 9. Unify tool-call lifecycle and hooks

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

### 10. Feed diagnostics back immediately, then run authoritative gates

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

### 11. Make correction policy depend on typed failure

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

### 12. Add checkpoints and bounded rollback

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

### 13. Compact from typed state first

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

### 14. Execute personas as least-privilege child agents

Run each persona assignment in a child execution context with a phase-derived
capability envelope. The child receives only its declared artifact inputs,
durable user guidance, and the current assignment:

- Architect contexts receive the user intent, current specifications, relevant
  repository evidence, and specification evaluator results.
- Engineering Manager contexts receive approved specifications, project
  structure, proposed-PR evaluator results, and dependency state.
- Engineer contexts receive one proposed PR, its approved execution plan, the
  active implementation unit, current checkpoint, diagnostics, and unresolved
  findings.
- Reviewer contexts receive an immutable review snapshot: proposed PR, source
  specification references, complete diff, current validation ledger, and prior
  finding dispositions. They do not receive edit capability.

Each child returns a typed artifact or finding set instead of a prose summary.
Work should be resumable by assignment or execution-unit ID. Independent
Architect discovery and independent reviewers may run in parallel. Do not run
overlapping Engineer edits in the same worktree, and do not let one reviewer see
another reviewer's conclusions before producing its initial findings unless the
delivery profile explicitly requests a consensus round.

Explorer and failure-diagnosis helpers can remain internal subagents used by a
persona. They are implementation details of the execution kernel, not required
roles in the customizable delivery model.

## Skill changes

### Keep personas and skills separate

A persona is a durable responsibility and decision posture. A skill is a
customizable procedure the persona may use to produce one artifact or judgment.
The phase controller assigns a persona; the delivery profile selects its skills;
the kernel executes those skills safely.

This separation allows an organization to replace `review-implementation-diff`
with its own security-focused review skill without replacing the Code Reviewer
role, changing review permissions, or reimplementing action correction. Likewise,
the Architect can use organization-specific specification skills while the same
specification schemas and evaluators remain authoritative.

Checked-in skill steps should continue to list only the semantic actions they
support; that declaration activates kernel-owned generic instructions and
schemas. Workflow tasks should reference a `phase_type` and a persona assignment
from the active delivery profile rather than embedding another copy of persona
or orchestration instructions. Over time:

- the current broad `architect` assignee role splits into Architect for design
  artifacts and Engineering Manager for proposed-PR decomposition;
- `coder` becomes the Engineer persona;
- `reviewer` becomes one or more named Reviewer personas with separate finding
  categories and clean contexts;
- `llm_type` and `interaction_style` become assignment overrides of persona
  defaults, not substitutes for a persona contract.

### Existing skills to simplify

The specification skills should become the default Architect toolkit. They
should focus on eliciting intent and producing coherent structured system,
architecture, and implementation artifacts. They should not describe generic
action syntax, retries, tool errors, or workflow traversal.

`start-implementing-feature` currently spans Engineering Manager decomposition,
workflow generation, execution setup, and PR preparation. Split those
responsibilities. The Engineering Manager portion should produce and validate
the proposed-PR specification, then hand each executable proposed PR to the
controller. The kernel should instantiate and track execution; no persona skill
should need to restate those mechanics.

`run-tests-and-fix` should remain a reusable deterministic repair workflow, but
its test results and repair edits should update the shared obligation ledger and
checkpoint state. Its freeform diagnosis can become the recovery policy for a
failed test obligation used by the Engineer rather than an independently
orchestrated loop or a Validator persona with authority over results.

`finish-pr-prep` and `create-pull-request` should consume a deterministic PR
readiness report. The Engineering Manager or publishing policy may authorize
publication, but these skills should not independently rediscover whether tests,
criteria, scope, findings, and staged files are complete.

Specification skills should add stable acceptance-criterion IDs and validation
obligation metadata where absent. Those IDs are the join keys from proposed PR
to plan to evidence.

### New skills

Add thin, phase-specific skills whose supported actions activate generic runtime
instructions:

- `author-structured-design`: Architect-owned coordination of system,
  architecture, and implementation specification skills.
- `plan-proposed-prs`: Engineering Manager decomposition of approved
  specification effects into ordered, scoped proposed PRs.
- `review-proposed-pr-sequence`: Engineering Manager review of dependency order,
  incremental value, risk, acceptance criteria, and ownership boundaries.
- `plan-proposed-pr-execution`: derive an execution plan from gathered proposed
  PR context and code structure; Engineer-owned.
- `request-proposed-pr-amendment`: let the Engineer return a typed scope or
  assumption issue to the Engineering Manager instead of broadening the PR.
- `diagnose-validation-failure`: classify one failed obligation and produce a
  bounded repair packet for the Engineer.
- `verify-proposed-pr-evidence`: Specification Reviewer audit of the completed
  ledger against the proposed PR and source specifications.
- `review-implementation-diff`: Code Reviewer assessment of correctness,
  maintainability, risk, tests, and security.
- `resolve-review-findings`: Engineer-owned disposition and repair of every
  blocking typed finding.
- `summarize-execution-evidence`: create the concise evidence and finding
  disposition section used for Engineering Manager readiness and PR review.

Avoid a generic `implement-plan` skill containing all phase instructions. The
controller and compiled workflow own sequencing; personas own decisions and
skills supply focused judgment.

## Tool additions and changes

The following are conceptual tool contracts. Some should be internal runtime
services rather than model-callable tools.

| Capability | Purpose | Model-callable? |
|---|---|---|
| `execution_plan` | Create, inspect, validate, diff, and propose an amendment to the typed plan. | Yes, only in Plan PR or recovery-to-Plan PR. |
| `execution_status` | Return the current phase, unit, obligations, checkpoint, and allowed transitions. | Read-only, progressively discoverable. |
| `record_evidence` | Normalize a deterministic command/review result into the obligation ledger. | Normally runtime-only. |
| `checkpoint` / `revert_checkpoint` | Capture or restore files plus execution/evidence state. | Runtime policy first; optionally exposed with permission. |
| `diagnostics` | Return bounded diagnostics and code locations for touched paths. | Yes in Build/Resolve Findings; automatically run after edits when configured. |
| `symbol` extensions | Definition, references, callers, implementers, and diagnostics using BasedPyright. | Yes in Specify/Decompose/Plan PR/Build as scoped. |
| `scope_diff` | Compare the current tree against approved plan paths and specification effects. | Read-only; automatic at Review PR. |
| `validation_obligations` | List current/stale/failed obligations and exact rerun commands. | Read-only; writes are runtime-only. |

Every model-callable tool should return a discriminated result such as
`success`, `correctable_error`, `denied`, or `infrastructure_error`, plus stable
error code, concise message, structured details, and suggested next actions.
`PowdrrExecutionError` should carry that structure end to end.

## Suggested implementation sequence

Each item should be a separate proposed PR with its own acceptance criteria and
rollout switch.

1. **Define the extension boundary.** Add `delivery-profile-v1`, persona,
   assignment, artifact-handoff, and review-topology schemas. Document and test
   which fields are user-customizable and which changes require kernel code.
   Check in the Architect → Engineering Manager → Engineer/Reviewers profile as
   the default.
2. **Execution schemas and shadow phase state.** Add engine-owned phase,
   transition, execution-unit, obligation, evidence, finding, and checkpoint
   models. Derive and log phase and persona assignments during existing
   executions without changing behavior.
3. **Persona runner.** Build clean typed input/output packets, model-profile and
   prompt-overlay selection, assignment resume, and least-privilege child
   contexts. Initially run the Architect and Engineering Manager handoffs in
   shadow mode against existing specification and proposed-PR skills.
4. **Plan generator and evaluator.** Turn the current detailed-execution-plan
   handoff into an `execution-plan-v1` artifact owned by the Engineer, validate
   complete criterion coverage and scope, and retain compatibility with existing
   workflow tasks.
5. **Capability resolver.** Intersect the engine-owned phase-type envelope, step
   `actions`, plan scope, and policy; render only effective action/tool schemas.
   Begin in report-only mode, then enforce Architect, Engineering Manager, and
   Reviewer read-only product-code boundaries.
6. **Shared action lifecycle and typed errors.** Normalize chat/task events and
   extend `PowdrrExecutionError` with error code, category, details, retryability,
   and suggested recovery. Move response repair, retries, and correction routing
   behind this common kernel interface.
7. **Checkpoints and edit diagnostics.** Wrap mutating handlers, attach diffs and
   fast diagnostics, and implement bounded revert of files plus execution state.
8. **Validation evidence and review findings.** Convert deterministic pre-step
   and validation results into evidence, invalidate them after relevant edits,
   add typed independent reviewer findings and dispositions, and gate PR
   readiness on both current evidence and resolved blocking findings.
9. **Compile plans into workflow tasks.** Replace generic task reinterpretation
   with generated task slices and obligations. Migrate `execute-proposed-pr`,
   `run-tests-and-fix`, reviewer coordination, PR preparation, and PR creation.
10. **Deterministic compaction and compatibility removal.** Preserve persona,
    artifact, phase, finding, and evidence state without relying on prose
    summaries. Once checked-in skills and workflows use typed handoffs, remove
    legacy action inference and duplicated orchestration/correction instructions
    from prompts and skill prose.

## Validation and rollout

Use the existing workflow replay and tuning direction to compare the current and
new paths against identical fixtures. Required metrics should include:

- valid first-action rate per phase and step;
- valid persona artifact-handoff rate and missing-input rate;
- attempts by a persona or customized profile to exceed its phase envelope;
- repairs per completed unit, by failure category;
- repeated-action and semantic-stall rate;
- plan evaluator failures and post-approval plan amendments;
- edits outside approved file scope;
- acceptance criteria with current evidence at PR readiness;
- validation reruns avoided through targeted invalidation;
- prompt tokens per phase and total tokens per completed proposed PR;
- successful resume after interruption or compaction;
- rollback success after a deliberately broken repair;
- blocking review findings resolved with fresh evidence before readiness;
- agreement and useful disagreement between independent reviewer personas;
- user questions, separated into required decisions and avoidable discovery.

Roll out in four stages:

1. Validate delivery profiles and shadow persona assignments, state, and metrics
   with no changed decisions.
2. Enforce persona phase envelopes, read-only Architect/Engineering Manager/
   Reviewer product-code boundaries, and typed artifact handoffs.
3. Enforce typed Engineer plans, evidence-based Build/Validate transitions,
   independent finding disposition, and PR readiness.
4. Make layered compiled execution the default and delete legacy prompt-only
   orchestration and correction controls.

Deterministic CI scenarios should cover at least: a straightforward proposed PR,
an ambiguous plan requiring user input, an invalid plan, a syntax error after an
edit, a failing full suite, a stale-evidence edit, scope expansion, a denied
operation, a provider retry, compaction during repair, checkpoint revert, and
resume after process interruption.

Add profile-conformance scenarios proving that users can change persona prompts,
models, skills, validation commands, and reviewer composition without changing
kernel behavior. Add negative scenarios proving that a profile cannot expose an
edit action to a Reviewer, bypass a transition, suppress a failing obligation,
change retry semantics, or mark an unresolved blocking finding as complete.

## Risks and safeguards

- **Too much ceremony for small changes.** Allow a compact plan with one unit
  and auto-approval, but never skip criterion coverage or validation evidence.
- **Customization leaks into execution mechanics.** Keep delivery-profile fields
  declarative, resolve them through a closed set of phase types, reject unknown
  kernel fields, and test that profiles cannot expand capability envelopes or
  redefine success.
- **Personas become decorative prompt labels.** Give every assignment a typed
  input, typed output, artifact ownership, capability envelope, and independent
  event identity. Measure cross-persona handoff quality and permission leakage.
- **Personas duplicate skills.** Keep stable responsibility and decision posture
  in the persona; keep reusable domain procedures in skills; keep all response
  mechanics in the kernel.
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

Proceed with Option B as a layered architecture, not a larger configurable
workflow prompt. Check in a strong default delivery profile while preserving a
narrow, validated extension surface for other organizations.

The first useful vertical slice should prove both the persona handoff and the
hardened kernel boundary:

```text
customizable default delivery profile
  -> Architect produces one validated structured specification change
  -> Engineering Manager produces one scoped proposed PR
  -> Engineer produces a typed execution plan and one implementation unit
  -> hardened kernel validates actions, checkpoints edits, and records evidence
  -> Specification Reviewer and Code Reviewer return independent typed findings
  -> Engineer resolves blocking findings
  -> deterministic readiness gate confirms fresh evidence and resolved findings
```

The profile may change the persona prompts, selected skills, model profiles,
validation commands, or add another Reviewer without altering the kernel path.
That is the acceptance test for the separation. Once the slice is reliable,
expand the same contracts across the full feature and `execute-proposed-pr`
flows and remove duplicated orchestration, correction, and retry instructions
from skills and prompts.
