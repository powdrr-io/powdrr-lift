# Checkable acceptance criteria: PR 7 rendering and capture

This evaluation records the actual worker-request change for a stable source
sentence. The sentence and fixture did not change between the previous and new
renderers.

## Source and exact request effect

Source:

> Response.iter_json yields each array element.

Before, a reviewed criterion was rendered as schema-shaped fields, including its
compiler ID and source reference:

```text
Reviewed observable acceptance criteria:
Implement these source-supported checks while preserving all instruction requirements.
- [transformation; acceptance-criterion:test; sources: instruction-001] Response.iter_json returns array elements
  Setup: {"input": ["A", "B"]}
  Assert yielded elements equals ["A", "B"]
```

Now the captured `ImplementationRequest.prompt` contains a compact contract and
observable check:

```text
Observable acceptance checks:
These reviewed checks express required behavior. Literal setup values are examples unless an assertion states their significance; preserve the stated relationships and outcomes.

Contract 1: Response.iter_json returns array elements
1. Start with {"input":["A","B"]}.
   Check that yielded elements equals ["A","B"].
```

The end-to-end request compilation test asserts this exact captured text and
checks that `prompt_fingerprint` is the SHA-256 of that text. Request schema v3
adds that fingerprint; readers continue to accept v2 artifacts without one.
Repair prompts use the same `ImplementationPacket.render()` path, so they carry
the same accepted criteria and fingerprinting applies to the exact resulting
repair request too.

## Rendering rules

- Only criteria with `criterion_status=checkable` render as acceptance checks.
- Checks are grouped by contract, with criteria numbered within each contract.
- Source IDs, criterion IDs, kinds, and serialized quality labels stay in
  artifacts rather than worker-facing prose.
- Source-only scenarios remain visible through the existing source-only route.
- Legacy scenarios not yet assessed as acceptance checks are not repeated as
  candidate Given/when/then outcomes when reviewed typed criteria are available;
  product requirements remain in the source/product contract sections.
- Unresolved criteria contribute their material open questions, not their
  unverified operation or assertions.
- Explicit assumptions attached to matched legacy scenarios remain visible
  alongside accepted checks.
- The deterministic corpus audit checks normalized required and forbidden
  phrases across the whole prompt. It reports presence only; it is not a
  substitute for semantic review or paired coding-worker outcomes.

## Evidence and remaining evaluation

The focused tests cover contract grouping, event sequencing, assertion rendering,
source metadata omission, unresolved questions, explicit assumptions, exact
request capture, prompt fingerprint validation, and detection of a forbidden
claim even when a required claim also appears elsewhere in the prompt.

This PR verifies prompt construction and the deterministic mutation guard. It
does not claim improved implementation scores: paired current-versus-new coding
worker runs on development and held-out tasks remain necessary to establish that
the clearer checks improve accepted solutions without regressions. Missing token
usage remains unavailable, and a failed/incomplete task run is not a correctness
score.
