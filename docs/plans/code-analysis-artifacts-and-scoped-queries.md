# Code Analysis Artifacts and Scoped Project Queries

Status: proposed implementation plan  
Date: 2026-11-04  
Owner: Powdrr-Lift planning and execution

## 1. Outcome

Give Powdrr a durable, useful view of code health and project structure across languages. Analysis must help the planner understand the repository and help the implementation agent inspect only code relevant to a task. It must not flood either agent or user with unfiltered checker output.

The system will:

1. Discover repository components, languages, source roots, configuration, and declared analysis commands.
2. Run language-specific analyzers in the correct component and environment.
3. Preserve complete structured findings and bounded raw tool output as durable artifacts tied to exact repository revisions.
4. Compare candidate findings with a base-revision report and label findings as new, unchanged, resolved, or uncertain.
5. Answer bounded questions about findings and project structure using explicit filters and evidence references.
6. Feed the planner concise, task-relevant evidence and record which reports and queries informed the plan.
7. Keep analyzer coverage and failures visible. Missing tools, invalid project configuration, and import-resolution failures must not be presented as source defects or as successful analysis.

The primary design decision is to keep language-specific analyzers behind shared contracts. BasedPyright remains the Python type-analysis and semantic-navigation provider; it is not the universal analysis engine. Powdrr uses the repository's applicable analyzer for each language and normalizes results for storage, filtering, comparison, and presentation.

## 2. Product behavior

### 2.1 Default report

The default response to an analysis run is a short summary, not a diagnostic stream:

```text
Analysis: partial (main Python component analyzed; tests excluded by project config)
Revision: 8f71a20
Components: 2 analyzed, 1 unavailable
Findings: 4 new errors, 12 unchanged, 3 resolved
Setup issues: 1 unresolved import root; see analysis artifact
Top new findings:
  parsers.py:55 missing type arguments for list
  parsers.py:59 missing type arguments for dict
Artifact: code-analysis/sha256-...
```

Statuses and counts come from normalized analyzer output and explicit coverage records. The summary does not turn a failed or partial run into a pass. Users and agents can query the artifact for details.

### 2.2 Scoped questions

The agent can ask deterministic, bounded questions such as:

- Show new errors under `attestation/verifier`.
- Show unresolved import diagnostics for the `attestation` component.
- List functions and classes in `src/powdrr_lift/execution/runtime.py`.
- Find references to `parse_policy` inside the verifier component.
- Show direct package dependencies of files in this work unit.
- Show analysis coverage for changed Python and TypeScript files.

Every response carries the report/index ID, revision, query filters, result count, truncation status, relative paths, source ranges where available, and stable finding or symbol IDs. Responses are capped by result count and serialized size. A query never returns the full report by default.

Natural-language questions may use a model to select a bounded query and explain results. Query execution and filters remain deterministic. A model cannot remove findings, mark analysis successful, or invent a relationship absent from indexed evidence.

## 3. Current implementation and gaps

The repository already has useful pieces that this work must extend rather than duplicate:

- `basedpyright_tools.py` provides Python symbol search, file structure, and cross-file reference lookup through BasedPyright's language server.
- Planning uses those Python symbols and references to ground Structrr candidate selection and work-unit dependency evidence.
- GitHub issue plan generation asks bounded symbol questions and retrieves matching declarations and excerpts.
- `ExecutionArtifact` records an artifact ID, type, schema version, owner, and content reference.
- `execution.checkpoints.run_diagnostics` bounds captured text, while `runtime.diagnose` records whether each hook succeeded.
- Structrr bootstrap currently serializes Python source subjects and bindings inline in its YAML document; the new schema moves the exhaustive symbol inventory to a separate referenced artifact.

The gaps are:

- Diagnostic output is an opaque string. Findings lack stable IDs, severity, rule, source range, component, baseline status, and coverage context.
- A successful process invocation does not prove the correct files and project environment were analyzed.
- A checker run at the wrong repository root can emit import errors that obscure actionable findings. In the gaterr check, the repository-root invocation showed 31 errors, including 14 unresolved package imports; a focused verifier-source invocation showed 10 missing generic type arguments. The import failures appear to be project-root or `src`-layout configuration noise, an inference that should be confirmed from project metadata rather than silently assumed.
- Planning's semantic structure query is Python-specific. Other languages have no common project-structure query contract.
- Planner prompts can receive raw findings or search output without a standard scope, size limit, or durable query provenance.

Do not add a second independent Python symbol/reference pipeline. Route the new query contract to the existing BasedPyright provider, then migrate current callers to that contract.

## 4. Design decisions and invariants

1. **Native analysis, shared results.** Use repository-declared checkers first. Use a supported default only when repository metadata is sufficient to construct the correct command and environment. Do not run every checker at the repository root by default.
2. **No universal checker.** BasedPyright is the Python provider. Other languages use repository-configured checkers. Missing providers are reported as unavailable coverage.
3. **Separate diagnostics from structure indexing.** Type checkers and linters produce findings. Semantic indexes answer definitions and references. Syntax parsers provide syntax outlines only; they do not claim type resolution or exact cross-file references.
4. **Preserve source output.** Normalization may add classifications and summaries but must not delete raw findings. Full structured output is retained within byte limits; truncation is explicit.
5. **Compare revisions, not counts.** Base and candidate reports bind to Git object IDs, analyzer/configuration fingerprints, and component scope.
6. **Make setup problems distinct.** Missing tool, invalid config, dependency resolution failure, import root unresolved, unsupported syntax, process failure, and complete analysis are separate result states.
7. **Never infer a pass from missing data.** An absent report, missing component, failed parser, timeout, or malformed structured result is not successful analysis.
8. **Do not use an LLM as the filter of record.** Stable fingerprints, project scope, finding disposition, and query filters are deterministic. Model-written explanations are advisory and cite evidence.
9. **Prioritize change relevance.** Interactive summaries show new findings in affected components and changed paths first. Pre-existing findings remain queryable and visible in aggregate counts.
10. **Reuse component discovery.** Depend on the component and capability model in `polyglot-repository-and-go-support.md`; do not create a competing monorepo detector.
11. **Keep source local.** Analysis and indexes are produced and queried in the local worktree for this plan. Do not upload full source or raw output to a hosted service.
12. **Structrr owns the symbol contract, not the inventory payload.** The Structrr snapshot declares the code-symbol schema, artifact identity, revision, digest, and coverage. The potentially large symbol and relationship arrays live in a separate artifact and are loaded through Structrr's provider.

## 5. Canonical records

### 5.1 Analysis run

```yaml
schema_version: code-analysis-run-v1
analysis_id: analysis:sha256:...
revision: 40-character-git-object-id
comparison_revision: 40-character-git-object-id-or-null
scope:
  component_ids: [component:python:attestation/verifier]
  paths: [attestation/verifier/src/**]
  requested_by: planner | user | validation
status: complete | partial | unavailable | failed
providers:
  - provider_id: basedpyright
    version: 1.39.9
    configuration_digest: sha256:...
    environment_digest: sha256:...
    status: complete
coverage:
  analyzed_paths: 6
  excluded_paths: 0
  unsupported_paths: []
  failed_components: []
summary:
  error_count: 10
  warning_count: 235
  setup_issue_count: 0
finding_artifact_ref: artifact://...
raw_output_artifact_refs: [artifact://...]
code_symbol_artifact_ref: artifact://...
fingerprint: sha256:...
```

Counts are illustrative. Artifacts store all results; summary records are small indexes into them. Reports are immutable. A changed revision, tool version, configuration, environment, provider, or scope creates a new analysis identity.

### 5.2 Finding

```yaml
schema_version: code-finding-v1
finding_id: finding:sha256:...
provider_id: basedpyright
provider_rule_id: reportMissingTypeArgument
category: source | configuration | environment | analyzer
severity: error | warning | note
message: Expected type arguments for generic class "list"
location:
  path: attestation/verifier/src/powdrr_attestation/parsers.py
  start_line: 55
  start_column: 11
  end_line: 55
  end_column: 15
component_id: component:python:attestation/verifier
baseline_status: new | unchanged | resolved | uncertain | not_compared
fingerprint: sha256:...
evidence_refs: [artifact://...]
```

Severity remains the provider's severity. Powdrr may add policy priority as a separate field; it must not silently rewrite warnings as errors or vice versa. Import-resolution findings may be categorized as configuration/environment only when component and environment evidence supports that classification. Otherwise retain the original provider rule and use `uncertain`.

### 5.3 Structrr code-symbol format and artifact boundary

The canonical persisted format is **versioned JSON**, with media type
`application/vnd.powdrr.structrr-code-symbols+json` and schema URI
`https://powdrr.io/schema/structrr-code-symbols-v1`. JSON is the interchange
format owned by Structrr; it is deterministic, inspectable, streamable, and
compatible with the existing JSON artifact and snapshot tooling. SCIP indexes
may be an input to a provider, but they are not the Structrr storage contract.
Tree-sitter or another syntax parser may also feed this format, with every
record labeled `syntactic` rather than `semantic`.

Store the symbol inventory as a separate immutable content-addressed artifact,
for example `artifacts/structrr/code-symbols/sha256-<digest>.json`. Do not copy
the symbol or relationship arrays into the Structrr bootstrap YAML or ordinary
planner prompts. The bootstrap contains only a compact reference:

```yaml
structrr_schema: https://powdrr.io/schema/structrr-bootstrap-v2
code_symbol_artifact:
  schema: https://powdrr.io/schema/structrr-code-symbol-artifact-ref-v1
  artifact_id: structrr-code-symbols:sha256:<digest>
  content_ref: artifacts/structrr/code-symbols/sha256-<digest>.json
  media_type: application/vnd.powdrr.structrr-code-symbols+json
  content_digest: sha256:<digest>
  source_revision: <40-character-git-object-id>
  component_fingerprints:
    - component_id: component:python:attestation/verifier
      fingerprint: sha256:...
      coverage: complete | partial | unavailable | failed
  symbol_count: 248
  relationship_count: 411
```

The JSON artifact has this top-level shape. Object keys are canonicalized for
fingerprinting; record arrays are sorted by stable ID before serialization.
The content digest is SHA-256 over canonical UTF-8 JSON with sorted object keys
and no insignificant whitespace; the Structrr reference carries that digest
and uses it in the content-addressed artifact path and artifact ID.

```json
{
  "schema": "https://powdrr.io/schema/structrr-code-symbols-v1",
  "source_revision": "<40-character-git-object-id>",
  "components": [
    {
      "component_id": "component:python:attestation/verifier",
      "language": "python",
      "root": "attestation/verifier",
      "source_roots": ["attestation/verifier/src"],
      "provider_id": "basedpyright",
      "provider_version": "1.39.9",
      "provider_fingerprint": "sha256:...",
      "coverage": "complete"
    }
  ],
  "symbols": [
    {
      "symbol_id": "symbol:python:attestation/verifier:powdrr_attestation.policy.parse_policy:function",
      "component_id": "component:python:attestation/verifier",
      "language": "python",
      "kind": "function",
      "name": "parse_policy",
      "qualified_name": "powdrr_attestation.policy.parse_policy",
      "path": "attestation/verifier/src/powdrr_attestation/policy.py",
      "span": {"start_line": 92, "start_column": 0, "end_line": 130, "end_column": 1},
      "signature": "parse_policy(data: Mapping[str, object]) -> Policy",
      "resolution": "semantic",
      "provider_id": "basedpyright",
      "source_fingerprint": "sha256:..."
    }
  ],
  "relationships": [
    {
      "relationship_id": "relationship:sha256:...",
      "source_symbol_id": "symbol:...",
      "target_symbol_id": "symbol:...",
      "kind": "references",
      "resolution": "semantic",
      "provider_id": "basedpyright"
    }
  ],
  "coverage": [
    {
      "component_id": "component:python:attestation/verifier",
      "indexed_paths": 6,
      "excluded_paths": [],
      "unsupported_paths": [],
      "diagnostics": []
    }
  ]
}
```

Symbol IDs are language- and component-qualified and derive from canonical
qualified name plus kind, not line number. A rename or kind change creates a
new ID and is reconciled by Structrr's existing diff/rebase review rather than
silently reusing an unrelated identity. Every relationship endpoint must
resolve to a symbol in the same artifact or to an explicitly declared external
symbol identity. Relationships distinguish `contains`, `imports`, `calls`,
`references`, and `implements`; providers emit only kinds they can support.

### 5.4 How the artifact extends Structrr

Structrr remains the owner of code-symbol meaning and use:

- Add a versioned `code_symbol_artifact` reference to the Structrr snapshot and
  bootstrap schema. Validate schema URI, content digest, source revision,
  component fingerprints, counts, unique IDs, path containment, and referenced
  relationship endpoints when loading the artifact.
- Make code symbols and relationships addressable Structrr source subjects.
  `source_bindings` and proposals can refer to `symbol_id` plus artifact ID;
  they do not inline symbol rows. Semantic entities remain in the normal
  Structrr entity collection, with bindings linking those entities to symbols.
- Add Structrr context lenses for component inventory, symbols by path/name,
  references/callers, dependency neighborhoods, and relevant findings. Lenses
  return bounded projections with artifact IDs and provider/coverage
  provenance; they do not expose the entire JSON artifact by default.
- Extend Structrr source comparison and rebase to compare symbol IDs,
  signatures, source fingerprints, and relationships from the referenced
  artifacts. Missing/stale artifacts or partial provider coverage yield an
  explicit incomplete comparison, never an empty-symbol success.
- Keep analyzer findings in the separate code-analysis report artifact. The
  Structrr symbol artifact may reference related finding IDs, but it does not
  duplicate diagnostic messages or own their severity/baseline lifecycle.
- Bump the Structrr bootstrap schema/section version for the reference and
  binding semantics. Do not preserve a second full inline symbol inventory as
  a compatibility representation; migrate consumers to the artifact-backed
  Structrr query contract in the same staged rollout.

For example, a compact snapshot binding can identify the semantic entity and
external source symbol without copying the symbol record:

```yaml
source_bindings:
  - id: binding:feature:policy-validation:parse-policy
    entity_id: feature:policy-validation
    symbol_artifact_id: structrr-code-symbols:sha256:<digest>
    symbol_id: symbol:python:attestation/verifier:powdrr_attestation.policy.parse_policy:function
    relationship: implemented_by
```

The v1 Structrr query contract can read the artifact directly without a graph
database. Storage, digest verification, schema validation, and query behavior
remain behind Structrr APIs, so future implementations can change the physical
artifact layout without changing planner contracts.

### 5.5 Query contract

```yaml
schema_version: structrr-code-query-v1
structrr_snapshot_id: structrr:sha256:...
analysis_id: analysis:sha256:... # required for findings/coverage queries
query_kind: findings | symbols | references | dependencies | components | coverage
filters:
  component_ids: []
  paths: [attestation/verifier/**]
  languages: [python]
  severities: [error]
  categories: [source, configuration]
  baseline_statuses: [new]
  rule_ids: []
  symbol_query: null
  relationship_kinds: []
limit: 50
```

Structrr resolves the symbol-artifact reference from `structrr_snapshot_id`; callers do not supply arbitrary artifact paths. `analysis_id` is required only for finding/coverage queries and must belong to the same snapshot revision. The response echoes the snapshot ID, resolved artifact IDs, exact normalized filters, matching records, omitted count, truncation flag, and evidence references. Query size is bounded by record count and encoded bytes. Paths are repository-relative and cannot escape the worktree.

## 6. Planner integration

Code analysis is a planning evidence provider. It does not own product intent, requirement extraction, work allocation, or final implementation judgments.

### 6.1 Flow

1. **Pin the planning snapshot.** Planning records the base commit and loads Structrr's component inventory and language capability report for that revision.
2. **Load or create analysis evidence.** Reuse a complete report matching the commit, component scope, provider versions, configuration, and controlled environment. Otherwise analyze relevant components and persist the report. Planning may continue with partial coverage only under an explicit degraded-evidence state; it must never label missing analysis complete.
3. **Give the planner a compact inventory.** Supply component IDs, languages, source/test roots, configured providers, coverage, and finding counts. Do not put every warning or raw output into the source-ledger prompt.
4. **Allow scoped queries.** The planner may request `structrr-code-query-v1` for concrete questions against the pinned Structrr snapshot. Initial scope comes from proposal paths, requirements, Structrr entities, and component relationships. Journal filters, snapshot/artifact IDs, result fingerprint, and query purpose.
5. **Ground code mapping.** Use symbol, reference, and dependency results to check whether a requirement may already be implemented, identify tests and callers, and propose candidate paths. A symbol match alone does not prove a requirement is implemented; a reference edge alone does not prove two work units depend on each other.
6. **Compile an evidence-bound plan.** Each implementation item records affected components, relevant source entities, required validation profiles, baseline analysis ID, and planned post-edit analysis scope.
7. **Analyze after implementation.** Run applicable read-only analyzers on affected components and compare with the pinned base report. Persist the candidate report and compact delta summary as review evidence.
8. **Route findings into repair.** New findings in changed/relevant scope become actionable validation evidence. Unchanged findings remain context unless the intent asks to fix them. Resolved findings are recorded as improvement evidence. Uncertain matches remain visible for review.

### 6.2 Planner evidence packet

```yaml
analysis_context:
  analysis_id: analysis:sha256:...
  revision: ...
  coverage_status: complete | partial | unavailable
  affected_components: [...]
  counts: {new_errors: 0, existing_errors: 2, warnings: 5}
  relevant_findings: [finding-reference, ...]
  relevant_symbols: [symbol-reference, ...]
  relevant_relationships: [relationship-reference, ...]
  omitted_count: 0
```

Every reference resolves to a durable artifact and retains provider, path, range, rule, revision, and fingerprint. The model may explain the relationship between evidence and a requirement, but deterministic orchestration selects scope, enforces caps, and validates returned references.

The planner must not copy unfiltered transcripts into questions, treat checker errors as user requirements, make repair of old findings mandatory without user intent, infer unsupported coverage from another component, or claim semantic completeness from syntax-only indexing.

### 6.3 Existing planner code

- Route `find_workspace_symbols` and `search_structural_document_analysis` through Structrr's artifact-backed symbol query while retaining BasedPyright as the Python provider that produces the first artifact.
- Route the symbol lookup loop in `github_issue_plan_generation.py` through the same contract so queries can specify component, symbol kind, path scope, and result limit.
- Extend `PlanningSession`'s structural cache to key on Structrr snapshot ID, symbol-artifact digest, analysis/index ID, and normalized filters. A path list alone is insufficient when checkout, provider, or configuration changes.
- Journal query requests and deterministic results with existing planning questions. Resume reuses accepted evidence and detects stale inputs.
- Keep semantic confirmation for ambiguous cases. Deterministic candidate selection reduces the question; it does not replace judgment.

### 6.4 Planning gates

- An unavailable provider is a coverage finding. Planning may continue only if existing language-capability policy permits degraded evidence for that component.
- If an implementation item requires structure or validation coverage that the component lacks, block execution before edits or use the existing explicit capability-exception flow.
- Base-revision findings are not new planning obligations.
- Candidate findings are fresh validation evidence, but the planner checks scope and changed-path relevance before routing them to repair.
- Analyzer output cannot approve proposal coverage, implementation correctness, or release readiness by itself.

## 7. Artifact lifecycle and comparison

### 7.1 Storage

- Write the normalized report/findings and bounded raw output under the run's existing artifact directory. Write the Structrr code-symbol inventory as its own versioned JSON artifact with a content digest.
- Register `ExecutionArtifact` references for the report and the symbol inventory, with distinct artifact types, schema versions, owners, and content references. The Structrr snapshot stores only the compact symbol-artifact descriptor and its provenance.
- Include revision, component fingerprints, analyzer versions, configuration digests, environment identity, and scope in every report.
- Artifacts are immutable. A rerun creates a new report; acceptance references the chosen report.
- Keep full reports out of ordinary chat and event summaries. Persist a compact summary and artifact reference.
- Query artifacts only for the active repository and execution unless another revision is explicitly requested.

### 7.2 Stable comparison

Finding fingerprints are provider-aware and exclude absolute paths, timestamps, and nondeterministic output. The initial fingerprint combines provider ID, rule ID, normalized message, repository-relative path, and a normalized source anchor.

1. Exact fingerprint match: `unchanged`.
2. Same provider/rule/path/message with a moved range: `unchanged` with relocated location.
3. Unique same-rule/message match within a component after a path move: `uncertain`.
4. Unmatched candidate: `new`. Unmatched base finding: `resolved` only if candidate analysis fully covers the corresponding base component and provider scope.
5. Material provider, configuration, or scope differences: comparison is `partial`; missing findings are not called resolved.

Status is derived by code. A model may suggest a match for human convenience but cannot set the persisted status.

### 7.3 Failure taxonomy

Analysis run status is `complete`, `partial`, `unavailable`, or `failed`. Provider/component details distinguish missing executable, invalid configuration, dependency/import resolution, timeout, process failure, parse failure, malformed output, output truncation, and unsupported file types. Tool exit code alone never determines overall status.

## 8. Safety and resource limits

- Analysis is read-only and runs under existing repository sandbox and validation restrictions.
- Use argv arrays, never shell interpolation. Resolve workdirs and query paths inside the selected worktree.
- Do not install dependencies automatically in the first implementation. Use an available environment or the repository's declared manager under existing approval policy; report missing dependencies honestly.
- Bound wall time, output bytes, file count, query result count, and model-context bytes. Persist timeout and truncation flags.
- Exclude `.git`, virtual environments, generated outputs, vendored trees, and ignored directories according to component policy; report exclusions in coverage.
- Do not persist source excerpts in broad report artifacts unless a provider requires them. Query responses include only requested, short excerpts and ranges.
- Do not send raw artifacts to Jev, Typesafe, or hosted services. Planner prompts contain only bounded evidence allowed by the existing model boundary.

## 9. Implementation sequence

Each milestone is a separate reviewable PR unless implementation evidence shows adjacent work cannot be separated. Do not replace BasedPyright before a measured Python fixture demonstrates an unresolved capability gap.

### PR 1: Analysis contracts and artifact persistence

Add typed versioned analysis-run, finding, provider-result, coverage, code-symbol, code-symbol-relationship, query, and query-response records. Add canonical JSON serialization, fingerprints, and persistence through existing execution/run artifact boundaries. Add a compact Structrr artifact-reference section, without embedding symbol arrays. Do not change planner behavior.

Acceptance: deterministic round trips; rejection of invalid statuses, path escapes, duplicate IDs, and bad fingerprints; byte-bounded raw outputs with explicit truncation; replay retrieves report content by reference; existing diagnostic hooks continue to work during migration.

### PR 2: Python diagnostics adapter and project-aware invocation

Build a BasedPyright JSON adapter. Resolve Python component, project configuration, interpreter, source roots, and execution environment from repository metadata. Add Ruff JSON as a separate provider when declared. Normalize findings and distinguish setup failures.

Acceptance: a src-layout fixture resolves imports when its config/environment require it; wrong-root analysis is marked as configuration/coverage failure; every diagnostic survives normalization; invalid JSON, timeout, missing executable, and import resolution have distinct outcomes; a gaterr-like fixture separates actual type errors from unresolved import roots.

### PR 3: Baseline comparison and compact report views

Implement component/provider-aware baseline matching and a compact summary renderer. Persist base report references and candidate deltas.

Acceptance: exact, relocated, new, resolved, uncertain, and incomparable cases; incomplete coverage cannot mark findings resolved; default output shows aggregate counts plus bounded new findings; full reports and raw output remain inspectable.

### PR 4: Scoped code/project query interface

Add `structrr-code-query-v1` and resolve code-structure queries through the symbol-artifact reference in the requested snapshot. Adapt existing BasedPyright symbols, structure, and references as the first artifact producer. Implement deterministic component/path/language/severity/rule/status/kind filters and bounded pagination.

Acceptance: queries return exactly matching records and filter provenance; semantic relationships are emitted only when supported; path traversal, unbounded limits, stale artifact IDs, and unsupported kinds fail clearly; existing BasedPyright tools remain available through the common provider.

### PR 5: Planner evidence integration

Add analysis evidence to `PlanningSession` and GitHub issue plan generation. Use revision-bound component summaries, bounded queries, and query fingerprints. Include relevant evidence references in planning context and work units; add post-implementation analysis obligations for code changes.

Acceptance: planner answers implementation questions with bounded symbol/reference queries and no diagnostic dump; replay reuses matching evidence and re-queries on stale revision/provider/config/scope; analysis failure cannot become pass or become a user requirement; only new findings in changed scope become default repair candidates; implementation plans bind validation and analysis to affected components.

### PR 6: Multi-language structure and analyzer providers

Integrate with the component/capability registry in `polyglot-repository-and-go-support.md`. Normalize language-provider output into the Structrr-owned JSON symbol schema. Use semantic indexers that emit SCIP as provider inputs where available. Use Tree-sitter or equivalent only for syntax outlines. Add native analyzer adapters as their language/component capability is supported.

Acceptance: mixed-language reports retain per-provider status; Python/Go/TypeScript structures retain language namespaces and provenance; syntax-only results cannot answer semantic reference queries; provider failure does not erase another provider's findings and yields partial status; unsupported files have explicit coverage and no fabricated symbols.

### PR 7: End-to-end planner and user verification

Add hermetic fixtures for Python src layouts, Python monorepos, mixed languages, incomplete environments, baseline debt, and changed-code findings. Verify artifact persistence, query scoping, planner provenance, compact summaries, repair routing, resume, and stale-report rejection.

Acceptance: no default planner prompt contains an unbounded transcript; every presented finding resolves to a durable report and revision; base/candidate comparison requires compatible scope; stored reports can be queried without rerunning providers; results are stable across filesystem ordering; outputs distinguish source findings, setup issues, analyzer failures, and unsupported coverage.

## 10. Verification strategy

Use golden normalized records and real provider-output fixtures, plus hermetic end-to-end runs. Failure and coverage states are first-class fixture cases.

The first end-to-end Python fixture reproduces the gaterr issue: a src-layout package, missing generic arguments, imports resolvable only from the declared source root, and a noisy repository-root invocation. The report must retain all findings, identify invocation/coverage problems, and prioritize actionable type errors without silently discarding warnings or claiming import errors are harmless.

The planner fixture starts from a requirement about existing cross-file behavior, queries only relevant components and symbols, creates a plan with evidence references, introduces one analyzer finding, and shows only the relevant new finding routed to repair. An unrelated pre-existing finding remains queryable without swamping the planner.

## 11. Non-goals and exit condition

Non-goals: replacing BasedPyright solely because output is noisy; running every analyzer on every repository at every planning step; treating a successful type check as proof of proposal fulfillment; automatically fixing or suppressing findings; asking an LLM to classify raw output; building a remote analysis service or uploading whole-repository indexes; claiming full semantic cross-language analysis without a semantic provider.

The plan is complete when a planner can pin a revision, reuse or produce a durable analysis artifact, ask bounded questions about findings and project structure, use cited evidence to plan affected work, and compare post-implementation findings against the base without flooding the model or user. Supported and unsupported scope is explicit, and all underlying findings remain inspectable.
