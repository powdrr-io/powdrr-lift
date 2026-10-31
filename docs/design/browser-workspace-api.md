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
readable by browser JavaScript.

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

List jobs with `GET /api/v1/repositories/{repository_id}/jobs`. Repeat the
`states` query parameter to filter by one or more states, and use the returned
`next_cursor` fields as `before_updated_at` and `before_job_id` for the next
page. Fetch a job and its saved attempt identifiers from
`GET /api/v1/repositories/{repository_id}/jobs/{job_id}`. Attempt details omit
local worktree paths and provider configuration data.

All errors use this stable envelope:

```json
{"error":{"code":"stale_revision","message":"This conversation changed in another window.","current_revision":4}}
```

The health route is unauthenticated and reports only API/schema and scheduler
health. It does not include credentials, prompt content, or local paths.
