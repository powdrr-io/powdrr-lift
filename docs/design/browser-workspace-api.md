# Browser Workspace API

The local HTTP API is created with `powdrr_local.web.create_app`. It is bound
to a loopback origin and uses the same SQLite workspace database as the local
service. The API does not start workflow execution from a request handler.

## Session bootstrap

The application supplies its short-lived bootstrap credential to the browser
through a trusted local launch path. The browser sends it in the request body,
never in a URL:

```http
POST /api/v1/session
Origin: http://127.0.0.1:8765
Content-Type: application/json

{"token":"<one-use-bootstrap-token>"}
```

On success, the server sets an HttpOnly, same-site cookie and returns a CSRF
token for browser memory:

```json
{"csrf_token":"<csrf-token>"}
```

The bootstrap token is consumed atomically in SQLite, so separate service
processes cannot exchange it more than once. Mutating requests must include the
exact configured `Origin` and the returned `X-CSRF-Token`. The cookie is not
readable by browser JavaScript. After a browser reload, call
`POST /api/v1/session/refresh` with the configured `Origin`; the live session
cookie authorizes an atomic CSRF-token rotation. Keep the returned token in
browser memory. Requests from another origin cannot refresh a token.

## Repository and conversation requests

After bootstrap, the browser cookie is sent automatically. Register a repository
with an explicit request ID so a network retry returns the original result:

```http
POST /api/v1/repositories
Origin: http://127.0.0.1:8765
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"path":"/work/project","display_name":"Project","request_id":"<uuid>"}
```

List registered repositories with `GET /api/v1/repositories`. The response
contains public repository IDs and display names; canonical local paths are not
returned. Read a repository's snapshot and event cursor with
`GET /api/v1/repositories/{repository_id}/snapshot`.

Create a design conversation with:

```http
POST /api/v1/repositories/{repository_id}/conversations
Origin: http://127.0.0.1:8765
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"title":"Search design","request_id":"<uuid>"}
```

Conversation updates carry `expected_revision`; a stale update returns HTTP 409
with an `error` object containing `code`, `message`, and the current revision.
Conversation and message reads are bounded and support cursor pagination.

Submit a user message to start one accepted turn in its conversation:

```http
POST /api/v1/repositories/{repository_id}/conversations/{conversation_id}/messages
Origin: http://127.0.0.1:8765
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"request_id":"<uuid>","message_id":"<uuid>","content":"Add search"}
```

The server returns HTTP 202 only after committing the user message, accepted
turn, idempotency result, and pending dispatch intent together:

```json
{"message":{"schema_version":1,"message_id":"<uuid>","conversation_id":"<uuid>","role":"user","content":"Add search","created_at":"<timestamp>","sequence":17},"turn_id":"<uuid>","state":"accepted"}
```

Retry the identical command with the same `request_id` to receive the original
acceptance. Reusing that ID with a different payload returns HTTP 409. Each
conversation accepts at most one active turn. A 202 confirms durable acceptance;
it does not mean the assistant has completed. The supervisor consumes the
pending dispatch intent outside the HTTP request handler.

List jobs with `GET /api/v1/repositories/{repository_id}/jobs`. Repeat the
`states` query parameter to filter by one or more states, and use the returned
`next_cursor` fields as `before_updated_at` and `before_job_id` for the next
page. Fetch a job and its saved attempt identifiers from
`GET /api/v1/repositories/{repository_id}/jobs/{job_id}`. Job detail includes the
latest attempt, a bounded attempt history, dependency blockers, artifact outputs,
and actions allowed by the current job state. Stage and question are `null` until
the workflow adapter records structured stage or job-specific question data.
Attempt details omit local worktree paths, checkpoint paths, and provider
configuration data. The detail projection is read in one SQLite transaction.

Request an allowed job action with `POST /api/v1/repositories/{repository_id}/jobs/{job_id}/actions`, including the current job revision and a stable request ID:

```http
POST /api/v1/repositories/repo-1/jobs/job-1/actions
Origin: http://127.0.0.1:8765
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"request_id":"<uuid>","expected_revision":1,"action":"start"}
```

The endpoint returns HTTP 202 after saving the action intent, job event, and
idempotency result in one SQLite transaction. Repeating the identical command
returns the original action ID; a stale revision or conflicting pending action
returns HTTP 409. The job remains in its current state until a supervisor
consumes and applies the intent. The request handler never starts or cancels
workflow code itself.

The supervisor claims one intent under its active lease and sends it to the
configured action dispatcher. A completed intent means the dispatcher accepted
the action; the job may still be running. A retryable dispatch error returns the
intent to the pending queue so the supervisor can apply its retry policy. Other
dispatch errors mark the intent failed. Since a supervisor can crash after an
external effect but before recording completion, dispatchers must deduplicate
effects by `action_id`. Event notifications only wake readers; the durable event
log remains authoritative.

```json
{"schema_version":1,"job":{"schema_version":1,"job_id":"job-1","repository_id":"repo-1","kind":"planning","title":"Implement search","state":"pending","conversation_id":null,"dependency_ids":["job-2"],"artifact_refs":[],"detail":"","revision":1,"created_at":"<timestamp>","updated_at":"<timestamp>"},"latest_attempt":null,"attempts":[],"stage":null,"blockers":[{"schema_version":1,"job_id":"job-2","title":"Plan search","state":"ready"}],"question":null,"outputs":[],"allowed_actions":["cancel"]}
```

Create a planning job from an explicit pinned specification and a provider
configuration reference:

```http
POST /api/v1/repositories/{repository_id}/jobs
Origin: http://127.0.0.1:8765
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"request_id":"<uuid>","kind":"planning","title":"Plan search","conversation_id":"<conversation-id>","dependency_ids":[],"job_inputs":{"pinned_spec_ref":"spec:search-v2"},"source_refs":["spec:search-v2"],"source_spec_revision":"revision-7","provider_config_refs":["provider:deepinfra-flash"]}
```

The endpoint returns HTTP 201 with the created job. For `pr_execution`,
`job_inputs` must include an `approved_change_ref` present in `source_refs`.
Source references, validated inputs, source revision, and provider configuration
references are saved with the job and must match when its operation starts. Do
not send provider credentials; the backend resolves provider references through
its existing configuration. A request ID retry returns the original job, while
reuse with different input returns HTTP 409.

## Event replay

Open `GET /api/v1/repositories/{repository_id}/events?after={event_cursor}` to
receive server-sent events after the snapshot cursor. The server assigns each
event's durable sequence as its SSE `id`. On reconnect, `Last-Event-ID` takes
precedence over the `after` query parameter. Events are read in bounded batches
from SQLite, so a disconnected browser does not hold workflow work or an
unbounded event queue. Heartbeats are SSE comments and do not advance the cursor.

If the supplied cursor is ahead of the repository's current event cursor, the
server returns HTTP 409 with `code: "resync_required"` and the current cursor.
Fetch a new repository snapshot, then reconnect from its `event_cursor`. Events
are retained for the beta and are not silently skipped.

All errors use this stable envelope:

```json
{"error":{"code":"stale_revision","message":"This conversation changed in another window.","current_revision":4}}
```

The health route is unauthenticated and reports only API/schema and scheduler
health. It does not include credentials, prompt content, or local paths.
