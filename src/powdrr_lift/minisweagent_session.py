"""Continue a mini-SWE-agent conversation saved as a trajectory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.exceptions import FormatError, InterruptAgentFlow
from minisweagent.models import get_model
from minisweagent.utils.serialize import UNSET, recursive_merge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    previous = json.loads(args.previous.read_text(encoding="utf-8"))
    messages = previous.get("messages") if isinstance(previous, dict) else None
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"trajectory has no messages: {args.previous}")

    config_specs = args.config or [str(builtin_config_dir / "mini.yaml")]
    config = recursive_merge(
        *(get_config_from_spec(spec) for spec in config_specs),
        {
            "run": {"task": args.task},
            "agent": {
                "mode": "yolo",
                "confirm_exit": False,
                "cost_limit": 0,
                "output_path": args.output,
            },
            "model": {"model_name": args.model or UNSET},
        },
    )
    model = get_model(config=config.get("model", {}))
    environment = get_environment(config.get("environment", {}), default_type="local")
    agent = cast(
        Any,
        get_agent(
            model, environment, config.get("agent", {}), default_type="interactive"
        ),
    )
    agent.messages = messages[:-1] if messages[-1].get("role") == "exit" else messages
    agent.add_messages(model.format_message(role="user", content=args.task))

    while True:
        try:
            agent.step()
            agent.n_consecutive_format_errors = 0
        except FormatError as error:
            agent.cost += error.messages[0].get("extra", {}).get("cost", 0.0)
            agent.n_consecutive_format_errors += 1
            if (
                0
                < agent.config.max_consecutive_format_errors
                <= agent.n_consecutive_format_errors
            ):
                agent.add_messages(
                    *error.messages,
                    {
                        "role": "exit",
                        "content": "RepeatedFormatError",
                        "extra": {
                            "exit_status": "RepeatedFormatError",
                            "submission": "",
                        },
                    },
                )
            else:
                agent.add_messages(*error.messages)
        except InterruptAgentFlow as error:
            agent.add_messages(*error.messages)
        finally:
            agent.save(agent.config.output_path)
        if agent.messages[-1].get("role") == "exit":
            status = agent.messages[-1].get("extra", {}).get("exit_status")
            return 0 if status == "Submitted" else 125


if __name__ == "__main__":
    raise SystemExit(main())
