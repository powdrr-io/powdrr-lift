# Structrr Context Language Specification

## Status and scope

This document specifies the target Structrr language needed for durable product
memory, decision-time context, semantic rebasing, source alignment, and
cross-cutting assurance. It is a design specification; the current repository
implements only portions of it.

The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.
Structrr is declarative. It MUST NOT become an execution language or duplicate
commands and policy configuration owned by Make, CI, scanners, or other tools.

## Language responsibilities

Structrr MUST represent:

- stable product entities and typed relationships;
- requirements, decisions, assumptions, invariants, and guidance;
- immutable design revisions and materialized current state;
- future proposal overlays and semantic patches;
- declared and observed source bindings;
- context lenses, requests, packets, and coverage;
- applicable validation, security, compliance, and governance capabilities;
- semantic impact, rebase reports, review findings, and merge certificates; and
- provenance and lifecycle for every authoritative statement.

Structrr MUST NOT represent:

- arbitrary process control flow;
- provider prompts or model retry policy;
- shell command implementations;
- CI job internals;
- scanner-specific configuration already owned by the scanner; or
- mutable execution state owned by Workrr.

## Common value types

### Resource identifiers

Every addressable object MUST have a stable typed identifier. A canonical URI
form is recommended:

```text
structrr://product/<product-id>/entity/<entity-id>
structrr://product/<product-id>/decision/<decision-id>
structrr://product/<product-id>/relationship/<relationship-id>
structrr://product/<product-id>/proposal/<proposal-id>@<revision>
```

Human-readable local IDs MAY be accepted in source documents, but compilation
MUST resolve them to canonical identifiers or report ambiguity. Renaming a
display label MUST NOT change identity.

### Revision references

Authoritative aggregates MUST be immutable and content-addressed:

```yaml
revision:
  schema: structrr/revision-v1
  product: powdrr-lift
  parent: sha256:...
  digest: sha256:...
  created_from: change:PR-712
```

Canonical serialization MUST produce stable fingerprints. A reference to
`current` MAY be accepted at a user interface, but persisted decisions MUST
record the resolved revision.

### Lifecycle

Versioned semantic objects MUST use an explicit lifecycle state:

```text
proposed, accepted, active, deprecated, superseded, retired, rejected
```

Objects SHOULD record `effective_from`, `effective_until`, `supersedes`, and
`reconsider_when` where relevant. Retrieval MUST exclude inactive meaning by
default while preserving it for history and explanation.

### Provenance

Every authoritative statement MUST identify its source, author or authority,
creation revision, and content fingerprint. Exact human wording SHOULD be
stored once and referenced rather than copied into derived objects.

## Entity model

An entity is a stable unit of product meaning:

```yaml
entity:
  id: query-api
  type: public-api
  lifecycle: active
  title: Query API
  summary: Stable application interface for product search.
  version: 4
  supersedes: legacy-query-api
  labels: [public, query]
```

Entity IDs and types MUST be non-empty. Entity types MUST resolve through a
versioned taxonomy. Type definitions MAY declare required fields, source
binding roles, context lenses, and relationship cardinality.

Not every entity needs source code. Objectives, user outcomes, regulations,
and market concepts MAY remain conceptual.

## Relationship model

Relationships are stable, typed, directional edges:

```yaml
relationship:
  id: cli-consumes-query-api
  source: entity:cli
  type: consumes
  target: entity:query-api
  lifecycle: active
  rationale: The CLI uses the public query contract.
```

Relationship types MUST define relevant semantics:

```yaml
relationship_type:
  id: consumes
  source_types: [component, application]
  target_types: [public-api]
  directional: true
  transitive: false
  context_propagation:
    public-api-change: reverse
  cardinality: many-to-many
```

Compilation MUST validate endpoints against the complete product graph, not
only entities declared in the same change document. Unknown relationship
types, invalid endpoint types, illegal cardinality, and unresolved references
MUST be diagnostics.

Core relationship vocabulary SHOULD include `implements`, `depends_on`,
`consumes`, `exposes`, `constrains`, `owns`, `supersedes`, `conflicts_with`,
`derived_from`, `validated_by`, `governed_by`, and `generated_from`.

## Product statements

### Requirement

A requirement defines a needed outcome and SHOULD relate to affected entities,
acceptance criteria, and evidence expectations.

### Decision

A decision MUST capture the selected meaning and SHOULD capture alternatives:

```yaml
decision:
  id: stable-query-envelope
  lifecycle: active
  authority: product-architecture
  scope: [entity:query-api]
  selected: Preserve the existing response envelope.
  rejected:
    - Return persistence records directly.
  rationale: Keep public clients independent of storage representation.
  assumptions:
    - Cursor pagination remains sufficient.
  reconsider_when:
    - A versioned API is introduced.
```

### Invariant

An invariant states what MUST remain true. It SHOULD include a typed assertion
or evidence requirement when possible:

```yaml
invariant:
  id: query-api-backward-compatible
  lifecycle: active
  severity: blocking
  scope: [entity:query-api]
  assertion:
    kind: compatibility
    baseline: artifact:query-api-schema
```

### Guidance

Guidance influences judgment without independently blocking completion. It
MUST be distinguishable from decisions and invariants so prompt order cannot
accidentally change authority.

### Assumption

Material assumptions SHOULD be independently addressable. A proposal reading
an assumption becomes stale when the assumption changes or is invalidated.

## Source subjects and bindings

### Source subject

A source subject is a language-aware reference to an artifact or symbol:

```text
python://src/query/api.py#QueryClient.search
rust://src/query.rs#impl.QueryEngine.search
openapi://api/openapi.yaml#/paths/~1search/get
config://pyproject.toml#project.dependencies
file://docs/query-api.md
```

Path and line span MUST be treated as resolved locations, not stable identity.
A subject MAY carry a signature, structural fingerprint, language, resolver
version, and source revision.

### Source binding

```yaml
source_binding:
  id: query-api-implementation
  entity: entity:query-api
  subject: python://src/query/api.py#QueryClient.search
  role: implements
  authority: authoritative
  provenance:
    kind: declared
    change: change:PR-712
```

Binding roles SHOULD include `defines`, `implements`, `exposes`, `consumes`,
`configures`, `validates`, `documents`, `migrates`, `generates`, and
`generated_from`.

Declared bindings represent accepted product intent. Observed bindings are
derived evidence from source analysis. An observation MUST NOT silently create
or replace an authoritative declared binding.

Binding reconciliation MUST classify subjects as unchanged, moved, renamed,
split, merged, deleted, or ambiguous. Moves and unambiguous renames MAY be
mechanical. Splits, merges, and ambiguous matches require an explicit semantic
decision.

## Tool and assurance references

Structrr's existing tool catalog SHOULD be extended minimally to express
semantic capability, applicability, and an external execution reference:

```yaml
tool:
  id: python-validation
  provides:
    - capability:python-quality-validation
  when:
    changed_languages: [python]
  execution_ref: make:validate-python
  evidence_kind: successful-run
```

`execution_ref` is opaque to Structrr except for reference syntax and
resolution. The referenced Make target, CI job, package script, or scanner
profile owns command composition and configuration.

Cross-cutting concerns SHOULD be represented as applicable product constraints
that reference capabilities:

```yaml
assurance_requirement:
  id: dependency-security-validation
  concern: supply-chain-security
  when:
    any:
      - changed_paths: [pyproject.toml, uv.lock]
      - changed_entity_types: [external-dependency]
  requires_capabilities:
    - capability:dependency-security-validation
  severity: blocking
```

Structrr MUST NOT duplicate the scanner threshold or command when that policy
is already authoritatively configured by the referenced tool. It MAY record a
product-level outcome that is not represented elsewhere.

Exceptions MUST be explicit, scoped, authorized, expiring artifacts that
reference the exact requirement they weaken.

## Context lenses

A context lens declares required categories and graph traversal for a class of
decision:

```yaml
context_lens:
  id: public-api-change
  applies_when:
    decision_kind: edit-source
    affected_entity_types: [public-api]
  requires:
    - entity-definition
    - incoming-consumers
    - active-decisions
    - active-invariants
    - rejected-alternatives
    - concurrent-proposals
    - applicable-tools
  optional:
    - historical-rationale
  budget:
    max_items: 40
    max_tokens: 6000
```

Required categories MUST be deterministic. Relationship-type definitions
control traversal direction and depth. Semantic similarity MAY rank eligible
items but MUST NOT determine whether mandatory context applies.

## Context request

Workrr creates a request at a Procedrr decision boundary:

```yaml
context_request:
  schema: structrr/context-request-v1
  request_id: context-request-184
  lens: public-api-change
  decision_kind: edit-source
  phase: implementation
  work_item: proposal:PR-712@6
  subjects:
    entities: [entity:query-api]
    paths: [src/query/api.py]
    symbols: [python://src/query/api.py#QueryClient.search]
  proposed_operation:
    kind: edit
  revisions:
    design: sha256:...
    source: git:...
    policy: sha256:...
```

Requests MUST bind to exact revisions before resolution.

## Context packet and coverage

A packet is immutable, content-addressed, and explainable:

```yaml
context_packet:
  schema: structrr/context-packet-v1
  packet_id: sha256:...
  request: context-request-184
  objective: requirement:query-pagination
  affected_entities: [entity:query-api]
  decisions: [decision:stable-query-envelope]
  invariants: [invariant:query-api-backward-compatible]
  tools: [tool:python-validation]
  concurrent_proposals: [proposal:replace-query-serializer@2]
  reasons:
    decision:stable-query-envelope:
      graph_path: [entity:query-api, decision:stable-query-envelope]
      rule: active-decision-constrains-entity
```

Each item MUST include version, lifecycle, authority, provenance, and inclusion
reason in the resolved representation.

Coverage MUST be returned separately:

```yaml
context_coverage:
  lens: public-api-change
  resolved: [entity-definition, active-decisions, active-invariants]
  unresolved: [incoming-consumers]
  status: incomplete
```

Missing required categories MUST prevent a decision unless the lens explicitly
permits an unknown result. Optional context MAY be omitted under budget.

## Semantic patches

An LLM proposes operations; the Structrr compiler owns canonical documents:

```yaml
semantic_patch:
  schema: structrr/semantic-patch-v1
  patch_id: proposal:PR-712@6
  base_revision: sha256:...
  context_packet: sha256:...
  operations:
    - operation_id: revise-query-api
      revise_entity:
        id: entity:query-api
        expected_version: 4
        set:
          summary: Add bounded cursor pagination.
```

Supported operations SHOULD include create, revise, retire, restore, add or
retire relationship, supersede decision, declare invariant, migrate binding,
and amend proposal. Every operation MUST have deterministic preconditions and
effects. Unknown keys and stale expected versions MUST be rejected.

## Proposal dependency contract

A proposal MUST persist its compiler-derived semantic dependencies:

```yaml
dependencies:
  reads:
    - subject: decision:stable-query-envelope
      fields: [lifecycle, selected]
      fingerprint: sha256:...
  writes:
    - subject: entity:query-api
      fields: [summary]
  assumptions:
    - assumption:cursor-pagination-sufficient
  expected_impact:
    entities: [entity:query-api, entity:cli]
```

The read set MUST include context that materially informed the proposal. The
write set MUST be derived from patch operations rather than model prose.

## Semantic rebase

Rebase compares the proposal base, current design, and semantic patch. Results
MUST classify changes as:

- clean;
- mechanically rebased;
- context refresh required;
- targeted update required;
- conflicted; or
- invalidated.

Direct write/write conflicts, changes to read assumptions, newly applicable
blocking constraints, retired subjects, and changed dependent proposals MUST
be reported separately. A rebase MUST preserve unaffected operations and
produce targeted amendment requirements.

Field and relationship schemas SHOULD classify changes by semantic weight:
presentation, contextual, contract, applicability, and identity. Only schema
rules and validated equivalence functions may authorize a mechanical rebase.

## Source impact and alignment review

Source analysis produces an observed semantic impact against exact source and
binding revisions. Review compares:

```text
declared design delta
observed source delta
applicable current contract
```

Typed findings SHOULD include undeclared impact, unimplemented design change,
broken binding, changed consumer, violated invariant, reintroduced rejected
alternative, stale assumption, stale evidence, and concurrent proposal
conflict. Every finding MUST include provenance and the graph path supporting
it.

## Merge certificate

A merge certificate binds readiness to current revisions:

```yaml
merge_certificate:
  schema: structrr/merge-certificate-v1
  proposal: proposal:PR-712@7
  design_revision: sha256:...
  source_revision: git:...
  policy_revision: sha256:...
  design_rebase: clean
  source_rebase: clean
  semantic_correspondence: verified
  context_packets: [sha256:...]
  evidence: [evidence:python-validation/...]
  status: ready
```

Any change to the proposal branch, a dependency, an applicable constraint, a
source binding, tool configuration, or evidence input MUST invalidate the
affected portion of the certificate.

## Diagnostics

Diagnostics MUST be machine-readable and correction-oriented:

- stable error code;
- source object and field path;
- expected type or contract;
- actual value or revision;
- candidate identifiers for ambiguity;
- permitted semantic repairs; and
- whether the error blocks compilation, context coverage, or merge.

## Compatibility and extensibility

Schema versions MUST be explicit. Extensions MUST be namespaced. Unknown
normative fields MUST be rejected; unknown namespaced annotations MAY be
preserved without affecting semantics.

Evolution MUST use explicit migrations. A migration MUST preserve identifiers,
provenance, and semantic fingerprints or produce a report identifying changed
meaning.

## Required guarantees

A conforming implementation MUST guarantee that:

1. current meaning is reproducible from an exact revision;
2. all authoritative references resolve or fail compilation;
3. required context applicability is deterministic;
4. every context packet is revision-bound and explainable;
5. semantic patches cannot silently overwrite stale state;
6. substantive rebases require targeted reconsideration;
7. observed source facts cannot silently replace declared design;
8. tool execution remains delegated to its external authority; and
9. merge readiness is invalidated by relevant subsequent change.
