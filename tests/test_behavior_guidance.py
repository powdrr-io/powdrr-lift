from pathlib import Path

import pytest

from powdrr_lift.core.behavior_rule import (
    BehaviorRule,
    FileBehaviorRuleStore,
    applicable_behavior_rules,
    nominate_behavior_rule,
)
from powdrr_lift.execution.guidance import load_applicable_guidance


def rule(rule_id: str = "review-comments") -> BehaviorRule:
    return nominate_behavior_rule(
        "When changes address review comments, resolve the comments afterwards.",
        rule_id=rule_id,
        source_ref="conversation:42",
        scope={"repository": "powdrr-lift", "phase": "resolve_findings"},
    )


def test_guidance_is_scoped_and_precedence_ordered() -> None:
    broad = rule("broad")
    narrow = rule("narrow")
    narrow = type(narrow)(
        narrow.rule_id,
        narrow.text,
        narrow.normalized_text,
        narrow.source_ref,
        narrow.scope,
        10,
    )
    matched = applicable_behavior_rules(
        (broad, narrow), {"repository": "powdrr-lift", "phase": "resolve_findings"}
    )
    assert [item.rule_id for item in matched] == ["narrow", "broad"]
    assert not applicable_behavior_rules(
        (broad,), {"repository": "other", "phase": "resolve_findings"}
    )


def test_guidance_survives_restart_and_is_explainable(tmp_path: Path) -> None:
    store = FileBehaviorRuleStore(tmp_path)
    saved = store.save(rule())
    reloaded = FileBehaviorRuleStore(tmp_path).list()[0]
    assert reloaded == saved
    assert reloaded.source_ref == "conversation:42"
    assert load_applicable_guidance(
        FileBehaviorRuleStore(tmp_path),
        {"repository": "powdrr-lift", "phase": "resolve_findings"},
    ) == (saved,)
    explanation = store.explain(saved.rule_id)
    assert explanation["rule"]["rule_id"] == saved.rule_id
    assert explanation["superseded_by"] == ()
    assert explanation["applicable"] is True


def test_stale_update_and_revoke_are_rejected(tmp_path: Path) -> None:
    store = FileBehaviorRuleStore(tmp_path)
    saved = store.save(rule())
    updated = store.save(rule(), expected_version=saved.version)
    assert updated.version == 2
    with pytest.raises(ValueError, match="stale"):
        store.save(rule(), expected_version=saved.version)
    revoked = store.revoke(saved.rule_id, expected_version=updated.version)
    assert not revoked.active
    assert store.list() == ()
    assert store.explain(saved.rule_id)["applicable"] is False


def test_empty_rule_is_not_nominated() -> None:
    with pytest.raises(ValueError):
        nominate_behavior_rule(
            " ", rule_id="empty", source_ref="message:1", scope={"repository": "x"}
        )
