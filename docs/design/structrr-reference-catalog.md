# Frozen Structrr reference catalog

Step 2 of the [source references plan](../plans/completed/structrr-source-references.md)
implements a pure, immutable worker-context projection. It does not read source
files, call providers, install accepted state, or infer meaning from annotations.

## Canonical definitions

`packages/service/src/powdrr_service/structrr/reference_definitions.py` defines
`ReferenceDefinition` and the `structrr-reference-definition-v1` schema. The model
supplies canonical feature and assumption representations as well as decision,
invariant, procedure, and guidance records. It does not extend the runtime's
`IntentKind` enum or create a second writable intent store.

Each record contains:

- An opaque, case-sensitive `id` matching the annotation grammar.
- Explicit `kind`, exact `statement`, and `source_ref` provenance.
- A positive integer `version` and explicit `lifecycle`.
- Optional `rationale` and `successor`.
- Immutable `evidence_refs` and assumption-only `invalidation_triggers`.

`to_data()` emits every field and the schema version; `from_data()` validates the
complete record, rejecting unknown fields and unsupported revisions. Empty text,
invalid IDs, boolean versions, and conflicting successor/lifecycle combinations
are errors. A source ID's spelling never determines its kind.

Canonical records live in the accepted document's `reference_definitions` array.
The existing desired-state model preserves that section and `assumptions`, so
definitions can survive design-state serialization and subsequent handoffs.
Desired-state loading validates canonical definitions. Active canonical features
and invariants require current evidence in the complete design-agreement check,
just like their legacy sections; inactive historical definitions do not impose
current implementation obligations.

```python
from powdrr_service.structrr.reference_definitions import ReferenceDefinition

assumption = ReferenceDefinition(
    id="storage-deduplication",
    kind="assumption",
    statement="Storage deduplicates requests by transaction ID.",
    source_ref="spec:retry-design",
    rationale="Retries reuse the original transaction identity.",
    invalidation_triggers=("The storage adapter's deduplication contract changes.",),
    evidence_refs=("test:storage-idempotency",),
)
accepted_document = {"reference_definitions": [assumption.to_data()]}
```

## Compilation and compatibility adapters

`compile_reference_catalog(accepted_document, base_commit=...)` returns a frozen
`ReferenceCatalog`. The base must be a full lowercase SHA-1 or SHA-256 Git commit
ID, not a mutable branch name. The accepted document's entire JSON content is
fingerprinted with stable dictionary ordering. Entry ordering is canonical by
ID; source array order remains part of the accepted document's identity.
Serialized `structrr-desired-state-v1` inputs are validated and unwrapped before
projection; their entire serialized input remains the accepted-document digest.

The compiler supports these explicit adapters:

| Input | Canonical meaning |
| --- | --- |
| `reference_definitions` | Strict versioned canonical definitions. |
| `active_intent` | Existing clauses, identified by `clause_id`; active entries pass through `resolve_active_intent` without a feature overlay. |
| `invariants`, `guidance` | Legacy fallback when no active inventory exists, plus explicit inactive history even when an active inventory exists. |
| `features` | Feature records with an explicit statement or description. |
| `decisions`, `human-decisions` | Decision records with an explicit statement or summary. |
| `assumptions` | Assumption records with a statement or description and optional invalidation triggers. |

Section records may also use the complete versioned canonical schema; their kind
must agree with their section. Legacy source provenance follows the existing
resolver's `source_ref`, `source`, then `document:<id>` fallback and is bound to
the accepted document digest. Missing rationale is preserved as missing.

A feature's delivery state, such as `implemented`, cannot supply its statement.
Records that lack semantic content fail initialization instead of receiving
generated wording. Unrelated candidate data is never searched for definitions.
The caller must pass accepted design through the accepted-document parameter;
the compiler cannot determine acceptance by inspecting a filename or a claimed
authority field inside arbitrary repository content.

Identical duplicate definitions are deduplicated. Any disagreement in kind,
meaning, version, lifecycle, provenance, or other context for one ID is a conflict.
Inactive legacy clauses are retained independently of the active resolver.
Accepted `supersedes_clause_id` and legacy decision `replaces` links can supply a
historical successor. Legacy decision `status` is retained as lifecycle when
applicable. The predecessor must already be explicitly inactive; the compiler does not silently
retire accepted active intent.

## Authorized amendments

An arbitrary feature overlay is not an authorized amendment. The optional
`authorized_amendment` parameter requires a
`structrr-authorized-reference-amendment-v1` envelope, constructed with
`AuthorizedReferenceAmendment`:

```python
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import (
    AuthorizedReferenceAmendment,
    compile_reference_catalog,
)

# Workflow authorization must be established before constructing this envelope.
amendment = AuthorizedReferenceAmendment(
    authorization_ref="workflow:approved-design-revision-2",
    base_commit=base_commit,
    accepted_document_digest=content_fingerprint(accepted_document),
    definitions=(approved_replacement,),
)
catalog = compile_reference_catalog(
    accepted_document,
    base_commit=base_commit,
    authorized_amendment=amendment.to_data(),
)
```

The envelope binds its authorization provenance, full base commit, exact accepted
document digest, and canonical definitions. Its base and document must match the
catalog inputs. Replacement and retirement records must advance the existing
ID's version; duplicate amendment IDs are errors. New records are permitted.
Retirement changes lifecycle and retains the record for diagnostics.

Unchanged entries retain `accepted` authority. Entries supplied by an amendment
are labeled `authorized_amendment`, and the catalog includes the canonical
amendment digest. The caller is responsible for authenticating workflow approval
and protecting frozen artifacts. An authorization reference and self-hash are
provenance and integrity checks, not cryptographic proof of permission.

## Catalog serialization and resolution

`structrr-reference-catalog-v1` contains the full base commit, accepted document
digest, optional amendment digest, renderer revision, sorted entries, and catalog
digest. Each entry serializes the canonical definition and its compiler-assigned
authority. The current renderer revision is `structrr-reference-renderer-v1`.

`ReferenceCatalog.from_data()` rejects unknown schemas/renderers, malformed
records, unsorted or duplicate IDs, amended entries without an amendment digest,
and digest mismatches. Every serialized context field contributes to the digest.
Models retain only immutable records and tuples; returned dictionaries and lists
are detached copies. Filesystem freezing and post-invocation mutation checks
remain part of the later orchestration step.

`resolve_reference(catalog, ref)` returns a `ReferenceResolution` with exact ID,
status, entry when present, and reason:

| Status | Meaning |
| --- | --- |
| `resolved` | The exact ID identifies an active definition. |
| `inactive_reference` | The definition exists but its lifecycle is proposed, accepted, deprecated, retired, superseded, or rejected. Its successor is reported without implicit redirection. |
| `unresolved_reference` | The ID is absent or violates the ID grammar. |

Only `active` means current applicability. An accepted but not yet activated
record is retained for diagnostics, not silently activated. Later placement
preflight must reject required references whose resolution is not `resolved`.
Resolution records also have validated `to_data()`/`from_data()` round trips.

Catalog, canonical-definition, and authorization schema errors fail initialization
explicitly. Unknown and inactive source IDs produce structured resolution results.
The context-aware reader and worker integration are subsequent implementation
steps; neither is enabled by this catalog change.
