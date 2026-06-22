from powdrr_lift.change_log_parser import parse_change_log


def test_parse_change_log_maps_yaml_into_dataclasses() -> None:
    change_log = parse_change_log(
        """
        schema: https://powdrr.io/schemas/changelog-v1
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
    assert (change_log.decisions or [])[0].id == "ADR-042"
    assert (change_log.entity_changes or [])[0].id == "AuthService"
    assert (change_log.entity_changes or [])[0].action == "added"
    assert (change_log.file_changes or [])[0].span.start_line == 42
    assert (change_log.file_changes or [])[0].related.entities == [
        "AuthService",
        "UserSession",
    ]
    assert (change_log.entity_relationship_changes or [])[0].relationship == (
        "stores_refresh_tokens"
    )


def test_parse_change_log_maps_version_two_yaml_into_nested_dataclasses() -> None:
    change_log = parse_change_log(
        """
        schema: https://powdrr.io/schema/changelog-v2
        change_id: CHG-2026-002
        title: Expand review workflow metadata

        intent:
          problem: The changelog format needs richer change-level structure.
          goal: Capture files, entities, invariants, and guidance per hunk.

        decisions:
          - id: ADR-200
            summary: Keep version 1 support while introducing version 2.

        structured_files:
          - docs/system/system-specification.yaml

        files:
          - path: src/review/workflow.py
            type: modified
            span:
              start_line: 1
              end_line: 3
            summary: Update the review workflow file.
            rationale: Capture the file-level context.
            related:
              entities:
                - ReviewSkill
                - ChangelogValidation

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

        entity_relationships:
          - source: ReviewSkill
            target: ChangelogValidation
            relationship: depends_on
            action: added

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
        features:
          - id: ReviewSkill
            state: in_progress
        prs:
          - id: 42
            state: completed
                """
    )

    assert change_log.version == 2
    assert change_log.change_id == "CHG-2026-002"
    assert change_log.title == "Expand review workflow metadata"
    assert (change_log.decisions or [])[0].id == "ADR-200"
    assert change_log.structured_files == ["docs/system/system-specification.yaml"]
    assert [entity.id for entity in (change_log.entity_changes or [])] == [
        "ReviewSkill",
        "LegacyReviewNote",
        "ReviewSkill",
    ]
    assert [entity.action for entity in (change_log.entity_changes or [])] == [
        "added",
        "deleted",
        "modified",
    ]
    assert (change_log.file_changes or [])[0].path == "src/review/workflow.py"
    assert (change_log.file_changes or [])[0].type == "modified"
    assert (change_log.file_changes or [])[0].entities == [
        "ReviewSkill",
        "ChangelogValidation",
    ]
    assert (change_log.file_changes or [])[0].related.entities == [
        "ReviewSkill",
        "ChangelogValidation",
    ]
    assert (change_log.file_changes or [])[0].span.start_line == 1
    assert (change_log.file_changes or [])[0].summary == (
        "Update the review workflow file."
    )
    assert (change_log.file_changes or [])[0].rationale == (
        "Capture the file-level context."
    )
    assert (change_log.invariant_changes or [])[0].related.entities == ["ReviewSkill"]
    assert (change_log.guidance_changes or [])[0].related.files == [
        "src/review/workflow.py"
    ]
    assert (change_log.entity_relationship_changes or [])[0].relationship == (
        "depends_on"
    )
    assert [feature.id for feature in (change_log.feature_changes or [])] == [
        "ReviewSkill"
    ]
    assert [feature.state for feature in (change_log.feature_changes or [])] == [
        "in_progress"
    ]
    assert [pr.id for pr in (change_log.pr_changes or [])] == ["42"]
    assert [pr.state for pr in (change_log.pr_changes or [])] == ["completed"]


def test_empty_yaml_returns_empty_changelog() -> None:
    change_log = parse_change_log("")

    assert change_log == type(change_log)()
