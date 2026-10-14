// Reproduce the deepCopy helper added by the saved Koota reference solution.
// Source artifact: tasks/koota-entity-snapshot-rollback/solution/solution.patch
// This is an experiment witness, not a replacement for running the task suite.

import assert from "node:assert/strict";

function deepCopy(obj) {
  const result = {};
  for (const key of Object.keys(obj)) {
    const val = obj[key];
    if (val !== null && typeof val === "object" && !Array.isArray(val)) {
      result[key] = deepCopy(val);
    } else if (Array.isArray(val)) {
      result[key] = [...val];
    } else {
      result[key] = val;
    }
  }
  return result;
}

const plainObjectInput = { nested: { count: 1 } };
const plainObjectCopy = deepCopy(plainObjectInput);
plainObjectInput.nested.count = 2;
assert.equal(plainObjectCopy.nested.count, 1);

const input = { items: [{ count: 1 }] };
const snapshot = deepCopy(input);
input.items[0].count = 2;
const sourceToSnapshot = snapshot.items[0].count;

const input2 = { items: [{ count: 1 }] };
const snapshot2 = deepCopy(input2);
snapshot2.items[0].count = 3;
const snapshotToSource = input2.items[0].count;

assert.equal(sourceToSnapshot, 2);
assert.equal(snapshotToSource, 3);
console.log(
  JSON.stringify({
    plainObjectControl: "independent",
    sourceToSnapshot: { observed: sourceToSnapshot, expected: 1 },
    snapshotToSource: { observed: snapshotToSource, expected: 1 },
    finding: "nested array elements are shared",
  }),
);
