# Independent source reference validation

The service owns `structrr/reference_validation.py`, which recomputes annotation
ownership from supplied baseline and candidate bytes. The local adapter
`powdrr_lift.workrr.source_reference_validation.SourceReferenceValidationContext`
acquires those bytes from Git and the worktree. Compatibility exports are in
`powdrr_lift.structrr.reference_validation`.

This implements step 6 of the source annotations plan. Automatic worker rollout
remains step 7. Reference-bearing implementation requests stay blocked before
provider invocation until that rollout binds the private context and attempt
metadata. Adding this validator does not enable those requests by itself.

## Frozen inputs and coverage

`validation_input_identity(handoff)` supplies the exact base commit, desired-state
fingerprint, catalog digest, contract fingerprint, prompt artifact fingerprint,
and private validation input digest to record before an attempt. The context
constructor requires that recorded mapping and freezes detached handoff/catalog
and input data. The service rejects mismatches, including canonical definitions
that disagree with the trusted catalog. Neither catalog edits in candidate files
nor worker reports establish accepted intent.

Acquisition reuses Workrr's committed-path and working-path collection machinery.
Its Git adapter uses NUL-delimited literal paths, disables rename heuristics,
external diff/textconv, fsmonitor, replacement objects, hooks, and optional locks.
It combines the full base-to-HEAD committed change with index, worktree, and
standard untracked changes. A rename therefore contributes both source paths.
It also scans unchanged exact owners and tracked/standard untracked Python files
under flexible selectors. This is attributable Git scope, not universal filesystem
mediation or a transactional repository snapshot.

Baseline source comes from regular blobs at the exact base commit. Candidate
source uses the reader's safe descriptor-relative bounded byte acquisition.
Escaping/dangling links and unsupported baseline symlinks are explicit acquisition
failures. The policy caps each file at one MiB, total acquired baseline/candidate
bytes at 16 MiB, and the selection at 1,000 files. Incomplete acquisition blocks
acceptance; no source range is silently truncated. Git operations have individual
30-second timeouts. The caller must also budget any configured review client's
network calls, as with existing semantic review clients.

The service hashes and parses the same bytes, records both sides' coverage and
digests (including invalid source bytes), and derives a snapshot digest including
the changed paths. The local adapter reacquires the snapshot after checking and
fails if code, paths, or acquisition coverage changed during review.

## Placement, preservation, and claims

Only directly bound annotations satisfy placements. Ancestor/module inheritance
is useful reading context but cannot satisfy a required function owner. Exact
path/kind/qualified-name selectors must be unambiguous. Cardinalities are checked
per obligation; repeated annotations do not create additional owners. Changed
source is parsed for malformed, dangling, duplicate, invalid-scope, unresolved,
and inactive references. Final candidate parse failures block validation.

Unchanged legacy issues are reported as warnings. For bound references, unchanged
owner source and annotation membership prove that a sibling edit did not change
that owner. Editing the affected owner, adding an invalid reference, or violating
an explicit mandatory placement requires correction. Unbound invalid markers
whose unchanged ownership cannot be established remain errors in changed files.

Every baseline binding in changed/deleted files must survive at its owner or have
an accepted preservation rule. A matching ref elsewhere and a Git rename hint do
not authorize removal. Explicit removal is permitted; supersession must place the
canonical successor; relocation must reach its specified destination and receive
independent semantic review. Ambiguous baseline identities cannot silently collapse
to one binding. Missing baseline bindings named by accepted rules fail explicitly.

Worker completion claims are compared with fresh direct bindings. Missing or false
claims are diagnostic warnings, never the source of truth. Actual missing required
placements fail regardless of what the worker reports.

## Independent review

Flexible owners, relocations, and unplanned new bindings of valid IDs use a bounded
read-only JSON review client and the existing `DecisionSpecification` /
`DecisionResult` evidence API. The request contains the exact canonical definition,
obligation/responsibility, actual owner source and spans, accepted scope,
baseline/candidate bindings, and syntactic callers in the acquired scope. Dynamic
dispatch and missing caller coverage must remain unresolved when material.

The client receives no editing tools or private oracle contents. Responses must be
`satisfied`, `violated`, or `needs_review`, with an explanation and evidence locations
drawn from the supplied owner/snapshot identities. A conclusive flexible binding
must account for every proposed owner. Evidence from different bytes is invalid.
The host binds typed decisions to the request fingerprint and policy revision.

Unavailable, malformed, ambiguous, partial, or oversized review evidence produces
`needs_review`, which blocks acceptance. Review context is capped at 48,000
characters and there are at most 50 review calls/records per validation. Exception
details from a review client are not copied into public findings. These mechanisms
bind evidence and fail closed; they do not assert that a model is an infallible
semantic oracle.

`checks` relationships additionally require trusted collected-test ownership
evidence with the current snapshot digest. A worker's test-like function name or
owner claim cannot substitute for that receipt. Collection is supplied by the
host's test-validation path; the source checker never imports candidate tests.
Without fresh collection evidence, that obligation remains `needs_review`.

## Reports, profiles, and local checks

`structrr-reference-validation-v1` reports `passed`, `failed`, or `needs_review`,
scan completeness, frozen input identities, changed paths, coverage, findings,
and structured reviews. Findings carry codes, severity, path/line/owner, reference
and obligation IDs, expected responsibility, observed condition, and correction.
The immutable serialized backing has a fingerprint for integrity, not authority.
Post-run validation must recompute through the trusted context rather than accept
a worker-supplied report.

`ValidationRunner` accepts a trusted `reference_context` and emits a separate
`structrr-references` result for reference-bearing requests. Missing context,
prompt mismatch, and unresolved review block the report. That profile name is
reserved and cannot be replaced by a caller-supplied passing shell command.
All ordinary required profiles still run. A reference-bearing request without
any other validation profile is blocked, and the source report always states
`behavioral_evidence_satisfied: false`. Existing behavioral verification remains
responsible for the adequacy of those other profiles and their case mappings.
Placement pass cannot mask behavioral failure, or vice versa.

The isolated worker command is:

```text
python -m powdrr_agent.execution.source_reader check
```

It takes no catalog, manifest, selector, or claim overrides. A trusted caller
supplies `StructrrContextBridge(reference_check=...)`, typically a closure over
the private context and workspace. Missing configuration fails explicitly. The
response includes the public report, not the manifest or private validation data.
Automatic provider construction of this callback is part of step 7.

For a trusted local host, `python -m powdrr_lift.structrr.source_reader check`
loads `POWDRR_STRUCTRR_WORKSPACE`, `POWDRR_STRUCTRR_CATALOG`,
`POWDRR_STRUCTRR_HANDOFF`, and `POWDRR_STRUCTRR_VALIDATION_INPUTS` (the latter three
are serialized file paths). This host command's environment is configuration,
not an isolation boundary. Those private paths must not be passed into the worker;
the isolated proxy uses only the endpoint. Exit codes are 0 for placement pass,
1 for failed/unresolved validation, and 2 for invalid startup configuration.
