# Proof-of-concept result: Koota snapshot deep copy

## Question tested

For a data trait containing an object inside an array, does a snapshot retain
its own nested value when either the entity or snapshot is mutated?

This question was available from the Koota instruction and the existing
reference validation `v004` before inspecting the candidate solution. The
reference says nested mutation must not change the other side.

## Evidence inspected

- The candidate is the saved Koota reference `solution/solution.patch` from the
  local DeepSWE task corpus, not a live coding-agent submission.
- Its `deepCopy` helper recursively copies plain object properties. Its array
  branch uses `[...val]`, which creates a new array but retains references to
  objects inside that array.
- The added task tests named “should deep copy trait data” and “snapshot deep
  copy” change the entity's top-level trait values with `entity.set`. The
  relation-data test compares a simple `{ role: 'engineer' }` value. These cases
  do not mutate an object nested inside an array.

## Witness

The exact array-copy logic from the saved helper was run against this input:

```js
const original = { items: [{ count: 1 }] };
const snapshot = deepCopy(original);
original.items[0].count = 2;
// snapshot.items[0].count is 2; expected 1

const original2 = { items: [{ count: 1 }] };
const snapshot2 = deepCopy(original2);
snapshot2.items[0].count = 3;
// original2.items[0].count is 3; expected 1
```

Observed output:

```json
{"direction":"source-to-snapshot","snapshotCount":2,"expected":1}
{"direction":"snapshot-to-source","sourceCount":3,"expected":1}
```

The reproducible helper replay is
[`witness_koota_array_copy.mjs`](witness_koota_array_copy.mjs). It also checks
that the helper's plain nested-object path behaves independently as a control.

## Result

The focused question exposed a real behavioral gap in the saved reference
solution and a matching gap in its added tests. A top-level “does it deep copy?”
review could be falsely reassured by the passing-looking test names and flat
value assertions. Asking for a nested mutation scenario led to a discriminating
check, and tracing the implementation explained why that check fails.

This is evidence that the question-to-obligation-to-evidence method can add
value in one targeted case. It is not a measured comparison against an LLM
holistic reviewer, and it does not establish prevalence or overall reviewer
accuracy. The witness exercised the helper logic copied from the saved patch;
the full DeepSWE task suite was not run. The reference validation is
agent-authored, so the finding should be confirmed against a human-reviewed
obligation before being counted in a benchmark.

## Next check

Add a focused regression case to the task's validation patch that mutates an
object nested inside an array in both directions. Run it against the saved
reference implementation and a deliberately shallow-copy mutant. The check
should fail for the current helper and shallow mutant, and pass only when array
elements are copied recursively. Then compare that finding with a blinded
holistic review to measure whether atomic questioning improves detection.
