# Typed Resource Identifiers

## Proposal

Powdrr should give every durable resource one canonical identifier with the
shape:

```text
<type>:<id>
```

The type should be hierarchical and namespaced:

```text
powdrr/<domain>/<resource>:<opaque-id>
```

Examples:

```text
powdrr/execution/run:01JQ8T2K7M4F
powdrr/execution/action:01JQ8T2N91AB
powdrr/execution/obligation:01JQ8T30C6PZ
powdrr/intent/invariant:01JQ8T5F3M9C
powdrr/workflow/task:implement-feature
powdrr/artifact/diff:sha256-abc123
```

The type answers “what is this?” and the opaque ID answers “which one is it?”
Relationships, versions, status, and attempts belong in structured fields,
not in the identifier.

This is a resource identity contract, not a requirement that every string
called an “id” become a resource. A value that identifies a resource must use
this contract. A local lookup key, enum, tool name, phase name, or other
non-resource identifier must be named as a `*_key`, `*_name`, or equivalent.
Identifiers for external systems must use an explicit `external_ref` object.

## Why change

The codebase currently has several ID conventions: execution IDs are arbitrary
strings, events and capability exceptions compose IDs with colons, shadow
execution uses dash-generated action and event IDs, evidence has its own
prefixes, and workflow task IDs carry topology that is later parsed to infer a
workflow. This makes a string difficult to interpret and encourages each
producer to invent another convention.

A typed identifier provides one representation across execution state,
durable intent, structured specifications, workflows, CLI output, MCP payloads,
logs, and prompts. It also makes invalid references detectable before they
reach the execution kernel or the LLM.

## Initial type registry

### Execution

```text
powdrr/execution/run
powdrr/execution/event
powdrr/execution/action
powdrr/execution/attempt
powdrr/execution/obligation
powdrr/execution/evidence
powdrr/execution/finding
powdrr/execution/checkpoint
```

An action remains the same resource when retried. Each retry is a separate
`execution/attempt`; a new obligation must not be manufactured by concatenating
the action ID and a rule name.

### Intent and specification

```text
powdrr/intent/source
powdrr/intent/clause
powdrr/intent/invariant
powdrr/intent/procedure
powdrr/intent/contract
powdrr/spec/entity
powdrr/spec/relationship
```

Original user intent should receive a durable `intent/source` ID once. Clauses,
procedures, and invariants reference that source. An effective contract is a
compiled view of those clauses and should only receive its own durable ID if it
must be independently persisted or replayed. This keeps typed IDs from
creating a state explosion.

### Workflow, repository, artifacts, and controls

```text
powdrr/workflow/skill
powdrr/workflow/plan
powdrr/workflow/unit
powdrr/workflow/task
powdrr/repository/worktree
powdrr/repository/branch
powdrr/repository/commit
powdrr/repository/pull-request
powdrr/artifact/document
powdrr/artifact/diff
powdrr/artifact/tool-output
powdrr/control/capability-exception
powdrr/control/user-decision
```

The complete initial registry also includes the resource types represented by
existing durable fields and relationships:

```text
powdrr/execution/plan
powdrr/execution/relationship
powdrr/intent/rule
powdrr/workflow/instance
powdrr/workflow/profile
powdrr/control/actor
```

Every field that currently carries a resource identity, including fields such
as `plan_id`, `profile_id`, `rule_id`, `relationship_id`, `actor_id`, and
`checkpoint_id`, must map to exactly one registered type. The migration is not
complete while an identifier-bearing resource remains an untyped string. The
registry is the source of truth; call sites may not introduce a new resource
type without adding it there first.

External identifiers, such as a GitHub pull request number, should remain an
explicit `external_ref` rather than being confused with a Powdrr resource ID.

## Identifier rules

1. Types are lowercase ASCII, stable, and slash-separated. A type has the
   grammar `powdrr/<domain>/<resource>` where each segment contains only
   lowercase letters, digits, and hyphens, and starts and ends with a letter or
   digit. New types are added to the registry rather than invented at call
   sites.
2. The canonical separator is the first colon after the type. The ID portion
   is non-empty, contains no colon or slash, and uses only ASCII letters,
   digits, `.`, `_`, and `-`. Canonical serialization is case-sensitive for
   IDs and lowercase for types; parsers reject non-canonical spellings rather
   than normalizing them silently.
3. Runtime-created IDs use UUIDv7 or ULID. Generators own collision
   prevention, and IDs are unique for their registered type across the durable
   store. Human-authored workflow IDs are readable but still receive a
   person-derived prefix of no more than five lowercase ASCII characters:
   `<person-prefix>-<workflow-key>`. The prefix is derived from the configured
   person's stable handle or name, is not guessed from prompt text, and is
   recorded as metadata. It is a routing/ownership prefix, not a substitute
   for a person resource reference. Prefix collisions are resolved by the
   workflow key or explicit workflow scope; the prefix itself is never extended
   beyond five characters.
4. IDs are never reused. Renames and state transitions update fields, not IDs.
5. Versions are metadata. A revision can point to its predecessor using a
   typed reference such as `supersedes_ref`.
6. The LLM may refer to known IDs but runtime code allocates and validates all
   resource references.
7. Temporary prompt context and other derived views do not automatically become
   durable resources.

## Reference representation

Introduce one value object, for example:

```python
@dataclass(frozen=True)
class ResourceRef:
    type: str
    id: str

    @property
    def canonical(self) -> str:
        return f"{self.type}:{self.id}"
```

`ResourceRef` construction validates the type against the registry and the ID
against the canonical grammar. It exposes parsing, canonical serialization,
and expected-type validation as one API; callers must not split or concatenate
resource IDs themselves. `type` should be represented internally by a
registered type value rather than an unconstrained string where practical.

Persisted fields should use `*_ref` and `*_refs` names and serialize to the
canonical string:

```json
{
  "execution_ref": "powdrr/execution/run:01JQ8T2K7M4F",
  "source_action_ref": "powdrr/execution/action:01JQ8T2N91AB",
  "evidence_refs": [
    "powdrr/execution/evidence:01JQ8T7S2W2B"
  ]
}
```

Parsing should validate both syntax and the expected type. An action reference
must not silently be accepted where an execution reference is required.

The registry should also record whether a type is durable, immutable, runtime-
or user-created, allowed parent types, allowed reference targets, and whether
it may appear in prompts or contains sensitive data.

Each registry entry must additionally define its uniqueness scope, generator
(`uuidv7`, `ulid`, or `human`), migration policy, and whether the resource has
an external identity. Resource references are validated at deserialization and
at persistence boundaries, including parent/reference-target constraints.

## Migration plan

### 1. Establish the contract

Add the `ResourceRef` value object, type registry, generator, parser, schema,
and round-trip tests. Reject malformed and unregistered types.

### 2. Migrate durable execution

Convert execution runs, events, actions, attempts, obligations, evidence,
findings, checkpoints, and capability exceptions. Update the kernel, state
store, runtime, relationships, replay, and checkpoint paths together.

### 3. Migrate intent, specifications, and workflows

Convert intent sources and clauses, invariants and procedures, specification
entities and relationships, plans, units, skills, and tasks. Replace string
parsing of task IDs with explicit workflow references.

### 4. Migrate boundaries

Update CLI, MCP, logs, prompts, and artifact metadata. Keep external system IDs
inside explicit adapters and `external_ref` fields.

There should be one deliberate data migration at the persistence boundary,
not two competing ID semantics at runtime. Ambiguous existing values should
fail with an actionable repair error rather than being guessed.

The migration must provide a durable old-to-new mapping for every migrated
resource, support recovery after an interrupted migration, and preserve lookup
of old filenames and event streams until the mapping is committed. Legacy
composite strings must never be parsed heuristically as new typed references.

## Acceptance criteria

- Every durable resource has one canonical typed ID.
- Every persisted reference includes its resource type.
- Parent-child relationships are explicit rather than encoded in strings.
- Action retries preserve action identity and create distinct attempts.
- Original intent remains addressable across sessions and execution restarts.
- Replay and checkpoint restore preserve exact references.
- CLI, MCP, logs, and prompts use the same canonical representation.
- Runtime code no longer creates IDs through ad hoc string concatenation.
- Derived prompt context does not receive unnecessary durable IDs.
- Tests cover parsing, type compatibility, serialization, uniqueness, migration,
  replay, and checkpoint restore.
- The registry contains an entry for every durable resource identity and every
  persisted resource reference has a registered expected type.
- Human-authored workflow IDs use a configured person-derived prefix of at most
  five characters, with workflow scope or key handling collisions explicitly.

The governing principle is: **the type tells us what a resource is; the ID tells
us which resource it is; relationships, versions, and state belong in
structured data.**
