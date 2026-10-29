from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_agent.repository import (
    LocalActionError,
    collect_tracked_worktree,
    execute_command,
    materialize_artifact,
)
from powdrr_contracts.models import Artifact, OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService

from powdrr_lift.cli import main as cli_main
from powdrr_lift.structrr.actual_diff import compile_actual_structrr_diff
from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.structrr.source_manifest import SourceManifest


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _structrr_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "fixture-repository"
    root.mkdir()
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (root / taxonomy.name).write_bytes(taxonomy.read_bytes())
    source = root / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def run() -> str:\n    return 'ok'\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Powdrr tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


def test_service_bootstrap_matches_legacy_fixture_document_and_report(
    tmp_path: Path,
) -> None:
    root, sha = _structrr_repository(tmp_path)
    output_path = f"docs/structrr/current/baseline-{sha[:7]}.yaml"
    legacy = bootstrap_structrr(root, output_path=output_path)
    request = collect_tracked_worktree(
        root,
        request_id="bootstrap-parity",
        operation_kind="bootstrap",
        inputs={
            "output_path": output_path,
            "taxonomy_path": "software_development_entity_taxonomy.md",
            "change_id": "bootstrap",
            "title": None,
            "repository_name": root.name,
        },
    )

    update = JsonServiceBridge(LocalPowdrrService()).start(request)

    assert update.status == "succeeded", update.failure
    outputs = {artifact.artifact_id: artifact for artifact in update.output_artifacts}
    assert legacy.report_path is not None
    assert yaml.safe_load(outputs[output_path].data) == legacy.document
    assert outputs[output_path].data == legacy.output_path.read_bytes()
    assert outputs[str(Path(output_path).with_suffix(".md"))].data == (
        legacy.report_path.read_bytes()
    )


def test_bootstrap_cli_uses_boundary_and_materializes_service_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _structrr_repository(tmp_path)

    exit_code = cli_main(["bootstrap-structrr", "--repo-root", str(root), "--json"])

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    output_path = Path(summary["output_path"])
    report_path = Path(summary["report_path"])
    assert exit_code == 0
    assert output_path.is_file()
    assert report_path.is_file()
    assert summary["entity_count"] == 3
    assert "## Code structure" in report_path.read_text(encoding="utf-8")
    assert captured.err == ""


def test_candidate_diff_through_service_matches_legacy_structrr_diff(
    tmp_path: Path,
) -> None:
    root, _ = _structrr_repository(tmp_path)
    service = JsonServiceBridge(LocalPowdrrService())

    def bootstrap(request_id: str, output_path: str) -> dict[str, Any]:
        request = collect_tracked_worktree(
            root,
            request_id=request_id,
            operation_kind="bootstrap",
            inputs={
                "output_path": output_path,
                "taxonomy_path": "software_development_entity_taxonomy.md",
                "change_id": "bootstrap",
                "title": "Fixture Structrr",
                "repository_name": root.name,
            },
        )
        update = service.start(request)
        assert update.status == "succeeded", update.failure
        return yaml.safe_load(
            next(
                item.data
                for item in update.output_artifacts
                if item.artifact_id == output_path
            )
        )

    baseline = bootstrap("baseline-bootstrap", "out/baseline.yaml")
    source = root / "src/app.py"
    source.write_text("def run() -> str:\n    return 'candidate'\n", encoding="utf-8")
    candidate = bootstrap("candidate-bootstrap", "out/candidate.yaml")
    request = OperationRequest(
        request_id="candidate-diff",
        operation_kind="compile_structrr_diff",
        artifacts=(
            Artifact(
                "baseline/structrr.yaml",
                "application/yaml",
                yaml.safe_dump(baseline, sort_keys=False).encode(),
            ),
            Artifact(
                "candidate/structrr.yaml",
                "application/yaml",
                yaml.safe_dump(candidate, sort_keys=False).encode(),
            ),
        ),
    )

    update = service.start(request)

    assert update.status == "succeeded", update.failure
    actual = json.loads(update.output_artifacts[0].data)
    expected = compile_actual_structrr_diff(
        baseline,
        candidate,
        SourceManifest.from_data(baseline["source_manifest"]),
        SourceManifest.from_data(candidate["source_manifest"]),
    )
    assert actual == expected.to_data()
    assert "src/app.py" in actual["behavioral_review_candidates"]


def test_snapshot_includes_dirty_tracked_content_but_omits_untracked_files(
    tmp_path: Path,
) -> None:
    root, _ = _structrr_repository(tmp_path)
    tracked = root / "src/app.py"
    tracked.write_text("def run() -> str:\n    return 'dirty'\n", encoding="utf-8")
    (root / "untracked.py").write_text("untracked = True\n", encoding="utf-8")

    request = collect_tracked_worktree(
        root,
        request_id="dirty-worktree",
        operation_kind="bootstrap",
        inputs={},
    )

    files = {artifact.artifact_id: artifact.data for artifact in request.artifacts}
    assert files["src/app.py"].endswith(b"dirty'\n")
    assert "untracked.py" not in files
    assert request.repository is not None
    assert request.repository.head_sha != ""
    assert request.inputs["snapshot_policy"] == {
        "tracked_content": "worktree",
        "dirty_tracked_included": True,
        "untracked_included": False,
    }


def test_materialize_artifact_rejects_forbidden_paths_and_stale_preconditions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = root / "result.yaml"
    destination.write_text("old\n", encoding="utf-8")
    initial_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.write_text("concurrent change\n", encoding="utf-8")

    with pytest.raises(LocalActionError) as stale:
        materialize_artifact(
            root,
            Artifact("result.yaml", "application/yaml", b"new\n"),
            expected_digest=initial_digest,
        )
    assert stale.value.category == "stale_precondition"

    with pytest.raises(LocalActionError) as forbidden:
        materialize_artifact(
            root,
            Artifact("../outside.yaml", "application/yaml", b"bad\n"),
            expected_digest=None,
        )
    assert forbidden.value.category == "forbidden_path"


def test_local_command_failure_is_returned_as_a_receipt() -> None:
    receipt, stdout, stderr = execute_command(
        Path.cwd(),
        (sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"),
        timeout_seconds=10,
    )

    assert receipt.status == "failed"
    assert receipt.exit_code == 7
    assert stdout.data == b"failed\n"
    assert stderr.data == b""


def test_local_command_timeout_is_distinct_from_nonzero_exit(tmp_path: Path) -> None:
    receipt, _, _ = execute_command(
        tmp_path,
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_seconds=1,
    )

    assert receipt.status == "timed_out"
    assert receipt.exit_code is None
