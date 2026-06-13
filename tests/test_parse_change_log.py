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


def test_empty_yaml_returns_empty_changelog() -> None:
    change_log = parse_change_log("")

    assert change_log == type(change_log)()
