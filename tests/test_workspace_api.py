import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from powdrr_local.web import create_app
from powdrr_local.workspace import WorkspaceStore


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
        "schema_version": 9,
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
            "attempt_id": "attempt-plan",
            "run_id": "run-plan",
            "source_spec_revision": "revision-1",
            "created_at": operation.created_at,
        }
    ]
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
