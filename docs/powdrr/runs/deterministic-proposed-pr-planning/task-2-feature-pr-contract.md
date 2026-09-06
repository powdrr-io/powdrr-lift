# Task 2: Feature and proposed PR contract

Generate, fill, and validate the repository feature/PR specification for
`deterministic-proposed-pr-planning`, using the validated system map and current
source as evidence.

The contract must define an implementation plan that:

- separates proposed PR planning from authoritative effect allocation;
- gives each model-owned output a machine-checkable schema;
- supplies authoritative effects as a structured runtime-owned handoff with
  stable effect references;
- compiles split semantic inputs into the existing read-only proposed PR YAML;
- routes successful evaluation forward and invalid evaluation to repair in
  runtime code;
- preserves exact effect equivalence and prevents mutation on invalid input;
- extends unit and live-harness coverage for malformed and valid split outputs;
  and
- keeps the change in one reviewable PR unless the repository contract requires
  a dependency split.

Inspect the implementation boundaries and name concrete files, verification
commands, acceptance criteria, dependencies, and risks. Use the required
feature-pr-specification CLI/template and evaluator. Record outcomes in a task
report and mark only Task 2 complete in the ledger. Do not edit product code,
commit, or push.
