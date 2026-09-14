# Dependable Context System Implementation Plan

## Objective

Implement the Structrr context language and integrate it with Procedrr and
Workrr so every consequential LLM decision receives complete, current,
explainable product context and every merged source change corresponds to an
accepted semantic product change.

This plan implements:

- [Dependable software development context](../design/dependable-development-context.md)
- [Structrr context language specification](../design/structrr-context-language-specification.md)
- [Structrr, Procedrr, and Workrr integration](../design/structrr-procedrr-workrr-integration.md)

## Scope

The plan includes product graph strengthening, source bindings, decision-time
context, semantic proposals and rebases, design/source alignment, external
tool applicability, and merge certification.

It does not duplicate Makefiles, CI workflows, package scripts, scanner
configuration, or arbitrary process control flow. It does not expand the
execution-safety work currently deferred in the agent-definition separation
plan except where a public integration contract is required.

## Current foundation

The repository already contains useful pieces:

- changelog-v2 entities, relationship changes, decisions, invariants,
  guidance, files, and related IDs;
- changelog indexing, entity graph construction, source provenance, and line
  attribution in `powdrr_lift.core.index`;
- current-state projection and entity-focused reference, relationship, and
  decision queries;
- canonical durable intent sources, clauses, selectors, versions, and
  precedence in `powdrr_lift.structrr.intent`;
- specification-v1 module and tool declarations, including `when_to_use`,
  `validation_action`, and `evidence` fields;
- Procedrr immutable control nodes, typed `DecisionContract`,
  `context_bindings`, resource limits, and compilation certificates; and
- Workrr provider protocols, proposal loop, action handling, runtime state,
  and operation adapters.

These pieces are not yet one context system. Product-language implementation
is still distributed through historical `core` modules; relationships and
selectors are weakly typed; context queries are caller-driven; and proposal,
source, and evidence revisions are not reconciled by one semantic transaction.

## Architectural target

The implementation should converge on:

```text
src/powdrr_lift/structrr/
  ids.py
  model.py
  taxonomy.py
  revisions.py
  graph.py
  source.py
  statements.py
  tools.py
  context.py
  patches.py
  rebase.py
  impact.py
  review.py
  certificates.py
  diagnostics.py
  store.py

src/procedrr/
  context.py
  model.py
  parser.py
  compiler.py

src/powdrr_lift/workrr/
  context.py
  composition.py
  tool_resolution.py
  source_observation.py
  reconciliation.py
```

Exact filenames may change, but ownership and dependency direction may not.

## Phase 0: Contract baseline and characterization

### Deliverables

- Add shared provider-neutral contracts for typed resource and revision
  references.
- Record golden examples for current changelog, specification, proposal,
  source-provenance, and intent behavior.
- Add package-boundary tests prohibiting Structrr imports of Workrr or
  Procedrr runtime code.
- Define canonical serialization and fingerprint rules.
- Inventory every existing entity, relationship, tool, intent, and proposal
  field and publish an explicit migration mapping.

### Exit criteria

- Existing behavior is characterized.
- Contract serialization is deterministic.
- New implementation cannot create a circular package dependency.

## Phase 1: Canonical product graph and revisions

### Deliverables

- Add required typed entity, relationship, lifecycle, provenance, and revision
  models under `structrr`.
- Define a versioned relationship ontology with endpoint, direction,
  cardinality, and context-propagation rules.
- Build relationships against the complete repository graph rather than
  requiring endpoints in one changelog.
- Materialize immutable design snapshots from accepted changes.
- Add aliases, rename, supersession, retirement, and effective-time handling.
- Move product semantics out of historical `core` implementation modules as
  each feature is cut over; do not leave compatibility implementations.

### Tests

- Referential integrity across documents and revisions.
- Relationship endpoint and cardinality validation.
- Deterministic snapshot reconstruction.
- Rename and supersession history.
- Invalid lifecycle transition rejection.

### Exit criteria

- Any historical or current design revision can be reproduced by digest.
- Every active graph reference resolves.
- Entity identity survives presentation changes and source movement.

## Phase 2: Source subjects, bindings, and reconciliation

### Deliverables

- Define `SourceSubject`, `SourceBinding`, `SourceObservation`, and binding
  reconciliation contracts.
- Add language-aware source resolvers beginning with Python and generic file,
  configuration, and schema subjects.
- Store declared bindings separately from observed bindings.
- Extend the source index to map paths and changed lines to enclosing symbols,
  bindings, entities, and relationships.
- Classify binding evolution as unchanged, moved, renamed, split, merged,
  deleted, or ambiguous.
- Add semantic operations for accepting binding migrations.

### Tests

- Symbol extraction and canonical subject resolution.
- Bidirectional entity-to-source and source-to-entity lookup.
- Move/rename preservation across Git revisions.
- Split/merge ambiguity requiring an explicit decision.
- Deleted and dangling binding diagnostics.

### Exit criteria

- A source diff can identify directly affected Structrr entities.
- Source movement does not destroy product identity.
- Observations cannot silently modify declared design.

## Phase 3: Context language and resolver

### Deliverables

- Define context lens, request, packet, coverage, inclusion reason, and budget
  models.
- Add decision-kind and entity-type lens selection.
- Add deterministic relationship traversal rules per lens.
- Resolve active decisions, invariants, assumptions, guidance, concurrent
  proposals, source bindings, and applicable tools.
- Implement authority, specificity, graph-distance, lifecycle, and budget
  ordering.
- Permit semantic ranking only after deterministic eligibility.
- Cache packets by complete request and revision tuple.

### Tests

- Required-context recall for representative decision lenses.
- Exact graph paths and inclusion explanations.
- Incomplete coverage when required context cannot be resolved.
- Stable packet fingerprints.
- Budget behavior that never removes mandatory items.
- Packet invalidation after relevant and irrelevant changes.

### Exit criteria

- Structrr can compile a bounded and explainable packet for an exact decision
  and revision.
- Required context cannot be omitted by ranking or token pressure.

## Phase 4: Procedrr context declarations

### Deliverables

- Extend Procedrr decisions with a typed context declaration containing
  provider, lens, subject bindings, resolution timing, and coverage policy.
- Validate all referenced bindings statically.
- Include context request bounds in activation and workflow cost analysis.
- Define explicit nodes or hooks for post-operation context refresh where
  effects can change applicability.
- Include context guarantees in the Procedrr compilation certificate.

### Tests

- Missing and ill-typed bindings fail compilation.
- Context requirements cannot grant model transition authority.
- Worst-case context activations remain bounded.
- Nested procedures preserve typed references without copying packet content.

### Exit criteria

- A compiled procedure completely declares when product context is required.
- Procedrr remains executable against a fake context provider without importing
  Structrr internals.

## Phase 5: Workrr context composition

### Deliverables

- Add a context-provider protocol and Structrr adapter.
- Build `ContextRequest` values from Procedrr bindings, runtime state, and
  proposed or observed effects.
- Make required context resolution a mandatory pre-decision hook.
- Compose procedure, execution state, context packet, and latest observation
  into every model activation.
- Record `DecisionRecord` values with packet and proposal fingerprints.
- Re-resolve after actions that add paths, symbols, entities, dependencies, or
  assurance applicability.
- Preserve packet and resource IDs through compaction and nested execution.

### Tests

- Model activation is impossible when required coverage is incomplete.
- Fake providers receive the expected packet at the expected decision.
- Newly observed dependency and public-API changes refresh context.
- Optional retrieval cannot remove mandatory context.
- Every accepted proposal links to the packet that informed it.

### Exit criteria

- Context delivery no longer depends on an LLM calling an inspection tool.
- Every consequential decision has an auditable context record.

## Phase 6: Semantic patches and proposal dependencies

### Deliverables

- Define exact-key semantic create, revise, retire, restore, relationship,
  decision, invariant, proposal-amendment, and binding-migration operations.
- Add optimistic expected versions and deterministic preconditions.
- Compile LLM semantic outputs into canonical Structrr documents.
- Derive proposal read sets from context packets and write sets from patch
  operations.
- Store assumptions, expected impact, dependency proposals, and base design
  revision.
- Add proposal lifecycle and immutable amendment lineage.

### Tests

- Stale writes and unknown keys are rejected.
- Canonical materialization is deterministic.
- Unaffected operations remain byte-for-byte stable under targeted amendment.
- Proposal dependencies form an acyclic validated graph.

### Exit criteria

- LLMs no longer author authoritative Structrr document structure directly.
- Every proposal identifies what meaning it read and intends to change.

## Phase 7: Semantic rebase

### Deliverables

- Implement three-way design diff and patch replay.
- Add field-level semantic weights and validated equivalence functions.
- Classify clean, mechanical, context-refresh, targeted-update, conflict, and
  invalidation outcomes.
- Produce targeted amendment items with changed dependencies, affected
  operations, graph paths, and permitted repairs.
- Have Workrr materialize amendment items as a sealed Procedrr worklist.

### Tests

- Unrelated upstream changes rebase automatically.
- Identity-preserving renames remap mechanically.
- Read/write and write/write conflicts are distinguished.
- Newly applicable invariants trigger targeted review.
- Retired entities invalidate dependent proposals.
- Concurrent proposal revisions propagate staleness only to dependents.

### Exit criteria

- No substantive design change is classified as mechanical.
- Rebase reopens only affected decisions.

## Phase 8: Source impact and alignment review

### Deliverables

- Derive observed semantic impact from branch source changes and source
  bindings.
- Compare declared impact, observed impact, and current applicable contracts.
- Add typed findings for undeclared impact, missing implementation, broken
  bindings, consumer changes, invariant violations, stale assumptions, and
  reintroduced rejected alternatives.
- Attach exact graph paths and provenance to every finding.
- Reconcile accepted source-binding changes through semantic patches.

### Tests

- Declared and observed impact agreement.
- Textually clean but semantically conflicting source rebases.
- Source-only and design-only drift detection.
- Targeted review context for each finding class.

### Exit criteria

- Review can explain whether code implements the rebased proposal and where it
  deviates from product direction.

## Phase 9: Tool applicability and assurance evidence

### Deliverables

- Extend Structrr tools with semantic capabilities, typed applicability, opaque
  external execution references, and evidence kinds.
- Represent cross-cutting assurance requirements and bounded exceptions.
- Add Workrr resolvers for existing Make, CI, and package-script references.
- Capture evidence with source, tool, and configuration fingerprints.
- Invalidate evidence only when declared relevant inputs change.

### Tests

- Python changes select the repository's Python validation capability.
- Dependency changes select security and license capabilities when declared.
- Make and CI remain the source of executable command composition.
- Stale evidence cannot satisfy readiness.
- Scoped, authorized, expiring exceptions are visible and deterministic.

### Exit criteria

- The system answers what validation applies and whether it is satisfied
  without duplicating external tool definitions.

## Phase 10: Merge reconciliation and certification

### Deliverables

- Rebase intended design onto current design at merge time.
- Reconcile branch source against current source and bindings.
- Recompute context, observed impact, applicable tools, and evidence freshness.
- Route substantive findings through targeted Procedrr decisions.
- Issue a content-addressed `MergeCertificate` only when design rebase, source
  rebase, alignment, coverage, and evidence are current.
- Invalidate certificates after any relevant branch, design, proposal, policy,
  binding, or evidence-input change.

### Tests

- Certificate issuance and precise invalidation.
- Concurrent mainline design and source changes.
- Merge blocked by incomplete context or stale evidence.
- Successful targeted amendment and recertification.

### Exit criteria

- A merge-ready PR has a reproducible proof that current code corresponds to a
  current accepted design delta under current applicable requirements.

## Migration strategy

### Add before replacing

Introduce canonical models and adapters behind existing read paths. Build the
new graph and context packets in shadow mode and compare them with current
entity queries before making them authoritative.

### No duplicate public semantics

Once a feature cuts over, move ownership to `structrr` and remove the old
implementation. Temporary adapters may translate serialized legacy documents,
but compatibility modules must not become a second source of truth.

### Document migration

Provide deterministic migration commands for existing changelogs and
specifications. Preserve original files and provenance until the migrated
revision validates. Never ask an LLM to perform bulk schema migration through
free-form edits.

### Enforcement progression

Use three explicit modes:

1. `observe`: compute packets, impacts, and findings without changing flow;
2. `warn`: surface missing coverage, drift, and stale proposals; and
3. `enforce`: block decisions or merge on normative failures.

Mode is deployment policy, not a weakening of artifact semantics.

## Verification strategy

### Unit and property tests

- Serialization and fingerprint stability.
- Graph referential integrity and lifecycle transitions.
- Patch preconditions and rebase classifications.
- Context eligibility, ranking, budgeting, and invalidation.
- Source binding reconciliation.

### Golden compiler tests

Keep source documents, canonical compiled artifacts, packets, rebase reports,
alignment findings, and diagnostics together. Any semantic output change must
be reviewed explicitly.

### Historical replay

Reconstruct prior PR decision points and define the context that should have
been supplied. Measure required-context recall, irrelevant-context rate,
staleness detection, and finding accuracy.

### Adversarial tests

- Omit a relevant entity from a proposal.
- Rename or split an implementation symbol.
- Change an invariant after proposal authoring.
- Add a dependency during an apparently unrelated edit.
- Supply stale tool evidence.
- Reintroduce a rejected alternative under a new name.
- Create conflicting concurrent proposal overlays.

### End-to-end acceptance scenarios

At minimum, demonstrate:

1. proposal authoring against a known design revision;
2. automatic rebase after an unrelated design change;
3. targeted amendment after a substantive decision change;
4. implementation with automatic source-to-entity context;
5. dynamic discovery of a new validation requirement;
6. merge-time source and design reconciliation; and
7. certificate invalidation after a relevant mainline change.

## Metrics

Track:

- mandatory-context recall;
- irrelevant context tokens per activation;
- incomplete coverage frequency;
- packet cache hit and invalidation accuracy;
- automatic versus targeted versus conflicting rebases;
- undeclared source impact findings;
- stale evidence and certificate detections;
- LLM repair rate for Structrr semantic operations; and
- decisions later invalidated because required context was absent.

## Completion criteria

The complete system is delivered when:

- Structrr can reproduce historical, current, and proposed product states;
- source changes resolve to stable product entities and relationship impact;
- Procedrr declares all required decision-time context statically;
- Workrr automatically supplies complete current packets at those boundaries;
- proposals and completed PRs undergo semantic three-way rebase;
- targeted amendments preserve unaffected decisions;
- applicable external validation is selected without command duplication;
- review compares declared design, observed source, and current direction;
- merge certificates are reproducible and precisely invalidated; and
- end-to-end tests prove correctness does not depend on transcript recall or an
  optional LLM retrieval action.
