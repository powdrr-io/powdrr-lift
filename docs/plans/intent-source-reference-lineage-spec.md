# Intent lineage for protected source-reference runs

## Status

Proposed. This specification covers the remaining work to make accepted
Structrr intent traceable through Powdrr's supported implementation handoff.
It supersedes the broader intent plans archived in `superseded/`.

## Purpose

Powdrr already compiles reviewed source-reference intent into a fingerprinted
worker prompt and private validation manifest, executes one protected MiniSWE
attempt, checks the candidate independently, and transfers only an accepted
patch. Powdrr also has a deterministic intent-lineage auditor, but that auditor
is a standalone CLI over a proposal-history format and is not produced or run by
the protected source-reference route.

Complete the missing run-bound lineage record and gate. For each protected run,
an operator must be able to establish which reviewed Structrr source governed
the work, which obligations and verification cases it selected, what exact
attempt and candidate were checked, and which evidence allowed or blocked patch
transfer. Missing or mismatched links must fail closed.

This is a handoff and auditability specification. It does not add another user
memory system or a second implementation route.

## Current Powdrr direction

- **Structrr** owns the reviewed design and source-reference obligations.
- **Procedrr** orders workflow gates and invokes the selected Workrr operation.
- **Workrr** loads the reviewed design from the exact committed base, compiles
  its immutable handoff, runs the protected worker once, validates the
  candidate, and controls patch transfer.
- **MiniSWE** is an untrusted repository editor. It does not decide intent,
  acceptance, or delivery.
- The only supported code-writing route in scope is the protected workflow
  issue route selected with `source_reference_design_file`. Other code-writing
  routes remain removed or explicitly rejected, as recorded in the
  [implementation route matrix](implementation-route-matrix.md).

The source-reference handoff already binds placements, preservations, typed
behavior scenarios, required test cases, allowed scope, and private validation
inputs into its prompt and manifest fingerprints. Do not repeat that compiler
work here. Extend its existing artifacts at the Workrr run boundary.

## Implemented foundations (not remaining tasks)

| Capability | Current implementation |
| --- | --- |
| Versioned intent source and clauses, selectors, supersession, revocation, and optimistic updates | `src/powdrr_lift/structrr/intent.py` |
| Accepted source-reference placements, preservations, and typed cases | `docs/design/structrr-source-reference-handoff.md` and its service compiler |
| Exact committed-base authority and plan/item binding | `src/powdrr_lift/workrr/source_reference_dispatch.py` and `structrr-source-task-dispatch.md` |
| Fingerprinted public prompt and private validation manifest | `powdrr_service.structrr.source_reference_handoff` |
| One protected MiniSWE attempt, private run artifacts, candidate checks, and guarded patch transfer | `src/powdrr_lift/workrr/source_reference_run.py` and `source_reference_dispatch.py` |
| Deterministic proposal-history lineage audit, currently disconnected from the protected route | `packages/service/src/powdrr_service/structrr/intent_lineage.py`; CLI `audit-intent-lineage` |

Existing design and implementation documents remain authoritative for those
implemented contracts. This plan must not restate them as unfinished work.

## Scope

Add a versioned lineage record for one protected source-reference run. Reuse
the accepted design, compiled handoff bundle, run identity, terminal outcome,
candidate identity, validation reports, and dispatch result already created by
Workrr. The record is private run evidence and follows the existing temporary
artifact retention policy; this plan does not promise a permanent audit store.

The record must link, with stable IDs and fingerprints where available:

1. Repository identity, workflow and plan item identity, exact base commit,
   reviewed design path, committed blob digest, and parsed design identity.
2. Accepted source-reference obligation IDs, their Structrr references and
   dispositions, and the required case/scenario IDs that verify them.
3. Catalog, handoff contract, public prompt, private manifest, and complete
   bundle fingerprints. Do not copy private oracle contents into the record.
4. The reserved run identity and sole worker attempt, including its terminal
   classification and candidate worktree/tree or diff identity.
5. Candidate-bound verification evidence, independent source-reference review,
   final validation outcome, patch digest, and transfer eligibility decision.

The lineage record must identify which obligations each case and evidence item
supports. It may reference existing artifacts by typed IDs and fingerprints;
it must not rely on filesystem ordering, mutable paths alone, worker-reported
claims, or reconstructed conversation history.

## Required behavior

1. Produce the record from frozen run inputs and Workrr-observed results. The
   worker cannot create, edit, or supply authoritative lineage fields.
2. Recompute document, catalog, handoff, candidate, and evidence fingerprints
   using the existing canonical validators. A digest by itself does not prove
   that an artifact is accepted or that an evidence item passed.
3. Audit the record deterministically and include a machine-readable report in
   the private terminal artifacts. Repeated audits over identical input must
   produce identical report data.
4. Prevent patch transfer unless every required lineage edge resolves to the
   exact frozen run, candidate, obligation, and evidence artifact and all
   required outcomes pass.
5. Fail closed on absent, duplicate, stale, malformed, cross-run, or tampered
   records. Preserve findings in terminal diagnostics; never start another
   implementation attempt to repair them.
6. Keep source authority at the reviewed committed design. Changes to the
   design or plan require a newly reviewed commit and a new run identity.
7. Preserve the existing one-invocation boundary, OS sandbox, path limits,
   candidate isolation, privacy boundary, and no-fallback behavior.

## Delivery sequence

### 1. Define the source-run lineage schema

Specify the schema version, required artifact references, canonical
normalization, fingerprint rules, and finding codes. It must model the
source-reference contract directly. Do not force placement-only runs through
proposal revision or durable-intent clause fields that they do not use. Keep
the proposal-history auditor available for its existing input format unless a
separate migration is explicitly justified.

### 2. Produce and audit lineage in Workrr

Build the record from the prepared source-reference bundle, invocation record,
candidate identity, and independent validation. Store it under the run's private
artifact directory, bind its fingerprint and audit result into the terminal
record, and audit it before accepting the patch. The record binds the exact
patch proposed for transfer and the transfer eligibility decision; the terminal
result records whether transfer then succeeded. The worker must not see private
lineage or validation inputs.

### 3. Gate delivery and expose findings

Map audit findings to the existing blocked/failed terminal result. A successful
worker exit, passing behavioral tests, or valid patch cannot override missing
lineage. Transfer only the patch whose digest the audit accepted. Report
concise finding codes and references in the dispatch result; retain detailed
evidence and the final transfer outcome in the private run artifacts. Keep
artifacts under the established temporary roots and retention policy.

### 4. Verify end-to-end cases

Exercise the real source-reference preparation, invocation, candidate
validation, audit, and transfer boundaries with offline/scripted workers. Cover
at least:

- a complete passing run whose lineage resolves every accepted obligation to
  required cases, candidate evidence, and transferred patch;
- each missing required edge and each mismatched base, design, bundle, attempt,
  candidate, or evidence fingerprint;
- duplicate IDs, stale evidence, altered private artifacts, and cross-run
  artifact substitution;
- worker-reported ownership or completion data that disagrees with independent
  Workrr evidence;
- a failed lineage audit that leaves the caller checkout untouched and cannot
  trigger a second worker invocation; and
- deterministic audit replay for an unchanged record.

## Completion criteria

This plan is complete when the protected source-reference route creates and
audits the versioned lineage record for every run; patch transfer is blocked on
any unresolved or failed lineage; all evidence is bound to the exact reviewed
design, sole attempt, and candidate; failure artifacts remain inspectable under
the existing retention policy; and the end-to-end cases above pass.

The standalone proposal-history CLI audit is not sufficient evidence of
completion. No broad conversation-memory guarantee, permanent audit retention,
new model compiler, legacy route, or automatic correction loop is part of this
plan.

## Superseded documents

The following documents are retained for historical context in `superseded/`:

- `superseded/durable-intent-implementation-plan.md`
- `superseded/durable-user-intent-retrieval-and-enforcement.md`
- `superseded/intent-preserving-change-control.md`
- `superseded/intent-preserving-change-implementation-plan.md`
- `superseded/intent-preserving-change-pr-plan.md`
- `superseded/structrr-diff-and-invariant-repair-loop.md`

They describe a broader or older OpenCode/feature-endpoint architecture and are
not implementation instructions for the current protected route.
