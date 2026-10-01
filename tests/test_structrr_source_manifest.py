from __future__ import annotations

from pathlib import Path

from powdrr_lift.structrr.source_manifest import (
    SourceManifest,
    compare_source_manifests,
    compile_source_manifest,
)


def _manifest(root: Path, paths: tuple[str, ...], revision: str) -> SourceManifest:
    return compile_source_manifest(
        root,
        paths,
        submission_base="base-revision",
        source_revision=revision,
        taxonomy_fingerprint="sha256:taxonomy-v1",
    )


def test_source_manifest_observes_additions_deletions_and_body_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.py").write_text("value = 1\n", encoding="utf-8")
    (root / "src" / "removed.py").write_text("old = True\n", encoding="utf-8")
    (root / "docs" / "current" / "product").mkdir(parents=True)
    (root / "docs" / "current" / "product" / "system-specification.yaml").write_text(
        "id: declared-product\n", encoding="utf-8"
    )
    baseline = _manifest(
        root,
        (
            "src/api.py",
            "src/removed.py",
            "docs/current/product/system-specification.yaml",
        ),
        "commit-base",
    )

    (root / "src" / "api.py").write_text("value = 2\n", encoding="utf-8")
    (root / "src" / "removed.py").unlink()
    (root / "tests").mkdir()
    (root / "tests" / "test_api.py").write_text(
        "def test_api(): pass\n", encoding="utf-8"
    )
    candidate = _manifest(
        root,
        (
            "src/api.py",
            "tests/test_api.py",
            "docs/current/product/system-specification.yaml",
        ),
        "commit-candidate",
    )

    report = compare_source_manifests(baseline, candidate)

    changes = {(item["path"], item["status"]) for item in report["changes"]}
    assert changes == {
        ("src/api.py", "modified"),
        ("src/removed.py", "deleted"),
        ("tests/test_api.py", "added"),
    }
    assert report["passed"] is True
    assert report["baseline_product_digest"] != report["candidate_product_digest"]
    declaration = next(
        item
        for item in baseline.files
        if item.path.endswith("system-specification.yaml")
    )
    assert declaration.evidence_kind == "declaration"


def test_source_manifest_separates_commit_identity_from_product_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("same product\n", encoding="utf-8")
    first = _manifest(root, ("README.md",), "commit-one")
    second = _manifest(root, ("README.md",), "commit-two")

    assert first.product_digest == second.product_digest
    assert first.fingerprint != second.fingerprint
    assert first.to_data()["source_revision"] == "commit-one"


def test_source_manifest_records_exclusions_and_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (root / "docs" / "proposals" / "demo").mkdir(parents=True)
    (root / "docs" / "proposals" / "demo" / "structrr-diff.yaml").write_text(
        "planning: true\n", encoding="utf-8"
    )
    manifest = compile_source_manifest(
        root,
        ("src/module.py", "docs/proposals/demo/structrr-diff.yaml"),
        submission_base="base",
        source_revision="candidate",
        taxonomy_fingerprint="sha256:taxonomy",
        excluded_paths=("docs/proposals/demo/",),
    )

    assert tuple(item.path for item in manifest.files) == ("src/module.py",)
    assert manifest.excluded_paths == ("docs/proposals/demo",)
    assert SourceManifest.from_data(manifest.to_data()) == manifest

    tampered = manifest.to_data()
    tampered["files"][0]["content_digest"] = "sha256:changed"
    try:
        SourceManifest.from_data(tampered)
    except ValueError as error:
        assert "fingerprint" in str(error) or "product digest" in str(error)
    else:
        raise AssertionError("tampered source manifest unexpectedly loaded")


def test_source_manifest_preserves_coverage_errors(tmp_path: Path) -> None:
    manifest = compile_source_manifest(
        tmp_path,
        ("missing.py",),
        submission_base="base",
        source_revision="candidate",
        taxonomy_fingerprint="sha256:taxonomy",
    )

    assert manifest.coverage_complete is False
    assert manifest.extraction_errors[0].startswith("missing.py: FileNotFoundError:")
    assert SourceManifest.from_data(manifest.to_data()) == manifest
