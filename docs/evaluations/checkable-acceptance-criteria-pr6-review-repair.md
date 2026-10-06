# Checkable acceptance criteria: PR 6 review and repair

This note records the prompt-level effect of PR 6 in the generic instruction to
prompt workflow. It uses a compact unchanged source sentence from the focused
regression test; it is not a GraphQL-specific production rule.

## Source and observed change

Source sentence:

> The result mapping forwards each payload and preserves its contents.

The generated candidate also asserted that `result.cancelled` equals `true`.
Assertion review accepted the forwarding claim using the exact source excerpt
`forwards each payload`, rejected cancellation as unsupported, and marked the
criterion source-only because the candidate was not adequate. That candidate
therefore contributed no checkable acceptance criterion to the worker prompt.

After one local repair, the worker packet contains the retained forwarding check
and no cancellation check:

```text
Reviewed observable acceptance criteria:
  Assert result.payload equals "the forwarded payload"
```

The repair regression renders the `ImplementationPacket` text used by the
coding-agent prompt builder, while the existing end-to-end workflow test captures
the actual request and asserts that its reviewed-criteria section is present. The
repair fixture verifies that the prior accepted assertion ID and contents survive;
the changed criterion fingerprint receives a new review before it can be marked
checkable. Fixture values are illustrative; the reviewer does not turn individual
JSON leaves into separate claims.

## Upstream decisions and evidence rules

- Each whole behavioral assertion gets one of six categories:
  `source_supported`, `repository_supported`, `illustrative_setup`,
  `decision_required`, `unsupported`, or `contradicts_source`.
- `source_supported` requires a verbatim excerpt from one of the assertion's
  cited clauses. `repository_supported` requires a location, repository
  revision or content fingerprint, and excerpt that exactly match evidence
  attached to the review request. Current requests carry no repository evidence,
  so a reviewer cannot promote a repository claim from an invented citation.
- Setup has a separate review. An illustrative setup needs a rationale showing
  that the values add no product constraint. Assertion review covers behavioral
  relationships as a whole rather than treating fixture literals as product
  claims.
- A material open dimension is recorded as `specified`, `unspecified`,
  `not_applicable`, or `needed_for_implementation`. Decision records retain the
  question, affected requirement/assertion IDs, alternatives, selected
  interpretation, source constraints, basis/evidence, confidence, and residual
  uncertainty. A choice without verified evidence is labeled an assumption; if
  it is needed to implement the behavior, the criterion remains unresolved.
- Review-plan records use `acceptance-criterion-review-plan-v2`. Criterion
  serialization remains v1 and `BehaviorScenario` v1 readers remain unchanged.

## Repair behavior and bounds

The live design-interview workflow repairs within each behavioral contract, for
at most two rounds after initial generation. Requests contain only failed
criteria, their review findings, accepted assertions to retain, and missing
requirement coverage. Structural failures and overflow are sent back as bounded
regeneration requests; the global claim budget is not enlarged. Changed and new
criteria are reviewed again, while unchanged criteria reuse evidence only when
their fingerprints still match. Attempt round, request IDs, changed IDs, and
replaced IDs are recorded. At exhaustion, uncovered requirements remain
source-only and unresolved material choices remain unresolved.

Repair requests carry the existing uncertainty policy: `clarify` leaves
unevidenced choices open, while `normative_default` permits only a cited local
convention and keeps the choice labeled as an assumption.

## Regression coverage

The focused tests cover exact source excerpts, repository claims without
attached evidence, unsupported cancellation removal while preserving forwarding,
repair attempt provenance, assumption labeling, unresolved material decisions,
and the resulting checkable worker-prompt text. Workflow end-to-end tests exercise
the added repeat in the real design-interview flow. The source instruction,
solution, and validation patches are not inputs to production repair.

## Remaining limits

The current design-interview request provides no repository evidence to reviewers.
Repository-backed claims are consequently rejected until a generic upstream
stage supplies bounded, revision-pinned repository excerpts. The decision record
captures material choices made during review, but PR 7 still needs to render
necessary choices and material open questions in the final worker prompt and
evaluate deliberately mutated criteria across the task corpus.
