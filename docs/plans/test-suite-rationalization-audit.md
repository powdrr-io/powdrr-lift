# Test suite rationalization audit

## Baseline

On 2026-10-29, pytest collected 1,910 cases across 163 test modules. The test
files contain 1,765 test functions; parameterization accounts for the remaining
145 collected cases.

An AST scan found no duplicate test names or identical test bodies. It found one
tautological comparison: `test_manifest_fingerprint_is_stable` compared the
fingerprint of a manifest to itself. That assertion now compares independently
constructed equivalent manifests.

A test-context coverage run passed 1,904 tests with 6 skipped. A follow-up
instrumented the standalone `procedrr`, `procedrr_evaluator`, and `science`
trees. Across the combined coverage data, 1,838 cases exercised traced
production lines; 72 cases did not, mostly architecture or source-inspection
checks. There are 114 cases beyond one representative in groups with identical
executed production-line sets. The cases use different input values or assert
different outcomes, so equal line coverage alone does not establish that they
are redundant.

One parameterized workflow test formed 16 combinations, but 8 combinations set
`unshaped_execution_error=True`; that branch returned before reading the other
three parameters. Those 8 cases exercised the same behavior. The matrix now
keeps one unshaped-error case plus all 8 combinations that exercise the other
parameters, removing 7 redundant cases. The current collection is 1,903 cases.

## Reduction plan

The requested 400-case reduction is a target, not yet a safe deletion list. The
current evidence proves only one vacuous assertion and no fully duplicated test
bodies. The following review narrows candidates before removing cases:

1. Repeat test-context coverage across all repository-owned source packages,
   including `procedrr` and `procedrr_evaluator`, and include subprocess test
   selectors where the child process can be traced.
2. Group cases by production behavior, relevant input partition, and asserted
   observable outcome. Treat identical coverage as a candidate signal only.
3. Remove cases only when another retained case covers the same input class and
   outcome, or when the case exercises an implementation detail already covered
   by a public contract or end-to-end test.
4. Review compatibility and migration tests against the supported-input policy.
   Keep tests for formats and imports that remain supported; remove them only
   after that support has ended.
5. Publish a removal ledger with each test selector, the retained covering test,
   and any behavior coverage lost. Continue until the ledger supports at least
   400 case removals without deleting unique behavior checks.

Large files and repeated parameterized inputs are review priorities, not removal
evidence by themselves. The highest collected modules include
`test_feature_endpoint.py` (124), `test_workflow_task_agent.py` (72),
`test_science_acceptance_templates.py` (65),
`test_github_issue_plan_generation.py` (49), and
`test_semantic_contract_compiler.py` (44).
