from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.structrr.reference_definitions import ReferenceDefinition

from powdrr_lift.workrr.coding_agent import MiniSWEAgentProvider
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.source_reference_run import (
    SourceReferenceRun,
    context_delivery_status,
)

BASELINE = (
    "# structrr: invariant:retry\n# structrr: decision:helper\n"
    "def retry(value):\n    return value + 1\n"
)
CANDIDATE = (
    "# structrr: invariant:retry\ndef retry(value):\n    return helper(value)\n\n"
    "# structrr: decision:helper\ndef helper(value):\n    return value + 1\n"
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class Reviewer:
    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        packet = json.loads(messages[-1]["content"])
        assert "PRIVATE-ORACLE" not in messages[-1]["content"]
        return {
            "classification": "satisfied",
            "explanation": "The helper retains its accepted responsibility.",
            "evidence_locations": packet["available_evidence"],
        }


def prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    native: bool = False,
    run_id: str = "run-1",
    profile_code: str | None = None,
) -> SourceReferenceRun:
    root = tmp_path / "repo"
    if not root.exists():
        root.mkdir()
        git(root, "init")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        (root / "retry.py").write_text(BASELINE)
        git(root, "add", ".")
        git(root, "commit", "-m", "baseline")
    document = {
        "reference_definitions": [
            ReferenceDefinition(
                "invariant:retry", "invariant", "Keep the retry result.", "spec:1"
            ).to_data(),
            ReferenceDefinition(
                "decision:helper",
                "decision",
                "Isolate the retry calculation.",
                "spec:2",
            ).to_data(),
        ],
        "source_reference_placements": [
            {
                "obligation_id": "place:retry",
                "ref": "invariant:retry",
                "relationship": "preserves",
                "requirement": "Keep the retry result.",
                "owner": {
                    "path": "retry.py",
                    "kind": "function",
                    "qualified_name": "retry",
                },
                "cardinality": "exactly_one",
                "verification_refs": ["case:retry"],
            },
            {
                "obligation_id": "place:helper",
                "ref": "decision:helper",
                "relationship": "realizes",
                "requirement": "Isolate the retry calculation.",
                "owner": {
                    "path": "retry.py",
                    "kind": "function",
                    "qualified_name": "helper",
                },
                "cardinality": "exactly_one",
                "verification_refs": ["case:retry"],
            },
        ],
        "source_reference_preservations": [
            {
                "id": "move:helper",
                "ref": "decision:helper",
                "owner": {
                    "path": "retry.py",
                    "kind": "function",
                    "qualified_name": "retry",
                },
                "disposition": "relocate",
                "reason": "Extract the calculation.",
                "successor": None,
                "destination": {
                    "path": "retry.py",
                    "kind": "function",
                    "qualified_name": "helper",
                },
            },
        ],
    }
    if not native:
        monkeypatch.setattr(SourceReferenceRun, "_probe_protection", lambda self: None)
    monkeypatch.setenv("MSWEA_COST_TRACKING", "ignore_errors")
    try:
        return SourceReferenceRun.prepare(
            workspace_root=root,
            accepted_design=document,
            base_commit=git(root, "rev-parse", "HEAD"),
            objective="Extract the retry helper without changing results.",
            allowed_paths=("retry.py",),
            artifact_root=tmp_path / "artifacts",
            run_id=run_id,
            model="test",
            profiles={
                "behavior": ValidationProfile(
                    "behavior",
                    (
                        sys.executable,
                        "-c",
                        profile_code
                        or (
                            "import retry; "
                            "assert [retry.retry(x) for x in (-1,0,4)] == [0,1,5]"
                        ),
                    ),
                )
            },
            private_validation_data={"oracle": "PRIVATE-ORACLE"},
            review_client=Reviewer(),
            worker_timeout_seconds=30,
            validation_timeout_seconds=30,
        )
    except ValueError as error:
        if native and (
            sys.platform != "darwin"
            or "sandbox_apply: Operation not permitted" in str(error)
        ):
            pytest.skip(
                "Native source protection is unavailable in the outer test environment"
            )
        raise


def unused_inference(request: ChatCompletionRequest) -> ChatCompletionResponse:
    raise AssertionError("No inference expected")


def test_prepared_inputs_are_bound_and_cannot_be_reused_or_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare(tmp_path, monkeypatch)
    data = run.handoff.worker_prompt.to_data()
    assert data["contract"]["workspace_root"] == str(run.workspace_root)
    assert "PRIVATE-ORACLE" not in run.request.prompt
    assert str(run.private_root) not in run.request.prompt
    assert "structrr-reference-completion-v1" in run.request.prompt
    provider = MiniSWEAgentProvider(
        inference_handler=unused_inference,
        model=run.model,
        diagnostics_root=run.private_root,
        timeout_seconds=run.worker_timeout_seconds,
        source_reference_mode="enforce",
        source_reference_catalog=run.catalog,
        source_reader_limits=run.reader_limits,
        source_reference_execution=run,
    )
    run.request.require_supported_execution(provider, run.workspace_root)
    with pytest.raises(ValueError):
        replace(run.request, validation_profiles=()).require_supported_execution(
            provider, run.workspace_root
        )
    provider.source_reference_mode = "observe"
    with pytest.raises(ValueError):
        run.request.require_supported_execution(provider, run.workspace_root)
    run.claim_invocation(run.request, run.run_id)
    with pytest.raises(ValueError):
        run.claim_invocation(run.request, run.run_id)
    (run.private_root / "catalog.json").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        run.verify_inputs()
    with pytest.raises(FileExistsError):
        prepare(tmp_path, monkeypatch)


def test_context_delivery_requires_every_exact_page_and_trusted_catalog() -> None:
    event = {
        "catalog_digest": "c",
        "enrichment_mode": "verified_reader",
        "context_pages": [
            {"ref": "r", "offset": 0, "end_offset": 5, "total_characters": 10}
        ],
    }
    assert not context_delivery_status([event], {"r"}, "c")["complete"]
    continuation = {
        **event,
        "context_pages": [
            {"ref": "r", "offset": 5, "end_offset": 10, "total_characters": 10}
        ],
    }
    assert context_delivery_status([continuation, event], {"r"}, "c")["complete"]
    assert not context_delivery_status(
        [{**event, "catalog_digest": "forged"}, continuation], {"r"}, "c"
    )["complete"]
    assert not context_delivery_status(
        [event, {**continuation, "enrichment_mode": "visible_markers"}], {"r"}, "c"
    )["complete"]


def test_unsupported_protection_has_no_plain_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError, match="Seatbelt"):
        run.protect_command((sys.executable, "-c", "pass"))


@pytest.mark.parametrize(
    "scenario",
    ["success", "wrong_owner", "missing_context", "design_gap", "mutating_validation"],
)
def test_native_fake_model_end_to_end_protects_private_inputs_and_validates_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    run = prepare(
        tmp_path,
        monkeypatch,
        native=True,
        profile_code=(
            "from pathlib import Path; p=Path('retry.py'); "
            "p.write_text(p.read_text()+'\\n# changed by tests\\n')"
            if scenario == "mutating_validation"
            else None
        ),
    )
    candidate = (
        CANDIDATE
        if scenario != "wrong_owner"
        else CANDIDATE.replace("# structrr: decision:helper\n", "")
        + "\n# structrr: decision:helper\ndef unrelated(): return 0\n"
    )
    probe = (
        "from pathlib import Path; p=Path("
        + repr(str(run.private_root / "handoff.json"))
        + ");\n"
        "try:\n p.read_text(); raise AssertionError('private input readable')\n"
        "except PermissionError: pass\n"
    )
    edit = (
        "from pathlib import Path; Path('retry.py').write_text(" + repr(candidate) + ")"
    )
    completion = {
        "schema_version": "structrr-reference-completion-v1",
        "reference_owners": [
            {
                "ref": "invariant:retry",
                "path": "retry.py",
                "kind": "function",
                "qualified_name": "retry",
            },
            {
                "ref": "decision:helper",
                "path": "retry.py",
                "kind": "function",
                "qualified_name": "helper",
            },
        ],
        "design_gaps": [],
    }
    commands = [
        "pwd; git rev-parse --show-toplevel",
        f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}",
        (
            f"{shlex.quote(sys.executable)} -m powdrr_agent.execution.source_reader "
            "read retry.py --start 4 --end 4"
        ),
        f"{shlex.quote(sys.executable)} -c {shlex.quote(edit)}",
        f"{shlex.quote(sys.executable)} -m powdrr_agent.execution.source_reader check",
        "printf '%s\\n' COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT "
        + shlex.quote(json.dumps(completion)),
    ]
    if scenario == "missing_context":
        commands[2] = "cat retry.py"
    if scenario == "design_gap":
        completion["design_gaps"] = [
            {"location": "helper", "consequence": "Needs a design decision"}
        ]
        commands[-1] = (
            "printf '%s\\n' COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT "
            + shlex.quote(json.dumps(completion))
        )
    requests: list[ChatCompletionRequest] = []

    def infer(request: ChatCompletionRequest) -> ChatCompletionResponse:
        assert "PRIVATE-ORACLE" not in json.dumps(request.to_dict())
        requests.append(request)
        if len(requests) == 5 and scenario != "missing_context":
            observations = [m for m in request.messages if m.get("role") == "tool"]
            assert "Keep the retry result." in observations[-2]["content"]
        command = commands[len(requests) - 1]
        call = {
            "id": f"call-{len(requests)}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": command})},
        }
        return ChatCompletionResponse(
            content=None, tool_calls=(call,), finish_reason="tool_calls"
        )

    result = run.execute(infer)
    assert result.accepted is (scenario == "success"), result.to_data()
    assert len(requests) == len(commands), result.to_data()
    if scenario not in {"design_gap", "missing_context"}:
        assert result.to_data()["context_delivery"]["complete"]
    expected = {
        "design_gap": "incomplete_design",
        "missing_context": "incomplete_context",
        "mutating_validation": "invalid_candidate",
    }
    if scenario in expected:
        assert result.to_data()["outcome"] == expected[scenario], result.to_data()
    assert (run.private_root / "terminal.json").is_file()
    assert (run.private_root / "invocation.json").is_file()
    trajectory = json.loads((run.public_root / f"{run.run_id}.traj.json").read_text())
    assert run.request.prompt in trajectory["messages"][1]["content"]
    assert (run.workspace_root / "retry.py").read_text().startswith(candidate)
    with pytest.raises(ValueError, match="twice"):
        run.execute(infer)
