from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.structrr.rebase import rebase_structrr_snapshot, snapshot_digest

PROJECT_ROOT = Path(__file__).parents[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _bootstrap(repo: Path, output_path: Path) -> dict:
    result = bootstrap_structrr(repo, output_path=output_path)
    assert result.validation.successful, result.validation.issues
    return result.document


def test_bootstrap_real_powdrr_checkout_is_valid_and_repeatable(
    tmp_path: Path,
) -> None:
    first = _bootstrap(PROJECT_ROOT, tmp_path / "first.yaml")
    second = _bootstrap(PROJECT_ROOT, tmp_path / "second.yaml")

    subjects = first["source_subjects"]
    bindings = first["source_bindings"]
    subject_ids = {subject["id"] for subject in subjects}
    entity_ids = {entity["id"] for entity in first["entities"]}

    assert len(subjects) > 100
    assert len(subject_ids) == len(subjects)
    assert any(
        subject["qualified_name"]
        == "src.powdrr_lift.structrr.bootstrap.bootstrap_structrr"
        for subject in subjects
    )
    assert bindings
    assert all(binding["subject_id"] in subject_ids for binding in bindings)
    assert all(binding["entity_id"] in entity_ids for binding in bindings)
    assert snapshot_digest(first) == snapshot_digest(second)


def test_rebase_bootstrapped_git_history_filters_unrelated_changes_and_tracks_move(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "real-history"
    repo.mkdir()
    (repo / "software_development_entity_taxonomy.md").write_text(
        (PROJECT_ROOT / "software_development_entity_taxonomy.md").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src/example.py").write_text(
        "class Client:\n"
        "    def search(self, query: str) -> str:\n"
        "        return query\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Structrr Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")

    baseline = _bootstrap(repo, tmp_path / "baseline.yaml")
    old_subject = next(
        subject
        for subject in baseline["source_subjects"]
        if subject["kind"] == "method"
        and subject["qualified_name"].endswith("Client.search")
    )

    (repo / "README.md").write_text("An unrelated product note.\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "unrelated change")
    unrelated = _bootstrap(repo, tmp_path / "unrelated.yaml")
    unrelated_report = rebase_structrr_snapshot(
        baseline,
        unrelated,
        referenced_source_subject_keys=(old_subject["stable_key"],),
    )
    assert unrelated_report.classification == "clean"

    _git(repo, "mv", "src/example.py", "src/client.py")
    _git(repo, "commit", "-qam", "move client module")
    moved = _bootstrap(repo, tmp_path / "moved.yaml")
    moved_report = rebase_structrr_snapshot(
        unrelated,
        moved,
        referenced_source_subject_keys=(old_subject["stable_key"],),
    )

    assert moved_report.classification == "mechanically_rebased"
    assert len(moved_report.remappings) == 1
    assert moved_report.remappings[0].before == old_subject["stable_key"]
