from powdrr_lift.core.action_relationship import (
    ActionFact,
    ActionRelationship,
    expand_action_relationships,
    validate_relationship_graph,
)
from powdrr_lift.execution.relationships import (
    action_can_complete,
    expand_obligations,
    explain_obligation,
    satisfy_obligation,
)


def test_review_edit_expands_validation_then_thread_resolution() -> None:
    initial = expand_obligations((ActionFact("edit_for_review_comment"),))
    assert [item.required_action for item in initial.obligations] == ["run_validation"]
    validated = expand_obligations(
        (ActionFact("edit_for_review_comment", frozenset({"validated"})),)
    )
    assert [item.required_action for item in validated.obligations] == [
        "resolve_review_thread",
        "run_validation",
    ]
    assert "review-edit-requires-thread-resolution" in explain_obligation(
        validated.obligations[0]
    )
    assert not action_can_complete("resolve_review_thread", initial.obligations)
    assert action_can_complete("resolve_review_thread", validated.obligations[0:1])
    assert satisfy_obligation(validated.obligations[0], "resolve_review_thread")
    assert not satisfy_obligation(validated.obligations[0], "run_validation")


def test_mutable_row_expands_unlabeled_safety_work() -> None:
    obligations = expand_action_relationships((ActionFact("change_mutable_row"),))
    assert {item.required_action for item in obligations} == {
        "add_optimistic_lock",
        "run_concurrency_test",
    }
    already_safe = expand_action_relationships(
        (
            ActionFact(
                "change_mutable_row",
                frozenset({"optimistic_locking", "concurrency_evidence"}),
            ),
        )
    )
    assert already_safe == ()


def test_relationships_are_deduplicated_and_cycles_rejected() -> None:
    relation = ActionRelationship("r", "edit", "validate", "validate it")
    facts = (ActionFact("edit"), ActionFact("edit"))
    assert len(expand_action_relationships(facts, relationships=(relation,))) == 1
    cycle = (
        ActionRelationship("a", "one", "two", "two"),
        ActionRelationship("b", "two", "one", "one"),
    )
    assert validate_relationship_graph(cycle)
    assert not expand_obligations(facts, relationships=cycle).valid
