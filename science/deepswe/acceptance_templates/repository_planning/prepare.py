"""Prepare an oracle-free DeepSWE environment for planning, without executing tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from science.deepswe.acceptance_templates.common import write_json

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def command(*args: str, timeout: int = 180) -> str:
    return subprocess.check_output(list(args), text=True, timeout=timeout).strip()


def inspect(container: str, request: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "python",
            "-m",
            "repository_planning.worker",
        ],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )
    return json.loads(result.stdout)


def context(task: Path, output: Path, base_image: str) -> None:
    if output.exists():
        raise ValueError("Use a fresh setup output directory")
    output.mkdir(parents=True)
    build = output / "build-context"
    build.mkdir()
    shutil.copytree(
        ROOT / "src", build / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copyfile(HERE / "worker.py", build / "worker.py")
    shutil.copyfile(HERE / "symbols.py", build / "symbols.py")
    (build / "Dockerfile").write_text(
        f"FROM {base_image}\n"
        "RUN pip install basedpyright==1.39.9 PyYAML jsonschema\n"
        "COPY src /opt/powdrr/src\n"
        "COPY worker.py /opt/planning/repository_planning/worker.py\n"
        "COPY symbols.py /opt/planning/repository_planning/symbols.py\n"
        "ENV PYTHONPATH=/opt/powdrr/src:/opt/planning PYTHONDONTWRITEBYTECODE=1\n"
        'CMD ["sleep", "infinity"]\n'
    )
    # Only these original task artifacts are copied. No task directory mount.
    shutil.copyfile(task / "instruction.md", output / "instruction.md")
    shutil.copyfile(task / "environment/Dockerfile", output / "task.Dockerfile")
    write_json(
        output / "context.json",
        {
            "task_id": task.name,
            "base_image": base_image,
            "instruction_sha256": hashlib.sha256(
                (output / "instruction.md").read_bytes()
            ).hexdigest(),
            "task_dockerfile_sha256": hashlib.sha256(
                (output / "task.Dockerfile").read_bytes()
            ).hexdigest(),
            "source_revision": command("git", "-C", str(ROOT), "rev-parse", "HEAD"),
            "powdrr_sources": {
                str(p.relative_to(build)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(build.rglob("*"))
                if p.is_file()
            },
            "tests_run": False,
            "oracle_mounted": False,
        },
    )


def start(output: Path, image: str, container: str, base_sha: str) -> None:
    command(
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/analysis",
        image,
    )
    try:
        status = inspect(container, {"operation": "status"})
        if status != {"head": base_sha, "status": ""}:
            raise ValueError(f"Task checkout mismatch: {status}")
        bootstrap = inspect(container, {"operation": "bootstrap"})
        write_json(output / "bootstrap-response.json", bootstrap)
        if bootstrap.get("error"):
            raise ValueError(f"Bootstrap failed: {bootstrap}")
        command("docker", "cp", f"{container}:/analysis/.", str(output / "bootstrap"))
        write_json(
            output / "environment.json",
            {
                "container": container,
                "image": image,
                "image_id": command(
                    "docker", "image", "inspect", "--format", "{{.Id}}", image
                ),
                "base_sha": base_sha,
                "initial_status": status,
                "network": "none",
                "read_only": True,
                "packages": command(
                    "docker", "exec", container, "python", "-m", "pip", "freeze"
                ),
            },
        )
    except Exception:
        command("docker", "stop", container)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["context", "start"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--base-image")
    parser.add_argument("--image")
    parser.add_argument("--container")
    parser.add_argument("--base-sha")
    args = parser.parse_args()
    if args.stage == "context":
        if not args.task or not args.base_image:
            parser.error("context requires --task and --base-image")
        context(args.task, args.output, args.base_image)
    else:
        if not args.image or not args.container or not args.base_sha:
            parser.error("start requires --image, --container and --base-sha")
        start(args.output, args.image, args.container, args.base_sha)


if __name__ == "__main__":
    main()
