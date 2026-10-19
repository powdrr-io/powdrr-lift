# Prompt grader and benchmark feedback plan

## Objective

Add a reusable evaluation for the exact worker prompt generated from a task's
instructions, and connect that evaluation to benchmark validation when the same
run continues through implementation. The evaluation must produce a final
0–100 prompt score with criterion-level findings. Benchmark results must then
show where the prompt and implementation validation agree or diverge, preserve
the evidence for those findings, and identify whether the next improvement
belongs in prompt generation, the rubric, or worker execution.

This is an implementation plan. It does not authorize starting benchmark coding
runs as part of implementing the grader. Prompt capture and grader development
can run without a coding worker. Benchmark comparisons use existing benchmark
runs or runs explicitly requested for that purpose.

## Current implementation to extend

The repository already has a prompt-only capture mode and
`evaluate-deepswe-prompt`. The evaluator checks run/task identity, instruction
ledger text, captured prompt hashes, and rubric anchors, then asks a judge to
grade each criterion and quote candidate-prompt evidence. The current evaluator
is DeepSWE-specific: it expects `task.toml`, solution and verifier patches, and
task-specific rubric files. The existing baseline records 100% rubric coverage
for three prompts, while correctly making no claim about coding-worker outcomes
because no worker was run.

Extend the existing capture and evaluation path rather than creating a second
prompt pipeline. Preserve the current provenance checks and offline-only use of
reference solutions and verifier patches. Production generation must never see
benchmark solutions, verifier patches, reference rubrics, or ideal prompts.

Relevant implementation and evidence:

- `src/powdrr_lift/workrr/deepswe_design_evaluation.py`
- `src/powdrr_lift/cli.py` (`evaluate-deepswe-prompt` command)
- `docs/evaluations/deepswe-design-evaluation.md`
- `docs/evaluations/semantic-prompt-baseline-2026-10-04/README.md`
- `docs/evaluations/deepswe-python-statemachine-state-data.yaml`
- `docs/evaluations/deepswe-ytt-jsonpath-query-api.yaml`
- `docs/evaluations/deepswe-skrub-duration-encoding.yaml`

## Design principles

1. **Grade the actual worker boundary.** Evaluate the exact prompt bytes sent to
   each worker request, identified by request ID and SHA-256. Do not grade an
   earlier design document or a reconstructed prompt.
2. **Separate prompt quality from implementation success.** Prompt score and
   benchmark validation score are distinct measurements. Never average them
   into one blended score.
3. **Make every judgment auditable.** Each criterion finding includes its
   source requirement, verdict, evidence quote from the prompt, concise reason,
   rubric version, judge identity, and any mapped validation results.
4. **Use code for deterministic facts and an LLM judge for semantic questions.**
   Hashes, source anchors, expected literal claims, forbidden literal claims,
   identity, and validation result parsing are deterministic. Requirement
   meaning, contradiction, unsupported narrowing, and actionability need a
   semantic judge.
5. **A high average cannot hide a critical defect.** Report a numeric score and
   an explicit gate status. A critical criterion that is missing,
   contradicted, or unresolved fails the gate regardless of the score.
6. **Treat ambiguity honestly.** `ambiguous` is not a pass. It scores zero for
   the initial gate and is separately visible so reviewers can distinguish an
   unclear prompt from a failed or malformed judge call.
7. **Learn from real failures without leaking answers into generation.**
   Benchmark outcomes may inform offline rubric and generator improvements;
   reference answers remain outside the production prompt-generation context.

## Evaluation inputs and versioned records

### Prompt evaluation case

Generalize the evaluator to accept a versioned case/run bundle containing:

- Task or case ID and source instruction fingerprint.
- Captured worker prompt index and exact prompt files, including provider,
  request ID, and prompt fingerprint.
- A versioned rubric with stable criterion IDs and an importance level.
- Optional offline reference anchors used to author and audit the rubric, such
  as solution symbols and verifier test names. These anchors validate rubric
  freshness; only the minimum selected evidence needed for a criterion is sent
  to the judge.
- Powdrr revision, prompt-generation configuration fingerprint, capture mode,
  and capture timestamp when available.

Rubrics should contain atomic criteria. Each criterion records the exact source
excerpt (or stable source-clause ID plus fingerprint), expected prompt behavior,
importance (`critical` or `important`), dimension, required prompt claims,
forbidden prompt claims, and optional validation test IDs. Source excerpts
must be checked against the exact instruction. Stale excerpts, missing prompt
files, mismatched hashes, and task identity mismatches are evaluation errors,
not low prompt scores.

Initial rubric dimensions:

- **Coverage:** required behaviors and applicable cases appear in the prompt.
- **Faithfulness:** wording preserves the source's conditions, scope,
  alternatives, and requirements that apply together.
- **Grounding:** the prompt does not turn unspecified behavior into a
  requirement or claim unsupported behavior as required.
- **Actionability:** the worker can identify the operation and observable
  behavior expected; checks distinguish important incorrect implementations.
- **Consistency and scope:** prompt sections agree, product obligations remain
  distinct from process/delivery instructions, and unrelated work is not added.
- **Clarity:** the prompt can be followed without ambiguity that changes
  implementation choices. Style/readability should be diagnostic until
  separately calibrated against outcomes.

### Criterion verdicts

For each criterion and each relevant worker prompt, the judge returns exactly
one verdict:

- `supported`: the prompt states the expected behavior clearly and does not
  weaken or contradict it elsewhere.
- `missing`: the prompt does not state the behavior.
- `contradicted`: the prompt states incompatible or weakened behavior.
- `ambiguous`: the prompt evidence does not support a reliable decision.

The response also includes an exact quote from the candidate prompt for
`supported` and `contradicted`, plus a short reason. A quote that is not present
verbatim in the hashed prompt invalidates the finding as `ambiguous`. The judge
must review all relevant prompt sections; one correct sentence does not
override a conflicting instruction elsewhere.

The deterministic evaluator should handle exact absence checks and other
mechanical rubric checks without judge calls. Judge/API errors, malformed JSON,
and timeouts are recorded as evaluation errors and are not converted into
prompt defects or silent passes.

## Final prompt score and gate

The report exposes a single headline **Prompt score: 0–100**, plus the scoring
details and a separate gate result.

For the initial scoring contract:

- `supported` earns the criterion's full weight.
- `missing`, `contradicted`, and `ambiguous` earn zero.
- `critical` criteria have weight 3; `important` criteria have weight 1.
- `prompt_score = round(100 * supported_weight / total_weight)`.
- `gate = pass` only if every critical criterion is `supported` and there are
  no evaluation errors. Otherwise `gate = fail` or `gate = error`, respectively.
- Report weighted coverage, supported/total criterion counts, all critical
  failures, per-dimension scores, and the unweighted verdict counts alongside
  the headline score.

Do not use partial credit for `ambiguous` in v1. A score must not hide
uncertainty behind fractional points. If criteria or dimensions are not
applicable, exclude them only through explicit versioned rubric configuration;
never skip them dynamically based on a judge response. Any later change to
weights or verdict mapping requires a score schema/version change and a
recomputed baseline.

## Benchmark comparison when implementation continues

### Required validation linkage

For benchmark runs that proceed beyond prompt capture, preserve a stable mapping
from rubric criteria to benchmark validation tests or named validation
obligations. A criterion may map to several tests, and a test may cover several
criteria. Record the mapping in the rubric or a versioned benchmark adapter;
do not infer it from test names after a failure.

After benchmark validation finishes, append or link a benchmark comparison
section to the prompt evaluation report. It records:

- Benchmark/task identity, run ID, Powdrr and worker versions, and prompt
  fingerprints used by that run.
- Validation command/profile and whether validation completed successfully.
- Per-test or per-obligation result (`passed`, `failed`, `not_run`, or
  `infrastructure_error`) with concise failure evidence and artifact links.
- Per-criterion mapped validation summary, including the list of test IDs and
  their individual outcomes.
- Validation pass rate over tests actually run, with `not_run` and
  infrastructure failures reported separately and excluded from the test pass
  denominator.
- The prompt score and gate copied from the immutable prompt-evaluation result,
  not recalculated after seeing benchmark outcomes.

Keep the prompt report immutable after prompt grading. Store the benchmark
comparison as a linked, fingerprinted report or as an append-only result object
that references the prompt report fingerprint. This preserves what was known
when the prompt was graded and prevents outcome-informed rubric edits from
rewriting historical scores.

### Shortcoming classification

Generate a criterion-level comparison matrix and use these initial
interpretations as review prompts, not automatic root-cause assertions:

| Prompt finding | Validation | Initial interpretation |
| --- | --- | --- |
| Missing or contradicted | Mapped tests fail | Candidate prompt-generation gap; inspect whether the rubric captured the source requirement correctly. |
| Supported | Mapped tests fail | Worker implementation, tool use, or validation gap; inspect the run trace before changing prompt generation. |
| Missing or contradicted | Mapped tests pass | Prompt defect without observed benchmark impact, or a stale/overstrict rubric; review before changing generation. |
| Supported | Mapped tests pass | Prompt and implementation agree for this criterion and run. |
| Any verdict | Relevant failed test has no rubric mapping | Rubric coverage gap; add or revise criteria and mappings after human review. |
| Any verdict | Tests were not run or infrastructure failed | Inconclusive; do not attribute to prompt quality or worker behavior. |

The report may recommend areas for investigation, but must not automatically
rewrite production prompts, rubrics, or generation rules from one benchmark
outcome. Human-reviewed shortcomings become versioned eval cases and regression
examples. Store the accepted diagnosis, supporting trace/test evidence, and
which artifact should change (generator, rubric, worker/runtime, or validation).

## CLI and artifacts

Generalize the existing prompt evaluation command rather than removing its
DeepSWE interface. The generalized command should accept a captured run bundle,
rubric, optional benchmark task/reference bundle, report path, and judge
configuration. Retain a compatibility path or alias for current
`evaluate-deepswe-prompt` users.

Suggested commands and workflow:

```text
powdrr-lift evaluate-prompt --run-dir RUN --rubric RUBRIC --report REPORT
powdrr-lift compare-prompt-benchmark --prompt-report REPORT --benchmark-run RUN
```

The final command names are subject to the existing CLI conventions discovered
at implementation time. Prompt-only runs stop after the first command. Runs
that continue into the benchmark invoke the comparison only after validation
artifacts are finalized.

The prompt report should contain:

- Versioned schema and scoring contract identifiers.
- Run, task, rubric, source, prompt, evaluator, and judge fingerprints/IDs.
- Headline prompt score and gate, dimension scores, and verdict counts.
- Findings with source anchors, exact candidate-prompt evidence, verdict,
  reason, importance, and criterion weight.
- Judge/infrastructure errors and deterministic check results.
- Benchmark comparison reference, when available.

The benchmark comparison report should reference the prompt report fingerprint
and preserve validation artifacts and criterion/test mappings. Summary text
printed by the CLI must include the 0–100 score, gate, critical failure count,
and—when present—the benchmark pass rate and unresolved/infrastructure counts.

## Implementation sequence

Keep this work reviewable in staged PRs. Each stage must integrate with the
actual capture/evaluation flow and include its own evidence; do not stop after
adding schemas or report models that no command uses.

### Stage 1: Score and report contract

- Version the prompt score, weight mapping, dimension breakdown, gate, and
  finding fields.
- Add deterministic score calculation and report validation to the current
  evaluator.
- Preserve current task-specific rubric inputs and compatibility behavior.
- Add tests for all verdict/weight combinations, critical gating, malformed
  evidence quotes, evaluation errors, and stable report fingerprints.
- Recompute the checked-in baseline reports or add a compact score summary
  that identifies the exact rubric/evaluator revisions.

**Exit evidence:** current prompt-only captures produce a complete report with
a 0–100 score, dimension details, criterion evidence, and a separate gate. The
score is reproducible from report findings without another model call.

### Stage 2: Generalized prompt eval cases and rubric authoring

- Separate generic run/prompt loading and score aggregation from DeepSWE task
  discovery and file naming.
- Define and validate a versioned generic rubric/case format, including exact
  source anchors, importance, dimension, forbidden/required claims, and
  optional validation mappings.
- Preserve stale-anchor checks, task/source identity checks, and prompt hashes.
- Add rule-based checks where deterministic; retain the evidence-quoting LLM
  judge for semantic findings.
- Keep reference solutions and verifier files restricted to offline rubric
  validation and selected judge evidence.

**Exit evidence:** both the existing DeepSWE command and a generic prompt eval
command run through the same evaluator core and emit the same report schema.
At least one non-DeepSWE fixture exercises the generic input path.

### Stage 3: Benchmark result ingestion and criterion comparison

- Identify the benchmark validation artifact formats already emitted by the
  supported run adapters.
- Add a normalized validation-result reader that distinguishes test failure,
  not-run, and infrastructure failure.
- Join results to criteria only through declared stable mappings.
- Emit criterion/test matrix, benchmark pass rate, unmapped failures, and the
  interpretation categories above without automatically diagnosing root cause.
- Link the immutable prompt report fingerprint and benchmark run provenance.

**Exit evidence:** a fixture with a mixed validation run (pass, fail, not-run,
infrastructure error, and unmapped failure) produces the expected comparison
without counting unrun or infrastructure-failed tests as product failures.

### Stage 4: Feedback corpus and calibration

- Add a versioned feedback record for human-reviewed shortcomings, with
  evidence, diagnosis, affected criterion/tests, owner area (generator, rubric,
  worker/runtime, validation), and follow-up regression case.
- Seed with existing prompt failure cases and available benchmark artifacts;
  do not fabricate outcome labels when no worker/validation result exists.
- Keep a held-out task/domain split for grader checks and generator comparisons.
- Review a sample of judge outputs against human labels; report precision/recall
  or agreement for supported-vs-not-supported and critical defects, plus
  disagreement examples.
- Re-run selected examples more than once to measure judge stability. Track
  provider/model/version and cost. Keep repeated-run statistics separate from
  the deterministic headline score for a single captured prompt.

**Exit evidence:** known human-labeled omissions, contradictions, and supported
cases are reliably distinguished on development and held-out cases; repeated
judge disagreement is visible; each failure is retained as a regression case.

### Stage 5: Operational rollout

- Run the grader on prompt-only captures first; report score and findings as an
  artifact without blocking benchmark execution during initial calibration.
- After calibration, optionally gate on evaluation errors and critical prompt
  failures. Keep the gate configurable and make the reason visible.
- For benchmark runs, write the comparison after validation completes and
  attach both reports to the run artifacts.
- Add regression comparisons for prompt-generation changes and judge/model
  changes. A grader change must not silently rewrite historic scores.

**Exit evidence:** a prompt-only run and a full benchmark run both produce
linked, reproducible reports; prompt and validation findings can be traced to
the exact source, prompt, rubric, and benchmark artifacts.

## Validation strategy

Implementation verification should cover:

1. Unit tests for rubric schema, deterministic checks, score calculation,
   critical gate, report fingerprints, validation parsing, and mapping joins.
2. Evaluator tests with a scripted judge for supported, missing, contradicted,
   ambiguous, malformed, and failed calls; assert evidence quotes are exact.
3. Prompt-capture end-to-end test proving the grader consumes the exact
   provider-ready prompt indexed by the run and rejects a changed prompt hash.
4. Benchmark comparison end-to-end fixture proving validation results map to
   the matching prompt evaluation and preserve failed/not-run/infrastructure
   distinctions.
5. Replay of the existing three task captures and known prompt failure cases.
   Report score changes with rubric/evaluator revisions; do not treat a score
   increase alone as proof the generator improved.
6. A small set of real benchmark runs, when explicitly authorized, comparing
   prompt findings with verifier outcomes. Analyze disagreement and revise
   rubrics or generation only after inspecting trace and test evidence.

The central success measure is not a high prompt score by itself. Success means
that criterion findings are reproducible and evidence-backed, critical prompt
defects are detected, and post-benchmark comparisons help humans locate the
right improvement target without confusing prompt omissions with worker or
infrastructure failures.

## Risks and mitigations

- **Rubric overfit:** task-specific reference knowledge can make a grader look
  strong while missing other domains. Use atomic rubrics, diverse failure cases,
  and held-out tasks/domains.
- **Judge self-confirmation:** a judge may accept plausible paraphrase or miss a
  contradiction elsewhere. Require exact quotes, whole-prompt review, human
  sampling, and adversarial regression cases.
- **Score gaming:** optimizing one aggregate can sacrifice critical behaviors.
  Keep critical gating and per-dimension/per-criterion details visible.
- **Misattributed failures:** a clear prompt does not guarantee a correct
  implementation. Use separate prompt and benchmark measures and inspect
  trajectories before assigning a cause.
- **Incomplete validation:** tests may not cover every rubric criterion.
  Declare mappings, surface unmapped failures, and report partial validation
  rather than implying full coverage.
- **Infrastructure noise:** provider outages and test setup failures can look
  like product defects. Classify them separately and mark the comparison
  inconclusive when required evidence is unavailable.
- **Benchmark answer leakage:** reference solutions and verifier patches can
  contaminate generation. Keep them in offline eval and post-run comparison
  codepaths only.
- **Cost and latency:** LLM judging can be expensive. Use deterministic checks
  first, cache by all source/prompt/rubric/judge fingerprints, and begin with
  criterion-level calls only where semantic judgment is needed.

## Source

The approach follows the distinction in Yesha Shah's [Agentic Evals: A
Software Engineer's Guide, Part 1](https://www.linkedin.com/pulse/agentic-evals-software-engineers-guide-part-1-yesha-shah-sdjle):
use deterministic autoraters for mechanical facts, evidence-backed LLM judges
for judgment calls, versioned eval data, and end-to-end outcome evidence. Here,
prompt evaluation is one stage measurement; benchmark validation supplies the
separate outcome signal needed to judge whether that measurement predicts
useful implementation results.
