# Procedrr-Only Workflow Runtime

## Decision

`workflow-chat` and the durable task agent will execute Procedrr definitions
only. The legacy `skill-definitions/` language, its parser/compiler, and the
second step-control implementation are migration inputs, not a supported
runtime format. Do not add new legacy definitions or extend legacy execution
while this migration is in progress.

This is a runtime migration, not a deletion of workflow chat or durable task
processing. User interaction, durable workflow/task records, Git operations,
provider clients, and tool adapters remain where they are useful; Procedrr owns
the declared process, LLM decision boundaries, branching, looping, and step
progression.

## Current boundary

- The feature endpoint already parses and evaluates declarations under
  `docs/procedrr/skill-definitions/` with `procedrr` and
  `procedrr_evaluator`.
- `workflow-chat` still loads `skill-definitions/` through
  `powdrr_lift.process.model` and runs its own step/action interpreter.
- The durable task agent also loads those skills and implements parallel
  branch, action, prompt, retry, and progression logic. Workflow task documents
  and task persistence are separate contracts and are not themselves the old
  skill language.
- Procedrr's evaluator currently runs a complete declaration and returns
  bindings/events. The chat and durable-task integrations must preserve their
  respective persistence, suspension/resumption, event recording, and tool
  authorization contracts when adopting it.

## Target architecture

1. A single Procedrr catalog loads, parses, and validates process definitions.
   Selection returns a stable process identifier and typed input bindings; it
   does not construct or mutate a legacy `Skill` object.
2. One Procedrr execution service is the only owner of process control flow.
   It invokes the evaluator with a caller-scoped operation executor and
   provider/judge clients, and emits typed events suitable for replay and
   durable persistence.
3. `workflow-chat` remains responsible for the user-facing conversation,
   collecting missing declared inputs, choosing among available process
   definitions, presenting results, and requesting explicit continuation. It
   must not independently interpret legacy steps, actions, branches, or nested
   skills.
4. The durable task agent remains responsible for claiming a ready task,
   checking dependencies and repository state, binding task inputs, recording
   task outcomes, and handing off human work. Each task delegates its declared
   process to the shared Procedrr execution service. The task agent must not
   maintain a second LLM-driven process program counter.
5. Procedrr operations are resolved through the existing command/capability
   catalogs and the caller's existing authorization boundary. A process
   declaration cannot grant itself broader filesystem, Git, network, or
   publication authority.
6. A suspended execution stores the validated process identity/version,
   bindings, evaluator state needed to resume, and event cursor. Resumption
   revalidates the definition and external preconditions; stale or incompatible
   state fails closed rather than silently restarting a different process.

## Migration sequence

### 1. Establish the shared runtime contract

- Define typed process identity, initial bindings, execution events, terminal
  status, and resumable state at the Procedrr boundary.
- Add a shared Workrr adapter that maps Procedrr operations to existing
  authorized handlers and routes named judges to configured providers.
- Prove event replay and suspension/resumption without invoking a live provider
  in deterministic tests.

### 2. Migrate workflow chat

- Replace legacy skill discovery and selection output with the Procedrr
  catalog and declared process inputs.
- Run the selected process through the shared runtime; retain the chat UI and
  existing repository/tool authorization.
- Port supported user questions, nested process calls, and terminal outcomes
  to explicit Procedrr operations/control nodes.
- Convert each still-supported root `skill-definitions/` workflow to a
  Procedrr definition and add behavior-equivalent scenario/replay coverage.

### 3. Migrate durable task execution

- Change the task-to-process reference to a validated Procedrr process ID and
  typed bindings; preserve the task document's independent dependency,
  assignment, claim, and completion semantics.
- Delegate task execution to the shared runtime and persist/resume its
  evaluator state with the durable task record.
- Verify retries, stale worktree checks, human handoffs, nested work, and
  cancellation at the task/runtime boundary.

### 4. Remove the old language and parallel interpreter

Only after both callers use Procedrr exclusively and their existing behaviors
have migration coverage:

- remove the legacy `Skill`/`SkillStep` schemas, parser/serializer,
  validation, catalog, compiler, action/branch/liveness machinery, and
  `skill-definitions/` YAML/JSON files;
- remove the old workflow-chat/task-agent step interpreters and their
  definition-oriented CLI options/aliases;
- remove legacy-only fixtures, tests, generated baselines, package-data
  settings, and documentation; retain durable task/template modules that have
  independent consumers;
- update documentation to identify Procedrr as the sole process-definition
  language and move still-useful historical plans to an explicitly archived
  location or delete them when they have no remaining normative content.

## Acceptance criteria

- Both `workflow-chat` and durable task execution accept only validated
  Procedrr definitions; a legacy file is rejected and no legacy fallback is
  available.
- The two entry points use the same evaluator/control semantics and
  operation-authorization boundary.
- Existing user interaction, task lifecycle, replay, persistence, and
  human-handoff behavior is covered by deterministic integration scenarios.
- There is one source of truth for workflow branches, repeats, nested calls,
  decision schemas, and terminal status.
- No production imports, CLI paths, packaged files, test fixtures, or active
  docs refer to the removed skill language after the final migration change.
- Full tests, format, lint, type checking, module-boundary checks, and Procedrr
  definition validation pass before the final removal is merged.

## Removal evidence

Static analysis also found and removed independent dead code while this
migration is prepared: the unreferenced `OpenCodeReviewClient`, an unused
validation parameter, and two unreachable branches in chat transport. The
unreachable backup-model diagnostic was restored to its intended reachable
path; the obsolete post-`continue` retry prompt was deleted.
