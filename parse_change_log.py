import yaml

class ChangeLog:
    def __init__(self, data):
        self.version = data.get('version')
        self.change_id = data.get('change_id')
        self.title = data.get('title')
        self.intent = Intent(data.get('intent'))
        self.decision = Decision(data.get('decision'))
        self.entities = [Entity(entity) for entity in data.get('entities', [])]
        self.changes = [Change(change) for change in data.get('changes', [])]
        self.relationship_changes = [
            RelationshipChange(relationship_change)
            for relationship_change in data.get('relationship_changes', [])
        ]

class Intent:
    def __init__(self, data):
        self.problem = data.get('problem')
        self.goal = data.get('goal')

class Decision:
    def __init__(self, data):
        self.id = data.get('id')
        self.summary = data.get('summary')

class Entity:
    def __init__(self, data):
        self.id = data.get('id')
        self.type = data.get('type')

class Change:
    def __init__(self, data):
        self.id = data.get('id')
        self.file = data.get('file')
        self.span = Span(data.get('span'))
        self.summary = data.get('summary')
        self.affects = [Affect(affect) for affect in data.get('affects', [])]
        self.rationale = data.get('rationale')

class Span:
    def __init__(self, data):
        self.start_line = data.get('start_line')
        self.end_line = data.get('end_line')

class Affect:
    def __init__(self, data):
        self.id = data

class RelationshipChange:
    def __init__(self, data):
        self.action = data.get('action')
        self.source = data.get('source')
        self.target = data.get('target')
        self.relationship = data.get('relationship')
        self.rationale = data.get('rationale')

def parse_change_log(yaml_content):
    data = yaml.safe_load(yaml_content)
    return ChangeLog(data)

# Example usage
if __name__ == '__main__':
    yaml_content = """
version: 1

change_id: CHG-2026-001

title: Introduce JWT refresh token flow

intent:
  problem: Access tokens expire too frequently
  goal: Maintain authenticated sessions

decision:
  id: ADR-042
  summary: Store refresh tokens in Redis

entities:
  - id: AuthService
    type: Component

  - id: UserSession
    type: DomainEntity

  - id: RedisCache
    type: Infrastructure

changes:

  - id: C1

    file: src/auth/token_service.py

    span:
      start_line: 42
      end_line: 78

    summary: Added refresh token generation

    affects:
      - AuthService
      - UserSession

    rationale: >
      Access tokens should remain short-lived while
      preserving user sessions.

  - id: C2

    file: src/auth/session_manager.py

    span:
      start_line: 120
      end_line: 163

    summary: Persist refresh tokens in Redis

    affects:
      - AuthService
      - RedisCache

    rationale: >
      Centralized storage allows token revocation
      across multiple application instances.

relationship_changes:

  - action: add

    source: AuthService
    target: RedisCache

    relationship: stores_refresh_tokens

    rationale: >
      Refresh tokens must be available to all nodes.
    """

    change_log = parse_change_log(yaml_content)
    print(f"Version: {change_log.version}")
    print(f"Change ID: {change_log.change_id}")
    print(f"Title: {change_log.title}")
    print("Intent:")
    print(f"  Problem: {change_log.intent.problem}")
    print(f"  Goal: {change_log.intent.goal}")
    print("Decision:")
    print(f"  ID: {change_log.decision.id}")
    print(f"  Summary: {change_log.decision.summary}")
    print("Entities:")
    for entity in change_log.entities:
        print(f"  - ID: {entity.id}, Type: {entity.type}")
    print("Changes:")
    for change in change_log.changes:
        print(f"  - ID: {change.id}, File: {change.file}")
        print(f"    Span: Start Line {change.span.start_line}, End Line {change.span.end_line}")
        print(f"    Summary: {change.summary}")
        print("    Affects:")
        for affect in change.affects:
            print(f"      - {affect.id}")
        print(f"    Rationale: {change.rationale}")
    print("Relationship Changes:")
    for relationship_change in change_log.relationship_changes:
        print(f"  - Action: {relationship_change.action}, Source: {relationship_change.source}, Target: {relationship_change.target}")
        print(f"    Relationship: {relationship_change.relationship}")
        print(f"    Rationale: {relationship_change.rationale}")
