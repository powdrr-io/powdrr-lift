import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from powdrr_local.web import create_app
from powdrr_local.workspace import WorkspaceStore
from powdrr_local.workspace_api import _event_stream
from starlette.requests import Request


def _client(tmp_path: Path) -> tuple[TestClient, WorkspaceStore, str, str]:
    store = WorkspaceStore(tmp_path / "workspace.db")
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    subprocess.run(
        ["git", "init", str(repository_path)], check=True, capture_output=True
    )
    repository = store.register_repository(repository_path, request_id="register")
    origin = "http://127.0.0.1:8765"
    secret = "bootstrap-secret-value-that-is-at-least-32-characters"
    app = create_app(store=store, bootstrap_secret=secret, expected_origin=origin)
    return TestClient(app, base_url=origin), store, repository.repository_id, secret


def _sign_in(client: TestClient, secret: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={"token": secret},
    )
    assert response.status_code == 201
    return response.json()["csrf_token"]


def test_health_is_public_and_session_bootstrap_is_single_use(tmp_path: Path) -> None:
    client, store, _, secret = _client(tmp_path)
    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "schema_version": 14,
        "scheduler": {"status": "not_configured"},
    }

    _sign_in(client, secret)
    second_app_client = TestClient(
        create_app(
            store=WorkspaceStore(store.path),
            bootstrap_secret=secret,
            expected_origin="http://127.0.0.1:8765",
        ),
        base_url="http://127.0.0.1:8765",
    )
    response = second_app_client.post(
        "/api/v1/session",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={"token": secret},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bootstrap_used"


def test_conversation_mutations_require_origin_and_csrf(tmp_path: Path) -> None:
    client, _, repository_id, secret = _client(tmp_path)
    csrf = _sign_in(client, secret)
    path = f"/api/v1/repositories/{repository_id}/conversations"
    payload = {"title": "Search design", "request_id": "conversation-1"}

    missing_csrf = client.post(
        path, headers={"Origin": "http://127.0.0.1:8765"}, json=payload
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_rejected"

    created = client.post(
        path,
        headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": csrf},
        json=payload,
    )
    assert created.status_code == 201
    conversation = created.json()

    stale = client.patch(
        f"{path}/{conversation['conversation_id']}",
        headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": csrf},
        json={
            "title": "Changed",
            "expected_revision": 4,
            "request_id": "conversation-update-1",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "stale_revision",
        "message": "This conversation changed in another window.",
        "current_revision": 1,
    }


def test_message_submission_is_durable_and_idempotent(tmp_path: Path) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    csrf = _sign_in(client, secret)
    headers = {
        "Origin": "http://127.0.0.1:8765",
        "X-CSRF-Token": csrf,
    }
    conversation = client.post(
        f"/api/v1/repositories/{repository_id}/conversations",
        headers=headers,
        json={"title": "Search", "request_id": "create-search"},
    ).json()
    path = (
        f"/api/v1/repositories/{repository_id}/conversations/"
        f"{conversation['conversation_id']}/messages"
    )
    payload = {
        "request_id": "submit-search",
        "message_id": "message-search-1",
        "content": "Add search",
    }

    accepted = client.post(path, headers=headers, json=payload)
    retried = client.post(path, headers=headers, json=payload)

    assert accepted.status_code == retried.status_code == 202
    result = accepted.json()
    assert retried.json() == result
    assert result["message"]["content"] == "Add search"
    assert result["turn_id"]
    assert result["state"] == "accepted"
    assert len(store.list_messages(conversation["conversation_id"])) == 1
    assert len(store.list_pending_turn_dispatches(repository_id)) == 1

    reopened = WorkspaceStore(store.path)
    assert len(reopened.list_pending_turn_dispatches(repository_id)) == 1
    conflicting_retry = client.post(
        path,
        headers=headers,
        json={**payload, "content": "Do something else"},
    )
    assert conflicting_retry.status_code == 409
    assert conflicting_retry.json()["error"]["code"] == "request_conflict"

    active_turn_conflict = client.post(
        path,
        headers=headers,
        json={
            "request_id": "submit-search-second",
            "message_id": "message-search-2",
            "content": "Another turn",
        },
    )
    assert active_turn_conflict.status_code == 409
    assert len(reopened.list_pending_turn_dispatches(repository_id)) == 1


def test_mutation_with_wrong_origin_is_rejected(tmp_path: Path) -> None:
    client, _, _, secret = _client(tmp_path)
    _sign_in(client, secret)
    response = client.post(
        "/api/v1/repositories",
        headers={"Origin": "http://localhost:8765", "X-CSRF-Token": "irrelevant"},
        json={"path": "/tmp", "request_id": "register-another"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_rejected"


def test_job_list_is_filtered_and_uses_bounded_cursors(tmp_path: Path) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    first = store.create_job(
        repository_id, kind="planning", title="First", request_id="job-1"
    )
    second = store.create_job(
        repository_id, kind="pr_execution", title="Second", request_id="job-2"
    )

    first_page = client.get(
        f"/api/v1/repositories/{repository_id}/jobs?states=ready&limit=1"
    )
    assert first_page.status_code == 200
    page = first_page.json()
    assert len(page["jobs"]) == 1
    assert page["next_cursor"] is not None

    cursor = page["next_cursor"]
    second_page = client.get(
        f"/api/v1/repositories/{repository_id}/jobs",
        params={
            "states": "ready",
            "limit": 1,
            "before_updated_at": cursor["updated_at"],
            "before_job_id": cursor["job_id"],
        },
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["jobs"]) == 1
    assert {
        page["jobs"][0]["job_id"],
        second_page.json()["jobs"][0]["job_id"],
    } == {first.job_id, second.job_id}


def test_job_creation_persists_validated_inputs_and_replays_request(
    tmp_path: Path,
) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    csrf = _sign_in(client, secret)
    headers = {
        "Origin": "http://127.0.0.1:8765",
        "X-CSRF-Token": csrf,
    }
    conversation = store.create_conversation(repository_id, "Search")
    payload = {
        "request_id": "create-search-plan",
        "kind": "planning",
        "title": "Plan search",
        "conversation_id": conversation.conversation_id,
        "dependency_ids": [],
        "job_inputs": {"pinned_spec_ref": "spec:search-v2", "include_tests": True},
        "source_refs": ["spec:search-v2"],
        "source_spec_revision": "revision-7",
        "provider_config_refs": ["provider:deepinfra-flash"],
    }
    path = f"/api/v1/repositories/{repository_id}/jobs"

    created = client.post(path, headers=headers, json=payload)
    retried = client.post(path, headers=headers, json=payload)

    assert created.status_code == 201, created.text
    assert retried.status_code == 201
    assert retried.json() == created.json()
    job = created.json()
    assert job["kind"] == "planning"
    assert job["state"] == "ready"
    submission = store.get_job_submission(job["job_id"])
    assert submission["job_inputs"] == payload["job_inputs"]
    assert submission["source_refs"] == ("spec:search-v2",)
    assert submission["source_spec_revision"] == "revision-7"
    assert submission["provider_config_refs"] == ("provider:deepinfra-flash",)

    changed_retry = client.post(
        path,
        headers=headers,
        json={**payload, "job_inputs": {"pinned_spec_ref": "spec:search-v2"}},
    )
    assert changed_retry.status_code == 409
    assert changed_retry.json()["error"]["code"] == "request_conflict"

    invalid = client.post(
        path,
        headers=headers,
        json={
            **payload,
            "request_id": "create-secret-plan",
            "job_inputs": {
                "pinned_spec_ref": "spec:search-v2",
                "access_token": "never-persist-this",
            },
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_job"
    assert len(store.list_jobs(repository_id)) == 1


def test_job_detail_hides_local_paths_and_checks_repository_association(
    tmp_path: Path,
) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    job = store.create_job(
        repository_id, kind="planning", title="Plan", request_id="job-plan"
    )
    worktree = tmp_path / "private-worktree"
    store.start_job_operation(
        job.job_id,
        expected_revision=job.revision,
        run_id="run-plan",
        attempt_id="attempt-plan",
        workflow_directory=worktree,
        checkpoint_ref="checkpoint:plan",
        job_inputs={"pinned_spec_ref": "spec:search"},
        source_refs=["spec:search"],
        source_spec_revision="revision-1",
        provider_config_refs=["provider:deepinfra-flash"],
    )

    detail = client.get(f"/api/v1/repositories/{repository_id}/jobs/{job.job_id}")
    assert detail.status_code == 200
    operation = store.get_job_operation(job.job_id, "attempt-plan")
    assert detail.json()["attempts"] == [
        {
            "schema_version": 1,
            "attempt_id": "attempt-plan",
            "run_id": "run-plan",
            "source_spec_revision": "revision-1",
            "created_at": operation.created_at,
        }
    ]
    assert detail.json()["latest_attempt"] == detail.json()["attempts"][0]
    assert detail.json()["stage"] is None
    assert detail.json()["blockers"] == []
    assert detail.json()["question"] is None
    assert detail.json()["outputs"] == []
    assert detail.json()["allowed_actions"] == ["cancel"]
    assert str(worktree) not in detail.text
    assert "workflow_directory" not in detail.text

    other_repository_path = tmp_path / "other-repository"
    other_repository_path.mkdir()
    subprocess.run(
        ["git", "init", str(other_repository_path)], check=True, capture_output=True
    )
    other_repository = store.register_repository(
        other_repository_path, request_id="register-other"
    )
    response = client.get(
        f"/api/v1/repositories/{other_repository.repository_id}/jobs/{job.job_id}"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_job_detail_reports_dependency_blockers_and_allowed_actions(
    tmp_path: Path,
) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    prerequisite = store.create_job(
        repository_id,
        kind="planning",
        title="Plan prerequisite",
        request_id="job-prerequisite",
    )
    dependent = store.create_job(
        repository_id,
        kind="pr_execution",
        title="Execute plan",
        dependency_ids=[prerequisite.job_id],
        request_id="job-dependent",
    )

    response = client.get(
        f"/api/v1/repositories/{repository_id}/jobs/{dependent.job_id}"
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["job"]["state"] == "pending"
    assert detail["blockers"] == [
        {
            "schema_version": 1,
            "job_id": prerequisite.job_id,
            "title": "Plan prerequisite",
            "state": "ready",
        }
    ]
    assert detail["latest_attempt"] is None
    assert detail["attempts"] == []
    assert detail["stage"] is None
    assert detail["question"] is None
    assert detail["allowed_actions"] == ["cancel"]


def test_job_action_is_durable_revision_checked_and_idempotent(
    tmp_path: Path,
) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    csrf = _sign_in(client, secret)
    headers = {
        "Origin": "http://127.0.0.1:8765",
        "X-CSRF-Token": csrf,
    }
    job = store.create_job(
        repository_id,
        kind="planning",
        title="Plan search",
        request_id="create-plan-action",
    )
    path = f"/api/v1/repositories/{repository_id}/jobs/{job.job_id}/actions"
    payload = {
        "request_id": "start-plan-action",
        "expected_revision": job.revision,
        "action": "start",
    }

    accepted = client.post(path, headers=headers, json=payload)
    retried = client.post(path, headers=headers, json=payload)

    assert accepted.status_code == retried.status_code == 202
    assert retried.json() == accepted.json()
    assert accepted.json()["action"] == "start"
    assert accepted.json()["state"] == "accepted"
    assert store.get_job(job.job_id).revision == job.revision + 1
    detail = client.get(f"/api/v1/repositories/{repository_id}/jobs/{job.job_id}")
    assert detail.json()["allowed_actions"] == ["cancel"]
    reopened = WorkspaceStore(store.path)
    with reopened._connection() as db:
        intents = db.execute(
            "SELECT action_id,action,state FROM job_action_intents WHERE job_id=?",
            (job.job_id,),
        ).fetchall()
    assert len(intents) == 1
    assert tuple(intents[0]) == (accepted.json()["action_id"], "start", "pending")

    stale = client.post(
        path,
        headers=headers,
        json={
            "request_id": "stale-plan-action",
            "expected_revision": job.revision,
            "action": "cancel",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"


def test_job_actions_reject_unavailable_or_duplicate_actions(tmp_path: Path) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    csrf = _sign_in(client, secret)
    headers = {
        "Origin": "http://127.0.0.1:8765",
        "X-CSRF-Token": csrf,
    }
    job = store.create_job(
        repository_id,
        kind="planning",
        title="Plan search",
        dependency_ids=[
            store.create_job(
                repository_id,
                kind="planning",
                title="Prerequisite",
                request_id="action-prerequisite",
            ).job_id
        ],
        request_id="create-blocked-action",
    )
    path = f"/api/v1/repositories/{repository_id}/jobs/{job.job_id}/actions"
    unavailable = client.post(
        path,
        headers=headers,
        json={
            "request_id": "unavailable-start",
            "expected_revision": 1,
            "action": "start",
        },
    )
    assert unavailable.status_code == 422

    cancellation = client.post(
        path,
        headers=headers,
        json={
            "request_id": "cancel-blocked",
            "expected_revision": 1,
            "action": "cancel",
        },
    )
    duplicate = client.post(
        path,
        headers=headers,
        json={
            "request_id": "cancel-blocked-2",
            "expected_revision": 2,
            "action": "cancel",
        },
    )
    assert cancellation.status_code == 202
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "request_conflict"


def test_event_stream_formats_durable_replay_events(tmp_path: Path) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    conversation = WorkspaceStore(store.path).create_conversation(
        repository_id, "Replay event", request_id="replay-conversation"
    )
    cursor = store.event_cursor(repository_id)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
        "app": client.app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)

    async def first_frame() -> str:
        stream = _event_stream(store, repository_id, cursor - 1, request)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(first_frame())
    lines = frame.splitlines()
    assert lines[0] == f"id: {cursor}"
    assert lines[1] == "event: conversation.created"
    assert lines[2].startswith("data: ")
    event = json.loads(lines[2].removeprefix("data: "))
    assert event["entity_id"] == conversation.conversation_id


def test_event_stream_requires_snapshot_for_a_future_cursor(tmp_path: Path) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    current_cursor = store.event_cursor(repository_id)

    response = client.get(
        f"/api/v1/repositories/{repository_id}/events",
        params={"after": current_cursor + 5},
    )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "resync_required",
        "message": (
            "The event cursor is ahead of this repository. Fetch a fresh snapshot "
            "and reconnect from its event cursor."
        ),
        "current_event_cursor": current_cursor,
    }


def test_event_stream_reads_events_written_by_another_store_instance(
    tmp_path: Path,
) -> None:
    client, store, repository_id, secret = _client(tmp_path)
    _sign_in(client, secret)
    cursor = store.event_cursor(repository_id)
    other_store = WorkspaceStore(store.path)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
        "app": client.app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)

    async def receive_new_event() -> tuple[str, str]:
        async def publish() -> str:
            await asyncio.sleep(0.02)
            conversation = await asyncio.to_thread(
                other_store.create_conversation,
                repository_id,
                "Published by another process",
                request_id="other-process-conversation",
            )
            return conversation.conversation_id

        publisher = asyncio.create_task(publish())
        stream = _event_stream(store, repository_id, cursor, request)
        try:
            frame = await anext(stream)
            conversation_id = await publisher
            return frame, conversation_id
        finally:
            await stream.aclose()

    frame, conversation_id = asyncio.run(receive_new_event())
    lines = frame.splitlines()
    assert lines[1] == "event: conversation.created"
    event = json.loads(lines[2].removeprefix("data: "))
    assert event["entity_id"] == conversation_id
