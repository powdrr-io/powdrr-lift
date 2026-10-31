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
