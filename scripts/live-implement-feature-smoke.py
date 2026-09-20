#!/usr/bin/env python3
"""Run one real implement-feature flow in a disposable repository.

This is intentionally an opt-in live acceptance check. It invokes the
production ``workrr-feature`` CLI with the configured planning provider and
OpenCode executable; it is not part of the normal pytest suite.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURE = (
    "Add a second greeting to hello_world.py. Keep Hello, world! first and "
    "print Hello from Powdrr! second."
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-description", default=DEFAULT_FEATURE)
    parser.add_argument("--work-item-name", default="live-hello-world")
    parser.add_argument("--planning-provider", default="deepinfra-cheap")
    parser.add_argument("--planning-model")
    parser.add_argument("--planning-api-key")
    parser.add_argument("--planning-base-url")
    parser.add_argument("--opencode-executable", default="opencode")
    parser.add_argument(
        "--opencode-model",
        default="deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--run-root", type=Path)
    parser.add_argument(
        "--keep-repository",
        action="store_true",
        help="Retain the disposable repository after a successful run.",
    )
    return parser.parse_args()


def _run_root(explicit: Path | None, work_item_name: str) -> Path:
    if explicit is not None:
        return explicit.resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        SOURCE_ROOT / ".powdrr" / "live-implement-feature" / f"{stamp}-{work_item_name}"
    )


def _create_fixture(repository: Path, origin: Path) -> None:
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repository.mkdir(parents=True, exist_ok=False)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    subprocess.run(
        ["git", "config", "user.name", "Powdrr Live Smoke"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "powdrr-live-smoke@example.invalid"],
        cwd=repository,
        check=True,
    )
    (repository / ".gitignore").write_text(".powdrr/\n", encoding="utf-8")
    (repository / "hello_world.py").write_text(
        'print("Hello, world!")\n', encoding="utf-8"
    )
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_hello_world.py").write_text(
        "import subprocess\n\n\n"
        "def test_hello_world() -> None:\n"
        "    result = subprocess.run(\n"
        '        ["python", "hello_world.py"],\n'
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=True,\n"
        "    )\n"
        '    assert result.stdout == "Hello, world!\\nHello from Powdrr!\\n"\n',
        encoding="utf-8",
    )
    shutil.copytree(
        SOURCE_ROOT / "docs" / "procedrr" / "skill-definitions",
        repository / "docs" / "procedrr" / "skill-definitions",
    )
    shutil.copy2(
        SOURCE_ROOT / "software_development_entity_taxonomy.md",
        repository / "software_development_entity_taxonomy.md",
    )
    shutil.copy2(SOURCE_ROOT / "pyproject.toml", repository / "pyproject.toml")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed live implement-feature smoke fixture"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=repository, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "--set-upstream", "origin", "main"],
        cwd=repository,
        check=True,
    )


def _command(
    args: argparse.Namespace, repository: Path, output_root: Path
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "powdrr_lift.cli",
        "workrr-feature",
        "--repo-root",
        str(repository),
        "--feature-description",
        args.feature_description,
        "--work-item-name",
        args.work_item_name,
        "--allowed-path",
        "hello_world.py",
        "--allowed-path",
        "tests",
        "--validation-command",
        f"{sys.executable} -m pytest -q",
        "--planning-provider",
        args.planning_provider,
        "--opencode-executable",
        args.opencode_executable,
        "--opencode-model",
        args.opencode_model,
        "--output-root",
        str(output_root),
        "--no-open-pr",
        "--json",
    ]
    if args.planning_model:
        command.extend(["--planning-model", args.planning_model])
    if args.planning_api_key:
        command.extend(["--planning-api-key", args.planning_api_key])
    if args.planning_base_url:
        command.extend(["--planning-base-url", args.planning_base_url])
    return command


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_result(output_root: Path, stdout: str) -> dict[str, Any] | None:
    result_path = output_root / "run-result.json"
    if result_path.is_file():
        value = json.loads(result_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            return value
    return None


def _assert_success(
    repository: Path, output_root: Path, result: dict[str, Any]
) -> None:
    failures: list[str] = []
    worktree_value = result.get("worktree")
    worktree = Path(worktree_value) if isinstance(worktree_value, str) else repository
    if result.get("status") != "completed":
        failures.append(f"endpoint status={result.get('status')!r}")
    if result.get("review", {}).get("passed") is not True:
        failures.append("post-implementation review did not pass")
    if result.get("validation", {}).get("status") != "passed":
        failures.append("validation did not pass")
    required_artifacts = (
        output_root / "proposal-review-receipt.json",
        output_root / "verification-obligations.json",
    )
    failures.extend(
        f"missing artifact: {path}" for path in required_artifacts if not path.is_file()
    )
    verification_path = output_root / "verification-obligations.json"
    if verification_path.is_file():
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        if verification.get("failures"):
            failures.append(f"verification failures: {verification['failures']}")
        if not verification.get("obligations"):
            failures.append("verification obligations are empty")
    if (worktree / "hello_world.py").read_text(encoding="utf-8") != (
        'print("Hello, world!")\nprint("Hello from Powdrr!")\n'
    ):
        failures.append("hello_world.py does not contain the expected implementation")
    if _git(worktree, "status", "--porcelain"):
        failures.append("implementation worktree has uncommitted changes")
    if _git(repository, "status", "--porcelain"):
        failures.append("source fixture has uncommitted changes")
    if "Implement " not in _git(worktree, "log", "-1", "--format=%s"):
        failures.append("implementation worktree has no implementation commit")
    if not failures:
        return
    raise RuntimeError(
        "live implement-feature smoke failed:\n- " + "\n- ".join(failures)
    )


def main() -> int:
    args = _parse_args()
    run_root = _run_root(args.run_root, args.work_item_name)
    run_root.mkdir(parents=True, exist_ok=False)
    repository = run_root / "repository"
    origin = run_root / "origin.git"
    output_root = run_root / "feature-run"
    _create_fixture(repository, origin)
    command = _command(args, repository, output_root)
    environment = os.environ.copy()
    source_path = str(SOURCE_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path + os.pathsep + environment.get("PYTHONPATH", "")
    )
    print(f"Live implement-feature run: {run_root}", flush=True)
    print("$ " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=repository,
            env=environment,
            check=False,
            timeout=args.timeout,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(f"Timed out after {args.timeout:g}s; artifacts: {run_root}")
        return 1
    result = _load_result(output_root, "")
    if completed.returncode != 0 or result is None:
        print(f"Live run failed; artifacts: {run_root}", file=sys.stderr)
        return 1
    try:
        _assert_success(repository, output_root, result)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"{error}\nArtifacts: {run_root}", file=sys.stderr)
        return 1
    if not args.keep_repository:
        shutil.rmtree(repository)
    print(f"Live implement-feature smoke passed; artifacts: {run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
