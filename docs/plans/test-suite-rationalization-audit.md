# Test suite rationalization audit

## Baseline and runtime profile

The earlier audit collected 1,910 cases across 163 test modules. The current
suite collects 1,905 cases in 1.46 seconds. There are 1,765 test functions and
140 parameterized cases beyond that count. A complete run passed 1,899 tests and
skipped 6 in 239.88 seconds.

The initial 50 slowest call durations showed a bounded concentration: the
slowest test took 23.71 seconds, the second slowest 10.82 seconds, and only 15
tests took more than 2.5 seconds. The leading cases were:

| Test | Call time | Audit finding |
| --- | ---: | --- |
| `test_structrr_real_repositories.py::test_bootstrap_real_powdrr_checkout_is_valid_and_repeatable` | 23.71s | Real-checkout smoke and golden-symbol check. A second full bootstrap duplicated deterministic behavior already covered by `test_structrr_bootstrap.py::test_bootstrap_is_deterministic_and_does_not_stage_output`; removed the duplicate call. |
| `test_implement_feature_e2e.py::test_deepswe_state_data_instructions_produce_valid_test_contracts` | 10.82s | Broad integration check for long-form input, contract generation, and coverage linkage. Assertions span distinct artifacts; not a safe deletion candidate. |
| `test_implement_feature_e2e.py::test_implement_feature_generates_tailored_repair_prompt_and_retries_probe` | 8.46s | Exercises the post-coding repair path and prompt content; not duplicated by the complete-flow test. |
| `test_implement_feature_e2e.py::test_implement_feature_decomposes_state_data_api_requirements_before_design` | 7.23s | Checks bounded clause decomposition and parent provenance; distinct from broad state-data contract generation. |
| `test_workflow_scenario.py::test_checked_in_workflow_scenario_suite_passes` | 7.03s | Runs all eight manifest scenarios. Some scenario wrapper coverage may overlap with individual runs; compare manifest membership before pruning. |
| `test_implement_feature_e2e.py::test_implement_feature_reconciles_non_required_sentence_with_design` | 6.13s | Configures a non-required sentence but only asserts that all clauses remain projected and obligated. It does not assert the non-required disposition or explain why the obligation remains; clarify or repair this contract before counting it as meaningful coverage. |
| `test_implement_feature_e2e.py::test_implement_feature_blocks_untraceable_required_test_obligation` | 5.80s | Tests required-test intent references and verification artifacts; likely a distinct failure boundary. |
| `test_implement_feature_e2e.py::test_implement_feature_continues_with_valid_scenarios_when_one_stays_generic` | 5.28s | Tests partial scenario quality and exclusion provenance; distinct from all-invalid and invalid-syntax cases. |
| `test_implement_feature_e2e.py::test_implement_feature_uses_deterministic_structural_proposal_gate` | 4.95s | Covers deterministic proposal-gate behavior; distinct from planner schema retry. |
| `test_workflow_task_scenario.py::test_execute_proposed_pr_failure_fixtures_recover_or_handoff` | 4.87s | Exercises failure recovery/handoff across task fixtures; inspect parameter overlap before changing. |
| `test_implement_feature_e2e.py::test_implement_feature_runs_the_complete_flow_with_a_deterministic_worker` | 4.83s | Broad successful-path integration; uniquely checks end-to-end artifact linkage. |
| `test_implement_feature_e2e.py::test_implement_feature_retries_schema_invalid_planner_output` | 4.62s | Planner response retry boundary; retain unless a focused test covers the same contract. |
| `test_implement_feature_e2e.py::test_implement_feature_stops_before_coding_when_generated_probe_is_invalid` | 4.21s | Invalid probe syntax, retry count, and coding prevention; distinct from weak but syntactically valid probes. |
| `test_implement_feature_e2e.py::test_implement_feature_rejects_worker_out_of_scope_edits` | 3.42s | Worker scope enforcement; distinct behavior. |
| `test_implement_feature_e2e.py::test_implement_feature_blocks_coding_when_probe_repair_stays_generic` | 2.88s | Prevents coding with a generic probe; distinct from invalid syntax and partial-quality acceptance. |

These integration tests create temporary Git repositories, run planning and
review stages, and validate generated artifacts. Their different scenarios
represent separate acceptance boundaries; deleting them based on shared setup
or similar names would remove distinct guarantees. A follow-up optimization
should extract focused tests for expensive boundaries while retaining at least
one complete-flow test, then measure the runtime change.

After removing the second real-checkout bootstrap, that test took 8.41 seconds
in the validation run, 15.30 seconds less than the initial measurement. The
entire validation run took 268.40 seconds versus 239.88 seconds initially, so
the single-test improvement does not establish an overall suite speedup; run
time varies across the rest of the suite.

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
parameters, removing 7 redundant cases. The current collection is 1,905 cases.
The real-checkout bootstrap now runs once; repeatability remains covered by the
focused fixture-repository test, avoiding a second scan of the Powdrr checkout.
This saves runtime but does not reduce the collected case count.

## Second audit pass: duplicate tests and dead code

The latest branch collects 1,918 test cases. A duplicate scenario test was
removed: it loaded `live-task-all.yaml`, replaced its live provider and
expectations with those from `task-all.yaml`, and ran the same `run_all` fixture
already included in the checked-in scenario suite. In the preceding timing run,
this duplicate execution took 2.52 seconds.

The same test module contained a 177-line live coding-loop test helper whose
name began with `_`. Pytest never collected it, so its API calls, assertions,
and environment-gated skip path never ran. The helper and imports used only by
it were removed. This removes dead test code, not a collected case.

A static reference scan initially flagged 41 private functions. Manual checks
excluded workflow command handlers resolved with `getattr`, Textual event
handlers registered by decorators, and other dynamically registered callbacks.
Thirty private function definitions had no call sites in Python, tests, or
checked-in workflows and were removed. These included stale wrappers, parsers,
validators, output helpers, an unused gate runner, and the unused workflow-step
checkpoint snapshot/restore subsystem. The changed-path reader used by
`procedrr_chat` was retained after mypy exposed its local import. No behavior
was moved because the removed functions were unreachable.

The public `--context-compaction-threshold` option and its config field still
have no runtime reader; their compatibility should be decided separately before
removing that CLI option.

An AST check found no duplicate literal parameter rows across 179 inspected
`pytest.mark.parametrize` entries. Two test functions have no syntactic
`assert`, but each calls a validator that raises for invalid input, so they are
not dead tests based on that signal.

After cleanup, pytest collected 1,918 cases and passed 1,912 with 6 skipped in
168.72 seconds. The duplicate scenario removal is the only collected-case
reduction in this pass. The other cleanup removes uncollected code. This pass
still does not support deleting 400 collected cases; the 400-case goal needs a
selector-level removal ledger showing equivalent input classes and outcomes.

## Reduction plan

The requested 400-case reduction is a target, not yet a safe deletion list. The
current evidence proves one vacuous assertion, one redundant parameter matrix
branch, and one redundant full-repository operation. It does not yet prove 400
test cases have no unique behavior coverage. Continue the audit with these steps:

1. Repeat test-context coverage across all repository-owned source packages,
   including `procedrr` and `procedrr_evaluator`, and include subprocess test
   selectors where the child process can be traced.
2. Group cases by production behavior, relevant input partition, and asserted
   observable outcome. Treat identical coverage as a candidate signal only.
3. Remove cases only when another retained case covers the same input class and
   outcome, or when the case exercises an implementation detail already covered
   by a public contract or end-to-end test.
4. Inspect `test_implement_feature_reconciles_non_required_sentence_with_design`:
   its setup and assertions appear inconsistent. Assert the intended routing
   behavior and why clauses remain obligations, or replace it with a focused
   contract test. Do not count it as evidence of that behavior until explicit.
5. Review compatibility and migration tests against the supported-input policy.
   Keep tests for formats and imports that remain supported; remove them only
   after that support has ended.
6. Publish a removal ledger with each test selector, the retained covering test,
   and any behavior coverage lost. Continue until the ledger supports at least
   400 case removals without deleting unique behavior checks.

Large files and repeated parameterized inputs are review priorities, not
removal evidence by themselves. The highest collected modules include
`test_feature_endpoint.py` (124), `test_workflow_task_agent.py` (72),
`test_science_acceptance_templates.py` (65),
`test_github_issue_plan_generation.py` (49), and
`test_semantic_contract_compiler.py` (44).
