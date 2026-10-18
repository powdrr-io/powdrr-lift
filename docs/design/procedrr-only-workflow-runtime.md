# Procedrr-Only Workflow Runtime

## Decision

`workflow-chat` and the durable task agent execute validated Procedrr
definitions only. The legacy definition catalog, parser, compiler, interpreter,
and validation CLI have been removed. Do not add another workflow-definition
format or execution path.

This is a runtime migration, not a deletion of workflow chat or durable task
processing. User interaction, durable workflow/task records, Git operations,
provider clients, and tool adapters remain where they are useful; Procedrr owns
the declared process, LLM decision boundaries, branching, looping, and step
progression.

## Current boundary

- The feature endpoint already parses and evaluates declarations under
  `docs/procedrr/processes/` with `procedrr` and
  `procedrr_evaluator`.
- `workflow-chat` loads the validated Procedrr catalog and runs processes
  through the shared `ProcedrrExecutionService`; Workrr continues to own the
  user-facing conversation, worktree context, and operation authorization.
- The durable task agent loads the same Procedrr catalog and executes through
  the shared service. Workflow task documents retain their independent
  dependency, assignment, claim, and completion contracts.
- Procedrr persists resumable evaluator checkpoints. Durable task manifests and
  event journals retain the process fingerprint, checkpoint, and event cursor
  across human handoffs.

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

Status: migrated. Chat selects from the validated process catalog and runs the
selected process through the shared service. Every supported root workflow has
a catalogued Procedrr counterpart, the old root definitions have been removed,
and the active CLI no longer exposes the legacy validation commands.

- Replace legacy skill discovery and selection output with the Procedrr
  catalog and declared process inputs.
- Run the selected process through the shared runtime; retain the chat UI and
  existing repository/tool authorization.
- Port supported user questions, nested process calls, and terminal outcomes
  to explicit Procedrr operations/control nodes.
- Keep deterministic scenarios aligned with Procedrr operation and terminal
  events; legacy scenario fixtures remain part of the removal work.

### 3. Migrate durable task execution

Status: migrated. Durable tasks execute the declared Procedrr process, persist
event journals and checkpoints, and resume after a human response.

- Task records identify a validated process ID and preserve independent
  dependency, assignment, claim, and completion semantics.
- The shared runtime persists and resumes evaluator state with the durable
  task record.
- Deterministic scenarios cover retries, stale worktree checks, human handoffs,
  nested work, and task completion.

### 4. Remove the old language and parallel interpreter

Status: complete. The legacy skill parser, catalog, compiler, action and branch
machinery, action protocol, chat/task interpreters, and their dedicated tests
are removed. The active CLI accepts Procedrr process catalogs and durable task
documents only. Durable task and template contracts remain because they retain
independent lifecycle and persistence responsibilities.

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
  definition validation pass before the migration is published.

## Removal evidence

Static analysis also found and removed independent dead code while this
migration is prepared: the unreferenced `OpenCodeReviewClient`, an unused
validation parameter, and two unreachable branches in chat transport. The
unreachable backup-model diagnostic was restored to its intended reachable
path; the obsolete post-`continue` retry prompt was deleted.
