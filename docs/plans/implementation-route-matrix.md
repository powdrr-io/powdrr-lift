# Implementation route matrix

Status: audited against `origin/main` at the start of milestone 1.

This matrix describes live implementation-capable entry points and their
current authority, execution, gates, and delivery. “Legacy” means the route is
still supported but does not inherit the protected source-reference run's
complete boundary. It does not imply that the route has no controls.

| Entry point / selector | Classification | Accepted authority and prompt identity | Invocation and scope | Required gates and delivery/artifacts |
| --- | --- | --- | --- | --- |
| Workflow issue worker, implementation task with `source_reference_design_file` | Protected | Proposed plan item is bound to workflow ID and base commit; source design is loaded as a regular committed blob from that exact base and checked against plan/item IDs. Request objective and allowed paths are compiled from the item. | `dispatch_source_reference_task` prepares one immutable run and claims one MiniSWE invocation. Allowed paths come from the reviewed item. | Source-reference validation and protected execution; optional pinned story context/test authoring; configured validation profile; patch identity/scope checks before transfer. Run receipts and scratch artifacts live under the source-reference run's temporary roots. Tests: `test_source_reference_dispatch.py`, `test_source_reference_run.py`. |
| Workflow issue worker, implementation task without `source_reference_design_file` | Legacy | Proposed plan context, workflow/base commit, optional Structrr proposal and inherited decisions. No single immutable protected prompt contract. | Legacy coding-agent path, which can run recovery/provider attempts; scope is checked against request/attempt snapshots. | Existing authored tests, validation profile, recovery/evaluation and plan checks as configured; workflow task result is delivered to the workflow. Issue-worker artifacts are under the system temporary directory. Tests: `test_workflow_task_agent.py`, `test_coding_agent.py`. |
| Workflow issue worker, reference intent without reviewed design selector | Explicitly unsupported | Reference placements, preservations, or definitions without `source_reference_design_file` are rejected. | No provider invocation. | Fails closed before legacy authorship/recovery. Test: `test_task_routes_before_legacy_authorship_and_recovery`. |
| Workflow task `ci-repair` mode | Legacy | CI response context and observed failure gathered by the CI response workflow. | `_run_ci_repair_agent` and its retry machinery; bounded by configured roundtrips/retries, not the protected one-invocation source run. | CI failure research, repair-attempt validation, and finalization gates; CI repair result/artifacts are managed by CI response workflow. Tests: `test_workflow_task_agent.py`, `test_ci_response_cli.py`, `test_ci_response_policy.py`. |
| Workflow task `plan-amendment` mode | Legacy | Existing plan plus amendment context. | `_run_plan_amendment_agent`; provider/recovery behavior remains in task-agent workflow. | Plan repair/validation and workflow result handling. Test coverage is in `test_workflow_task_agent.py` and plan-repair tests. |
| Feature endpoint / Structrr workflow, `run_code_task_agent` phase | Legacy | Feature workflow state, proposal/design records, generated worker prompt, and state-carried scope. Prompt and state are compiled across the command catalog and endpoint. | `feature_endpoint._run_code_agent_phase` invokes coding-agent recovery/evaluation; retries may occur. | Feature workflow validation, required test cases, verification evidence, diff review, and final review steps are available in later phases. Artifacts are kept in feature/workflow state and configured worktree paths. Tests: `test_feature_endpoint.py`, `test_coding_agent_end_to_end.py`. |
| CLI `run-coding-agent` / `run_coding_agent` | Legacy standalone bounded attempt | Required implementation-request JSON compiled from a validated execution unit; request ID/base commit plus explicit worktree and attempt ID. | One CLI attempt, selected provider (`opencode` or `minisweagent`); request allowed paths govern scope. This entry point has its own attempt boundary, not the protected source-reference boundary. | Registered validation profiles and attempt snapshot/scope checks; request/attempt artifacts in required output directory. Tests: `test_coding_agent.py`, `test_implementation_packet.py`. |
| CLI issue, feature, and CI response commands | Delegating wrappers | Authority is supplied by the selected workflow/issue/PR context and then follows the corresponding row above. | Inherits delegated workflow behavior. | Inherits delegated gates and artifact locations. CLI parsing is in `cli.py`; command-specific tests are listed above. |
| Harbor/Pier benchmark or worker wrapper | Delegating wrapper | Task/plan payload supplied to the workflow adapter; no separate protected implementation contract was found in this repository audit. | Delegates into workflow/task routes, so route selection remains governed by the issue-worker selector or the legacy task path. | Inherits delegated route gates; benchmark run artifacts are external to the production handoff and are not a delivery authority. Relevant adapters: `task_scenario.py`, `planned_workflow_task.py`; tests include `test_harbor_env.py`. |

## Migration contract

1. The reviewed `source_reference_design_file` field is the only currently
   accepted selector for the protected source-reference implementation route.
2. Reference intent without that selector must fail before any legacy provider
   or recovery path is entered.
3. Ordinary implementation, CI repair, plan amendment, feature endpoint, and
   standalone CLI behavior remain on their existing routes until an explicitly
   reviewed plan/configuration change migrates them.
4. No route may claim protected-run guarantees based only on sharing a provider
   or validation command. Invocation ownership, immutable inputs, scope, gates,
   and delivery checks must each be established at the route boundary.
5. The protected source-reference route is currently MiniSWE-only. The CLI and
   legacy routes can select other providers and have distinct retry/recovery
   behavior; these differences remain open migration work.

## Evidence and known gaps

The route-selector fixture in `test_source_reference_dispatch.py` proves that a
reviewed selector reaches the protected dispatcher without entering legacy
Structrr authorship, and that reference intent without the selector fails
closed. The source-run tests cover single invocation and prepared-run controls.
The other rows are inventory findings backed by their component tests; they do
not yet have a shared executable route registry or a cross-entry-point fixture
that proves route identity end to end. That is a remaining integration gap,
not evidence that the legacy paths are protected.

In particular, feature endpoint recovery can retry; CI repair has its own
roundtrip/retry policy; standalone CLI has a distinct single-attempt boundary;
and the generic issue-worker route may use legacy recovery. Prompt identity,
invocation count, validation, artifact retention, and delivery authority are
not uniform across those paths. Milestones 2–5 must select migrations and add
route-level evidence before claiming a common handoff contract.
