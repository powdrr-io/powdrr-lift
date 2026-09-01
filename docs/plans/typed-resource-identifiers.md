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

External identifiers, such as a GitHub pull request number, should remain an
explicit `external_ref` rather than being confused with a Powdrr resource ID.

## Identifier rules

1. Types are lowercase, stable, and slash-separated. New types are added to a
   registry rather than invented at call sites.
2. The ID portion is opaque, contains no colon, and does not encode parent IDs,
   status, version, or lifecycle state.
3. Runtime-created IDs should use UUIDv7 or ULID. Human-authored workflow IDs
   may remain readable, provided they use the same validation rules.
4. IDs are never reused. Renames and state transitions update fields, not IDs.
5. Versions are metadata. A revision can point to its predecessor using a
   typed reference such as `supersedes_ref`.
6. The LLM may refer to known IDs but runtime code allocates IDs and validates
   all references.
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

The governing principle is: **the type tells us what a resource is; the ID tells
us which resource it is; relationships, versions, and state belong in
structured data.**
