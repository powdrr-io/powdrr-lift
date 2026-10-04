from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from powdrr_lift.cli import main
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.credential_check import (
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
    assert json.loads(stdout.getvalue())["passed"] is True
