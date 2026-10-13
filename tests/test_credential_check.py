from __future__ import annotations

from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from powdrr_lift.cli import build_parser, main
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.credential_check import (
    check_jev_access,
    check_model_access,
    configured_model_mapping,
)
from powdrr_lift.workrr.llm import WorkflowLLMHTTPError
from powdrr_lift.workrr.provider_config import LLMModelMapping
from powdrr_lift.workrr.providers import ProviderCredentials


def test_check_model_access_reports_success_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, ...]] = []
    build_options: dict[str, object] = {}

    class Client:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, bool]:
            calls.append((messages,))
            return {"ok": True}

    def build(*_args: object, **kwargs: object) -> Client:
        build_options.update(kwargs)
        return Client()

    def resolve(*args: object) -> ProviderCredentials:
        calls.append(args)
        return ProviderCredentials(
            "example", "secret-value", "TEST_API_KEY", "url", "default"
        )

    result = check_model_access(
        role="planning",
        provider="example",
        mapping=LLMModelMapping("model-a", "example"),
        repo_root=tmp_path,
        credential_resolver=resolve,
        client_builder=build,
    )

    assert result.to_data() == {
        "role": "planning",
        "provider": "example",
        "model": "model-a",
        "status": "passed",
        "credential_source": "TEST_API_KEY",
    }
    assert len(calls) == 2
    assert build_options["timeout"] == 10.0


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "authentication_failed"),
        (403, "model_access_denied"),
        (429, "rate_or_quota_limited"),
    ],
)
def test_check_model_access_classifies_provider_http_errors(
    tmp_path: Path, status_code: int, expected: str
) -> None:
    def builder(*_args: object, **_kwargs: object) -> object:
        class Client:
            def complete_json(self, _messages: list[dict[str, str]]) -> dict[str, bool]:
                raise WorkflowLLMHTTPError("Provider", status_code, "denied")

        return Client()

    result = check_model_access(
        role="coding",
        provider="example",
        mapping=LLMModelMapping("model-b", "example"),
        repo_root=tmp_path,
        credential_resolver=lambda *_args: ProviderCredentials(
            "example", "secret-value", "TEST_API_KEY", "url", "default"
        ),
        client_builder=builder,
    )

    assert result.status == expected
    assert result.to_data()["message"] == f"Provider returned HTTP {status_code}."


def test_configured_model_mapping_uses_explicit_model() -> None:
    mapping = configured_model_mapping("deepinfra", "example/model")

    assert mapping == LLMModelMapping("example/model", "deepinfra")


def test_check_model_access_reports_missing_credentials(tmp_path: Path) -> None:
    result = check_model_access(
        role="planning",
        provider="example",
        mapping=LLMModelMapping("model-c", "example"),
        repo_root=tmp_path,
        credential_resolver=lambda *_args: (_ for _ in ()).throw(
            PowdrrExecutionError("No Example credentials found. Set EXAMPLE_API_KEY.")
        ),
    )

    assert result.status == "credentials_missing"


def test_check_jev_access_makes_authenticated_request_without_exposing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFEAI_API_KEY", "private-test-key")
    captured: dict[str, object] = {}

    def call_jev(
        request: object, api_key: str, endpoint: str, model: str
    ) -> dict[str, object]:
        captured.update(
            request=request, api_key=api_key, endpoint=endpoint, model=model
        )
        return {"answer": {"choice": "ok"}}

    monkeypatch.setattr("powdrr_lift.workrr.credential_check._call_jev", call_jev)

    result = check_jev_access()

    assert result.status == "passed"
    assert result.credential_source == "TYPESAFEAI_API_KEY"
    assert "private-test-key" not in str(result.to_data())
    assert captured["api_key"] == "private-test-key"
    assert captured["endpoint"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["model"] == "jev-latest"


def test_check_jev_access_reports_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TYPESAFEAI_API_KEY",
        "TYPESAFE_API_KEY",
        "SYSTEM_ONE_API_KEY",
        "SUBQ_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    result = check_jev_access()

    assert result.status == "credentials_missing"
    assert result.message == "Set a Jev or SUBQ_API_KEY to check classifier access."


def test_check_jev_access_uses_subq_when_jev_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUBQ_API_KEY", "private-subq-key")
    captured: dict[str, object] = {}

    def call_subq(
        request: object, api_key: str, endpoint: str, model: str
    ) -> dict[str, object]:
        captured.update(
            request=request, api_key=api_key, endpoint=endpoint, model=model
        )
        return {"answer": {"choice": "ok"}}

    monkeypatch.setattr("powdrr_lift.workrr.credential_check._call_subq", call_subq)

    result = check_jev_access()

    assert result.status == "passed"
    assert result.credential_source == "SUBQ_API_KEY"
    assert result.provider == "subq"
    assert captured["endpoint"] == "https://api.subq.ai/v1/systemone"
    assert captured["model"] == "subq-snap-preview"


def test_check_jev_access_reports_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import HTTPError

    monkeypatch.setenv("TYPESAFEAI_API_KEY", "private-test-key")

    def reject(*_args: object) -> object:
        raise HTTPError("https://api.typesafe.ai", 401, "Unauthorized", Message(), None)

    monkeypatch.setattr("powdrr_lift.workrr.credential_check._call_jev", reject)

    result = check_jev_access()

    assert result.status == "authentication_failed"
    assert result.message == "Decision API returned HTTP 401."
    assert "private-test-key" not in str(result.to_data())


def test_check_credentials_cli_checks_both_routes_and_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    checks: list[tuple[str, str]] = []

    def check(*, role: str, provider: str, **_kwargs: object) -> SimpleNamespace:
        checks.append((role, provider))
        return SimpleNamespace(
            to_data=lambda: {
                "role": role,
                "provider": provider,
                "model": "test-model",
                "status": "passed",
            }
        )

    monkeypatch.setattr("powdrr_lift.cli.check_model_access", check)
    monkeypatch.setattr(
        "powdrr_lift.cli.check_coding_executable",
        lambda executable, *, role: SimpleNamespace(
            to_data=lambda: {
                "role": role,
                "provider": "local-executable",
                "model": executable,
                "status": "passed",
            }
        ),
    )
    monkeypatch.setattr(
        "powdrr_lift.cli.check_jev_access",
        lambda: SimpleNamespace(
            to_data=lambda: {
                "role": "jev-classifier",
                "provider": "typesafe-ai",
                "model": "jev-latest",
                "status": "passed",
            }
        ),
    )
    github_calls: list[list[str]] = []

    def run_github(command: list[str], **_kwargs: object) -> SimpleNamespace:
        github_calls.append(command)
        if command == ["gh", "auth", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout='{"nameWithOwner":"example/project","viewerPermission":"WRITE"}',
            stderr="",
        )

    monkeypatch.setattr("powdrr_lift.cli.subprocess.run", run_github)

    import json
    from contextlib import redirect_stdout
    from io import StringIO

    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "check-credentials",
                "--repo-root",
                str(tmp_path),
                "--planning-provider",
                "zai",
                "--coding-provider",
                "deepinfra",
                "--json",
            ]
        )

    assert exit_code == 0
    assert checks == [("planning", "zai"), ("coding", "deepinfra")]
    report = json.loads(stdout.getvalue())
    assert report["passed"] is True
    assert report["checks"][-2] == {
        "role": "jev-classifier",
        "provider": "typesafe-ai",
        "model": "jev-latest",
        "status": "passed",
    }
    assert report["checks"][-1] == {
        "role": "github-access",
        "provider": "gh",
        "model": "GitHub CLI and repository permissions",
        "status": "passed",
        "repository": "example/project",
        "permission": "WRITE",
    }
    assert github_calls == [
        ["gh", "auth", "status"],
        ["gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"],
    ]


def test_bootstrap_opens_pull_request_by_default_with_local_opt_out() -> None:
    parser = build_parser()

    assert parser.parse_args(["bootstrap"]).open_pr is True
    assert parser.parse_args(["bootstrap", "--no-open-pr"]).open_pr is False
