# Publishing requirement execution receipts

Powdrr can use a successful verifier run as deterministic planning evidence when
the run is pinned to the exact source commit. Target repositories can publish
the named artifact from a reusable GitHub Actions workflow after their verifier
job has completed.

## Caller workflow

The verifier job uploads the artifact directory emitted by
`VerificationEvidenceRunner` as `powdrr-verification-evidence`. It contains a
`verification-run.json` index and one evidence directory per stable obligation
ID. Each index entry points to that obligation's persisted `evidence.json`.
The index records the candidate tree and the serialized obligations so the
publisher does not need a hand-authored receipt input file.

The target repository commits its Structrr diff artifacts at
`docs/structrr/diffs/**/structrr-diff.yaml`. Each
`verification.required_test_cases` entry may contain selectors and explicit
`requirement_bindings` with stable requirement IDs and typed digests. The
publisher matches each selector to exactly one verifier obligation by exact
string equality. It publishes a receipt only when every selector bound to that
requirement has one matching passing evidence record. Missing, ambiguous, and
failed evidence is omitted, allowing planning to use semantic coverage.
Malformed identities, digest conflicts, and evidence for a different pinned
commit or candidate tree fail publication.

After uploading verifier evidence, a downstream job calls the reusable workflow
with a full Powdrr commit SHA:

```yaml
  publish-receipts:
    needs: verify
    uses: powdrr-io/powdrr-lift/.github/workflows/publish-requirement-execution-receipts.yml@<powdrr-ref>
    with:
      powdrr_ref: <full-powdrr-commit-sha>
```

The caller must grant the workflow `actions: read` and `contents: read`. The
reusable workflow checks out the caller at `github.sha`, verifies that checkout
HEAD equals the pin, downloads the verifier evidence artifact, checks the
candidate tree and exact requirement bindings, and publishes
`powdrr-requirement-execution-receipts`. Powdrr planning consumes the receipt
artifact only from a completed successful GitHub Actions run on the exact
pinned commit.
