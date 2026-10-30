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
