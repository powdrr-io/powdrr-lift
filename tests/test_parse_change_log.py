from powdrr_lift import parse_change_log


def test_parse_change_log_maps_yaml_into_dataclasses() -> None:
    change_log = parse_change_log(
        """
        version: 1
        change_id: CHG-2026-001
        title: Introduce JWT refresh token flow

        intent:
          problem: Access tokens expire too frequently
          goal: Maintain authenticated sessions

        decisions:
          - id: ADR-042
            summary: Store refresh tokens in Redis

        entities:
          - id: AuthService
            type: Component
            action: added

        changes:
          - file: src/auth/token_service.py
            span:
              start_line: 42
              end_line: 78
            summary: Added refresh token generation
            affects:
              - AuthService
              - UserSession
            rationale: Keep sessions valid.

        relationship_changes:
          - action: add
            source: AuthService
            target: RedisCache
            relationship: stores_refresh_tokens
            rationale: Refresh tokens must be available to all nodes.
        """
    )

    assert change_log.version == 1
    assert change_log.change_id == "CHG-2026-001"
    assert change_log.title == "Introduce JWT refresh token flow"
    assert change_log.intent.problem == "Access tokens expire too frequently"
    assert change_log.decisions[0].id == "ADR-042"
    assert change_log.entities[0].id == "AuthService"
    assert change_log.entities[0].action == "added"
    assert change_log.changes[0].span.start_line == 42
    assert change_log.changes[0].affects == ["AuthService", "UserSession"]
    assert change_log.relationship_changes[0].relationship == "stores_refresh_tokens"


def test_parse_change_log_maps_version_two_yaml_into_nested_dataclasses() -> None:
    change_log = parse_change_log(
        """
        version: 2
        change_id: CHG-2026-002
        title: Expand review workflow metadata

        intent:
          problem: The changelog format needs richer change-level structure.
          goal: Capture files, entities, invariants, and guidance per hunk.

        decisions:
          - id: ADR-200
            summary: Keep version 1 support while introducing version 2.

        changes:
          - files:
              - path: src/review/workflow.py
                type: modified
                entities:
                  - id: ReviewSkill
                    type: Skill
                  - id: ChangelogValidation
                    type: Tool
                span:
                  start_line: 1
                  end_line: 3
                summary: Update the review workflow file.
                rationale: Capture the file-level context.
            entities:
              - id: ReviewSkill
                type: Skill
                action: added
              - id: LegacyReviewNote
                type: Document
                action: deleted
              - id: ReviewSkill
                type: Skill
                action: modified
            invariants:
              - id: INV-001
                description: Review guidance remains changelog-aware.
                action: added
                related:
                  files:
                    - src/review/workflow.py
                  entities:
                    - ReviewSkill
              - id: INV-002
                description: Validation remains a required step.
                action: altered
                related:
                  guidance:
                    - GUID-001
            guidance:
              - id: GUID-001
                description: Show the validation command explicitly.
                action: added
                related:
                  files:
                    - src/review/workflow.py
                  entities:
                    - ChangelogValidation
                """
    )

    assert change_log.version == 2
    assert change_log.change_id == "CHG-2026-002"
    assert change_log.title == "Expand review workflow metadata"
    assert change_log.decisions[0].id == "ADR-200"
    assert [entity.id for entity in change_log.entities] == [
        "ReviewSkill",
        "LegacyReviewNote",
        "ReviewSkill",
    ]
    assert [entity.action for entity in change_log.entities] == [
        "added",
        "deleted",
        "modified",
    ]
    assert change_log.changes[0].file == "src/review/workflow.py"
    assert change_log.changes[0].files[0].type == "modified"
    assert [entity.id for entity in change_log.changes[0].files[0].entities] == [
        "ReviewSkill",
        "ChangelogValidation",
    ]
    assert change_log.changes[0].files[0].span.start_line == 1
    assert change_log.changes[0].files[0].summary == "Update the review workflow file."
    assert change_log.changes[0].files[0].rationale == "Capture the file-level context."
    assert change_log.changes[0].entities[0].id == "ReviewSkill"
    assert change_log.changes[0].entities[0].action == "added"
    assert change_log.changes[0].entities[1].id == "LegacyReviewNote"
    assert change_log.changes[0].entities[1].action == "deleted"
    assert change_log.changes[0].entities[2].id == "ReviewSkill"
    assert change_log.changes[0].entities[2].action == "modified"
    assert change_log.changes[0].invariants[0].related.entities == ["ReviewSkill"]
    assert change_log.changes[0].guidance[0].related.files == ["src/review/workflow.py"]
    assert change_log.relationship_changes == []


def test_empty_yaml_returns_empty_changelog() -> None:
    change_log = parse_change_log("")

    assert change_log == type(change_log)()
