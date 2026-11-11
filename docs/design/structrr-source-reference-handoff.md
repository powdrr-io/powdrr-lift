# Structrr placement handoffs

The service-owned `structrr/source_reference_handoff.py` compiles explicit source
placement intent into one public prompt and one private validation manifest.
Compatibility exports live in `powdrr_lift.workrr.source_reference_handoff`.
The compiler reads no repository files, candidate diffs, transcripts, or inferred
ID counts. Its caller establishes that the supplied design is accepted and that
any catalog amendments are authorized. The catalog's accepted-document digest
must match the exact supplied document.

## Accepted design records

Desired state now preserves `source_reference_placements` and
`source_reference_preservations`. Proposal operations identify placements by
`obligation_id`; preservation records use `id`. These are ordinary desired-state
records, so projection and revision fingerprints retain them.

```yaml
source_reference_placements:
  - obligation_id: placement:retry
    ref: invariant:retry-key
    relationship: preserves
    requirement: Keep the original key.
    owner:
      path: payments/retry.py
      kind: function
      qualified_name: retry
    cardinality: exactly_one
    verification_refs: [case:retry-key]
```

Requirements retain the exact canonical statement. Only catalog lifecycle
`active` can supply a mandatory placement. Relationships map `implements` to
feature, `realizes` to decision, `preserves` to invariant, and `relies_on` to
assumption. `checks` requires an explicit test owner (`role: test`, function or
async function); later validation must establish collected test ownership.
Reference prefixes carry no meaning.

Exact selectors default to `binding: exact` and `role: implementation`. Supported
kinds match the Python index: module, class, function, async_function, and block.
Paths must be normalized literal repository-relative Python paths inside the
allowed edit scope. Multiple required owners use distinct obligation IDs.

Where implementation layout is intentionally free, use
`binding: requires_semantic_review`, `allowed_paths`, `kind`, `responsibility`,
and optional `role` instead of an exact path and qualified name. Such obligations
require `cardinality: one_or_more`. Worker owner claims remain unverified until
an independent reviewer binds them; the compiler emits no passing receipt.

Preservation records have exactly `id`, `ref`, `owner`, `disposition`, `reason`,
`successor`, and `destination`. Nullable fields are explicit. Baseline owners
are exact selectors. `preserve` keeps that binding; `remove` authorizes its
removal; `relocate` requires a destination; `supersede` requires a distinct active
successor agreeing with canonical lifecycle history. Changing the baseline
binding requires its source path in edit scope. Unchanged preserved owners can
be outside that scope. Duplicate/conflicting baseline rules and removal of a
simultaneously required binding fail compilation. A preservation-only handoff is
valid. General preservation of existing applicable references remains part of
the worker instruction and the later baseline comparison.

## Atomic artifacts and privacy

`compile_source_reference_handoff` accepts the document, frozen catalog,
objective, allowed paths, focused verification argument vectors, an optional
local reference-check argument vector, and optional private validation data.
It returns immutable JSON-backed `CompiledSourceReferenceHandoff`.

The public `worker_prompt` is `minisweagent-source-reference-prompt-v1`; the private
artifact is `source-reference-validation-manifest-v1`. Both contain the exact
same canonical contract: base commit, desired-state fingerprint, accepted
document digest, catalog digest, placements, preservation rules, referenced
definitions, scope, objective, and commands. They share a contract fingerprint;
the public artifact additionally binds a digest of the private validation inputs,
without their contents. Changing private inputs requires a new prompt artifact
fingerprint even when the rendered prompt bytes remain identical. The manifest
binds the complete prompt artifact fingerprint. The bundle
fingerprint binds both. Round-trip parsing recomputes normalization, rendering,
parity, and fingerprints. `require_matches` compares a worker artifact against
the frozen private bundle. Hash consistency alone never establishes authority.

Only `worker_prompt` belongs in a worker request. Private validation data is
namespaced exclusively in the manifest. The compiler never renders the whole
accepted document, proposal operations, private oracle contents, or manifest
locations into the prompt. Worker JSON is a public projection, not the bundle.
The separate schema names identify a placement-focused projection; they do not
claim the full behavioral obligation compiler described by the boundary design.

The rendered prompt retains exactly eight sections: Objective, Repository
starting point, Required behavior, Required verification, Preserve and avoid,
Allowed scope, Focused commands, and Completion protocol. Each placement appears
once with its exact statement and selector. Reference metadata retains rationale
and lifecycle. The plan's worker instruction is included verbatim. Commands are
argument vectors, with no shell evaluation by the compiler. Completion requests
reference-to-owner claims and design gaps in the existing submission, without
creating another repository artifact. Rendering above 64,000 characters fails
explicitly rather than dropping requirements or splitting the handoff.

## Request compatibility and rollout

`ImplementationRequest.from_source_reference_handoff` creates
`implementation-request-v3` with the public artifact and explicit `enforce` mode.
The request must match its prompt, objective, base, design fingerprint, and path
scope. Downgrading the mode or schema, changing the prompt, widening paths, or
constructing a repair prompt fails. Legacy v1/v2 requests remain readable with
references disabled; they cannot carry new reference fields. New non-reference
requests use explicit `off`. The legacy Structrr handoff adapter rejects designs
containing reference placement/preservation records, preventing silent bypass.

Reference-bearing requests are deliberately rejected before provider invocation
until placement validation and worker rollout are integrated. The common runner
returns policy denied, and direct miniSWE/OpenCode adapters also reject them.
The optional local check is rendered only when supplied; otherwise the prompt
explicitly reports that the check is unavailable and cannot be claimed passing.
Supplying a command is not evidence that its validator exists or has passed.

Environment integration is tracked separately in
[PR #1418](https://github.com/powdrr-io/powdrr-lift/pull/1418), which was still open
when this implementation began. Step 6 supplies independent placement and
preservation validation; step 7 binds immutable worker inputs and enables rollout.
