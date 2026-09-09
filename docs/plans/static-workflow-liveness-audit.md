# Static Workflow Liveness Plan Audit

This audit maps the liveness plan to the implementation in PR #596. The
analyzer is intentionally execution-free: template instantiation and prompt
rendering are represented by pure static substitutions/contracts, so validation
does not create files, invoke tools, contact providers, or mutate Git state.

| Plan area | Evidence in implementation | Regression evidence |
| --- | --- | --- |
| Capability effects and ownership | `workflow_liveness.py` effect registry, `StepControlContract`, `SkillEffectSummary` | effect/ownership paths in definition-analysis tests |
| Finite abstract state and fixed point | `AbstractWorkflowState`, `AbstractTransition`, `build_abstract_execution_graph` | idempotent/read-cycle fixtures |
| Deadlocks and non-progress cycles | terminal-state and SCC diagnostics with cycle and entry path | missing-output and self-loop fixtures |
| Retry usefulness | gate observed-domain mapping and retry write intersection | gate retry diagnostic path |
| Completion observability | required-output producer and runner-result checks | missing-output fixture |
| Coding-loop bounds | positive bound and stopping-condition checks | unbounded-loop fixture |
| Nested skills | call graph SCCs, required outputs, effect summaries | nested-call validation path |
| Templates | symbolic checks plus pure representative placeholder instantiation | template validation path |
| Prompt authority | prose checks plus rendered-snapshot contract comparison helper | forbidden-action fixture |
| Baselines and CI | versioned owner/reason/expiry baseline schema; errors are never suppressible | baseline suppression fixture; CI invokes `--baseline` |
| Diagnostics and CLI | stable codes, severity, state, cycle, entry path, remediation; JSON and text output | CLI and JSON tests |

Repository verification for this change set is the full test suite, formatter,
linter, type checker, and repository-wide definition validation. Advisory
unknown-shell and prompt warnings remain visible; proven liveness errors fail
validation and cannot be hidden by the baseline.
