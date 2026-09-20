# Polyglot Repository and Go Support Implementation Plan

Status: proposed

## Purpose

This document defines how Powdrr will manage repositories containing one or
more implementation languages and then applies that architecture to Go. It is
an implementation plan, not a general design sketch: each phase identifies
the production boundary, required records, expected files, validation gates,
and exit condition another agent must satisfy.

Powdrr remains implemented in Python. Language support means that Powdrr can
discover, model, instruct OpenCode about, validate, and review code written in
that language.

## Target behavior

For any proposed change, Powdrr must be able to prove:

1. Which independently buildable repository components exist.
2. Which languages, manifests, tools, generated outputs, and validation rules
   belong to each component.
3. Which components, source subjects, durable intents, and cross-component
   contracts the proposal can affect.
4. Which language capabilities are available for every affected component.
5. Which exact, component-scoped context and edit permissions OpenCode needs.
6. Which read-only validation and verification evidence must be produced.
7. Whether the resulting multi-language diff fulfills the proposal and
   preserves all applicable intent.

The governing invariant is:

> Powdrr composes guarantees across the affected repository-component graph.
> It never privileges a primary language, silently skips a detected language,
> or treats unsupported analysis as successful evidence.

## Current state

The current implementation contains several language-neutral foundations:

- Git worktree isolation, changed-path enforcement, diff capture, proposal
  receipts, intent packets, repair loops, and post-implementation review do
  not depend on Python syntax.
- `bootstrap._file_entity_type` recognizes `.go` and several other source
  suffixes as source files.
- `discover_validation_profiles` detects a root `go.mod` and emits
  `go test ./...`.
- `ValidationRunner` executes arbitrary argv commands without a shell.
- `VerificationProviderRegistry` already establishes a provider boundary.

The current language assumptions are nevertheless structural:

- Structrr calls `_extract_python_source_model` directly and emits detailed
  subjects only for `.py` and `.pyi` files.
- Validation profiles have a name, argv, and source, but no component, working
  directory, mutability, applicability, environment, or toolchain identity.
- Validation profile provider identity is inferred from a name prefix.
- The default verification registry contains only the pytest provider.
- Profile names are repository-global, which creates collisions in monorepos.
- Root-only manifest detection misses nested modules and workspaces.
- OpenCode receives paths and intent but no deterministic component,
  language, package, toolchain, generated-code, or cross-language contract
  packet.
- A repository can receive file-level representation for an unsupported
  language without an explicit statement that structural coverage is absent.

Consequently, Powdrr can attempt a simple Go edit and run a broad Go test
command today, but it cannot provide the same structural and verification
guarantees it provides for Python.

## Scope

This program includes:

- deterministic repository-component discovery;
- explicit language capability and coverage reporting;
- pluggable source-model extraction;
- component-aware validation profiles;
- deterministic cross-component impact closure;
- Go module, package, symbol, and test support;
- targeted polyglot OpenCode work orders;
- read-only validation enforcement; and
- real mixed-language end-to-end fixtures.

This program does not:

- rewrite Powdrr in Go;
- ask an LLM to identify repository components, choose validators, or decide
  whether unsupported analysis is acceptable;
- infer cross-language relationships from similar names alone;
- promise exact inventory of dynamically generated Go subtests in the first
  Go provider version;
- run mutation-producing formatters or generators during validation; or
- require every repository component to use the same language or toolchain.

## Ownership boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| Structrr | Component inventory, source subjects, capability coverage, dependency graph, validation declarations, stable identities | Running tools or accepting implementation evidence |
| Procedrr | Mandatory capability, impact, implementation, validation, and intent-review gates | Language-specific parsing or command construction |
| Workrr | Deterministic discoverers, extractors, tool execution, provider adapters, normalized evidence | Product-intent interpretation or optionalizing gates |
| OpenCode | Requested edits and explicitly authorized edit-producing commands | Component discovery, validation acceptance, support waivers, or final intent decisions |

## Mandatory invariants

1. A repository may contain zero, one, or many components for each language.
2. No `primary_language` field or equivalent controls analysis or validation.
3. Component and source identities include an explicit language namespace.
4. Discovery and extraction order cannot affect persisted output.
5. Duplicate component IDs, source-subject IDs, or profile IDs fail closed.
6. Every detected component reports structural, validation, and verification
   support independently.
7. A proposal touching a component cannot proceed when its required support
   capability is unavailable or degraded.
8. File-only coverage is represented as file-only coverage, never as complete
   structural coverage.
9. Cross-language dependency edges require manifest, schema, build, generated
   artifact, or explicit Structrr evidence.
10. Validation profiles are scoped to a component and an in-repository working
    directory.
11. Validation operations declared read-only must leave the candidate tree
    unchanged. Mutation is a failed validation result.
12. Formatters, import rewriters, generators, and dependency updaters are edit
    operations and may run only inside an OpenCode implementation attempt.
13. Evidence fingerprints include component, command, working directory,
    controlled environment, toolchain, provider, candidate tree, and relevant
    manifest identities.
14. An LLM may explain a coverage failure but cannot convert it into a pass.
15. Every affected component receives all deterministically selected local and
    repository-global validation profiles.

## Canonical domain model

### Repository component

Add a provider-neutral immutable record:

```yaml
schema_version: repository-component-v1
component_id: component:go:services/auth
kind: module
language: go
root: services/auth
manifests:
  - services/auth/go.mod
source_roots:
  - services/auth
test_roots:
  - services/auth
generated_paths:
  - services/auth/api/generated/**
toolchain:
  kind: go
  requested_version: "1.24"
dependency_ids:
  - component:schema:api
discovery_refs:
  - services/auth/go.mod
fingerprint: sha256:...
```

`component_id` is repository-relative and stable while the component root and
manifest identity remain the same. A module-path change is a semantic component
change and produces a successor identity or explicit alias, not an unnoticed
relabeling.

### Language capability report

```yaml
schema_version: language-capability-v1
component_id: component:go:services/auth
language: go
structural_support: complete
validation_support: complete
verification_support: complete
diagnostics: []
adapter_versions:
  component_discoverer: go-component-v1
  source_extractor: go-source-v1
  validation_discoverer: go-validation-v1
  verification_provider: go-test-v1
toolchain_fingerprint: sha256:...
fingerprint: sha256:...
```

Support levels are closed enums:

- `complete`: the required capability ran and produced valid output;
- `file_only`: tracked files are known, but language structure is not;
- `declared_commands_only`: validation commands exist without language-aware
  result semantics;
- `unavailable`: the adapter or required toolchain is missing;
- `failed`: the adapter ran but returned invalid or incomplete output.

Capability reports may be persisted with degraded states so the repository can
be inspected. The proposal capability gate decides whether the affected change
may proceed and blocks all code edits requiring unavailable capabilities.

### Source model extractor

Define a registry-owned protocol, initially internal rather than a public
plugin API:

```python
class SourceModelExtractor(Protocol):
    language: str
    adapter_version: str

    def supports(self, component: RepositoryComponent) -> bool: ...
    def extract(self, request: SourceExtractionRequest) -> SourceModelResult: ...
```

`SourceModelResult` contains sorted source subjects, bindings, relationships,
diagnostics, coverage, and fingerprints. The registry must invoke every
matching extractor exactly once per component and merge results by canonical
ID. It must never use first-match or primary-language selection.

### Component-aware validation profile

Replace implicit profile semantics with:

```yaml
schema_version: validation-profile-v2
profile_id: validation:go:services/auth:test
component_id: component:go:services/auth
provider: go-test
cwd: services/auth
argv: [go, test, -mod=readonly, ./...]
environment: {}
mode: readonly
applies_to:
  paths: [services/auth/**/*.go, services/auth/go.mod, services/auth/go.sum]
  entity_ids: []
global: false
source_refs: [services/auth/go.mod]
toolchain_fingerprint: sha256:...
fingerprint: sha256:...
```

`cwd` must resolve inside the implementation worktree. Environment keys are an
explicit allowlisted map and participate in the fingerprint. `mode` is one of
`readonly` or `edit`. Only read-only profiles are legal in validation loops.

### Component dependency edge

```yaml
schema_version: component-dependency-v1
source_component_id: component:go:services/auth
target_component_id: component:schema:api
relationship: generated_from
evidence_refs:
  - buf.gen.yaml
  - services/auth/api/generated/models.go
fingerprint: sha256:...
```

Initial deterministic edge sources are workspace/manifests, package imports,
build configuration, generator configuration, schema references, and explicit
Structrr relationships. Identifier-name similarity is not evidence.

## Deterministic repository flow

```text
tracked repository state
-> discover all components
-> run all matching language adapters
-> merge source subjects and capability reports
-> build component dependency graph
-> compile proposal impact closure
-> gate required capabilities
-> compile component-scoped execution units
-> invoke OpenCode with targeted context and edit permissions
-> observe diff
-> rerun selected read-only profiles
-> verify exact required test cases
-> reconcile implementation and preserved intent
```

Every arrow produces a typed, fingerprinted artifact or a deterministic gate
result.

## PR 1: Repository component inventory and capability schema

### Goal

Represent a polyglot repository without changing current Python extraction or
feature-flow behavior.

### Implementation

- Add `src/powdrr_lift/structrr/components.py` containing
  `RepositoryComponent`, `ComponentDependency`, `LanguageCapabilityReport`,
  closed support enums, canonical serialization, and fingerprints.
- Add `src/powdrr_lift/structrr/component_discovery.py` containing a registry
  and deterministic merge logic.
- Implement initial discoverers for the currently recognized root manifests,
  but preserve multiple nested manifests instead of collapsing them.
- Ignore manifests beneath existing ignored directories such as `.git`,
  `.worktrees`, `node_modules`, and `vendor`.
- Add `components`, `component_dependencies`, and `language_capabilities` as
  versioned Structrr bootstrap sections.
- Keep current file entities intact for backward compatibility.
- Sort persisted components by `component_id` and dependencies by their full
  canonical tuple.

### Required tests

- Python-only repository yields one Python component.
- Two nested Go modules yield two components.
- A Go workspace links its declared modules without merging them.
- A mixed Python/Go/TypeScript fixture yields all components in stable order.
- Duplicate component IDs fail.
- Reversing filesystem enumeration produces byte-identical component output.
- Missing toolchains produce explicit capability diagnostics, not omitted
  components.

### Exit condition

Bootstrap describes every detected component and its support state without any
consumer relying on a primary language.

## PR 2: Source-extractor registry and Python migration

### Goal

Make source extraction genuinely language-pluggable while preserving existing
Python subject and binding output.

### Implementation

- Add `src/powdrr_lift/structrr/source_extractors.py` with the extractor
  protocol, request/result records, registry, collision checks, and merge.
- Move Python AST behavior from `structrr/bootstrap.py` into
  `structrr/python_source.py`.
- Register Python explicitly; do not make it a fallback adapter.
- Preserve existing Python IDs, stable keys, spans, kinds, and bindings so this
  PR does not force a Structrr baseline migration.
- Have bootstrap invoke the registry for all components and aggregate results.
- Emit `file_only` reports for detected source files whose component has no
  extractor.
- Increment only bootstrap section versions whose serialized semantics change.

### Required tests

- Existing Python bootstrap fixtures remain equivalent.
- Two extractors both execute in one repository.
- Extractor registration order does not affect output.
- Duplicate subject IDs or stable keys fail with component and adapter details.
- Syntax failure is attached to the correct component and cannot become
  `complete` coverage.
- Unsupported source files remain visible as file entities and report
  `file_only` structural coverage.

### Exit condition

Python is one adapter among many, with no direct Python extractor call in the
bootstrap orchestrator.

## PR 3: Validation profile v2 and read-only enforcement

### Goal

Make validation safe and unambiguous across components before adding another
language provider.

### Implementation

- Extend `DiscoveredValidationProfile` and Workrr `ValidationProfile` with the
  v2 fields defined above.
- Replace prefix-derived provider identity with an explicit `provider` field.
- Use component-qualified profile IDs; reject collisions.
- Validate `cwd` with the same repository-containment rules used for changed
  paths.
- Fingerprint argv, cwd, controlled environment, applicability, source refs,
  mode, component, and toolchain.
- Before each read-only validation, capture tracked and untracked candidate
  state. Capture it again afterward. Return a typed `mutated_repository`
  failure if the command changes repository contents.
- Do not automatically restore validator mutations; preserve the failed
  worktree for diagnosis.
- Separate OpenCode edit-command permissions from Workrr validation profiles.
  A formatter or generator cannot become a validation command merely because
  OpenCode is permitted to run it.
- Provide a compatibility loader for v1 bootstrap inventory while all current
  baselines are migrated.

### Required tests

- Identical profile names in different components do not collide.
- A profile runs in its declared component directory.
- Escaping `cwd` and environment injection are rejected.
- A read-only command that creates, edits, or deletes a file fails.
- A failed validator mutation cannot be accepted by a semantic judge.
- Profile fingerprints change for argv, cwd, environment, toolchain, source,
  applicability, or mode changes.
- Existing Python validation still runs through v2.

### Exit condition

Every production validation result identifies exactly where and how it ran and
proves that validation did not mutate the candidate tree.

## PR 4: Component impact closure and capability gate

### Goal

Select implementation context and validation from the affected component
graph, and block edits when Powdrr lacks required language support.

### Implementation

- Add a pure `compile_component_impact` function in Structrr.
- Seed the closure from proposal paths, entity references, intent references,
  protected inputs, and explicit component references.
- Expand only through typed component dependency edges whose relationship
  declares downstream impact.
- Select component-local profiles whose path/entity applicability intersects
  the closure and all profiles marked global.
- Produce explicit selection or exclusion explanations for every discovered
  profile.
- Add a deterministic required-capability policy. Code changes initially
  require `complete` structural and validation support. Exact required test
  contracts additionally require `complete` verification support.
- Add Procedrr operations and gates before `run_opencode` for impact
  compilation and capability sufficiency.
- Bind the impact and capability fingerprints into proposal review and
  implementation-start revalidation.

### Required tests

- A local Python change does not select an unrelated Go component.
- An explicit schema-to-Go dependency selects the Go component.
- Global profiles are always selected.
- Unsupported touched code blocks before OpenCode.
- Unsupported untouched code is reported but does not block an unrelated
  component.
- Unknown, stale, conflicting, or missing capability evidence blocks.
- The same repository and proposal compile to the same closure and profile
  worklist regardless of input ordering.

### Exit condition

The production flow cannot invoke OpenCode until affected components and
required language capabilities have been established mechanically.

## PR 5: Go component and source adapter

### Goal

Provide complete structural coverage for ordinary Go modules and workspaces.

### Implementation decisions

- Use the Python `tree-sitter` and `tree-sitter-go` packages, pinned to a
  compatible minor range, for deterministic syntax and source spans. Powdrr
  remains Python.
- Use `go list -json` for toolchain-resolved module, package, active-file,
  import, and dependency identity.
- Detect `go.work` first, then its modules, then tracked standalone `go.mod`
  files not already represented. Never treat `vendor` modules as repository
  components.
- Define Go component IDs from repository-relative module roots. Record module
  import path as versioned component metadata.
- Detect generated files from the canonical `Code generated ... DO NOT EDIT.`
  marker and record build constraints.
- Parse package declarations, named types, structs, interfaces, functions,
  methods and receivers, constants, variables, imports, tests, benchmarks,
  fuzz tests, and examples.
- Use subject IDs of the form
  `go:<component-id>::<import-path>.<qualified-symbol>` and include receiver
  identity for methods.
- Include both ordinary and `_test.go` files. Mark files excluded by the active
  build configuration without pretending they were compiled.
- Create package-import dependency edges within Go. Cross-language edges still
  require explicit build/schema/Structrr evidence.
- If the Go executable is absent or `go list` fails, retain syntax-level
  diagnostics but report structural support as unavailable or failed. Do not
  claim complete support.

### Expected files

- `src/powdrr_lift/structrr/go_components.py`
- `src/powdrr_lift/structrr/go_source.py`
- `tests/test_structrr_go_components.py`
- `tests/test_structrr_go_source.py`
- pinned parser dependencies in `pyproject.toml` and the lockfile

### Required tests

- Single module, nested modules, and `go.work` fixtures.
- Package, struct, interface, alias, function, method, receiver, import,
  constant, variable, and test subject extraction.
- Exact UTF-8 line spans.
- Stable output after file enumeration changes.
- Stable identities across formatting-only edits.
- Explicit identity change for package/module/symbol renames.
- Build-tagged, generated, external-test-package, and malformed-file cases.
- Toolchain unavailable and `go list` failure diagnostics.
- No source subjects emitted from `vendor`.

### Exit condition

Structrr can identify and bind Go source subjects at package and symbol level,
and every detected Go component has an honest capability report.

## PR 6: Go validation and exact verification provider

### Goal

Produce fresh, read-only, component-scoped Go evidence and resolve Go required
test cases to exact executable selectors.

### Validation discovery

For each Go component:

- prefer repository-declared CI or build-system commands;
- otherwise add `go test -mod=readonly ./...`;
- add non-mutating formatting verification using a Workrr result adapter that
  treats any `gofmt -l` output as failure;
- add `go vet` only when repository policy or CI declares it;
- discover declared `staticcheck` and `golangci-lint` profiles;
- honor component cwd, workspace mode, build tags, vendor mode, CGO, GOOS, and
  GOARCH when explicitly declared; and
- fingerprint `go version`, selected environment, `go.mod`, `go.sum`,
  `go.work`, and `go.work.sum` as applicable.

Do not execute `go fmt`, `gofmt -w`, `goimports -w`, `go generate`, `go get`,
or `go mod tidy` as validation.

### Go test provider

Add `GoTestVerificationProvider` and register it beside pytest.

- Canonical selector: `<package-import-path>::<top-level-test-name>`.
- Inventory packages with `go list` and executable top-level tests with
  component-scoped `go test -list`.
- Inventory `Test`, `Benchmark`, `Fuzz`, and `Example` names, but initial
  required-test contracts may use only top-level `Test` and `Example` entries.
- Run exact selected tests with an anchored, escaped `-run` expression.
- Request `go test -json` and normalize package/test pass, fail, skip, build
  failure, timeout, and missing-test outcomes.
- A package-level pass without the selected test event is `not_collected`, not
  passed.
- Dynamic subtests are not inventory identities in v1. A future provider
  version may add runtime subtest discovery with a new selector schema.

### Required tests

- Root module and workspace profile discovery.
- Read-only module mode and vendor-mode selection.
- Unformatted Go fails without modifying the file.
- Exact test inventory is sorted and fingerprinted.
- Exact test pass, fail, skip, package build failure, timeout, and missing test.
- Regex metacharacters in selectors cannot broaden execution.
- Toolchain or manifest changes invalidate inventory and evidence.
- Two modules containing the same test name remain distinct.
- A Go contract compiles into a verification obligation and stale evidence is
  rejected.

### Exit condition

The production flow can require and verify exact Go tests and broad Go profiles
with immutable, non-mutating evidence.

## PR 7: Polyglot OpenCode packets and edit commands

### Goal

Give OpenCode the smallest complete context for each component operation while
allowing language-specific edit tools without weakening Workrr validation.

### Implementation

- Extend `ExecutionUnit`, `IntentPacket`, and `ImplementationRequest` with
  selected component IDs, language capabilities, source-subject refs,
  cross-component contract refs, and edit-command permissions.
- Compile these fields from the accepted impact closure; OpenCode does not
  choose them.
- Include component root, language/toolchain metadata, relevant source spans,
  neighboring tests, generated-file policy, and validation profile names in
  the rendered worker prompt.
- Keep full intent content in Structrr, but send only clauses selected for the
  operation with deterministic selection explanations.
- Permit Go formatter/import/generator commands only when declared as edit
  commands for that operation and when all possible output paths are allowed.
- Treat `go.mod`, `go.sum`, generated output, mocks, and API clients as ordinary
  planned changes: they require explicit proposal operations and allowed paths.
- Independently capture the resulting diff and rerun the entire selected
  validation worklist after every repair.

### Required tests

- A Go-only operation receives no unrelated Python source context.
- A cross-language schema operation receives all and only affected component
  contracts.
- An unplanned `go.mod`, `go.sum`, generated-file, or unrelated-component edit
  fails scope review.
- `gofmt -w` permission does not authorize arbitrary shell commands or paths.
- A formatter-generated diff is included in post-implementation evidence.
- Repair packets retain the original component and intent boundaries.
- Reduced-context prompts remain sufficient for a deterministic fake worker to
  complete each fixture operation.

### Exit condition

Every OpenCode invocation is component-aware, language-aware, intent-targeted,
and independently validated without sending the entire repository intent set.

## PR 8: Polyglot end-to-end proof and repository onboarding

### Goal

Prove the complete flow against a repository where one feature crosses
language boundaries and make the required Powdrr assets installable in an
external repository.

### Fixture

Add a small tracked fixture containing:

```text
api/openapi.yaml
services/auth/go.mod
services/auth/*.go
clients/python/pyproject.toml
clients/python/src/**
web/package.json
web/src/**
generator configuration linking all three consumers to api/openapi.yaml
```

The feature changes the shared schema and requires explicit Go and Python
consumer changes while leaving the unrelated web component unchanged. A
second scenario changes only Go behavior and proves unrelated components are
excluded.

### Implementation

- Add a `powdrr init` or installed-resource resolution path for the taxonomy
  and checked-in Procedrr definitions currently expected inside the target
  repository. Choose one source of truth and version it; do not silently copy
  mutable process files on every run.
- Add CI setup for a pinned supported Go toolchain.
- Exercise bootstrap, proposal impact, capability gates, targeted worker
  requests, Go/Python validation, exact test evidence, repair, intent review,
  changelog generation, and PR-ready output.
- Use a deterministic fake OpenCode provider for mandatory CI. Add a separate
  opt-in live OpenCode harness; live-model behavior is not the acceptance gate.

### Required gates

1. Component inventory and source extraction are deterministic.
2. Shared-schema impact reaches exactly the declared consumers.
3. Unsupported affected components block before editing.
4. Every selected validation profile runs in the correct cwd.
5. Validators leave the candidate tree unchanged.
6. Exact Python and Go required tests produce fresh evidence.
7. A deliberately broken Go edit enters targeted repair.
8. Unrelated component edits are rejected.
9. Post-implementation review checks the full cross-language diff against all
   affected intent.
10. Replaying the same fixture produces equivalent fingerprints and worklists.

### Exit condition

Powdrr can be pointed at an initialized mixed-language repository and carry a
cross-language feature through proposal, implementation, repair, validation,
intent preservation, and PR preparation without Python-specific control-flow
assumptions.

## Compatibility and migration

- Bootstrap readers must accept the previous section versions while migration
  is in progress. Writers emit only the newest version.
- Existing Python IDs remain stable through PR 2.
- Validation profile v1 records are readable but cannot satisfy new
  content-addressed evidence after v2 enforcement is enabled.
- Existing required test contracts continue to use pytest unchanged.
- Go support does not alter Python provider semantics.
- Capability gating is enabled only after current Python repositories produce
  complete reports, preventing a flag day that blocks all feature runs.
- Baseline regeneration must be an explicit committed transition with a review
  of ID additions, removals, and aliases.

## Failure policy

| Failure | Required result |
| --- | --- |
| Unknown language in untouched component | Record file-only coverage; continue unrelated work |
| Unknown language in affected code component | Block before OpenCode |
| Missing required toolchain | Record unavailable capability; block affected work |
| Extractor parse failure | Record failed capability with file diagnostics; block affected work |
| Duplicate identity | Fail bootstrap |
| Unknown cross-component relationship | Do not infer an edge; require explicit evidence |
| Validation mutates repository | Fail validation and preserve diagnostics |
| Test selector absent | `not_collected`; never pass |
| Dynamic Go subtest requested in v1 | Unsupported selector; block contract compilation |
| Toolchain or manifest changes after review | Invalidate affected inventory, worklist, and evidence |

## Program dependency order

```text
PR 1  Component inventory and capability schema
  |
PR 2  Extractor registry and Python migration
  |
PR 3  Validation profile v2 and read-only enforcement
  |
PR 4  Impact closure and capability gate
  |
PR 5  Go component and source adapter
  |
PR 6  Go validation and verification provider
  |
PR 7  Polyglot targeted OpenCode packets
  |
PR 8  Mixed-language end-to-end proof and onboarding
```

Do not implement PRs against independently invented versions of predecessor
records. Each phase fingerprints artifacts consumed by the next phase.

## Per-PR agent checklist

For every implementation PR, the agent must:

1. Create a dedicated worktree and branch from current `origin/main`.
2. Read this plan and the intent-linked verification plan.
3. State the exact production transition the PR protects.
4. List new and changed schemas, section versions, and fingerprint inputs.
5. Implement pure records and deterministic functions before orchestration.
6. Add malformed, stale, collision, ordering, and unavailable-tool cases.
7. Add the production Structrr, Procedrr, and Workrr integration required by
   that phase; disconnected types do not satisfy an exit condition.
8. Prove OpenCode cannot bypass component, scope, capability, or validation
   gates through direct invocation.
9. Run focused tests while developing, then the full repository suite,
   formatting, linting, typing, changelog validation, and scope validation.
10. Inspect the final diff for generated files and unrelated edits.
11. Commit, push, and open a PR without merging it.

The PR description must identify supported and unsupported repository shapes,
deterministic discovery inputs, capability failure behavior, evidence freshness
rules, migration behavior, and explicit non-goals.

## Program completion condition

The program is complete only when:

```text
polyglot repository state
-> deterministic component and capability inventory
-> language-specific structural model
-> cross-component affected closure
-> capability-complete accepted proposal
-> component-scoped targeted OpenCode work orders
-> independently observed multi-language diff
-> non-mutating component validation and exact verification evidence
-> operation fulfillment and intent-preservation proof
```

Adding a new language after Go should require implementing adapters and tests,
not modifying core proposal, process, or evidence semantics.
