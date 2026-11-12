# Structrr references in source and automatic miniSWE context

Status: **Complete** for the planned Python source-reference scope on supported
macOS and Linux protection adapters. The seven implementation stages and the
section 12 reconciliation workflow are integrated. The last production gap—wiring
post-worker reconciliation into normal Workrr source-task dispatch—merged in
[PR #1440](https://github.com/powdrr-io/powdrr-lift/pull/1440); the shared
reconciliation engine, CLI, and deterministic edit path merged in
[PR #1436](https://github.com/powdrr-io/powdrr-lift/pull/1436).

| Stage | Current implementation | Regression coverage |
| --- | --- | --- |
| Python annotation parsing and owner indexing | `packages/service/src/powdrr_service/structrr/source_references.py` | `tests/test_structrr_source_references.py` |
| Frozen canonical catalog and lifecycle resolution | `packages/service/src/powdrr_service/structrr/reference_catalog.py`, `reference_definitions.py` | `tests/test_structrr_reference_catalog.py` |
| Bounded source reader and context enrichment | `packages/service/src/powdrr_service/structrr/source_reader.py`, agent environment and observation adapters | `tests/test_structrr_source_reader.py`, `tests/test_minisweagent_environment.py` |
| Placement handoff and immutable request contract | `packages/service/src/powdrr_service/structrr/source_reference_handoff.py` | `tests/test_source_reference_handoff.py` |
| Independent placement and preservation validation | `packages/service/src/powdrr_service/structrr/reference_validation.py`, `src/powdrr_lift/workrr/source_reference_validation.py` | `tests/test_structrr_reference_validation.py` |
| Protected Workrr execution and patch transfer | `src/powdrr_lift/workrr/source_reference_run.py`, `source_reference_dispatch.py` | `tests/test_source_reference_run.py`, `tests/test_source_reference_dispatch.py` |
| Reciprocal reconciliation and deterministic annotation edits | `packages/service/src/powdrr_service/structrr/reference_reconciliation.py`, `reference_editing.py`, `src/powdrr_lift/workrr/source_reference_postrun.py` | `tests/test_structrr_reference_reconciliation.py`, `tests/test_source_reference_dispatch.py` |

The bootstrap and standalone check/reconcile commands are implemented in
`src/powdrr_lift/cli.py`. The design contracts remain linked below. The completed
implementation supports Python on macOS and Linux protection adapters.
Unsupported platforms and other languages fail closed or remain future work, as
described by the design.
Contract references: [catalog](../../design/structrr-reference-catalog.md),
[reader](../../design/structrr-source-reader.md),
[environment](../../design/structrr-miniswe-environment.md),
[handoff](../../design/structrr-source-reference-handoff.md),
[validation](../../design/structrr-reference-validation.md), and
[rollout](../../design/structrr-reference-rollout.md).

The pure parser exposes `index_python_source`, `applicable_references`, and
`find_owner`, with versioned `SourceReferenceIndex` serialization. Parse status
describes scope extraction; annotation diagnostics must also be checked. Partial
source failures retain discovered markers without claiming inherited context.
Applicable-reference results retain every contributing location, ordered by ID
and source location, so consumers can resolve distinct IDs once. Owner selectors
fail on duplicate qualified names; spans distinguish observations within one
hashed snapshot and do not establish identity across moves.

## 1. Outcome

Code carries stable references to the Structrr features, decisions, assumptions,
and invariants that explain it. Structrr owns their meaning, provenance, lifecycle,
and verification requirements. miniSWE receives relevant intent while inspecting
source and writes correctly placed references while implementing accepted work.
Workrr independently checks the result.

An agent reading a small slice inside a function must be able to discover the
function's obligations even when its annotation is outside the requested slice.
An agent writing a function must know which references belong there. Moving code
must preserve or deliberately relocate applicable references. Deleting code must
not silently leave an accepted implementation obligation without an owner.

The initial implementation supports Python source. Other languages require
language-specific comment and scope adapters; do not apply Python token rules to
them. Bootstrap can backfill references using the reconciliation phase in
section 12; the same phase can run independently on an existing Structrr document.

## 2. Boundaries and implementation ownership

The implementation is split across the service, agent, and Workrr packages.
These are the current entry points and adjacent systems with which source
references integrate:

| Current file | Responsibility |
| --- | --- |
| `src/powdrr_lift/workrr/coding_agent.py` | `ImplementationRequest`, prompt rendering, provider invocation, configuration and trajectory handling |
| `packages/agent/src/powdrr_agent/execution/minisweagent_session.py` | Large-task and continuation entry point; constructs the configured environment |
| `src/powdrr_lift/structrr/intent.py` | Compatibility-facing intent source and clause types |
| `packages/service/src/powdrr_service/structrr/active_intent.py` | Active-intent resolution and baseline/feature-overlay semantics |
| `packages/service/src/powdrr_service/structrr/source_manifest.py` | Source fingerprints and comparisons |
| `src/powdrr_lift/workrr/coding_agent_validation.py` | Independent attempt validation and reports |
| `src/powdrr_lift/workrr/task_agent.py` | Worker orchestration and inherited-intent checks |
| `packages/agent/src/powdrr_agent/execution/minisweagent_environment.py` | miniSWE local-environment interception and observation enrichment |
| `packages/service/src/powdrr_service/structrr/source_references.py` | Python source parsing, owner indexing, marker discovery and applicability |
| `packages/service/src/powdrr_service/structrr/reference_catalog.py` | Frozen canonical catalog and lifecycle resolution |
| `packages/service/src/powdrr_service/structrr/reference_reconciliation.py` | Reciprocal reconciliation of source markers and accepted obligations |

The source-reference-specific modules and their tests are indexed at the top of
this document. The boundary and compilation designs are linked in the contract
references above; their current paths are `docs/design/external-coding-agent-boundary.md`
and `docs/design/source-anchored-semantic-contract-compilation.md`.

Observed mini-swe-agent 2.4.6 behavior:

1. `DefaultAgent.execute_actions()` calls `env.execute(action)` for each action.
2. It formats returned outputs through `model.format_observation_messages()`.
3. `LocalEnvironment.execute()` executes arbitrary shell commands, returns output,
   return code and exception information, and recognizes the submission sentinel.
4. `get_environment()` accepts a fully qualified class name through the
   `environment_class` configuration field.

There is no dedicated native source-read tool. Implement a custom environment
and a source-reader command; do not patch installed miniSWE files or monkeypatch
`open()`. A child process reading files cannot be intercepted by monkeypatching
the parent Python process.

The current code still contains continuation/recovery facilities. This feature
must not add new recovery invocations or validation-derived repair prompts. In
the target single-invocation path, failed post-run reference validation fails the
attempt and returns findings to Workrr. Normal tool observations during the one
invocation do not change the initial prompt or its fingerprint.

This plan explicitly extends the target boundary's restriction on internal IDs:
source reference IDs are actionable implementation inputs. Include those IDs
and resolved statements, but keep private validation manifests, raw Structrr
snapshots, classifier output and internal workflow state out of worker context.
The handoff integration updated
`docs/design/external-coding-agent-boundary.md` to document the shared request
contract and private validation boundary.

## 3. Annotation syntax and scope

Use these exact forms in Python comments:

```python
# structrr[module]: feature:payment-recovery

# structrr: invariant:payment-idempotency
# structrr: decision:retry-backoff
@instrumented
async def schedule_retry(payment):
    # structrr[block]: decision:retain-original-idempotency-key
    retry.idempotency_key = payment.idempotency_key
```

Each annotation contains one opaque canonical ID. The examples are illustrative;
an ID's prefix is not its type. Resolve type from Structrr. IDs contain only
ASCII letters, digits, `_`, `-`, `.`, `/`, and `:`; maximum length is 256
characters. Existing IDs outside this grammar require an explicit migration or
grammar revision before rollout, never lossy rewriting. Trim surrounding spaces;
do not lowercase IDs. Recognize only full standalone COMMENT tokens matching
the form, not strings, docstrings, trailing comments, or arbitrary prose.
Unrecognized text beginning with the reserved `# structrr` marker produces a
malformed-annotation diagnostic. Ordinary comments mentioning Structrr do not.

Scope rules are deterministic:

- `structrr[module]` is legal only at module level before the first executable
  statement, allowing a module docstring, shebang and encoding declaration.
  It applies to the whole file. It does not by itself satisfy an obligation
  requiring a function owner.
- Plain `structrr` attaches to the immediately following function, async function
  or class at the same indentation. A contiguous annotation group can include
  ordinary standalone comments. No blank lines or intervening statements are
  allowed. For decorated declarations, attach above the first decorator.
- `structrr[block]` attaches to the next AST statement at the same indentation,
  under the same adjacency rules. A compound statement owns its entire subtree;
  a simple statement owns its own span. Do not invent a free-floating range end.
- A read inherits applicable module, enclosing class, enclosing function, and
  enclosing block references. Duplicate effective IDs are resolved once while
  retaining every contributing location.
- Detached references, duplicate IDs in one owner group, illegal module markers,
  and plain markers preceding assignments produce diagnostics.

Use `tokenize` to discover real comments and `ast` to bind owners. Account for
decorator spans and nested declarations. Store one-based inclusive line ranges;
line numbers are navigation, not durable identity. Qualified names such as
`RetryPolicy.schedule_retry` identify owners within a file. Distinguish duplicate
qualified names with spans and report ambiguity when a selector requires one
owner. Do not import or execute candidate modules.

Correct placement means the smallest declaration or block responsible for the
obligation. Do not repeat a feature ID on every helper or put all IDs at a file's
top to satisfy coverage. A cross-cutting requirement may legitimately have
multiple explicit owners, specified by the handoff.

## 4. Canonical catalog and lifecycle (implemented)

The parser and immutable index are implemented in
`packages/service/src/powdrr_service/structrr/source_references.py`; the frozen
worker catalog is implemented in
`packages/service/src/powdrr_service/structrr/reference_catalog.py` and
`reference_definitions.py`. Compatibility exports remain under
`src/powdrr_lift/structrr/`.
Use dataclasses and explicit `to_data`/`from_data` validation consistent with the
repository. Reject unsupported schema versions and conflicting duplicate IDs.

Define these versioned contracts:

- `structrr-reference-catalog-v1`: base commit, accepted document digest, optional
  explicitly authorized amendment digest, catalog digest, entries, renderer
  revision. Each entry has canonical ID, kind, statement, source reference,
  version, authority (`accepted` or `authorized_amendment`), lifecycle, optional
  rationale, assumption invalidation triggers, evidence references and successor.
- `structrr-source-index-v1`: relative path, source SHA-256, parse status,
  annotations with owner kind/name/span, and diagnostics.
- `structrr-reference-obligations-v1`: design/base/catalog digests plus required
  reference placements and preservation requirements, described in section 7.
- `structrr-reference-report-v1`: input digests, scan coverage, findings,
  mechanically checked placements, semantic review results, behavioral evidence
  references, and separate statuses for each check category.

The catalog is a projection of canonical Structrr records, not another writable
intent store. Existing active clauses use `clause_id` as their ID. Explicit
adapters project canonical feature, decision and assumption records from the
accepted design data; these types are not represented by `IntentKind`, which
continues to list decision, invariant, procedure and guidance. Each adapter is
backed by canonical representation and round-trip validation. The catalog never
infers an assumption or accepted human decision from code comments.

Use the following public API boundaries. Define the named immutable result types
in their owning modules; keep parsing/resolution free of filesystem side effects.

```python
# source_references.py
def index_python_source(path: str, source: bytes) -> SourceReferenceIndex: ...
def applicable_references(
    index: SourceReferenceIndex, start: int, end: int
) -> tuple[SourceAnnotation, ...]: ...


# reference_catalog.py
def compile_reference_catalog(
    accepted_document: Mapping[str, Any],
    *,
    base_commit: str,
    authorized_amendment: Mapping[str, Any] | None = None,
) -> ReferenceCatalog: ...
def resolve_reference(catalog: ReferenceCatalog, ref: str) -> ReferenceResolution: ...


# source_reader.py
def read_source(
    workspace: Path, catalog: ReferenceCatalog, path: str, start: int, end: int
) -> SourceReadResult: ...


# reference_validation.py
def validate_reference_placements(
    baseline_indexes: Sequence[SourceReferenceIndex],
    candidate_indexes: Sequence[SourceReferenceIndex],
    catalog: ReferenceCatalog,
    obligations: ReferenceObligations,
) -> ReferenceValidationReport: ...
```

The pure validator returns pending semantic-review items; orchestration performs
the independent review and incorporates its result. Parser and resolution errors
are structured findings, not uncaught exceptions. Invalid configuration and
corrupt frozen artifact schemas fail initialization explicitly. Keep CLI wiring,
event recording and model formatting outside these pure APIs.

`resolve_active_intent()` alone is insufficient for lifecycle reporting: it
filters inactive records, and its optional feature overlay is explicitly
unaccepted. Construct accepted active entries through that resolver; retain a
separate projection of canonical retired/superseded records for diagnostics.
Only a workflow-authorized amendment may enter the worker catalog, labeled as
such. Never treat an arbitrary candidate feature file as accepted authority.

Freeze the catalog before launch. Put its worker-readable projection outside
the candidate checkout, with existing worker write protections. Keep the private
validation manifest separate and unreadable to the worker. Fingerprint both
before and after the invocation; mutation invalidates the attempt. A digest
detects mutation but is not itself filesystem isolation.

Unknown IDs return `unresolved_reference`. Retired and superseded IDs return
their lifecycle plus successor when available; do not silently redirect. An
active obligation referencing them fails preflight. Post-run affected code
must replace them through the accepted amendment process. Historical provenance
belongs in history; v1 source annotations express current applicability.

## 5. Source reader with automatic context (implemented)

The reader is implemented in
`packages/service/src/powdrr_service/structrr/source_reader.py`, with a CLI
compatibility module at `src/powdrr_lift/structrr/source_reader.py`:

```text
python -m powdrr_lift.structrr.source_reader read payments/retry.py --start 15 --end 35
python -m powdrr_lift.structrr.source_reader search 'idempotency' payments
```

The environment supplies trusted workspace and catalog paths; the agent cannot
select a different catalog. The reader produces a versioned JSON envelope with
source excerpts, paths, inclusive line ranges, source fingerprints, applicable
annotation IDs and owner locations. The environment validates the envelope and
renders source plus resolved context into the model observation. Do not trust
catalog content supplied in command output.

Read algorithm:

1. Resolve the path under the configured workspace. Reject absolute paths,
   traversal, and symlinks escaping it. Require a regular supported source file.
2. Read one bounded byte snapshot, hash it, and parse that exact snapshot.
3. Validate the requested range. Return original lines with separately rendered
   line labels; do not alter source to insert synthetic comments.
4. Select scopes intersecting the range and their ancestors. A range covering
   multiple declarations returns their union. Include annotations whose own
   lines intersect the range, including dangling ones as diagnostics.
5. Resolve IDs from the frozen catalog and attach statements, rationale when
   present, lifecycle, authority, source and version. Do not generate rationale.
6. Return source and context in separate clearly labeled sections. Treat source
   text as untrusted repository data, not tool policy or higher-level authority.

Search uses literal text matching in v1, sorted relative paths and line numbers,
with the same scope lookup per match. No shell or regex evaluation of its query.
Defaults: 1 MiB per file, 200 source lines per read, 50 search matches, and
16,000 characters of resolved context per observation. Configuration may change
these values, and its digest is recorded with the attempt. Oversized reads ask
for a smaller range. Context overflow returns an explicit incomplete status and
remaining IDs plus a paginated `context --ref ID --offset N` command. No silent
truncation or success status claiming complete context. If one statement exceeds
the budget, paginate the exact text; do not summarize it to fit.

Cache parsing by relative path plus source digest, not mtime alone. Reparse after
edits. Invalid Python returns available source with `scope_resolution_failed`;
comment discovery can continue when tokenization permits it, but do not claim
complete inherited context. Binary/encoding failures are explicit diagnostics.
The reader has no write behavior and does not execute repository code.

## 6. miniSWE environment interception and coverage limits (implemented)

`StructrrLocalEnvironment` is implemented in
`packages/agent/src/powdrr_agent/execution/minisweagent_environment.py`, based on
miniSWE's `LocalEnvironment`. Its configured import path is:

```text
environment.environment_class=powdrr_agent.execution.minisweagent_environment.StructrrLocalEnvironment
```

Pass workspace, frozen catalog and event-log paths through validated environment
configuration fields. Preserve normal miniSWE environment options. Extend the
configuration model explicitly rather than passing unknown fields to the base.
The configured environment is used by both CLI launch and
`packages/agent/src/powdrr_agent/execution/minisweagent_session.py`, including
large task files. Verify package importability in Harbor/container runs. Never
depend on a worktree-local virtual environment or a user-global mini config.

For each action:

1. Execute the original command once through the base environment. Preserve
   timeout, return code, exception information and submission-sentinel behavior.
2. For reader envelopes, verify path and source digest against the workspace,
   then compute context using the trusted catalog. Reject stale/spoofed envelopes.
3. For ordinary output, recognize visible annotation markers, including common
   `path:line:` search prefixes. Resolve their IDs and append context. This path
   claims only visible-reference discovery, not AST scope coverage.
4. Keep the original output string unchanged as a prefix and append a delimited
   context section. Never append metadata to the submitted final artifact.
5. Emit one structured event with command/output digests, source/catalog digests,
   IDs, completeness, diagnostics and enrichment mode. Avoid copying credentials
   or all process environment variables into the event.

Check the configured model formatter's truncation behavior. Reserve context space
or format context as a separate supported observation message after truncating
only the source view. Test the actual configured formatter: adding context to
`output` is insufficient if the formatter subsequently removes it. Record exact
delivered context in the trajectory; do not deduplicate solely because an ID was
seen earlier. Later turns may still need the obligation.

Guarantee: supported reader calls automatically supply enclosing context.
Ordinary shell output receives best-effort visible-marker enrichment. Reading
only a function body with `sed`, or printing source from arbitrary Python, can
omit every marker. Do not infer accessed files by parsing arbitrary shell text
or call this universal interception.

If complete mediation of every source read becomes a product requirement, it
needs a separate filesystem/process access design and adversarial bypass tests.
Do not claim a prompt instruction or PATH wrappers enforce that guarantee. This
implementation makes the reader the default, records coverage, and reports its
limits honestly.

## 7. Making miniSWE write the references (implemented)

Placement handoffs are implemented in
`packages/service/src/powdrr_service/structrr/source_reference_handoff.py`, with
compatibility exports at `src/powdrr_lift/workrr/source_reference_handoff.py`.
The compiler derives obligations from the accepted design and shares the same
canonical contract between prompt rendering and private validation data. It does
not infer mandatory IDs from a diff.

Each placement obligation contains:

```yaml
obligation_id: placement:retry-idempotency
ref: invariant:payment-idempotency
relationship: preserves
requirement: Every payment retry retains the original idempotency key.
owner:
  path: payments/retry.py
  kind: function
  qualified_name: schedule_retry
cardinality: exactly_one
verification_refs:
  - case:retry-keeps-key
```

Allowed relationships: `implements`, `realizes`, `preserves`, `relies_on` and
`checks`. Validate them against canonical kinds: feature, decision, invariant,
assumption and test ownership respectively. The source comment itself remains
small; the obligation record holds the richer relation.

Existing owners use exact path/kind/name selectors. New owners use a planned
exact selector when design determines one. When design leaves implementation
layout free, specify allowed paths, owner kind and semantic responsibility,
with cardinality `one_or_more`; mark binding `requires_semantic_review`.
miniSWE reports its chosen owner, and an independent reviewer binds it. Never
turn an ambiguous semantic selector into a passing deterministic check. Multiple
required owners need separate obligations, not a global count of ID occurrences.

Reference requirements are part of the original prompt:

- Under `Required behavior`, show each ID, exact statement, relationship,
  responsibility and expected owner.
- Under `Preserve and avoid`, give annotation grammar, placement rules, baseline
  references to preserve, and accepted supersessions/removals.
- Under `Focused commands`, provide the source-reader and local reference-check
  commands. Inspection defaults to the reader and context-aware search.
- Under `Completion protocol`, request reference-to-owner claims and unresolved
  design gaps in the existing submission, not an additional repository artifact.

Use this worker instruction verbatim as the starting template:

> Add the supplied Structrr IDs to the smallest declarations or blocks that own
> their requirements, using the supplied annotation convention. Preserve and
> relocate existing applicable references when moving code. Read referenced
> context before changing the owner. Do not invent IDs, alter canonical intent,
> or satisfy coverage by listing IDs in unrelated code. If an essential decision
> or assumption is missing, report the gap with its location and consequence.
> Run the local reference check before submitting. Report each implemented ID
> and its owning path and qualified symbol. These claims are independently checked.

An unresolved design gap leaves the attempt incomplete; the normal design
workflow decides any amendment. A worker cannot self-approve it. Local checks
during the invocation are allowed; Workrr does not start a second coding process
or append a repair prompt after post-run validation.

## 8. Independent validation and preservation (implemented)

Independent validation is implemented in
`packages/service/src/powdrr_service/structrr/reference_validation.py`, with
Workrr orchestration in `src/powdrr_lift/workrr/source_reference_validation.py`
and a compatibility export in `src/powdrr_lift/structrr/reference_validation.py`.
Trusted run configuration supplies catalog and obligation inputs; reference
findings remain separate from behavioral evidence.

Validation sequence:

1. Check design/base/catalog fingerprints against the recorded attempt.
2. Scan baseline and candidate Python source from the authoritative change scope,
   including committed, uncommitted, added, deleted and renamed files. Reuse
   Workrr's attributable-diff machinery rather than assuming `git diff HEAD`.
3. Parse and resolve all annotations in changed Python files. Scan any unchanged
   owners named by obligations. Track complete scan coverage.
4. Evaluate required placements against bound owner spans, exact selectors and
   cardinalities. An inherited module marker cannot satisfy a function placement.
5. Compare baseline bindings for changed/deleted owners. Removal is permitted
   only with an accepted retirement, scope change or independently verified
   relocation. A Git rename heuristic alone does not establish relocation.
6. Evaluate flexible-owner responsibility and suspicious placement with the
   existing independent review machinery. Supply the statement, actual owner
   source, relevant callers, accepted scope, and baseline/candidate binding data.
   Require structured `satisfied`, `violated`, or `needs_review` results with
   evidence locations. No editing tools for this review. Unavailable or ambiguous
   review yields `needs_review`, which blocks acceptance of that obligation.
7. Run existing behavior and preservation verification separately. An annotation
   or passing placement check cannot substitute for a test or other required
   behavioral evidence.
8. Recompute from candidate bytes. Compare worker completion claims, but never
   use them as the source of truth. Missing claims are diagnostic; actual missing
   required placements fail regardless of claims.

Finding codes: `malformed_annotation`, `unresolved_reference`,
`inactive_reference`, `ambiguous_reference`, `dangling_annotation`,
`duplicate_annotation`, `invalid_scope`, `missing_required_placement`,
`wrong_owner`, `missing_preserved_reference`, `scope_resolution_failed`,
`stale_catalog`, `incomplete_context`, and `semantic_review_required`.
Findings include path/line/owner when available, ID, obligation, severity,
expected responsibility, observed condition and actionable correction.

Example:

```text
missing_required_placement: payments/retry.py::schedule_retry
Required: invariant:payment-idempotency on this function.
Observed: the ID appears only in a module annotation.
Correction: attach the reference to the declaration owning retry behavior.
```

Hard failures include unknown IDs newly introduced, invalid annotations in changed
files, required missing placements, unauthorized reference removal, stale inputs,
and unresolved semantic review. Unchanged legacy problems discovered incidentally
are reported separately and do not force unrelated backfills. Existing invalid
references in changed owners must be addressed. A transient read parse error is
reported to the worker; final Python parse failure is a validation failure.
Context overflow resolved by pagination is not itself a failed implementation;
an incomplete required retrieval remains visible in telemetry and review.

## 9. Implementation sequence (complete)

The implementation followed this order; all seven steps are complete and their
current code ownership and regression coverage are summarized at the top of this
document. The detailed acceptance criteria remain here as the record of intended
behavior and verification.

1. **Parser and scope index.** Implemented `source_references.py` and tests; the
   scope and false-positive cases pass without model calls.
2. **Frozen catalog adapters.** Implemented `reference_catalog.py`, canonical kind
   adapters, lifecycle lookup and round-trip/digest tests; accepted authority is
   distinguished from candidate edits.
3. **Reader and search.** Implemented `source_reader.py` and fixture-based CLI
   tests; body-only reads supply parent context and stale indexes refresh.
4. **Environment integration.** Implemented `minisweagent_environment.py` and
   configured miniSWE entry paths in `coding_agent.py` and
   `packages/agent/src/powdrr_agent/execution/minisweagent_session.py`. Fake-model
   trajectories verify context delivery through the actual formatter and
   preserve baseline command/submission behavior.
5. **Handoff.** Implemented `source_reference_handoff.py`, placement compilation,
   prompt rendering and versioned request serialization, retaining legacy reads
   with references disabled. The boundary design documents the shared obligations;
   prompt and validator inputs share fingerprints without exposing private manifests.
6. **Validation.** Implemented `reference_validation.py`, baseline preservation
   comparison, structured semantic-review integration and validation-profile
   wiring; wrong-owner ID stuffing fails and legitimate relocation passes.
7. **Worker orchestration and rollout.** Integrated immutable catalog creation,
   trusted configuration, artifact recording and terminal results in Workrr, with
   offline end-to-end coverage and an opt-in live-provider smoke test.

Add `source_reference_mode` with values `off`, `observe`, `enforce`; default `off`
for legacy requests lacking a catalog. `observe` enriches reads and records
findings without gating acceptance. `enforce` requires valid frozen inputs and
all required reference validations. New requests declaring mandatory placement
obligations must use `enforce`; they cannot downgrade to `observe` or `off`.
The mode is recorded in immutable attempt metadata. Newly compiled
reference-bearing tasks use enforcement after offline acceptance passes. There
is no silent fallback to a plain environment if reference initialization fails
in enforce mode.

## 10. Required verification matrix

Use tests named `test_structrr_source_references.py`,
`test_structrr_reference_catalog.py`, `test_structrr_source_reader.py`,
`test_minisweagent_environment.py`, `test_source_reference_handoff.py`, and
`test_structrr_reference_validation.py`. Extend existing miniSWE provider/session
and worker tests rather than building another orchestration harness.

| Area | Required cases |
| --- | --- |
| Parsing | Real comments; marker in string/docstring ignored; malformed/oversized ID; CRLF; Unicode source; duplicate and dangling markers |
| Scope | Decorators; async/nested functions; nested classes; module docstring; block/simple statement; blank-line detachment; duplicate qualified names |
| Resolution | All supported kinds; conflicting IDs; exact case; accepted versus unaccepted overlay; retired/superseded ID; explicit authorized amendment |
| Reading | Body-only slice inherits context; multiple scopes; edit refreshes digest; invalid Python; no annotations; empty file; unsafe path and escaping symlink |
| Bounds | Long source; context overflow; oversized single statement; pagination; explicit incomplete status; deterministic ordering |
| Environment | `cat`, `sed`, `rg` visible markers; omitted marker honestly unenriched; reader envelope spoof; failed command; timeout; sentinel; formatter truncation |
| Entry paths | Normal CLI; task-file launch; existing session loader; container import/configuration; explicit mode with no valid catalog |
| Handoff | IDs and exact requirements present; shared fingerprint; no invented IDs; no private manifest; stable eight-section prompt structure |
| Validation | Correct owner passes; top-of-file stuffing fails; missing ID; wrong owner; unauthorized deletion; accepted relocation/retirement; committed candidate edits |
| Evidence | Annotation present but behavior broken fails; behavior passes but required annotation absent fails; unavailable semantic review cannot pass |
| Isolation | Candidate catalog edits cannot change authority; post-run digest mutation fails; frozen validation inputs unreadable to worker |

Offline end-to-end fixture: baseline contains an annotated retry function. A
scripted fake model reads only its body through the reader, observes the exact
invariant, adds a helper and relocates a required decision reference correctly,
then submits. Assert source, context, catalog revision and final validation in
the trajectory/report. A paired fixture puts every reference on an unrelated
function and must fail despite including all IDs. Neither fixture calls a paid
provider. The optional live test checks real model compliance; deterministic
tests remain the acceptance gate.

Run the complete repository suite before pushing, using the shared environment
and the current worktree's source:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"
export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"
export UV_NO_SYNC=1
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff format --check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy src tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m powdrr_lift.cli validate-procedrr-processes docs/procedrr/processes
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest -n auto --cov=powdrr_lift --cov-branch --cov-report=term-missing --cov-report=xml tests
rtk proxy ./scripts/run-deterministic-workflow-suite.sh
```

## 11. Completion criteria

The feature is complete when source owns discoverable stable references; the
reader returns exact applicable intent for partial source reads; every miniSWE
entry path can use automatic enrichment; accepted tasks require appropriate
annotations; independent validation rejects misplaced or missing references;
behavioral verification remains mandatory; and artifacts distinguish mechanical
coverage, semantic review, evidence and read-context completeness.

Bootstrap annotation and subsequent validation must also reconcile Structrr's
source bindings with code annotations using section 12. Link correction decisions
are narrowly scoped and their artifact edits are deterministic.

Do not claim universal interception of arbitrary shell reads, support for
unimplemented languages, or semantic correctness established by ID presence.
Those claims are outside this implementation's guarantees.

## 12. Bootstrap annotation and bidirectional link reconciliation (complete)

### 12.1. One logical relationship, two artifact representations

Structrr source bindings and source comments represent the same applicability
relationship. Validation must check both directions. Neither representation wins
merely because it exists, and an inconsistent pair must not be resolved by
silently copying one side to the other.

Normalize a logical edge to `(canonical_reference_id, source_owner, relationship)`.
The owner includes repository-relative path, language, owner kind, qualified name
and a disambiguator when necessary. Resolve it against current parsed source;
line ranges are derived navigation data. The relationship comes from the accepted
placement obligation or the registered kind/owner mapping in section 7. If that
mapping is ambiguous, report an unresolved relationship rather than infer one
from the ID's spelling. Comments need not duplicate the relationship field.

Distinguish these records:

- Semantic Structrr bindings asserting applicability of a canonical feature,
  decision, invariant or assumption: these require reciprocal source annotations.
- Structural bindings such as file membership and other generated inventory:
  these do not automatically require comments.

Define a versioned eligibility mapping for existing binding kinds. Unknown kinds
are reported as unsupported; do not annotate every bootstrap binding indiscriminately.
Current bootstrap name matches are candidate evidence, even when labeled high
confidence. They are not proof that the semantic link should exist.

Let `S` be eligible normalized Structrr edges and `C` the normalized source edges.
The mechanical validator reports `S - C`, `C - S`, invalid endpoints, duplicates,
stale owner locations, lifecycle problems, and relationship ambiguity. Required
coverage is checked separately against the frozen placement obligations: equality
of two empty sets does not establish coverage.

### 12.2. One narrow semantic question per candidate edge

For a mismatch or a newly inferred bootstrap binding, prepare one bounded
provider-neutral decision. Jev is the preferred configured provider; an alternate
implements the same decision contract. Reuse
`packages/service/src/powdrr_service/planning/semantic_decision.py` for identity, input fingerprints,
provenance and evidence binding. Register a new decision kind
`source_link_existence` with values `exists` and `absent`; use the existing
unresolved-result mechanism for insufficient or conflicting evidence. Update
all decision schema/provider validators and fixtures that enumerate decision kinds.
Do not overload a broader candidate-search or implementation-review decision.

The entire question is:

> Under the supplied accepted intent and source context, should this exact
> relationship exist between this Structrr reference and this code owner?

The input packet contains:

- The exact edge identity and relationship definition.
- The canonical statement, relevant rationale, provenance, authority, version,
  lifecycle and accepted amendments.
- The exact owner's source, signature, containing scopes and source fingerprint.
- Relevant caller/test excerpts or other evidence needed to decide ownership.
- Both observed artifact representations, marking which side is missing or
  inconsistent, and whether the proposed edge came from name matching.
- Applicable mandatory placement/preservation obligations.

Context assembly is deterministic and records every included excerpt and digest.
If bounded context is insufficient, the provider abstains; orchestration may
assemble additional evidence and issue a new fingerprinted decision. Never turn
context truncation into an unsupported yes/no answer. Do not treat existing
comments and generated bindings as independent evidence confirming each other.

The provider returns only the allowed value or a structured unresolved reason.
Powdrr records evidence references and provenance. The provider does not return
patches, rewritten statements, replacement IDs, alternative owners, code changes
or instructions for repairing artifacts. A relocation is two independently
identified candidate edges; it cannot be invented inside the answer for one edge.
Candidate discovery supplies those targets before existence decisions are asked.

Deterministic facts need no provider call: missing IDs, unparseable source,
duplicate serialization and stale spans are mechanical findings. Unresolvable
endpoints block an existence question until the endpoint can be identified.
Previously accepted decisions may be reused only when their full input fingerprint
and policy/provider revisions match. Do not repeatedly ask until a desired answer
appears. If Jev or the configured primary provider returns unresolved,
orchestration must ask the configured LLM once before reporting a final unresolved
link. The LLM receives the same exact edge-existence question, the full bounded
evidence packet and the primary provider's unresolved reason. Deterministically
assemble any additional available evidence requested by that reason and record
the resulting input fingerprint. The LLM has the same `exists`, `absent` or
unresolved response contract and no artifact-editing tools.

An `exists` or `absent` LLM answer drives the same deterministic correction table.
If the LLM also returns unresolved, report the case with both attempts, evidence
references and the remaining reason. Preserve provider/model revisions and input
fingerprints for both stages; reuse a result only with matching stage inputs.
If the primary provider is itself an LLM and returns unresolved, still run this
explicit fallback pass once with its unresolved reason; never recurse through
additional fallback passes. An unavailable LLM or invalid response is recorded
as a failed fallback attempt and reported unresolved, never treated as a decision.
An enforce-mode run must configure this LLM fallback before starting. Fallback
addresses an unresolved result and cannot override a resolved accepted decision.

### 12.3. Deterministic correction table

For a valid, current decision, compile these operations mechanically:

| Decision | Structrr operation | Source operation |
| --- | --- | --- |
| `exists` | Ensure exactly one eligible binding for the edge; refresh derived owner/span metadata | Ensure exactly one correctly scoped annotation on the owner |
| `absent` | Remove the edge's applicability binding, retaining the decision in audit history | Remove only the annotation representing this edge |
| primary unresolved | Record the primary attempt and ask the LLM fallback | Preserve artifacts pending the fallback decision |
| fallback unresolved or failed | Report the unresolved case with both attempts | Preserve the artifacts and block completion of affected required links |

Remove neither the canonical decision/invariant nor the implementation when a
link is absent. If the result conflicts with a mandatory accepted placement,
record the result and reconcile the link as specified, but keep the obligation
unsatisfied and the run blocked. A separate design amendment or implementation
change must resolve that conflict. An existence decision cannot waive an accepted
requirement or certify behavioral compliance.

An absent edge may share a physical comment with another valid normalized edge.
Remove the comment only when no retained edge needs it. Treat ambiguous comment
projection as unresolved. Existing correct reciprocal links require no existence
call unless their ownership/intent context changed or they were explicitly
flagged for semantic reassessment. Mechanical consistency is not evidence of
semantic correctness; changed owners still receive the checks in section 8.

Implement comment insertion/removal through tokens and syntax-tree ownership,
not an LLM editor. Insert above decorators using the section 3 grammar. Preserve
unrelated comments, indentation, encoding and newline conventions. Compare Python
ASTs before/after, excluding location attributes, to prove product code did not
change. Existing correct annotations are no-ops; a second run produces no diff.

### 12.4. Apply as a checked artifact transaction

Edge normalization, mismatch detection, decision preparation, and operation
compilation are implemented in
`packages/service/src/powdrr_service/structrr/reference_reconciliation.py`.
Deterministic source edits and staged artifact application are implemented in
`packages/service/src/powdrr_service/structrr/reference_editing.py`; provider
invocation remains in orchestration. Earlier sections' legacy
`src/powdrr_lift/structrr/` paths refer to these service-owned responsibilities.

Persist `structrr-link-reconciliation-v1` with source/document/base digests,
eligibility-policy version, candidate edges, decisions and their fingerprints,
compiled operations, unresolved cases, before/after digests and validation receipt.
Operation types are `ensure_binding`, `remove_binding`, `ensure_annotation` and
`remove_annotation`; operations name one exact edge and expected input digests.

Before writing, recheck every source and Structrr digest. A stale input invalidates
the affected decision and operation plan; do not apply its old line offsets to
new source. Stage the entire linked change set in an isolated worktree, validate
all candidate artifacts, then publish the document and code together in one PR.
Use temporary files and atomic replacement per file; multi-file replacement is
not globally atomic. Retain a transaction receipt and recoverable preimages so
interruption cannot be reported as successful partial application.

Reparse annotated files and regenerate source spans/fingerprints after edits.
Regeneration must retain reconciled bindings rather than rerun name heuristics
that resurrect rejected edges. Comments inserted by this phase are projections,
not new independent evidence for a later bootstrap. Preserve negative decisions
for the same fingerprint so the pipeline does not repeatedly propose rejected
links. Re-evaluate when relevant intent, source or policy changes.

Completion requires zero unresolved affected mismatches, no duplicate eligible
edges, reciprocal representations, unchanged Python ASTs and satisfied independent
placement/behavior obligations. Finish with a fresh read-only consistency scan;
do not recursively trigger more semantic decisions during that scan.

### 12.5. Bootstrap and later validation entry points (implemented)

Implemented CLI surface:

```text
powdrr-lift bootstrap-structrr --annotate-source
powdrr-lift structrr references reconcile --document DOCUMENT --plan
powdrr-lift structrr references reconcile --document DOCUMENT --apply PLAN
powdrr-lift structrr references check --document DOCUMENT
```

Bootstrap discovers source subjects and validates/stabilizes canonical IDs,
then plans candidate links, asks narrow existence decisions, applies deterministic
edits, re-indexes, and validates the result. The standalone command runs the same
reconciliation phase on an existing document. `check` reports findings without
mutations or provider calls. Reconciliation is explicit, auditable, and repeatable.

Later implementation validation detects mismatches after miniSWE returns and
applies accepted annotation corrections through Workrr before independent
validation, without another coding-agent invocation or repair prompt. The
correction receipt is separately attributable. Workrr rechecks the source digest,
preserves the worker's product-code AST, and leaves frozen catalog and private
validation inputs unchanged. A source-only link that would require a new accepted
placement blocks for a reviewed design amendment; reconciliation cannot create or
alter accepted intent.

The section 9 modes are enforced: `observe` may propose decisions and operations but
does not mutate artifacts; `enforce` may apply validated annotation/binding
corrections and blocks on unresolved required inconsistencies. Correction is
limited to applicability metadata; canonical intent amendments remain separate.

### 12.6. Implementation order and acceptance cases (complete)

After the section 9 parser/catalog work, edge normalization and the read-only
consistency checker were added, followed by the narrow semantic-decision adapter,
deterministic operation compilation, token editing, bootstrap integration,
existing-document backfill, and post-worker validation. Provider behavior is
covered by fake-provider tests; live provider tests remain opt-in.

Required cases: Structrr-only and source-only links for both `exists` and
`absent`; duplicate links; missing endpoints; unsupported binding kinds;
unresolved/conflicting evidence; primary-unresolved to LLM-`exists` and
LLM-`absent`; both stages unresolved; unavailable/invalid LLM; exactly one fallback
pass even when the primary is an LLM; retained provenance for both stages;
stage-specific stale decision fingerprints; same-input reuse;
shared annotation retention; mandatory-obligation conflict; relocation candidates;
decorated/nested owners; token-preserving removal; interrupted application;
repeat-run idempotence; final reciprocal scan; regenerated spans; rejected-edge
non-resurrection; and a post-worker correction that leaves the frozen manifest
and product-code AST unchanged. Include a consistently linked but semantically
wrong edge to prove reciprocal presence alone cannot certify correctness.
