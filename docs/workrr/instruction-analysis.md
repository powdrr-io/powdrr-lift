# Instruction analysis endpoint

`powdrr-lift analyze-instruction` converts one UTF-8 instruction file into a
read-only Powdrr planning packet. It is the proposal-side boundary between a
user instruction and the later `implement-feature` flow.

The command runs the `analyze-instruction` Procedrr skill. Procedrr owns the
sequence and the single planning-model activation; deterministic internal
operations load evidence and compile the resulting packet.

```sh
powdrr-lift analyze-instruction \
  --instruction-file request.txt \
  --work-item-name add-instruction-analysis \
  --repo-root . \
  --allowed-path src/powdrr_lift \
  --allowed-path tests \
  --output .powdrr/instruction-analysis.json
```

The planning provider is used to propose a candidate Structrr plan. The
provider is not asked to edit files, and this command never invokes OpenCode.
The candidate is then compiled locally into the same deterministic artifacts
used by the implementation flow:

- the current baseline and its fingerprint;
- active intent before and after the candidate overlay;
- explicit feature obligations, each linked to acceptance criteria;
- the canonical proposal revision and fingerprint;
- verification-obligation closure, selected contracts, and diagnostics;
- discovered validation profiles;
- evidence fingerprints and the proposal-review worklist.

The report is marked `status: proposal_only`, `review_required: true`, and
`implementation_authorized: false`. A successful command therefore means that
the report was generated, not that its proposal was accepted or that code may
be changed.

## Deterministic boundaries

The model response is limited to a candidate `plan` object. Intent resolution,
operation extraction, active-intent overlay, verification-contract selection,
structural review, and fingerprints are all performed by Powdrr. Every changed
plan item must have an `action` of `added`, `deleted`, or `removed`; operations
that affect active intent must also provide `intent_effect`. Missing structural
facts appear in `proposal_review.structural_failures`.

The command reads the latest committed
`docs/structrr/current/baseline-*.yaml`. If no baseline exists, it bootstraps a
baseline into a temporary directory and uses that in-memory document. It does
not create proposal files, modify Structrr state, run validation commands, or
change Git state. `--output` is the only intentional write, and it writes the
same JSON report that is printed to stdout.

The resulting report can be passed to a later proposal-review process. It is
not itself a review receipt and must not be used as authorization to invoke
OpenCode.
