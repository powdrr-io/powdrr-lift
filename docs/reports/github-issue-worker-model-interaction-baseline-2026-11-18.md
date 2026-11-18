# GitHub issue worker model-interaction baseline

Baseline date: 2026-11-18  
Implementation revision: `c9720f7d20f009bf9f6bb7528a4b68e81268f8a4`  
Audit: [GitHub issue worker LLM audit](../audits/github-issue-worker-llm-audit.md)

## Status

Trace instrumentation and report aggregation are implemented for the routes
listed below. **No representative runtime runs were available for measurement
when this report was prepared.** Logical request counts, provider attempts,
retries, duration, tokens, cost, and configured provider/model are therefore
unobserved. This report does not estimate runtime totals from the audit's static
fanout formulas.

The inspected repository-local `.powdrr-lift/state` contained a code index
database but no worker run directories, `procedrr-events.jsonl`, MiniSWE
`*.inference-attempts.jsonl`, or test-suite artifacts with embedded
quality-review records. Test fixtures verify trace handling; they are not
production measurements.

## Trace coverage by representative route

| Route | Audit IDs | Trace source | Runtime measurement |
| --- | --- | --- | --- |
| Successful non-story implementation | A03-A07, A16 | Procedrr provider attempts in `procedrr-events.jsonl`; MiniSWE attempts in per-attempt diagnostics | Unobserved: no run artifacts |
| Story-test authoring and review | A01-A05 | MiniSWE author attempts; redacted quality-review records in the test-suite artifact and `run-telemetry.json` | Unobserved: no run artifacts |
| Plan-defect decision and amendment | A14-A15, A17, A19 | Procedrr judge/provider events; typed amendment application itself is deterministic | Unobserved: no run artifacts |
| Pull-request publication recovery | A18-A19 | Procedrr judge/provider events and recovery step events | Unobserved: no run artifacts |
| CI repair | A20-A21 | MiniSWE per-attempt diagnostics, labeled `ci_repair` and `ci_repair_contract_validation`; Procedrr events for orchestration | Unobserved: no run artifacts |

The normal implementation MiniSWE path is labeled `issue_worker_implementation`
and `implementation_candidate_validation`. Source-reference implementation
attempts have a separate caller and gate label. Exact source-reference membership
checks do not require a model call; semantic residual reviews use the wrapped
Procedrr client where configured.

The run summary counts embedded quality-review records separately from
transport-request records. It reports quality logical interactions, provider
call attempts, failures, and retries by role. It does not label those provider
call attempts as network requests because the `complete_json` interface does
not expose internal transport attempts, token usage, or cost.

## Trace fields and limits

- Procedrr traces include interaction and task context, judge path, gate,
  evidence and prompt fingerprints, provider/model when supplied, duration,
  outcome, retry cause, replay status, and token/cost usage when reported by the
  provider.
- MiniSWE traces include request, task, attempt, caller, gate, evidence IDs and
  fingerprint, check/candidate IDs, prompt fingerprint, provider/model, duration,
  outcome, and usage when returned by the inference client. The outer MiniSWE
  adapter records one attempt per inference-handler invocation; retries hidden
  inside an underlying provider are not separately observable there.
- Per-test quality traces record the decision, opinion, or suggestion role,
  request/test/criterion/check identity, evidence and prompt fingerprints,
  provider-call attempt, duration, outcome, and retry cause. They contain no
  prompt or source text. The current JSON completion interface does not provide
  usage or an access-controlled request/response artifact reference.
- Optional/custom process routes B01-B05 were not included in the representative
  worker paths and were not measured. Their activity remains conditional and
  must be established from a route-specific run before planning changes.

## Baseline conclusion

The code can now preserve and summarize trace records for the covered route
families, including the per-test quality-review fanout. This is an
**instrumentation baseline**, not an empirical usage baseline. The WP0 empirical
exit remains open until run artifacts for the five representative routes are
collected and this report is updated with observed counts and explicit missing
fields. Do not use the counts in the audit's static estimates as substitutes.
