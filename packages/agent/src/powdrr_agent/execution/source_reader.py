"""Public reader command for an isolated worker; meaning stays in the service."""

from __future__ import annotations

import argparse
import json
import os
import socket
from typing import Any

from powdrr_agent.execution.inference_ipc import receive_frame, send_frame


def context_request(endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(endpoint)
        send_frame(connection, request)
        response = receive_frame(connection)
    if response.get("ok") is not True or not isinstance(response.get("response"), dict):
        raise RuntimeError(str(response.get("error", "Invalid Structrr IPC response")))
    return dict(response["response"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read source with service-owned Structrr context"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    read = commands.add_parser("read")
    read.add_argument("path")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("path", nargs="?", default=".")
    context = commands.add_parser("context")
    context.add_argument("--ref", required=True)
    context.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        response = context_request(
            os.environ["POWDRR_STRUCTRR_ENDPOINT"],
            {"operation": "reader", "request": vars(args)},
        )
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "structrr-source-reader-initialization-error-v1",
                    "status": "error",
                    "code": "invalid_reader_configuration",
                    "message": str(error),
                }
            )
        )
        return 2
    # Continuation commands must work inside the isolated worker distribution.
    # Keep the issued envelope exact; the formatter supplies adapted commands.
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
