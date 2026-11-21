# Publishing requirement execution receipts

Powdrr can use a successful verifier run as deterministic planning evidence when
the run is pinned to the exact source commit. Target repositories can publish
the named artifact from a reusable GitHub Actions workflow after their verifier
job has completed.

## Caller workflow

The verifier job uploads an artifact named `powdrr-verification-receipt-input`
containing one `receipt-input.json` file. The file has this shape:

```json
{
  "schema_version": "verification-receipt-input-v1",
  "candidate_tree": "<candidate-tree-fingerprint>",
  "obligations": [],
  "evidence": [],
  "requirement_bindings": {
    "<obligation-id>": [
      {
        "requirement_id": "<stable-requirement-id>",
        "requirement_digest": "sha256:<typed-requirement-digest>"
      }
    ]
  },
  "evidence_artifact_refs": {
    "<obligation-id>": "<artifact-reference-to-evidence.json>"
  }
}
```

`obligations` and `evidence` are the corresponding records emitted by
`VerificationEvidenceRunner`. Requirement bindings must come from the pinned
compiled execution plan. The producer does not infer a requirement from an
obligation name, selector, test content, or story reference. One verifier result
may be bound to multiple requirements, but a requirement may appear only once
in the input.

After uploading the input, a downstream job calls the reusable workflow with a
full Powdrr commit SHA:

```yaml
  publish-receipts:
    needs: verify
    uses: powdrr-io/powdrr-lift/.github/workflows/publish-requirement-execution-receipts.yml@<powdrr-ref>
    with:
      powdrr_ref: <full-powdrr-commit-sha>
```

The caller must grant the workflow `actions: read` and `contents: read`. The
reusable workflow checks out the caller at `github.sha`, verifies that checkout
HEAD equals the pinned commit, validates each evidence record against its
explicit obligation and candidate tree, and publishes
`powdrr-requirement-execution-receipts`. Failed, missing, ambiguous, or
mismatched evidence makes the producer fail and no receipt artifact is
published. Powdrr planning only consumes the artifact from a completed
successful GitHub Actions run on the exact pinned commit.
