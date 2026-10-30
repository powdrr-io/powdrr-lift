# Browser workspace

Build a local web application with a familiar LLM chat layout that works in
browsers on macOS, Linux, and Windows. The local backend owns repository access,
credentials, execution, and recovery. Closing a tab must not stop a job.

## Interface

The left sidebar has design conversations in its upper section and autonomous
jobs below. Jobs have Running and Ready/Pending tabs. Selecting a conversation
shows its messages and composer on the right; selecting a job shows its stage,
recent activity, dependencies, questions, results, and issue or PR links. Completed
jobs remain accessible through history. Provide keyboard navigation, Markdown and
code rendering, clear unread/input indicators, and light/dark appearance.

## Delivery

1. **Durable workspace records:** conversation metadata and messages, job states,
   dependencies, artifact links, and replayable events in local storage.
2. **Local API and execution adapters:** connect the existing Procedrr interview,
   planning, and PR execution flows to the records. Add a worker supervisor and
   event streaming with reconnect cursors and idempotent submissions.
3. **Chat interface:** implement the sidebar, persistent chat selection, transcript,
   interview answers, draft composer, and specification references.
4. **Job interface:** implement the Running and Ready/Pending tabs, progress,
   blockers, human input, results, and history.
5. **Launch and recovery:** package assets, provide one launch command, and verify
   installation, execution, and recovery on all three operating systems.

The initial quality target is internal beta. Workflow policy remains in Procedrr
and Structrr. Workspace status updates must not add GitHub polling or checkpoint
writes. Existing local workflow checkpoints remain authoritative for recovery.

## First increment: persistence

`powdrr_contracts.workspace` defines conversation, message, job, and event records.
`powdrr_local.workspace.WorkspaceStore` persists them in a caller-selected SQLite
database. This increment is a storage API; the existing CLI and TUI will start
writing these records when execution adapters are introduced in increment 2.

Conversations belong to a repository, have editable titles and specification
references, and retain messages in submission order. A caller supplies the message
ID so a repeated submission records one message and event. Reusing the same ID
with different content fails instead of silently replacing history.

Jobs have stable identities, a repository, optional source conversation,
dependencies, artifact references, current detail, and a revision. Planning and
PR execution are the initial job kinds. Dependencies must exist in the same
repository and are immutable; new jobs can depend only on existing jobs, so this
API cannot create dependency cycles.

| State | Meaning | Allowed next states |
| --- | --- | --- |
| pending | Waiting for dependencies | ready automatically after all prerequisites succeed; cancelled |
| ready | Eligible to start | running; cancelled |
| running | Claimed by a worker | waiting_for_input; interrupted; succeeded; failed; cancelled |
| waiting_for_input | Requires a user response | ready after the response is recorded; cancelled |
| interrupted | Worker stopped before a final outcome | ready after recovery is approved by the supervisor; cancelled |
| failed | Attempt ended unsuccessfully | ready for an explicit retry; cancelled |
| succeeded | Completed successfully | terminal |
| cancelled | Cancelled | terminal |

Progress can update detail and artifact links without changing an active state.
Every update checks the expected job revision inside a write transaction, so two
workers cannot both claim the same ready job. A dependency failure keeps its
dependents pending; the UI must present the failed dependency as their blocker.

The UI should group `running` and `waiting_for_input` under Running, with a visible
input-needed indicator. Ready/Pending contains eligible jobs and jobs waiting for
dependencies. Interrupted and failed jobs need visible recovery actions in job
history. Increment 2 owns reconciliation with actual worker processes: opening a
database connection never marks jobs interrupted or resumes execution.

Record mutations and their events commit atomically. Events have a monotonically
increasing database sequence, repository scope, entity identity, type, timestamp,
and JSON payload. Consumers reconnect using an exclusive sequence cursor and read
bounded batches. Events remain available across restarts; retention is deferred
until consumers have a defined snapshot/replay protocol. Conversation and job
records provide the current snapshot; execution checkpoints are separate.

SQLite uses WAL mode, foreign keys, transactional schema initialization, and
versioned schemas. There is no platform-specific file locking or external database
service. Cross-platform installation and worker lifecycle verification remain part
of increment 5.

## Detailed implementation instructions for the remaining increments

### Status, scope, and working rules

The storage code above exists in this PR. Everything below is planned work, not
an assertion that the browser application or its execution adapters already exist.
Implement the work packages in the order below. Each package should be its own
reviewable PR; packages 2A–2C together deliver the original second increment.
Do not merge your own PR. Build dependent work from the reviewed predecessor or
use explicitly documented stacked branches while review is pending.

The target is a local, single-user internal beta. Support multiple registered
repositories in one application, with one repository selected in the UI at a time.
Use the following initial technology choices:

- Python FastAPI and Uvicorn for the local HTTP application and streaming API.
- React with TypeScript and Vite for the browser application, in `ui/`.
- Plain CSS with shared theme variables for layout, spacing, and colors.
- SQLite through `WorkspaceStore` for workspace records and durable events.
- HTTP requests for commands and Server-Sent Events (SSE) for server updates.
- Playwright for browser workflow verification; pytest for backend behavior.

These are implementation defaults chosen for this plan. Select mutually compatible
stable dependency versions when creating each package, commit lockfile changes,
and document the supported Python and Node versions. Users of the packaged
application must not need Node. Do not add Electron, a remote hosting service,
Redis, another database, or a second workflow engine for this release.

Keep Python contracts in `packages/contracts/src/powdrr_contracts/`, local storage,
supervision, and HTTP composition in `packages/local/src/powdrr_local/`, repository
capabilities in the existing agent package, and workflow policy in its existing
service/Procedrr owner. Do not move proprietary policy into the browser. Add CLI
launch wiring in `src/powdrr_lift/cli.py`. The proposed new module names below may
be split into smaller files, but preserve these ownership boundaries.

Before changing an entry point, read its implementation and its existing tests.
Use this inventory as the starting point:

| Existing code | Reuse or adaptation needed |
| --- | --- |
| `packages/local/src/powdrr_local/workspace.py` | Persist UI snapshots and events; extend with versioned migrations rather than replacing the database. |
| `packages/contracts/src/powdrr_contracts/workspace.py` | Extend records with versioned JSON serialization and validation. |
| `src/powdrr_lift/workrr/procedrr_chat.py::run_procedrr_workflow_chat` | Run the existing interview with structured questions, responses, progress, and conversation-scoped context. |
| `src/powdrr_lift/workrr/chat_tui.py` | Reference existing input/progress behavior; its console text parsing is not the browser API. |
| `src/powdrr_lift/workrr/chat_config.py::WorkflowChatConfig` | Carry existing provider configuration and execution identity into chat runs. |
| `src/powdrr_lift/cli.py::_run_plan_session` | Extract reusable application orchestration from argument parsing and terminal presentation. |
| `packages/local/src/powdrr_local/planning/checkpoints.py::LocalCheckpointStore` | Preserve planning checkpoint and retry behavior. |
| `src/powdrr_lift/cli.py::_run_github_issue_worker` | Reuse PR-execution orchestration and existing attempt identities. |
| `src/powdrr_lift/workrr/github_issue_queue.py::run_worker_once` | Preserve issue claims, dependencies, publication, and failure write-back. |
| `packages/local/src/powdrr_local/bridge.py::JsonServiceBridge` | Preserve the validated service boundary where an operation is already supported. |
| `src/powdrr_lift/execution/runtime.py::ExecutionRuntime` | Preserve execution state, typed events, and recovery policy. |

The service protocol alone does not establish that every operation is implemented
by the current local service. Inspect the adapter before using an operation. Do
not simulate success for an unsupported operation or call a new LLM directly from
an HTTP route to work around missing orchestration.

### Package 2A: complete contracts, migrations, and portable persistence

**Purpose:** make the existing storage usable by a reconnecting UI and by durable
workers without introducing a second source of execution truth.

1. Add explicit `to_dict`/`from_dict` validation for workspace API records. Define
   enums for job kind, message role, turn state, question state, and event type.
   Reject unknown schema versions, malformed IDs, invalid states, non-finite JSON
   numbers, and unexpected fields in command payloads. Keep contracts independent
   of FastAPI and browser code.
2. Add a repository registry containing an opaque ID, display name, and canonical
   local root. The user supplies a local path through a setup form or CLI option.
   Validate that it is a Git repository and resolve symlinks. Return the opaque
   repository ID to the browser; resolve it to a path in the backend for every
   operation. Do not accept arbitrary filesystem paths on job-action routes.
3. Add migrations from the existing database version 1. Perform each migration in
   a transaction and advance `user_version` only after it succeeds. Preserve
   existing conversations, messages, jobs, IDs, timestamps, and event sequences.
   Refuse an unknown newer version with an actionable error.
   Add revisions to editable conversation metadata as well as jobs so two browser
   windows cannot overwrite a renamed chat or its spec links without a conflict.
   Extend the job state model with `waiting_for_external` for an actual workflow
   pause on PR review/merge or an external prerequisite. Permit running to enter
   this state, and permit cancellation or return to ready only after the existing
   workflow continuation validates the prerequisite. Include it in the Running
   tab with a visible blocker. A browser answer cannot stand in for a required
   GitHub merge or successful validation.
4. Add durable operation bindings: conversation/job ID to run ID, attempt ID,
   workflow directory, checkpoint reference, and source specification revision.
   Persist validated job inputs and immutable source references before dispatch.
   Planning input must identify the pinned spec; PR execution must identify the
   approved proposed PR/issue, rather than whichever document happens to be latest.
   Persist provider configuration references, never API keys or other secrets in
   job input records; resolve credentials through the existing backend configuration.
5. Add request deduplication. Every mutating HTTP command will have a client request
   ID. Store its canonical payload digest and result in the same transaction as
   the mutation. An identical retry returns the original result. Reuse with a
   different payload returns a conflict. This must cover creating chats/jobs,
   starting work, answering questions, retrying, resuming, and cancelling.
6. Add conversation turns separately from messages. A turn records its user message,
   execution binding, state, and final assistant response or error. Permit only
   one active turn per conversation. An unanswered interview question belongs to
   that turn; submitting its answer continues the turn instead of starting an
   unrelated agent run. Other conversations can progress independently.
7. Persist questions and answers using question IDs and revisions. Store prompt,
   allowed response shape, originating run/attempt, status, answer, and delivery
   receipt. A stale or duplicate answer must not advance a run twice. If the process
   crashes after storing an answer but before delivering it, recovery can deliver
   it to the same continuation once.
8. Add worker ownership records: supervisor generation, worker identity, attempt,
   heartbeat/lease expiry, process identity, cancellation request, and dispatch
   intent. Add a supervisor lease per data directory so a second launcher does
   not start a second scheduler against the same database.
9. Provide an atomic repository snapshot read returning conversation summaries,
   job summaries, active questions/turns, and a high-water event cursor from one
   SQLite read transaction. Add pagination to transcripts, job history, events,
   and conversation lists. Do not load an unbounded history for each refresh.
10. Define event envelopes with schema version, sequence, repository ID, entity
    ID/type, event type, timestamp, attempt/revision where applicable, and payload.
    Publish either a complete updated projection or enough typed fields for a
    deterministic merge. Never emit browser events by parsing console strings.
11. Separate committed messages from optional token previews. If the existing
    provider adapter can stream tokens, use a turn-local preview ID and replace
    it with one committed message on completion. Do not require token streaming
    or persist an event for every token to finish the beta.
12. Introduce a portable lock adapter for existing checkpoint/runtime stores.
    Audit imports of `fcntl` in planning checkpoints, execution store, behavior
    rules, Structrr intent, and the GitHub issue queue. Use one maintained locking
    implementation with supported Windows and POSIX behavior; preserve existing
    exclusive-lock scope, atomic replacement, and crash release semantics.
    Avoid a broad catch that silently disables locking on Windows. Place the
    primitive in a shared infrastructure location allowed by package boundaries.

**Meaningful verification:** reopen an existing version-1 database after migration;
force migration failure and verify rollback; race two start commands with the same
request ID; race two worker claims; reconnect between a snapshot and a new event;
recover a stored but undelivered answer; reject cross-repository links. Exercise
checkpoint locking with two processes on Windows and POSIX, not only two threads.

**Done when:** typed records can represent a complete resumable UI interaction,
all mutations are idempotent at the command boundary, and importing the required
execution/persistence modules works on Windows without disabling locks. Existing
CLI/TUI and checkpoint tests must still pass.

### Package 2B: local HTTP API and event replay

**Purpose:** expose the application boundary without running workflow code in
request handlers. Add `powdrr_local.web` and `powdrr_local.workspace_api` modules.

1. Create an application factory that accepts a store, repository registry, command
   dispatcher, and event source. Tests supply real temporary storage and a
   deterministic dispatcher. Do not construct global services at import time.
2. Bind to loopback only for this release. Serve frontend assets and API from the
   same origin in packaged mode. Restrict allowed hosts/origins and require a
   random local session secret for API access. Deliver the secret via a one-use
   browser bootstrap exchange, then an HttpOnly same-origin session cookie; remove
   the bootstrap credential from browser history. Check origin and CSRF protection
   on mutating requests. Do not enable wildcard CORS. Provider keys stay server-side.
3. Implement the following routes under `/api/v1`. Every entity route checks the
   selected repository association, even when an ID is globally unique.

| Route | Behavior |
| --- | --- |
| `GET /health` | Report application/schema compatibility and scheduler health; no credentials or prompt content. |
| `GET/POST /repositories` | List or explicitly register local repositories. |
| `GET /repositories/{repo}/snapshot` | Return the atomic UI snapshot and event cursor. |
| `GET/POST /repositories/{repo}/conversations` | List or create design chats. |
| `GET/PATCH /repositories/{repo}/conversations/{chat}` | Fetch or rename a chat and update permitted metadata using its revision. |
| `GET /repositories/{repo}/conversations/{chat}/messages` | Read a bounded page of committed messages. |
| `POST /repositories/{repo}/conversations/{chat}/messages` | Persist the user submission/turn and dispatch intent; return acceptance and turn ID. |
| `GET/POST /repositories/{repo}/jobs` | List/filter jobs or create one from validated pinned inputs. |
| `GET /repositories/{repo}/jobs/{job}` | Return status, attempt, stage, blockers, question, outputs, and allowed actions. |
| `POST /repositories/{repo}/jobs/{job}/actions` | Accept `start`, `cancel`, `retry`, or `resume` with request ID and expected revision. |
| `POST /repositories/{repo}/questions/{question}/answers` | Persist a validated answer for the identified question/run. |
| `GET /repositories/{repo}/events` | Stream ordered SSE updates after the supplied cursor. |
| `GET /repositories/{repo}/artifacts/{artifact}` | Read a registered specification or result within its allowed repository/worktree scope. |

4. Define success/error response shapes in the contracts package and document
   example requests. Use 201 for creation, 202 for accepted asynchronous work,
   400/422 for malformed input, 401/403 for rejected access, 404 for unknown IDs,
   409 for stale revisions or conflicting request IDs, and 503 for a temporarily
   unavailable supervisor. Return a stable error code and useful user message.
   Use these command envelopes consistently (IDs below are placeholders):

   ```json
   {"request_id":"client-uuid","message_id":"message-uuid","content":"Add search"}
   ```

   ```json
   {"request_id":"client-uuid","expected_revision":3,"action":"start"}
   ```

   A message submission returns the persisted message and turn ID/state, not a
   promise that the assistant has finished. A stale job response should use an
   error envelope such as the following, with the current revision available for
   refresh; do not expose a Python traceback:

   ```json
   {"error":{"code":"stale_revision","message":"This job changed in another window.","current_revision":4}}
   ```
5. Do not return 202 until the request and dispatch intent are durable. A network
   disconnect after acceptance must not discard the work. A disconnected SSE
   subscriber must not cancel a turn or worker.
6. Start SSE from the snapshot cursor, exclusive. Set the SSE event ID to the
   durable sequence. Honor `Last-Event-ID` on reconnect, bound batches, send
   heartbeat comments, and release resources on disconnect. Use one shared
   backend notification mechanism plus bounded local reads; do not poll GitHub
   for UI updates. A slow subscriber disconnects and replays later instead of
   causing an unbounded in-memory queue.
7. Retain durable events for the beta. If an unknown/future cursor is received,
   require a fresh snapshot with an explicit resync response. Do not silently
   skip events. Add a documented retention/resnapshot protocol before pruning.
8. Reuse the application's event loop for HTTP and streaming. Send blocking Git,
   database, and workflow work to the appropriate worker/thread boundary; a long
   model call must not block another browser request.
9. Provide bounded sanitized diagnostics and registered artifact links. Serve
   Markdown as data; block path traversal and arbitrary local-file reads. The
   browser cannot request a shell command through a generic HTTP endpoint.

**Meaningful verification:** use an actual HTTP client against the app with a
temporary store. Drop the response after a successful POST and retry it; establish
a snapshot then reconnect SSE while new events arrive; close one browser/client
while another continues; verify stale answers and revisions return 409. Verify
foreign origins and artifact path traversal are rejected without launching work.

**Done when:** a client can create a chat, submit a turn, create/start a job, inspect
records, and reconnect to ordered progress using deterministic adapters. This
package must not report mock adapters as working production execution.

### Package 2C: worker supervision and real workflow adapters

**Purpose:** make the API drive the existing design interview, planning, and PR
execution flows with durable ownership and structured progress.

1. Extract reusable application functions from `_run_plan_session` and
   `_run_github_issue_worker`; keep CLI parsing/printing in the CLI. Preserve
   `JsonServiceBridge` calls, existing provider configuration, retry policies,
   worktree creation, unique branch names, and mini-SWE model selection. Both
   CLI and web adapters should call the extracted application functions.
2. Add structured callback/protocol boundaries for assistant messages, questions,
   stage changes, artifact creation, retry notices, and final outcomes. Adapt the
   terminal presentation to these callbacks where needed. Keep stderr logs as
   diagnostics; do not derive job success or progress from matching log text.
3. Make `run_procedrr_workflow_chat` accept conversation-scoped persisted context
   and an explicit initial message. Replace the browser path's blocking `input()`
   boundary with durable question/answer handling. Locate the current context
   loader/writer before editing: different chats in the same repository must
   never share their mutable interview state or resume each other's question.
   A saved transcript alone is insufficient to resume an active Procedrr run.
4. Use the existing design-interview process, `gather_context`, module-scoped
   Structrr decisions, and specification compiler. Preserve the requested quality
   level, delivery order, legacy-behavior expectations, and defaults marked for
   later review. Associate generated spec revisions with the source conversation.
   Resume through saved interpreter state; do not replay the transcript by
   resending every old message as new input.
5. Start workers with a portable spawn/subprocess entry point and structured IPC.
   Never use `fork` as a requirement. Persist a claim and attempt binding before
   executing external work. Include run, job, attempt, and supervisor generation
   in worker messages. Reject updates from an old attempt/generation.
   Define an authenticated loopback control channel with a per-attempt nonce so a
   restarted supervisor can verify and reattach to a surviving worker. Persist the
   channel identity in local ownership metadata with restricted access. Reattachment
   must acknowledge the new supervisor generation before it accepts new commands.
   If reattachment is unavailable, confirm the old worker has stopped before
   resuming from its checkpoint; do not guess from a PID or launch a second writer.
6. Initial scheduling defaults: at most two active executions globally and one
   repository-mutating execution per repository. Separate design interviews may
   wait for input independently without consuming an LLM/execution slot. Use
   dedicated worktrees and conversation-specific output directories for edits.
   Queue same-repository edits when necessary; do not share a process-wide cwd,
   global input queue, stdout replacement, or mutable workflow-context file.
7. A newly created job appears Ready/Pending. Starting a ready job sets durable
   dispatch intent. Provide explicit enqueue intent for a pending job; it may
   dispatch after prerequisites succeed. Merely loading or highlighting a job
   never starts it. Pause automatic dispatch on supervisor startup until ownership
   reconciliation completes; expose scheduler state to the UI.
8. Translate workflow outcomes accurately. A returned error, retry in progress,
   human question, cancelled run, and exhausted failure are distinct. Persist
   meaningful named stages and completed/total units only when supplied by the
   workflow; do not invent a progress percentage. A transient provider/service
   retry stays running with a retry notice until the existing retry budget ends.
9. Preserve local checkpointing. Link to the existing checkpoint/run and project
   its status into workspace records. Preserve the catch-all GitHub failure
   write-back after retry exhaustion. UI polling, heartbeats, and SSE publication
   must not write extra GitHub issue checkpoints or trigger issue status reads.
   Instrument CLI-launched planning/PR jobs through the same recorder and data
   directory so the UI lists their live status too. Record whether a process is
   supervisor-owned or externally launched. Show external runs read-only unless
   their control adapter supports the requested action. Discover older local
   checkpoints once at startup for recovery/history; a checkpoint without verified
   live process ownership must not be labelled Running. Do not invent live jobs
   from stale GitHub issue labels or repeatedly scan Git history for UI refreshes.
10. Preserve the execute-PR flow for plan alterations and implementation PRs.
    Record both artifact kinds distinctly. An alteration PR awaiting review is a
    blocker or continuation, not successful completion of the requested feature.
    Preserve existing review feedback handling and original scope constraints.
11. Implement cancellation as a request with a visible `cancelling` activity while
    the authoritative state remains running. Stop at safe boundaries, persist
    available checkpoints, then mark cancelled after the worker confirms exit.
    Closing a browser or changing selection must never send cancellation.
12. Handle process trees explicitly on POSIX and Windows, including coding-agent
    descendants. Use owned process handles/groups and cooperative shutdown first.
    On escalation, record the outcome and preserve recovery data. Never kill a
    process solely because its PID matches a stale database row. If worker exit
    cannot be confirmed, block resumption and expose an actionable recovery state.
13. On backend restart, acquire the supervisor lease, inspect saved worker
    identities/leases, and reconcile only owned attempts. A live owned worker
    remains running; a confirmed dead one becomes interrupted. Fence obsolete
    workers and confirm exit before another attempt can edit the same worktree.
    Expired heartbeats alone are not proof that an external mutation stopped.
14. Resume interrupted jobs only through the existing checkpoint recovery path.
    Validate repository/worktree identity, pinned inputs, and checkpoint integrity.
    A missing/corrupt checkpoint must produce a clear blocked recovery outcome;
    do not silently restart from scratch. Before retrying publication, reconcile
    existing commits/issues/PRs so a crash after an external success does not
    produce a duplicate PR. Explicit retries create a new attempt linked to the
    prior attempt; retain its events and diagnostics.

**Meaningful verification:** use deterministic in-process providers and real local
Git repositories. Complete an interview to a validated spec, generate a plan from
that exact revision, and execute a scoped proposed PR through existing adapters.
Stub external GitHub effects at the existing boundary and assert publication is
invoked once across a crash/retry. Run two chats in one repo and prove isolation.
Kill a worker after checkpointing, restart the supervisor, and resume the same run.
Exercise human-input delivery and cancellation while another job remains active.
Add one opt-in live-provider smoke run with bounded scope and explicit credentials.

**Done when:** a real design interview, planning run, and PR execution can be driven
through the HTTP API and recovered after interruption. Terminal workflows retain
their existing behavior and retry/checkpoint semantics.

### Package 3: browser shell and design conversations

**Purpose:** deliver the first usable graphical milestone: persistent design
interviews in the familiar chat layout.

1. Create `ui/` with React, TypeScript, Vite, npm lockfile, type checking, linting,
   and build commands. Keep provider configuration, credentials, prompts, and
   workflow logic out of frontend bundles. Use typed API DTOs derived from the
   backend schema; verify schema generation rather than manually maintaining a
   conflicting second definition of job states.
2. Suggested modules: `api/client.ts`, `api/events.ts`, `state/workspace.ts`,
   `components/WorkspaceShell.tsx`, `ConversationList.tsx`, `ConversationView.tsx`,
   `MessageList.tsx`, `MessageComposer.tsx`, and `QuestionCard.tsx`. Components
   render state and dispatch typed user actions; API calls live in the client.
3. Build a full-height shell. On desktop use an initially 300-pixel sidebar with
   a resizable boundary (240–420 pixels). Put New design chat, search, and chat
   list in its upper section; reserve its lower section for the job panel added
   in package 4. The content pane fills the remaining width. Keep repository and
   settings controls at the sidebar bottom.
4. Use a subtle sidebar background, clear active-row highlight, readable text,
   subdued timestamps, and restrained borders. Center the conversation content
   at a comfortable reading width of roughly 850 pixels while allowing code
   blocks horizontal scrolling. Keep the composer anchored to the content pane
   bottom, with sufficient transcript padding so it never covers the last message.
5. On first launch show repository setup. After registration, create/select chats
   in that repository. Display title, last activity, active-turn status, and an
   input-needed/unread indicator for each row. Allow rename from a row menu.
   Filter chat titles locally within the loaded page; do not imply full-transcript
   search unless a backend search endpoint is implemented.
6. Put repository and selected chat/job IDs in a reloadable URL. Use browser
   history for selection and back/forward navigation; do not put prompt text or
   credentials in URLs. Preserve selection on reconnect, and choose a clear empty
   state if the selected entity no longer exists.
7. Load the snapshot and then subscribe after its cursor. Store normalized records
   keyed by ID. Apply events in sequence, ignore duplicates/older revisions, and
   fetch a new snapshot on a resync response. Use one repository event connection
   for the entire shell instead of one connection per list item.
8. Render user and assistant messages with clear roles, Markdown, lists, and
   fenced code blocks with language labels and copy buttons. Disable raw HTML
   rendering or sanitize it rigorously; restrict links to safe schemes. Tool
   logs belong in expandable activity details, not the main assistant answer.
9. Composer behavior: multiline text, Send button, Enter to send, Shift+Enter for
   newline, and correct IME composition handling. Disable duplicate sending while
   an accepted request is unresolved. Preserve one unsent draft per conversation
   in local browser storage; clear it only after acceptance. Do not clear a draft
   when switching to a job or another conversation.
10. Show the persisted user message immediately after acceptance, plus an
    assistant-working indicator tied to turn state. If a request times out,
    reconcile/retry using the same request ID before allowing a second submission.
    A failed assistant turn gets a retry action for that turn, not duplicate user
    prose appended to the chat. Switching selection leaves its execution running.
11. Render persisted interview questions as input cards tied to question IDs.
    Use choices when provided and a text field when free text is allowed. Display
    the accepted answer in conversation history. A stale question becomes read-only
    after refresh; do not silently send its answer to the newest question.
12. Show linked generated specifications in a collapsible side panel or header
    action. Display the quality target, delivery order, unresolved decisions, and
    AI-provided defaults marked for revisit from structured data. Editing a spec
    must go through its validated application command; a Markdown preview must
    not become an unvalidated alternate specification source.
13. Provide auto-scroll only while the user is near the bottom. If they scroll up,
    retain their position and show a New messages button. Load older messages
    without jumping the viewport. Show connection loss separately from workflow
    failure, with bounded reconnect backoff and an explicit retry control.
14. Provide accessible names, keyboard-operable lists and menus, visible focus,
    contrast, reduced-motion support, and an unobtrusive live region for status.
    Implement light/dark/system themes using CSS variables and persist the choice.
    At narrow widths collapse the sidebar into a drawer; keep the same selection
    and draft behavior. Test 1280×800 and 1920×1080 desktop sizes and a narrow view.

**Meaningful verification:** browser tests with the real API and deterministic
workflow adapter cover creating two chats, receiving/answering an interview,
switching while work continues, preserving drafts, refreshing, reconnecting, and
viewing the generated spec. Test Markdown safety and keyboard-only use. Visually
inspect actual screenshots in light and dark themes; fix overflow, composer
overlap, and focus problems before calling the layout complete.

**Done when:** a developer can launch the app, conduct separate design interviews,
leave/reopen the browser, and continue the correct interview with its spec intact.
The job panel can show a clear empty/loading placeholder until package 4 lands.

### Package 4: autonomous job sidebar and detail view

**Purpose:** complete the requested left-side job lists and selected-job status
experience without interrupting design chats.

1. Add `JobPanel.tsx`, `JobList.tsx`, `JobDetail.tsx`, `JobTimeline.tsx`,
   `JobActions.tsx`, and a shared `ArtifactLinks.tsx`. Use the same repository
   snapshot/event connection as conversations. Sidebar sections should scroll
   independently and retain a usable minimum height; provide an accessible
   splitter between chats and jobs if the user resizes their relative heights.
2. Put Running and Ready/Pending tabs at the top of the lower sidebar section.
   Running includes `running`, `waiting_for_input`, and `waiting_for_external`.
   Label waiting states Needs input or the actual external blocker. Ready/Pending
   includes eligible jobs and jobs waiting on
   dependencies, with distinct labels. Show counts. Add a separate History entry
   containing succeeded, failed, cancelled, and interrupted jobs, with filters
   and prominent recovery/input badges so failures cannot disappear silently.
3. Each job row shows type, title, current stage or blocker, status icon/text, and
   recent activity time. Do not rely on color alone. Selection highlights one
   chat or one job, never both; retain each view's scroll position and drafts.
4. Job detail shows title, kind, state, attempt, elapsed time, named current
   stage, last activity, dependency links, originating chat, pinned source spec,
   linked issue/PRs, and generated artifacts. Keep human-readable progress first;
   put bounded logs and technical diagnostics behind an expandable section.
5. Display an ordered stage/activity timeline. Preserve old attempts as collapsed
   history after retry/resume. Use a percentage only if actual completed/total
   work units exist; otherwise show an active stage and activity timestamps.
6. The backend returns allowed actions. Render Start for eligible ready work,
   Queue for valid dependency-blocked work, Cancel for cancellable work, Retry
   for failed attempts, and Resume for recoverable interrupted work. Include
   revision/request IDs on actions. Reconcile a 409 by fetching current state
   and explaining the change; do not automatically execute a new action against
   the newer revision. Disable a button only for its unresolved request.
7. Show dependency failures and manual prerequisites as explicit blockers with
   links and supplied instructions. Render question cards using the same answer
   component as chats. Do not offer a fake Mark done button when completion must
   be established by existing validation or GitHub checks.
8. Add Create planning job from a validated completed chat/spec and Create PR
   execution job from an approved proposed PR/issue. Show a compact review of
   repository, pinned input revision, selected plan item, and dependencies before
   submission. Return field-level validation errors and select the newly created
   job. Do not silently run every proposed PR when the user selects one item.
9. Display implementation and alteration PR links with their different purposes.
   Awaiting review/merge appears as a blocker. PR publication success is not
   synonymous with merge. Reuse existing approval policy; the UI does not merge
   the agent's PR automatically.
10. Surface interrupted/failed work on startup and let the user inspect checkpoint
    availability before requesting recovery. A job without a valid checkpoint
    shows the backend's recovery error and permitted alternatives. Preserve its
    conversation link and previously produced artifacts.

**Meaningful verification:** browser tests cover one active chat plus two jobs,
switching all views, pending dependency promotion, failure visibility, a question
answered exactly once, stale action conflicts, cancellation, retry/resume with a
new attempt, and implementation versus alteration PR results. Assert rendered
status follows backend events rather than fake timers or optimistic success.

**Done when:** the original sidebar/right-pane requirements work with real adapters,
and a user can understand and act on running, queued, blocked, and finished work.

### Package 5: launch, distribution, shutdown, and platform acceptance

**Purpose:** make the complete application installable and dependable on all three
desktop operating systems, rather than merely browser-compatible.

1. Add `powdrr-lift ui --repo-root PATH` with `--port`, `--no-open`, and an explicit
   data-directory override. Default to the OS user application-data directory,
   outside Git checkouts. Use a platform-aware path utility. Keep databases,
   owned process records, run bindings, and logs out of the repository index.
2. Start a single backend/supervisor for that data directory, wait for its health
   endpoint, then open the browser through Python's portable browser launcher.
   Reuse a verified existing healthy instance when launched again. Handle port
   conflicts explicitly; never connect to an unrelated service just because its
   port is occupied. Print the local URL if automatic browser opening fails.
3. Build Vite assets in the release pipeline and include them in the Python wheel.
   Serve them using package resources, with correct MIME types and hashed-asset
   caching. Keep API/session responses uncached. Missing assets produce clear
   installation/build instructions rather than a blank browser page. Development
   uses the Vite server with a documented local API proxy and restricted origins.
4. Keep server, frontend, and contract versions compatible. Expose a build/schema
   identity to the frontend. When a stale browser bundle meets an upgraded server,
   prompt to reload instead of applying incompatible events. Back up local data
   before schema upgrades and document recovery; never overwrite an unknown schema.
5. Audit the full import and execution path for POSIX assumptions: `fcntl`, terminal
   input, PTYs, `select` on stdin, signal names, process groups, shell executables,
   path separators, executable bits, Git path syntax, and environment inheritance.
   Browser workers use structured input and never need a TTY. Invoke commands
   with argv and explicit cwd/env. Preserve project-specific tool prerequisites
   and report missing tools at setup/preflight, not halfway through publication.
6. Define shutdown: closing the tab leaves execution running. Stopping the backend
   stops new dispatch, requests safe worker shutdown, records recoverable state,
   and confirms owned child exit before reporting completion. If the shutdown
   budget expires, report affected attempts and recovery locations. A crash is
   reconciled through the ownership/checkpoint rules in package 2C.
7. Add a CI matrix on Ubuntu, macOS, and Windows for package import, migrations,
   locks, supervisor ownership, process cancellation, checkpoint recovery, launch,
   and built-asset serving. Run browser scenarios on Chromium on each OS and
   Firefox/WebKit where their runners support them. A Linux WebKit result is not
   a substitute for checking the shipped app in Safari on macOS or Edge on Windows.
8. Exercise paths containing spaces and non-ASCII characters, repositories on a
   different drive on Windows, two browser windows, offline/reconnected browsers,
   provider retry exhaustion, abrupt worker exit, abrupt backend exit, and a
   database carried forward from the first storage increment.
9. Document install/start/stop, repository registration, configuration of existing
   providers and GitHub credentials, where local state lives, backup, recovery,
   and known platform-specific project-tool limitations. Provide a diagnostic
   export with explicit user action and sanitized bounded logs; exclude secrets
   and full prompt/transcript contents by default.

**Done when:** an installed wheel starts the complete UI without Node on supported
macOS, Linux, and Windows environments; a design-to-plan-to-PR acceptance scenario
works through the browser; tab closure, cancellation, and crash/restart recovery
have verified outcomes. Record exact tested OS/browser/tool versions and remaining
project-tool constraints in release notes. Do not claim full Windows execution
support based only on frontend rendering or SQLite tests.

### End-to-end acceptance script and review checklist

Use a small disposable Git repository with deterministic workflow responses for
repeatable CI. Keep any live-provider/GitHub publication smoke run opt-in and
clearly distinguish it from deterministic coverage.

1. Install the built wheel into a clean environment and launch the UI.
2. Register the repository. Create chats A and B. Enter a feature request in A,
   answer its quality/delivery/legacy questions, and leave an unsent draft in B.
3. Complete A's interview; inspect the validated spec, module scope, and marked
   defaults. Refresh and reopen the browser; both histories and B's draft survive.
4. Create a planning job from A's specific spec revision and start it. Switch to
   B while planning runs, then select the planning job and inspect real stages.
5. Create two PR execution jobs with a dependency. Start/enqueue them deliberately.
   Verify only eligible work claims execution and the blocked job explains why.
6. Drop the browser connection during an update, reconnect, and confirm a single
   ordered message/status history without duplicate requests or execution.
7. Trigger a real workflow question, answer it from the UI, retry the same HTTP
   request, and confirm the continuation consumes the answer once.
8. Interrupt a worker after a durable checkpoint, restart the backend, inspect the
   interrupted job, and resume. Verify earlier work and publication are not repeated.
9. Exercise an alteration PR before implementation. Confirm the job shows its
   review/merge blocker and later reports the separate implementation PR correctly.
10. Cancel a job with an owned coding-agent child process. Confirm the process tree
    exits or recovery stays explicitly blocked; another job/chat remains usable.
11. Exhaust a simulated transient-service retry budget. Confirm one final failed
    state, useful diagnostics, and existing GitHub failure write-back behavior.
12. Review screenshots at the specified sizes/themes and complete keyboard-only
    navigation. Repeat the platform-critical lifecycle steps on all three OSes.

For each PR, provide the changed contracts/modules, migration impact, meaningful
verification results, screenshots when UI changes, and remaining limitations.
Run the repository-required Python checks with the shared worktree environment,
plus the frontend type/lint/build and browser checks relevant to that increment.
Avoid tests that assert component class names, hardcoded prose, or mock internals;
verify user behavior, persistence, isolation, and external-effect boundaries.

### Reference documentation

- [FastAPI custom and streaming responses](https://fastapi.tiangolo.com/advanced/custom-response/)
- [React components and shared state](https://react.dev/learn)
- [Vite production asset builds](https://vite.dev/guide/build)
- [Playwright installation and supported browser engines](https://playwright.dev/docs/intro)

These references describe the selected tools. Repository contracts, workflow
semantics, and the acceptance criteria above determine the implementation.
