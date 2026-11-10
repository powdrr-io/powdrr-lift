# Structrr as the intended source design

Structrr describes the source code we want and why that design exists. Design
and debugging agents propose changes to that description. Coding agents change
source to satisfy it. Validation reports agreement or concrete discrepancies.

This document describes the implemented `structrr-desired-state-v1`,
`structrr-design-handoff-v1`, and `structrr-design-agreement-v1` contracts. It
narrows the broader context-language specification to a usable handoff boundary.
Deployment, process scheduling, and model selection remain outside this language.

## Meaning and authority

There are three separate objects:

1. The accepted desired state at a revision.
2. A proposed change and its projected desired state.
3. Observations of candidate source and the evidence for agreement.

Only an explicit design change changes intended meaning. Source extraction,
passing tests, and exhausted coding retries do not amend the design. Accepting a
different implementation requires a new proposal. A proposal's fingerprint and
all evidence bound to it change when its meaning or rationale changes.

Importing an existing snapshot is an explicit adoption operation. It preserves
design sections, including intent, and excludes source manifests, command
results, and runtime state from the desired-state identity. Adoption does not
establish that inferred structure was deliberately designed. Missing rationale
remains missing; a compiler does not invent historical reasons.

The legacy bootstrap and changelog formats remain adapters. Current-state
reporting still supports existing repositories. The canonical desired-state
artifact provides a complete baseline for subsequent portable handoffs, while
the existing source inventory remains observational.

## State and changes

`packages/service/src/powdrr_service/structrr/design.py` owns the shared `project_sections` operation. Multi-PR
structural planning uses it as well as the portable handoff compiler.

Design records have stable identities within their section. Supported sections
include entities, relationships, source subjects and bindings, requirements,
decisions, invariants, guidance, features, modules, and verification declarations.
Every field within a record is retained, including rationale, provenance,
supersession references, and verification expectations. Collections are sorted by
identity; dictionary formatting does not affect fingerprints. State objects use
immutable serialized backing and return detached documents.

The change rules are:

| Operation | Preconditions | Result |
| --- | --- | --- |
| Add | Subject absent | Install the declared record. |
| Change | Subject present | Merge explicit fields; omitted fields survive. Null explicitly clears a field. |
| Remove | Subject present | Remove the subject from active state. |
| Supersede | Explicit removal plus addition | Retire the old subject and retain `supersedes` on its replacement. |

Duplicate identities and duplicate operations on one subject in one change are
errors. Unknown fields at the state boundary and unknown actions are errors.
Legacy `altered` and `modified` normalize to `change`;
`deleted` normalizes to `remove`. Historical removals cannot be imported as active
records. Relationship endpoints must exist in the resulting desired state.

The compatibility `files` operation addresses `source_subjects`; stable keys take
precedence over generated display IDs. The older execution-unit field
`planned_additions` carries both additions and modifications, preserving each
record's action. Worker intent distinguishes modifications from additions.

Rationale is design meaning, not a fact source extraction must reproduce. The
structural comparator excludes rationale, provenance, and verification metadata
when comparing observed values. Structural planning retains both the enduring
`rationale` and the operation's `change_reason`. Changelog parsing and reconstructed
invariants, guidance, and decisions preserve rationale.

## Portable handoff

A handoff contains the original baseline, its canonical design, the proposal,
the projected desired state, and an explicit repository base commit. All contents
are fingerprinted. Loading replays the proposal and rejects stale baselines,
tampered fields, and mismatched projected state. No conversation or mutable
workflow state is required.

Compile a handoff from an existing baseline and proposal revision:

```sh
powdrr-lift compile-structrr-handoff \
  --baseline accepted-design.json \
  --proposal proposal-revision.json \
  --base-commit "$BASE_COMMIT" \
  --validation-profile pytest \
  --allowed-command 'python -m pytest' \
  --output /tmp/retry-design.json
```

The compiler writes the handoff, `.desired.json`, `.request.json`, and `.prompt.md`
siblings. The desired sibling is the *proposed result*, not an acceptance receipt.
After explicit design acceptance it can serve as the next baseline. A raw legacy
design diff can also be supplied; use repeatable `--allowed-path` arguments to
declare its implementation scope. Precompiled proposals retain their frozen scope.

`ImplementationRequest.from_structrr_handoff` renders the complete desired design,
changes, rationale, preservation requirements, acceptance criteria, non-goals,
scope, and provenance into the existing coding-worker request format. The request
can be supplied to `run-coding-agent` in a dedicated worktree. The existing runner
enforces its base commit and allowed scope. Ordinary feature execution also
persists `structrr-design-handoff.json` during proposal review; its bounded task
prompts continue to use the existing context projections.

The complete portable prompt is intentionally unpruned. Large adopted repositories
can exceed a provider's prompt limit. Existing runner limits apply; the compiler
does not silently drop required design to fit. Context selection is separate work.

## Agreement and discrepancies

Every evaluated subject has one of three outcomes:

- `satisfied`: current evidence establishes agreement.
- `contradicted`: current evidence establishes a design violation.
- `unknown`: evidence is missing, stale, ambiguous, unsupported, or incomplete.

Required subjects must be satisfied. An explicit `required: false` makes a subject
advisory while retaining its finding. Requirements, invariants, features,
structural subjects, active intent, and identified acceptance/test records require
evidence by default. Decisions and guidance without verification explain the
design; `required: true` or explicit verification also makes them evaluable.
Proposal-level acceptance, preservation, and non-goal statements always require
their own bound evidence, even when they overlap a record.

Records can declare deterministic source checks:

```yaml
invariants:
  - id: retry-storage-boundary
    action: added
    description: Retry dispatch has no direct persistence dependency.
    rationale: Downstream deduplication owns transaction storage.
    verification:
      - kind: python-symbol
        path: src/retry.py
        symbol: Retry.dispatch
      - kind: python-import
        path: src/retry.py
        module: persistence
        present: false
```

Implemented checks are `file-exists`, qualified `python-symbol`, and static
`python-import`, with an optional boolean `present` (default true). Static imports
include nested imports and `from` imports. These checks do not establish dynamic
dependency absence, Python import resolution, or arbitrary semantic equivalence.
Unsupported checks return unknown. Invalid syntax, unreadable source, paths outside
the repository, and missing inventory coverage cannot establish agreement.

Run an agreement check, keeping artifacts outside the candidate source or under
the excluded `.powdrr` directory:

```sh
powdrr-lift check-structrr-design \
  --handoff /tmp/retry-design.json \
  --repo-root /path/to/task-worktree \
  --evidence /tmp/retry-evidence.json \
  --output /tmp/retry-agreement.json
```

The CLI captures tracked and nonignored untracked files. The checker verifies
manifest bytes and executable bits against disk. Its report binds the handoff,
desired state, manifest, and candidate product digest. Exit status is zero only
when every required subject passes and extraction has no errors. Omitting evidence
is useful for obtaining the current digest and outstanding evidence requests.

Behavioral and review evidence comes from a trusted validation adapter in this
form, under a top-level `evidence` array:

```json
{
  "subject": "invariants:stable-transaction-id",
  "handoff_fingerprint": "sha256:...",
  "candidate_product_digest": "sha256:...",
  "status": "satisfied",
  "reason": "Two attempts used the same transaction ID.",
  "evidence_refs": ["pytest:tests/test_retry.py::test_preserves_id"]
}
```

Proposal-level subjects use `proposal:acceptance_criteria:1`,
`proposal:must_preserve:1`, or `proposal:non_goals:1`. Duplicate evidence, unknown
subjects, stale digests, and missing references cannot pass. Supplied evidence
cannot override a failing explicit source check. Fingerprints bind evidence;
they do not prove that an arbitrary evidence producer honestly ran a test. The
caller must trust its validation adapter, as with existing Workrr receipts.

Existing feature execution retains its behavioral probes and semantic reviews.
Its final gate runs explicit source checks against the frozen handoff, alongside
existing behavioral validation. The source-check receipt is labeled
`explicit_source_checks`; standalone complete reports use
`complete_desired_design`. Its final gate preserves required structural discrepancies after correction
exhaustion and includes them in actualization. It cannot declare completion by
removing the operations that remain unsatisfied. The standalone agreement checker
is also usable without that workflow; it does not install a new accepted design
or authorize publication.

## Verification

Tests cover complete semantic transitions, rationale changes, immutable state,
tampering, duplicate/missing subjects, independent-process request compilation,
static architecture violations and repairs, behavioral evidence freshness,
unsupported/unsafe checks, and the CLI handoff-to-agreement path. Existing feature
flow tests cover the stricter final gate. A live coding-model success rate is not
established by these deterministic tests.
