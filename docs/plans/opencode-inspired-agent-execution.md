# OpenCode-Inspired Agent Execution Proposal

The companion
[`engineering implementation plan`](opencode-inspired-agent-execution-engineering-plan.md)
defines the concrete contracts, module boundaries, migration sequence, tests,
and acceptance gates for this proposal.

## Decision requested

Adopt a typed phase controller around Powdrr's existing specifications, proposed
PRs, skills, and deterministic workflow tasks. Implement it as two deliberately
separate layers:

- A customizable delivery model describes what needs to be accomplished, which
  persona owns each phase, which structured artifacts cross each boundary, and
  what approval or review policy applies.
- A hardened execution kernel orchestrates LLM roundtrips and tools, validates
  actions and transitions, corrects recoverable errors, manages checkpoints and
  retries, expands related obligations from durable user guidance, and records
  evidence. Its normal tools should be powerful but safe by construction and
  should not require routine human permission checks. Ordinary skill and
  workflow customization must not be able to replace or weaken this machinery.

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

Powdrr should adopt dynamic, context-specific tool resolution and shared hooks,
but not OpenCode's expectation that safety is primarily an allow/ask/deny
decision. Powdrr has stronger prior structure: the current phase, explicit step
actions, proposed-PR scope, active execution unit, worktree, and required
evidence can constrain a tool before it is exposed. The normal path should run
within those constraints without asking a human to judge every operation.

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

### Safety still trends toward permission checks or narrow tool coverage

A broad software-development agent eventually needs more than file reads,
edits, shell, Git, and GitHub. It needs dependency management, code navigation,
build systems, databases, migrations, containers, cloud diagnostics, issue and
review operations, and project-specific tools. Adding each tool behind another
human approval does not scale and gives the human too little context to make a
real safety decision.

The explicit actions on each step provide a better starting point. Powdrr does
not yet have a manifest and broker that can map those semantic actions to a
growing tool set, constrain effects by phase and plan, sandbox execution, and
prove the result. Arbitrary shell remains too broad to be the default escape
hatch for every missing capability.

### User guidance is remembered as context more than behavior

Powdrr preserves durable decisions and corrections in prompts and artifacts,
but it does not yet compile a clear user instruction into a reusable behavioral
rule that creates future obligations. Two representative instructions are:

- “When you make changes based on a review comment, resolve the comment
  afterwards.”
- “Always use optimistic locking for mutable database rows.”

The first relates a source action, implementation and validation, and a required
follow-up action. The second relates a class of code changes to an architectural
pattern and review obligation. Repeating either sentence in every prompt is
weaker than storing its scope and source, matching it to relevant proposed
actions, and preventing completion until the related obligations are satisfied.

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
| **B. Layered typed phase controller** | Add a customizable delivery-profile layer over an engine-owned phase runtime, safe capability broker, durable behavior-rule and action-relationship graph, validated artifacts, evidence ledger, common tool lifecycle hooks, and checkpoints around the existing runners. | **Recommended.** Users retain control over objectives, personas, handoffs, engineering guidance, and review policy while tool safety, transitions, correction, memory application, and evidence stay engine-owned. Can ship incrementally and be evaluated in shadow mode. | Moderate runtime work; requires clear extension and tool-effect contracts, a stable action ontology, and migration to thinner skill declarations; temporary compatibility layer while both paths exist. |
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
| Skill selection and domain-specific instructions | Effective capability, effect-scope, and tool resolution |
| Durable engineering guidance and organization conventions | Guidance matching and action-relationship obligation expansion |
| Human escalation policy within system safety constraints | Safe tool broker, effect constraints, sandboxing, and exception enforcement |
| Approval and review policy | Tool lifecycle, timeouts, provider retries, and cancellation |
| Review topology, such as code, security, or specification reviewers | Typed error classification and correction routing |
| Organization-specific quality requirements and validation commands | Checkpoints, rollback, evidence invalidation, and resume |
| Optional additional phases and handoff requirements | Event persistence, replay, observer triggers, and compaction |

The delivery model says **what good work looks like and who is responsible**.
The kernel decides **how to obtain a valid next action safely and reliably**.
Skills and workflows must not contain custom JSON-repair loops, retry policies,
checkpoint behavior, generic tool safety instructions, or prose that can bypass
a failed gate. User guidance may add or narrow obligations but may not expand a
phase capability envelope or redefine deterministic success.

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
   quality rules, validation commands, durable behavioral guidance, escalation
   policy, and additional reviews.
2. **Validated structural customization:** adding or ordering phase types and
   handoffs when their schemas and transition requirements compose correctly.
3. **Kernel changes:** new action types, tool effect classes, phase envelopes,
   retry behavior, correction routing, checkpoint semantics, relationship
   semantics, or evidence rules. These require product code and tests, not a
   user-authored skill edit.

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
current skill step and policy. Persona prose never expands capability.

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
the plan should return a constraint violation or trigger a plan-amendment
transition, not merely produce generic correction prose.

### 7. Make the normal tool path safe by construction

Replace routine permission prompts with a capability broker that exposes only
tools capable of implementing the current step's explicit semantic actions
within the current phase and effect scope:

```text
effective tool call
  = declared step action
  ∩ phase-type capability envelope
  ∩ active plan and artifact scope
  ∩ tool effect manifest
  ∩ sandbox and external-effect policy
  ∩ outstanding behavioral obligations
```

A tool should declare a machine-readable manifest containing:

- stable tool and operation IDs;
- semantic actions it implements;
- input and output schemas;
- effect classes, such as workspace read, workspace write, process execution,
  network read, Git mutation, GitHub mutation, database mutation, or secret use;
- scope dimensions, such as paths, commands, hosts, repositories, branches,
  review-thread IDs, databases, or cloud resources;
- preconditions and postconditions;
- reversibility and checkpoint strategy;
- idempotency behavior;
- automatic validation and evidence hooks;
- sandbox profile and whether the operation can ever require an exception.

At an assignment boundary, the broker binds the manifest to concrete scope. An
Engineer edit tool receives the approved paths for the active execution unit. A
test tool receives declared commands and the worktree. A Git tool receives the
feature branch and can structurally prevent protected-branch pushes. A review
tool receives the exact repository, pull request, and review-thread IDs and can
make comment resolution idempotent. The model does not receive ambient authority
that it must use carefully; it receives operations that cannot express unrelated
effects.

The supported tool set should grow substantially. Favor typed tools for common
software-development effects, including code search and symbols, structured
edits, builds and tests, dependency changes, package-manager operations,
database migrations, containers, Git, GitHub reviews, issue tracking, CI and log
inspection, and project-specific validation. Multiple tools may implement the
same semantic action. Progressive discovery should first show the action and a
compact list of matching tools, then expose a selected tool's full schema and
examples through help.

Arbitrary shell should not be the universal implementation of new tools. A
sandboxed command runner may execute plan-declared project commands with bounded
filesystem, process, environment, network, and time effects. An unrestricted
host shell, secret access, protected-branch mutation, undeclared external write,
or destructive database/cloud operation belongs to the exception path.

An out-of-scope call normally returns a typed `constraint_violation` to the
agent. The kernel should select a safe alternative, request a plan amendment, or
correct the arguments. It should not turn every constraint violation into a
human question.

### 8. Make capability exceptions rare and decision-ready

When the requested result genuinely cannot be achieved inside available safe
tools, create a `capability-exception-request-v1` artifact. This is not a generic
“allow this tool?” prompt. It must tell the human:

- the user goal, current phase, persona, proposed PR, and execution unit;
- the exact blocked semantic action and why normal tools cannot perform it;
- alternatives attempted and why they were insufficient;
- the exact operation, targets, effect classes, and external systems involved;
- a preview or diff where possible;
- data and secrets accessed, network destinations, and expected side effects;
- worst credible failure, blast radius, and reversibility;
- checkpoint or backup state and rollback procedure;
- validations that will run afterward;
- requested duration, invocation count, and scope;
- whether the request is a one-time exception or a proposal for a new safe tool.

The human can approve that exact operation once, approve a narrower bounded
exception, deny it with guidance, or request that a proper tool be added. The
grant becomes a scoped capability token tied to the artifact fingerprint and
expires after its declared use. It must not become ambient session permission or
silently alter the delivery profile.

Exception frequency is a product metric. Repeated exceptions with the same
effect pattern should create a proposal for a typed tool or manifest extension,
not normalize repeated approval prompts. High-quality context and a rare
frequency are what make human review meaningful.

### 9. Separate the durable plan from mutable execution state

OpenCode's plan file and todo list serve different purposes. Powdrr should make
that distinction explicit:

- The execution plan is reviewed, versioned, and changed only through an
  amendment.
- Execution state is a mutable list of units and obligations with pending,
  running, passed, failed, blocked, or stale status.

The LLM may propose the next unit, but the runtime owns status transitions. This
allows interruption and resume without asking a compaction summary to recreate
what remains.

### 10. Compile user guidance into durable behavioral rules

Treat a user instruction about future behavior as a typed artifact, not merely a
sentence retained in conversation history. Add `behavior-rule-v1` with:

- stable ID and original user wording;
- source message, author, and timestamp;
- normalized intent and rule kind;
- scope: current assignment, proposed PR, work item, repository, organization,
  or user default;
- trigger conditions and contextual selectors;
- required, forbidden, or preferred related actions and patterns;
- required evidence and completion effect;
- priority, status, version, superseded rule, and optional expiration;
- conflicts and the decision that resolved them.

Storage should follow scope. Assignment and proposed-PR rules live with durable
execution state. Work-item and repository rules should be versioned in a
checked-in guidance artifact linked to the relevant specifications. Organization
and user-default rules live in their configured durable profile store. The
conversation transcript is provenance, never the only copy of remembered
behavior.

The guidance extractor may use an LLM to propose this structure, but the kernel
validates rule shape, known action and relationship types, scope, and conflicts.
A clear directive should take effect in the current execution immediately and
be stored at the narrowest durable scope that satisfies the wording. Words such
as “always,” “whenever,” and “from now on” indicate durable guidance rather than
a one-turn correction. The system should briefly report what it remembered and
its scope, without requiring confirmation for an unambiguous, non-destructive
rule. It should ask one targeted question only when scope or meaning would
materially change behavior.

Rules must be inspectable, explainable, editable, and revocable. Provide
`remember_guidance`, `list_guidance`, `explain_guidance`, and `revoke_guidance`
operations, backed by the same storage whether invoked by a user interface, CLI,
or agent. Preserve the original wording so a normalized rule never becomes an
untraceable reinterpretation.

Precedence should be explicit:

1. Kernel safety and deterministic truth cannot be overridden by guidance.
2. Approved specification invariants and proposed-PR scope require an explicit
   amendment rather than a silent conflicting rule.
3. A direct current user instruction may supersede older user guidance at the
   same or broader scope; the supersession is recorded.
4. More specific applicable guidance takes precedence over general preferences.
5. Unresolved conflicts become a decision obligation rather than arbitrary LLM
   choice.

Do not inject every remembered rule into every prompt. Match rules against the
current phase, artifact, entities, proposed actions, and code context, then send
only applicable rules and the obligations they produced.

### 11. Expand proposed actions through an action-relationship graph

Use a typed relationship graph to turn relevant guidance into executable
behavior. Nodes should include semantic action types, action instances,
artifacts, evidence, review comments, code entities and traits, engineering
patterns, phases, personas, and external resources. Useful edge types include:

- `requires_before` and `requires_after`;
- `implies` and `expands_to`;
- `satisfies` and `evidence_for`;
- `invalidates` and `reopens`;
- `conflicts_with` and `supersedes`;
- `responds_to`, `derived_from`, and `resolves`;
- `applies_pattern` and `forbids_pattern`.

Build on Powdrr's existing structured entity relationships, specification
effects, workflow dependencies, and action declarations, but do not overload
the design-specification graph with ephemeral runtime state. Durable rules may
reference specification entity and relationship IDs; each execution gets a
separate action-instance graph whose completed evidence can later be summarized
back into durable project state.

When the agent proposes a typed action, the kernel enriches it with provenance,
target entities, effect classes, and code/specification relationships, then
computes the applicable relationship closure. The result is a set of ordered
obligations added to execution state. The model may choose how to satisfy an
obligation within its available actions, but it cannot finish the phase while a
required relationship remains open.

For the review-comment convention:

```text
implement_change
  --responds_to--> review_comment:<comment-id>
  --requires_after--> validate_changed_scope
  --requires_after--> resolve_review_comment:<comment-id>
```

The GitHub review tool can safely bind the follow-up to the exact comment ID and
make resolution idempotent. If validation fails, resolution remains blocked. If
the change is reverted, the relationship closes without falsely resolving the
comment. PR readiness includes no implemented review comment with an open
required-resolution edge.

For the optimistic-locking convention:

```text
modify_code --targets--> mutable_database_row
mutable_database_row --requires_pattern--> optimistic_locking
optimistic_locking --expands_to--> version_checked_update
optimistic_locking --expands_to--> version_increment
optimistic_locking --expands_to--> conflict_handling
optimistic_locking --requires_after--> concurrency_test_evidence
```

The trigger should not depend only on the model remembering to label its edit.
Combine the proposed action with specification entities, project structure,
symbol references, database/tool metadata, and the resulting diff. If the
runtime discovers the relationship after an edit, it adds the missing
obligations and marks prior readiness evidence stale. The Architect can also
promote a repeatedly used rule into an explicit architecture invariant so it
becomes part of the durable design source of truth.

Relationship types and closure semantics belong to the kernel. Users and skills
may create validated relationship instances and behavior rules, but cannot
invent an edge meaning that bypasses transitions or evidence. Every generated
obligation should answer “why is this required?” with the source user guidance,
matched relationship path, and affected action or entity.

### 12. Add a validation evidence ledger

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

### 13. Unify tool-call lifecycle and hooks

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

### 14. Feed diagnostics back immediately, then run authoritative gates

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

### 15. Make correction policy depend on typed failure

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
| Tool constraint violation | Choose an in-envelope alternative, correct scope, or request a plan amendment; do not ask a human by default. |
| Capability exception required | Produce the complete exception artifact and wait for an exact scoped decision; never convert approval into ambient authority. |
| Explicit user correction or durable guidance | Apply it to the current action, normalize it into a scoped behavior rule when applicable, and recompute related obligations. |
| Provider/transient infrastructure failure | Retry with provider policy without asking the model to “fix” it. |
| Internal invariant failure | Stop as a product defect with replay/checkpoint data; do not ask the agent to improvise. |

The observer should intervene only after deterministic policy cannot select a
safe correction, or when semantic progress regresses. Its output should remain
guidance; validators and the phase controller stay authoritative.

### 16. Add checkpoints and bounded rollback

Capture a lightweight checkpoint before every mutating action and at every phase
transition. Retain:

- tree fingerprint and changed paths;
- active plan version and execution unit;
- evidence ledger and action-obligation graph versions;
- event boundary.

Expose `revert_action` and `revert_to_checkpoint` as runtime operations, normally
selected by correction policy rather than directly by the model. Reverting must
also restore execution, evidence, action-instance, and obligation state, not only
files. Durable behavior rules remain in force across a revert.

This enables safe experimentation inside an approved unit and prevents a repair
loop from accumulating broken edits.

### 17. Compact from typed state first

Powdrr's current compaction correctly protects the latest actionable failure.
The next step is to make most compaction deterministic:

Always preserve verbatim:

- user intent and proposed PR identity;
- approved execution-plan version;
- current phase and unit;
- IDs and exact text of applicable behavior rules;
- unresolved decisions, user corrections, and rule conflicts;
- open action relationships and their source paths;
- outstanding validation obligations;
- latest failed action and diagnostics;
- current checkpoint and changed-file summary.

Prune or summarize old reads, successful tool output, superseded repair attempts,
and completed unit discussion. Use an LLM compactor only for residual prose, not
to recreate typed state. Skills and specifications should be referenced by ID
and loaded progressively when needed.

### 18. Execute personas as least-privilege child agents

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
role, changing its phase capability envelope, or reimplementing action
correction. Likewise, the Architect can use organization-specific specification
skills while the same specification schemas and evaluators remain authoritative.

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
| `tool_catalog` | Discover tools implementing an available semantic action, then load one manifest and help document. | Yes, read-only and phase-filtered. |
| `record_evidence` | Normalize a deterministic command/review result into the obligation ledger. | Normally runtime-only. |
| `checkpoint` / `revert_checkpoint` | Capture or restore files plus execution, relationship, and evidence state. | Runtime policy first; optionally exposed inside the current worktree envelope. |
| `diagnostics` | Return bounded diagnostics and code locations for touched paths. | Yes in Build/Resolve Findings; automatically run after edits when configured. |
| `symbol` extensions | Definition, references, callers, implementers, and diagnostics using BasedPyright. | Yes in Specify/Decompose/Plan PR/Build as scoped. |
| `scope_diff` | Compare the current tree against approved plan paths and specification effects. | Read-only; automatic at Review PR. |
| `validation_obligations` | List current/stale/failed obligations and exact rerun commands. | Read-only; writes are runtime-only. |
| `remember_guidance` / `revoke_guidance` | Create or supersede a scoped validated behavior rule while preserving its user source. | Yes when the user gives behavioral guidance. |
| `explain_obligation` | Show the rule and relationship path that caused an action or evidence requirement. | Yes, read-only. |
| `resolve_review_comment` | Resolve one exact external review thread after its required change and evidence are current. | Yes in Resolve Findings/Publish; idempotent and thread-scoped. |
| `request_capability_exception` | Materialize a decision-ready request after the broker proves no safe in-envelope route is available. | Injected only after `exception_required`; requires human disposition. |

Every model-callable tool should return a discriminated result such as
`success`, `correctable_error`, `constraint_violation`, `exception_required`, or
`infrastructure_error`, plus stable error code, concise message, structured
effects, and suggested next actions. `PowdrrExecutionError` should carry that
structure end to end.

## Suggested implementation sequence

Each item should be a separate proposed PR with its own acceptance criteria and
rollout switch.

1. **Define the extension boundary.** Add `delivery-profile-v1`, persona,
   assignment, artifact-handoff, review-topology, and behavior-rule schemas.
   Document and test which fields are user-customizable and which changes
   require kernel code. Check in the Architect → Engineering Manager →
   Engineer/Reviewers profile as the default.
2. **Execution schemas and shadow phase state.** Add engine-owned phase,
   transition, execution-unit, action-instance, obligation, relationship,
   evidence, finding, checkpoint, and capability-exception models. Derive and
   log phase and persona assignments during existing executions without changing
   behavior.
3. **Tool manifests and safe capability broker.** Define semantic-action,
   effect, scope, sandbox, reversibility, idempotency, and validation metadata.
   Wrap current edit, shell, Git, GitHub, BasedPyright, and internal tools. In
   shadow mode compare broker decisions with current execution, then remove
   routine approval checks for fully constrained operations.
4. **Decision-ready exception path.** Add the exception artifact, exact scoped
   capability tokens, human disposition UI, audit trail, expiration, and metrics.
   Prove that constraint violations prefer safe correction and that only
   `exception_required` can reach a human.
5. **Persona runner.** Build clean typed input/output packets, model-profile and
   prompt-overlay selection, assignment resume, and capability-brokered child
   contexts. Initially run the Architect and Engineering Manager handoffs in
   shadow mode against existing specification and proposed-PR skills.
6. **Plan generator and evaluator.** Turn the current detailed-execution-plan
   handoff into an `execution-plan-v1` artifact owned by the Engineer, validate
   complete criterion coverage and scope, and retain compatibility with existing
   workflow tasks.
7. **Durable guidance store.** Extract, normalize, scope, version, explain, and
   revoke behavior rules while preserving original user wording. Make clear
   guidance affect the current execution and survive resume without injecting
   all rules into every prompt.
8. **Action-relationship closure.** Add validated relationship types, contextual
   matching, provenance, obligation expansion, conflict handling, and readiness
   gates. Ship review-comment resolution and optimistic-locking scenarios as the
   first end-to-end rules.
9. **Shared action lifecycle and typed errors.** Normalize chat/task events and
   extend `PowdrrExecutionError` with error code, category, details, retryability,
   effects, and suggested recovery. Move response repair, retries, constraint
   correction, and guidance recomputation behind this common kernel interface.
10. **Checkpoints and edit diagnostics.** Wrap mutating handlers, attach diffs
    and fast diagnostics, and implement bounded revert of files, actions,
    obligations, and evidence while retaining durable guidance.
11. **Validation evidence and review findings.** Convert deterministic results
    into evidence, invalidate them after relevant edits or relationships, add
    typed independent reviewer findings and dispositions, and gate PR readiness
    on current evidence, resolved findings, and closed required action edges.
12. **Compile plans into workflow tasks.** Replace generic task reinterpretation
    with generated task slices and obligations. Migrate `execute-proposed-pr`,
    `run-tests-and-fix`, reviewer coordination, comment resolution, PR
    preparation, and PR creation.
13. **Deterministic compaction and compatibility removal.** Preserve persona,
    artifact, phase, guidance, relationship, finding, and evidence state without
    relying on prose summaries. Once checked-in skills and workflows use typed
    handoffs, remove legacy action inference, routine permission prompts, and
    duplicated orchestration/correction instructions from prompts and skill
    prose.

## Validation and rollout

Use the existing workflow replay and tuning direction to compare the current and
new paths against identical fixtures. Required metrics should include:

- valid first-action rate per phase and step;
- valid persona artifact-handoff rate and missing-input rate;
- attempts by a persona or customized profile to exceed its phase envelope;
- percentage of normal tool calls completed without human approval, targeting
  effectively all in-envelope development operations;
- tool constraint violations corrected automatically versus escalated;
- capability-exception frequency, context completeness, scope, and repeated
  effect patterns that should become safe tools;
- durable guidance match rate, missed applicable rules, false-positive matches,
  conflict rate, and successful revocation;
- required action relationships closed before phase and PR completion;
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

Roll out in five stages:

1. Validate delivery profiles, tool manifests, behavior-rule extraction, and
   shadow persona/phase state with no changed decisions.
2. Route current tools through the constrained capability broker, automatically
   run in-envelope calls, and exercise decision-ready exceptions without
   removing current fallbacks.
3. Enforce persona phase envelopes, read-only Architect/Engineering Manager/
   Reviewer product-code boundaries, typed artifact handoffs, and typed Engineer
   plans.
4. Enforce action-relationship obligations, evidence-based Build/Validate
   transitions, independent finding disposition, review-comment resolution, and
   PR readiness.
5. Make layered compiled execution the default and delete routine permission
   prompts, ambient tool exposure, and legacy prompt-only orchestration and
   correction controls.

Deterministic CI scenarios should cover at least: a straightforward proposed PR,
an ambiguous plan requiring user input, an invalid plan, a syntax error after an
edit, a failing full suite, a stale-evidence edit, scope expansion, an
automatically corrected tool constraint violation, a genuinely exceptional
operation with complete human context, a denied exception, a provider retry,
compaction during repair, checkpoint revert, and resume after process
interruption.

Behavioral-memory scenarios should include the two motivating cases. One should
record the review-comment rule, apply a comment-driven change, withhold comment
resolution until validation passes, resolve the exact thread, and remember the
rule after resume. The other should record the optimistic-locking rule, detect a
mutable-row change even when the edit action omits the label, require the
pattern and concurrency evidence, and explain the originating user instruction.

Add profile-conformance scenarios proving that users can change persona prompts,
models, skills, validation commands, and reviewer composition without changing
kernel behavior. Add negative scenarios proving that a profile cannot expose an
edit action to a Reviewer, bypass a transition, suppress a failing obligation,
change retry semantics, register an unvalidated tool effect, broaden a capability
exception, create a relationship type with unknown semantics, or mark an
unresolved blocking finding as complete.

## Risks and safeguards

- **Too much ceremony for small changes.** Allow a compact plan with one unit
  and auto-approval, but never skip criterion coverage or validation evidence.
- **Customization leaks into execution mechanics.** Keep delivery-profile fields
  declarative, resolve them through a closed set of phase types, reject unknown
  kernel fields, and test that profiles cannot expand capability envelopes or
  redefine success.
- **Personas become decorative prompt labels.** Give every assignment a typed
  input, typed output, artifact ownership, capability envelope, and independent
  event identity. Measure cross-persona handoff quality and capability leakage.
- **Safe tools create false confidence.** Treat manifests as executable safety
  contracts, test effect boundaries and sandbox escapes, checkpoint mutations,
  and keep external identifiers and idempotency keys explicit.
- **The tool catalog grows into another ambient shell.** Require semantic action
  mappings and effect manifests for every operation; keep arbitrary host access
  on the exception path and use repeated exceptions to prioritize typed tools.
- **Exception prompts become routine permissions by another name.** Track their
  frequency, reject requests without alternatives and bounded scope, and require
  a tool proposal when the same effect pattern recurs.
- **Guidance is overgeneralized or remembered at the wrong scope.** Preserve the
  exact source, choose the narrowest sufficient scope, make matches explainable,
  support supersession/revocation, and ask only on material ambiguity.
- **The relationship graph produces obligation explosions or cycles.** Keep a
  closed relationship ontology, validate acyclicity where ordering requires it,
  deduplicate closure, cap diagnostic expansion, and expose the minimal source
  path for every obligation.
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
- Do not make safety a permission prompt in front of ambient tools. Enable a
  powerful catalog through explicit actions, effect manifests, bound scope,
  sandboxing, checkpoints, idempotency, and automatic evidence.
- Do not give general subagents ambient edit capability when a child has a
  narrow role and active execution unit.
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
  -> capability broker exposes powerful in-scope tools without human approval
  -> hardened kernel validates actions, checkpoints edits, expands applicable
     remembered guidance into related obligations, and records evidence
  -> Specification Reviewer and Code Reviewer return independent typed findings
  -> Engineer resolves blocking findings and the exact originating review threads
  -> deterministic readiness gate confirms fresh evidence, resolved findings,
     and closed required action relationships
```

The profile may change the persona prompts, selected skills, model profiles,
validation commands, or add another Reviewer without altering the kernel path.
That is the acceptance test for the separation. Once the slice is reliable,
expand the same contracts across the full feature and `execute-proposed-pr`
flows and remove duplicated orchestration, correction, and retry instructions
from skills and prompts.

The companion safety acceptance test is that the vertical slice completes
ordinary development without a permission prompt, while an intentionally
out-of-envelope external or destructive effect produces one bounded,
decision-ready exception request. The companion memory acceptance test is that
a user rule survives interruption, applies only when its relationship trigger
matches, changes readiness behavior, and can explain and revoke itself.
