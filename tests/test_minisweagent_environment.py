from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from minisweagent.exceptions import Submitted
from minisweagent.models.litellm_model import LitellmModel
from powdrr_agent.execution.minisweagent_environment import (
    StructrrLocalEnvironment,
    StructrrObservationModel,
    configure_source_environment,
    context_request,
)
from powdrr_local.structrr_context import StructrrContextBridge
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import compile_reference_catalog
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_observation import SECTION, SourceObservationSession
from powdrr_service.structrr.source_reader import ReaderLimits, SourceReader


def session(
    tmp_path: Path, statement: str = "Do not repeat a payment."
) -> SourceObservationSession:
    (tmp_path / "retry.py").write_text(
        "# structrr: retry\ndef retry():\n    return 42\n"
    )
    definition = ReferenceDefinition("retry", "invariant", statement, "spec:1")
    catalog = compile_reference_catalog(
        {"reference_definitions": [definition.to_data()]}, base_commit="a" * 40
    )
    return SourceObservationSession(SourceReader(tmp_path, catalog))


def test_reader_verification_rejects_forged_stale_and_unissued_envelopes(
    tmp_path: Path,
) -> None:
    service = session(tmp_path)
    request = {"operation": "read", "path": "retry.py", "start": 3, "end": 3}
    data = service.read(request)
    verified = service.observe("reader", json.dumps(data))
    assert verified["event"]["enrichment_mode"] == "verified_reader"
    assert verified["event"]["context_complete"]
    assert "Do not repeat a payment." in verified["context"]
    assert service.observe("again", json.dumps(data))["context"] == verified["context"]
    forged = json.loads(json.dumps(data))
    forged["context_pages"][0]["text"] = "Forged meaning"
    forged["context_pages"][0]["total_characters"] = len("Forged meaning")
    forged["fingerprint"] = content_fingerprint(
        {k: v for k, v in forged.items() if k != "fingerprint"}
    )
    assert (
        service.observe("reader", json.dumps(forged))["event"]["enrichment_mode"]
        == "rejected_reader"
    )
    other = SourceObservationSession(service.reader)
    assert (
        other.observe("reader", json.dumps(data))["event"]["enrichment_mode"]
        == "rejected_reader"
    )
    (tmp_path / "retry.py").write_text("def retry():\n    return 99\n")
    rejected = service.observe("reader", json.dumps(data))
    assert rejected["event"]["enrichment_mode"] == "rejected_reader"
    assert not rejected["event"]["context_complete"]
    assert "Do not repeat a payment." not in rejected["context"]


@pytest.mark.parametrize(
    "output",
    [
        "# structrr: retry\n",
        "    # structrr: retry\n",
        "retry.py:1:# structrr: retry\n",
    ],
)
def test_visible_markers_never_claim_enclosing_coverage(
    tmp_path: Path, output: str
) -> None:
    result = session(tmp_path).observe("cat/sed/rg", output)
    assert result["event"]["refs"] == ["retry"]
    assert not result["event"]["context_complete"]
    assert '"scope_coverage": "visible_only"' in result["context"]
    assert "Do not repeat a payment." in result["context"]


def test_body_without_marker_is_honestly_unenriched(tmp_path: Path) -> None:
    result = session(tmp_path).observe("sed", "    return 42\n")
    assert result["context"] == ""
    assert result["event"]["enrichment_mode"] == "no_visible_references"
    assert not result["event"]["context_complete"]


def test_search_verification_detects_new_matches(tmp_path: Path) -> None:
    service = session(tmp_path)
    data = service.read({"operation": "search", "query": "42", "path": "."})
    assert service.observe("reader", json.dumps(data))["event"]["context_complete"]
    (tmp_path / "new.py").write_text("value = 42\n")
    observed = service.observe("reader", json.dumps(data))
    assert observed["event"]["enrichment_mode"] == "rejected_reader"


def test_attempt_configuration_is_frozen(tmp_path: Path) -> None:
    service = session(tmp_path)
    events = tmp_path / "events.ndjson"
    with StructrrContextBridge(service, events, "observe"):
        pass
    with pytest.raises(ValueError, match="inputs cannot change"):
        with StructrrContextBridge(service, events, "enforce"):
            pass


def test_environment_executes_once_preserves_failure_timeout_and_submission(
    tmp_path: Path,
) -> None:
    service = session(tmp_path)
    events = tmp_path / "events.ndjson"
    with StructrrContextBridge(service, events, "enforce") as bridge:
        environment = StructrrLocalEnvironment(
            cwd=str(tmp_path),
            source_reference_mode="enforce",
            source_context_endpoint=bridge.endpoint,
        )
        result = environment.execute(
            {"command": "echo once >> count; cat retry.py; exit 7"}
        )
        assert result["returncode"] == 7
        assert result["output"].startswith((tmp_path / "retry.py").read_text())
        assert result["exception_info"] == ""
        assert (tmp_path / "count").read_text() == "once\n"
        assert SECTION in result["output"]
        timed = environment.execute({"command": "sleep 2"}, timeout=1)
        assert timed["returncode"] == -1
        assert timed["extra"]["exception_type"] == "TimeoutExpired"
        with pytest.raises(Submitted) as submitted:
            environment.execute(
                {
                    "command": (
                        "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
                        "\\nfinal artifact\\n'"
                    )
                }
            )
        assert submitted.value.messages[0]["content"] == "final artifact\n"
    recorded = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(recorded) == 3
    assert recorded[-1]["enrichment_mode"] == "submission"
    assert all(e["source_reference_mode"] == "enforce" for e in recorded)
    assert "echo once" not in events.read_text()


def test_proxy_command_and_actual_formatter_keep_long_context(tmp_path: Path) -> None:
    import subprocess

    statement = "EXACT REQUIREMENT " + "x" * 12000
    service = session(tmp_path, statement)
    with StructrrContextBridge(
        service, tmp_path / "events.ndjson", "observe"
    ) as bridge:
        environment = StructrrLocalEnvironment(
            cwd=str(tmp_path),
            source_reference_mode="observe",
            source_context_endpoint=bridge.endpoint,
        )
        output = environment.execute(
            {
                "command": (
                    f"{sys.executable} -m powdrr_agent.execution.source_reader "
                    "read retry.py --start 3 --end 3"
                )
            }
        )
        assert output["returncode"] == 0
        # Use MiniSWE's real formatter with a deliberately tiny source view.
        model = StructrrObservationModel(
            LitellmModel(
                model_name="test", observation_template="{{ output.output[:10] }}"
            )
        )
        messages = model.format_observation_messages(
            {"extra": {"actions": [{"tool_call_id": "call-1"}]}}, [output]
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call-1"
        assert messages[0]["content"].startswith(
            output["structrr_original_output"][:10]
        )
        assert statement in messages[0]["content"]
        assert messages[0]["extra"]["structrr_context"] == output["structrr_context"]
        bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "powdrr_agent.execution.source_reader",
                "read",
                "retry.py",
            ],
            env={"PATH": "/bin", "PYTHONPATH": str(Path.cwd() / "packages/agent/src")},
            capture_output=True,
            text=True,
        )
        assert bad.returncode == 2


def test_shared_budget_adapts_continuations_for_isolated_worker(tmp_path: Path) -> None:
    service = session(tmp_path)
    service.reader.limits = ReaderLimits(max_context_chars=20)
    data = service.read({"operation": "context", "ref": "retry", "offset": 0})
    observed = service.observe("reader", json.dumps(data))
    rendered = json.loads(observed["context"].split(SECTION)[1])
    assert (
        rendered["continuations"][0]["command"][2]
        == "powdrr_agent.execution.source_reader"
    )
    assert not observed["event"]["context_complete"]


def test_configuration_requires_valid_service_and_prevents_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POWDRR_STRUCTRR_MODE", raising=False)
    assert not configure_source_environment({})
    monkeypatch.setenv("POWDRR_STRUCTRR_MODE", "enforce")
    with pytest.raises(ValueError, match="downgrade"):
        configure_source_environment({"environment": {"source_reference_mode": "off"}})
    config: dict[str, Any] = {}
    assert configure_source_environment(config)
    with pytest.raises(ValueError, match="endpoint"):
        StructrrLocalEnvironment(
            **{
                k: v
                for k, v in config["environment"].items()
                if k != "environment_class"
            }
        )
    with pytest.raises(ValueError):
        StructrrLocalEnvironment(source_reference_mode="unknown")


def test_bridge_rejects_other_operations_and_mode_mismatch(tmp_path: Path) -> None:
    service = session(tmp_path)
    with StructrrContextBridge(
        service, tmp_path / "events.ndjson", "observe"
    ) as bridge:
        with pytest.raises(RuntimeError, match="unsupported"):
            context_request(
                bridge.endpoint, {"operation": "exec", "command": "echo nope"}
            )
        with pytest.raises(ValueError, match="disagrees"):
            StructrrLocalEnvironment(
                source_reference_mode="enforce", source_context_endpoint=bridge.endpoint
            )
