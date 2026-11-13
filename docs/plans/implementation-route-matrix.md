# Implementation route matrix

Status: current-state inventory; legacy implementation routes are removal or
explicit-rejection candidates under the migration policy in the consolidated
plan.

This matrix describes live implementation-capable entry points and their
current authority, execution, gates, and delivery. “Legacy” is a description of
current code only; these routes are not required to remain supported. The
target is to remove code-writing routes that do not use the protected boundary,
not to port them.

| Entry point / selector | Classification | Accepted authority and prompt identity | Invocation and scope | Required gates and delivery/artifacts |
| --- | --- | --- | --- | --- |
| Workflow issue worker, implementation task with `source_reference_design_file` | Protected | Proposed plan item is bound to workflow ID and base commit; source design is loaded as a regular committed blob from that exact base and checked against plan/item IDs. Request objective and allowed paths are compiled from the item. | `dispatch_source_reference_task` prepares one immutable run and claims one MiniSWE invocation. Allowed paths come from the reviewed item. | Source-reference validation and protected execution; optional pinned story context/test authoring; configured validation profile; patch identity/scope checks before transfer. Run receipts and scratch artifacts live under the source-reference run's temporary roots. Tests: `test_source_reference_dispatch.py`, `test_source_reference_run.py`. |
| Workflow issue worker, implementation task without `source_reference_design_file` | Explicitly rejected | Proposed plan context, workflow/base commit, optional Structrr proposal and inherited decisions. No single immutable protected prompt contract. | The issue-worker guard rejects before request compilation, provider invocation, or recovery. | No code-writing provider runs. Regression: `test_task_routes_before_legacy_authorship_and_recovery` also proves ordinary non-reference work is rejected. |
| Workflow issue worker, reference intent without reviewed design selector | Explicitly unsupported | Reference placements, preservations, or definitions without `source_reference_design_file` are rejected. | No provider invocation. | Fails closed before legacy authorship/recovery. Test: `test_task_routes_before_legacy_authorship_and_recovery`. |
| Workflow task `ci-repair` mode | Explicitly rejected; implementation removed | No accepted authority or prompt. | A narrow mode guard rejects before provider invocation; worker helpers, Procedrr operations, and registered repair processes were removed. | CI observation remains available without repair dispatch. Regression: `test_ci_repair_mode_rejects_legacy_code_writing`. |
| Workflow task `plan-amendment` mode | Retained non-code-writing workflow | Existing reviewed plan plus typed repair request grounded in failure evidence. | `_run_plan_amendment_agent` applies the typed repair through `PlanRepairService`; it does not invoke a coding worker. | Plan validation and result handling remain. Keep this route distinct from product-code implementation. Test coverage is in `test_workflow_task_agent.py` and plan-repair tests. |
| Feature endpoint / Structrr workflow, `run_code_task_agent` and `run_code_agent` phases | Explicitly rejected | Feature workflow state, proposal/design records, generated worker prompt, and state-carried scope. | Command-catalog entry points reject these code-writing operations; the legacy task-agent implementation body was removed. | Planning/review commands remain available; code-writing stops with a clear unsupported error. Regressions: `test_feature_endpoint.py` and workflow dispatch tests. |
| Feature endpoint candidate correction | Removed; stale dispatch rejected | No accepted correction authority. | The automatic correction loop and helper were removed from the process and endpoint; stale `correct_candidate_from_structrr_diff` dispatch is rejected. | Final review now fails closed on unresolved obligations; it does not start a second coding session. |
| CLI `run-coding-agent` / `run_coding_agent` | Removed | The parser command and handler were removed. | Requests fail argparse validation; no generic standalone provider invocation is available. Tests: `test_coding_agent.py`, `test_coding_agent_end_to_end.py`. |
| CLI and Procedrr `respond-to-pr-ci` | Observation only | CI status, associated issue/PR, verification command, and observed PR head. | The process invokes `ci_observe` and returns; the repair workflow declarations, research/compilation helpers, retry logic, and repair operations are removed. | Observation and checkpoint reporting remain available. Tests: `test_ci_response_cli.py`, `test_ci_response_policy.py`, `test_procedrr_chat.py`. |
| CLI issue and feature commands; Harbor/Pier adapters | Delegating wrappers | Authority is supplied by the selected workflow/issue/PR context and then follows the corresponding row above. | Inherits delegated workflow behavior. | Inherits delegated gates and artifact locations. CLI parsing is in `cli.py`; adapters are `task_scenario.py` and `planned_workflow_task.py`. |
| Harbor/Pier benchmark or worker wrapper | Delegating wrapper | Task/plan payload supplied to the workflow adapter; no separate protected implementation contract was found in this repository audit. | Delegates into workflow/task routes, so route selection remains governed by the issue-worker selector or the legacy task path. | Inherits delegated route gates; benchmark run artifacts are external to the production handoff and are not a delivery authority. Relevant adapters: `task_scenario.py`, `planned_workflow_task.py`; tests include `test_harbor_env.py`. |

## Migration contract

1. The reviewed `source_reference_design_file` field is the only currently
   accepted selector for the protected source-reference implementation route.
2. Any code-writing request without that selector must fail before any legacy
   provider or recovery path is entered.
3. Legacy implementation routes are removed or explicitly unsupported; they
   are not preserved or ported. Non-code-writing workflow functions can remain.
4. No route may claim protected-run guarantees based only on sharing a provider
   or validation command. Invocation ownership, immutable inputs, scope, gates,
   and delivery checks must each be established at the route boundary.
5. The protected source-reference route is currently MiniSWE-only. Remove
   generic provider selection and recovery behavior where it is not required by
   this route.

## Evidence and known gaps

The route-selector fixture in `test_source_reference_dispatch.py` proves that a
reviewed selector reaches the protected dispatcher without entering legacy
Structrr authorship, and that reference intent without the selector fails
closed. The source-run tests cover single invocation and prepared-run controls.
The other rows are inventory findings backed by their component tests; they do
not yet prove removal or rejection of their code-writing paths. That is the
remaining milestone 1 gap.

The generic issue-worker fallback is rejected, feature endpoint code-writing
commands are rejected, and the standalone generic coding-agent CLI is removed.
CI repair modes, operations, worker helpers, retry logic, and process
declarations are removed; CI observation remains. The feature candidate
correction loop and helper are also removed. Typed plan amendment remains a
non-code-writing service workflow. Route-level evidence for delegating entry
points remains before claiming a common handoff contract.
