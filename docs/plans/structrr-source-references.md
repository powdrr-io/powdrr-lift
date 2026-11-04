# Structrr references in source and automatic miniSWE context

Status: implementation plan. This document specifies new behavior; the modules
marked **new** below do not exist yet. It does not implement that behavior.

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
them. No repository-wide annotation backfill is required.

## 2. Boundaries and existing implementation

Read these files before changing code:

| Existing file | Relevant responsibility |
| --- | --- |
| `src/powdrr_lift/workrr/coding_agent.py` | `ImplementationRequest`, prompt rendering, `MiniSWEAgentProvider.run`, configuration and trajectory handling |
| `src/powdrr_lift/minisweagent_session.py` | Large-task and continuation entry point; constructs environment through `get_environment` |
| `src/powdrr_lift/structrr/intent.py` | Canonical intent sources, clauses, versions, source wording, supersession |
| `src/powdrr_lift/structrr/active_intent.py` | `resolve_active_intent`, `ActiveIntentReference`, baseline and feature-overlay semantics |
| `src/powdrr_lift/structrr/source_manifest.py` | Source fingerprints and comparisons |
| `src/powdrr_lift/workrr/coding_agent_validation.py` | Independent attempt validation and reports |
| `src/powdrr_lift/workrr/task_agent.py` | Current worker orchestration and inherited-intent checks |
| `docs/design/external-coding-agent-boundary.md` | Target single-prompt execution and private validation manifest |
| `docs/design/source-anchored-semantic-contract-compilation.md` | Target design-to-implementation compilation |

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
Update that boundary document when implementing the handoff integration.

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

## 4. Canonical catalog and lifecycle

Add **new** `src/powdrr_lift/structrr/source_references.py` for immutable models,
annotation parsing, scope binding and resolution. Add **new**
`src/powdrr_lift/structrr/reference_catalog.py` for the frozen worker projection.
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
intent store. Adapt existing active clauses using `clause_id` as their ID.
Add explicit adapters for canonical feature, decision and assumption records in
the accepted design data. Do not pretend `IntentKind` already supports features
and assumptions: it currently lists decision, invariant, procedure and guidance.
If a required kind has no canonical representation, add that representation and
round-trip validation before enabling its adapter. Never infer an assumption or
accepted human decision from code comments.

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

## 5. Source reader with automatic context

Add **new** `src/powdrr_lift/structrr/source_reader.py`, callable as:

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

## 6. miniSWE environment interception and coverage limits

Add **new** `src/powdrr_lift/minisweagent_environment.py` containing
`StructrrLocalEnvironment`, based on miniSWE's `LocalEnvironment`. Configure:

```text
environment.environment_class=powdrr_lift.minisweagent_environment.StructrrLocalEnvironment
```

Pass workspace, frozen catalog and event-log paths through validated environment
configuration fields. Preserve normal miniSWE environment options. Extend the
configuration model explicitly rather than passing unknown fields to the base.
Apply it in both CLI launch and `minisweagent_session.py` entry paths, including
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

## 7. Making miniSWE write the references

Add **new** `src/powdrr_lift/workrr/source_reference_handoff.py`. Compile placement
obligations from the accepted design, atomically with the worker prompt and the
private validation data. Do not derive mandatory IDs by guessing from a diff.
The same canonical obligation set must drive prompt rendering and validation.

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

## 8. Independent validation and preservation

Add **new** `src/powdrr_lift/structrr/reference_validation.py`. Expose a read-only
`check` command through the new source-reader module, with catalog and obligation
inputs supplied by trusted run configuration. Wire its result into the existing
validation report; references are a separate category from behavioral evidence.

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

## 9. Implementation sequence

Implement in this order; each step includes its tests before the next integration.
Use feature branches/worktrees and PRs, following repository instructions.

1. **Parser and scope index.** Add `source_references.py` and tests. Exit when
   all scope and false-positive cases in section 10 pass without model calls.
2. **Frozen catalog adapters.** Add `reference_catalog.py`, canonical kind adapters,
   lifecycle lookup and round-trip/digest tests. Exit when accepted authority and
   authorized amendments cannot be confused with candidate edits.
3. **Reader and search.** Add `source_reader.py` and fixture-based CLI tests.
   Exit when a body-only read supplies parent context and stale indexes refresh.
4. **Environment integration.** Add `minisweagent_environment.py`; configure every
   miniSWE entry path in `coding_agent.py` and `minisweagent_session.py`. Exit when
   fake-model trajectories show context delivered through the actual formatter
   and baseline command/submission behavior remains intact.
5. **Handoff.** Add `source_reference_handoff.py`, placement compilation and prompt
   rendering. Extend versioned request serialization explicitly, retaining legacy
   reads with references disabled. Update boundary design docs. Exit when prompt
   and validator obligations share fingerprints and no private manifest leaks.
6. **Validation.** Add `reference_validation.py`, baseline preservation comparison,
   structured semantic-review integration and validation-profile wiring. Exit
   when wrong-owner ID stuffing fails and legitimate relocation passes.
7. **Worker orchestration and rollout.** Integrate immutable catalog creation,
   trusted configuration, artifact recording and terminal results in Workrr.
   Add offline end-to-end coverage, then an opt-in live-provider smoke test.

Add `source_reference_mode` with values `off`, `observe`, `enforce`; default `off`
for legacy requests lacking a catalog. `observe` enriches reads and records
findings without gating acceptance. `enforce` requires valid frozen inputs and
all required reference validations. New requests declaring mandatory placement
obligations must use `enforce`; they cannot downgrade to `observe` or `off`.
Record the mode in immutable attempt metadata. Enable enforcement for newly
compiled reference-bearing tasks after offline acceptance passes. No silent
fallback to a plain environment if reference initialization fails in enforce mode.

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

Do not claim universal interception of arbitrary shell reads, support for
unimplemented languages, or semantic correctness established by ID presence.
Those claims are outside this implementation's guarantees.
