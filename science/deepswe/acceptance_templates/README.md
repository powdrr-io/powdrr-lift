# Acceptance criteria template experiment

A standalone science runner for the [template catalog proposal](../../../docs/plans/acceptance-criteria-template-catalog-proposal.md). Its original pilot compares direct acceptance-criteria generation with whole-catalog selection, source-based slot binding, and deterministic rendering. A separate production-routed experiment feeds the production ledger, atomicity judge, splitter, and JEV router into template matching; it does not change the production prompt or Structrr diff paths.

The initial dataset has four tasks from repository families outside the twelve used to design the catalog: cattrs partial structuring, Helm merge strategies, Koota entity snapshots, and dateutil timezone interoperability. There are 91 agent-authored source-based reference validations, prepared before inspecting generated criteria. These are development evaluation labels, not human gold and not an untouched final holdout after this experiment.

The [production-routed run](runs/production-routed-v1/README.md) reruns those tasks after production sentence capture, JEV atomicity classification, planning-model splitting, and JEV routing. Its results are not directly comparable as a causal estimate because the matcher/catalog version and batch size changed; the report preserves the complete classifier and generation traces.

The [catalog retrieval experiment](runs/catalog-retrieval-v1/README.md) compares embedding thresholds, the captured LLM shortlist, JEV top-one suggestions, and their union against both the existing reference template mappings and an independent catalog-coverage audit. It flags disagreements instead of treating model `no_fit` responses as confirmed catalog gaps.

Start with [FOLLOWUP.md](FOLLOWUP.md) for the latest applicability fixes, failed approaches, reviewer calibration, and the mixed full-task results. [PILOT.md](PILOT.md) preserves the original experiment.

## Run an experiment

Use the shared environment from AGENTS.md. From the repository root:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli collect \
  --tasks-dir "$HOME/code/powdrr-deep-swe/tasks" \
  --output-dir /tmp/acceptance-data \
  --tasks cattrs-partial-structuring-recovery helm-array-merge-strategies

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli generate \
  --inputs-dir /tmp/acceptance-data/inputs \
  --output-dir /tmp/acceptance-run \
  --provider deepinfra --model deepseek-ai/DeepSeek-V4-Flash-0731

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli evaluate \
  --inputs-dir /tmp/acceptance-data/inputs \
  --run-dir /tmp/acceptance-run \
  --references-dir science/deepswe/acceptance_templates/data/references \
  --provider deepinfra --model Qwen/Qwen3-Next-80B-A3B-Instruct

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli score \
  --inputs-dir /tmp/acceptance-data/inputs \
  --run-dir /tmp/acceptance-run \
  --references-dir science/deepswe/acceptance_templates/data/references
```

`DEEPINFRA_API_TOKEN` or the provider's equivalent credential must already be set. Keys are never put in request artifacts. Other existing Powdrr providers work through `--provider` / `--model`. The generator and semantic reviewer are configured separately; the pilot uses different model families. No new SDK or dependency is needed.

Do not repurpose HOME or CODEX_HOME for experiments. The only exports above select the approved shared environment and current worktree source.

## Generation and comparison

1. `collect` excludes the catalog development tasks **and their repository families**. `inputs/` holds only task ID and original instruction. A separate manifest records artifact hashes, repository identities, and exclusions. Patch contents never enter generation.
2. In the original pilot, a shared model pass creates an atomic requirement inventory from stable instruction-line spans. It also records context and process statements. The separate production-routed run replaces this step with the production instruction ledger, atomicity/splitting, and route classification before matching.
3. The **direct arm** writes criteria in its own prose from that inventory and the complete instruction.
4. The **template arm** nominates multiple templates per requirement from the 52 short applicability descriptions. A separate binding pass confirms yes/no/unknown and fills typed slots with instruction evidence. One requirement/template pair can have multiple instances. Code renders the sentences and omits unsupported optional clauses. Exact duplicates are merged, with all bindings preserved.
5. Requirements with no generated criterion remain in `prompt.md`. They count as incomplete validation generation, not successful coverage. A generation failure writes a failed artifact with the requirement text retained; it does not prevent other tasks/arms from running. An inventory failure remains visible as an incomplete task.

Both arms use the same generator, shared inventory, batch size, and per-call output budget. The template arm adds a selection call and can produce more criteria. **Total work and final output size are not equalized in this pilot**: use the recorded calls, timings, and sizes when interpreting quality. A later comparison should control total cost and prompt length before claiming an advantage at equal resources. There is no test-count forecast or requested criterion count.

The frozen [catalog.json](catalog.json) contains prose renderers, required/optional semantic slots, applicability descriptions, and near misses. Slot values are strings tagged with semantic kinds; validators check kinds, required slots, source IDs, and placeholders. They do not prove the resulting predicates follow from English. The review is intended to expose those semantic errors.

The follow-up adds explicit prerequisites to T17 (value selection), T18 (collision precedence), and T31 (object identity). Order and read-only/equal-value language do not satisfy those prerequisites. Every accepted instance supplies prerequisite evidence; every filled parameter supplies an explanation and separate verbatim `{source_id, quote}` entries. Separate quotes allow evidence from several clauses without concatenating or inventing source text. Local checks verify quotation provenance, not semantic entailment. Negative/unknown decisions emit no instance; their requirements remain eligible for other templates or the retained-requirement fallback.

Quote validation accepts whitespace/backtick formatting differences and restores the original source quote with absolute offsets. Words, punctuation, and case must match. Raw model responses remain separate from these annotations.

For these three cards, a separate call classifies the source relationship before rendering: effective-value selection versus traversal, a conflict winner versus retaining both inputs, or shared reference versus equal/read-only contents. This call sees the instruction, requirement, and an untrusted operation locator; it does not see the proposed criterion or winner. Code derives template eligibility from the returned relation. Ordinary generation uses its configured generator for this call. A missing/ambiguous relationship or provider failure withholds the instance and continues generation. The original requirement remains available downstream in the experiment's retained-requirement section when no other criterion represents it.

To rebuild the catalog from the approved proposal, run `python -m science.deepswe.acceptance_templates.build_catalog` using the shared environment and RTK. The source document hash is recorded. Use a new run directory if the catalog, model, prompt, schema, or input changes.

## Evaluation

Evaluation reads reference labels separately from generation. It makes two independent kinds of judgments:

- **Coverage:** for each reference validation, does the extracted requirement inventory contain it, and do the final rendered criteria fully, partly, or not at all state it? Several criteria may together cover a validation. Different wording and valid alternative templates are accepted. Neither unrendered slots nor retained raw instruction text receives criterion credit.
- **Support:** for every emitted criterion, are all its mandated assertions supported by the instruction? Status is supported, partly supported, unsupported, or uncertain. A plausible match to a patch test is insufficient; source support must be checked independently.

Review requests contain final criterion IDs/text, rather than unrendered bindings or the generator's explanations. Coverage schemas constrain citation namespaces and the number of verdicts. Those constraints catch format errors; they do not establish semantic correctness.

`evaluate --atomic-support` splits each criterion into its mandated assertions, without giving the splitter the instruction or allowing it to repair the criterion. It then reviews one assertion per request against the full instruction. Code derives the criterion support status from all assertion judgments. This uses more calls and still depends on model judgments, including whether decomposition preserved every assertion. It leaves the existing criterion-level precision denominator unchanged. The targeted cattrs replay demonstrates why this option exists: a rubric reminder alone kept accepting invented flag behavior, while isolated assertion review rejected it.

`score` validates exact review completeness and artifact fingerprints, then calculates:

- Strict recall: fully covered reference validations / reference validations. Partial coverage is reported separately and counts as a miss for strict recall.
- Strict precision: fully supported criterion rows / emitted criterion rows. Partly supported and uncertain rows are not credited. This is **criterion-level precision**; the arms may group predicates differently, so it is not an atomically normalized assertion precision estimate.
- Inventory recall and diagnostic candidate/accepted template-group agreement. Template IDs are not semantic ground truth; a different suitable template can still render a correct criterion.
- All-validations-present per task, unmatched requirements, actual generation calls, elapsed time, reported token usage, and concrete misses/precision flags.

Missing token usage is `null`, not zero. Shared inventory calls are reported separately because both arms reuse them. Completed-only aggregates are accompanied by eligible task counts and incomplete-run details; paired aggregates include only tasks with reviews for both arms.

The report labels reference provenance (`agent_authored`, `model_draft`, or `human_reviewed`) and review provenance (`automated` or `human_reviewed`). Automated scores are estimates. They are not evidence that generated code passes a task's validation patch. No coding agent or task verifier is run by this package.

`review.json` is also the review sheet: inspect its explanations and edit judgments when necessary. Record a reviewer identity and mark `review_status` as `human_reviewed` only after actual human review. Preserve fingerprints and all required rows. Likewise, change reference `label_status` only after reviewing those labels. Rerun `score`, which needs no provider calls.

## Add new DeepSWE tasks with mostly automated labeling

Collect the new task IDs. Draft labels in a separate directory **before generating their criteria**:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli draft-reference \
  --inputs-dir /tmp/new-acceptance-data/inputs \
  --output-dir /tmp/new-acceptance-references \
  --tasks-dir "$HOME/code/powdrr-deep-swe/tasks" \
  --provider deepinfra --model Qwen/Qwen3-Next-80B-A3B-Instruct
```

`--tasks-dir` optionally gives this **label-drafting path only** the validation and solution patches. The drafter must distinguish instruction-recoverable behavior from patch-only or uncertain details. Only instruction-recoverable labels enter required-validation scoring; excluded details remain in the artifact. Drafts are marked `model_draft`, never silently promoted to human labels. Review questionable source recoverability and a random sample of confident labels; errors in the reference inventory can otherwise hide false negatives.

The initial pilot uses prewritten labels rather than this drafter, avoiding a model generating its own answer key after seeing its output. The initial patch spot checks are recorded in [data/patch-spotchecks.json](data/patch-spotchecks.json).

## Applicability and reviewer calibration

The [contrasts](data/contrasts.json) include unsupported identity/precedence matches and positive controls for genuine reference sharing, collision winners, and nearest overrides. Their expected labels and descriptive case IDs are withheld from model calls; task IDs are neutral instruction hashes. These are agent-authored development cases, not human gold.

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase binding --output-dir /tmp/acceptance-binding-contrasts

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase review --review-version v1 --output-dir /tmp/acceptance-review-baseline

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase review --review-version v2 --output-dir /tmp/acceptance-review-current

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase replay --output-dir /tmp/acceptance-pilot-review-replay

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase atomic --output-dir /tmp/acceptance-assertion-review

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.calibration \
  --phase atomic --fixtures science/deepswe/acceptance_templates/data/atomic-controls-neutral.json \
  --output-dir /tmp/acceptance-assertion-controls
```

The replay uses the actual original pilot payloads/schemas. The baseline reviewer prompts are preserved in `data/review-prompts-v1.json`. `--repairs` configures a persisted total attempt budget; raising it explicitly does not overwrite earlier failed attempts. A mismatch exits nonzero and remains in the report, including differences between partial and unsupported that do not change strict precision credit.

To regenerate templates while keeping the original requirement inventories fixed:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.rerun \
  --baseline-runs science/deepswe/acceptance_templates/runs/pilot-v1 \
                  science/deepswe/acceptance_templates/runs/pilot-extra-v1 \
  --tasks helm-array-merge-strategies dateutil-rfc5545-timezone-interop \
  --batch-size 3 --output-dir /tmp/acceptance-evidence-rerun
```

The rerunner validates input and inventory fingerprints, records baseline generation hashes, and makes no inventory or direct-generation calls. Follow with `evaluate` and `score`. Use the same reviewer configuration on original and new outputs when comparing scores. The smaller binding batch and additional evidence are different work budgets; this is a development comparison, not an equal-cost experiment.

To classify relationships on saved bindings without regenerating them, including using a separately configured model:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.apply_guards \
  --baseline-run science/deepswe/acceptance_templates/runs/evidence-v3 \
  --baseline-catalog science/deepswe/acceptance_templates/data/catalog-evidence-v2.json \
  --tasks helm-array-merge-strategies dateutil-rfc5545-timezone-interop \
  --model Qwen/Qwen3-Next-80B-A3B-Instruct --output-dir /tmp/acceptance-relations
```

`--baseline-catalog` permits prerequisite metadata changes only when the old catalog fingerprint matches and every renderer/slot definition is identical. The binding calibration runner offers the same check with `--bindings-from` and `--baseline-catalog`. Saved task bindings must match the original instruction and inventory hashes.

## Failure diagnostics and resume

Every call checkpoints the exact prompt, allowlisted payload, response schema, provider/model, request fingerprint, parsed response, usage, duration, and failure type/detail. Provider error details are scrubbed for credential environment values. A schema correction is a separate persisted attempt. No automatic transport retry occurs during the initial invocation.

Replacing a generation preserves its previous generation/prompt and matching review under `history/<generation_sha256>/`. Review checkpoint namespaces depend on their actual rendered-text inputs and rubric, so changed text receives fresh review calls. Cached calls and superseded correction attempts retain their original elapsed cost. Primary follow-up artifacts stay unpacked; preliminary trials also include `artifacts.tar.gz` and its file-hash manifest to restore parsed outputs from the repository root.

`--resume` reuses completed calls only when their request fingerprint matches and their response still validates. It also reuses completed schema-repair transcripts. An explicit resume after a transport failure consumes the next persisted attempt within `--repairs` (default 1); it preserves the failed attempt and cannot repeatedly reset the budget. Resume mismatches stop with a diagnostic rather than reuse unrelated output.

The model streams through the existing provider client. Its timeout measures socket inactivity, not a hard total request duration. Repetitive output can take longer while chunks keep arriving; output budgets, recorded durations, and failed responses therefore matter when comparing cost.

The committed run artifacts include generated prompts, bindings, reviews, metrics, and compressed full call archives. To resume a committed pilot, extract its `call-checkpoints.tar.gz` from the repository root first; the archive contains repository-relative checkpoint paths. Raw checkpoint directories are kept locally and ignored by Git. `score` does not need extraction.

## Local verification

Run the focused regression checks and explicitly type-check this namespace package:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest -q tests/test_science_acceptance_templates.py
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy --explicit-package-bases \
  science/deepswe/acceptance_templates tests/test_science_acceptance_templates.py
```

The checks protect blinded input boundaries, repository-family exclusion, source offsets, slot/evidence validation, nonblocking no-match fallback, strict metric denominators, incomplete/stale reviews, credential redaction, and reuse of repaired checkpoints.
