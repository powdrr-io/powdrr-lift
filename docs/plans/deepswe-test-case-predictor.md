# DeepSWE Test Case Predictor: Agent Instructions

Status: proposed

## Objective

Build a predictor that reads a DeepSWE task's instructions and available validation information, then predicts the test cases that would verify the requested behavior. Focus on observable inputs, actions, and expected outcomes. Do not use or analyze solution code.

The first milestone is a reproducible baseline and an evaluation report. Do not train a specialized model until the baseline has been measured and the available ground truth has been audited.

## Scope and boundaries

Use as predictor inputs only information available before an implementation attempt:

- The original task instructions, exactly as presented to the coding agent.
- Validation commands, framework names, and validation configuration supplied with the task or discoverable from its starting repository.
- Explicit acceptance criteria or test requirements included in the task instructions.

Keep post-implementation data out of predictor inputs. Patches, solution code, agent plans, generated specifications, validation failures from an implementation attempt, and verifier results may be used only to construct or evaluate ground truth. Record the origin of every input so this boundary can be checked.

Predict **test scenarios**, not exact test function names. A scenario states the condition, action, and observable result that a test would assert. Exact test names may be attached during evaluation if available.

## Repository starting points

- `src/powdrr_lift/integrations/harbor/powdrr_agent.py` receives Harbor's task instruction and passes it to the Powdrr feature flow.
- `src/powdrr_lift/structrr/validation.py` discovers validation commands from repository configuration and CI files.
- `src/powdrr_lift/core/instruction_ledger.py` provides a way to preserve and identify instruction clauses.
- `src/powdrr_lift/core/behavior_contract.py` defines a richer behavior-scenario structure. Reuse its ideas where useful, but keep the predictor's first output schema small.
- `docs/harbor/deepswe-python-statemachine-state-data-scoping.md` describes a task where concrete API requirements were present in the instruction but lost during planning. Use it as a qualitative example, not as the sole evaluation set.

DeepSWE/Pier runs may contain `verifier/ctrf.json` with individual test names and statuses, `verifier/test-stdout.txt` with test output, and `verifier/reward.json` with aggregate counts. Audit the actual files before writing a parser. Do not assume every run has all three files or that the same format applies to every task.

## Work sequence

### 1. Audit the available task data

Locate the DeepSWE task directories and any existing Pier run artifacts. Make an inventory with one row per **unique task**, including:

- Task ID and repository identity.
- Path and format of the original instruction.
- Available validation configuration and commands.
- Available sources of individual verifier test cases.
- Number of feature tests, if known.
- Whether test names or output describe the asserted behavior clearly enough to label it.

Treat multiple runs of one task as one task for dataset size and train/evaluation splitting. Runs can help recover missing artifacts or statuses; they are not independent examples. If the available tasks are too few for a meaningful quantitative result, report that explicitly and proceed with a small qualitative pilot.

### 2. Define a compact data format

Create a versioned, machine-readable record for each task. Separate predictor inputs from ground truth. A suggested shape is:

```json
{
  "schema_version": "deepswe-test-prediction-task-v1",
  "task_id": "example-task",
  "repository_id": "example-repository",
  "input": {
    "instruction": "Original task text",
    "validation": [
      {"name": "pytest", "command": ["pytest", "-q"], "source": "pyproject.toml"}
    ]
  },
  "ground_truth": {
    "availability": "individual_tests",
    "cases": []
  },
  "provenance": {
    "instruction_source": "path or artifact ID",
    "validation_sources": ["path or artifact ID"],
    "ground_truth_sources": ["path or artifact ID"]
  }
}
```

Use an explicit availability value when only aggregate counts or no verifier output exists. Never turn an absent test report into an empty ground-truth case list. Preserve raw artifact references so labels can be audited.

### 3. Build the first predictor

Implement a baseline that takes one task's `input` object and returns a ranked list of scenarios. An LLM with a fixed prompt is sufficient for the first version. The prompt should:

1. Extract each distinct requirement from the instruction, including named APIs, supported inputs, output shape, error behavior, state transitions, and compatibility requirements.
2. Expand each requirement into the smallest useful set of observable test scenarios: a normal case, relevant boundary or error cases, and interactions explicitly implied by the instruction.
3. Avoid inventing behavior that cannot be supported by the instruction or validation context. Mark reasonable but unstated cases as inferred.
4. Rank cases by how directly the instruction supports them and how likely they are to be checked.
5. Return structured JSON only.

Use a compact prediction schema such as:

```json
{
  "schema_version": "deepswe-test-predictions-v1",
  "task_id": "example-task",
  "cases": [
    {
      "id": "pred-001",
      "subject": "State(data=...) constructor",
      "given": "a mapping of initial values",
      "when": "a State is created with data=",
      "then": "the state exposes those initial values",
      "category": "normal",
      "instruction_evidence": ["exact instruction excerpt or clause ID"],
      "basis": "explicit",
      "confidence": 0.9
    }
  ]
}
```

Allow `basis` values `explicit` and `inferred`. Require every prediction to cite an instruction excerpt or clause ID; for an inferred case, explain the inference in a separate field. Reject duplicate scenarios and predictions whose expected outcome is too vague to test. The predictor must not read the `ground_truth` or `provenance.ground_truth_sources` fields.

### 4. Construct ground truth

Extract individual feature tests from verifier reports where possible. Keep existing/base tests separate from tests introduced to verify the task. Associate each ground-truth test with its task ID, test identifier, status, source artifact, and a short behavior description when that description can be established from available validation evidence.

Do not infer a test's full assertion from its name alone. Mark unclear cases as `behavior_unknown` and exclude them from semantic recall until they are reviewed. Keep the raw test inventory alongside any human or model-produced descriptions. Do not use the test patch or solution implementation as predictor input.

### 5. Evaluate predictions

Freeze the prediction for a task before exposing its verifier cases to the evaluator. Compare predicted scenarios with ground-truth behaviors using a documented matching rubric:

- A match requires the same behavior and expected outcome; shared keywords are insufficient.
- One prediction may cover multiple test parametrizations of the same behavior, but distinct behaviors require distinct predictions.
- Record exact, partial, and no match, with a short explanation for disputed cases.

Report recall at 5, 10, and 20 predictions where enough cases exist; precision at the same cutoffs; coverage of explicit requirements; and the number of unsupported predictions. Provide per-task results as well as totals. Evaluate an instruction-only run and an instruction-plus-validation-metadata run to determine whether validation information improves predictions. Split future training and evaluation by repository or task family, and keep all runs of one task in the same split.

### 6. Analyze failures and recommend the next iteration

For every evaluated task, list missed behaviors, overly broad predictions, unsupported predictions, and cases that cannot be judged from the available artifacts. Identify whether failures come from requirement extraction, scenario expansion, ranking, or weak ground truth. Recommend changes to the prompt or data collection based on those errors. Propose model training only if the dataset contains enough distinct, well-labeled tasks to support it.

## Deliverables

1. A data audit listing unique tasks, available inputs, and ground-truth quality.
2. A versioned task-record schema and a repeatable extraction command or script.
3. A predictor command or callable interface that reads a task record and writes the structured predictions above.
4. Frozen prediction files for the pilot tasks.
5. An evaluation report with matching decisions, metrics, examples of misses, and limitations.
6. A short recommendation for the next iteration, grounded in the measured errors.

Keep raw benchmark artifacts out of the repository unless they are small, redistributable fixtures. Document how to reproduce extraction from artifact paths supplied by the operator.

## Completion criteria

- A new task can be passed to the predictor using only its instruction and pre-implementation validation information.
- Predictions name concrete observable outcomes, cite instruction evidence, and distinguish explicit requirements from inferred cases.
- The evaluation never passes verifier results, test output, test patches, or solution code to the predictor.
- Repeated runs of the same DeepSWE task are deduplicated in reported sample sizes and splits.
- The report shows individual matching decisions and identifies missing or ambiguous ground truth.
- Another agent can reproduce the pilot from the documented commands and artifact inputs.

Follow `AGENTS.md` for repository changes: work on a feature branch in a dedicated worktree, keep the change scoped, run the required verification suite before pushing, open a pull request, and leave merging to the user.
