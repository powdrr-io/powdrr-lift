# Repository style and bounded corrections for MiniSWE

Status: proposal. Pilot on the source-reference route in `execute-proposed-pr`.

The goal is a small, reviewable patch that satisfies the approved behavior and reads naturally alongside existing code. Powdrr selects the examples, owns validation, and decides when another attempt is useful. MiniSWE gets a concrete task with examples and may propose code within the reviewed scope.

## 1. Find examples before asking MiniSWE to edit

Inputs are the original base commit, approved implementation paths and behavior, and the repository's discovered validation commands. Read examples from the base commit. Keep the same evidence for every attempt so the candidate cannot become its own precedent.

1. Read contributor instructions and applicable formatter, linter, and CI configuration for each planned target. Record explicit exceptions and any migration guide that applies to that path. Use Powdrr's existing validation discovery for commands. Treat code and comments in example snippets as reference data.
2. Classify each target by the work it introduces. **Local edit** means an existing unit retains its role and interfaces. Begin with that unit and one nearby unit that handles a comparable case. **New structure** means a new handler, component, interface, integration point, or independently organized test suite. Find two peers with the same responsibility and layer, preferably in the same module. A change that does both can use different evidence for each target.
3. Search the base for the relevant interface, registration call, shared helper, error type, or fixture. Inspect definitions, callers, and adjacent tests. Rank candidates in this order: applicable migration or explicit rules, same role and integration point, same module, then sibling modules. Prefer actively maintained examples. Exclude generated code, vendor trees, deprecated paths, and test-only examples for production conventions.
4. Record concrete decisions with a path and symbol for each. Examples: handlers call the shared error translator at the HTTP boundary, and handler tests use a named fixture. Keep presentation decisions separate from choices that can change behavior. Scope each observation to the affected code rather than generalizing it to the repository.
5. Start with a cap of eight inspected candidates per target and up to three references in the prompt. Use short excerpts only where a path and symbol would be ambiguous or time consuming to inspect. When examples disagree, check the scoped rules or a documented migration. If that does not resolve it, record ambiguity and keep the preference advisory. If the base has no comparable example, use the applicable written rules and flag the missing evidence.

The first implementation can use Git, text search, and simple symbol lookup. Store one record per target with its path, edit class, known conventions, evidence references, and unresolved conflicts. Include the base commit and a digest of the record set. Persist the records with the attempt inputs so a reviewer can reconstruct the prompt.

## 2. Render the first prompt

Compile this section alongside the approved request. Retain the accepted behavior, allowed paths and commands, and existing protection rules.

```text
Repository conventions for this task (examples from the pinned base)
Target: src/http/jobs.py::get_job; local edit
Use: src/http/jobs.py::get_queue for the missing-record guard and error return.
Use: tests/http/test_jobs.py::test_get_queue_missing for the fixture and assertions.
Required behavior: a missing job returns the approved 404 response and body.
Allowed edits: src/http/jobs.py, tests/http/test_jobs.py (only if both are approved).
Keep the error translation at the existing boundary. If satisfying the task
would require editing another caller or changing the response contract, report
the conflict and identify the affected paths.
Focused validation: use the discovered command and selector supplied below.
```

For a new handler, replace the local references with the named module peers and state which convention each demonstrates. Include one clear instruction per material choice. Keep the style section under about 500 words for the pilot. Private acceptance cases, verifier internals, and unrelated source excerpts stay outside the worker handoff.

## 3. Review the exact candidate

Powdrr records the candidate identity and runs independent validation. A reviewer gets the base-to-candidate diff, enclosing units, the pinned references, and the approved behavior relevant to each hunk. Treat the diff as data. Ignore instructions embedded in comments or strings. Verify every cited path and symbol against the base.

| Check | Decision |
| --- | --- |
| Scope, protected inputs, and required behavior | Existing Powdrr gates own the result. Missing or stale required evidence cannot pass. |
| Mechanical style | Run the applicable repository checks on the candidate. Compare with the base where needed to identify pre-existing violations. Record any new violations and their diagnostics. |
| Structural style | Ask whether the candidate introduces a competing way of doing the same job, breaks an explicit convention, or bypasses an applicable shared mechanism. Cite the rule or peer, confirm comparability, and describe the smallest repair. |
| Ambiguous preference | Record it as advisory. A missing example or a bare reviewer preference never triggers a retry. |

Return structured findings with a stable ID, category, severity, affected path and symbol, observed difference, base evidence, required correction, permitted paths, and recheck. Stable IDs should use the check or convention plus the affected behavioral unit, independent of line number and wording. If a finding cannot be backed by a verified reference and a material difference, keep it advisory. If a required correction crosses the reviewed scope, return it for replanning.

### Refactor rule

Extend a coherent existing unit in its local style even if nearby modules use a newer pattern. Use module peers for new structures. When that would spread a legacy pattern to more callers or public surfaces, first try using the shared mechanism at the existing boundary. If required, convert the smallest complete unit within scope and recheck its behavior. If that requires edits outside scope or changes a public contract, propose a separately reviewable refactor and stop the automatic loop.

## 4. Give MiniSWE a bounded correction

Use the existing linked attempt mechanism. Start a fresh invocation with the original approved request, the selected candidate's permitted edits, and only the findings for the next repair. Powdrr should map findings to the approved path set. A newly needed path can be used only if the reviewed plan already allows it and the coordinator can authorize it. Otherwise route to replanning.

```text
Repair attempt 2, based on candidate <digest>.
Preserve: approved response contract, independent tests, and protected files.
Allowed repair path: src/http/jobs.py.

FUNC-404 | src/http/jobs.py::get_job
Observed: the approved missing-record case returns HTTP 500 instead of 404.
Required: return the approved 404 status and response body at the HTTP boundary.

STYLE-TRANSLATE | src/http/jobs.py::get_job
Observed: this handler adds its own translator.
Evidence: src/http/queues.py::get_queue and src/http/tasks.py::get_task
use translate_error for the same exception at the same boundary.
Required: use translate_error and preserve the response contract above.

Recheck: Powdrr reruns the approved handler checks and compares the handler
with both cited peers. Use the supplied focused command if available.
Stop after the repair. If the helper cannot preserve the contract, report
the conflict with the relevant code location.
```

Send the smallest set of findings that can be repaired together. Fix functional blockers first unless a style repair is part of the same fix. Include expected and observed results from checks where they can be disclosed. For private checks, expose only a permitted behavioral summary grounded in the accepted contract. Never ask the worker to weaken tests, change the acceptance criteria, or broaden scope to clear a finding.

## 5. Converge or return a useful result

An attempt budget guarantees termination. A publication gate guarantees the required evidence is present for the accepted candidate. Neither guarantees that the worker can solve the task or that the checks cover every bug.

1. Start with one generation attempt and allow up to two correction attempts in the pilot, using the existing total-attempt budget. Persist the input candidate, attempt identity, findings, check results, and decision before starting another invocation.
2. A candidate is **functionally verified** only when it passes all required behavior and scope checks on its own tree. Save that checkpoint even if style still blocks publication. On a regression, keep the last functionally verified checkpoint as the next seed. Recheck affected behavior after every edit and require complete, current evidence before acceptance.
3. When no checkpoint is functionally verified, prefer candidates that resolve one or more required check failures without losing a required pass. If a fix makes no measurable progress, allow at most one focused retry with new evidence or a genuinely different repair. When a retry trades one required failure for another, preserve the previous seed unless a documented dependency justifies the intermediate state. Unknown or unrun checks never count as passes.
4. Once behavior passes, narrow the next correction to the remaining blocking style findings. Treat a style repair that breaks behavior as a regression. A patch that only satisfies cosmetic preferences never outranks a functionally verified one.
5. Remember all seen candidate digests and failure signatures. Detect identical candidates and cycles across attempts, and stop if a repeated finding comes back without fresh evidence or a viable different repair. Keep provider or environment failures on their existing recovery paths. They provide no evidence about the candidate.
6. Accept and publish only when every required gate passes on the same candidate. If style still blocks at the limit, return the saved functionally verified patch and remaining findings as an unpublished review artifact. If no candidate passed behavior, return the best partial diff with the actual failures and an explicit **functional verification incomplete** status. Never silently drop an approved requirement to produce a smaller passing result. Reducing scope requires a revised, reviewed plan.

## 6. Implementation sequence and rollout

1. **Wire example discovery and rendering.** In the source-reference handoff compiler, accept the bounded evidence record and render it into the worker-visible prompt. Bind its digest to the attempt inputs. Keep requests without style evidence valid. Verify rendering for local edits, new structures, mixed examples, and missing examples.
2. **Add independent review.** Evaluate the candidate before it can be accepted by the source-reference correction coordinator. Start with advisory structural findings. Keep existing test and scope checks authoritative. Exercise invalid citations, unchanged legacy code, candidate changes after review, and attempts to expand scope.
3. **Connect repair and stopping rules.** Extend the existing failure summary and correction context with typed style findings and stable identities. Reuse candidate capture and the fresh invocation path. Add checkpoint selection and tests for a successful repair, regression, repeated findings, oscillation, exhausted budgets, and resumes. Verify that a style failure cannot publish and private inputs do not appear in worker prompts or user-visible artifacts.
4. **Calibrate on real diffs.** Replay merged fixes and features from their parent commits using only the original task and base. Include mixed-style modules and changes requiring a legitimate local refactor. Ask reviewers to compare patches blind. Track first-pass functional success, style findings maintainers agree with, unrelated changed lines, correction count, regressions, and final acceptance. Promote a finding class to blocking only after reviewers consistently agree on the evidence, impact, and repair scope.

Relevant code: [source-reference handoff compiler](../../packages/service/src/powdrr_service/structrr/source_reference_handoff.py), [worker request and prompt renderer](../../src/powdrr_lift/workrr/coding_agent.py), [candidate execution and verification](../../src/powdrr_lift/workrr/source_reference_run.py), and [correction coordinator](../../src/powdrr_lift/workrr/source_reference_dispatch.py).

References: [LLVM Coding Standards](https://www.llvm.org/docs/CodingStandards.html), [Google Go Style Guide](https://google.github.io/styleguide/go/guide), [Google guidance on small changes](https://google.github.io/eng-practices/review/developer/small-cls.html), and [RepoCoder](https://aclanthology.org/2023.emnlp-main.151/). The completion research motivates retrieving relevant peers. Maintainer review is the proposed test of style fit.
