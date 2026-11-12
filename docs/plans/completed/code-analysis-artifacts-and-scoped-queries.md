# Planning Code-Symbol Evidence

Status: Complete for the defined scope. The revision-pinned Structrr symbol
artifact, BasedPyright producer, both planning callers, and end-to-end planning
scenarios are implemented and covered by merged PR #1367. Diagnostics,
post-implementation analysis, reviewer diffs, and non-Python providers remain
future work outside this plan's scope.
Date: 2026-11-04  
Owner: Powdrr-Lift planning

## 1. Objective

Help planning answer concrete repository questions with bounded, revision-pinned
evidence about Python symbols and their source locations. Examples include:

- Is the requested behavior already implemented, and where?
- Which functions, classes, or tests are plausible places to change?
- What callers or declarations are relevant to a proposed work unit?
- Which component and source root own the affected code?

Planning receives compact symbol and relationship results, each traceable to the
exact source revision and provider inputs used to produce it. The planner uses
that evidence to form or revise a plan; it still makes the semantic judgment
about requirements, implementation scope, and dependencies.

The initial implementation supports Python through the existing BasedPyright
symbol provider. It stores the symbol index as a derived YAML artifact referenced
by Structrr and exposes bounded Structrr queries to the existing planning
callers.

The content-addressed symbol artifact is a repository file under
`artifacts/structrr/code-symbols/` and is checked in alongside the source or
planning change that needs it. Its `source_revision` records the exact source
snapshot used to build it; its source-tree digest lets a later commit that only
adds the artifact reuse it without indexing again. The ignored
`.powdrr/cache/` directory stores only local lookup metadata; it is not the
artifact's canonical storage.

## 2. Scope

### Included

- Python symbol definitions, signatures, source ranges, and supported
  relationships needed by current planning questions.
- A deterministic, content-addressed YAML artifact tied to a Git revision,
  component, BasedPyright version, and configuration fingerprint.
- A compact artifact reference in the Structrr snapshot; the full index stays
  outside the bootstrap YAML and model prompt.
- Bounded symbol and relationship queries through one Structrr contract.
- Migration of `PlanningSession` and `github_issue_plan_generation.py` from
  ephemeral BasedPyright results to artifact-backed evidence.
- Journaling, replay/reuse, stale-evidence detection, and failure/coverage
  behavior for these planning queries.
- End-to-end planner verification that demonstrates better path selection and
  evidence-bound plans without requiring whole-repository reference discovery.

### Explicitly out of scope for this plan

- Lint, type-check, or other diagnostic findings; diagnostic baselines; and
  post-implementation analyzer runs.
- Reviewer symbol diffs or reviewer prompt/context changes.
- Go, TypeScript, or other language providers and a general multi-language
  analyzer framework.
- Replacing BasedPyright, building a hosted analysis service, uploading source,
  or maintaining a manually edited code model.
- Using an LLM to classify, filter, or repair the generated symbol index.

These are separate future projects. They are not prerequisites for completing
the planning slice described here.

## 3. Existing planning path and constraints

- `PlanningSession` already asks BasedPyright for symbols and cross-file
  references to ground proposed work-unit paths and dependencies.
- `github_issue_plan_generation.py` already asks bounded workspace-symbol
  questions to find declarations and excerpts.
- Both callers currently receive transient provider results. They do not share
  a durable, revision-keyed index or common query provenance.
- The current reference helper is designed for scoped lookups. A full scan of
  300 tracked Python files exceeded its 1,024-request safety cap after about
  46 seconds and did not produce a complete reference index. Planning must not
  depend on such a scan.
- Symbol indexing of those 300 files took about 1.18 seconds cold, produced
  35,762 symbols including nested declarations, and serialized to about
  9.67 MiB using the provider's raw records. This is a sizing signal, not a
  requirement to put the whole repository index into planner context.

The implementation must use the existing BasedPyright provider rather than
introducing a parallel Python parser or another symbol-generation path.

## 4. Planning contract and invariants

1. **Planning is the consumer.** This feature exists to improve planning
   questions, path candidates, and plan provenance. It does not create a general
   user-facing code-analysis product.
2. **Pinned inputs.** Every artifact and query identifies the exact source
   revision, component, provider version, and relevant configuration
   fingerprint.
3. **Bounded scope.** Queries are constrained by component, path, symbol name or
   ID, relationship kind, record count, and encoded response size. The planner
   never receives the whole index by default.
4. **Evidence is not intent.** A symbol match or reference edge is a candidate
   for the planner to consider, not proof that a requirement is implemented or
   that two work units depend on one another.
5. **Coverage is explicit.** Complete, partial, unavailable, and failed index
   states are distinguishable. Missing index data cannot be interpreted as an
   empty successful result.
6. **References are optional and scoped.** A missing, incomplete, or capped
   reference query does not invalidate valid symbol and location evidence.
   Whole-repository reference discovery is not part of this plan.
7. **Reproducible output.** YAML serialization, symbol identifiers, sorting,
   query filters, and result fingerprints are deterministic.
8. **Source remains authoritative.** The index is a rebuildable materialized
   view of the pinned source revision. It is never edited independently.
9. **No unrelated analyzer state.** Planning symbol coverage is recorded
   separately from diagnostics, which this work does not run or report.

## 5. Stored symbol artifact

### 5.1 Structrr reference

Structrr owns the symbol contract and query semantics. Its snapshot carries a
small reference to the full artifact, not the symbol inventory itself:

```yaml
structrr_schema: https://powdrr.io/schema/structrr-bootstrap-v2
code_symbol_artifact:
  schema: https://powdrr.io/schema/structrr-code-symbol-artifact-ref-v1
  artifact_id: structrr-code-symbols:sha256:<digest>
  content_ref: artifacts/structrr/code-symbols/sha256-<digest>.yaml
  media_type: application/vnd.powdrr.structrr-code-symbols+yaml
  content_digest: sha256:<digest>
  source_revision: <40-character-git-object-id>
  components:
    - component_id: component:python:attestation/verifier
      provider_id: basedpyright
      provider_version: 1.39.9
      configuration_fingerprint: sha256:...
      coverage: complete | partial | unavailable | failed
  symbol_count: 248
  relationship_count: 411
```

Structrr validates the schema, digest, revision, component identity, path
containment, unique IDs, and relationship endpoints when resolving the
artifact. Consumers pass a Structrr snapshot ID to query APIs; they do not pass
arbitrary artifact paths.

### 5.2 YAML index

The persisted format is versioned YAML, matching the Structrr snapshot format.
SCIP or another index may be considered later as a provider input, but it is
not the storage contract for this work. Emit UTF-8 with LF line endings,
documented mapping-key order, no aliases, and arrays sorted by stable ID. Hash
the exact serialized bytes and use the digest for the artifact ID and path.

```yaml
schema: https://powdrr.io/schema/structrr-code-symbols-v1
source_revision: <40-character-git-object-id>
components:
  - component_id: component:python:attestation/verifier
    language: python
    root: attestation/verifier
    source_roots: [attestation/verifier/src]
    provider_id: basedpyright
    provider_version: 1.39.9
    configuration_fingerprint: sha256:...
    coverage: complete
symbols:
  - symbol_id: symbol:python:attestation/verifier:powdrr_attestation.policy.parse_policy:function
    component_id: component:python:attestation/verifier
    language: python
    kind: function
    name: parse_policy
    qualified_name: powdrr_attestation.policy.parse_policy
    path: attestation/verifier/src/powdrr_attestation/policy.py
    span: {start_line: 92, start_column: 0, end_line: 130, end_column: 1}
    signature: 'parse_policy(data: Mapping[str, object]) -> Policy'
    resolution: semantic
    provider_id: basedpyright
    source_fingerprint: sha256:...
relationships:
  - relationship_id: relationship:sha256:...
    source_symbol_id: symbol:...
    target_symbol_id: symbol:...
    kind: references
    resolution: semantic
    provider_id: basedpyright
coverage:
  - component_id: component:python:attestation/verifier
    indexed_paths: 6
    excluded_paths: []
    unsupported_paths: []
    status: complete | partial | unavailable | failed
```

Symbol identity is qualified by language and component and is derived from
canonical qualified name plus kind, not line number. Relationships are stored
only when BasedPyright provides them within the bounded requested scope. Each
record includes provider provenance and repository-relative source ranges.

### 5.3 Structrr planning query

Use a single versioned query/response contract for planner symbol lookups:

```yaml
schema_version: structrr-code-query-v1
structrr_snapshot_id: structrr:sha256:...
query_kind: symbols | references
filters:
  component_ids: [component:python:attestation/verifier]
  paths: [attestation/verifier/src/powdrr_attestation/policy.py]
  symbol_ids: []
  symbol_names: [parse_policy]
  relationship_kinds: [references]
limit: 50
max_encoded_bytes: 32768
purpose: map requirement to existing code
```

The response contains the snapshot and resolved artifact IDs, normalized
filters, matching records, omitted count, truncation status, result
fingerprint, coverage status, and durable evidence references. Results are
deterministic and bounded by both record count and encoded bytes. Paths are
repository-relative and cannot escape the pinned worktree.

Supported query behavior in this slice:

- `symbols`: find declarations by component, path, name, ID, and kind; return
  signatures and exact source spans.
- `references`: find callers/references for requested symbol IDs, restricted
  to specified components or paths and a strict result cap. This can use the
  existing on-demand BasedPyright reference operation; it does not imply a
  complete reference graph.

Do not implement finding, diagnostic, baseline, reviewer-diff, or generic
project-analysis query kinds in this planning contract.

## 6. Planning integration

### 6.1 Runtime flow

1. **Pin the planning revision.** Associate the planning session with the exact
   Git commit and Structrr snapshot used for its current proposal.
2. **Choose component scope.** Derive candidate components from the requested
   work, Structrr entities, and existing proposal/work-unit paths. Index only
   supported Python components needed by planning; retain component coverage
   for every requested component.
3. **Load or build the index.** Reuse a matching artifact keyed by revision,
   component scope, BasedPyright version, and configuration fingerprint.
   Otherwise call the existing BasedPyright symbol provider and persist the
   deterministic YAML artifact. Build once per component revision and share it
   across questions in the session.
4. **Give a compact inventory.** Provide component IDs, roots, symbol counts,
   coverage, and artifact IDs. Do not include symbol arrays in the planning
   prompt.
5. **Answer scoped questions.** Query symbols for requirement terms, candidate
   files, and proposed write paths. Query references only for selected symbols
   where callers or dependency evidence may change the plan. Record the exact
   filters and query purpose.
6. **Use evidence in the plan.** The planner may cite symbols and references
   when proposing paths, identifying existing behavior, or explaining
   candidate dependencies. It must preserve uncertainty where evidence is
   missing or ambiguous.
7. **Journal and resume.** Store snapshot/artifact IDs, query filters, result
   fingerprints, and evidence references with the planning session. On resume,
   reuse evidence only if all pinned inputs still match; otherwise rebuild and
   re-query.

### 6.2 Planner evidence packet

```yaml
planning_code_context:
  snapshot_id: structrr:sha256:...
  source_revision: <git-object-id>
  symbol_artifact_id: structrr-code-symbols:sha256:<digest>
  component_coverage:
    - component_id: component:python:attestation/verifier
      status: complete | partial | unavailable | failed
      symbol_count: 248
  queries:
    - query_id: query:sha256:...
      purpose: locate existing policy parser behavior
      result_fingerprint: sha256:...
      evidence_refs: [symbol:..., relationship:...]
      omitted_count: 0
```

The planner context contains only relevant result records or references within
the configured prompt budget. Each presented symbol retains its provider,
component, revision, path, range, and artifact reference. The artifact and query
IDs allow replay and inspection without rerunning the provider.

### 6.3 Existing callers

- **`PlanningSession`:** Replace transient symbol lookups with
  `structrr-code-query-v1` queries against the pinned snapshot. Preserve the
  existing planning question flow, but attach query provenance to the question
  and record relevant symbol references in the resulting plan/work units.
- **`github_issue_plan_generation.py`:** Route existing workspace-symbol
  questions through the same query API. Keep issue/task path context as filters
  and return bounded declarations with stable evidence references.
- **Shared reuse:** Both callers use the same component-revision artifact and
  Structrr query implementation. Do not add a second BasedPyright invocation
  path, cache format, or symbol schema for issue planning.

### 6.4 Failure and coverage behavior

- Complete symbol coverage plus unavailable references remains usable for
  symbol/location planning questions; reference-dependent conclusions are
  marked unavailable.
- Partial symbol coverage returns the available symbols with an explicit
  coverage state and omitted/unsupported paths.
- Missing provider, invalid configuration, timeout, malformed provider output,
  and artifact-integrity failure are distinct failures in planning evidence.
- An unavailable or failed index is not an empty index. Planning either uses
  the existing explicit degraded-evidence path or stops before making claims
  that require missing code structure.
- A stale revision, component fingerprint, provider version, or configuration
  invalidates reuse and triggers re-indexing.
- Query truncation and result caps are visible to the planner; it may narrow
  the query but cannot assume omitted matches do not exist.

## 7. Artifact lifecycle and resource bounds

- Store the YAML index as an immutable, content-addressed execution artifact
  with its schema version, owner, source revision, provider inputs, and digest.
- Store the compact artifact reference in Structrr. Never inline the complete
  index into bootstrap state, planning prompts, or ordinary run summaries.
- Reuse an artifact only when the indexed source-tree digest, component scope,
  provider version, and configuration fingerprint match exactly. Keep the
  artifact's original source revision as provenance and bind each query to its
  requested Structrr snapshot separately.
- Treat the source tree as authoritative. Missing or corrupt artifacts are
  rebuilt from source; a failed rebuild is explicit unavailable/failed
  coverage.
- Bound indexing by existing component and repository policies. Bound each
  query by paths/components, result count, and encoded bytes. Reference lookup
  remains on-demand and subject to the existing safety cap.
- Resolve all paths inside the selected worktree. Exclude generated,
  vendored, ignored, and environment directories according to existing
  component discovery rules.
- Keep analysis local under the current agent/service boundary. This work does
  not upload source or symbol artifacts to a hosted service.

## 8. Implementation sequence

Keep every implementation step focused on planning evidence. Combine adjacent
steps into a larger PR when the contracts and integration are easier to review
together; do not create separate PRs for infrastructure that planning does not
use.

### Step 1: Structrr symbol artifact and query contract

Define the versioned YAML records, deterministic serialization and digest,
Structrr snapshot reference, bounded `symbols`/`references` query/response, and
artifact validation/retrieval. Keep query kinds limited to what planning
callers use. Do not add diagnostic or reviewer-diff contracts.

**Acceptance:** round-trip serialization is stable; duplicate IDs, path
escapes, mismatched revisions, bad digests, and invalid relationship endpoints
are rejected; missing artifacts are reported unavailable rather than as a
successful empty index; result limits and response byte limits are enforced.

### Step 2: BasedPyright artifact producer and planner callers

Produce the Python index from the existing BasedPyright symbol provider. Add
revision/component/provider/configuration keyed reuse. Migrate both
`PlanningSession` and `github_issue_plan_generation.py` to scoped Structrr
queries, journaling artifact/query provenance with planning evidence. Keep
references bounded and optional.

**Acceptance:** both callers find a fixture function, signature, and source
range through the shared artifact; repeated questions reuse one index for an
unchanged snapshot; stale inputs rebuild it; a capped or failed reference
query does not discard valid symbol results; planner context contains only
scoped evidence and resolvable artifact references.

### Step 3: End-to-end planning proof

Add hermetic planner scenarios for an existing implementation, a requirement
that needs a new path, ambiguous symbol matches, incomplete component coverage,
unavailable references, resume, and stale snapshot/provider/configuration.
Verify both planning entry points and inspect the final plan evidence.

**Acceptance:** relevant existing code changes the planner's candidate paths
or prevents duplicate implementation when appropriate; each evidence-based
path claim links to the exact symbol artifact/query; unrelated symbols do not
enter the prompt; incomplete evidence remains visible; replay returns the same
results for identical inputs; stale evidence cannot be reused.

## 9. Verification and completion criteria

Use provider-output fixtures and hermetic end-to-end tests with a small Python
repository containing a `src` layout, cross-file callers, tests, nested
declarations, and an existing implementation relevant to a requirement. Test
the real planning paths rather than only the serializer or provider wrapper.

Planning support is complete when both current planning callers can use the
same revision-pinned symbol artifact and bounded Structrr query contract;
relevant symbols and optional references improve path/dependency reasoning;
the plan records inspectable evidence provenance; and resume, stale inputs,
partial coverage, and failures behave as specified. Diagnostics,
post-implementation analysis, reviewer diffs, and non-Python providers remain
separate future scope.
