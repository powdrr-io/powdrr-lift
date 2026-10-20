from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.clean_pr_publisher import (
    publish_clean_pull_request,
    verify_publication_scope,
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def test_publication_scope_checks_every_path_and_rejects_runtime_state(
    tmp_path: Path,
) -> None:
    intended = [f"attestation/component-{index:02d}.py" for index in range(65)]
    for relative in intended:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("implementation\n", encoding="utf-8")
    runtime = tmp_path / ".powdrr/procedrr-runs/checkpoint.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{}\n", encoding="utf-8")

    accepted = verify_publication_scope({"paths": intended}, worktree_root=tmp_path)
    rejected = verify_publication_scope(
        {"paths": [*intended, ".powdrr/procedrr-runs/checkpoint.json"]},
        worktree_root=tmp_path,
    )

    assert accepted == {"confirmed": True, "findings": [], "paths": intended}
    assert rejected["confirmed"] is False
    assert rejected["paths"][-1] == ".powdrr/procedrr-runs/checkpoint.json"
    assert "not publishable" in rejected["findings"][0]


def test_clean_publication_branch_excludes_checkpoint_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy a large product change set while excluding committed runtime state."""
    remote = tmp_path / "gaterr.git"
    source = tmp_path / "gaterr-source"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-b", "main", str(source), cwd=tmp_path)
    _git("config", "user.name", "Powdrr Test", cwd=source)
    _git("config", "user.email", "powdrr-test@example.invalid", cwd=source)
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-m", "initial", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("switch", "-c", "powdrr/dirty-integration", cwd=source)

    product_paths = [f"attestation/component-{index:02d}.py" for index in range(65)]
    for index, relative in enumerate(product_paths):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"implementation {index}\n", encoding="utf-8")
    runtime_paths = [
        f".powdrr/procedrr-runs/old-run-{index:03d}.json" for index in range(104)
    ]
    for relative in runtime_paths:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("checkpoint state\n", encoding="utf-8")
    _git("add", "attestation", ".powdrr", cwd=source)
    _git("commit", "-m", "implementation and historical checkpoints", cwd=source)

    # New runtime output is also present but untracked in the source worktree.
    transient = source / ".powdrr/procedrr-runs/current.events.jsonl"
    transient.write_text('{"event":"running"}\n', encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh_calls = tmp_path / "gh-calls.txt"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$GH_CALLS"\n'
        'if [ "$1" = "pr" ] && [ "$2" = "view" ]; then\n'
        '  printf \'%s\\n\' \'{"url":"https://github.com/powdrr-io/powdrr-gaterr/pull/901","state":"CLOSED"}\'\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "pr" ] && [ "$2" = "create" ]; then\n'
        "  printf '%s\\n' 'https://github.com/powdrr-io/powdrr-gaterr/pull/902'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_CALLS", str(gh_calls))

    result = publish_clean_pull_request(
        {
            "files_to_publish": product_paths,
            "base": "main",
            "publication_branch": "powdrr/attestation-pr-01-implementation",
            "title": "Add local validation attestation implementation",
            "body": "## Summary\n\nPublish the validated protocol implementation.",
            "commit_message": "Implement local validation attestation PR-01",
        },
        worktree_root=source,
    )

    assert result == "https://github.com/powdrr-io/powdrr-gaterr/pull/902"
    remote_paths = set(
        _git(
            "--git-dir",
            str(remote),
            "ls-tree",
            "-r",
            "--name-only",
            "refs/heads/powdrr/attestation-pr-01-implementation",
            cwd=tmp_path,
        ).splitlines()
    )
    remote_changed_paths = set(
        _git(
            "--git-dir",
            str(remote),
            "diff",
            "--name-only",
            "refs/heads/main...refs/heads/powdrr/attestation-pr-01-implementation",
            cwd=tmp_path,
        ).splitlines()
    )
    assert remote_changed_paths == set(product_paths)
    assert not (set(runtime_paths) & remote_paths)
    assert ".powdrr/procedrr-runs/current.events.jsonl" not in remote_paths
    assert (
        _git("branch", "--show-current", cwd=source).strip()
        == "powdrr/dirty-integration"
    )
    assert (
        _git("status", "--short", cwd=source)
        == "?? .powdrr/procedrr-runs/current.events.jsonl\n"
    )

    calls = gh_calls.read_text(encoding="utf-8")
    assert "pr view powdrr/attestation-pr-01-implementation --json url,state" in calls
    assert (
        "pr create --draft --base main --head powdrr/attestation-pr-01-implementation"
        in calls
    )
