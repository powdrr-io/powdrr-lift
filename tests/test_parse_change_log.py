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
              added:
                - id: ReviewSkill
                  type: Skill
              removed:
                - id: LegacyReviewNote
                  type: Document
              relationships:
                - action: altered
                  source: ReviewSkill
                  target: ChangelogValidation
                  relationship: references
                  rationale: The review skill now points at the validation CLI.
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
    ]
    assert [entity.action for entity in change_log.entities] == [
        "added",
        "removed",
    ]
    assert change_log.relationship_changes[0].action == "altered"
    assert change_log.changes[0].file == "src/review/workflow.py"
    assert change_log.changes[0].files[0].type == "modified"
    assert change_log.changes[0].entities[0].id == "ReviewSkill"
    assert change_log.changes[0].entities[0].action == "added"
    assert change_log.changes[0].entities[1].id == "LegacyReviewNote"
    assert change_log.changes[0].entities[1].action == "removed"
    assert change_log.changes[0].entity_relationships[0].target == "ChangelogValidation"
    assert change_log.changes[0].invariants[0].related.entities == ["ReviewSkill"]
    assert change_log.changes[0].guidance[0].related.files == ["src/review/workflow.py"]


def test_empty_yaml_returns_empty_changelog() -> None:
    change_log = parse_change_log("")

    assert change_log == type(change_log)()
