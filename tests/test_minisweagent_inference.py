from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse

from powdrr_lift.inference_ipc import receive_frame, send_frame
from powdrr_lift.minisweagent_monitor import run_minisweagent


def test_miniswe_model_subprocess_calls_service_while_parent_keeps_progressing(
    tmp_path: Path,
) -> None:
    parent, child = socket.socketpair()
    progress_observed = threading.Event()
    requests: list[ChatCompletionRequest] = []

    def infer(request: ChatCompletionRequest) -> ChatCompletionResponse:
        requests.append(request)
        assert progress_observed.wait(timeout=1.0), (
            "parent monitor should continue while the service inference is running"
        )
        command = (
            f"echo ipc-ok > {tmp_path / 'ipc-output.txt'}"
            if len(requests) == 1
            else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        )
        tool_call = {
            "id": f"call-{len(requests)}",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": command}),
            },
        }
        model_response: dict[str, Any] = {
            "id": f"chatcmpl-ipc-test-{len(requests)}",
            "object": "chat.completion",
            "created": 1,
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call],
                    },
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        }
        return ChatCompletionResponse(
            content=None,
            tool_calls=(tool_call,),
            finish_reason="tool_calls",
            usage=model_response["usage"],
            provider_data=model_response,
        )

    trajectory_path = tmp_path / "miniswe.traj.json"
    command = [
        sys.executable,
        "-m",
        "powdrr_lift.minisweagent_session",
        "--task",
        "Run the provided test commands and submit the task.",
        "--model",
        "deepinfra/test-model",
        "--output",
        str(trajectory_path),
    ]
    stdout_seen: list[str] = []
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = os.pathsep.join(
        str(Path.cwd() / source_root)
        for source_root in (
            "src",
            "packages/agent/src",
            "packages/service/src",
            "packages/local/src",
            "packages/contracts/src",
        )
    )
    child_environment["MSWEA_CONFIGURED"] = "true"
    child_environment["MSWEA_COST_TRACKING"] = "ignore_errors"
    child_environment["MSWEA_SILENT_STARTUP"] = "true"
    child_environment["MSWEA_GLOBAL_CONFIG_DIR"] = str(tmp_path / "mini-config")
    child_environment["POWDRR_MINISWE_INFERENCE_FD"] = str(child.fileno())

    def on_snapshot(_snapshot: Any) -> None:
        stdout_seen.append("snapshot")
        progress_observed.set()

    try:
        result = run_minisweagent(
            command,
            trajectory_path=None,
            log_path=None,
            cwd=tmp_path,
            env=child_environment,
            timeout_seconds=30.0,
            poll_interval=0.05,
            on_snapshot=on_snapshot,
            inference_parent=parent,
            inference_child=child,
            inference_handler=infer,
        )
    finally:
        parent.close()
        child.close()

    assert result.returncode == 0, (
        f"{result.stdout}; requests={requests!r}; snapshots={stdout_seen!r}"
    )
    assert (tmp_path / "ipc-output.txt").read_text().strip() == "ipc-ok"
    assert len(requests) == 2
    assert requests[0].model == "deepinfra/test-model"
    assert any(
        message.get("role") == "user"
        and "Run the provided test commands" in str(message.get("content"))
        for message in requests[0].messages
    )
    assert all(request.tools for request in requests)
    assert stdout_seen
    assert trajectory_path.exists()
    trajectory = json.loads(trajectory_path.read_text())
    assert trajectory["messages"][-1]["extra"]["exit_status"] == "Submitted"


def test_inference_frames_are_length_prefixed_and_json_validated() -> None:
    sender, receiver = socket.socketpair()
    try:
        send_frame(sender, {"kind": "infer", "sequence": 7})
        assert receive_frame(receiver) == {"kind": "infer", "sequence": 7}
    finally:
        sender.close()
        receiver.close()


def test_miniswe_timeout_kills_child_during_outstanding_inference(
    tmp_path: Path,
) -> None:
    parent, child = socket.socketpair()
    service_started = threading.Event()
    release_service = threading.Event()
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = os.pathsep.join(
        str(Path.cwd() / source_root)
        for source_root in (
            "src",
            "packages/agent/src",
            "packages/service/src",
            "packages/local/src",
            "packages/contracts/src",
        )
    )
    child_script = """
import socket
import sys
from powdrr_contracts.models import ChatCompletionRequest
from powdrr_lift.inference_ipc import receive_frame, send_frame

connection = socket.socket(fileno=int(sys.argv[1]))
request = ChatCompletionRequest(
    model="deepinfra/test-model",
    messages=({"role": "user", "content": "wait for a slow provider"},),
    request_id="cancel-test",
)
send_frame(connection, {"request": request.to_dict()})
receive_frame(connection)
"""
    command = [sys.executable, "-c", child_script, str(child.fileno())]

    def infer(_request: ChatCompletionRequest) -> ChatCompletionResponse:
        service_started.set()
        release_service.wait(timeout=3.0)
        return ChatCompletionResponse(content="done")

    try:
        result = run_minisweagent(
            command,
            trajectory_path=None,
            log_path=None,
            cwd=tmp_path,
            env=child_environment,
            timeout_seconds=0.5,
            poll_interval=0.05,
            inference_parent=parent,
            inference_child=child,
            inference_handler=infer,
        )
    finally:
        parent.close()
        child.close()
        release_service.set()

    assert service_started.is_set()
    assert result.returncode == 124
