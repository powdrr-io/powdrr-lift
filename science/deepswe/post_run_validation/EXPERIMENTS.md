# Planned experiments: independent post-run validation

## Claim to test

Question-led instruction analysis can produce a more complete and more
actionable audit than a broad “did the agent follow the instructions?” review.
When paired with candidate-specific evidence, it can catch missed requirements
and instruction violations without accepting the coding agent's own account as
proof.

No experiment in this file has been run. The thresholds below are proposed
before data collection so results can be judged against a fixed bar.

## E0: Build a human-reviewed obligation and outcome set

**Purpose:** establish a reference set independent of generated checklists and
reviewer outputs.

**Tasks:** begin with the four tasks under
`science/deepswe/acceptance_templates/data/inputs/`, which already have
instruction-based reference validations. Add tasks from
`science/classifications/root_disposition/data/task_runs/` to include explicit
scope, process, branch, commit, and verification instructions.

**Procedure:** two reviewers independently decompose each original instruction
without seeing generated prompts, candidate changes, solution patches, or test
patches. They record source spans, atomic obligations, normative strength,
obligation kind, conditional applicability, a check question, plausible evidence
routes, and what evidence would justify pass, fail, or unknown. Resolve
disagreements before using these labels. Record any use of existing
agent-authored reference validations separately; do not silently promote those
labels to human gold.

**Deliverable:** a versioned obligation set and adjudication log. Keep at least
one repository family out of prompt development for a task-family holdout.

**Gate:** do not score checklist-generation methods against model-authored
reference labels alone. If two reviewers cannot agree on an obligation or its
observability, label it ambiguous and keep it out of strict accuracy scoring.

## E1: Which question style finds the right obligations?

**Input:** original instruction only, with repository instructions supplied as
a separately identified source. No candidate code, agent report, tests, or
patches.

**Arms:**

| Arm | Method |
| --- | --- |
| A: broad | Ask whether the completed task followed the instructions; request a concise checklist. |
| B: direct | Ask for acceptance criteria in declarative form. |
| C: question-led | For each instruction clause, ask what separate observable questions and evidence routes are needed to judge compliance. |

Use the same model, context, and output budget for all arms. Randomize task IDs
and arm order. Do not let one arm see another arm's output. Run once on the
development tasks, revise the prompts only there, then run the frozen prompts
on the held-out repository family.

**Score against E0:** atomic obligation recall and precision, unsupported
obligations, merged independent obligations, lost conditions or normative
strength, duplicate checks, evidence-route accuracy, and `unknown` use. Also
record obligation count, output size, and call cost.

**Primary comparison:** C versus B on held-out atomic recall at matched
unsupported-obligation rate. A is a deliberately weak baseline, not a target
for a production gate.

**Proposed gate:** C must improve held-out atomic recall by at least 10
percentage points over B, while unsupported obligations rise by no more than 3
points and precision remains at least 90%. If the sample is too small for a
stable estimate, report intervals and expand the task set rather than claim a
win.

## E2: Can the evidence route catch planted violations?

**Input:** candidate revisions with baseline and candidate identifiers,
complete diffs, and captured verification commands and results. The obligation
set must be frozen before exposing candidates to reviewers.

**Build cases:** for each applicable obligation, include a compliant candidate
and at least one candidate with a planted violation. Derive mutations from the
obligation itself: remove a required export, alter an observable outcome, break
a prohibition, add an unrelated change, skip a requested check, or falsify a
process condition such as the target branch. Keep positive controls and
negative controls in each task. A human reviewer records the expected outcome
and confirms that the supplied evidence can reveal it.

Do not treat test names, patch intent, or seeded-mutant metadata as reviewer
evidence. The reviewer receives only the frozen obligation, source instruction,
candidate-bound evidence, and evidence-route description. The evaluator
retains the planted label separately.

**Routing policies to compare:**

1. Diff-only review for all obligations.
2. Test-only review for behavioral obligations.
3. Question-led route: structural checks for presence/scope, executable checks
   for behavior, repository metadata for process requirements, and isolated
   semantic review where deterministic evidence cannot decide.
4. Question-led route with all evidence presented to one holistic reviewer.

**Score:** obligation-level violation recall, false-pass rate, false-fail rate,
unknown rate, evidence citation validity, stale-evidence rejection, and cost.
Report each obligation kind separately; an overall score can hide a route that
never checks process instructions or prohibitions.

**Proposed gate:** policy 3 reduces false passes by at least 25% relative to
policy 4, with false failures increasing by no more than 5 points. Missing or
stale candidate evidence must never receive a pass. If policy 3 cannot beat
diff-only or test-only on the obligation types they cover, keep the simpler
checker for those types.

## E3: Does asking one evidence question at a time improve review?

**Input:** the same frozen obligations and candidate evidence from E2.

**Arms:**

| Arm | Review prompt |
| --- | --- |
| A: holistic | “Did the implementation follow this instruction?” with full diff and test output. |
| B: checklist | Ask for pass/fail/unknown for the full obligation list in one response. |
| C: isolated | Ask one focused question per obligation; show only evidence for that obligation; aggregate verdicts in code. |

Use an independent reviewer model where possible. Hide the coding agent summary
in all primary arms. Add a diagnostic ablation that reveals it to measure
whether it biases verdicts. Randomize case order and blind task and mutation
labels.

**Score:** false-pass rate is primary. Also score planted-violation recall,
false-fail rate, agreement with E0 outcomes, `unknown` calibration, citation
quality, and cost. Manually inspect every false pass and a sample of correct
passes. Treat unsubstantiated citations as unsupported evidence, even when the
verdict happens to match the label.

**Proposed gate:** C reduces false passes by at least 20% versus A and B on the
held-out cases, while false failures do not rise by more than 5 points. Summary
exposure must not materially improve apparent accuracy; if it does, investigate
whether the reviewer is echoing agent claims instead of examining evidence.

## E4: Shadow review on real agent runs

**Purpose:** check whether controlled-mutant performance transfers to normal
coding runs.

Capture at least 20 completed task runs across multiple repository families:
original instruction, frozen obligation inventory, baseline and candidate
revision, complete diff, and command/result transcript. Run the question-led
review without blocking or steering the coding agent. Two human reviewers then
audit every flagged failure and a random sample of passes and unknowns, without
seeing model verdicts until their independent labels are recorded.

Report reviewer precision, missed issues in audited passes, unknown rate,
human review time, model cost, and findings by obligation kind. Preserve model,
prompt, candidate, diff, and evidence fingerprints. Do not claim population
accuracy from this convenience sample; use it to find failure modes and size a
later holdout.

## Execution order and current blockers

1. Complete E0 before comparing prompts.
2. Run E1 on development tasks, freeze the prompts, then score held-out tasks.
3. Collect candidate revisions and transcripts; the current saved acceptance
   template runs do not include them.
4. Create and human-check E2 controls before running E2 and E3 reviewers.
5. Run E4 only after the controlled cases meet their gates.

The immediate data gap is candidate-run evidence. Existing instructions and
reference validations are enough to start E0 and E1, but not enough to claim a
post-run validation result. Until candidate runs are captured, this track should
report checklist discovery results only.
