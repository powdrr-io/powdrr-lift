from pathlib import Path

import pytest

from powdrr_lift.core.behavior_rule import (
    FileBehaviorRuleStore,
    nominate_behavior_rule,
)
from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.effective_contract import resolve_effective_contract
from powdrr_lift.core.intent import (
    IntentClause,
    IntentContract,
    IntentKind,
    IntentStore,
    IntentTrigger,
    make_intent_source,
)
from powdrr_lift.execution.compaction import compatibility_report
from powdrr_lift.execution.runtime import ExecutionRuntime


def _captured(store: IntentStore) -> tuple[IntentStore, IntentClause, IntentClause]:
    source = make_intent_source(
        intent_id="intent-1",
        exact_text="Always use optimistic locking for mutable rows.",
        source_ref="conversation:1/message:1",
        supplied_by="user:test",
    )
    clauses = (
        IntentClause(
            "clause-locking",
            source.intent_id,
            (0, 48),
            IntentKind.INVARIANT,
            IntentContract(
                selectors={"path": ("src/db",), "phase_type": ("build",)},
                trigger=IntentTrigger.BEFORE_ACTION,
                trigger_action="change_mutable_row",
                requirements=("add_optimistic_lock", "run_concurrency_test"),
                precedence=10,
            ),
        ),
        IntentClause(
            "clause-guidance",
            source.intent_id,
            (0, 48),
            IntentKind.GUIDANCE,
            IntentContract(selectors={"repository": ("powdrr-lift",)}),
        ),
    )
    store.capture(source, clauses)
    return store, clauses[0], clauses[1]


def test_intent_source_is_stored_once_and_resolution_is_exact(tmp_path: Path) -> None:
    store, locking, guidance = _captured(IntentStore(tmp_path))

    duplicate_source = make_intent_source(
        intent_id="different-id",
        exact_text="Always use optimistic locking for mutable rows.",
        source_ref="conversation:1/message:1",
        supplied_by="user:test",
    )
    source, clauses, created = store.capture(duplicate_source, (locking, guidance))

    assert not created
    assert source.intent_id == "intent-1"
    assert len(store.sources()) == 1
    contract = resolve_effective_contract(
        store, {"path": "src/db/models.py", "phase_type": "build"}
    )
    assert contract.clause_ids == ("clause-locking",)
    assert contract.fingerprint.startswith("sha256:")
    assert contract.to_data()["clauses"][0]["text"].startswith("Always use")


def test_intent_supersede_and_revoke_use_optimistic_versions(tmp_path: Path) -> None:
    store, locking, _ = _captured(IntentStore(tmp_path))
    replacement = IntentClause(
        "clause-locking-v2",
        locking.intent_id,
        locking.source_span,
        locking.kind,
        IntentContract(selectors={"phase_type": ("validate",)}),
    )
    installed = store.update_clause(
        locking.clause_id, replacement, expected_version=locking.version
    )
    assert installed.supersedes_clause_id == locking.clause_id
    assert [item.clause_id for item in store.list()] == [
        "clause-guidance",
        "clause-locking-v2",
    ]
    with pytest.raises(ValueError, match="stale"):
        store.revoke(installed.clause_id, expected_version=2)
    revoked = store.revoke(installed.clause_id, expected_version=installed.version)
    assert not revoked.active


def test_legacy_behavior_rules_migrate_without_copying_source_per_clause(
    tmp_path: Path,
) -> None:
    legacy = FileBehaviorRuleStore(tmp_path)
    legacy.save(
        nominate_behavior_rule(
            "Prefer focused modules.",
            rule_id="focused-modules",
            source_ref="conversation:2/message:4",
            scope={"profile_id": "default"},
        )
    )
    store = IntentStore(tmp_path)
    assert store.migrate_legacy_behavior_rules() == 1
    assert store.migrate_legacy_behavior_rules() == 0
    assert store.sources()[0].exact_text == "Prefer focused modules."
    assert store.list()[0].kind is IntentKind.GUIDANCE


def test_compatibility_report_rejects_unknown_persisted_versions(
    tmp_path: Path,
) -> None:
    (tmp_path / "unknown.json").write_text(
        '{"schema_version": "future-v9"}\n', encoding="utf-8"
    )
    report = compatibility_report(tmp_path)
    assert report["inspected"] == 1
    assert (
        "unsupported persisted workflow schema"
        in report["diagnostics"][0]["diagnostic"]
    )


def test_runtime_prompt_context_resolves_intent_without_model_retrieval(
    tmp_path: Path,
) -> None:
    store, _, _ = _captured(IntentStore(tmp_path))
    source = make_intent_source(
        intent_id="intent-runtime",
        exact_text="Prefer focused modules.",
        source_ref="conversation:runtime/message:1",
        supplied_by="user:test",
    )
    store.capture(
        source,
        (
            IntentClause(
                "clause-runtime",
                source.intent_id,
                (0, len(source.exact_text)),
                IntentKind.GUIDANCE,
                IntentContract(selectors={"profile_id": ("default",)}),
            ),
        ),
    )
    runtime = ExecutionRuntime(
        "intent-runtime",
        profile_id="default",
        workflow_directory=tmp_path,
        repo_root=tmp_path,
        phase=PhaseType.BUILD,
    )
    context = runtime.prompt_context()
    assert "clause_ids" not in context
    assert "effective_contract" not in context
    assert "contract_fingerprint" not in context
    assert "intent_ids" not in context

    compacted = runtime.compact_prompt_context(
        {**context, "transcript": "long context " * 2_000}
    )
    restored = runtime.retrieve_prompt_context(compacted["full_context_ref"])
    assert restored["runtime_state"] == context

    restarted = ExecutionRuntime(
        "intent-runtime",
        profile_id="default",
        workflow_directory=tmp_path,
        repo_root=tmp_path,
        phase=PhaseType.BUILD,
    )
    assert restarted.prompt_context() == context
