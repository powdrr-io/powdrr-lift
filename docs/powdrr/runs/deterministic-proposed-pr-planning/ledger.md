# Deterministic proposed PR planning run

Goal: split proposed PR planning from effect allocation, provide authoritative effects as structured runtime input, enforce machine-checkable output schemas, and make validation branching deterministic.

## Tasks

- [x] Task 1: Build and validate the system map. (completed; native filename validation gap documented)
- [x] Task 2: Build and validate the feature/PR contract. (completed)
- [x] Task 3: Implement the runtime, skill, and test changes. (completed)
- [ ] Task 4: Review the implementation and resolve findings.
- [ ] Task 5: Prepare and validate the PR changelog and plan diff.
- [ ] Task 6: Complete branch-wide and invariant reviews.

## Constraints

- The model must not transcribe YAML or authoritative section/id/action triples.
- Planning and effect allocation must be separate validated phases.
- Runtime-owned branches must not require model confirmation.
- Generated YAML remains a read-only compatibility artifact.
- Preserve existing proposed PR validation and effect-equivalence behavior.

## Evidence

- PR 556 live run: invalid output nesting and assignment shapes were rejected before mutation; valid semantic input compiled and passed deterministic evaluation.
